# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import math
from typing import List, Optional
import frappe
from frappe import _
from frappe.model.meta import get_field_precision
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt

from bop_erp.inventory.exceptions import WarehouseNotFoundError
from bop_erp.inventory.models import (
	ATPBreakdownWarehouse,
	ChannelATP,
	ChannelATPBreakdown,
	WarehouseATP,
)


def get_stock_precision(doctype: str = "Bin", fieldname: str = "actual_qty") -> int:
	"""
	Resolves the appropriate float precision for stock calculations from Frappe metadata.
	Preserves fractional precision without hardcoding.
	"""
	prec = None
	try:
		meta = frappe.get_meta(doctype)
		field = meta.get_field(fieldname) if meta else None
		prec = get_field_precision(field) if field else None
	except Exception:
		prec = None

	if prec is None:
		try:
			prec = cint(frappe.db.get_default("float_precision")) or 3
		except Exception:
			prec = 3
	return int(prec)


def get_safety_stock(warehouse: str, item_code: Optional[str] = None) -> float:
	"""
	Resolves safety stock buffer for a warehouse and item using hierarchical inheritance:
	1. Specific Item + Warehouse policy override
	2. Item Group + Warehouse policy override
	3. Warehouse default policy override (item_code and item_group are null/empty)
	4. Fallback: 0.0
	"""
	if not warehouse:
		return 0.0

	# 1. Item + Warehouse override
	if item_code:
		item_policy = frappe.db.get_value(
			"Inventory Availability Policy",
			{"warehouse": warehouse, "item_code": item_code, "enabled": 1},
			"safety_stock_qty",
		)
		if item_policy is not None:
			return max(0.0, flt(item_policy))

		# 2. Item Group + Warehouse override
		item_group = frappe.db.get_value("Item", item_code, "item_group")
		if item_group:
			group_policy = frappe.db.get_value(
				"Inventory Availability Policy",
				{"warehouse": warehouse, "item_group": item_group, "enabled": 1},
				"safety_stock_qty",
			)
			if group_policy is not None:
				return max(0.0, flt(group_policy))

	# 3. Warehouse default policy
	wh_default = frappe.db.sql(
		"""
		SELECT safety_stock_qty
		FROM `tabInventory Availability Policy`
		WHERE warehouse = %s
		  AND enabled = 1
		  AND (item_code IS NULL OR item_code = '')
		  AND (item_group IS NULL OR item_group = '')
		ORDER BY modified DESC
		LIMIT 1
		""",
		(warehouse,),
		as_dict=True,
	)
	if wh_default and wh_default[0].get("safety_stock_qty") is not None:
		return max(0.0, flt(wh_default[0].get("safety_stock_qty")))

	return 0.0


