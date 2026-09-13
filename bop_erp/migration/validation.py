# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import frappe
from frappe import _

from bop_erp.migration.exceptions import MigrationValidationError
from bop_erp.migration.normalization import normalize_staging_row

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_customer_candidate(candidate: Dict[str, Any], company: str) -> Tuple[List[str], List[str]]:
	errors = []
	warnings = []

	if not candidate.get("external_id"):
		errors.append("Missing mandatory source external_id.")
	if not candidate.get("customer_name"):
		errors.append("Missing mandatory customer_name.")

	comp = candidate.get("company") or company
	if not comp:
		errors.append("Missing mandatory company assignment.")
	elif not frappe.db.exists("Company", comp):
		errors.append(f"Referenced company '{comp}' does not exist in target ERP.")

	curr = candidate.get("default_currency")
	if curr and not frappe.db.exists("Currency", curr):
		errors.append(f"Currency '{curr}' does not exist in target ERP Currency master.")

	email = candidate.get("email_id")
	if email and not EMAIL_REGEX.match(email):
		warnings.append(f"Customer email '{email}' has malformed format.")

	return errors, warnings


def validate_vendor_candidate(candidate: Dict[str, Any], company: str) -> Tuple[List[str], List[str]]:
	errors = []
	warnings = []

	if not candidate.get("external_id"):
		errors.append("Missing mandatory source external_id.")
	if not candidate.get("supplier_name"):
		errors.append("Missing mandatory supplier/vendor name.")

	curr = candidate.get("default_currency")
	if curr and not frappe.db.exists("Currency", curr):
		errors.append(f"Currency '{curr}' does not exist in target ERP Currency master.")

	email = candidate.get("email_id")
	if email and not EMAIL_REGEX.match(email):
		warnings.append(f"Vendor email '{email}' has malformed format.")

	return errors, warnings


def validate_item_candidate(candidate: Dict[str, Any], company: str) -> Tuple[List[str], List[str]]:
	errors = []
	warnings = []

	if not candidate.get("external_id"):
		errors.append("Missing mandatory source external_id.")
	if not candidate.get("item_code"):
		errors.append("Missing mandatory item_code (SKU).")

	uom = candidate.get("stock_uom")
	if not uom:
		errors.append("Missing mandatory stock_uom.")
	elif not frappe.db.exists("UOM", uom):
		errors.append(f"Stock UOM '{uom}' does not exist in target ERP UOM master.")

	grp = candidate.get("item_group")
	if grp and not frappe.db.exists("Item Group", grp):
		errors.append(f"Item Group '{grp}' does not exist in target ERP.")

	return errors, warnings


def validate_warehouse_candidate(
	candidate: Dict[str, Any],
	company: str,
	staged_wh_ids: Optional[Set[str]] = None,
) -> Tuple[List[str], List[str]]:
	errors = []
	warnings = []

	if not candidate.get("external_id"):
		errors.append("Missing mandatory source external_id.")
	if not candidate.get("warehouse_name"):
		errors.append("Missing mandatory warehouse_name.")

	comp = candidate.get("company") or company
	if not comp:
		errors.append("Missing mandatory company assignment.")
	elif not frappe.db.exists("Company", comp):
		errors.append(f"Referenced company '{comp}' does not exist in target ERP.")

	parent_ref = candidate.get("parent_warehouse_ref")
	if parent_ref:
		# Allowed if parent warehouse already exists in ERP or is present in staged warehouse set
		parent_in_erp = frappe.db.exists("Warehouse", parent_ref) or frappe.db.exists("Warehouse", {"warehouse_name": parent_ref})
		parent_in_staged = staged_wh_ids and parent_ref in staged_wh_ids
		if not parent_in_erp and not parent_in_staged:
			errors.append(f"Referenced parent warehouse '{parent_ref}' does not exist in ERP and is not in staged batch.")

	return errors, warnings


