# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Dict, List, Any
import frappe
from frappe.utils import flt


class MigrationValidator:
	"""
	Executes full pre-migration validation on staged Inventory Migration Row records.
	Identifies all structural, catalog, warehouse, and quantity errors without writing stock entries.
	"""

	@classmethod
	def validate_batch(cls, batch_name: str) -> Dict[str, Any]:
		batch = frappe.get_doc("Inventory Migration Batch", batch_name)
		if batch.status == "APPLIED":
			raise frappe.ValidationError(f"Migration batch '{batch.batch_id}' is already APPLIED.")

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

		# Cache company
		company = batch.company

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

			row_doc.save(ignore_permissions=True)

		batch.total_rows = len(rows)
		batch.valid_rows = valid_count
		batch.error_rows = error_count

		if error_count == 0 and valid_count > 0:
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
			"status": batch.status,
			"errors": all_errors,
			"warnings": all_warnings,
		}
