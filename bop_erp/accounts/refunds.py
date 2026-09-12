# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple, Union

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, get_datetime, now_datetime, nowdate

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
	TransactionOrigin,
)
from bop_erp.accounts.exceptions import (
	CompanyMismatchError,
	CurrencyMismatchError,
	CustomerMismatchError,
	DuplicateRefundError,
	OverRefundBlockedError,
	RefundDriftError,
	RefundEligibilityError,
	RefundError,
	RefundReplayCancelledError,
	RefundStatusIneligibleError,
	SalesInvoiceError,
)
from erpnext.controllers.sales_and_purchase_return import make_return_doc
from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import reconcile_dr_cr_note

try:
	from bop_erp.inventory.publication import schedule_channel_inventory_publication
except ImportError:
	schedule_channel_inventory_publication = None

logger = frappe.logger("bop_erp")


class ExternalRefundStatus:
	SETTLED = "SETTLED"
	COMPLETED = "COMPLETED"
	PENDING = "PENDING"
	AUTHORIZED = "AUTHORIZED"
	FAILED = "FAILED"
	VOIDED = "VOIDED"
	UNKNOWN = "UNKNOWN"
	CHARGEBACK = "CHARGEBACK"

	ELIGIBLE = (SETTLED, COMPLETED)
	INELIGIBLE = (PENDING, AUTHORIZED, FAILED, VOIDED, UNKNOWN, CHARGEBACK)


# Structured Observability Counters (Section 27 & Telemetry)
REFUND_COUNTERS: Dict[str, int] = {
	"refund_requests": 0,
	"refunds_completed": 0,
	"refund_reused": 0,
	"refund_blocked": 0,
	"refund_drift_blocked": 0,
	"partial_refunds": 0,
	"full_refunds": 0,
	"stock_returns": 0,
	"financial_only_credits": 0,
	"refund_failures": 0,
}


def reset_refund_counters() -> None:
	"""Resets all structured refund counters to zero."""
	for k in REFUND_COUNTERS:
		REFUND_COUNTERS[k] = 0


def get_refund_counters() -> Dict[str, int]:
	"""Returns a snapshot copy of current refund counters."""
	return dict(REFUND_COUNTERS)


def _round_curr(val: Any, precision: int = 2) -> Decimal:
	"""Deterministically quantizes amounts to given decimal precision."""
	return Decimal(str(flt(val, precision))).quantize(
		Decimal(f"1e-{precision}"), rounding=ROUND_HALF_UP
	)


@dataclass
class ExternalRefundItem:
	"""
	Provider-neutral representation of a line item targeted for refund or return.
	"""
	external_order_line_id: Optional[str] = None
	item_code: Optional[str] = None
	qty: Optional[float] = None
	rate: Optional[float] = None
	sales_invoice_item: Optional[str] = None
	warehouse: Optional[str] = None
	physical_return_evidence: bool = False
	refund_amount: Optional[float] = None
	returned_qty: Optional[float] = None



@dataclass
class ExternalRefundRecord:
	"""
	Provider-neutral domain model representing an external refund / credit event.
	Decouples e-commerce / marketplace / gateway payloads from ERP models.
	"""
	provider: str
	sales_channel: str
	external_refund_id: str
	external_order_id: Optional[str] = None
	external_payment_id: Optional[str] = None
	sales_invoice: Optional[str] = None
	amount: Optional[float] = None
	shipping_refund_amount: Optional[float] = None
	currency: str = "USD"
	status: str = ExternalRefundStatus.SETTLED
	refund_date: Optional[str] = None
	reason: Optional[str] = None
	transaction_origin: str = TransactionOrigin.WEB
	company: Optional[str] = None
	customer: Optional[str] = None
	items: Optional[List[ExternalRefundItem]] = None
	return_stock: bool = False
	metadata: Optional[Dict[str, Any]] = None