def get_effective_reserved_qty(item_code: str, warehouse: str) -> float:
	"""
	Authoritatively derives effective reserved quantity for an item and warehouse.
	Prevents double-counting by strictly partitioning reservations:
	1. Active native Stock Reservation Entries (net of deliveries/transfers/consumption).
	2. Unreserved Sales Order demand: max(0, Bin.reserved_qty - SRE_for_Sales_Orders).
	   Since Bin.reserved_qty includes all open Sales Orders, subtracting SREs tied
	   to Sales Orders guarantees that Sales Orders with active SREs are NOT counted twice.
	3. Manufacturing & subcontract allocations tracked in Bin:
	   (reserved_qty_for_production + reserved_qty_for_sub_contract + reserved_qty_for_production_plan).

	Formula:
	    effective_reserved_qty = net_sre_reserved + unreserved_so_qty + mfg_reserved
	"""
	# 1. Active native Stock Reservation Entries (overall net)
	net_sre_res = frappe.db.sql(
		"""
		SELECT SUM(reserved_qty - delivered_qty - transferred_qty - consumed_qty)
		FROM `tabStock Reservation Entry`
		WHERE docstatus = 1
		  AND item_code = %s
		  AND warehouse = %s
		  AND delivered_qty < reserved_qty
		  AND status NOT IN ('Closed', 'Delivered', 'Cancelled')
		""",
		(item_code, warehouse),
	)
	net_sre_reserved = flt(net_sre_res[0][0]) if net_sre_res and net_sre_res[0][0] is not None else 0.0

	# 2. SO-specific active native SREs to prevent double-counting with Bin.reserved_qty
	so_sre_res = frappe.db.sql(
		"""
		SELECT SUM(reserved_qty - delivered_qty - transferred_qty - consumed_qty)
		FROM `tabStock Reservation Entry`
		WHERE docstatus = 1
		  AND item_code = %s
		  AND warehouse = %s
		  AND voucher_type = 'Sales Order'
		  AND delivered_qty < reserved_qty
		  AND status NOT IN ('Closed', 'Delivered', 'Cancelled')
		""",
		(item_code, warehouse),
	)
	sre_for_so = flt(so_sre_res[0][0]) if so_sre_res and so_sre_res[0][0] is not None else 0.0

	# 3. Native Bin reservations (Sales Orders and Manufacturing)
	bin_data = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		[
			"reserved_qty",
			"reserved_qty_for_production",
			"reserved_qty_for_sub_contract",
			"reserved_qty_for_production_plan",
		],
		as_dict=True,
	)
	mfg_reserved = 0.0
	unreserved_so_qty = 0.0
	if bin_data:
		mfg_reserved = (
			flt(bin_data.reserved_qty_for_production)
			+ flt(bin_data.reserved_qty_for_sub_contract)
			+ flt(bin_data.reserved_qty_for_production_plan)
		)
		# Bin.reserved_qty tracks all open Sales Orders.
		# Net out SREs already tied to Sales Orders to eliminate double-counting.
		unreserved_so_qty = max(0.0, flt(bin_data.reserved_qty) - sre_for_so)

	prec = get_stock_precision("Bin", "reserved_stock")
	return flt(net_sre_reserved + unreserved_so_qty + mfg_reserved, prec)


def get_warehouse_atp(
	item_code: str,
	warehouse: str,
	allow_sellable_stock: bool = True,
	allow_fulfillment: bool = True,
) -> WarehouseATP:
	"""
	Computes point-in-time Available-To-Promise (ATP) for an item in a specific warehouse.
	Formula:
	    candidate_atp_qty = max(0, actual_qty - effective_reserved_qty - safety_stock_qty)
	If allow_sellable_stock is False, candidate_atp_qty is clamped to 0.0.
	Does NOT add incoming stock (ordered, indented, planned, projected).
	"""
	wh_data = frappe.db.get_value("Warehouse", warehouse, ["name", "company", "is_group"], as_dict=True)
	if not wh_data:
		raise WarehouseNotFoundError(_("Warehouse '{0}' not found.").format(warehouse))

	item_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
	prec = get_stock_precision("Bin", "actual_qty")

	bin_actual = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		"actual_qty",
	)
	actual_qty = round(flt(bin_actual), prec) if bin_actual is not None else 0.0

	sre_reserved = frappe.qb.DocType("Stock Reservation Entry")
	sre_res = (
		frappe.qb.from_(sre_reserved)
		.select(Sum(sre_reserved.reserved_qty - sre_reserved.delivered_qty - sre_reserved.transferred_qty - sre_reserved.consumed_qty))
		.where(
			(sre_reserved.docstatus == 1)
			& (sre_reserved.item_code == item_code)
			& (sre_reserved.warehouse == warehouse)
			& (sre_reserved.delivered_qty < sre_reserved.reserved_qty)
			& (sre_reserved.status.notin(["Closed", "Delivered", "Cancelled"]))
		)
	).run()[0][0]
	native_reserved_qty = round(flt(sre_res), prec) if sre_res is not None else 0.0

	effective_reserved_qty = round(flt(get_effective_reserved_qty(item_code, warehouse)), prec)
	safety_stock_qty = round(flt(get_safety_stock(warehouse, item_code)), prec)

	if not allow_sellable_stock:
		candidate_atp = 0.0
	else:
		raw_atp = actual_qty - effective_reserved_qty - safety_stock_qty
		candidate_atp = max(0.0, round(flt(raw_atp), prec))

	return WarehouseATP(
		item_code=item_code,
		warehouse=warehouse,
		company=wh_data.company or "",
		actual_qty=actual_qty,
		native_reserved_qty=native_reserved_qty,
		effective_reserved_qty=effective_reserved_qty,
		safety_stock_qty=safety_stock_qty,
		candidate_atp_qty=candidate_atp,
		stock_uom=item_uom,
		allow_sellable_stock=allow_sellable_stock,
		allow_fulfillment=allow_fulfillment,
	)


