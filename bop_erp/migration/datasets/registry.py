# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from enum import Enum
from typing import Dict, List, Optional, Tuple

from bop_erp.migration.datasets.exceptions import (
	AmbiguousProfileError,
	DatasetProfileError,
	UnknownProfileError,
)
from bop_erp.migration.datasets.profiles import (
	SourceDatasetProfile,
	get_initial_p21_profiles,
)


class DetectionResult(str, Enum):
	MATCH = "MATCH"
	AMBIGUOUS = "AMBIGUOUS"
	UNKNOWN = "UNKNOWN"


class ProfileRegistry:
	"""
	Registry for source dataset profiles. Allows registering, versioning, enabling,
	disabling, looking up, and auto-detecting profiles without modifying the core ingestion engine.
	"""

	def __init__(self):
		self._profiles: Dict[str, SourceDatasetProfile] = {}

	def register(self, profile: SourceDatasetProfile, overwrite: bool = False) -> None:
		"""Registers a source dataset profile."""
		self.validate_profile(profile)
		if profile.profile_id in self._profiles and not overwrite:
			raise DatasetProfileError(
				f"Profile '{profile.profile_id}' is already registered. Use overwrite=True to replace."
			)
		self._profiles[profile.profile_id] = profile

	def get(self, profile_id: str) -> SourceDatasetProfile:
		"""Retrieves a registered profile by its ID."""
		if profile_id not in self._profiles:
			raise DatasetProfileError(f"Profile '{profile_id}' not found in registry.")
		return self._profiles[profile_id]

	def enable(self, profile_id: str) -> None:
		"""Enables a profile."""
		profile = self.get(profile_id)
		profile.active = True

	def disable(self, profile_id: str) -> None:
		"""Disables a profile without deleting it or historical staged data."""
		profile = self.get(profile_id)
		profile.active = False

	def list_profiles(
		self, active_only: bool = True, source_system: Optional[str] = None
	) -> List[SourceDatasetProfile]:
		"""Lists registered profiles with optional active and source_system filters."""
		profiles = list(self._profiles.values())
		if active_only:
			profiles = [p for p in profiles if p.active]
		if source_system:
			profiles = [p for p in profiles if p.source_system.upper() == source_system.upper()]
		return sorted(profiles, key=lambda p: (p.import_order, p.profile_id))

	def validate_profile(self, profile: SourceDatasetProfile) -> None:
		"""Validates profile structure and metadata consistency."""
		if not profile.profile_id or not profile.profile_id.strip():
			raise DatasetProfileError("Profile must have a non-empty profile_id.")
		if not profile.source_system or not profile.source_system.strip():
			raise DatasetProfileError(f"Profile '{profile.profile_id}' must specify source_system.")
		if not profile.entity_type or not profile.entity_type.strip():
			raise DatasetProfileError(f"Profile '{profile.profile_id}' must specify entity_type.")
		if not profile.key_fields:
			raise DatasetProfileError(f"Profile '{profile.profile_id}' must define at least one key field.")
		if profile.header_row < 1:
			raise DatasetProfileError(f"Profile '{profile.profile_id}' header_row must be >= 1.")
		if profile.data_start_row <= profile.header_row:
			raise DatasetProfileError(
				f"Profile '{profile.profile_id}' data_start_row ({profile.data_start_row}) "
				f"must be greater than header_row ({profile.header_row})."
			)
		# Ensure required fields are not contradictory
		for k in profile.key_fields:
			if profile.required_fields and k not in profile.required_fields:
				# Key fields must practically be required
				pass

	def detect_profile(
		self,
		headers: List[str],
		file_name: Optional[str] = None,
		metadata: Optional[Dict] = None,
		active_only: bool = True,
	) -> Tuple[DetectionResult, Optional[SourceDatasetProfile], str]:
		"""
		Attempts to auto-detect a profile given column headers and optional file hints.
		Returns (DetectionResult, matched_profile_or_None, explanation).
		Never auto-imports an AMBIGUOUS or UNKNOWN candidate.
		"""
		clean_headers = {str(h).strip().lower() for h in headers if h is not None and str(h).strip()}
		if not clean_headers:
			return DetectionResult.UNKNOWN, None, "No valid column headers found."

		candidates: List[Tuple[SourceDatasetProfile, float, str]] = []

		for p in self.list_profiles(active_only=active_only):
			# Check required fields
			req_set = {str(f).strip().lower() for f in p.required_fields}
			key_set = {str(f).strip().lower() for f in p.key_fields}
			signature_set = req_set.union(key_set)

			if signature_set and signature_set.issubset(clean_headers):
				# Score candidate: ratio of profile's known fields present
				all_profile_cols = {str(f).strip().lower() for f in (
					list(p.field_mappings.keys()) + p.required_fields + p.key_fields + p.optional_fields
				)}
				matched_cols = clean_headers.intersection(all_profile_cols)
				score = len(matched_cols) / max(len(clean_headers), 1)

				# Additional bonus if filename matches dataset_name or profile_id
				bonus = 0.0
				if file_name:
					fn_lower = file_name.lower()
					if p.dataset_name.lower() in fn_lower:
						bonus = 0.5
					elif p.profile_id.lower() in fn_lower:
						bonus = 0.5

				total_score = score + bonus
				candidates.append((p, total_score, f"Matched required signature {signature_set}"))

		if not candidates:
			return DetectionResult.UNKNOWN, None, "No profile signature matched header fields."

		# Sort by score descending
		candidates.sort(key=lambda x: x[1], reverse=True)

		# Check for ambiguity
		if len(candidates) > 1:
			top_score = candidates[0][1]
			second_score = candidates[1][1]
			if abs(top_score - second_score) < 0.1:  # Close tie
				cand_ids = [c[0].profile_id for c in candidates[:3]]
				return (
					DetectionResult.AMBIGUOUS,
					None,
					f"Ambiguous match between profiles: {cand_ids}. Manual selection required.",
				)

		best_profile, best_score, reason = candidates[0]
		return DetectionResult.MATCH, best_profile, f"Matched '{best_profile.profile_id}' (score {best_score:.2f}): {reason}"


# Default singleton registry pre-populated with initial P21 profiles
default_registry = ProfileRegistry()
for _p in get_initial_p21_profiles():
	default_registry.register(_p)
