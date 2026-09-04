# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Dict, Any
import frappe
from frappe.utils import flt, now_datetime


class MigrationExecutor:
	"""
	Executes pre-apply safety gate and applies valid migration batches via native Stock Reconciliation.
	Never directly mutates Bin or Stock Ledger Entry.
	"""

	@classmethod
	def apply_batch(cls, batch_name: str, user: str = "Administrator") -> Dict[str, Any]:
		batch = frappe.get_doc("Inventory Migration Batch", batch_name)

		# 1. Pre-apply safety gate
		if batch.status != "READY":
			raise frappe.ValidationError(
				f"Migration batch '{batch.batch_id}' is in status '{batch.status}'. Only 'READY' batches can be applied."
			)

		if batch.error_rows > 0:
			raise frappe.ValidationError(
				f"Migration batch '{batch.batch_id}' has {batch.error_rows} error rows and cannot be applied."
			)

		if batch.valid_rows == 0:
			raise frappe.ValidationError(f"Migration batch '{batch.batch_id}' has no valid rows to apply.")

		valid_rows = frappe.get_all(
			"Inventory Migration Row",
			filters={"batch": batch.name, "status": "VALID"},
			fields=[
				"name",
				"source_record_id",
				"resolved_item",
				"resolved_warehouse",
				"quantity",
				"valuation_rate",
				"stock_uom",
				"batch_no",
				"serial_no",
			],
			order_by="resolved_item asc, resolved_warehouse asc",
		)

		if not valid_rows:
			raise frappe.ValidationError(f"No valid rows found for batch '{batch.batch_id}'.")

		# 2. Build native Stock Reconciliation document
		# For Opening Stock with perpetual inventory, ERPNext requires an Asset, Liability, or Equity account (Balance Sheet)
		diff_acc = frappe.db.get_value(
			"Account",
			{"company": batch.company, "account_type": "Temporary Opening", "is_group": 0},
			"name",
		)
		if not diff_acc:
			diff_acc = frappe.db.get_value(
				"Account",
				{"company": batch.company, "root_type": ["in", ["Equity", "Liability"]], "report_type": "Balance Sheet", "is_group": 0},
				"name",
			)

		reco = frappe.get_doc({
			"doctype": "Stock Reconciliation",
			"company": batch.company,
			"purpose": "Opening Stock",
			"posting_date": batch.posting_date,
			"posting_time": batch.posting_time,
			"expense_account": diff_acc,
			"items": [],
		})

		for r in valid_rows:
			item_row = {
				"item_code": r.resolved_item,
				"warehouse": r.resolved_warehouse,
				"qty": flt(r.quantity),
				"valuation_rate": flt(r.valuation_rate),
				"stock_uom": r.stock_uom,
			}
			if r.batch_no:
				item_row["batch_no"] = r.batch_no
			if r.serial_no:
				item_row["serial_no"] = r.serial_no
			if r.batch_no or r.serial_no:
				item_row["use_serial_batch_fields"] = 1

			reco.append("items", item_row)

		try:
			reco.insert(ignore_permissions=True)
			reco.submit()
		except Exception as e:
			frappe.db.rollback()
			batch.status = "FAILED"
			batch.notes = (batch.notes or "") + f"\nApply error: {str(e)}"
			batch.save(ignore_permissions=True)
			frappe.db.commit()
			raise frappe.ValidationError(f"Failed to submit Stock Reconciliation: {str(e)}")

		# 3. Update batch & rows to APPLIED
		batch.status = "APPLIED"
		batch.applied_stock_reconciliation = reco.name
		batch.applied_by = user
		batch.applied_at = now_datetime()
		batch.save(ignore_permissions=True)

		for r in valid_rows:
			frappe.db.set_value("Inventory Migration Row", r.name, "status", "APPLIED")

		frappe.db.commit()

		return {
			"batch_name": batch.name,
			"batch_id": batch.batch_id,
			"stock_reconciliation": reco.name,
			"status": "APPLIED",
			"applied_rows": len(valid_rows),
		}
