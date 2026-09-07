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
	ChannelDemandBreakdown,
	EffectiveReservedBreakdown,
	WarehouseATP,
)
from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
	get_sre_reserved_qty_for_item_and_warehouse,
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


def get_effective_reserved_breakdown(item_code: str, warehouse: str) -> EffectiveReservedBreakdown:
	"""
	Computes an auditable, mutually exclusive breakdown of effective reserved demand
	for an item in a specific warehouse, mirroring native ERPNext v16.32.3 semantics.

	Buckets:
	1. sales_order_demand:
	   max(Bin.reserved_qty, SREs tied to Sales Orders for this warehouse)
	2. standalone_sre_demand:
	   Active SREs not tied to Sales Orders, Work Orders, Production Plans, or Subcontracting Orders.
	3. production_demand:
	   Bin.reserved_qty_for_production
	4. subcontract_demand:
	   Bin.reserved_qty_for_sub_contract
	5. production_plan_demand:
	   Bin.reserved_qty_for_production_plan

	Their sum equals total_effective_reserved.
	Also exposes native_reserved_stock (Bin.reserved_stock) for diagnostics/consistency checking.
	"""
	prec = get_stock_precision("Bin", "reserved_stock")

	# 1. Native active SRE total for item and warehouse via ERPNext v16.32.3 helper
	try:
		native_sre_total = flt(get_sre_reserved_qty_for_item_and_warehouse(item_code, warehouse), prec)
	except Exception:
		# Fallback if QB context unavailable
		sre_res = frappe.db.sql(
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
		native_sre_total = flt(sre_res[0][0], prec) if sre_res and sre_res[0][0] is not None else 0.0

	# 2. SRE breakdown by voucher_type and voucher_no
	sre_voucher_rows = frappe.db.sql(
		"""
		SELECT 
			COALESCE(voucher_type, '') as voucher_type,
			COALESCE(voucher_no, '') as voucher_no,
			SUM(reserved_qty - delivered_qty - transferred_qty - consumed_qty) as net_qty
		FROM `tabStock Reservation Entry`
		WHERE docstatus = 1
		  AND item_code = %s
		  AND warehouse = %s
		  AND delivered_qty < reserved_qty
		  AND status NOT IN ('Closed', 'Delivered', 'Cancelled')
		GROUP BY voucher_type, voucher_no
		""",
		(item_code, warehouse),
		as_dict=True,
	)
	sre_for_so = 0.0
	sre_for_wo = 0.0
	sre_for_pp = 0.0
	sre_for_sco = 0.0
	standalone_sre_demand = 0.0

	known_demand_vouchers = {
		"Sales Order",
		"Work Order",
		"Production Plan",
		"Subcontracting Order",
		"Subcontracting Inward Order",
	}

	if sre_voucher_rows:
		for row in sre_voucher_rows:
			if isinstance(row, dict):
				vtype = row.get("voucher_type") or ""
				vno = row.get("voucher_no") or ""
				nqty = flt(row.get("net_qty"), prec)
			elif isinstance(row, (list, tuple)):
				vtype = row[0] if len(row) > 0 and row[0] else ""
				vno = row[1] if len(row) > 1 and isinstance(row[1], str) else ""
				nqty = flt(row[-1], prec) if len(row) > 1 else 0.0
			else:
				vtype = getattr(row, "voucher_type", "")
				vno = getattr(row, "voucher_no", "")
				nqty = flt(getattr(row, "net_qty", 0.0), prec)

			# SREs with synthetic RES- voucher numbers or without native demand voucher
			# represent standalone channel/Bop reservations.
			if str(vno).startswith("RES-") or not vtype or vtype not in known_demand_vouchers:
				standalone_sre_demand += nqty
			elif vtype == "Sales Order":
				sre_for_so += nqty
			elif vtype == "Work Order":
				sre_for_wo += nqty
			elif vtype == "Production Plan":
				sre_for_pp += nqty
			elif vtype in ("Subcontracting Order", "Subcontracting Inward Order"):
				sre_for_sco += nqty
			else:
				standalone_sre_demand += nqty

	standalone_sre_demand = flt(standalone_sre_demand, prec)
	sre_for_so = flt(sre_for_so, prec)
	sre_for_wo = flt(sre_for_wo, prec)
	sre_for_pp = flt(sre_for_pp, prec)
	sre_for_sco = flt(sre_for_sco, prec)

	# 3. Native Bin quantities
	bin_data = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		[
			"reserved_qty",
			"reserved_stock",
			"reserved_qty_for_production",
			"reserved_qty_for_sub_contract",
			"reserved_qty_for_production_plan",
		],
		as_dict=True,
	)

	bin_reserved_qty = flt(bin_data.reserved_qty, prec) if bin_data else 0.0
	bin_reserved_stock = flt(bin_data.reserved_stock, prec) if bin_data else 0.0
	bin_production_qty = flt(bin_data.reserved_qty_for_production, prec) if bin_data else 0.0
	bin_subcontract_qty = flt(bin_data.reserved_qty_for_sub_contract, prec) if bin_data else 0.0
	bin_pp_qty = flt(bin_data.reserved_qty_for_production_plan, prec) if bin_data else 0.0

	# Reconcile native Bin demands with active SREs tied to those vouchers without double counting:
	sales_order_demand = flt(max(bin_reserved_qty, sre_for_so), prec)
	production_demand = flt(max(bin_production_qty, sre_for_wo), prec)
	subcontract_demand = flt(max(bin_subcontract_qty, sre_for_sco), prec)
	production_plan_demand = flt(max(bin_pp_qty, sre_for_pp), prec)

	total_effective = flt(
		sales_order_demand
		+ standalone_sre_demand
		+ production_demand
		+ subcontract_demand
		+ production_plan_demand,
		prec,
	)

	return EffectiveReservedBreakdown(
		item_code=item_code,
		warehouse=warehouse,
		sales_order_demand=sales_order_demand,
		standalone_sre_demand=standalone_sre_demand,
		production_demand=production_demand,
		subcontract_demand=subcontract_demand,
		production_plan_demand=production_plan_demand,
		total_effective_reserved=total_effective,
		native_reserved_stock=bin_reserved_stock,
	)


