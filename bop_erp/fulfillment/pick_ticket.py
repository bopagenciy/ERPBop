# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from bop_erp.constants import (
	IntegrationReadinessStatus,
	PickTicketStatus,
	TransactionOrigin,
)
from bop_erp.fulfillment.exceptions import (
	DuplicatePickTicketError,
	FulfillmentError,
	InsufficientStockError,
	NonStockItemPickError,
	OrderNotReadyForPickingError,
	PartialPickBlockedError,
	WarehouseAllocationMismatchError,
)

logger = logging.getLogger("bop_erp.fulfillment")

# Observability Counters
PICK_COUNTERS: Dict[str, int] = {
	"pick_requests": 0,
	"pick_tickets_created": 0,
	"pick_tickets_reused": 0,
	"pick_tickets_submitted": 0,
	"pick_tickets_cancelled": 0,
	"pick_requests_blocked": 0,
	"reservation_mismatch": 0,
	"insufficient_stock": 0,
	"concurrent_replay": 0,
	"failed": 0,
}


def get_pick_counters() -> Dict[str, int]:
	"""Returns a copy of current fulfillment observability counters."""
	return dict(PICK_COUNTERS)


def reset_pick_counters() -> None:
	"""Resets fulfillment observability counters to zero (used in tests)."""
	for k in PICK_COUNTERS:
		PICK_COUNTERS[k] = 0


def is_imported_sales_order(so_doc: Document) -> bool:
	"""Determines if a Sales Order originates from an external channel integration."""
	return bool(
		so_doc.get("external_order_id")
		or so_doc.get("integration_provider")
		or (
			so_doc.get("sales_channel")
			and so_doc.get("transaction_origin") in (TransactionOrigin.WEB, TransactionOrigin.MARKETPLACE)
		)
	)


