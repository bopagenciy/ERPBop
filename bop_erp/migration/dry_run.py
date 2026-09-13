# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from typing import Any, Dict, List

import frappe
from frappe import _

from bop_erp.constants import ExternalEntityType
from bop_erp.migration.exceptions import DryRunError

TARGET_TABLES_TO_VERIFY = [
	"Customer",
	"Supplier",
	"Item",
	"Warehouse",
	"Address",
	"Contact",
	"Bin",
	"Stock Ledger Entry",
	"GL Entry",
	"External ID Mapping",
]


def execute_dry_run(run_id: str) -> Dict[str, Any]:
	"""
	Executes a read-only dry-run preview for a Migration Run.
	Calculates would_create, would_update, would_skip, would_error, would_warn per entity type.
	Guarantees absolutely ZERO mutations across all target ERP tables.
	"""
	run_doc = frappe.get_doc("Migration Run", run_id)

	# Snapshot baseline table counts before dry run
	baseline_counts = {t: frappe.db.count(t) for t in TARGET_TABLES_TO_VERIFY}

	rows = frappe.get_all(
		"Migration Staging Row",
		filters={"migration_run": run_id},
		fields=[
			"name",
			"entity_type",
			"source_record_id",
			"validation_status",
			"normalized_payload_json",
			"target_doctype",
		],
	)

	entity_types = ["CUSTOMER", "VENDOR", "ITEM", "WAREHOUSE"]
	by_entity = {
		e: {
			"would_create": 0,
			"would_update": 0,
			"would_skip": 0,
			"would_error": 0,
			"would_warn": 0,
		}
		for e in entity_types
	}

	summary = {
		"would_create": 0,
		"would_update": 0,
		"would_skip": 0,
		"would_error": 0,
		"would_warn": 0,
	}

	for r in rows:
		e_type = r.entity_type
		stats = by_entity.setdefault(
			e_type,
			{"would_create": 0, "would_update": 0, "would_skip": 0, "would_error": 0, "would_warn": 0},
		)

		if r.validation_status == "ERROR":
			stats["would_error"] += 1
			summary["would_error"] += 1
			continue

		if r.validation_status == "WARNING":
			stats["would_warn"] += 1
			summary["would_warn"] += 1

		# Check existing mapping or document in target ERP
		candidate = json.loads(r.normalized_payload_json or "{}")
		target_doc_exists = False

		# 1. Check External ID Mapping
		map_type = ExternalEntityType.PRODUCT if e_type == "ITEM" else (ExternalEntityType.VENDOR if e_type == "VENDOR" else e_type)
		existing_map = frappe.db.get_value(
			"External ID Mapping",
			{
				"provider": run_doc.source_system,
				"external_entity_type": map_type,
				"external_id": r.source_record_id,
				"active": 1,
			},
			["name", "erp_doctype", "erp_document"],
			as_dict=True,
		)

		if existing_map and existing_map.erp_document:
			if frappe.db.exists(existing_map.erp_doctype, existing_map.erp_document):
				target_doc_exists = True

		# 2. Check direct target doctype existence if not mapped
		if not target_doc_exists:
			if e_type == "ITEM" and candidate.get("item_code"):
				target_doc_exists = bool(frappe.db.exists("Item", candidate["item_code"]))
			elif e_type == "WAREHOUSE" and candidate.get("warehouse_name"):
				target_doc_exists = bool(
					frappe.db.exists("Warehouse", {"warehouse_name": candidate["warehouse_name"], "company": run_doc.company})
				)
			elif e_type == "CUSTOMER" and candidate.get("customer_name"):
				target_doc_exists = bool(
					frappe.db.exists("Customer", {"customer_name": candidate["customer_name"]})
				)
			elif e_type == "VENDOR" and candidate.get("supplier_name"):
				target_doc_exists = bool(
					frappe.db.exists("Supplier", {"supplier_name": candidate["supplier_name"]})
				)

		if target_doc_exists:
			stats["would_skip"] += 1
			summary["would_skip"] += 1
		else:
			stats["would_create"] += 1
			summary["would_create"] += 1

	# Snapshot counts after dry run to verify zero mutations
	post_counts = {t: frappe.db.count(t) for t in TARGET_TABLES_TO_VERIFY}
	deltas = {t: post_counts[t] - baseline_counts[t] for t in TARGET_TABLES_TO_VERIFY}

	for t, delta in deltas.items():
		if delta != 0:
			raise DryRunError(f"CRITICAL: Dry-run mutated table '{t}' with delta {delta}! Dry run must be read-only.")

	return {
		"run_id": run_id,
		"dry_run": True,
		"summary": summary,
		"by_entity_type": by_entity,
		"target_table_deltas": deltas,
	}