def get_effective_reserved_qty(item_code: str, warehouse: str) -> float:
	"""
	Authoritatively derives effective reserved quantity for an item and warehouse.
	Prevents double-counting by strictly partitioning reservations:
	1. sales_order_demand: max(Bin.reserved_qty, SREs tied to Sales Orders)
	2. standalone_sre_demand: SREs not tied to native demand buckets
	3. production_demand: Bin.reserved_qty_for_production
	4. subcontract_demand: Bin.reserved_qty_for_sub_contract
	5. production_plan_demand: Bin.reserved_qty_for_production_plan

	Formula:
	    effective_reserved_qty = sales_order_demand + standalone_sre_demand + production_demand + subcontract_demand + production_plan_demand
	"""
	breakdown = get_effective_reserved_breakdown(item_code, warehouse)
	return breakdown.total_effective_reserved


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

	try:
		sre_res = get_sre_reserved_qty_for_item_and_warehouse(item_code, warehouse)
	except Exception:
		sre_res = frappe.db.sql(
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
		sre_res = flt(sre_res[0][0]) if sre_res and sre_res[0][0] is not None else 0.0
	native_reserved_qty = round(flt(sre_res), prec)

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
	Aggregates Available-To-Promise (ATP) across all enabled sellable Channel Inventory Sources for a sales channel.
	Sources with allow_sellable_stock = 0 contribute 0 ATP and their inventory/deficits do not affect sellable aggregates.
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
	sellable_warehouses: List[str] = []

	agg_actual = 0.0
	agg_reserved = 0.0
	agg_safety = 0.0
	agg_atp = 0.0

	total_so_demand = 0.0
	total_standalone_sre = 0.0
	total_production_demand = 0.0
	total_subcontract_demand = 0.0
	total_prod_plan_demand = 0.0

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

		# SECTION 8 & 9: Only enabled sellable sources contribute to sellable channel aggregates.
		# Non-sellable sources contribute 0 ATP and their inventory/deficits do NOT alter sellable totals.
		if allow_sellable:
			sellable_warehouses.append(wh)
			agg_actual += wh_atp.actual_qty
			agg_reserved += wh_atp.effective_reserved_qty
			agg_safety += wh_atp.safety_stock_qty
			agg_atp += wh_atp.candidate_atp_qty

			try:
				bd = get_effective_reserved_breakdown(item_code, wh)
				total_so_demand += bd.sales_order_demand
				total_standalone_sre += bd.standalone_sre_demand
				total_production_demand += bd.production_demand
				total_subcontract_demand += bd.subcontract_demand
				total_prod_plan_demand += bd.production_plan_demand
			except Exception:
				pass

	cross_overlap_qty = 0.0

	# Cross-Warehouse Sales Order Demand De-duplication:
	# If a Sales Order item targets warehouse A (sellable in this channel), but has active SRE allocations
	# in other sellable warehouses B/C (also in this channel), the demand is represented in Bin A.reserved_qty
	# and also in Bin B/C.reserved_stock.
	# At the aggregate channel level, de-duplicate this overlap so aggregate_reserved_qty
	# and aggregate_atp_qty reflect exact net logical demand across channel sellable warehouses.
	# SECTION 10: Only warehouses in sellable_warehouses are included; excluded warehouses do not deduct/add.
	if len(sellable_warehouses) > 1:
		cross_wh_sres = frappe.db.sql(
			"""
			SELECT 
				sre.reserved_qty - sre.delivered_qty - sre.transferred_qty - sre.consumed_qty as net_qty
			FROM `tabStock Reservation Entry` sre
			JOIN `tabSales Order Item` soi ON soi.name = sre.voucher_detail_no
			WHERE sre.docstatus = 1
			  AND sre.item_code = %s
			  AND sre.voucher_type = 'Sales Order'
			  AND sre.delivered_qty < sre.reserved_qty
			  AND sre.status NOT IN ('Closed', 'Delivered', 'Cancelled')
			  AND sre.warehouse IN ({wh_placeholders})
			  AND soi.warehouse IN ({wh_placeholders})
			  AND sre.warehouse != soi.warehouse
			""".format(
				wh_placeholders=", ".join(["%s"] * len(sellable_warehouses))
			),
			tuple([item_code] + sellable_warehouses + sellable_warehouses),
			as_dict=True,
		)
		cross_overlap_qty = sum(flt(r.net_qty) for r in cross_wh_sres) if cross_wh_sres else 0.0
		if cross_overlap_qty > 0:
			agg_reserved = max(0.0, agg_reserved - cross_overlap_qty)
			max_recoverable = max(0.0, agg_actual - agg_reserved - agg_safety - agg_atp)
			effective_cross_overlap = min(cross_overlap_qty, max_recoverable)
			agg_atp = min(agg_actual, max(0.0, agg_atp + effective_cross_overlap))

	demand_breakdown = ChannelDemandBreakdown(
		sales_order_demand=round(flt(total_so_demand), prec),
		cross_warehouse_sales_order_demand=round(flt(cross_overlap_qty), prec),
		standalone_sre_demand=round(flt(total_standalone_sre), prec),
		production_demand=round(flt(total_production_demand), prec),
		subcontract_demand=round(flt(total_subcontract_demand), prec),
		production_plan_demand=round(flt(total_prod_plan_demand), prec),
		warehouse_local_commitments=round(
			flt(total_standalone_sre + total_production_demand + total_subcontract_demand + total_prod_plan_demand),
			prec,
		),
		safety_stock=round(flt(agg_safety), prec),
		total_demand=round(flt(agg_reserved + agg_safety), prec),
	)

	return ChannelATP(
		item_code=item_code,
		sales_channel=sales_channel,
		company=ch_company,
		warehouses=warehouse_atps,
		aggregate_actual_qty=round(flt(agg_actual), prec),
		aggregate_reserved_qty=round(flt(agg_reserved), prec),
		aggregate_safety_stock_qty=round(flt(agg_safety), prec),
		aggregate_atp_qty=round(flt(agg_atp), prec),
		cross_warehouse_adjustments={
			"sales_order_unallocated_demand": round(flt(max(0.0, total_so_demand - cross_overlap_qty)), prec),
			"deduplicated_sre_demand": round(flt(cross_overlap_qty), prec),
		},
		demand_breakdown=demand_breakdown,
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
	ch_atp = get_channel_atp(item_code, sales_channel)

	sources = frappe.get_all(
		"Channel Inventory Source",
		filters={"sales_channel": sales_channel, "enabled": 1},
		fields=["warehouse", "priority"],
		order_by="priority asc, creation asc",
	)
	prio_map = {
		(s.get("warehouse") if isinstance(s, dict) else s.warehouse): int(
			s.get("priority", 0) if isinstance(s, dict) else (s.priority or 0)
		)
		for s in sources
	}

	lines = [
		ATPBreakdownWarehouse(
			warehouse=wh.warehouse,
			actual_qty=wh.actual_qty,
			reserved_qty=wh.effective_reserved_qty,
			safety_stock_qty=wh.safety_stock_qty,
			atp_qty=wh.candidate_atp_qty,
			allow_sellable_stock=wh.allow_sellable_stock,
			priority=prio_map.get(wh.warehouse, 0),
		)
		for wh in ch_atp.warehouses
	]

	return ChannelATPBreakdown(
		item_code=item_code,
		sales_channel=sales_channel,
		company=ch_atp.company,
		lines=lines,
		channel_atp=ch_atp.aggregate_atp_qty,
		cross_warehouse_adjustments=ch_atp.cross_warehouse_adjustments,
		demand_breakdown=ch_atp.demand_breakdown,
		stock_uom=ch_atp.stock_uom,
	)
