# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional
import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext.stock.utils import get_or_make_bin
from bop_erp.inventory.availability import (
	get_channel_atp,
	get_effective_reserved_qty,
	get_stock_precision,
	get_warehouse_atp,
)
from bop_erp.inventory.exceptions import (
	InsufficientStockToReserveError,
	InvalidReservationRequestError,
	ReservationNotFoundError,
)
from bop_erp.inventory.models import (
	ReservationAllocation,
	ReservationResult,
	ReservationSnapshot,
)


def lock_inventory_scope(item_code: str, warehouses: List[str]) -> None:
	"""
	Acquires exclusive database row-level locks on tabBin for the given item and warehouses.
	To prevent deadlocks across concurrent threads, warehouses are locked in strict deterministic
	alphabetical order.
	"""
	sorted_warehouses = sorted(set(warehouses))
	for wh in sorted_warehouses:
		# Ensure the Bin row exists in the database before acquiring the row lock
		get_or_make_bin(item_code, wh)
		frappe.db.sql(
			"""
			SELECT name, actual_qty, reserved_stock
			FROM `tabBin`
			WHERE item_code = %s AND warehouse = %s
			FOR UPDATE
			""",
			(item_code, wh),
		)


def get_reservation_snapshot(item_code: str, warehouse: str) -> ReservationSnapshot:
	"""
	Returns an auditable snapshot of active Stock Reservation Entries for an item and warehouse.
	"""
	wh_company = frappe.db.get_value("Warehouse", warehouse, "company") or ""
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
	prec = get_stock_precision("Bin", "reserved_stock")

	sres = frappe.get_all(
		"Stock Reservation Entry",
		filters={
			"item_code": item_code,
			"warehouse": warehouse,
			"docstatus": 1,
			"status": ["not in", ["Closed", "Delivered", "Cancelled"]],
		},
		fields=[
			"name",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"reserved_qty",
			"delivered_qty",
			"transferred_qty",
			"consumed_qty",
			"status",
			"creation",
			"modified",
			"owner",
		],
		order_by="creation asc",
	)

	tot_reserved = 0.0
	tot_delivered = 0.0
	for s in sres:
		tot_reserved += flt(s.reserved_qty)
		tot_delivered += flt(s.delivered_qty) + flt(s.transferred_qty) + flt(s.consumed_qty)

	net_reserved = max(0.0, tot_reserved - tot_delivered)

	return ReservationSnapshot(
		item_code=item_code,
		warehouse=warehouse,
		company=wh_company,
		total_reserved_qty=flt(tot_reserved, prec),
		total_delivered_qty=flt(tot_delivered, prec),
		net_reserved_qty=flt(net_reserved, prec),
		stock_uom=stock_uom,
		active_reservations=sres,
	)


