# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from typing import Any, Dict, Optional

import frappe

from bop_erp.migration.exceptions import NormalizationError
from bop_erp.migration.staging import compute_payload_hash


def normalize_customer(raw: Dict[str, Any], default_company: Optional[str] = None) -> Dict[str, Any]:
	"""
	Produces candidate normalized Customer dictionary from raw source payload.
	Produces candidates only; performs zero ERPNext document insertions.
	"""
	ext_id = str(raw.get("id") or raw.get("source_record_id") or raw.get("code") or "").strip()
	name = str(raw.get("name") or raw.get("customer_name") or ext_id).strip()
	c_type = str(raw.get("type") or raw.get("customer_type") or "Company").strip().capitalize()
	if c_type not in ("Company", "Individual"):
		c_type = "Company"

	candidate = {
		"doctype": "Customer",
		"external_id": ext_id,
		"customer_name": name,
		"customer_type": c_type,
		"email_id": str(raw.get("email") or raw.get("email_id") or "").strip().lower() or None,
		"mobile_no": str(raw.get("phone") or raw.get("mobile_no") or raw.get("telephone") or "").strip() or None,
		"tax_id": str(raw.get("tax_id") or raw.get("vat_number") or raw.get("nit") or "").strip() or None,
		"default_currency": str(raw.get("currency") or "COP").strip().upper(),
		"payment_terms": str(raw.get("payment_terms") or "").strip() or None,
		"billing_address": raw.get("billing_address") or raw.get("address"),
		"shipping_address": raw.get("shipping_address"),
		"company": default_company,
	}
	return candidate


def normalize_vendor(raw: Dict[str, Any], default_company: Optional[str] = None) -> Dict[str, Any]:
	"""
	Produces candidate normalized Supplier/Vendor dictionary from raw source payload.
	"""
	ext_id = str(raw.get("id") or raw.get("source_record_id") or raw.get("code") or "").strip()
	name = str(raw.get("name") or raw.get("supplier_name") or raw.get("vendor_name") or ext_id).strip()
	s_type = str(raw.get("type") or raw.get("supplier_type") or "Company").strip().capitalize()
	if s_type not in ("Company", "Individual"):
		s_type = "Company"

	candidate = {
		"doctype": "Supplier",
		"external_id": ext_id,
		"supplier_name": name,
		"supplier_type": s_type,
		"email_id": str(raw.get("email") or raw.get("email_id") or "").strip().lower() or None,
		"mobile_no": str(raw.get("phone") or raw.get("mobile_no") or raw.get("telephone") or "").strip() or None,
		"tax_id": str(raw.get("tax_id") or raw.get("vat_number") or raw.get("nit") or "").strip() or None,
		"default_currency": str(raw.get("currency") or "COP").strip().upper(),
		"payment_terms": str(raw.get("payment_terms") or "").strip() or None,
		"company": default_company,
	}
	return candidate


def normalize_item(raw: Dict[str, Any], default_company: Optional[str] = None) -> Dict[str, Any]:
	"""
	Produces candidate normalized Item dictionary from raw source payload.
	"""
	ext_id = str(raw.get("id") or raw.get("source_record_id") or raw.get("sku") or "").strip()
	item_code = str(raw.get("sku") or raw.get("item_code") or raw.get("id") or "").strip()
	name = str(raw.get("name") or raw.get("item_name") or item_code).strip()
	description = str(raw.get("description") or name).strip()
	uom = str(raw.get("uom") or raw.get("stock_uom") or "Nos").strip()
	item_group = str(raw.get("item_group") or "All Item Groups").strip()

	candidate = {
		"doctype": "Item",
		"external_id": ext_id,
		"item_code": item_code,
		"item_name": name,
		"description": description,
		"stock_uom": uom,
		"item_group": item_group,
		"is_stock_item": 1 if raw.get("is_stock", True) else 0,
		"has_serial_no": 1 if raw.get("is_serial", False) else 0,
		"has_batch_no": 1 if raw.get("is_batch", False) else 0,
	}
	return candidate


def normalize_warehouse(raw: Dict[str, Any], default_company: Optional[str] = None) -> Dict[str, Any]:
	"""
	Produces candidate normalized Warehouse dictionary from raw source payload.
	"""
	ext_id = str(raw.get("id") or raw.get("source_record_id") or raw.get("code") or "").strip()
	name = str(raw.get("name") or raw.get("warehouse_name") or ext_id).strip()
	parent_id = str(raw.get("parent_id") or raw.get("parent_warehouse") or "").strip() or None

	candidate = {
		"doctype": "Warehouse",
		"external_id": ext_id,
		"warehouse_name": name,
		"parent_warehouse_ref": parent_id,
		"is_group": 1 if raw.get("is_group", False) else 0,
		"company": default_company,
	}
	return candidate


def normalize_record(
	entity_type: str,
	raw_record: Dict[str, Any],
	default_company: Optional[str] = None,
) -> Dict[str, Any]:
	"""Dispatch helper to normalize by entity_type."""
	e_type = str(entity_type).strip().upper()
	if e_type == "CUSTOMER":
		return normalize_customer(raw_record, default_company)
	elif e_type == "VENDOR":
		return normalize_vendor(raw_record, default_company)
	elif e_type == "ITEM":
		return normalize_item(raw_record, default_company)
	elif e_type == "WAREHOUSE":
		return normalize_warehouse(raw_record, default_company)
	else:
		raise NormalizationError(f"Unsupported migration entity type for normalization: '{entity_type}'")


def normalize_staging_row(row_doc: Any, default_company: Optional[str] = None) -> Dict[str, Any]:
	"""Normalizes a single Migration Staging Row document and stores its normalized payload and hash."""
	raw = json.loads(row_doc.source_payload_json)
	normalized = normalize_record(row_doc.entity_type, raw, default_company)
	norm_hash = compute_payload_hash(normalized)

	row_doc.normalized_payload_json = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
	row_doc.normalized_payload_hash = norm_hash
	row_doc.target_doctype = normalized.get("doctype")
	row_doc.save(ignore_permissions=True)
	return normalized


def normalize_migration_run(run_id: str) -> int:
	"""
	Normalizes all staged rows for a Migration Run.
	"""
	run_doc = frappe.get_doc("Migration Run", run_id)
	rows = frappe.get_all(
		"Migration Staging Row",
		filters={"migration_run": run_id},
		fields=["name", "entity_type", "source_payload_json"],
	)
	normalized_count = 0
	for r in rows:
		row_doc = frappe.get_doc("Migration Staging Row", r.name)
		normalize_staging_row(row_doc, default_company=run_doc.company)
		normalized_count += 1
	frappe.db.commit()
	return normalized_count
