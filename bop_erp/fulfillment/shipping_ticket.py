# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple, Union
import frappe
from frappe import _
from frappe.utils import cint, flt

from bop_erp.constants import (
	IntegrationReadinessStatus,
	ShippingTicketStatus,
	TransactionOrigin,
)
from bop_erp.fulfillment.exceptions import (
	DuplicateShippingTicketError,
	FulfillmentError,
	OrderNotReadyForShippingError,
	PartialShippingBlockedError,
	PickTicketNotReadyForShippingError,
	PickTicketRequiredError,
	ShippingTicketError,
	WarehouseShippingMismatchError,
)
from bop_erp.fulfillment.pick_ticket import is_imported_sales_order
from bop_erp.orders.ingestion import (
	find_affected_channel_items_for_scopes,
	schedule_post_commit_publication,
)

logger = frappe.logger("bop_erp")

# Structured Observability Counters (Section 38)
SHIPPING_COUNTERS: Dict[str, int] = {
	"shipping_requests": 0,
	"shipping_tickets_created": 0,
	"shipping_tickets_reused": 0,
	"shipping_tickets_submitted": 0,
	"shipping_tickets_cancelled": 0,
	"shipping_requests_blocked": 0,
	"stock_issues": 0,
	"stock_reversals": 0,
	"concurrent_replay": 0,
	"publication_outbox_intents": 0,
	"failed": 0,
}


def reset_shipping_counters() -> None:
	"""Resets all structured shipping counters to zero for testing and telemetry."""
	for k in SHIPPING_COUNTERS:
		SHIPPING_COUNTERS[k] = 0


def get_shipping_counters() -> Dict[str, int]:
	"""Returns a snapshot copy of current shipping counters."""
	return dict(SHIPPING_COUNTERS)


def compute_shipping_ticket_idempotency_key(
	pick_ticket_name: str,
	so_name: str,
	lines: Optional[List[Dict[str, Any]]] = None,
) -> str:
	"""
	Derives a deterministic idempotency key for a Shipping Ticket request.
	Combines Pick List name, Sales Order name, and canonical sorted line allocations.
	"""
	normalized_lines = []
	if lines:
		for r in lines:
			normalized_lines.append((
				str(r.get("item_code") or ""),
				str(r.get("warehouse") or ""),
				float(flt(r.get("qty", 0.0))),
				str(r.get("batch_no") or ""),
				str(r.get("serial_no") or ""),
			))
		normalized_lines.sort()

	raw = f"{pick_ticket_name}|{so_name}|{json.dumps(normalized_lines, sort_keys=True)}"
	digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
	return f"ST-IDEMP-{digest}"


