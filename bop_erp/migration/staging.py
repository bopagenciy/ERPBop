# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe import _

from bop_erp.migration.adapters.base import SourceAdapter
from bop_erp.migration.exceptions import (
	SourcePayloadDriftError,
	StagingError,
)
from bop_erp.migration.namespaces import (
	canonical_source_instance_id,
	canonical_source_namespace,
	canonical_source_system,
)


def compute_staging_identity(
	source_system: str,
	source_instance_id: str,
	entity_type: str,
	source_record_id: str,
	company: Optional[str] = None,
) -> str:
	"""
	Computes the canonical SHA-256 staging identity key for a source record.
	Uses canonical source namespace.
	Identity tuple: [opt(company), source_system, source_instance_id, entity_type, source_record_id].
	"""
	sys, inst = canonical_source_namespace(source_system, source_instance_id)
	ent = str(entity_type).strip().upper()
	rec = str(source_record_id).strip()

	if company and str(company).strip():
		identity_tuple = [
			str(company).strip(),
			sys,
			inst,
			ent,
			rec,
		]
	else:
		identity_tuple = [
			sys,
			inst,
			ent,
			rec,
		]
	canonical_json = json.dumps(identity_tuple, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def compute_payload_hash(payload: Dict[str, Any]) -> str:
	"""
	Computes the deterministic SHA-256 hash of a payload dictionary.
	Keys are sorted and ASCII is preserved with compact separators.
	"""
	if not isinstance(payload, dict):
		raise StagingError("Payload must be a dictionary.")
	canonical_json = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def extract_record_id(record: Dict[str, Any], entity_type: str) -> str:
	"""Extracts the primary identifier from a raw source record dictionary."""
	candidates = ["id", "source_record_id", "external_id", "code", "sku"]
	for c in candidates:
		if c in record and str(record[c]).strip():
			return str(record[c]).strip()
	raise StagingError(f"Could not extract primary ID from {entity_type} source record: {list(record.keys())}")


def stage_source_record(
	run_id: str,
	source_system: str,
	source_instance_id: str,
	entity_type: str,
	source_record: Dict[str, Any],
	source_parent_id: Optional[str] = None,
) -> Tuple[Any, bool]:
	"""
	Stages a single raw source record idempotently into 'Migration Staging Row'.
	Returns (staging_row_doc, is_new_insert).

	Replay behavior:
	- Identical identity & identical payload -> converges to same record (is_new_insert=False).
	- Identical identity & differing payload -> detects payload drift (raises SourcePayloadDriftError).
	- New identity -> inserts new row.
	"""
	# Resolve run company and canonicalize source namespace
	run_doc = frappe.get_cached_doc("Migration Run", run_id) if hasattr(frappe, "db") and frappe.db else None
	run_company = run_doc.company if run_doc else None
	sys, inst = canonical_source_namespace(
		source_system or (run_doc.source_system if run_doc else "DEFAULT"),
		source_instance_id or (run_doc.source_instance_id if run_doc else "DEFAULT"),
	)

	rec_id = extract_record_id(source_record, entity_type)
	identity_key = compute_staging_identity(sys, inst, entity_type, rec_id, company=run_company)
	payload_hash = compute_payload_hash(source_record)
	payload_json = json.dumps(source_record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

	# Check for existing record within this run
	existing = frappe.db.get_value(
		"Migration Staging Row",
		{"migration_run": run_id, "staging_identity_key": identity_key},
		["name", "source_payload_hash"],
		as_dict=True,
	)

	if existing:
		if existing.source_payload_hash == payload_hash:
			# Replay convergence within same run: identical payload, return existing document without mutating
			doc = frappe.get_doc("Migration Staging Row", existing.name)
			return doc, False
		else:
			# Drift detected on replay within same run
			raise SourcePayloadDriftError(
				f"Source payload drift detected on replay within run for {entity_type} '{rec_id}' (identity {identity_key[:12]}). "
				f"Expected hash {existing.source_payload_hash[:12]}..., got {payload_hash[:12]}..."
			)

	# Cross-run snapshot comparison: scoped by (company, canonical source_system, canonical source_instance_id)
	if run_company:
		prior_snapshot = frappe.db.sql(
			"""
			SELECT r.name, r.source_payload_hash, r.creation
			FROM `tabMigration Staging Row` r
			INNER JOIN `tabMigration Run` m ON r.migration_run = m.name
			WHERE m.company = %s
			  AND m.source_system = %s
			  AND m.source_instance_id = %s
			  AND r.entity_type = %s
			  AND r.source_record_id = %s
			  AND r.migration_run != %s
			ORDER BY r.creation DESC, r.name DESC
			LIMIT 1
			""",
			(run_company, sys, inst, entity_type.upper(), rec_id, run_id),
			as_dict=True,
		)
	else:
		prior_snapshot = frappe.db.sql(
			"""
			SELECT r.name, r.source_payload_hash, r.creation
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
			(sys, inst, entity_type.upper(), rec_id, run_id),
			as_dict=True,
		)

	if prior_snapshot:
		if prior_snapshot[0].source_payload_hash == payload_hash:
			snapshot_state = "UNCHANGED"
		else:
			snapshot_state = "CHANGED"
	else:
		snapshot_state = "NEW"

	row_doc = frappe.get_doc({
		"doctype": "Migration Staging Row",
		"migration_run": run_id,
		"entity_type": entity_type.upper(),
		"source_record_id": rec_id,
		"source_parent_id": source_parent_id,
		"staging_identity_key": identity_key,
		"snapshot_state": snapshot_state,
		"source_payload_hash": payload_hash,
		"source_payload_json": payload_json,
		"validation_status": "PENDING",
		"import_status": "PENDING",
	})
	row_doc.insert(ignore_permissions=True)

	# Update total_rows on Migration Run
	frappe.db.set_value(
		"Migration Run",
		run_id,
		"total_rows",
		frappe.db.count("Migration Staging Row", {"migration_run": run_id}),
	)

	return row_doc, True


def extract_and_stage_from_adapter(
	adapter: SourceAdapter,
	run_id: str,
	entity_types: Optional[List[str]] = None,
	limit_per_type: Optional[int] = None,
) -> Dict[str, int]:
	"""
	Extracts data from the given SourceAdapter and stages all records into Migration Staging Row.
	"""
	target_types = [t.upper() for t in (entity_types or ["CUSTOMER", "VENDOR", "ITEM", "WAREHOUSE"])]
	staged_counts = {t: 0 for t in target_types}

	# Verify Run exists and is in EXTRACTING status
	run_doc = frappe.get_doc("Migration Run", run_id)
	if run_doc.status not in ("DRAFT", "EXTRACTING"):
		raise StagingError(f"Migration Run '{run_id}' is in status '{run_doc.status}'. Cannot stage records.")

	if run_doc.status == "DRAFT":
		run_doc.status = "EXTRACTING"
		run_doc.save(ignore_permissions=True)

	if "CUSTOMER" in target_types:
		for raw in adapter.stream_customers(limit=limit_per_type):
			stage_source_record(
				run_id=run_id,
				source_system=adapter.source_system,
				source_instance_id=adapter.source_instance_id,
				entity_type="CUSTOMER",
				source_record=raw,
			)
			staged_counts["CUSTOMER"] += 1

	if "VENDOR" in target_types:
		for raw in adapter.stream_vendors(limit=limit_per_type):
			stage_source_record(
				run_id=run_id,
				source_system=adapter.source_system,
				source_instance_id=adapter.source_instance_id,
				entity_type="VENDOR",
				source_record=raw,
			)
			staged_counts["VENDOR"] += 1

	if "ITEM" in target_types:
		for raw in adapter.stream_items(limit=limit_per_type):
			stage_source_record(
				run_id=run_id,
				source_system=adapter.source_system,
				source_instance_id=adapter.source_instance_id,
				entity_type="ITEM",
				source_record=raw,
			)
			staged_counts["ITEM"] += 1

	if "WAREHOUSE" in target_types:
		for raw in adapter.stream_warehouses(limit=limit_per_type):
			stage_source_record(
				run_id=run_id,
				source_system=adapter.source_system,
				source_instance_id=adapter.source_instance_id,
				entity_type="WAREHOUSE",
				source_record=raw,
				source_parent_id=raw.get("parent_id"),
			)
			staged_counts["WAREHOUSE"] += 1

	# Transition Run to STAGED
	run_doc.reload()
	run_doc.status = "STAGED"
	run_doc.total_rows = frappe.db.count("Migration Staging Row", {"migration_run": run_id})
	run_doc.save(ignore_permissions=True)
	frappe.db.commit()

	return staged_counts
