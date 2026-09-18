# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import frappe

from bop_erp.migration.datasets.exceptions import (
	DatasetParsingError,
	DatasetValidationError,
)
from bop_erp.migration.datasets.parser import stream_dataset_file
from bop_erp.migration.datasets.profiles import SourceDatasetProfile
from bop_erp.migration.datasets.validation import validate_row_structure
from bop_erp.migration.exceptions import SourcePayloadDriftError, StagingError
from bop_erp.migration.namespaces import canonical_source_namespace
from bop_erp.migration.staging import compute_payload_hash, compute_staging_identity


def extract_composite_source_identity(
	row: Dict[str, Any], profile: SourceDatasetProfile, source_row_num: int = 0
) -> str:
	"""
	Extracts a stable business identity key from the configured key fields of the profile.
	Never uses source row numbers, array indices, or offsets as business identity.
	"""
	key_parts: List[str] = []
	for k in profile.key_fields:
		val = row.get(k)
		if val is None or str(val).strip() == "":
			raise DatasetValidationError(
				f"Row {source_row_num}: Key field '{k}' is missing or empty in profile '{profile.profile_id}'."
			)
		key_parts.append(str(val).strip())
	return "::".join(key_parts)


def normalize_dataset_payload(row: Dict[str, Any], profile: SourceDatasetProfile) -> Dict[str, Any]:
	"""
	Produces a normalized payload dictionary based on profile field mappings,
	while keeping original values unchanged and typed.
	"""
	norm: Dict[str, Any] = {}
	for src_col, target_prop in profile.field_mappings.items():
		if src_col in row:
			norm[target_prop] = row[src_col]
	return norm