def get_channel_atp(item_code: str, sales_channel: str) -> ChannelATP:
	"""
	Aggregates Available-To-Promise (ATP) across all enabled Channel Inventory Sources for a sales channel.
	Sources with allow_sellable_stock = 0 contribute 0 ATP.
	Priority controls allocation routing order, but does not alter aggregate quantity.
	"""
	ch_data = frappe.db.get_value("Sales Channel", sales_channel, ["name", "company"], as_dict=True)
	ch_company = ch_data.company if ch_data else (frappe.db.get_single_value("Global Defaults", "default_company") or "")

	item_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
	prec = get_stock_precision("Bin", "actual_qty")

	sources = frappe.get_all(
		"Channel Inventory Source",
		filters={"sales_channel": sales_channel, "enabled": 1},
		fields=["warehouse", "priority", "allow_sellable_stock", "allow_fulfillment"],
		order_by="priority asc, creation asc",
	)

	warehouse_atps: List[WarehouseATP] = []
	seen_warehouses = set()

	agg_actual = 0.0
	agg_reserved = 0.0
	agg_safety = 0.0
	agg_atp = 0.0

	for src in sources:
		wh = src.get("warehouse") if isinstance(src, dict) else src.warehouse
		allow_sellable = bool(src.get("allow_sellable_stock") if isinstance(src, dict) else src.allow_sellable_stock)
		allow_fulfill = bool(src.get("allow_fulfillment") if isinstance(src, dict) else src.allow_fulfillment)

		if wh in seen_warehouses:
			continue
		seen_warehouses.add(wh)

		wh_atp = get_warehouse_atp(
			item_code=item_code,
			warehouse=wh,
			allow_sellable_stock=allow_sellable,
			allow_fulfillment=allow_fulfill,
		)
		warehouse_atps.append(wh_atp)

		agg_actual += wh_atp.actual_qty
		agg_reserved += wh_atp.effective_reserved_qty
		agg_safety += wh_atp.safety_stock_qty
		if wh_atp.allow_sellable_stock:
			agg_atp += wh_atp.candidate_atp_qty

	return ChannelATP(
		item_code=item_code,
		sales_channel=sales_channel,
		company=ch_company,
		warehouses=warehouse_atps,
		aggregate_actual_qty=round(flt(agg_actual), prec),
		aggregate_reserved_qty=round(flt(agg_reserved), prec),
		aggregate_safety_stock_qty=round(flt(agg_safety), prec),
		aggregate_atp_qty=round(flt(agg_atp), prec),
		stock_uom=item_uom,
	)


