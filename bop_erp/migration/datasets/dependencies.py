# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from collections import defaultdict, deque
from typing import Dict, List, Set, Tuple

from bop_erp.migration.datasets.exceptions import (
	DependencyCycleError,
	MissingDependencyError,
)
from bop_erp.migration.datasets.profiles import SourceDatasetProfile


def build_dependency_order(
	profiles: List[SourceDatasetProfile],
	allow_missing_as_deferred: bool = True,
) -> Tuple[List[SourceDatasetProfile], List[str]]:
	"""
	Calculates the deterministic topological execution order for a list of dataset profiles.
	Returns (ordered_profiles, deferred_dependencies).

	Raises:
	- DependencyCycleError if a circular dependency is detected.
	- MissingDependencyError if a dependency is absent and allow_missing_as_deferred is False.
	"""
	profile_map: Dict[str, SourceDatasetProfile] = {p.profile_id: p for p in profiles}
	in_degree: Dict[str, int] = defaultdict(int)
	adj_list: Dict[str, List[str]] = defaultdict(list)
	deferred_deps: Set[str] = set()

	for p in profiles:
		if p.profile_id not in in_degree:
			in_degree[p.profile_id] = 0

		for dep_id in p.dependency_profiles:
			if dep_id not in profile_map:
				if allow_missing_as_deferred:
					deferred_deps.add(dep_id)
				else:
					raise MissingDependencyError(
						f"Profile '{p.profile_id}' requires missing dependency '{dep_id}'."
					)
			else:
				# dep_id must come before p.profile_id
				adj_list[dep_id].append(p.profile_id)
				in_degree[p.profile_id] += 1

	# Kahn's algorithm with deterministic tie-breaking via (import_order, profile_id)
	ready = [
		p.profile_id
		for p in profiles
		if in_degree[p.profile_id] == 0
	]
	ready.sort(key=lambda pid: (profile_map[pid].import_order, pid))

	ordered_ids: List[str] = []

	while ready:
		curr_id = ready.pop(0)
		ordered_ids.append(curr_id)

		for neighbor_id in sorted(adj_list[curr_id], key=lambda nid: (profile_map[nid].import_order, nid)):
			in_degree[neighbor_id] -= 1
			if in_degree[neighbor_id] == 0:
				ready.append(neighbor_id)
				ready.sort(key=lambda pid: (profile_map[pid].import_order, pid))

	if len(ordered_ids) < len(profiles):
		# Cycle detected
		cyclic_ids = [pid for pid in profile_map if pid not in ordered_ids]
		raise DependencyCycleError(
			f"Cyclic dependency detected among dataset profiles: {cyclic_ids}"
		)

	ordered_profiles = [profile_map[pid] for pid in ordered_ids]
	return ordered_profiles, sorted(list(deferred_deps))
