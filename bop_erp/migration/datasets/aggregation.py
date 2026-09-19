# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from enum import Enum
import json
from typing import Any, Dict, List, Optional, Set

import frappe


class SourceDataClass(str, Enum):
	CLIENT_SAMPLE = "CLIENT_SAMPLE"
	SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


@dataclass
class CanonicalSourceItem:
	"""
	Provider-neutral offline aggregate representing an Item across all ingested datasets.
	Used for validation, preview, dry run, quality reporting, and cross-dataset reconciliation.
	Never mutates ERPNext DocTypes directly.
	"""

	item_id: str
	master: Dict[str, Any] = field(default_factory=dict)
	descriptions: List[Dict[str, Any]] = field(default_factory=list)
	uoms: List[Dict[str, Any]] = field(default_factory=list)
	locations: List[Dict[str, Any]] = field(default_factory=list)
	suppliers: List[Dict[str, Any]] = field(default_factory=list)
	supplier_location_overrides: List[Dict[str, Any]] = field(default_factory=list)
	provenance: Dict[str, Any] = field(default_factory=dict)
	validation_findings: List[Dict[str, Any]] = field(default_factory=list)
	completeness_status: str = "PENDING"
	source_data_class: str = SourceDataClass.CLIENT_SAMPLE.value

	def to_dict(self) -> Dict[str, Any]:
		return {
			"item_id": self.item_id,
			"master": dict(self.master),
			"descriptions": list(self.descriptions),
			"uoms": list(self.uoms),
			"locations": list(self.locations),
			"suppliers": list(self.suppliers),
			"supplier_location_overrides": list(self.supplier_location_overrides),
			"provenance": dict(self.provenance),
			"validation_findings": list(self.validation_findings),
			"completeness_status": self.completeness_status,
			"source_data_class": self.source_data_class,
		}


def classify_item_completeness(item: CanonicalSourceItem) -> str:
	"""
	Classifies the completeness status of an Item based on its component presence.
	Statuses:
	- COMPLETE: Has master, uom, location, supplier, and description
	- NOT_STOCKED: Has master but no locations assigned
	- MISSING_LOCATION: Missing location records
	- MISSING_UOM: Missing unit of measure records
	- MISSING_SUPPLIER: Missing supplier records
	- MISSING_DESCRIPTION: Missing extended description records
	- PARTIAL: Has master and at least one related record, but missing others
	- INVALID: Missing master or structural validation failures
	"""
	if not item.master:
		return "INVALID"

	has_loc = len(item.locations) > 0
	has_uom = len(item.uoms) > 0
	has_supp = len(item.suppliers) > 0
	has_desc = len(item.descriptions) > 0

	if has_loc and has_uom and has_supp and has_desc:
		return "COMPLETE"

	# Specific single-missing classifications
	if not has_loc and has_uom and has_supp and has_desc:
		return "NOT_STOCKED"
	if not has_uom and has_loc and has_supp and has_desc:
		return "MISSING_UOM"
	if not has_supp and has_loc and has_uom and has_desc:
		return "MISSING_SUPPLIER"
	if not has_desc and has_loc and has_uom and has_supp:
		return "MISSING_DESCRIPTION"

	# If missing multiple components but has master:
	return "PARTIAL"


def build_canonical_items_from_staging(
	run_id: str,
) -> Dict[str, CanonicalSourceItem]:
	"""
	Assembles CanonicalSourceItem aggregates from staged rows for a given Migration Run.
	Streams from database without loading unnecessary duplicates.
	"""
	items: Dict[str, CanonicalSourceItem] = {}

	if not (hasattr(frappe, "db") and frappe.db):
		return items

	# Fetch staged rows ordered deterministically
	staged_rows = frappe.db.sql(
		"""
		SELECT entity_type, source_record_id, source_payload_json,
		       validation_status, validation_errors_json,
		       source_file_identifier, source_row_number,
		       source_profile, profile_version
		FROM `tabMigration Staging Row`
		WHERE migration_run = %s
		ORDER BY creation ASC, name ASC
		""",
		(run_id,),
		as_dict=True,
	)

	for r in staged_rows:
		ent = r.entity_type
		rec_id = r.source_record_id
		payload = json.loads(r.source_payload_json or "{}")

		# Item ID is extracted using canonical parser
		from bop_erp.migration.datasets.staging import extract_item_id_from_record_id
		item_id = extract_item_id_from_record_id(rec_id)

		src_file = r.get("source_file_identifier") or ""
		is_client = any(k in src_file.lower() for k in ("sample", "local_data", "p21_samples"))
		data_class = SourceDataClass.CLIENT_SAMPLE.value if is_client else SourceDataClass.SYNTHETIC_FIXTURE.value

		if item_id not in items:
			items[item_id] = CanonicalSourceItem(item_id=item_id, source_data_class=data_class)

		item = items[item_id]

		if ent == "ITEM_MASTER":
			item.master = payload
			item.source_data_class = data_class
			item.provenance.update({
				"source_data_class": data_class,
				"source_item_id": item_id,
				"source_file": src_file,
				"source_row": r.get("source_row_number"),
				"profile_id": r.get("source_profile"),
				"profile_version": r.get("profile_version"),
				"migration_run": run_id,
				"canonical_source_identity": rec_id,
			})
		elif ent == "ITEM_DESCRIPTION":
			item.descriptions.append(payload)
		elif ent == "ITEM_UOM":
			item.uoms.append(payload)
		elif ent == "INVENTORY_LOCATION":
			item.locations.append(payload)
		elif ent == "INVENTORY_SUPPLIER":
			item.suppliers.append(payload)
		elif ent == "ITEM_SUPPLIER_BY_LOCATION":
			item.supplier_location_overrides.append(payload)

		item.provenance.setdefault("datasets", {})[ent] = {
			"source_file": src_file,
			"source_row": r.get("source_row_number"),
			"profile_id": r.get("source_profile"),
			"canonical_source_identity": rec_id,
		}

		if r.validation_status == "ERROR":
			item.validation_findings.append({
				"entity_type": ent,
				"source_record_id": rec_id,
				"errors": json.loads(r.validation_errors_json or "[]"),
			})

	# Classify completeness for each item
	for item in items.values():
		if item.validation_findings:
			item.completeness_status = "INVALID"
		else:
			item.completeness_status = classify_item_completeness(item)

	return items
