# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from bop_erp.migration.datasets.exceptions import FieldAuthorityConflictError


class FieldAuthorityPolicy(str, Enum):
	SOURCE_AUTHORITATIVE = "SOURCE_AUTHORITATIVE"
	BOP_AUTHORITATIVE = "BOP_AUTHORITATIVE"
	MERGE = "MERGE"
	REVIEW_ON_CONFLICT = "REVIEW_ON_CONFLICT"
	IMPORT_ONCE = "IMPORT_ONCE"


class AuthorityDecision(str, Enum):
	APPLY_SOURCE = "APPLY_SOURCE"
	KEEP_BOP = "KEEP_BOP"
	MERGED = "MERGED"
	REVIEW_REQUIRED = "REVIEW_REQUIRED"


def evaluate_field_authority(
	field_name: str,
	current_bop_val: Any,
	incoming_source_val: Any,
	policy: FieldAuthorityPolicy,
	is_initial_import: bool = False,
) -> Tuple[AuthorityDecision, Any, str]:
	"""
	Deterministic decision function to resolve field values between existing ERP state
	and incoming reimported dataset values according to specified policy.

	Returns (AuthorityDecision, resolved_value, reason_description).
	"""
	# If BOP value does not exist or is None/empty, incoming value always applies
	if current_bop_val is None or current_bop_val == "":
		return AuthorityDecision.APPLY_SOURCE, incoming_source_val, "Bop value is empty; applying incoming source value."

	# If values are identical, no conflict exists
	if current_bop_val == incoming_source_val:
		return AuthorityDecision.KEEP_BOP, current_bop_val, "Values are identical."

	if policy == FieldAuthorityPolicy.SOURCE_AUTHORITATIVE:
		return AuthorityDecision.APPLY_SOURCE, incoming_source_val, f"Field '{field_name}' is SOURCE_AUTHORITATIVE."

	elif policy == FieldAuthorityPolicy.BOP_AUTHORITATIVE:
		return AuthorityDecision.KEEP_BOP, current_bop_val, f"Field '{field_name}' is BOP_AUTHORITATIVE; preserving Bop ERP value."

	elif policy == FieldAuthorityPolicy.IMPORT_ONCE:
		if is_initial_import:
			return AuthorityDecision.APPLY_SOURCE, incoming_source_val, "Initial import allowed for IMPORT_ONCE field."
		else:
			return AuthorityDecision.KEEP_BOP, current_bop_val, f"Field '{field_name}' is IMPORT_ONCE; already populated."

	elif policy == FieldAuthorityPolicy.MERGE:
		# If both are lists or sets, combine them
		if isinstance(current_bop_val, (list, tuple)) and isinstance(incoming_source_val, (list, tuple)):
			merged_list = list(current_bop_val)
			for item in incoming_source_val:
				if item not in merged_list:
					merged_list.append(item)
			return AuthorityDecision.MERGED, merged_list, f"Field '{field_name}' merged lists."
		elif isinstance(current_bop_val, dict) and isinstance(incoming_source_val, dict):
			merged_dict = dict(current_bop_val)
			merged_dict.update(incoming_source_val)
			return AuthorityDecision.MERGED, merged_dict, f"Field '{field_name}' merged dictionaries."
		else:
			# Fallback for scalar merge: keep BOP, register review if different
			return AuthorityDecision.KEEP_BOP, current_bop_val, f"Field '{field_name}' scalar values merged by keeping Bop master."

	elif policy == FieldAuthorityPolicy.REVIEW_ON_CONFLICT:
		return (
			AuthorityDecision.REVIEW_REQUIRED,
			current_bop_val,
			f"Conflict on '{field_name}': current '{current_bop_val}' vs incoming '{incoming_source_val}'. Review required.",
		)

	return AuthorityDecision.REVIEW_REQUIRED, current_bop_val, f"Unknown policy '{policy}' for field '{field_name}'."