def assert_sales_order_ready_for_shipping(
	sales_order: Union[str, Any],
	pick_ticket: Optional[Union[str, Any]] = None,
) -> Tuple[Any, Optional[Any]]:
	"""
	Central shipping eligibility predicate (Section 4 & 5).

	Validates:
	1. Sales Order exists, is submitted (docstatus = 1), and not Cancelled or Closed.
	2. Company exists and matches.
	3. Sales Order is not already fully delivered (per_delivered < 100.0).
	4. If imported:
	   - integration_status == READY.
	   - No CHANGE_REVIEW_REQUIRED, FAILED_REVIEW, CANCELLATION_PENDING, or CANCELLED.
	   - Submitted Pick Ticket is MANDATORY (Policy 5).
	5. If Pick Ticket is provided:
	   - Must be submitted (docstatus = 1).
	   - Not cancelled (status != 'Cancelled', docstatus != 2).
	   - Company matches Sales Order.
	   - Sales channel matches Sales Order if both set.
	   - Contains items belonging to this Sales Order.
	6. Manual / native Sales Orders remain supported without requiring integration_status.
	"""
	if isinstance(sales_order, str):
		if not frappe.db.exists("Sales Order", sales_order):
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(_("Sales Order '{0}' does not exist.").format(sales_order))
		so_doc = frappe.get_doc("Sales Order", sales_order)
	else:
		so_doc = sales_order

	# 1. Submission status
	if so_doc.docstatus != 1:
		SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
		raise OrderNotReadyForShippingError(
			_("Sales Order '{0}' is not submitted.").format(so_doc.name)
		)
	if so_doc.status in ("Cancelled", "Closed"):
		SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
		raise OrderNotReadyForShippingError(
			_("Sales Order '{0}' status is '{1}'.").format(so_doc.name, so_doc.status)
		)

	# 2. Company validation
	if not so_doc.company or not frappe.db.exists("Company", so_doc.company):
		SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
		raise OrderNotReadyForShippingError(
			_("Company '{0}' on Sales Order '{1}' is invalid.").format(so_doc.company, so_doc.name)
		)

	# 3. Delivery status
	if flt(so_doc.per_delivered) >= 100.0:
		SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
		raise OrderNotReadyForShippingError(
			_("Sales Order '{0}' is already fully delivered.").format(so_doc.name)
		)

	# 4. Imported order operational guards
	is_imported = is_imported_sales_order(so_doc)
	if is_imported:
		status = so_doc.get("integration_status")

		if status == IntegrationReadinessStatus.CANCELLED:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(
				_("Sales Order '{0}' is CANCELLED.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.CANCELLATION_PENDING:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(
				_("Sales Order '{0}' has CANCELLATION_PENDING.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(
				_("Sales Order '{0}' is in CHANGE_REVIEW_REQUIRED status.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.FAILED_REVIEW:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(
				_("Sales Order '{0}' is in FAILED_REVIEW status.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.INGESTION_PENDING:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(
				_("Sales Order '{0}' is in INGESTION_PENDING status.").format(so_doc.name)
			)
		if status == IntegrationReadinessStatus.RESERVATION_PENDING:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(
				_("Sales Order '{0}' is in RESERVATION_PENDING status.").format(so_doc.name)
			)
		if status != IntegrationReadinessStatus.READY:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(
				_("Sales Order '{0}' is not READY for shipping (status: {1}).").format(so_doc.name, status)
			)

		# Mandatory Pick Ticket rule for automated imported orders (Section 5)
		if pick_ticket is None:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise PickTicketRequiredError(
				_("Imported Sales Order '{0}' requires a submitted Pick Ticket for shipping.").format(so_doc.name)
			)

	# 5. Pick Ticket validation
	pl_doc = None
	if pick_ticket is not None:
		if isinstance(pick_ticket, str):
			if not frappe.db.exists("Pick List", pick_ticket):
				SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
				raise PickTicketNotReadyForShippingError(
					_("Pick Ticket '{0}' does not exist.").format(pick_ticket)
				)
			pl_doc = frappe.get_doc("Pick List", pick_ticket)
		else:
			pl_doc = pick_ticket

		if pl_doc.docstatus != 1:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise PickTicketNotReadyForShippingError(
				_("Pick Ticket '{0}' is not submitted (docstatus: {1}).").format(pl_doc.name, pl_doc.docstatus)
			)

		if pl_doc.docstatus == 2 or pl_doc.status == "Cancelled":
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise PickTicketNotReadyForShippingError(
				_("Pick Ticket '{0}' is cancelled.").format(pl_doc.name)
			)

		if pl_doc.company != so_doc.company:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise WarehouseShippingMismatchError(
				_("Pick Ticket '{0}' company '{1}' does not match Sales Order company '{2}'.").format(
					pl_doc.name, pl_doc.company, so_doc.company
				)
			)

		if pl_doc.get("sales_channel") and so_doc.get("sales_channel") and pl_doc.sales_channel != so_doc.sales_channel:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(
				_("Pick Ticket '{0}' sales channel '{1}' does not match Sales Order channel '{2}'.").format(
					pl_doc.name, pl_doc.sales_channel, so_doc.sales_channel
				)
			)

		# Check locations belong to this Sales Order
		matching_locs = [
			loc for loc in (pl_doc.get("locations") or [])
			if (loc.get("sales_order") if isinstance(loc, dict) else getattr(loc, "sales_order", None)) == so_doc.name
		]
		if not matching_locs:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise OrderNotReadyForShippingError(
				_("Pick Ticket '{0}' has no locations matching Sales Order '{1}'.").format(
					pl_doc.name, so_doc.name
				)
			)

	return so_doc, pl_doc


@frappe.whitelist()
def create_shipping_ticket(
	pick_ticket: Union[str, Any],
	requested_lines: Optional[List[Dict[str, Any]]] = None,
	allow_partial: bool = False,
	idempotency_key: Optional[str] = None,
	submit: bool = False,
) -> Any:
	"""
	Provider-neutral Bop Shipping Ticket / Delivery Note creation service.

	Lifecycle:
	Pick Ticket submitted -> Delivery Note created -> Physical Stock decremented via SLE
	-> SO delivered_qty updated -> Outstanding demand settles -> ATP invariant strictly preserved
	-> Outbound transactional publication intent persisted.

	Parameters:
	- pick_ticket: Pick List document or name.
	- requested_lines: Optional specific line allocations to ship.
	- allow_partial: If False, blocks shipping if full remaining Pick Ticket scope cannot be shipped.
	- idempotency_key: Optional client-provided or computed idempotency key.
	- submit: If True, submits the Delivery Note using native lifecycle.
	"""
	SHIPPING_COUNTERS["shipping_requests"] += 1

	if isinstance(pick_ticket, str):
		pl_name = pick_ticket
		if not frappe.db.exists("Pick List", pl_name):
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise PickTicketNotReadyForShippingError(_("Pick Ticket '{0}' does not exist.").format(pl_name))
		pl_doc = frappe.get_doc("Pick List", pl_name)
	else:
		pl_doc = pick_ticket
		pl_name = pl_doc.name

	# 1. DB Row Lock for concurrency serialization
	frappe.db.sql("SELECT name FROM `tabPick List` WHERE name = %s FOR UPDATE", (pl_name,))

	# 2. Check for existing active Delivery Note (Idempotency / Reuse)
	existing_dn_rows = frappe.db.sql(
		"""
		SELECT DISTINCT dn.name, dn.docstatus, dn.status
		FROM `tabDelivery Note Item` dni
		JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.against_pick_list = %s
		  AND dn.docstatus IN (0, 1)
		  AND dn.status NOT IN ('Cancelled')
		ORDER BY dn.creation ASC
		""",
		(pl_name,),
		as_dict=True,
	)

	if existing_dn_rows:
		existing_dn_name = (
			existing_dn_rows[0].get("name")
			if isinstance(existing_dn_rows[0], dict)
			else getattr(existing_dn_rows[0], "name", str(existing_dn_rows[0]))
		)
		existing_docstatus = (
			existing_dn_rows[0].get("docstatus")
			if isinstance(existing_dn_rows[0], dict)
			else getattr(existing_dn_rows[0], "docstatus", 0)
		)

		if submit and existing_docstatus == 0:
			# Existing draft can be submitted
			so_names = list({
				(loc.get("sales_order") if isinstance(loc, dict) else getattr(loc, "sales_order", None))
				for loc in (pl_doc.get("locations") or [])
				if (loc.get("sales_order") if isinstance(loc, dict) else getattr(loc, "sales_order", None))
			})
			so_name = so_names[0] if so_names else None
			so_doc = frappe.get_doc("Sales Order", so_name) if so_name else None
			existing_dn = frappe.get_doc("Delivery Note", existing_dn_name)
			return _submit_delivery_note_with_atomicity(existing_dn, so_doc)

		SHIPPING_COUNTERS["shipping_tickets_reused"] += 1
		SHIPPING_COUNTERS["concurrent_replay"] += 1
		logger.info("Converged to existing Delivery Note '%s' for Pick Ticket '%s'.", existing_dn_name, pl_name)
		return frappe.get_doc("Delivery Note", existing_dn_name)

	# Resolve primary Sales Order and assert eligibility
	so_names = list({
		(loc.get("sales_order") if isinstance(loc, dict) else getattr(loc, "sales_order", None))
		for loc in (pl_doc.get("locations") or [])
		if (loc.get("sales_order") if isinstance(loc, dict) else getattr(loc, "sales_order", None))
	})
	so_name = so_names[0] if so_names else None
	if so_name:
		frappe.db.sql("SELECT name FROM `tabSales Order` WHERE name = %s FOR UPDATE", (so_name,))
		so_doc, _pl = assert_sales_order_ready_for_shipping(so_name, pl_doc)
	else:
		so_doc = None

	# 3. Full-Scope Policy (Section 10)
	total_pick_qty = sum(
		flt(loc.get("qty") if isinstance(loc, dict) else getattr(loc, "qty", 0.0))
		for loc in (pl_doc.get("locations") or [])
		if not (loc.get("product_bundle_item") if isinstance(loc, dict) else getattr(loc, "product_bundle_item", None))
	)

	if requested_lines is not None:
		total_requested = sum(flt(r.get("qty", 0.0)) for r in requested_lines)
		if submit and total_requested < total_pick_qty:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise PartialShippingBlockedError(
				_(
					"Partial-scope shipping blocked: automated submitted Shipping Tickets must cover "
					"the complete eligible Pick Ticket scope for '{0}' (requested: {1}, pick scope: {2}). "
					"Partial shipments may only be saved in Draft status."
				).format(pl_name, total_requested, total_pick_qty)
			)
		if not allow_partial and total_requested < total_pick_qty:
			SHIPPING_COUNTERS["shipping_requests_blocked"] += 1
			raise PartialShippingBlockedError(
				_(
					"Partial shipping not allowed: requested quantity {0} is less than eligible pick quantity {1}."
				).format(total_requested, total_pick_qty)
			)

	# 4. Native ERPNext Delivery Note creation
	from erpnext.stock.doctype.pick_list.pick_list import create_delivery_note

	dn_doc = create_delivery_note(pl_name)
	if not dn_doc or not dn_doc.get("items"):
		SHIPPING_COUNTERS["failed"] += 1
		raise ShippingTicketError(
			_("Failed to create Delivery Note from Pick Ticket '{0}'.").format(pl_name)
		)

	# 5. Apply partial line adjustments if draft partial is requested
	if requested_lines is not None and not submit:
		req_items_map = {}
		for r in requested_lines:
			key = (r.get("sales_order_item") or r.get("item_code"), r.get("warehouse"))
			req_items_map[key] = flt(r.get("qty", 0.0))

		filtered_items = []
		for it in dn_doc.items:
			key = (it.so_detail or it.item_code, it.warehouse)
			if key in req_items_map:
				it.qty = req_items_map[key]
				it.stock_qty = req_items_map[key]
				filtered_items.append(it)

		dn_doc.items = filtered_items

	# 6. Inherit Attribution (Section 8)
	sc = pl_doc.get("sales_channel") or (so_doc.get("sales_channel") if so_doc else None)
	to = pl_doc.get("transaction_origin") or (so_doc.get("transaction_origin") if so_doc else None)
	eo = pl_doc.get("external_order_id") or (so_doc.get("external_order_id") if so_doc else None)

	if sc:
		dn_doc.sales_channel = sc
	if to:
		dn_doc.transaction_origin = to
	if eo:
		dn_doc.external_order_id = eo

	dn_doc.save()
	SHIPPING_COUNTERS["shipping_tickets_created"] += 1
	logger.info("Created draft Delivery Note '%s' for Pick Ticket '%s'.", dn_doc.name, pl_name)

	# 7. Submit if requested
	if submit:
		return _submit_delivery_note_with_atomicity(dn_doc, so_doc)

	return dn_doc


def _submit_delivery_note_with_atomicity(dn_doc: Any, so_doc: Optional[Any] = None) -> Any:
	"""
	Atomically submits a native Delivery Note within a savepoint and persists
	outbound inventory publication intents in the same transaction.
	"""
	sp_submit = f"sp_ship_sub_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_submit)

	try:
		# Acquire exclusive row locks on Bins in deterministic alphabetical order
		items = dn_doc.get("items") or []
		unique_bins = sorted({
			(
				it.get("item_code") if isinstance(it, dict) else getattr(it, "item_code", None),
				it.get("warehouse") if isinstance(it, dict) else getattr(it, "warehouse", None),
			)
			for it in items
		})
		for b_item, b_wh in unique_bins:
			if b_item and b_wh:
				frappe.db.sql(
					"SELECT name FROM `tabBin` WHERE item_code = %s AND warehouse = %s FOR UPDATE",
					(b_item, b_wh),
				)

		# Normal validations enabled (Section 13)
		dn_doc.flags.ignore_validate = False
		dn_doc.flags.ignore_mandatory = False
		dn_doc.flags.ignore_permissions = False

		dn_doc.submit()
		SHIPPING_COUNTERS["shipping_tickets_submitted"] += 1
		SHIPPING_COUNTERS["stock_issues"] += len(items)
		logger.info("Submitted Delivery Note '%s'.", dn_doc.name)

		# Transactional Outbox Persistence (Section 18, 19, 20, 22)
		channel = dn_doc.get("sales_channel") or (so_doc.get("sales_channel") if so_doc else None)
		if channel:
			scopes = [
				(
					it.get("item_code") if isinstance(it, dict) else getattr(it, "item_code", None),
					it.get("warehouse") if isinstance(it, dict) else getattr(it, "warehouse", None),
				)
				for it in items
				if (it.get("item_code") if isinstance(it, dict) else getattr(it, "item_code", None))
			]
			channel_items_map = find_affected_channel_items_for_scopes(scopes, source_channel=channel)
			if channel_items_map:
				outbox_res = schedule_post_commit_publication(channel_items_map)
				SHIPPING_COUNTERS["publication_outbox_intents"] += outbox_res.get("outbox_persisted", 0)

	except Exception as sub_err:
		frappe.db.rollback(save_point=sp_submit)
		SHIPPING_COUNTERS["failed"] += 1
		logger.error("Failed to submit Delivery Note '%s': %s", dn_doc.name, str(sub_err))
		raise

	return dn_doc


@frappe.whitelist()
def cancel_shipping_ticket(shipping_ticket: Union[str, Any]) -> Any:
	"""
	Cancels a Shipping Ticket (Delivery Note) according to native ERPNext lifecycle.

	Guarantees:
	- Delivery Note is cancelled (docstatus = 2 or deleted if draft).
	- Stock Ledger Entries are cleanly reversed natively.
	- Bin physical quantity is restored natively.
	- Sales Order delivered quantity is restored natively.
	- Pick Ticket returns to Open status natively.
	- ATP remains strictly correct and invariant (no double counting).
	- Transactional outbound publication intents persisted for affected channels.
	- Zero financial side effects (no invoices/payments).
	- Zero PrestaShop order writebacks.
	"""
	if isinstance(shipping_ticket, str):
		if not frappe.db.exists("Delivery Note", shipping_ticket):
			raise FulfillmentError(_("Shipping Ticket '{0}' does not exist.").format(shipping_ticket))
		dn_doc = frappe.get_doc("Delivery Note", shipping_ticket)
	else:
		dn_doc = shipping_ticket

	if dn_doc.docstatus == 2 or dn_doc.status == "Cancelled":
		logger.info("Shipping Ticket '%s' is already cancelled.", dn_doc.name)
		return dn_doc

	if dn_doc.docstatus == 1:
		sp_cancel = f"sp_ship_cnc_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(sp_cancel)
		try:
			# Acquire exclusive row locks on Bins in deterministic alphabetical order
			items = dn_doc.get("items") or []
			unique_bins = sorted({
				(
					it.get("item_code") if isinstance(it, dict) else getattr(it, "item_code", None),
					it.get("warehouse") if isinstance(it, dict) else getattr(it, "warehouse", None),
				)
				for it in items
			})
			for b_item, b_wh in unique_bins:
				if b_item and b_wh:
					frappe.db.sql(
						"SELECT name FROM `tabBin` WHERE item_code = %s AND warehouse = %s FOR UPDATE",
						(b_item, b_wh),
					)

			dn_doc.cancel()
			SHIPPING_COUNTERS["shipping_tickets_cancelled"] += 1
			SHIPPING_COUNTERS["stock_reversals"] += len(items)
			logger.info("Cancelled submitted Delivery Note '%s'.", dn_doc.name)

			# Transactional outbox persistence for stock reversal (Section 25)
			channel = dn_doc.get("sales_channel")
			if channel:
				scopes = [
					(
						it.get("item_code") if isinstance(it, dict) else getattr(it, "item_code", None),
						it.get("warehouse") if isinstance(it, dict) else getattr(it, "warehouse", None),
					)
					for it in items
					if (it.get("item_code") if isinstance(it, dict) else getattr(it, "item_code", None))
				]
				channel_items_map = find_affected_channel_items_for_scopes(scopes, source_channel=channel)
				if channel_items_map:
					outbox_res = schedule_post_commit_publication(channel_items_map)
					SHIPPING_COUNTERS["publication_outbox_intents"] += outbox_res.get("outbox_persisted", 0)

		except Exception as cnc_err:
			frappe.db.rollback(save_point=sp_cancel)
			SHIPPING_COUNTERS["failed"] += 1
			logger.error("Failed to cancel Delivery Note '%s': %s", dn_doc.name, str(cnc_err))
			raise

	elif dn_doc.docstatus == 0:
		dn_name = dn_doc.name
		dn_doc.delete()
		SHIPPING_COUNTERS["shipping_tickets_cancelled"] += 1
		logger.info("Deleted draft Delivery Note '%s'.", dn_name)
		dn_doc.docstatus = 2
		dn_doc.status = "Cancelled"

	return dn_doc


@frappe.whitelist()
def get_shipping_ticket_status(shipping_ticket: Any) -> str:
	"""
	Returns the canonical user-facing Bop ShippingTicketStatus from native Delivery Note state.
	"""
	if isinstance(shipping_ticket, str):
		dn_doc = frappe.get_doc("Delivery Note", shipping_ticket)
	else:
		dn_doc = shipping_ticket

	if dn_doc.docstatus == 2 or dn_doc.status == "Cancelled":
		return ShippingTicketStatus.CANCELLED
	elif dn_doc.docstatus == 0:
		return ShippingTicketStatus.DRAFT
	elif dn_doc.docstatus == 1:
		return ShippingTicketStatus.SHIPPED

	return ShippingTicketStatus.DRAFT