def compute_external_refund_idempotency_key(
	sales_channel: Optional[str] = None,
	provider: Optional[str] = None,
	external_refund_id: Optional[str] = None,
	external_order_id: Optional[str] = None,
	amount: Optional[float] = None,
	currency: Optional[str] = None,
	items: Optional[List[ExternalRefundItem]] = None,
	refund_record: Optional[ExternalRefundRecord] = None,
	external_payment_id: Optional[str] = None,
	shipping_refund_amount: Optional[float] = None,
	status: Optional[str] = None,
	return_stock: Optional[bool] = None,
	metadata: Optional[Dict[str, Any]] = None,
	**kwargs,
) -> str:
	"""
	Computes a canonical deterministic SHA-256 idempotency key for an external refund event.
	Covers all material refund semantics:
	- provider
	- sales_channel
	- external_refund_id
	- external_order_id
	- external_payment_id (if supplied)
	- currency
	- refund_amount
	- shipping_refund_amount
	- status
	- return_stock
	- sorted line items (external_order_line_id, item_code, refund_amount, returned_qty, physical_return_evidence, warehouse)
	- metadata (deterministic sorting ensures key ordering variations do not drift)
	"""
	if refund_record is not None:
		provider = refund_record.provider
		sales_channel = refund_record.sales_channel
		external_refund_id = refund_record.external_refund_id
		external_order_id = refund_record.external_order_id
		external_payment_id = refund_record.external_payment_id
		currency = refund_record.currency
		amount = refund_record.amount
		shipping_refund_amount = refund_record.shipping_refund_amount
		status = refund_record.status
		return_stock = refund_record.return_stock
		items = refund_record.items
		metadata = refund_record.metadata

	prov = str(provider).strip().upper() if provider else ""
	channel = str(sales_channel).strip() if sales_channel else ""
	ref_id = str(external_refund_id).strip() if external_refund_id else ""
	ord_id = str(external_order_id).strip() if external_order_id else None
	pmt_id = (
		str(external_payment_id).strip()
		if external_payment_id
		else (str(kwargs["external_payment_id"]).strip() if kwargs.get("external_payment_id") else None)
	)
	curr = str(currency).strip().upper() if currency else "USD"
	st = (
		str(status).strip().upper()
		if status
		else (str(kwargs["status"]).strip().upper() if kwargs.get("status") else ExternalRefundStatus.SETTLED)
	)
	ship_amt = (
		shipping_refund_amount
		if shipping_refund_amount is not None
		else kwargs.get("shipping_refund_amount")
	)
	ret_stock = (
		bool(return_stock)
		if return_stock is not None
		else bool(kwargs.get("return_stock", False))
	)
	meta = metadata if metadata is not None else kwargs.get("metadata")

	payload: Dict[str, Any] = {
		"provider": prov,
		"sales_channel": channel,
		"entity_type": ExternalEntityType.REFUND,
		"external_refund_id": ref_id,
		"external_order_id": ord_id,
		"external_payment_id": pmt_id,
		"currency": curr,
		"refund_amount": float(_round_curr(amount)) if amount is not None else None,
		"shipping_refund_amount": float(_round_curr(ship_amt)) if ship_amt is not None else None,
		"status": st,
		"return_stock": ret_stock,
	}

	normalized_items = []
	if items:
		for it in items:
			item_code = str(it.item_code).strip() if getattr(it, "item_code", None) else None
			ext_line_id = str(it.external_order_line_id).strip() if getattr(it, "external_order_line_id", None) else None
			warehouse = str(it.warehouse).strip() if getattr(it, "warehouse", None) else None
			phys_ev = bool(getattr(it, "physical_return_evidence", False))

			ref_amt = getattr(it, "refund_amount", None)
			if ref_amt is not None:
				calc_ref_amt = float(_round_curr(ref_amt))
			elif getattr(it, "rate", None) is not None and getattr(it, "qty", None) is not None:
				calc_ref_amt = float(_round_curr(flt(it.rate) * flt(it.qty)))
			else:
				calc_ref_amt = None

			ret_q = getattr(it, "returned_qty", None)
			if ret_q is not None:
				calc_ret_q = float(ret_q)
			elif getattr(it, "qty", None) is not None:
				calc_ret_q = float(it.qty)
			else:
				calc_ret_q = None

			normalized_items.append({
				"external_order_line_id": ext_line_id,
				"item_code": item_code,
				"refund_amount": calc_ref_amt,
				"returned_qty": calc_ret_q,
				"physical_return_evidence": phys_ev,
				"warehouse": warehouse,
			})

		# Deterministic sorting
		normalized_items.sort(key=lambda x: (
			x["item_code"] or "",
			x["external_order_line_id"] or "",
			x["warehouse"] or "",
			x["refund_amount"] or 0.0,
			x["returned_qty"] or 0.0,
		))

	payload["items"] = normalized_items

	if meta and isinstance(meta, dict):
		payload["metadata"] = meta

	canonical_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
	return hashlib.sha256(canonical_bytes).hexdigest()


