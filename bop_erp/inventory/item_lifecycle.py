# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional, Tuple
import frappe
from frappe import _


class ItemLifecycleError(frappe.ValidationError):
	pass


TRANSACTIONAL_DOCTYPES = [
	("Stock Ledger Entry", "item_code"),
	("Sales Order Item", "item_code"),
	("Purchase Order Item", "item_code"),
	("Delivery Note Item", "item_code"),
	("Purchase Receipt Item", "item_code"),
	("Sales Invoice Item", "item_code"),
	("Purchase Invoice Item", "item_code"),
]


def check_item_deletion_safety(item_code: str) -> Tuple[bool, str, List[str]]:
	"""
	Audits whether an Item can be safely deleted or whether it has historical
	transaction references that necessitate disabling rather than physical deletion.
	Returns (is_safe_to_delete, explanation_message, list_of_referencing_doctypes).
	"""
	if not frappe.db.exists("Item", item_code):
		return True, f"Item '{item_code}' does not exist.", []

	found_references: List[str] = []

	for dt, field in TRANSACTIONAL_DOCTYPES:
		try:
			count = frappe.db.count(dt, {field: item_code})
			if count > 0:
				found_references.append(f"{dt} ({count} records)")
		except Exception:
			# If DocType is not installed or accessible, ignore
			pass

	if found_references:
		msg = (
			f"Item '{item_code}' cannot be deleted because historical/transactional references exist: "
			f"{', '.join(found_references)}. The item must be disabled instead."
		)
		return False, msg, found_references

	return True, f"Item '{item_code}' has no transactional history and may follow native deletion rules.", []


def safe_delete_or_disable_item(item_code: str, force_disable_if_history: bool = True) -> Dict[str, Any]:
	"""
	Enforces the Bop ERP Item Lifecycle Policy:
	- Unused Item with zero history: deleted following native ERPNext integrity checks.
	- Item with transactional history: physical delete blocked; item is disabled.
	- Never force-deletes Stock Ledger, GL, or historical document rows.
	"""
	is_safe, reason, refs = check_item_deletion_safety(item_code)

	if is_safe:
		# Native delete with full integrity checks
		frappe.delete_doc("Item", item_code, ignore_permissions=False)
		return {
			"action_taken": "DELETED",
			"item_code": item_code,
			"message": f"Item '{item_code}' deleted according to native ERPNext rules.",
		}
	else:
		if force_disable_if_history:
			frappe.db.set_value("Item", item_code, "disabled", 1)
			return {
				"action_taken": "DISABLED",
				"item_code": item_code,
				"message": f"Item '{item_code}' has transaction history. Physical delete was blocked and the item has been disabled.",
				"references": refs,
			}
		else:
			raise ItemLifecycleError(reason)


def verify_item_not_locked_by_provenance(item_code: str) -> bool:
	"""
	Confirms that an item with migration provenance / external mappings remains
	fully editable by standard Bop ERP workflows (manual edits, price changes, UOM edits).
	Source provenance is traceability, not a business lock.
	"""
	if not frappe.db.exists("Item", item_code):
		return False

	doc = frappe.get_doc("Item", item_code)
	# Check if document has any artificial lock flags
	if getattr(doc, "migration_locked", 0) == 1:
		return False
	return True