def get_product_bundle_atp(
	bundle_item_code: str,
	sales_channel: Optional[str] = None,
	warehouse: Optional[str] = None,
) -> float:
	"""
	Read-only ATP calculation for native ERPNext Product Bundle (Kit).
	Formula:
	    min(floor(component_atp / required_component_qty)) across all required stock components.
	Parent bundle itself is not reserved as physical stock.
	"""
	bundle_doc = frappe.db.get_value(
		"Product Bundle",
		{"new_item_code": bundle_item_code, "disabled": 0},
		["name"],
		as_dict=True,
	)
	if not bundle_doc:
		# If not a product bundle, return direct item ATP
		if sales_channel:
			return get_channel_atp(bundle_item_code, sales_channel).aggregate_atp_qty
		elif warehouse:
			return get_warehouse_atp(bundle_item_code, warehouse).candidate_atp_qty
		return 0.0

	bundle_items = frappe.get_all(
		"Product Bundle Item",
		filters={"parent": bundle_doc.name},
		fields=["item_code", "qty", "uom"],
	)
	if not bundle_items:
		return 0.0

	possible_bundles = []
	for b_item in bundle_items:
		item_cd = b_item.get("item_code") if isinstance(b_item, dict) else b_item.item_code
		req_qty = flt(b_item.get("qty") if isinstance(b_item, dict) else b_item.qty)
		if req_qty <= 0:
			continue

		if sales_channel:
			comp_atp = get_channel_atp(item_cd, sales_channel).aggregate_atp_qty
		elif warehouse:
			comp_atp = get_warehouse_atp(item_cd, warehouse).candidate_atp_qty
		else:
			comp_atp = 0.0

		bundles_for_component = math.floor(comp_atp / req_qty)
		possible_bundles.append(bundles_for_component)

	if not possible_bundles:
		return 0.0

	return float(max(0, min(possible_bundles)))


def get_atp_breakdown(item_code: str, sales_channel: str) -> ChannelATPBreakdown:
	"""
	Returns an auditable, line-by-line breakdown of how Channel ATP was derived.
	Useful for diagnostics, explanations, and admin desk view.
	"""
	ch_data = frappe.db.get_value("Sales Channel", sales_channel, ["name", "company"], as_dict=True)
	ch_company = ch_data.get("company", "") if ch_data else ""
	item_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"

	sources = frappe.get_all(
		"Channel Inventory Source",
		filters={"sales_channel": sales_channel, "enabled": 1},
		fields=["warehouse", "priority", "allow_sellable_stock", "allow_fulfillment"],
		order_by="priority asc, creation asc",
	)

	lines: List[ATPBreakdownWarehouse] = []
	total_atp = 0.0
	prec = get_stock_precision("Bin", "actual_qty")
	seen = set()

	for src in sources:
		wh = src.get("warehouse") if isinstance(src, dict) else src.warehouse
		allow_sellable = bool(src.get("allow_sellable_stock") if isinstance(src, dict) else src.allow_sellable_stock)
		allow_fulfill = bool(src.get("allow_fulfillment") if isinstance(src, dict) else src.allow_fulfillment)
		prio = int(src.get("priority", 0) if isinstance(src, dict) else (src.priority or 0))

		if wh in seen:
			continue
		seen.add(wh)

		wh_atp = get_warehouse_atp(
			item_code=item_code,
			warehouse=wh,
			allow_sellable_stock=allow_sellable,
			allow_fulfillment=allow_fulfill,
		)
		lines.append(
			ATPBreakdownWarehouse(
				warehouse=wh,
				actual_qty=wh_atp.actual_qty,
				reserved_qty=wh_atp.effective_reserved_qty,
				safety_stock_qty=wh_atp.safety_stock_qty,
				atp_qty=wh_atp.candidate_atp_qty,
				allow_sellable_stock=wh_atp.allow_sellable_stock,
				priority=prio,
			)
		)
		if wh_atp.allow_sellable_stock:
			total_atp += wh_atp.candidate_atp_qty

	return ChannelATPBreakdown(
		item_code=item_code,
		sales_channel=sales_channel,
		company=ch_company,
		lines=lines,
		channel_atp=round(flt(total_atp), prec),
		stock_uom=item_uom,
	)