def reserve_stock(
	item_code: str,
	warehouse: str,
	requested_qty: float,
	voucher_type: str = "Sales Order",
	voucher_no: Optional[str] = None,
	voucher_detail_no: Optional[str] = None,
	idempotency_key: Optional[str] = None,
	source_doctype: Optional[str] = None,
	source_document: Optional[str] = None,
	source_document_item: Optional[str] = None,
) -> ReservationResult:
	"""
	Concurrency-safe single-warehouse reservation service.
	Guarantees anti-overselling via deterministic row-level locking on tabBin.
	Wraps native ERPNext Stock Reservation Entry mechanisms.
	Enforces idempotency via Inventory Reservation Reference.
	"""
	prec = get_stock_precision("Bin", "actual_qty")
	requested_qty = flt(requested_qty, prec)

	if requested_qty <= 0:
		raise InvalidReservationRequestError(_("Requested reservation quantity must be greater than 0."))

	# 1. Check Idempotency before locking
	if idempotency_key:
		existing_ref = frappe.db.get_value(
			"Inventory Reservation Reference",
			{"idempotency_key": idempotency_key},
			["name", "stock_reservation_entry", "reserved_qty", "warehouse", "status"],
			as_dict=True,
		)
		if existing_ref and existing_ref.status in ("Reserved", "Partially Released"):
			return ReservationResult(
				success=True,
				item_code=item_code,
				requested_qty=requested_qty,
				reserved_qty=flt(existing_ref.reserved_qty, prec),
				allocations=[
					ReservationAllocation(
						warehouse=existing_ref.warehouse,
						allocated_qty=flt(existing_ref.reserved_qty, prec),
						stock_reservation_entry=existing_ref.stock_reservation_entry,
						idempotency_key=idempotency_key,
					)
				],
				idempotency_key=idempotency_key,
				is_idempotent_replay=True,
			)

	# 2. Acquire exclusive DB row lock on (item_code, warehouse)
	lock_inventory_scope(item_code, [warehouse])

	# 3. In-Transaction ATP Check under lock
	wh_atp = get_warehouse_atp(item_code, warehouse)
	if requested_qty > wh_atp.candidate_atp_qty:
		raise InsufficientStockToReserveError(
			_(
				"Cannot reserve {0} units of {1} in warehouse {2}. Available ATP is {3} (Actual: {4}, Effective Reserved: {5}, Safety Stock: {6})."
			).format(
				requested_qty,
				item_code,
				warehouse,
				wh_atp.candidate_atp_qty,
				wh_atp.actual_qty,
				wh_atp.effective_reserved_qty,
				wh_atp.safety_stock_qty,
			)
		)

	# 4. Create native Stock Reservation Entry
	item_doc = frappe.get_cached_value(
		"Item", item_code, ["is_stock_item", "has_serial_no", "has_batch_no", "stock_uom"], as_dict=True
	)
	if not item_doc or not item_doc.is_stock_item:
		raise InvalidReservationRequestError(_("Item {0} is not a valid stock item.").format(item_code))

	wh_company = frappe.db.get_value("Warehouse", warehouse, "company") or ""

	# 4. Create and Submit native Stock Reservation Entry
	ref_key = idempotency_key or f"SRE-REF-{frappe.generate_hash(length=12)}"
	resolved_voucher_type = voucher_type or "Work Order"
	resolved_voucher_no = voucher_no
	resolved_voucher_detail_no = voucher_detail_no

	if not resolved_voucher_no:
		resolved_voucher_type = "Work Order"
		resolved_voucher_no = f"RES-{ref_key}"
		resolved_voucher_detail_no = f"ITEM-{ref_key}"
	elif not resolved_voucher_detail_no:
		resolved_voucher_detail_no = f"ITEM-{resolved_voucher_no}"

	sre = frappe.new_doc("Stock Reservation Entry")
	sre.item_code = item_code
	sre.warehouse = warehouse
	sre.voucher_type = resolved_voucher_type
	sre.voucher_no = resolved_voucher_no
	sre.voucher_detail_no = resolved_voucher_detail_no
	sre.available_qty = wh_atp.actual_qty
	sre.voucher_qty = requested_qty
	sre.reserved_qty = requested_qty
	sre.company = wh_company
	sre.stock_uom = item_doc.stock_uom
	sre.has_serial_no = item_doc.has_serial_no
	sre.has_batch_no = item_doc.has_batch_no
	sre.reservation_based_on = "Qty"

	sre.flags.ignore_links = True
	sre.flags.ignore_validate = False
	sre.insert(ignore_permissions=True)
	sre.submit()

	# 5. Record Idempotency / Reference Mapping
	ref_key = idempotency_key or f"SRE-REF-{sre.name}"
	ref_doc = frappe.get_doc(
		{
			"doctype": "Inventory Reservation Reference",
			"idempotency_key": ref_key,
			"stock_reservation_entry": sre.name,
			"status": "Reserved",
			"item_code": item_code,
			"warehouse": warehouse,
			"reserved_qty": requested_qty,
			"source_doctype": source_doctype or voucher_type,
			"source_document": source_document or voucher_no,
			"source_document_item": source_document_item or voucher_detail_no,
		}
	)
	ref_doc.insert(ignore_permissions=True)

	return ReservationResult(
		success=True,
		item_code=item_code,
		requested_qty=requested_qty,
		reserved_qty=requested_qty,
		allocations=[
			ReservationAllocation(
				warehouse=warehouse,
				allocated_qty=requested_qty,
				stock_reservation_entry=sre.name,
				idempotency_key=ref_key,
			)
		],
		idempotency_key=ref_key,
		is_idempotent_replay=False,
	)


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
	"""
	Multi-warehouse reservation engine across enabled Channel Inventory Sources.
	- Deterministic Deadlock-free Locking: sorts all candidate warehouses canonically before acquiring locks.
	- All-or-Nothing Default: rejects whole request if total ATP < requested_qty unless allow_partial is True.
	- Priority-based Sequential Allocation: fulfills demand according to Channel Inventory Source.priority.
	- Native ERPNext SRE creation per allocated warehouse.
	- Idempotent request replay safety.
	"""
	prec = get_stock_precision("Bin", "actual_qty")
	requested_qty = flt(requested_qty, prec)

	if requested_qty <= 0:
		raise InvalidReservationRequestError(_("Requested reservation quantity must be greater than 0."))

	# 1. Idempotency Check
	if idempotency_key:
		existing_refs = frappe.get_all(
			"Inventory Reservation Reference",
			filters={"idempotency_key": ["like", f"{idempotency_key}%"], "status": "Reserved"},
			fields=["name", "stock_reservation_entry", "reserved_qty", "warehouse", "idempotency_key"],
		)
		if existing_refs:
			allocations = [
				ReservationAllocation(
					warehouse=r.warehouse,
					allocated_qty=flt(r.reserved_qty, prec),
					stock_reservation_entry=r.stock_reservation_entry,
					idempotency_key=r.idempotency_key,
				)
				for r in existing_refs
			]
			tot_res = sum(a.allocated_qty for a in allocations)
			return ReservationResult(
				success=True,
				item_code=item_code,
				requested_qty=requested_qty,
				reserved_qty=flt(tot_res, prec),
				allocations=allocations,
				idempotency_key=idempotency_key,
				is_idempotent_replay=True,
			)

	# 2. Query eligible Channel Inventory Sources
	sources = frappe.get_all(
		"Channel Inventory Source",
		filters={"sales_channel": sales_channel, "enabled": 1, "allow_sellable_stock": 1},
		fields=["warehouse", "priority"],
		order_by="priority asc, creation asc",
	)
	if not sources:
		raise InsufficientStockToReserveError(
			_("No enabled sellable inventory sources configured for Sales Channel '{0}'.").format(sales_channel)
		)

	candidate_warehouses = [s.warehouse for s in sources]

	# 3. Deterministic locking of all eligible warehouses
	lock_inventory_scope(item_code, candidate_warehouses)

	# 4. In-Transaction ATP Evaluation
	wh_atp_map = {}
	total_atp = 0.0
	for wh in candidate_warehouses:
		atp = get_warehouse_atp(item_code, wh).candidate_atp_qty
		wh_atp_map[wh] = atp
		total_atp += atp

	if requested_qty > total_atp and not allow_partial:
		raise InsufficientStockToReserveError(
			_(
				"All-or-Nothing check failed: Requested {0} units of {1}, but total channel ATP is only {2} across {3} sources."
			).format(requested_qty, item_code, total_atp, len(candidate_warehouses))
		)

	# 5. Sequential Priority Allocation
	remaining_demand = requested_qty
	allocations: List[ReservationAllocation] = []

	for s in sources:
		wh = s.warehouse
		avail = wh_atp_map.get(wh, 0.0)
		if avail <= 0:
			continue

		alloc_qty = min(remaining_demand, avail)
		alloc_qty = flt(alloc_qty, prec)
		if alloc_qty <= 0:
			continue

		sub_idem_key = f"{idempotency_key}:{wh}" if idempotency_key else None
		res = reserve_stock(
			item_code=item_code,
			warehouse=wh,
			requested_qty=alloc_qty,
			voucher_type=voucher_type,
			voucher_no=voucher_no,
			voucher_detail_no=voucher_detail_no,
			idempotency_key=sub_idem_key,
			source_doctype=source_doctype,
			source_document=source_document,
			source_document_item=source_document_item,
		)
		allocations.extend(res.allocations)
		remaining_demand = flt(remaining_demand - alloc_qty, prec)

		if remaining_demand <= 0:
			break

	tot_reserved = sum(a.allocated_qty for a in allocations)
	return ReservationResult(
		success=True,
		item_code=item_code,
		requested_qty=requested_qty,
		reserved_qty=flt(tot_reserved, prec),
		allocations=allocations,
		idempotency_key=idempotency_key,
		is_idempotent_replay=False,
	)


