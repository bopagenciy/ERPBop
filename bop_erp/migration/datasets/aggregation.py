# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
import json
from typing import Any, Dict, List, Optional, Set

import frappe


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
		       validation_status, validation_errors_json
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

		# Item ID is either the rec_id directly or the first component of composite key
		item_id = rec_id.split("::")[0] if "::" in rec_id else rec_id

		if item_id not in items:
			items[item_id] = CanonicalSourceItem(item_id=item_id)

		item = items[item_id]

		if ent == "ITEM_MASTER":
			item.master = payload
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
