# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional
import frappe
from frappe.model.meta import get_field_precision
from frappe.utils import flt

from bop_erp.inventory.availability import (
	get_atp_breakdown,
	get_channel_atp,
	get_effective_reserved_qty,
	get_product_bundle_atp,
	get_safety_stock,
	get_warehouse_atp,
)
from bop_erp.inventory.exceptions import WarehouseNotFoundError
from bop_erp.inventory.models import (
	ChannelATP,
	ChannelATPBreakdown,
	ChannelInventorySnapshot,
	InventoryComparisonResult,
	ReservationResult,
	ReservationSnapshot,
	WarehouseATP,
	WarehouseInventorySnapshot,
)
from bop_erp.inventory.reservations import (
	get_reservation_snapshot,
	release_stock_reservation,
	reserve_channel_stock,
	reserve_stock,
)


class InventoryService:
	"""
	Authoritative Bop ERP inventory service abstraction.
	Provides:
	1. Safe point-in-time native inventory snapshots (Warehouse & Channel).
	2. Formal Available-To-Promise (ATP) calculation layer with safety stock policy.
	3. Anti-overselling, concurrency-safe native Stock Reservation Entry lifecycle.
	4. Read-only external diagnostic comparison.
	Strictly adheres to ERPNext v16.32.3 native quantity semantics.
	"""

	# --- Native Point-In-Time Snapshots ---

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

		meta = frappe.get_meta("Bin")
		field = meta.get_field("actual_qty")
		prec = get_field_precision(field) if field else (frappe.db.get_default("float_precision") or 3)

		if bin_data:
			return WarehouseInventorySnapshot(
				item_code=item_code,
				warehouse=warehouse,
				company=wh_doc.company,
				stock_uom=bin_data.stock_uom or item_uom,
				actual_qty=flt(bin_data.actual_qty, prec),
				reserved_qty=flt(bin_data.reserved_qty, prec),
				ordered_qty=flt(bin_data.ordered_qty, prec),
				indented_qty=flt(bin_data.indented_qty, prec),
				planned_qty=flt(bin_data.planned_qty, prec),
				projected_qty=flt(bin_data.projected_qty, prec),
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

		meta = frappe.get_meta("Bin")
		field = meta.get_field("actual_qty")
		prec = get_field_precision(field) if field else (frappe.db.get_default("float_precision") or 3)

		return ChannelInventorySnapshot(
			item_code=item_code,
			sales_channel=sales_channel,
			company=ch_company,
			stock_uom=item_uom,
			warehouses=warehouse_snapshots,
			aggregate_actual_qty=flt(agg_actual, prec),
			aggregate_reserved_qty=flt(agg_reserved, prec),
			aggregate_ordered_qty=flt(agg_ordered, prec),
			aggregate_indented_qty=flt(agg_indented, prec),
			aggregate_planned_qty=flt(agg_planned, prec),
			aggregate_projected_qty=flt(agg_projected, prec),
		)

	# --- Available-To-Promise (ATP) Layer ---

	@staticmethod
	def get_safety_stock(warehouse: str, item_code: Optional[str] = None) -> float:
		"""Resolves safety stock from Inventory Availability Policy hierarchy."""
		return get_safety_stock(warehouse, item_code)

	@staticmethod
	def get_effective_reserved_qty(item_code: str, warehouse: str) -> float:
		"""Calculates net effective reserved quantity without double-counting."""
		return get_effective_reserved_qty(item_code, warehouse)

	@staticmethod
	def get_warehouse_atp(
		item_code: str,
		warehouse: str,
		allow_sellable_stock: bool = True,
		allow_fulfillment: bool = True,
	) -> WarehouseATP:
		"""Calculates point-in-time Warehouse ATP."""
		return get_warehouse_atp(item_code, warehouse, allow_sellable_stock, allow_fulfillment)

	@staticmethod
	def get_channel_atp(item_code: str, sales_channel: str) -> ChannelATP:
		"""Calculates Channel ATP across enabled sellable sources."""
		return get_channel_atp(item_code, sales_channel)

	@staticmethod
	def get_product_bundle_atp(
		bundle_item_code: str,
		sales_channel: Optional[str] = None,
		warehouse: Optional[str] = None,
	) -> float:
		"""Calculates read-only Product Bundle kit ATP."""
		return get_product_bundle_atp(bundle_item_code, sales_channel, warehouse)

	@staticmethod
	def get_atp_breakdown(item_code: str, sales_channel: str) -> ChannelATPBreakdown:
		"""Returns auditable explanation breakdown for Channel ATP."""
		return get_atp_breakdown(item_code, sales_channel)

	# --- Reservation Lifecycle & Concurrency ---

	@staticmethod
	def reserve_stock(
		item_code: str,
		warehouse: str,
		requested_qty: float,
		voucher_type: str = "Sales Order",
		voucher_no: Optional[str] = None,
		voucher_detail_no: Optional[str] = None,
		allow_partial: bool = False,
		idempotency_key: Optional[str] = None,
		source_doctype: Optional[str] = None,
		source_document: Optional[str] = None,
		source_document_item: Optional[str] = None,
	) -> ReservationResult:
		"""Reserves stock concurrency-safely in a single warehouse."""
		return reserve_stock(
			item_code=item_code,
			warehouse=warehouse,
			requested_qty=requested_qty,
			voucher_type=voucher_type,
			voucher_no=voucher_no,
			voucher_detail_no=voucher_detail_no,
			allow_partial=allow_partial,
			idempotency_key=idempotency_key,
			source_doctype=source_doctype,
			source_document=source_document,
			source_document_item=source_document_item,
		)

	@staticmethod
	def reserve_channel_stock(
		item_code: str,
		sales_channel: str,
		requested_qty: float,
		voucher_type: str = "Sales Order",
		voucher_no: Optional[str] = None,
		voucher_detail_no: Optional[str] = None,
		allow_partial: bool = False,
		idempotency_key: Optional[str] = None,
		source_doctype: Optional[str] = None,
		source_document: Optional[str] = None,
		source_document_item: Optional[str] = None,
	) -> ReservationResult:
		"""Reserves stock across enabled channel sources with deterministic locking & priority allocation."""
		return reserve_channel_stock(
			item_code=item_code,
			sales_channel=sales_channel,
			requested_qty=requested_qty,
			voucher_type=voucher_type,
			voucher_no=voucher_no,
			voucher_detail_no=voucher_detail_no,
			allow_partial=allow_partial,
			idempotency_key=idempotency_key,
			source_doctype=source_doctype,
			source_document=source_document,
			source_document_item=source_document_item,
		)

	@staticmethod
	def release_stock_reservation(
		reservation_entry_name: str,
		qty: Optional[float] = None,
		reason: Optional[str] = None,
	) -> bool:
		"""Releases an active Stock Reservation Entry and restores ATP."""
		return release_stock_reservation(reservation_entry_name, qty, reason)

	@staticmethod
	def get_reservation_snapshot(item_code: str, warehouse: str) -> ReservationSnapshot:
		"""Returns full audit snapshot of active reservations for item and warehouse."""
		return get_reservation_snapshot(item_code, warehouse)

	# --- Read-Only Diagnostic Comparison ---

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
		meta = frappe.get_meta("Bin")
		field = meta.get_field("actual_qty")
		prec = get_field_precision(field) if field else (frappe.db.get_default("float_precision") or 3)

		ext_val = flt(external_qty, prec)
		actual_val = flt(snap.aggregate_actual_qty, prec)
		proj_val = flt(snap.aggregate_projected_qty, prec)

		return InventoryComparisonResult(
			item_code=item_code,
			sales_channel=sales_channel,
			external_qty=ext_val,
			erp_aggregate_actual_qty=actual_val,
			erp_aggregate_projected_qty=proj_val,
			delta_actual=flt(ext_val - actual_val, prec),
			delta_projected=flt(ext_val - proj_val, prec),
		)

	@classmethod
	def compare_channel_atp_with_external(
		cls,
		item_code: str,
		sales_channel: str,
		external_qty: float,
	) -> Dict[str, Any]:
		"""
		Read-only comparison between external reported stock and ERP Available-To-Promise (ATP).
		Does NOT reconcile or mutate either system.
		"""
		atp = cls.get_channel_atp(item_code, sales_channel)
		meta = frappe.get_meta("Bin")
		field = meta.get_field("actual_qty")
		prec = get_field_precision(field) if field else (frappe.db.get_default("float_precision") or 3)

		ext_val = flt(external_qty, prec)
		atp_val = flt(atp.aggregate_atp_qty, prec)
		delta = flt(ext_val - atp_val, prec)

		return {
			"item_code": item_code,
			"sales_channel": sales_channel,
			"external_qty": ext_val,
			"erp_atp_qty": atp_val,
			"delta": delta,
			"external_source": "SOURCE EXTERNAL",
			"erp_source": "SOURCE ERP",
			"timestamp": atp.timestamp,
		}