def release_stock_reservation(
	reservation_entry_name: str,
	qty: Optional[float] = None,
	reason: Optional[str] = None,
) -> bool:
	"""
	Releases a native Stock Reservation Entry and restores ATP.
	If qty is None or >= remaining reserved qty, cancels the SRE.
	If qty < remaining, reduces SRE reserved quantity.
	Updates tracking in Inventory Reservation Reference.
	"""
	if not frappe.db.exists("Stock Reservation Entry", reservation_entry_name):
		raise ReservationNotFoundError(
			_("Stock Reservation Entry '{0}' not found.").format(reservation_entry_name)
		)

	sre = frappe.get_doc("Stock Reservation Entry", reservation_entry_name)
	if sre.docstatus == 2:
		return True  # Already cancelled

	# Lock the Bin row during release
	lock_inventory_scope(sre.item_code, [sre.warehouse])

	net_reserved = flt(sre.reserved_qty) - flt(sre.delivered_qty)

	if qty is None or flt(qty) >= net_reserved:
		# Full cancellation
		sre.reload()
		sre.cancel()
		frappe.db.set_value(
			"Inventory Reservation Reference",
			{"stock_reservation_entry": reservation_entry_name},
			"status",
			"Cancelled",
		)
	else:
		# Partial release
		new_reserved = flt(sre.reserved_qty) - flt(qty)
		sre.db_set("reserved_qty", new_reserved)
		sre.update_reserved_stock_in_bin()
		frappe.db.set_value(
			"Inventory Reservation Reference",
			{"stock_reservation_entry": reservation_entry_name},
			{"status": "Partially Released", "reserved_qty": new_reserved},
		)

	return True
