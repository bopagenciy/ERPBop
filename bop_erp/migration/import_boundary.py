# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from typing import Any, Dict, Optional

import frappe
from frappe import _

from bop_erp.constants import ExternalEntityType
from bop_erp.migration.exceptions import ImportBoundaryError
from bop_erp.migration.namespaces import (
	canonical_company_tag,
	canonical_provider,
	canonical_source_namespace,
	compute_migration_channel_id,
)


def get_or_create_migration_channel(
	company: str,
	source_system: str,
	source_instance_id: Optional[str] = None,
) -> str:
	"""
	Ensures a persistent Sales Channel exists for scoping migration External ID Mappings,
	uniquely keyed by (company, source_system, source_instance_id).
	"""
	channel_id = compute_migration_channel_id(company, source_system, source_instance_id)
	sys, inst = canonical_source_namespace(source_system, source_instance_id)
	co_tag = canonical_company_tag(company)

	if not frappe.db.exists("Sales Channel", channel_id):
		ch = frappe.get_doc({
			"doctype": "Sales Channel",
			"channel_id": channel_id,
			"channel_name": f"{sys} ({inst}) Migration Channel - {co_tag}",
			"channel_type": "INTERNAL",
			"company": company,
			"active": 1,
		})
		ch.insert(ignore_permissions=True)
	return channel_id


def import_validated_entity(row_name: str) -> Dict[str, Any]:
	"""
	Strictly controlled import boundary.
	Only rows from a Migration Run in READY or IMPORTING status,
	with validation_status == 'VALID', can become target ERP documents.
	Rows with WARNING or ERROR are strictly blocked from automated import.
	Direct adapter -> target ERP calls are strictly prohibited.
	"""
	row_doc = frappe.get_doc("Migration Staging Row", row_name)
	run_doc = frappe.get_doc("Migration Run", row_doc.migration_run)

	# Invariant gate 1: Run status must be READY or IMPORTING
	if run_doc.status not in ("READY", "IMPORTING"):
		raise ImportBoundaryError(
			f"Migration Run '{run_doc.name}' is in status '{run_doc.status}'. "
			"Import is only permitted on runs in 'READY' or 'IMPORTING' status."
		)

	# Invariant gate 2: Staging row validation status must be strictly VALID
	if row_doc.validation_status != "VALID":
		raise ImportBoundaryError(
			f"Cannot import row '{row_doc.name}' because its validation_status is '{row_doc.validation_status}'. "
			"Only rows with validation_status 'VALID' are eligible for automated import; "
			"rows with 'WARNING' or 'ERROR' are strictly blocked from auto-import and require review. "
			f"Errors/Warnings: {row_doc.validation_errors_json or row_doc.validation_warnings_json}"
		)

	if row_doc.import_status == "IMPORTED":
		return {
			"status": "ALREADY_IMPORTED",
			"target_doctype": row_doc.target_doctype,
			"target_name": row_doc.target_name,
		}

	if not row_doc.normalized_payload_json:
		raise ImportBoundaryError(f"Staging row '{row_doc.name}' lacks normalized payload.")

	candidate = json.loads(row_doc.normalized_payload_json)
	e_type = row_doc.entity_type
	target_doc = None
	channel_id = get_or_create_migration_channel(run_doc.company, run_doc.source_system, run_doc.source_instance_id)

	# Entity Creation / Target Document Mapping
	if e_type == "CUSTOMER":
		cust_name = candidate["customer_name"]
		if not frappe.db.exists("Customer", {"customer_name": cust_name}):
			target_doc = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": cust_name,
				"customer_type": candidate.get("customer_type", "Company"),
				"default_currency": candidate.get("default_currency", "COP"),
				"tax_id": candidate.get("tax_id"),
			}).insert(ignore_permissions=True)
		else:
			target_doc = frappe.get_doc("Customer", {"customer_name": cust_name})

		map_entity_type = ExternalEntityType.CUSTOMER

	elif e_type == "VENDOR":
		supp_name = candidate["supplier_name"]
		if not frappe.db.exists("Supplier", {"supplier_name": supp_name}):
			target_doc = frappe.get_doc({
				"doctype": "Supplier",
				"supplier_name": supp_name,
				"supplier_type": candidate.get("supplier_type", "Company"),
				"default_currency": candidate.get("default_currency", "COP"),
				"tax_id": candidate.get("tax_id"),
			}).insert(ignore_permissions=True)
		else:
			target_doc = frappe.get_doc("Supplier", {"supplier_name": supp_name})

		map_entity_type = ExternalEntityType.VENDOR

	elif e_type == "ITEM":
		item_code = candidate["item_code"]
		if not frappe.db.exists("Item", item_code):
			target_doc = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": candidate.get("item_name", item_code),
				"description": candidate.get("description", item_code),
				"stock_uom": candidate.get("stock_uom", "Nos"),
				"item_group": candidate.get("item_group", "All Item Groups"),
				"is_stock_item": candidate.get("is_stock_item", 1),
				"has_serial_no": candidate.get("has_serial_no", 0),
				"has_batch_no": candidate.get("has_batch_no", 0),
			}).insert(ignore_permissions=True)
		else:
			target_doc = frappe.get_doc("Item", item_code)

		map_entity_type = ExternalEntityType.PRODUCT

	elif e_type == "WAREHOUSE":
		wh_name = candidate["warehouse_name"]
		company_abbr = frappe.get_cached_value("Company", run_doc.company, "abbr") or "IDP"
		full_wh_name = f"{wh_name} - {company_abbr}"
		parent_wh = frappe.db.get_value("Warehouse", {"is_group": 1, "company": run_doc.company}, "name")

		if not frappe.db.exists("Warehouse", full_wh_name) and not frappe.db.exists("Warehouse", {"warehouse_name": wh_name, "company": run_doc.company}):
			target_doc = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": wh_name,
				"company": run_doc.company,
				"parent_warehouse": parent_wh,
				"is_group": candidate.get("is_group", 0),
			}).insert(ignore_permissions=True)
		else:
			wh_existing_name = frappe.db.get_value("Warehouse", {"warehouse_name": wh_name, "company": run_doc.company}, "name") or full_wh_name
			target_doc = frappe.get_doc("Warehouse", wh_existing_name)

		map_entity_type = ExternalEntityType.WAREHOUSE

	else:
		raise ImportBoundaryError(f"Unsupported entity type '{e_type}' at import boundary.")

	# Canonical External ID Mapping
	canonical_prov = canonical_provider(run_doc.source_system, run_doc.source_instance_id)
	map_filters = {
		"sales_channel": channel_id,
		"provider": canonical_prov,
		"external_entity_type": map_entity_type,
		"external_id": row_doc.source_record_id,
		"active": 1,
	}
	if not frappe.db.exists("External ID Mapping", map_filters):
		mapping_doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": channel_id,
			"provider": canonical_prov,
			"external_entity_type": map_entity_type,
			"external_id": row_doc.source_record_id,
			"erp_doctype": target_doc.doctype,
			"erp_document": target_doc.name,
			"active": 1,
		})
		mapping_doc.insert(ignore_permissions=True)

	# Update staging row
	row_doc.target_doctype = target_doc.doctype
	row_doc.target_name = target_doc.name
	row_doc.import_status = "IMPORTED"
	row_doc.save(ignore_permissions=True)

	# Update run imported count
	run_doc.reload()
	run_doc.imported_rows = frappe.db.count(
		"Migration Staging Row",
		{"migration_run": run_doc.name, "import_status": "IMPORTED"},
	)
	run_doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "IMPORTED",
		"target_doctype": target_doc.doctype,
		"target_name": target_doc.name,
	}
