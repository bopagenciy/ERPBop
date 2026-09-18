# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional, Set, Tuple

from bop_erp.migration.datasets.profiles import SourceDatasetProfile


def validate_row_structure(
	row: Dict[str, Any],
	profile: SourceDatasetProfile,
	source_row_num: int = 0,
) -> Tuple[bool, List[str], List[str]]:
	"""
	Validates a single source data row against profile constraints.
	Returns (is_valid, errors, warnings).
	"""
	errors: List[str] = []
	warnings: List[str] = []

	# Check required fields
	for req_field in profile.required_fields:
		if req_field not in row or row[req_field] is None or str(row[req_field]).strip() == "":
			errors.append(f"Row {source_row_num}: Required field '{req_field}' is missing or empty.")

	# Check key fields
	for key_field in profile.key_fields:
		if key_field not in row or row[key_field] is None or str(row[key_field]).strip() == "":
			errors.append(f"Row {source_row_num}: Primary key field '{key_field}' is missing or empty.")

	# Check extra columns if not permitted
	if not profile.allow_extra_columns:
		known_cols = set(profile.required_fields).union(profile.key_fields).union(profile.optional_fields).union(profile.field_mappings.keys())
		for col in row.keys():
			if col not in known_cols:
				errors.append(f"Row {source_row_num}: Unexpected column '{col}' not permitted by profile.")

	# Execute profile-specific custom validation rules if any
	for rule in profile.validation_rules:
		try:
			rule_errors = rule(row)
			if rule_errors:
				errors.extend([f"Row {source_row_num}: {re}" for re in rule_errors])
		except Exception as ex:
			errors.append(f"Row {source_row_num}: Custom validation rule exception: {str(ex)}")

	is_valid = len(errors) == 0
	return is_valid, errors, warnings


def validate_cross_dataset_relationship(
	child_entity_type: str,
	child_record: Dict[str, Any],
	parent_item_ids: Set[str],
	supplier_master_ids: Optional[Set[str]] = None,
	location_ids: Optional[Set[str]] = None,
) -> Tuple[str, Optional[str]]:
	"""
	Validates cross-dataset referential integrity without inventing missing parents.
	Classifies the relationship into:
	- VALID: parent entity exists in ingestion scope
	- WARNING: non-blocking advisory
	- ERROR: invalid orphaned entity where parent is mandatory
	- DEFERRED_DEPENDENCY: parent system/table not yet supplied (e.g., Supplier Master)
	- REVIEW_REQUIRED: requires manual operator inspection

	Returns (status, message).
	"""
	item_id = str(child_record.get("Item ID") or child_record.get("item_code") or "").strip()

	# Item Master resolution
	if child_entity_type in (
		"INVENTORY_LOCATION",
		"INVENTORY_SUPPLIER",
		"ITEM_UOM",
		"ITEM_DESCRIPTION",
		"ITEM_SUPPLIER_BY_LOCATION",
	):
		if not item_id:
			return "ERROR", f"Missing Item ID in {child_entity_type} record."
		if parent_item_ids and item_id not in parent_item_ids:
			return "ERROR", f"Item ID '{item_id}' in {child_entity_type} does not exist in Item Master."

	# Supplier Master resolution: if Supplier Master dataset not provided, treat as DEFERRED_DEPENDENCY
	if child_entity_type in ("INVENTORY_SUPPLIER", "ITEM_SUPPLIER_BY_LOCATION"):
		supp_id = child_record.get("Supplier ID") or child_record.get("supplier_id")
		if supp_id:
			if supplier_master_ids is None:
				# Supplier Master dataset has not yet been ingested -> DEFERRED
				return "DEFERRED_DEPENDENCY", f"Supplier Master not provided. Retaining supplier identity '{supp_id}'."
			elif str(supp_id).strip() not in supplier_master_ids:
				return "REVIEW_REQUIRED", f"Supplier ID '{supp_id}' not found in provided Supplier Master."

	# Location Master resolution: if location IDs provided and location missing
	if child_entity_type in ("INVENTORY_LOCATION", "ITEM_SUPPLIER_BY_LOCATION"):
		loc_id = child_record.get("Location ID") or child_record.get("location_id")
		if loc_id and location_ids is not None:
			if str(loc_id).strip() not in location_ids:
				return "REVIEW_REQUIRED", f"Location ID '{loc_id}' not found in known locations."

	return "VALID", None
