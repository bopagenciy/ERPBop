import traceback
from typing import Dict, Any
import frappe
from frappe.utils import flt, now_datetime
from bop_erp.inventory.migration.validator import MigrationValidator


class MigrationExecutor:
	"""
	Executes pre-apply safety gate, optimistic stock concurrency fencing,
	and applies valid migration batches via native Stock Reconciliation.
	Never directly mutates Bin or Stock Ledger Entry.
	"""

	@classmethod
	def apply_batch(cls, batch_name: str, user: str = "Administrator") -> Dict[str, Any]:
		# 1. Atomic Concurrency Claim: Attempt transition from READY -> APPLYING
		# Using direct UPDATE with row count prevents two workers from applying simultaneously
		frappe.db.sql(
			"""
			UPDATE `tabInventory Migration Batch`
			SET status = 'APPLYING', modified = NOW(), modified_by = %(user)s
			WHERE name = %(name)s AND status = 'READY'
			""",
			{"name": batch_name, "user": user},
		)

		if getattr(frappe.db._cursor, "rowcount", 0) <= 0:
			# Batch was either not READY, or another worker already claimed it
			current_status = frappe.db.get_value("Inventory Migration Batch", batch_name, "status")
			if not current_status:
				raise frappe.DoesNotExistError(f"Inventory Migration Batch '{batch_name}' does not exist.")
			if current_status == "APPLYING":
				raise frappe.ValidationError(
					f"Migration batch '{batch_name}' is currently being applied by another process."
				)
			raise frappe.ValidationError(
				f"Migration batch '{batch_name}' is in status '{current_status}'. Only 'READY' batches can be applied."
			)

		# Immediately commit the claim so concurrent transactions see APPLYING
		frappe.db.commit()

		# Reload batch document under the APPLYING lock
		batch = frappe.get_doc("Inventory Migration Batch", batch_name)

		try:
			# 2. Pre-apply Immutability & Safety Validation Gate
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

			# 3. Explicit Opening Difference Account Enforcement
			if not batch.opening_difference_account:
				raise frappe.ValidationError(
					"Explicit 'opening_difference_account' is required on Inventory Migration Batch before apply."
				)

			diff_acc_data = frappe.db.get_value(
				"Account",
				batch.opening_difference_account,
				["name", "company", "is_group", "disabled", "report_type"],
				as_dict=True,
			)
			if not diff_acc_data:
				raise frappe.ValidationError(
					f"Opening difference account '{batch.opening_difference_account}' does not exist."
				)
			if diff_acc_data.company != batch.company:
				raise frappe.ValidationError(
					f"Opening difference account '{batch.opening_difference_account}' belongs to company '{diff_acc_data.company}', not '{batch.company}'."
				)
			if diff_acc_data.is_group or diff_acc_data.disabled:
				raise frappe.ValidationError(
					f"Opening difference account '{batch.opening_difference_account}' cannot be a group or disabled account."
				)
			if diff_acc_data.report_type != "Balance Sheet":
				raise frappe.ValidationError(
					f"Opening difference account '{batch.opening_difference_account}' has report_type '{diff_acc_data.report_type}'. "
					"Opening stock reconciliation requires a Balance Sheet account (Asset, Liability, or Equity)."
				)

			# 4. Validation Snapshot Drift Protection (Optimistic Safety Check)
			# Extract all valid (item_code, warehouse) pairs and re-check current live stock hash
			pairs = [(r.resolved_item, r.resolved_warehouse) for r in valid_rows]
			current_snapshot_hash = MigrationValidator.compute_stock_snapshot_hash(pairs)

			if not batch.validation_snapshot_hash:
				raise frappe.ValidationError(
					f"Migration batch '{batch.batch_id}' is missing validation_snapshot_hash. Batch must be revalidated."
				)

			if current_snapshot_hash != batch.validation_snapshot_hash:
				raise frappe.ValidationError(
					f"ERP stock changed since validation for batch '{batch.batch_id}'. "
					f"Expected hash {batch.validation_snapshot_hash[:12]}..., but live hash is {current_snapshot_hash[:12]}... "
					"Batch must be revalidated before applying."
				)

			# 5. Build native Stock Reconciliation document
			reco = frappe.get_doc({
				"doctype": "Stock Reconciliation",
				"company": batch.company,
				"purpose": "Opening Stock",
				"posting_date": batch.posting_date,
				"posting_time": batch.posting_time,
				"expense_account": batch.opening_difference_account,
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
					# In ERPNext v16, Batch is a Link field requiring a Batch master record
					if not frappe.db.exists("Batch", r.batch_no):
						frappe.get_doc({
							"doctype": "Batch",
							"batch_id": r.batch_no,
							"item": r.resolved_item,
						}).insert(ignore_permissions=True)
					item_row["batch_no"] = r.batch_no
				if r.serial_no:
					item_row["serial_no"] = r.serial_no
				if r.batch_no or r.serial_no:
					item_row["use_serial_batch_fields"] = 1

				reco.append("items", item_row)

			# 6. Insert and Submit Stock Reconciliation
			reco.insert(ignore_permissions=True)
			reco.submit()

			# 7. Post-Submit Batch Status Update in Same Transaction
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

		except Exception as e:
			# Rollback any pending stock reconciliation insert/submit
			frappe.db.rollback()

			# In a fresh transaction, record failure on the batch
			try:
				err_msg = str(e)
				tb = traceback.format_exc()
				# If reco was already submitted before a subsequent failure, handle gracefully
				reco_name = getattr(locals().get("reco"), "name", None)
				reco_submitted = False
				if reco_name and frappe.db.exists("Stock Reconciliation", reco_name):
					reco_submitted = (frappe.db.get_value("Stock Reconciliation", reco_name, "docstatus") == 1)

				if reco_submitted:
					# Stock Reconciliation actually succeeded, so record it as APPLIED with warning
					batch.status = "APPLIED"
					batch.applied_stock_reconciliation = reco_name
					batch.applied_by = user
					batch.applied_at = now_datetime()
					batch.notes = (batch.notes or "") + f"\nPost-submit warning: {err_msg}"
					batch.save(ignore_permissions=True)
					frappe.db.commit()
					return {
						"batch_name": batch.name,
						"batch_id": batch.batch_id,
						"stock_reconciliation": reco_name,
						"status": "APPLIED",
						"applied_rows": len(valid_rows),
					}
				else:
					# Clean failure: batch set to FAILED (or reverted to VALIDATED on stock drift)
					if "ERP stock changed since validation" in err_msg:
						batch.status = "VALIDATED"
					else:
						batch.status = "FAILED"
					batch.notes = (batch.notes or "") + f"\nApply error: {err_msg}\n{tb[-500:]}"
					batch.save(ignore_permissions=True)
					frappe.db.commit()
			except Exception as log_err:
				frappe.logger().error(f"Failed to record migration batch apply failure: {str(log_err)}")

			raise frappe.ValidationError(f"Failed to apply Inventory Migration Batch: {str(e)}")

