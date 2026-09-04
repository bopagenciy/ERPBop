import hashlib
import json
from typing import Dict, List, Any
import frappe
from frappe.utils import flt
from bop_erp.inventory.service import InventoryService


class MigrationValidator:
	"""
	Executes full pre-migration validation on staged Inventory Migration Row records.
	Identifies all structural, catalog, warehouse, and quantity errors without writing stock entries.
	Captures a deterministic validation snapshot hash of live ERP stock for optimistic fencing.
	"""

	@classmethod
	def compute_stock_snapshot_hash(cls, items_warehouses: List[tuple]) -> str:
		"""
		Computes a deterministic SHA-256 hash of live ERP inventory for the given (item_code, warehouse) pairs.
		Canonical sorting ensures order independence.
		"""
		snapshot_entries = []
		# Deduplicate (item_code, warehouse)
		unique_pairs = sorted(list(set(items_warehouses)), key=lambda x: (x[0], x[1]))
		for item_code, warehouse in unique_pairs:
			snap = InventoryService.get_warehouse_inventory(item_code, warehouse)
			val_rate = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate") or 0.0
			snapshot_entries.append([
				item_code,
				warehouse,
				flt(snap.actual_qty),
				flt(val_rate),
			])
		canonical_json = json.dumps(snapshot_entries, ensure_ascii=False, separators=(",", ":"))
		return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

	@classmethod
	def validate_batch(cls, batch_name: str) -> Dict[str, Any]:
		batch = frappe.get_doc("Inventory Migration Batch", batch_name)
		if batch.status in ["APPLIED", "APPLYING"]:
			raise frappe.ValidationError(f"Migration batch '{batch.batch_id}' is in status '{batch.status}'.")

		rows = frappe.get_all(
			"Inventory Migration Row",
			filters={"batch": batch.name},
			fields=[
				"name",
				"source_record_id",
				"item_code",
				"warehouse",
				"quantity",
				"valuation_rate",
				"stock_uom",
				"batch_no",
				"serial_no",
			],
		)

		valid_count = 0
		error_count = 0
		all_errors = []
		all_warnings = []

		seen_item_warehouse = set()
		valid_item_warehouse_pairs = []

		# Cache company
		company = batch.company

		batch_errors = []

		# 0. Validate opening difference account if specified or flag as error if missing
		if not batch.opening_difference_account:
			batch_errors.append("Opening Difference Account is required on Inventory Migration Batch.")
		else:
			acc_data = frappe.db.get_value(
				"Account",
				batch.opening_difference_account,
				["name", "company", "is_group", "disabled", "report_type", "root_type", "account_type"],
				as_dict=True,
			)
			if not acc_data:
				batch_errors.append(f"Opening Difference Account '{batch.opening_difference_account}' does not exist.")
			else:
				if acc_data.company != company:
					batch_errors.append(
						f"Opening Difference Account '{batch.opening_difference_account}' belongs to company '{acc_data.company}', not batch company '{company}'."
					)
				if acc_data.is_group:
					batch_errors.append(
						f"Opening Difference Account '{batch.opening_difference_account}' is a group account. Must be a leaf account."
					)
				if acc_data.disabled:
					batch_errors.append(
						f"Opening Difference Account '{batch.opening_difference_account}' is disabled."
					)
				if acc_data.report_type != "Balance Sheet":
					batch_errors.append(
						f"Opening Difference Account '{batch.opening_difference_account}' has report_type '{acc_data.report_type}'. "
						"Perpetual inventory opening stock entries require a Balance Sheet account (Asset, Liability, or Equity)."
					)

		all_errors.extend(batch_errors)

		for r in rows:
			row_errors = []
			row_doc = frappe.get_doc("Inventory Migration Row", r.name)

			# 1. Warehouse validation
			wh = r.warehouse
			wh_data = frappe.db.get_value("Warehouse", wh, ["name", "company", "is_group", "disabled"], as_dict=True)
			if not wh_data:
				row_errors.append(f"Warehouse '{wh}' does not exist.")
			else:
				if wh_data.company != company:
					row_errors.append(f"Warehouse '{wh}' belongs to company '{wh_data.company}', not batch company '{company}'.")
				if wh_data.is_group:
					row_errors.append(f"Warehouse '{wh}' is a group warehouse. Opening stock must be booked to leaf warehouses.")
				if wh_data.disabled:
					row_errors.append(f"Warehouse '{wh}' is disabled.")
				row_doc.resolved_warehouse = wh_data.name

			# 2. Item validation
			item_code = r.item_code
			item_data = frappe.db.get_value(
				"Item",
				item_code,
				["name", "is_stock_item", "disabled", "stock_uom", "has_serial_no", "has_batch_no"],
				as_dict=True,
			)
			if not item_data:
				row_errors.append(f"Item '{item_code}' does not exist.")
			else:
				if not item_data.is_stock_item:
					row_errors.append(f"Item '{item_code}' is not a stock item.")
				if item_data.disabled:
					row_errors.append(f"Item '{item_code}' is disabled.")
				row_doc.resolved_item = item_data.name

				# Stock UOM
				if r.stock_uom and r.stock_uom != item_data.stock_uom:
					row_errors.append(
						f"Specified UOM '{r.stock_uom}' does not match Item master stock UOM '{item_data.stock_uom}'."
					)
				row_doc.stock_uom = item_data.stock_uom

				# Serial numbers
				if item_data.has_serial_no:
					if not r.serial_no:
						row_errors.append(f"Item '{item_code}' is serialized but no serial numbers were provided.")
					else:
						serials = [s.strip() for s in r.serial_no.replace("\n", ",").split(",") if s.strip()]
						if len(serials) != int(flt(r.quantity)):
							row_errors.append(
								f"Serialized Item '{item_code}' quantity ({r.quantity}) does not match serial count ({len(serials)})."
							)

				# Batch numbers
				if item_data.has_batch_no:
					if not r.batch_no:
						row_errors.append(f"Item '{item_code}' is batch-tracked but no batch_no was provided.")

			# 3. Quantity & Valuation Rate
			qty = flt(r.quantity)
			if qty < 0:
				row_errors.append(f"Negative opening quantity ({qty}) is not permitted.")

			rate = flt(r.valuation_rate)
			if rate < 0:
				row_errors.append(f"Negative valuation rate ({rate}) is not permitted.")
			elif rate == 0 and qty > 0:
				all_warnings.append(f"Row {r.source_record_id}: Item '{item_code}' has 0 valuation rate with positive quantity.")

			# 4. Duplicate (item_code, warehouse, batch/serial) in batch
			combo_key = (r.item_code, r.warehouse, str(r.batch_no or ""), str(r.serial_no or ""))
			if combo_key in seen_item_warehouse:
				row_errors.append(
					f"Duplicate entry for Item '{r.item_code}' in Warehouse '{r.warehouse}' in the same migration batch."
				)
			seen_item_warehouse.add(combo_key)

			# Record row status
			if row_errors:
				row_doc.status = "ERROR"
				row_doc.validation_error = "; ".join(row_errors)
				error_count += 1
				all_errors.append(f"Row {r.source_record_id}: " + "; ".join(row_errors))
			else:
				row_doc.status = "VALID"
				row_doc.validation_error = ""
				valid_count += 1
				if row_doc.resolved_item and row_doc.resolved_warehouse:
					valid_item_warehouse_pairs.append((row_doc.resolved_item, row_doc.resolved_warehouse))

			row_doc.save(ignore_permissions=True)

		# Compute validation snapshot hash across all valid items & warehouses
		snapshot_hash = cls.compute_stock_snapshot_hash(valid_item_warehouse_pairs)
		batch.validation_snapshot_hash = snapshot_hash

		batch.total_rows = len(rows)
		batch.valid_rows = valid_count
		batch.error_rows = error_count

		if error_count == 0 and len(batch_errors) == 0 and valid_count > 0:
			batch.status = "READY"
		else:
			batch.status = "VALIDATED"

		batch.save(ignore_permissions=True)
		frappe.db.commit()

		return {
			"batch_name": batch.name,
			"batch_id": batch.batch_id,
			"total_rows": len(rows),
			"valid_rows": valid_count,
			"error_rows": error_count,
			"validation_snapshot_hash": snapshot_hash,
			"status": batch.status,
			"errors": all_errors,
			"warnings": all_warnings,
		}

