# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import List, Optional
import frappe
from frappe.utils import flt

from bop_erp.inventory.exceptions import WarehouseNotFoundError
from bop_erp.inventory.models import (
	WarehouseInventorySnapshot,
	ChannelInventorySnapshot,
	InventoryComparisonResult,
)


class InventoryService:
	"""
	Read-only Bop ERP inventory service abstraction.
	Provides safe, point-in-time native inventory snapshots per Warehouse and per Sales Channel.
	Strictly adheres to ERPNext v16.32.3 native quantity semantics without inventing arbitrary ATS formulas.
	"""

	@staticmethod
	def get_warehouse_inventory(item_code: str, warehouse: str) -> WarehouseInventorySnapshot:
		"""
		Retrieves native Bin quantities for an item in a specific warehouse.
		Returns safe zero-quantities if no Bin record currently exists.
		"""
		wh_doc = frappe.db.get_value("Warehouse", warehouse, ["name", "company", "is_group"], as_dict=True)
		if not wh_doc:
			raise WarehouseNotFoundError(f"Warehouse '{warehouse}' not found.")

		item_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"

		bin_data = frappe.db.get_value(
			"Bin",
			{"item_code": item_code, "warehouse": warehouse},
			[
				"actual_qty",
				"reserved_qty",
				"ordered_qty",
				"indented_qty",
				"planned_qty",
				"projected_qty",
				"stock_uom",
			],
			as_dict=True,
		)

		if bin_data:
			return WarehouseInventorySnapshot(
				item_code=item_code,
				warehouse=warehouse,
				company=wh_doc.company,
				stock_uom=bin_data.stock_uom or item_uom,
				actual_qty=flt(bin_data.actual_qty, 3),
				reserved_qty=flt(bin_data.reserved_qty, 3),
				ordered_qty=flt(bin_data.ordered_qty, 3),
				indented_qty=flt(bin_data.indented_qty, 3),
				planned_qty=flt(bin_data.planned_qty, 3),
				projected_qty=flt(bin_data.projected_qty, 3),
			)
		else:
			return WarehouseInventorySnapshot(
				item_code=item_code,
				warehouse=warehouse,
				company=wh_doc.company,
				stock_uom=item_uom,
				actual_qty=0.0,
				reserved_qty=0.0,
				ordered_qty=0.0,
				indented_qty=0.0,
				planned_qty=0.0,
				projected_qty=0.0,
			)

	@classmethod
	def get_channel_inventory_snapshot(
		cls,
		item_code: str,
		sales_channel: str,
		sellable_only: bool = False,
	) -> ChannelInventorySnapshot:
		"""
		Aggregates native inventory quantities across all enabled Channel Inventory Source warehouses
		for the given sales channel.
		If sellable_only is True, filters to sources where allow_sellable_stock is checked.
		"""
		ch_company = frappe.db.get_value("Sales Channel", sales_channel, "company")
		if not ch_company:
			ch_company = frappe.db.get_single_value("Global Defaults", "default_company") or ""

		item_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"

		filters = {"sales_channel": sales_channel, "enabled": 1}
		if sellable_only:
			filters["allow_sellable_stock"] = 1

		sources = frappe.get_all(
			"Channel Inventory Source",
			filters=filters,
			fields=["warehouse", "priority", "allow_sellable_stock", "allow_fulfillment"],
			order_by="priority asc, creation asc",
		)

		warehouse_snapshots: List[WarehouseInventorySnapshot] = []
		seen_warehouses = set()

		agg_actual = 0.0
		agg_reserved = 0.0
		agg_ordered = 0.0
		agg_indented = 0.0
		agg_planned = 0.0
		agg_projected = 0.0

		for src in sources:
			wh = src.warehouse
			if wh in seen_warehouses:
				continue
			seen_warehouses.add(wh)

			wh_snap = cls.get_warehouse_inventory(item_code, wh)
			wh_snap.allow_sellable_stock = bool(src.allow_sellable_stock)
			wh_snap.allow_fulfillment = bool(src.allow_fulfillment)

			warehouse_snapshots.append(wh_snap)

			agg_actual += wh_snap.actual_qty
			agg_reserved += wh_snap.reserved_qty
			agg_ordered += wh_snap.ordered_qty
			agg_indented += wh_snap.indented_qty
			agg_planned += wh_snap.planned_qty
			agg_projected += wh_snap.projected_qty

		return ChannelInventorySnapshot(
			item_code=item_code,
			sales_channel=sales_channel,
			company=ch_company,
			stock_uom=item_uom,
			warehouses=warehouse_snapshots,
			aggregate_actual_qty=flt(agg_actual, 3),
			aggregate_reserved_qty=flt(agg_reserved, 3),
			aggregate_ordered_qty=flt(agg_ordered, 3),
			aggregate_indented_qty=flt(agg_indented, 3),
			aggregate_planned_qty=flt(agg_planned, 3),
			aggregate_projected_qty=flt(agg_projected, 3),
		)

	@classmethod
	def compare_channel_inventory_with_external(
		cls,
		item_code: str,
		sales_channel: str,
		external_qty: float,
	) -> InventoryComparisonResult:
		"""
		Read-only comparison helper between external reported stock and ERP native raw stock snapshot.
		Does NOT reconcile or mutate either system.
		"""
		snap = cls.get_channel_inventory_snapshot(item_code, sales_channel, sellable_only=True)
		ext_val = flt(external_qty, 3)
		actual_val = flt(snap.aggregate_actual_qty, 3)
		proj_val = flt(snap.aggregate_projected_qty, 3)

		return InventoryComparisonResult(
			item_code=item_code,
			sales_channel=sales_channel,
			external_qty=ext_val,
			erp_aggregate_actual_qty=actual_val,
			erp_aggregate_projected_qty=proj_val,
			delta_actual=flt(ext_val - actual_val, 3),
			delta_projected=flt(ext_val - proj_val, 3),
		)