def validate_migration_run(run_id: str) -> Dict[str, Any]:
	"""
	Validates all staged rows within a Migration Run.
	Classifies every row as VALID, WARNING, or ERROR.
	Updates run metrics. If error_rows == 0, transitions run to READY; otherwise REVIEW_REQUIRED.
	"""
	run_doc = frappe.get_doc("Migration Run", run_id)
	if run_doc.status not in ("STAGED", "VALIDATING", "REVIEW_REQUIRED"):
		raise MigrationValidationError(
			f"Migration Run '{run_id}' is in status '{run_doc.status}'. "
			"Only STAGED, VALIDATING, or REVIEW_REQUIRED runs can be validated."
		)

	run_doc.status = "VALIDATING"
	run_doc.save(ignore_permissions=True)
	frappe.db.commit()

	rows = frappe.get_all(
		"Migration Staging Row",
		filters={"migration_run": run_id},
		fields=["name", "entity_type", "source_record_id", "normalized_payload_json", "source_payload_json"],
	)

	# Build duplicate tracking sets within the run
	seen_identities: Dict[Tuple[str, str], List[str]] = {}
	seen_item_codes: Dict[str, List[str]] = {}
	staged_wh_ids: Set[str] = set()

	for r in rows:
		key = (r.entity_type, r.source_record_id)
		seen_identities.setdefault(key, []).append(r.name)
		if r.entity_type == "WAREHOUSE":
			staged_wh_ids.add(r.source_record_id)

	valid_count = 0
	warning_count = 0
	error_count = 0

	for r in rows:
		row_doc = frappe.get_doc("Migration Staging Row", r.name)

		# Ensure normalized payload exists
		if not row_doc.normalized_payload_json:
			normalize_staging_row(row_doc, default_company=run_doc.company)
			row_doc.reload()

		candidate = json.loads(row_doc.normalized_payload_json)
		e_type = row_doc.entity_type

		# Run type-specific candidate validation
		if e_type == "CUSTOMER":
			errors, warnings = validate_customer_candidate(candidate, run_doc.company)
		elif e_type == "VENDOR":
			errors, warnings = validate_vendor_candidate(candidate, run_doc.company)
		elif e_type == "ITEM":
			errors, warnings = validate_item_candidate(candidate, run_doc.company)
			sku = candidate.get("item_code")
			if sku:
				seen_item_codes.setdefault(sku, []).append(r.name)
		elif e_type == "WAREHOUSE":
			errors, warnings = validate_warehouse_candidate(candidate, run_doc.company, staged_wh_ids)
		else:
			errors, warnings = [f"Unsupported entity type: '{e_type}'"], []

		# Check for intra-run duplicate source identities
		if len(seen_identities.get((r.entity_type, r.source_record_id), [])) > 1:
			errors.append(f"Duplicate source identity '{r.source_record_id}' detected multiple times within migration run.")

		# Determine row classification
		if errors:
			row_status = "ERROR"
			error_count += 1
		elif warnings:
			row_status = "WARNING"
			warning_count += 1
		else:
			row_status = "VALID"
			valid_count += 1

		row_doc.validation_status = row_status
		row_doc.validation_errors_json = json.dumps(errors, ensure_ascii=False) if errors else None
		row_doc.validation_warnings_json = json.dumps(warnings, ensure_ascii=False) if warnings else None
		row_doc.save(ignore_permissions=True)

	# Check for duplicate SKU candidates across different item rows
	for sku, row_names in seen_item_codes.items():
		if len(row_names) > 1:
			for r_name in row_names:
				r_doc = frappe.get_doc("Migration Staging Row", r_name)
				errs = json.loads(r_doc.validation_errors_json or "[]")
				err_msg = f"Item code / SKU candidate '{sku}' is shared by multiple distinct items in this run."
				if err_msg not in errs:
					errs.append(err_msg)
					if r_doc.validation_status != "ERROR":
						if r_doc.validation_status == "VALID":
							valid_count -= 1
						elif r_doc.validation_status == "WARNING":
							warning_count -= 1
						error_count += 1
					r_doc.validation_status = "ERROR"
					r_doc.validation_errors_json = json.dumps(errs, ensure_ascii=False)
					r_doc.save(ignore_permissions=True)

	# Update Migration Run status and counts
	run_doc.reload()
	run_doc.valid_rows = valid_count
	run_doc.warning_rows = warning_count
	run_doc.error_rows = error_count
	run_doc.total_rows = len(rows)

	if error_count == 0:
		run_doc.status = "READY"
	else:
		run_doc.status = "REVIEW_REQUIRED"
		run_doc.notes = (run_doc.notes or "") + f"\nValidation completed with {error_count} blocking error(s)."

	run_doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": run_doc.status,
		"total_rows": len(rows),
		"valid_rows": valid_count,
		"warning_rows": warning_count,
		"error_rows": error_count,
	}
