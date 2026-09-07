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
	allow_partial: bool = False,
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

	# 1. Fast idempotency check before locking (if already committed by prior request)
	if idempotency_key:
		existing_ref = frappe.db.get_value(
			"Inventory Reservation Reference",
			{"idempotency_key": idempotency_key},
			["name", "stock_reservation_entry", "reserved_qty", "warehouse", "status"],
			as_dict=True,
		)
		if existing_ref and existing_ref.status in ("Reserved", "Partially Released"):
			res_qty = flt(existing_ref.reserved_qty, prec)
			return ReservationResult(
				success=True,
				item_code=item_code,
				requested_qty=requested_qty,
				reserved_qty=res_qty,
				unfulfilled_qty=max(0.0, flt(requested_qty - res_qty, prec)),
				allocations=[
					ReservationAllocation(
						warehouse=existing_ref.warehouse,
						allocated_qty=res_qty,
						stock_reservation_entry=existing_ref.stock_reservation_entry,
						idempotency_key=idempotency_key,
					)
				],
				idempotency_key=idempotency_key,
				is_idempotent_replay=True,
			)

	# 2. Acquire exclusive DB row lock on (item_code, warehouse)
	try:
		lock_inventory_scope(item_code, [warehouse])
	except (frappe.QueryDeadlockError, Exception):
		if idempotency_key:
			frappe.db.rollback()
			existing_ref = frappe.db.get_value(
				"Inventory Reservation Reference",
				{"idempotency_key": idempotency_key},
				["name", "stock_reservation_entry", "reserved_qty", "warehouse", "status"],
				as_dict=True,
			)
			if existing_ref and existing_ref.status in ("Reserved", "Partially Released"):
				res_qty = flt(existing_ref.reserved_qty, prec)
				return ReservationResult(
					success=True,
					item_code=item_code,
					requested_qty=requested_qty,
					reserved_qty=res_qty,
					unfulfilled_qty=max(0.0, flt(requested_qty - res_qty, prec)),
					allocations=[
						ReservationAllocation(
							warehouse=existing_ref.warehouse,
							allocated_qty=res_qty,
							stock_reservation_entry=existing_ref.stock_reservation_entry,
							idempotency_key=idempotency_key,
						)
					],
					idempotency_key=idempotency_key,
					is_idempotent_replay=True,
				)
		raise

	# 2b. CRITICAL CONCURRENCY HARDENING: Re-check idempotency UNDER LOCK
	# If a concurrent worker with the exact same idempotency_key committed while we waited for the lock,
	# converge cleanly to that winning reservation outcome instead of duplicating.
	if idempotency_key:
		existing_ref = frappe.db.get_value(
			"Inventory Reservation Reference",
			{"idempotency_key": idempotency_key},
			["name", "stock_reservation_entry", "reserved_qty", "warehouse", "status"],
			as_dict=True,
		)
		if existing_ref and existing_ref.status in ("Reserved", "Partially Released"):
			res_qty = flt(existing_ref.reserved_qty, prec)
			return ReservationResult(
				success=True,
				item_code=item_code,
				requested_qty=requested_qty,
				reserved_qty=res_qty,
				unfulfilled_qty=max(0.0, flt(requested_qty - res_qty, prec)),
				allocations=[
					ReservationAllocation(
						warehouse=existing_ref.warehouse,
						allocated_qty=res_qty,
						stock_reservation_entry=existing_ref.stock_reservation_entry,
						idempotency_key=idempotency_key,
					)
				],
				idempotency_key=idempotency_key,
				is_idempotent_replay=True,
			)

	# 3. In-Transaction ATP Check under lock
	wh_atp = get_warehouse_atp(item_code, warehouse)
	if requested_qty > wh_atp.candidate_atp_qty:
		if not allow_partial:
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
		alloc_qty = wh_atp.candidate_atp_qty
		if alloc_qty <= 0:
			raise InsufficientStockToReserveError(
				_("No stock available to partially reserve for {0} in warehouse {1}.").format(
					item_code, warehouse
				)
			)
	else:
		alloc_qty = requested_qty

	unfulfilled_qty = max(0.0, flt(requested_qty - alloc_qty, prec))

	# 4. Create native Stock Reservation Entry
	item_doc = frappe.get_cached_value(
		"Item", item_code, ["is_stock_item", "has_serial_no", "has_batch_no", "stock_uom"], as_dict=True
	)
	if not item_doc or not item_doc.is_stock_item:
		raise InvalidReservationRequestError(_("Item {0} is not a valid stock item.").format(item_code))

	wh_company = frappe.db.get_value("Warehouse", warehouse, "company") or ""

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
	sre.voucher_qty = alloc_qty
	sre.reserved_qty = alloc_qty
	sre.company = wh_company
	sre.stock_uom = item_doc.stock_uom
	sre.has_serial_no = item_doc.has_serial_no
	sre.has_batch_no = item_doc.has_batch_no
	sre.reservation_based_on = "Qty"

	sre.flags.ignore_links = True
	sre.flags.ignore_validate = False
	sre.insert(ignore_permissions=True)
	sre.submit()

	# 5. Record Idempotency / Reference Mapping with DB uniqueness guarantee
	ref_key = idempotency_key or f"SRE-REF-{sre.name}"
	try:
		ref_doc = frappe.get_doc(
			{
				"doctype": "Inventory Reservation Reference",
				"idempotency_key": ref_key,
				"stock_reservation_entry": sre.name,
				"status": "Reserved",
				"item_code": item_code,
				"warehouse": warehouse,
				"reserved_qty": alloc_qty,
				"source_doctype": source_doctype or voucher_type,
				"source_document": source_document or voucher_no,
				"source_document_item": source_document_item or voucher_detail_no,
			}
		)
		ref_doc.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		# DB caught concurrent duplicate key insert: cancel our duplicate SRE and return winner
		sre.cancel()
		existing_ref = frappe.db.get_value(
			"Inventory Reservation Reference",
			{"idempotency_key": ref_key},
			["name", "stock_reservation_entry", "reserved_qty", "warehouse", "status"],
			as_dict=True,
		)
		if existing_ref:
			res_qty = flt(existing_ref.reserved_qty, prec)
			return ReservationResult(
				success=True,
				item_code=item_code,
				requested_qty=requested_qty,
				reserved_qty=res_qty,
				unfulfilled_qty=max(0.0, flt(requested_qty - res_qty, prec)),
				allocations=[
					ReservationAllocation(
						warehouse=existing_ref.warehouse,
						allocated_qty=res_qty,
						stock_reservation_entry=existing_ref.stock_reservation_entry,
						idempotency_key=ref_key,
					)
				],
				idempotency_key=ref_key,
				is_idempotent_replay=True,
			)
		raise

	return ReservationResult(
		success=True,
		item_code=item_code,
		requested_qty=requested_qty,
		reserved_qty=alloc_qty,
		unfulfilled_qty=unfulfilled_qty,
		allocations=[
			ReservationAllocation(
				warehouse=warehouse,
				allocated_qty=alloc_qty,
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
	- Savepoint Rollback: if any allocation fails midway, the entire multi-warehouse transaction rolls back cleanly.
	- Priority-based Sequential Allocation: fulfills demand according to Channel Inventory Source.priority.
	- Native ERPNext SRE creation per allocated warehouse.
	- Idempotent request replay safety.
	"""
	prec = get_stock_precision("Bin", "actual_qty")
	requested_qty = flt(requested_qty, prec)

	if requested_qty <= 0:
		raise InvalidReservationRequestError(_("Requested reservation quantity must be greater than 0."))

	# 1. Fast idempotency check before locking
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
				unfulfilled_qty=max(0.0, flt(requested_qty - tot_res, prec)),
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

	# 3b. Re-check idempotency under lock
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
				unfulfilled_qty=max(0.0, flt(requested_qty - tot_res, prec)),
				allocations=allocations,
				idempotency_key=idempotency_key,
				is_idempotent_replay=True,
			)

	# 4. In-Transaction ATP Evaluation under lock
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

	# 5. Sequential Priority Allocation with Savepoint Protection
	sp_name = f"sp_chan_res_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_name)
	try:
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
				allow_partial=False,
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
		unfulfilled_qty = max(0.0, flt(requested_qty - tot_reserved, prec))

		if tot_reserved < requested_qty and not allow_partial:
			raise InsufficientStockToReserveError(
				_("All-or-Nothing check failed: Requested {0} units of {1}, but only {2} units could be allocated.").format(
					requested_qty, item_code, tot_reserved
				)
			)

		return ReservationResult(
			success=True,
			item_code=item_code,
			requested_qty=requested_qty,
			reserved_qty=flt(tot_reserved, prec),
			unfulfilled_qty=unfulfilled_qty,
			allocations=allocations,
			idempotency_key=idempotency_key,
			is_idempotent_replay=False,
		)
	except Exception:
		frappe.db.rollback(save_point=sp_name)
		raise


def release_stock_reservation(
	reservation_entry_name: str,
	qty: Optional[float] = None,
	reason: Optional[str] = None,
) -> bool:
	"""
	Releases a native Stock Reservation Entry and restores ATP.
	Idempotent: Releasing or cancelling an already released/cancelled reservation is a safe no-op.
	If qty is None or >= remaining reserved qty, cancels the SRE.
	If qty < remaining, reduces SRE reserved quantity.
	Updates tracking in Inventory Reservation Reference.
	Never allows negative reserved quantities.
	"""
	if not frappe.db.exists("Stock Reservation Entry", reservation_entry_name):
		# Also check if reservation_entry_name was passed as an idempotency key
		ref_sre = frappe.db.get_value(
			"Inventory Reservation Reference",
			{"idempotency_key": reservation_entry_name},
			"stock_reservation_entry",
		)
		if ref_sre and frappe.db.exists("Stock Reservation Entry", ref_sre):
			reservation_entry_name = ref_sre
		else:
			raise ReservationNotFoundError(
				_("Stock Reservation Entry or Reference '{0}' not found.").format(reservation_entry_name)
			)

	sre = frappe.get_doc("Stock Reservation Entry", reservation_entry_name)

	# Safe idempotency: if already cancelled or closed, do not re-cancel or error
	if sre.docstatus == 2 or sre.status in ("Cancelled", "Closed"):
		return True

	# Lock the Bin row during release
	lock_inventory_scope(sre.item_code, [sre.warehouse])

	net_reserved = flt(sre.reserved_qty) - flt(sre.delivered_qty) - flt(sre.transferred_qty) - flt(sre.consumed_qty)
	if net_reserved <= 0:
		# Nothing left to release (e.g. fully delivered)
		return True

	prec = get_stock_precision("Bin", "reserved_stock")

	if qty is None or flt(qty) >= net_reserved:
		# Full release / cancellation
		sre.reload()
		if sre.docstatus == 1:
			sre.cancel()
		frappe.db.set_value(
			"Inventory Reservation Reference",
			{"stock_reservation_entry": reservation_entry_name},
			"status",
			"Cancelled",
		)
	else:
		# Partial release
		release_qty = min(net_reserved, max(0.0, flt(qty)))
		new_reserved = max(0.0, round(flt(sre.reserved_qty) - release_qty, prec))
		sre.db_set("reserved_qty", new_reserved)
		sre.update_reserved_stock_in_bin()
		sre.update_status()
		frappe.db.set_value(
			"Inventory Reservation Reference",
			{"stock_reservation_entry": reservation_entry_name},
			{"status": "Partially Released", "reserved_qty": new_reserved},
		)

	return True