def get_delivery_notes_for_invoice(si_doc) -> List[str]:
	"""
	Returns all submitted original (non-return) Delivery Notes associated with a Sales Invoice.
	Checks both direct item row delivery_note links and Sales Order fulfillment links.
	"""
	dn_names = set()
	for row in si_doc.items:
		if getattr(row, "delivery_note", None) and isinstance(row.delivery_note, str):
			dn_names.add(row.delivery_note)

	so_name = getattr(si_doc, "sales_order", None)
	if not so_name or not isinstance(so_name, str):
		so_name = None
		for row in si_doc.items:
			if getattr(row, "sales_order", None) and isinstance(row.sales_order, str):
				so_name = row.sales_order
				break

	if so_name:
		dns = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabDelivery Note Item`
			WHERE against_sales_order = %s AND docstatus = 1
			""",
			(so_name,),
			pluck="parent",
		)
		for dn in dns:
			dn_names.add(dn)

	valid_dns = []
	for dn in dn_names:
		if frappe.db.exists("Delivery Note", dn):
			dn_info = frappe.db.get_value(
				"Delivery Note", dn, ["docstatus", "is_return"], as_dict=True
			)
			if dn_info and dn_info.docstatus == 1 and not dn_info.is_return:
				valid_dns.append(dn)
	return valid_dns



def get_physical_return_scope(
	si_doc, item_code: str, dn_names: Optional[List[str]] = None
) -> Tuple[float, float, float]:
	"""
	Returns (delivered_qty, physically_returned_qty, remaining_physical_qty) for a given item_code.
	Distinguishes between Path 1 (si_doc.update_stock == 1) and Path 2 (Delivery Note fulfillment).
	"""
	delivered_qty = 0.0
	physically_returned_qty = 0.0

	if cint(si_doc.update_stock) == 1:
		# Path 1: Sales Invoice directly moved stock
		for row in si_doc.items:
			if row.item_code == item_code:
				delivered_qty += abs(flt(row.qty))

		# Query submitted Return Sales Invoices with update_stock = 1
		ret_si_rows = frappe.db.sql(
			"""
			SELECT SUM(ABS(sii.qty))
			FROM `tabSales Invoice Item` sii
			INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
			WHERE si.return_against = %s
			  AND si.docstatus = 1
			  AND si.is_return = 1
			  AND si.update_stock = 1
			  AND sii.item_code = %s
			""",
			(si_doc.name, item_code),
		)
		if ret_si_rows and ret_si_rows[0][0]:
			physically_returned_qty = flt(ret_si_rows[0][0])
	else:
		# Path 2: Delivery Note fulfillment
		if dn_names is None:
			dn_names = get_delivery_notes_for_invoice(si_doc)

		if dn_names:
			del_rows = frappe.db.sql(
				"""
				SELECT SUM(qty)
				FROM `tabDelivery Note Item`
				WHERE parent IN %(dns)s
				  AND docstatus = 1
				  AND item_code = %(item_code)s
				""",
				{"dns": tuple(dn_names), "item_code": item_code},
			)

			if del_rows and del_rows[0][0]:
				delivered_qty = flt(del_rows[0][0])

			ret_dn_rows = frappe.db.sql(
				"""
				SELECT SUM(ABS(dni.qty))
				FROM `tabDelivery Note Item` dni
				INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
				WHERE dn.return_against IN %(dns)s
				  AND dn.docstatus = 1
				  AND dn.is_return = 1
				  AND dni.item_code = %(item_code)s
				""",
				{"dns": tuple(dn_names), "item_code": item_code},
			)
			if ret_dn_rows and ret_dn_rows[0][0]:
				physically_returned_qty = flt(ret_dn_rows[0][0])

	remaining_physical_qty = max(0.0, delivered_qty - physically_returned_qty)
	return (delivered_qty, physically_returned_qty, remaining_physical_qty)



def process_external_refund(
	refund_record: ExternalRefundRecord,
	submit: bool = True,
) -> frappe._dict:
	"""
	Provider-neutral pipeline for processing external refunds into native ERPNext Credit Notes.
	Enforces:
	1. Canonical refund identity & replay-safe idempotency.
	2. Status eligibility (SETTLED / COMPLETED only).
	3. Original Sales Invoice resolution & row locking.
	4. Company, Customer, Channel, and Currency invariance.
	5. Cumulative monetary over-refund protection.
	6. Line-level item over-return protection.
	7. Strict financial vs physical separation:
	   - If return_stock is False: Credit Note update_stock=0, zero stock movement, zero publication.
	   - If return_stock is True: update_stock=1 or Return Delivery Note, transactional publication intent.
	8. Native ERPNext AR / customer-credit reconciliation (original Payment Entry untouched).
	9. Cancellation safety: cancelled Credit Note retains external identity; replay is rejected.
	"""
	REFUND_COUNTERS["refund_requests"] += 1

	# 1. Basic Parameter Validation
	if not refund_record.sales_channel or not str(refund_record.sales_channel).strip():
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundEligibilityError(_("Sales channel is mandatory for external refund processing."))

	if not refund_record.external_refund_id or not str(refund_record.external_refund_id).strip():
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundEligibilityError(_("External refund identity is mandatory."))

	channel = str(refund_record.sales_channel).strip()
	clean_prov = str(refund_record.provider).strip().upper()
	ref_id = str(refund_record.external_refund_id).strip()

	if not frappe.db.exists("Sales Channel", channel):
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundEligibilityError(_("Sales Channel '{0}' does not exist.").format(channel))

	if not frappe.db.get_value("Sales Channel", channel, "active"):
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundEligibilityError(_("Sales Channel '{0}' is inactive.").format(channel))

	# 2. Status Eligibility Gate (Section 16)
	st = str(refund_record.status or ExternalRefundStatus.SETTLED).strip().upper()
	if st not in ExternalRefundStatus.ELIGIBLE:
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundStatusIneligibleError(
			_("External refund status '{0}' is ineligible for accounting mutation.").format(st)
		)

	# 3. Canonical Identity & Replay Check (Section 5 & 17)
	ref_idempotency_key = compute_external_refund_idempotency_key(
		refund_record=refund_record
	)


	existing_map = frappe.db.get_value(
		"External ID Mapping",
		{
			"sales_channel": channel,
			"provider": clean_prov,
			"external_entity_type": ExternalEntityType.REFUND,
			"external_id": ref_id,
		},
		["name", "erp_doctype", "erp_document", "sync_hash", "active"],
		as_dict=True,
	)

	if existing_map and isinstance(existing_map, dict):
		erp_dt = existing_map.get("erp_doctype")
		erp_dn = existing_map.get("erp_document")
		sync_hash = existing_map.get("sync_hash")
		if erp_dt and erp_dn:
			existing_doc = frappe.get_doc(erp_dt, erp_dn)
			# Cancellation check (Section 20 & 26): cancelled Credit Note identity is terminal; replay is blocked
			if existing_doc.docstatus == 2:
				REFUND_COUNTERS["refund_blocked"] += 1
				REFUND_COUNTERS["refund_failures"] += 1
				raise RefundReplayCancelledError(
					_(
						"External refund identity '{0}' was previously linked to Credit Note '{1}' which was CANCELLED. "
						"Replaying cancelled refund is blocked and requires manual review."
					).format(ref_id, existing_doc.name)
				)

			# Payload Drift Check (Section 17)
			if sync_hash != ref_idempotency_key:
				REFUND_COUNTERS["refund_drift_blocked"] += 1
				REFUND_COUNTERS["refund_failures"] += 1
				raise RefundDriftError(
					_(
						"Payload drift detected for external refund '{0}'. Stored hash '{1}' differs from incoming hash '{2}'."
					).format(ref_id, sync_hash, ref_idempotency_key)
				)

			REFUND_COUNTERS["refund_reused"] += 1
			logger.info(
				"Idempotent reuse of existing Credit Note '%s' for external refund '%s'.",
				existing_doc.name,
				ref_id,
			)
			return existing_doc

	# 4. Resolve Target Sales Invoice (Section 6)
	si_name = refund_record.sales_invoice
	if not si_name and refund_record.external_order_id:
		# Resolve mapped Sales Order
		so_map = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": channel,
				"provider": clean_prov,
				"external_entity_type": ExternalEntityType.ORDER,
				"external_id": str(refund_record.external_order_id).strip(),
				"active": 1,
			},
			"erp_document",
		)
		if so_map and frappe.db.exists("Sales Order", so_map):
			# Find submitted Sales Invoice against this Sales Order
			invoices = frappe.get_all(
				"Sales Invoice Item",
				filters={"sales_order": so_map, "docstatus": 1},
				pluck="parent",
			)
			if invoices:
				si_name = invoices[0]

	if not si_name or not frappe.db.exists("Sales Invoice", si_name):
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundEligibilityError(
			_("Original Sales Invoice could not be resolved for refund '{0}' (order '{1}', invoice '{2}').").format(
				ref_id, refund_record.external_order_id, si_name
			)
		)

	# 5. Row-level Lock & Invoice Validation (Section 6 & 12)
	locked_si = frappe.db.sql(
		"""
		SELECT name, docstatus, is_return, company, customer, currency,
		       grand_total, outstanding_amount, update_stock, sales_channel,
		       posting_date, posting_time
		FROM `tabSales Invoice`
		WHERE name = %s
		FOR UPDATE
		""",
		si_name,
		as_dict=True,
	)

	if not locked_si:
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundEligibilityError(_("Sales Invoice '{0}' could not be locked.").format(si_name))

	si_data = frappe._dict(locked_si[0])
	if si_data.docstatus != 1:
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundEligibilityError(
			_("Sales Invoice '{0}' must be in Submitted state (docstatus=1). Current: {1}.").format(
				si_name, si_data.docstatus
			)
		)

	if si_data.is_return == 1:
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundEligibilityError(
			_("Sales Invoice '{0}' is already a Return document. Cannot refund against a return.").format(si_name)
		)

	if si_data.sales_channel and si_data.sales_channel != channel:
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise RefundEligibilityError(
			_("Sales Channel mismatch: Invoice '{0}' belongs to '{1}', refund requested on '{2}'.").format(
				si_name, si_data.sales_channel, channel
			)
		)

	if refund_record.company and refund_record.company != si_data.company:
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise CompanyMismatchError(
			_("Company mismatch: Invoice '{0}' belongs to '{1}', refund specified '{2}'.").format(
				si_name, si_data.company, refund_record.company
			)
		)

	if refund_record.customer and refund_record.customer != si_data.customer:
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise CustomerMismatchError(
			_("Customer mismatch: Invoice '{0}' customer is '{1}', refund specified '{2}'.").format(
				si_name, si_data.customer, refund_record.customer
			)
		)

	if refund_record.currency and refund_record.currency.upper() != si_data.currency.upper():
		REFUND_COUNTERS["refund_blocked"] += 1
		REFUND_COUNTERS["refund_failures"] += 1
		raise CurrencyMismatchError(
			_("Currency mismatch: Invoice currency is '{0}', refund currency is '{1}'.").format(
				si_data.currency, refund_record.currency
			)
		)

	si_doc = frappe.get_doc("Sales Invoice", si_name)
	si_grand_total = flt(si_doc.grand_total)

	# 6. Cumulative Over-Refund Check (Section 11 & 12)
	existing_returns = frappe.get_all(
		"Sales Invoice",
		filters={"return_against": si_name, "docstatus": 1, "is_return": 1},
		fields=["name", "grand_total"],
	)
	prior_credited_total = sum(
		abs(flt(r.get("grand_total") if isinstance(r, dict) else getattr(r, "grand_total", 0.0)))
		for r in existing_returns
	)
	remaining_refundable = si_grand_total - prior_credited_total

	requested_amount = refund_record.amount
	if requested_amount is not None:
		if flt(requested_amount) <= 0:
			REFUND_COUNTERS["refund_blocked"] += 1
			REFUND_COUNTERS["refund_failures"] += 1
			raise RefundEligibilityError(_("Refund amount must be strictly positive. Received: {0}").format(requested_amount))

		req_amt = flt(requested_amount)
		if req_amt > (remaining_refundable + 0.005):
			REFUND_COUNTERS["refund_blocked"] += 1
			REFUND_COUNTERS["refund_failures"] += 1
			raise OverRefundBlockedError(
				_(
					"Requested refund amount {0} exceeds remaining refundable scope {1} "
					"(Invoice total: {2}, Prior credited: {3})."
				).format(req_amt, remaining_refundable, si_grand_total, prior_credited_total)
			)

	# 7. Line-level Item Validation & Over-Return Protection (Section 13)
	si_item_rows = {row.name: row for row in si_doc.items}
	si_item_codes = {row.item_code: row for row in si_doc.items}

	prior_returned_qtys = {}
	if existing_returns:
		ret_names = [r.get("name") if isinstance(r, dict) else getattr(r, "name", "") for r in existing_returns]
		prior_ret_items = frappe.get_all(
			"Sales Invoice Item",
			filters={"parent": ["in", ret_names], "docstatus": 1},
			fields=["sales_invoice_item", "item_code", "qty"],
		)
		for rit in prior_ret_items:
			item_row_name = rit.get("sales_invoice_item") if isinstance(rit, dict) else getattr(rit, "sales_invoice_item", None)
			item_c = rit.get("item_code") if isinstance(rit, dict) else getattr(rit, "item_code", None)
			q_val = rit.get("qty") if isinstance(rit, dict) else getattr(rit, "qty", 0.0)
			k = item_row_name or item_c
			if k:
				prior_returned_qtys[k] = prior_returned_qtys.get(k, 0.0) + abs(flt(q_val))

	if refund_record.items:
		for req_it in refund_record.items:
			target_row = None
			if req_it.sales_invoice_item and req_it.sales_invoice_item in si_item_rows:
				target_row = si_item_rows[req_it.sales_invoice_item]
			elif req_it.item_code and req_it.item_code in si_item_codes:
				target_row = si_item_codes[req_it.item_code]

			if not target_row:
				REFUND_COUNTERS["refund_blocked"] += 1
				REFUND_COUNTERS["refund_failures"] += 1
				raise RefundEligibilityError(
					_("Item '{0}' is not present in original Sales Invoice '{1}'.").format(
						req_it.item_code or req_it.sales_invoice_item, si_name
					)
				)

			req_qty = abs(flt(req_it.qty))
			if req_qty <= 0:
				REFUND_COUNTERS["refund_blocked"] += 1
				REFUND_COUNTERS["refund_failures"] += 1
				raise RefundEligibilityError(
					_("Requested return quantity must be strictly positive. Received: {0} for item '{1}'.").format(
						req_qty, target_row.item_code
					)
				)

			row_qty = abs(flt(target_row.qty))
			prior_q = prior_returned_qtys.get(target_row.name, 0.0)
			if prior_q == 0.0 and target_row.item_code in prior_returned_qtys:
				prior_q = prior_returned_qtys[target_row.item_code]

			is_physical_return = bool(refund_record.return_stock) or any(
				getattr(it, "physical_return_evidence", False) for it in (refund_record.items or [])
			)
			if not is_physical_return:
				remaining_q = row_qty - prior_q
				if req_qty > (remaining_q + 0.0001):
					REFUND_COUNTERS["refund_blocked"] += 1
					REFUND_COUNTERS["refund_failures"] += 1
					raise OverRefundBlockedError(
						_(
							"Requested return quantity {0} for item '{1}' exceeds remaining eligible quantity {2} "
							"(Invoiced: {3}, Prior returned: {4})."
						).format(req_qty, target_row.item_code, remaining_q, row_qty, prior_q)
					)

	# 7.1 Physical Return Capacity Validation (Goal C & Phase 1R.1)
	is_physical_return = bool(refund_record.return_stock) or any(
		getattr(it, "physical_return_evidence", False) for it in (refund_record.items or [])
	)
	if is_physical_return:
		items_to_validate = refund_record.items if refund_record.items else [
			ExternalRefundItem(item_code=r.item_code, qty=abs(flt(r.qty))) for r in si_doc.items
		]
		inv_dns = get_delivery_notes_for_invoice(si_doc) if not cint(si_doc.update_stock) else None
		for req_it in items_to_validate:
			item_code = req_it.item_code
			req_phys_qty = abs(flt(req_it.returned_qty if req_it.returned_qty is not None else req_it.qty))
			deliv_qty, ret_phys_qty, rem_phys_qty = get_physical_return_scope(
				si_doc, item_code, inv_dns
			)
			if req_phys_qty > (rem_phys_qty + 0.0001):
				REFUND_COUNTERS["refund_blocked"] += 1
				REFUND_COUNTERS["refund_failures"] += 1
				raise OverRefundBlockedError(
					_(
						"Requested physical return quantity {0} for item '{1}' exceeds remaining physical return capacity {2} "
						"(Delivered: {3}, Already physically returned: {4})."
					).format(req_phys_qty, item_code, rem_phys_qty, deliv_qty, ret_phys_qty)
				)


	# 8. Savepoint-wrapped Execution
	sp_refund = f"sp_ref_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_refund)

	try:
		# Call native ERPNext return mapper
		cn = make_return_doc("Sales Invoice", si_name)
		cn.sales_channel = channel
		cn.transaction_origin = refund_record.transaction_origin or TransactionOrigin.WEB
		if refund_record.reason:
			cn.remarks = refund_record.reason

		# Ensure posting datetime is strictly after original invoice & delivery note
		si_posting_dt = get_datetime(f"{si_doc.posting_date} {si_doc.posting_time or '00:00:00'}")
		now_dt = now_datetime()
		earliest_dt = max(now_dt, si_posting_dt)

		dn_name = None
		for row in si_doc.items:
			if getattr(row, "delivery_note", None) and isinstance(row.delivery_note, str):
				dn_name = row.delivery_note
				break
		if not dn_name:
			# Fallback: check if Delivery Note exists against the Sales Order
			so_name_val = getattr(si_doc, "sales_order", None)
			if not so_name_val or not isinstance(so_name_val, str):
				so_name_val = None
				for row in si_doc.items:
					if getattr(row, "sales_order", None) and isinstance(row.sales_order, str):
						so_name_val = row.sales_order
						break
			if so_name_val and isinstance(so_name_val, str):
				dn_names = frappe.db.sql(
					"""
					SELECT DISTINCT parent FROM `tabDelivery Note Item`
					WHERE against_sales_order = %s AND docstatus = 1
					ORDER BY creation DESC LIMIT 1
					""",
					(so_name_val,),
					pluck="parent",
				)
				if dn_names:
					dn_name = dn_names[0]

		if dn_name and isinstance(dn_name, str) and frappe.db.exists("Delivery Note", dn_name):
			dn_dt_val = frappe.db.get_value("Delivery Note", dn_name, ["posting_date", "posting_time"], as_dict=True)

			if dn_dt_val:
				dn_posting_dt = get_datetime(f"{dn_dt_val.posting_date} {dn_dt_val.posting_time or '00:00:00'}")
				earliest_dt = max(earliest_dt, dn_posting_dt)

		return_dt = add_to_date(earliest_dt, seconds=5)
		cn.posting_date = return_dt.strftime("%Y-%m-%d")
		cn.posting_time = return_dt.strftime("%H:%M:%S")
		cn.set_posting_time = 1

		# Apply line item adjustments or filtering
		if refund_record.items:
			req_by_row = {
				it.sales_invoice_item: it for it in refund_record.items if it.sales_invoice_item
			}
			req_by_code = {
				it.item_code: it for it in refund_record.items if it.item_code
			}

			retained_items = []
			for cn_row in cn.items:
				match = req_by_row.get(cn_row.sales_invoice_item) or req_by_code.get(cn_row.item_code)
				if match:
					cn_row.qty = -1 * abs(flt(match.qty))
					if match.rate is not None:
						cn_row.rate = flt(match.rate)
					retained_items.append(cn_row)

			cn.items = retained_items
			cn.set_missing_values()
			cn.calculate_taxes_and_totals()
		elif requested_amount is not None:
			req_amt = _round_curr(requested_amount)
			si_tot = _round_curr(si_doc.grand_total)
			if req_amt != si_tot or len(existing_returns) > 0:
				rem_credit_needed = flt(req_amt)
				new_items = []
				# First pass: try available items from make_return_doc (which have unreturned quantity)
				for cn_row in cn.items:
					if rem_credit_needed <= 0.001:
						break
					orig_rate = flt(cn_row.rate)
					orig_qty = abs(flt(cn_row.qty))
					if orig_qty <= 0.001:
						continue
					orig_line_total = orig_qty * orig_rate

					line_credit = min(rem_credit_needed, orig_line_total)
					if orig_rate > 0:
						full_units = int(line_credit // orig_rate)
						leftover = line_credit - (full_units * orig_rate)
						if full_units > 0 and leftover < 0.001:
							cn_row.qty = -1 * full_units
							cn_row.rate = orig_rate
							new_items.append(cn_row)
							rem_credit_needed -= line_credit
						else:
							needed_units = max(1.0, float(int(line_credit / orig_rate + 0.9999)))
							needed_units = min(needed_units, orig_qty)
							if needed_units > 0:
								effective_rate = line_credit / needed_units
								cn_row.qty = -1 * needed_units
								cn_row.rate = effective_rate
								new_items.append(cn_row)
								rem_credit_needed -= line_credit
					elif orig_qty > 0:
						cn_row.qty = -1 * orig_qty
						new_items.append(cn_row)

				# Second pass: if credit is still needed (e.g. earlier returns already credited against all item rows,
				# but invoice grand_total still allows further credit), source from original invoice rows
				# with sales_invoice_item set to None so ERPNext does not block on line-level over-return
				if rem_credit_needed > 0.001 and not refund_record.return_stock:
					for orig_row in si_doc.items:
						if rem_credit_needed <= 0.001:
							break
						orig_rate = flt(orig_row.rate)
						if orig_rate <= 0:
							continue
						# Create an unlinked return line for financial credit
						extra_row = cn.append("items", {})
						for fld in [
							"item_code", "item_name", "description", "uom", "stock_uom",
							"conversion_factor", "income_account", "cost_center"
						]:
							setattr(extra_row, fld, getattr(orig_row, fld, None))
						extra_row.sales_invoice_item = None
						extra_row.qty = -1.0
						extra_row.rate = rem_credit_needed
						new_items.append(extra_row)
						rem_credit_needed = 0.0
						break

				if not refund_record.return_stock:
					for row_it in new_items:
						row_it.sales_invoice_item = None

				cn.items = new_items
				cn.set_missing_values()

				cn.calculate_taxes_and_totals()

		# Handle stock return semantics (Section 8 & 9)
		return_dn_doc = None
		returned_item_codes = []
		if not refund_record.return_stock:
			# Financial-only refund: update_stock=0, NO stock movement, NO publication
			cn.update_stock = 0
			REFUND_COUNTERS["financial_only_credits"] += 1
		else:
			REFUND_COUNTERS["stock_returns"] += 1
			returned_item_codes = [r.item_code for r in cn.items if r.item_code]

			if cint(si_doc.update_stock) == 1:
				# Original SI was direct delivery: return SI with update_stock=1 restores stock natively
				cn.update_stock = 1
			else:
				# Original SI was delivered via Delivery Note: ERPNext requires Return Delivery Note
				cn.update_stock = 0
				inv_dns = get_delivery_notes_for_invoice(si_doc)
				target_dn = None
				if inv_dns:
					for cand in inv_dns:
						cand_suitable = True
						items_to_check = refund_record.items if refund_record.items else [
							ExternalRefundItem(item_code=r.item_code, qty=abs(flt(r.qty))) for r in si_doc.items
						]
						for it in items_to_check:
							it_code = it.item_code
							req_q = abs(flt(it.returned_qty if it.returned_qty is not None else it.qty))
							c_del, c_ret, c_rem = get_physical_return_scope(si_doc, it_code, [cand])
							if req_q > (c_rem + 0.0001):
								cand_suitable = False
								break
						if cand_suitable:
							target_dn = cand
							break
					if not target_dn:
						target_dn = inv_dns[0]
				elif dn_name:
					target_dn = dn_name

				if target_dn and frappe.db.exists("Delivery Note", target_dn):
					ret_dn = make_return_doc("Delivery Note", target_dn)
					ret_dn.posting_date = cn.posting_date
					ret_dn.posting_time = cn.posting_time
					ret_dn.set_posting_time = 1
					if refund_record.items:
						req_codes = {it.item_code for it in refund_record.items if it.item_code}
						ret_dn.items = [r for r in ret_dn.items if r.item_code in req_codes]
						for r in ret_dn.items:
							for it in refund_record.items:
								if it.item_code == r.item_code:
									ret_qty = it.returned_qty if it.returned_qty is not None else it.qty
									if ret_qty:
										r.qty = -1 * abs(flt(ret_qty))
					ret_dn.flags.ignore_permissions = True
					ret_dn.insert()
					ret_dn.submit()
					return_dn_doc = ret_dn


			# Transactional inventory publication intent (Section 22)
			if schedule_channel_inventory_publication and returned_item_codes:
				try:
					schedule_channel_inventory_publication(
						sales_channel=channel,
						item_codes=returned_item_codes,
					)
				except Exception as pub_err:
					logger.warning("Failed to schedule inventory publication for return: %s", str(pub_err))

		# Insert and Submit Return Sales Invoice
		cn.flags.ignore_permissions = True
		cn.insert()

		if submit:
			cn.submit()

		# Track full vs partial refund telemetry
		credit_amount = abs(flt(cn.grand_total))
		if credit_amount < (si_grand_total - 0.005):
			REFUND_COUNTERS["partial_refunds"] += 1
		else:
			REFUND_COUNTERS["full_refunds"] += 1

		# 9. Native Accounting Allocation (Section 14)
		si_doc.reload()
		if submit and flt(si_doc.outstanding_amount) > 0.001:
			# Unpaid or partially unpaid invoice: allocate credit note against invoice natively
			alloc_amount = min(credit_amount, flt(si_doc.outstanding_amount))
			allocation = frappe._dict({
				"voucher_type": "Sales Invoice",
				"voucher_no": cn.name,
				"against_voucher_type": "Sales Invoice",
				"against_voucher": si_doc.name,
				"allocated_amount": alloc_amount,
				"unadjusted_amount": flt(cn.outstanding_amount),
				"dr_or_cr": "credit_in_account_currency",
				"account": cn.debit_to,
				"party_type": "Customer",
				"party": cn.customer,
				"exchange_rate": flt(cn.conversion_rate or 1.0),
				"currency": cn.currency,
				"difference_amount": 0.0,
				"debit_or_credit_note_posting_date": cn.posting_date,
			})
			reconcile_dr_cr_note([allocation], company=cn.company)
			si_doc.reload()
			cn.reload()

		# 10. Persist External ID Mapping for REFUND (Section 5)
		map_doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": channel,
			"provider": clean_prov,
			"external_entity_type": ExternalEntityType.REFUND,
			"external_id": ref_id,
			"erp_doctype": "Sales Invoice",
			"erp_document": cn.name,
			"active": 1,
			"sync_hash": ref_idempotency_key,
			"last_synced_at": now_datetime(),
		})
		map_doc.flags.ignore_permissions = True
		map_doc.insert()

		REFUND_COUNTERS["refunds_completed"] += 1
		logger.info(
			"Successfully processed refund '%s' -> Credit Note '%s' (return_against '%s', total %s).",
			ref_id,
			cn.name,
			si_name,
			cn.grand_total,
		)

		if return_dn_doc:
			cn._return_delivery_note = return_dn_doc

		return cn

	except Exception as err:
		frappe.db.rollback(save_point=sp_refund)
		REFUND_COUNTERS["refund_failures"] += 1
		logger.error(
			"Failed to process external refund '%s' against invoice '%s': %s",
			ref_id,
			si_name,
			str(err),
		)
		raise
