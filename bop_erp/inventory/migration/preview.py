# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Dict, List, Any
import frappe
from frappe.utils import flt
from bop_erp.inventory.service import InventoryService


class MigrationPreview:
	"""
	Generates a non-mutating comparison preview:
	SOURCE OPENING TARGET vs CURRENT ERP STOCK vs PROPOSED ADJUSTMENT DELTA.
	"""

	@classmethod
	def get_reconciliation_preview(cls, batch_name: str) -> Dict[str, Any]:
		batch = frappe.get_doc("Inventory Migration Batch", batch_name)

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

		preview_lines = []
		total_opening_qty = 0.0
		total_current_qty = 0.0
		total_adjustment_delta = 0.0
		total_inventory_value = 0.0
		non_zero_current_stock_warnings = []

		for r in valid_rows:
			item_code = r.resolved_item
			warehouse = r.resolved_warehouse
			target_qty = flt(r.quantity)
			rate = flt(r.valuation_rate)
			inv_val = flt(target_qty * rate)

			snap = InventoryService.get_warehouse_inventory(item_code, warehouse)
			current_qty = flt(snap.actual_qty)
			delta = flt(target_qty - current_qty)

			if current_qty > 0:
				non_zero_current_stock_warnings.append(
					f"Item '{item_code}' in Warehouse '{warehouse}' already has current stock {current_qty}."
				)

			preview_lines.append({
				"row_name": r.name,
				"source_record_id": r.source_record_id,
				"item_code": item_code,
				"warehouse": warehouse,
				"stock_uom": r.stock_uom,
				"source_target_qty": target_qty,
				"current_qty": current_qty,
				"adjustment_delta": delta,
				"valuation_rate": rate,
				"inventory_value": inv_val,
				"batch_no": r.batch_no,
				"serial_no": r.serial_no,
			})

			total_opening_qty += target_qty
			total_current_qty += current_qty
			total_adjustment_delta += delta
			total_inventory_value += inv_val

		return {
			"batch_name": batch.name,
			"batch_id": batch.batch_id,
			"company": batch.company,
			"posting_date": batch.posting_date,
			"posting_time": batch.posting_time,
			"total_items": len(preview_lines),
			"total_opening_qty": flt(total_opening_qty, 3),
			"total_current_qty": flt(total_current_qty, 3),
			"total_adjustment_delta": flt(total_adjustment_delta, 3),
			"total_inventory_value": flt(total_inventory_value, 2),
			"warnings": non_zero_current_stock_warnings,
			"lines": preview_lines,
		}