def stage_dataset_row(
	run_id: str,
	source_file_identifier: str,
	source_sheet: Optional[str],
	source_row_number: int,
	raw_row: Dict[str, Any],
	profile: SourceDatasetProfile,
	company: Optional[str] = None,
) -> Tuple[Any, bool]:
	"""
	Stages a single dataset row idempotently into 'Migration Staging Row'.
	Returns (staging_row_doc, is_new_insert).

	Replay behavior:
	- Identical identity & identical payload -> converges to existing record.
	- Identical identity & differing payload -> raises SourcePayloadDriftError.
	- New identity -> inserts new staging row.
	"""
	# Validate row structure
	is_valid, errors, warnings = validate_row_structure(raw_row, profile, source_row_number)
	val_status = "VALID" if is_valid else "ERROR"

	# Build business identity
	rec_id = extract_composite_source_identity(raw_row, profile, source_row_number)

	# Canonical namespace
	run_doc = None
	if hasattr(frappe, "db") and frappe.db and frappe.db.exists("Migration Run", run_id):
		run_doc = frappe.get_cached_doc("Migration Run", run_id)
	target_company = company or (run_doc.company if run_doc else None)
	source_system = profile.source_system
	source_instance = run_doc.source_instance_id if run_doc else "DEFAULT"
	sys, inst = canonical_source_namespace(source_system, source_instance)

	identity_key = compute_staging_identity(sys, inst, profile.entity_type, rec_id, company=target_company)
	raw_hash = compute_payload_hash(raw_row)
	raw_json = json.dumps(raw_row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

	norm_payload = normalize_dataset_payload(raw_row, profile)
	norm_hash = compute_payload_hash(norm_payload)
	norm_json = json.dumps(norm_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

	# Check for existing record within this run (idempotency / drift check)
	if hasattr(frappe, "db") and frappe.db:
		existing = frappe.db.get_value(
			"Migration Staging Row",
			{"migration_run": run_id, "staging_identity_key": identity_key},
			["name", "source_payload_hash"],
			as_dict=True,
		)
		if existing:
			if existing.source_payload_hash == raw_hash:
				# Identical replay convergence
				doc = frappe.get_doc("Migration Staging Row", existing.name)
				return doc, False
			else:
				# Conflict / drift within same run
				raise SourcePayloadDriftError(
					f"Source payload drift detected on replay within run for {profile.entity_type} '{rec_id}'. "
					f"Expected hash {existing.source_payload_hash[:12]}..., got {raw_hash[:12]}..."
				)

		# Cross-run snapshot comparison
		prior_snapshot = frappe.db.sql(
			"""
			SELECT r.name, r.source_payload_hash
			FROM `tabMigration Staging Row` r
			INNER JOIN `tabMigration Run` m ON r.migration_run = m.name
			WHERE m.source_system = %s
			  AND m.source_instance_id = %s
			  AND r.entity_type = %s
			  AND r.source_record_id = %s
			  AND r.migration_run != %s
			ORDER BY r.creation DESC, r.name DESC
			LIMIT 1
			""",
			(sys, inst, profile.entity_type.upper(), rec_id, run_id),
			as_dict=True,
		)
		if prior_snapshot:
			snapshot_state = "UNCHANGED" if prior_snapshot[0].source_payload_hash == raw_hash else "CHANGED"
		else:
			snapshot_state = "NEW"

		doc_data = {
			"doctype": "Migration Staging Row",
			"migration_run": run_id,
			"entity_type": profile.entity_type.upper(),
			"source_record_id": rec_id,
			"staging_identity_key": identity_key,
			"snapshot_state": snapshot_state,
			"validation_status": val_status,
			"import_status": "PENDING",
			"source_payload_hash": raw_hash,
			"normalized_payload_hash": norm_hash,
			"source_payload_json": raw_json,
			"normalized_payload_json": norm_json,
			"validation_errors_json": json.dumps(errors),
			"validation_warnings_json": json.dumps(warnings),
		}

		# Optional metadata fields if present on DocType
		staging_meta = frappe.get_meta("Migration Staging Row")
		if staging_meta.has_field("source_profile"):
			doc_data["source_profile"] = profile.profile_id
		if staging_meta.has_field("profile_version"):
			doc_data["profile_version"] = profile.version
		if staging_meta.has_field("source_file_identifier"):
			doc_data["source_file_identifier"] = source_file_identifier
		if staging_meta.has_field("source_sheet"):
			doc_data["source_sheet"] = source_sheet or ""
		if staging_meta.has_field("source_row_number"):
			doc_data["source_row_number"] = source_row_number

		row_doc = frappe.get_doc(doc_data)
		row_doc.insert(ignore_permissions=True)
		return row_doc, True
	else:
		# Offline / mock return
		mock_doc = type("StagingRowMock", (), {
			"name": f"STG-MOCK-{identity_key[:8]}",
			"source_record_id": rec_id,
			"staging_identity_key": identity_key,
			"validation_status": val_status,
			"source_payload_hash": raw_hash,
			"normalized_payload_hash": norm_hash,
			"source_payload_json": raw_json,
			"normalized_payload_json": norm_json,
		})()
		return mock_doc, True


def stage_dataset_file(
	run_id: str,
	file_path: Union[str, Path],
	profile: SourceDatasetProfile,
	company: Optional[str] = None,
	max_rows: Optional[int] = None,
) -> Dict[str, Any]:
	"""
	Streams and stages an entire dataset file using bounded memory.
	Returns summary metrics of the staging run for this file.
	"""
	path = Path(file_path)
	file_id = path.name
	sheet_name = profile.sheet_name

	total_read = 0
	inserted_count = 0
	replayed_count = 0
	valid_count = 0
	error_count = 0

	for source_row_num, raw_row in stream_dataset_file(path, profile):
		if max_rows and total_read >= max_rows:
			break
		total_read += 1

		doc, is_new = stage_dataset_row(
			run_id=run_id,
			source_file_identifier=file_id,
			source_sheet=sheet_name,
			source_row_number=source_row_num,
			raw_row=raw_row,
			profile=profile,
			company=company,
		)

		if is_new:
			inserted_count += 1
		else:
			replayed_count += 1

		if getattr(doc, "validation_status", "VALID") == "VALID":
			valid_count += 1
		else:
			error_count += 1

	return {
		"file_identifier": file_id,
		"profile_id": profile.profile_id,
		"total_rows": total_read,
		"inserted_rows": inserted_count,
		"replayed_rows": replayed_count,
		"valid_rows": valid_count,
		"error_rows": error_count,
	}