def assert_sales_order_ready_for_picking(sales_order: Union[str, Document]) -> Document:
	"""
	Centralized eligibility predicate for Pick Ticket creation.

	Validates:
	1. Sales Order exists and is submitted (docstatus == 1).
	2. Sales Order is not closed or cancelled.
	3. Sales Order is not already fully delivered (per_delivered < 100%).
	4. Company is valid.
	5. For imported orders:
	   - integration_status == READY
	   - no CANCELLED or CANCELLATION_PENDING
	   - no CHANGE_REVIEW_REQUIRED or FAILED_REVIEW
	   - no INGESTION_PENDING or RESERVATION_PENDING
	   - stock reservations are complete (is_order_ingestion_complete)
	   - sales_channel is active
	6. Sales Order contains at least one pickable stock item.

	Native/manual Sales Orders remain supported without requiring integration_status.
	"""
	if isinstance(sales_order, str):
		if not frappe.db.exists("Sales Order", sales_order):
			PICK_COUNTERS["pick_requests_blocked"] += 1
			raise OrderNotReadyForPickingError(_("Sales Order '{0}' does not exist.").format(sales_order))
		so_doc = frappe.get_doc("Sales Order", sales_order)
	else:
		so_doc = sales_order

	# 1. Lifecycle checks
	if so_doc.docstatus == 0:
		PICK_COUNTERS["pick_requests_blocked"] += 1
		raise OrderNotReadyForPickingError(
			_("Sales Order '{0}' is in Draft status and cannot be picked.").format(so_doc.name)
		)
	if so_doc.docstatus == 2:
		PICK_COUNTERS["pick_requests_blocked"] += 1
		raise OrderNotReadyForPickingError(
			_("Sales Order '{0}' is cancelled and cannot be picked.").format(so_doc.name)
		)
	if so_doc.docstatus != 1:
		PICK_COUNTERS["pick_requests_blocked"] += 1
		raise OrderNotReadyForPickingError(
			_("Sales Order '{0}' is not submitted.").format(so_doc.name)
		)
	if so_doc.status in ("Cancelled", "Closed"):
		PICK_COUNTERS["pick_requests_blocked"] += 1
		raise OrderNotReadyForPickingError(
			_("Sales Order '{0}' status is '{1}'.").format(so_doc.name, so_doc.status)
		)

	# 2. Company validation
	if not so_doc.company or not frappe.db.exists("Company", so_doc.company):
		PICK_COUNTERS["pick_requests_blocked"] += 1
		raise OrderNotReadyForPickingError(
			_("Company '{0}' on Sales Order '{1}' is invalid.").format(so_doc.company, so_doc.name)
		)

	# 3. Delivery status
	if flt(so_doc.per_delivered) >= 100.0:
		PICK_COUNTERS["pick_requests_blocked"] += 1
		raise OrderNotReadyForPickingError(
			_("Sales Order '{0}' is already fully delivered.").format(so_doc.name)
		)

	# 4. Imported order operational guards
	if is_imported_sales_order(so_doc):
		status = so_doc.get("integration_status")

		if status == IntegrationReadinessStatus.CANCELLED:
			PICK_COUNTERS["pick_requests_blocked"] += 1
			raise OrderNotReadyForPickingError(
				_("Sales Order '{0}' is CANCELLED.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.CANCELLATION_PENDING:
			PICK_COUNTERS["pick_requests_blocked"] += 1
			raise OrderNotReadyForPickingError(
				_("Sales Order '{0}' has CANCELLATION_PENDING.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED:
			PICK_COUNTERS["pick_requests_blocked"] += 1
			raise OrderNotReadyForPickingError(
				_("Sales Order '{0}' is in CHANGE_REVIEW_REQUIRED status.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.FAILED_REVIEW:
			PICK_COUNTERS["pick_requests_blocked"] += 1
			raise OrderNotReadyForPickingError(
				_("Sales Order '{0}' is in FAILED_REVIEW status.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.INGESTION_PENDING:
			PICK_COUNTERS["pick_requests_blocked"] += 1
			raise OrderNotReadyForPickingError(
				_("Sales Order '{0}' is in INGESTION_PENDING status.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.RESERVATION_PENDING:
			PICK_COUNTERS["pick_requests_blocked"] += 1
			raise OrderNotReadyForPickingError(
				_("Sales Order '{0}' is in RESERVATION_PENDING status.").format(so_doc.name)
			)
		if status != IntegrationReadinessStatus.READY:
			PICK_COUNTERS["pick_requests_blocked"] += 1
			raise OrderNotReadyForPickingError(
				_("Sales Order '{0}' is not READY for picking (status: {1}).").format(so_doc.name, status)
			)

		# Check complete stock reservations
		from bop_erp.orders.ingestion import is_order_ingestion_complete
		is_complete, missing = is_order_ingestion_complete(so_doc.name)
		if not is_complete:
			PICK_COUNTERS["pick_requests_blocked"] += 1
			raise OrderNotReadyForPickingError(
				_("Sales Order '{0}' has incomplete stock reservations: {1}.").format(
					so_doc.name, "; ".join(missing)
				)
			)

		# Sales channel active check
		if so_doc.sales_channel:
			is_active = frappe.db.get_value("Sales Channel", so_doc.sales_channel, "active")
			if not is_active:
				PICK_COUNTERS["pick_requests_blocked"] += 1
				raise OrderNotReadyForPickingError(
					_("Sales Channel '{0}' is inactive.").format(so_doc.sales_channel)
				)

	# 5. Check if there is at least one stock item or packed item
	has_stock_items = False
	if so_doc.get("packed_items"):
		has_stock_items = True
	else:
		for item in (so_doc.get("items") or []):
			item_code = item.get("item_code") if isinstance(item, dict) else getattr(item, "item_code", None)
			is_stock = frappe.db.get_value("Item", item_code, "is_stock_item")
			if is_stock:
				has_stock_items = True
				break

	if not has_stock_items:
		PICK_COUNTERS["pick_requests_blocked"] += 1
		raise NonStockItemPickError(
			_("Sales Order '{0}' contains no pickable stock items.").format(so_doc.name)
		)

	return so_doc


def compute_pick_ticket_idempotency_key(
	so_name: str,
	requested_lines: Optional[List[Dict[str, Any]]] = None,
	fulfillment_scope: Optional[str] = None,
	custom_key: Optional[str] = None,
) -> str:
	"""
	Computes a deterministic idempotency key for pick ticket creation.
	"""
	if custom_key:
		return custom_key

	raw_parts = [str(so_name)]
	if requested_lines:
		sorted_lines = sorted(
			requested_lines,
			key=lambda x: (
				str(x.get("sales_order_item") or ""),
				str(x.get("item_code") or ""),
				str(x.get("warehouse") or ""),
			),
		)
		for line in sorted_lines:
			raw_parts.append(
				f"{line.get('item_code')}:{line.get('sales_order_item')}:{line.get('warehouse')}:{flt(line.get('qty'))}"
			)
	if fulfillment_scope:
		raw_parts.append(str(fulfillment_scope))

	content = ":".join(raw_parts)
	hash_val = hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
	return f"PTK-{hash_val}"


@frappe.whitelist()
def get_remaining_to_pick(sales_order: Any) -> Dict[str, Any]:
	"""
	Computes the remaining pickable quantity for each line in a Sales Order.

	Returns a structured breakdown:
	{
	    "sales_order": str,
	    "items": [
	        {
	            "sales_order_item": str,
	            "item_code": str,
	            "item_name": str,
	            "warehouse": str,
	            "ordered_qty": float,
	            "delivered_qty": float,
	            "picked_qty": float,
	            "draft_picked_qty": float,
	            "remaining_to_pick": float,
	            "is_stock_item": bool,
	        }
	    ],
	    "total_ordered": float,
	    "total_picked": float,
	    "total_remaining": float,
	    "is_fully_picked": bool,
	}
	"""
	if isinstance(sales_order, str):
		so_doc = frappe.get_doc("Sales Order", sales_order)
	else:
		so_doc = sales_order

	# Query active Pick Lists (draft and submitted) for this SO
	pl_rows = frappe.db.sql(
		"""
		SELECT pli.sales_order_item, pli.product_bundle_item, pli.item_code, pli.warehouse,
		       SUM(CASE WHEN pl.docstatus = 1 THEN pli.qty ELSE 0 END) as submitted_picked,
		       SUM(CASE WHEN pl.docstatus = 0 THEN pli.qty ELSE 0 END) as draft_picked
		FROM `tabPick List Item` pli
		JOIN `tabPick List` pl ON pl.name = pli.parent
		WHERE pli.sales_order = %s
		  AND pl.docstatus IN (0, 1)
		  AND pl.status NOT IN ('Cancelled')
		GROUP BY pli.sales_order_item, pli.product_bundle_item, pli.item_code, pli.warehouse
		""",
		(so_doc.name,),
		as_dict=True,
	)

	picked_by_so_item: Dict[str, Dict[str, float]] = {}
	for r in pl_rows:
		key = r.sales_order_item or r.product_bundle_item or r.item_code
		picked_by_so_item.setdefault(key, {"submitted": 0.0, "draft": 0.0})
		picked_by_so_item[key]["submitted"] += flt(r.submitted_picked)
		picked_by_so_item[key]["draft"] += flt(r.draft_picked)

	items_breakdown = []
	total_ordered = 0.0
	total_picked = 0.0
	total_remaining = 0.0

	# Process packed items if Product Bundle, else standard items
	if getattr(so_doc, "packed_items", None):
		for p_item in so_doc.packed_items:
			is_stock = bool(frappe.db.get_value("Item", p_item.item_code, "is_stock_item"))
			ordered = flt(p_item.qty)
			delivered = 0.0  # packed items track delivery via parent item or directly
			p_data = picked_by_so_item.get(p_item.name, {"submitted": 0.0, "draft": 0.0})
			sub_picked = p_data["submitted"]
			draft_picked = p_data["draft"]
			# ERPNext updates picked_qty on packed items
			native_picked = flt(getattr(p_item, "picked_qty", 0.0))
			effective_picked = max(native_picked, sub_picked)
			rem = max(0.0, ordered - delivered - effective_picked - draft_picked)

			if is_stock:
				total_ordered += ordered
				total_picked += effective_picked
				total_remaining += rem

			items_breakdown.append({
				"sales_order_item": p_item.name,
				"parent_item": p_item.parent_item,
				"item_code": p_item.item_code,
				"item_name": p_item.item_name or p_item.item_code,
				"warehouse": p_item.warehouse,
				"ordered_qty": ordered,
				"delivered_qty": delivered,
				"picked_qty": effective_picked,
				"draft_picked_qty": draft_picked,
				"remaining_to_pick": rem,
				"is_stock_item": is_stock,
			})
	else:
		for s_item in (so_doc.get("items") or []):
			item_code = s_item.get("item_code") if isinstance(s_item, dict) else getattr(s_item, "item_code", None)
			item_name = s_item.get("item_name") if isinstance(s_item, dict) else getattr(s_item, "item_name", None)
			s_wh = s_item.get("warehouse") if isinstance(s_item, dict) else getattr(s_item, "warehouse", None)
			s_name = s_item.get("name") if isinstance(s_item, dict) else getattr(s_item, "name", None)
			s_qty = s_item.get("qty") if isinstance(s_item, dict) else getattr(s_item, "qty", 0.0)
			s_del = s_item.get("delivered_qty") if isinstance(s_item, dict) else getattr(s_item, "delivered_qty", 0.0)
			s_pick = s_item.get("picked_qty") if isinstance(s_item, dict) else getattr(s_item, "picked_qty", 0.0)

			is_stock = bool(frappe.db.get_value("Item", item_code, "is_stock_item"))
			ordered = flt(s_qty)
			delivered = flt(s_del)
			p_data = picked_by_so_item.get(s_name, {"submitted": 0.0, "draft": 0.0})
			sub_picked = p_data["submitted"]
			draft_picked = p_data["draft"]
			native_picked = flt(s_pick)
			effective_picked = max(native_picked, sub_picked)
			rem = max(0.0, ordered - delivered - effective_picked - draft_picked)

			if is_stock:
				total_ordered += ordered
				total_picked += effective_picked
				total_remaining += rem

			items_breakdown.append({
				"sales_order_item": s_name,
				"item_code": item_code,
				"item_name": item_name or item_code,
				"warehouse": s_wh,
				"ordered_qty": ordered,
				"delivered_qty": delivered,
				"picked_qty": effective_picked,
				"draft_picked_qty": draft_picked,
				"remaining_to_pick": rem,
				"is_stock_item": is_stock,
			})

	return {
		"sales_order": so_doc.name,
		"items": items_breakdown,
		"total_ordered": total_ordered,
		"total_picked": total_picked,
		"total_remaining": total_remaining,
		"is_fully_picked": (total_remaining <= 0.0 and total_ordered > 0.0),
	}


@frappe.whitelist()
def create_pick_ticket(
	sales_order: Any,
	requested_lines: Optional[List[Dict[str, Any]]] = None,
	allow_partial: bool = False,
	idempotency_key: Optional[str] = None,
	submit: bool = False,
) -> Any:
	"""
	Authoritative entry point to create an operational Pick Ticket (ERPNext Pick List).

	Parameters:
	- sales_order: Sales Order doc or name.
	- requested_lines: Optional specific line allocations [{'sales_order_item': ..., 'qty': ..., 'warehouse': ...}].
	- allow_partial: If False, blocks picking if full remaining order quantity cannot be picked.
	- idempotency_key: Optional client-provided or computed idempotency key.
	- submit: If True, submits the Pick List using native lifecycle.

	Guarantees:
	- DB row lock prevents concurrent duplicate demand creation.
	- If an active Pick Ticket already exists, returns existing instance (idempotent convergence).
	- Stock reservations (SREs) are strictly honored for warehouse allocation.
	- Non-stock items are excluded from warehouse picking rows.
	- ATP and demand invariants are strictly maintained (demand counted exactly once).
	"""
	PICK_COUNTERS["pick_requests"] += 1

	if isinstance(sales_order, str):
		so_name = sales_order
	else:
		so_name = sales_order.name

	# 1. DB Row Lock for concurrency serialization
	frappe.db.sql("SELECT name FROM `tabSales Order` WHERE name = %s FOR UPDATE", (so_name,))

	# 2. Check Pick Eligibility
	so_doc = assert_sales_order_ready_for_picking(so_name)

	# 3. Check for existing active Pick List (Idempotency / Reuse)
	existing_pl_rows = frappe.db.sql(
		"""
		SELECT DISTINCT pl.name, pl.docstatus, pl.status
		FROM `tabPick List Item` pli
		JOIN `tabPick List` pl ON pl.name = pli.parent
		WHERE pli.sales_order = %s
		  AND pl.docstatus IN (0, 1)
		  AND pl.status NOT IN ('Cancelled')
		ORDER BY pl.creation ASC
		""",
		(so_name,),
		as_dict=True,
	)
	if existing_pl_rows:
		existing_pl_name = (
			existing_pl_rows[0].get("name")
			if isinstance(existing_pl_rows[0], dict)
			else getattr(existing_pl_rows[0], "name", str(existing_pl_rows[0]))
		)
		existing_pl = frappe.get_doc("Pick List", existing_pl_name)
		PICK_COUNTERS["pick_tickets_reused"] += 1
		PICK_COUNTERS["concurrent_replay"] += 1
		logger.info(
			"Pick ticket for Sales Order '%s' already exists (%s). Returning existing instance.",
			so_name,
			existing_pl.name,
		)
		return existing_pl

	# 4. Check remaining pickable quantities
	remaining_info = get_remaining_to_pick(so_doc)
	if remaining_info["total_remaining"] <= 0.0:
		PICK_COUNTERS["pick_requests_blocked"] += 1
		raise OrderNotReadyForPickingError(
			_("Sales Order '{0}' has no remaining quantity to pick.").format(so_name)
		)

	# 5. Fetch active Stock Reservation Entries (SREs)
	sre_rows = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, reserved_qty, voucher_detail_no, has_batch_no, has_serial_no
		FROM `tabStock Reservation Entry`
		WHERE voucher_type = 'Sales Order'
		  AND voucher_no = %s
		  AND status NOT IN ('Closed', 'Delivered', 'Cancelled')
		  AND docstatus = 1
		""",
		(so_name,),
		as_dict=True,
	)
	sres_by_detail: Dict[str, List[Any]] = {}
	for sre in sre_rows:
		det_key = sre.get("voucher_detail_no") if isinstance(sre, dict) else getattr(sre, "voucher_detail_no", None)
		sres_by_detail.setdefault(det_key, []).append(sre)

	# 6. Build locations for native Pick List
	locations_to_create = []

	# Check if Product Bundle packed items exist
	has_packed = bool(getattr(so_doc, "packed_items", None))

	if requested_lines is not None:
		# Caller specified exact lines
		# Phase 1M Policy A: Automated submitted Pick Tickets must cover the complete remaining
		# fulfillment scope of the Sales Order. Partial-scope picking is supported ONLY in Draft (submit=False).
		if submit:
			total_requested = sum(flt(r.get("qty")) for r in requested_lines)
			if total_requested < remaining_info["total_remaining"]:
				PICK_COUNTERS["pick_requests_blocked"] += 1
				raise PartialPickBlockedError(
					_(
						"Partial-scope pick submission blocked: automated submitted Pick Tickets must cover "
						"the complete remaining fulfillment scope of Sales Order '{0}' (requested: {1}, remaining: {2}). "
						"Partial picks may only be saved in Draft status."
					).format(so_name, total_requested, remaining_info["total_remaining"])
				)

		if not allow_partial:
			# Verify all pickable items are present and cover total remaining
			req_so_items = {r.get("sales_order_item") for r in requested_lines if r.get("sales_order_item")}
			for item_info in remaining_info["items"]:
				if item_info["is_stock_item"] and item_info["remaining_to_pick"] > 0:
					if item_info["sales_order_item"] not in req_so_items:
						PICK_COUNTERS["pick_requests_blocked"] += 1
						raise PartialPickBlockedError(
							_("Partial picking blocked: item '{0}' is omitted from requested pick.").format(
								item_info["item_code"]
							)
						)

		for req in requested_lines:
			so_item_id = req.get("sales_order_item")
			item_code = req.get("item_code")
			req_wh = req.get("warehouse")
			pick_qty = flt(req.get("qty"))

			if pick_qty <= 0:
				continue

			# Validate non-stock
			is_stock = frappe.db.get_value("Item", item_code, "is_stock_item")
			if not is_stock:
				PICK_COUNTERS["pick_requests_blocked"] += 1
				raise NonStockItemPickError(
					_("Cannot pick non-stock item '{0}'.").format(item_code)
				)

			# Validate SRE warehouse consistency if SRE exists
			assigned_wh = req_wh
			assigned_batch = req.get("batch_no")
			if so_item_id and so_item_id in sres_by_detail:
				sre_list = sres_by_detail[so_item_id]
				sre_warehouses = [
					(s.get("warehouse") if isinstance(s, dict) else getattr(s, "warehouse", None))
					for s in sre_list
				]
				if req_wh and req_wh not in sre_warehouses:
					PICK_COUNTERS["reservation_mismatch"] += 1
					PICK_COUNTERS["pick_requests_blocked"] += 1
					raise WarehouseAllocationMismatchError(
						_(
							"Requested warehouse '{0}' for item '{1}' does not match reserved warehouse '{2}'."
						).format(req_wh, item_code, ", ".join(sre_warehouses))
					)
				first_sre = sre_list[0]
				assigned_wh = first_sre.get("warehouse") if isinstance(first_sre, dict) else getattr(first_sre, "warehouse", None)
				first_batch = first_sre.get("batch_no") if isinstance(first_sre, dict) else getattr(first_sre, "batch_no", None)
				if first_batch:
					assigned_batch = first_batch

			# Check physical stock availability in assigned warehouse
			actual_stock = flt(
				frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": assigned_wh}, "actual_qty") or 0.0
			)
			if actual_stock < pick_qty:
				PICK_COUNTERS["insufficient_stock"] += 1
				PICK_COUNTERS["pick_requests_blocked"] += 1
				raise InsufficientStockError(
					_(
						"Insufficient stock in warehouse '{0}' for item '{1}': requested {2}, available {3}."
					).format(assigned_wh, item_code, pick_qty, actual_stock)
				)

			# Fetch item metadata
			item_meta = frappe.db.get_value("Item", item_code, ["item_name", "stock_uom"], as_dict=True)

			loc_row = {
				"item_code": item_code,
				"item_name": item_meta.item_name if item_meta else item_code,
				"sales_order": so_name,
				"warehouse": assigned_wh,
				"qty": pick_qty,
				"stock_qty": pick_qty,
				"picked_qty": pick_qty,
				"conversion_factor": 1.0,
				"uom": item_meta.stock_uom if item_meta else "Nos",
				"stock_uom": item_meta.stock_uom if item_meta else "Nos",
			}
			if has_packed:
				loc_row["product_bundle_item"] = so_item_id
			else:
				loc_row["sales_order_item"] = so_item_id

			if assigned_batch:
				loc_row["batch_no"] = assigned_batch

			locations_to_create.append(loc_row)

	else:
		# Automatic allocation for full remaining pickable quantity
		for item_info in remaining_info["items"]:
			if not item_info["is_stock_item"]:
				# Non-stock items produce zero pick demand
				continue

			rem_qty = item_info["remaining_to_pick"]
			if rem_qty <= 0:
				continue

			so_item_id = item_info["sales_order_item"]
			item_code = item_info["item_code"]

			# Determine warehouse from SRE or item line
			assigned_wh = None
			assigned_batch = None

			if so_item_id in sres_by_detail:
				# Honor active SRE warehouse allocations
				for sre in sres_by_detail[so_item_id]:
					sre_qty = flt(sre.get("reserved_qty") if isinstance(sre, dict) else getattr(sre, "reserved_qty", 0.0))
					alloc_qty = min(rem_qty, sre_qty)
					if alloc_qty <= 0:
						continue

					sre_wh = sre.get("warehouse") if isinstance(sre, dict) else getattr(sre, "warehouse", None)
					sre_batch = sre.get("batch_no") if isinstance(sre, dict) else getattr(sre, "batch_no", None)

					# Check stock in SRE warehouse
					actual_stock = flt(
						frappe.db.get_value(
							"Bin", {"item_code": item_code, "warehouse": sre_wh}, "actual_qty"
						) or 0.0
					)
					if actual_stock < alloc_qty:
						PICK_COUNTERS["insufficient_stock"] += 1
						PICK_COUNTERS["pick_requests_blocked"] += 1
						raise InsufficientStockError(
							_(
								"Insufficient stock in warehouse '{0}' for item '{1}': required {2}, available {3}."
							).format(sre_wh, item_code, alloc_qty, actual_stock)
						)

					item_meta = frappe.db.get_value("Item", item_code, ["item_name", "stock_uom"], as_dict=True)
					loc_row = {
						"item_code": item_code,
						"item_name": item_meta.item_name if item_meta else item_code,
						"sales_order": so_name,
						"warehouse": sre_wh,
						"qty": alloc_qty,
						"stock_qty": alloc_qty,
						"picked_qty": alloc_qty,
						"conversion_factor": 1.0,
						"uom": item_meta.stock_uom if item_meta else "Nos",
						"stock_uom": item_meta.stock_uom if item_meta else "Nos",
					}
					if has_packed:
						loc_row["product_bundle_item"] = so_item_id
					else:
						loc_row["sales_order_item"] = so_item_id

					if sre_batch:
						loc_row["batch_no"] = sre_batch

					locations_to_create.append(loc_row)
					rem_qty -= alloc_qty
					if rem_qty <= 0:
						break
			else:
				# Native / manual order without SRE
				assigned_wh = item_info["warehouse"]
				if not assigned_wh:
					PICK_COUNTERS["pick_requests_blocked"] += 1
					raise OrderNotReadyForPickingError(
						_("No warehouse specified for item '{0}' in Sales Order '{1}'.").format(
							item_code, so_name
						)
					)

				actual_stock = flt(
					frappe.db.get_value(
						"Bin", {"item_code": item_code, "warehouse": assigned_wh}, "actual_qty"
					) or 0.0
				)
				if actual_stock < rem_qty:
					PICK_COUNTERS["insufficient_stock"] += 1
					PICK_COUNTERS["pick_requests_blocked"] += 1
					raise InsufficientStockError(
						_(
							"Insufficient stock in warehouse '{0}' for item '{1}': required {2}, available {3}."
						).format(assigned_wh, item_code, rem_qty, actual_stock)
					)

				item_meta = frappe.db.get_value("Item", item_code, ["item_name", "stock_uom"], as_dict=True)
				loc_row = {
					"item_code": item_code,
					"item_name": item_meta.item_name if item_meta else item_code,
					"sales_order": so_name,
					"warehouse": assigned_wh,
					"qty": rem_qty,
					"stock_qty": rem_qty,
					"picked_qty": rem_qty,
					"conversion_factor": 1.0,
					"uom": item_meta.stock_uom if item_meta else "Nos",
					"stock_uom": item_meta.stock_uom if item_meta else "Nos",
				}
				if has_packed:
					loc_row["product_bundle_item"] = so_item_id
				else:
					loc_row["sales_order_item"] = so_item_id

				locations_to_create.append(loc_row)

	if not locations_to_create:
		PICK_COUNTERS["pick_requests_blocked"] += 1
		raise OrderNotReadyForPickingError(
			_("No pickable locations could be determined for Sales Order '{0}'.").format(so_name)
		)

	# 7. Create native Pick List document
	pl_doc = frappe.new_doc("Pick List")
	pl_doc.company = so_doc.company
	pl_doc.purpose = "Delivery"
	pl_doc.customer = so_doc.customer
	pl_doc.pick_manually = 1

	# Inherit attribution
	if so_doc.sales_channel:
		pl_doc.sales_channel = so_doc.sales_channel
	if so_doc.transaction_origin:
		pl_doc.transaction_origin = so_doc.transaction_origin
	if so_doc.external_order_id:
		pl_doc.external_order_id = so_doc.external_order_id

	for loc in locations_to_create:
		pl_doc.append("locations", loc)

	pl_doc.insert()
	PICK_COUNTERS["pick_tickets_created"] += 1
	logger.info("Created Pick Ticket '%s' for Sales Order '%s'.", pl_doc.name, so_name)

	# 8. Submit if requested
	if submit:
		# Native ERPNext Pick List submission with transactional SRE transition:
		# ERPNext requires stock reservations on the Sales Order to be released before Pick List submit.
		# To preserve all native validations without monkey-patching or broad bypasses:
		# 1. Acquire savepoint 'sp_pick_submit'.
		# 2. Acquire deterministic Bin row locks.
		# 3. Release active SREs via native so_doc.cancel_stock_reservation_entries(notify=False).
		# 4. Submit Pick List with full native validations.
		# 5. Rollback atomically if submission fails.
		sp_submit = f"sp_pick_sub_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(sp_submit)

		try:
			# Acquire exclusive row locks on Bins in deterministic alphabetical order
			locs = pl_doc.get("locations") or []
			unique_bins = sorted({
				(
					loc.get("item_code") if isinstance(loc, dict) else getattr(loc, "item_code", None),
					loc.get("warehouse") if isinstance(loc, dict) else getattr(loc, "warehouse", None),
				)
				for loc in locs
			})
			for b_item, b_wh in unique_bins:
				if b_item and b_wh:
					frappe.db.sql(
						"SELECT name FROM `tabBin` WHERE item_code = %s AND warehouse = %s FOR UPDATE",
						(b_item, b_wh),
					)

			if sre_rows and hasattr(so_doc, "cancel_stock_reservation_entries"):
				so_doc.cancel_stock_reservation_entries(notify=False)

			pl_doc.flags.ignore_validate = False
			pl_doc.flags.ignore_mandatory = False
			pl_doc.flags.ignore_permissions = False
			pl_doc.submit()
			PICK_COUNTERS["pick_tickets_submitted"] += 1
			logger.info("Submitted Pick Ticket '%s' for Sales Order '%s'.", pl_doc.name, so_name)
		except Exception as sub_err:
			frappe.db.rollback(save_point=sp_submit)
			PICK_COUNTERS["failed"] += 1
			logger.error("Failed to submit Pick Ticket '%s': %s", pl_doc.name, str(sub_err))
			raise

	return pl_doc


@frappe.whitelist()
def cancel_pick_ticket(pick_ticket: Any) -> Any:
	"""
	Cancels a Pick Ticket according to native ERPNext lifecycle.

	Guarantees:
	- Pick Ticket is cancelled (docstatus = 2 or deleted if draft).
	- Sales Order remains active and READY.
	- Stock reservations (SREs) and Bin demand remain valid.
	- ATP remains exactly unchanged.
	- Zero PrestaShop order writes.
	"""
	if isinstance(pick_ticket, str):
		if not frappe.db.exists("Pick List", pick_ticket):
			raise FulfillmentError(_("Pick Ticket '{0}' does not exist.").format(pick_ticket))
		pl_doc = frappe.get_doc("Pick List", pick_ticket)
	else:
		pl_doc = pick_ticket

	if pl_doc.docstatus == 2 or pl_doc.status == "Cancelled":
		logger.info("Pick Ticket '%s' is already cancelled.", pl_doc.name)
		return pl_doc

	if pl_doc.docstatus == 1:
		sp_cancel = f"sp_pick_cnc_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(sp_cancel)
		try:
			# Acquire exclusive row locks on Bins in deterministic alphabetical order
			locs = pl_doc.get("locations") or []
			unique_bins = sorted({
				(
					loc.get("item_code") if isinstance(loc, dict) else getattr(loc, "item_code", None),
					loc.get("warehouse") if isinstance(loc, dict) else getattr(loc, "warehouse", None),
				)
				for loc in locs
			})
			for b_item, b_wh in unique_bins:
				if b_item and b_wh:
					frappe.db.sql(
						"SELECT name FROM `tabBin` WHERE item_code = %s AND warehouse = %s FOR UPDATE",
						(b_item, b_wh),
					)

			pl_doc.cancel()
			PICK_COUNTERS["pick_tickets_cancelled"] += 1
			logger.info("Cancelled submitted Pick Ticket '%s'.", pl_doc.name)

			# Restore Stock Reservation Entries for imported or reserved Sales Orders
			so_cache: Dict[str, Any] = {}
			for loc in locs:
				so_name = loc.get("sales_order") if isinstance(loc, dict) else getattr(loc, "sales_order", None)
				if not so_name:
					continue

				if so_name not in so_cache:
					so_cache[so_name] = frappe.get_doc("Sales Order", so_name)
				so_inst = so_cache[so_name]

				if is_imported_sales_order(so_inst) or so_inst.get("reserve_stock"):
					so_item_id = (
						(loc.get("sales_order_item") or loc.get("product_bundle_item"))
						if isinstance(loc, dict)
						else (getattr(loc, "sales_order_item", None) or getattr(loc, "product_bundle_item", None))
					)
					loc_qty = flt(loc.get("qty") if isinstance(loc, dict) else getattr(loc, "qty", 0.0))
					if loc_qty <= 0:
						continue

					loc_item = loc.get("item_code") if isinstance(loc, dict) else getattr(loc, "item_code", None)
					loc_wh = loc.get("warehouse") if isinstance(loc, dict) else getattr(loc, "warehouse", None)
					loc_uom = loc.get("stock_uom") if isinstance(loc, dict) else getattr(loc, "stock_uom", None)
					loc_batch = loc.get("batch_no") if isinstance(loc, dict) else getattr(loc, "batch_no", None)
					loc_name = loc.get("name") if isinstance(loc, dict) else getattr(loc, "name", "loc")

					from bop_erp.inventory.reservations import reserve_stock

					reserve_stock(
						item_code=loc_item,
						warehouse=loc_wh,
						requested_qty=loc_qty,
						voucher_type="Sales Order",
						voucher_no=so_name,
						voucher_detail_no=so_item_id,
						allow_partial=False,
						idempotency_key=f"IRR-{so_name}-{loc_name}-restore",
						source_doctype="Sales Order",
						source_document=so_name,
						source_document_item=so_item_id,
					)

		except Exception as cnc_err:
			frappe.db.rollback(save_point=sp_cancel)
			PICK_COUNTERS["failed"] += 1
			logger.error("Failed to cancel Pick Ticket '%s': %s", pl_doc.name, str(cnc_err))
			raise

	elif pl_doc.docstatus == 0:
		# For draft, delete through native frappe lifecycle
		pl_name = pl_doc.name
		pl_doc.delete()
		PICK_COUNTERS["pick_tickets_cancelled"] += 1
		logger.info("Deleted draft Pick Ticket '%s'.", pl_name)
		# Return in cancelled state representation
		pl_doc.docstatus = 2
		pl_doc.status = "Cancelled"

	return pl_doc


@frappe.whitelist()
def get_pick_ticket_status(pick_ticket: Any) -> str:
	"""
	Returns the canonical user-facing Bop PickTicketStatus from native Pick List state.
	"""
	if isinstance(pick_ticket, str):
		pl_doc = frappe.get_doc("Pick List", pick_ticket)
	else:
		pl_doc = pick_ticket

	if pl_doc.docstatus == 2 or pl_doc.status == "Cancelled":
		return PickTicketStatus.CANCELLED
	elif pl_doc.docstatus == 0:
		return PickTicketStatus.DRAFT
	elif pl_doc.docstatus == 1:
		if pl_doc.status == "Completed":
			return PickTicketStatus.PICKED
		return PickTicketStatus.PICKING

	return PickTicketStatus.DRAFT
