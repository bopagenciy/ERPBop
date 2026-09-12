# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from typing import Any, Dict, List, Optional, Tuple

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

import erpnext.buying.doctype.purchase_order.purchase_order as po_module
import erpnext.stock.doctype.purchase_receipt.purchase_receipt as pr_module
import erpnext.accounts.doctype.purchase_invoice.purchase_invoice as pi_module
import erpnext.accounts.doctype.payment_entry.payment_entry as pe_module

from bop_erp.orders.ingestion import (
	find_affected_channel_items_for_scopes,
	schedule_post_commit_publication,
)
from bop_erp.purchasing.exceptions import (
	AccountMismatchError,
	CompanyMismatchError,
	ConcurrentReceiptConflictError,
	DocumentCancelledError,
	DownstreamCancellationBlockedError,
	OverBillingBlockedError,
	OverPaymentBlockedError,
	OverReceiptBlockedError,
	PurchaseInvoiceError,
	PurchaseOrderError,
	PurchaseReceiptError,
	PurchaseReplayCancelledError,
	PurchasingError,
	VendorPaymentError,
	WarehouseMismatchError,
)
from bop_erp.purchasing.idempotency import (
	PurchaseOperation,
	check_purchase_operation_replay,
	record_purchase_operation,
)

# Structured Observability Counters (Phase 1S Telemetry)
PURCHASING_COUNTERS: Dict[str, int] = {
	"purchase_order_requests": 0,
	"purchase_orders_created": 0,
	"purchase_receipts_created": 0,
	"purchase_invoices_created": 0,
	"vendor_payments_created": 0,
	"duplicate_operations_converged": 0,
	"purchase_operations_blocked": 0,
	"purchase_failures": 0,
}


def reset_purchasing_counters() -> None:
	"""Resets all structured purchasing telemetry counters to zero."""
	for k in PURCHASING_COUNTERS:
		PURCHASING_COUNTERS[k] = 0


def get_purchasing_counters() -> Dict[str, int]:
	"""Returns a snapshot copy of current purchasing counters."""
	return dict(PURCHASING_COUNTERS)


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
	"""Safe attribute/key lookup across dict and object records."""
	if isinstance(obj, dict):
		return obj.get(key, default)
	return getattr(obj, key, default)


def validate_company_warehouse(company: str, warehouse: str) -> None:
	"""Enforces that a warehouse exists and belongs to the specified company."""
	if not warehouse:
		return
	wh_company = frappe.db.get_value("Warehouse", warehouse, "company")
	if not wh_company:
		raise WarehouseMismatchError(f"Warehouse '{warehouse}' does not exist.")
	if wh_company != company:
		raise CompanyMismatchError(
			f"Warehouse '{warehouse}' belongs to Company '{wh_company}', not transaction Company '{company}'."
		)


def validate_company_account(company: str, account: str, account_role: str = "Account") -> None:
	"""Enforces that an account exists and belongs to the specified company."""
	if not account:
		return
	acct_company = frappe.db.get_value("Account", account, "company")
	if not acct_company:
		raise AccountMismatchError(f"{account_role} '{account}' does not exist.")
	if acct_company != company:
		raise CompanyMismatchError(
			f"{account_role} '{account}' belongs to Company '{acct_company}', not transaction Company '{company}'."
		)


# ==============================================================================
# PURCHASE ORDER
# ==============================================================================

def create_purchase_order(
	data: Dict[str, Any],
	submit: bool = False,
	operation_key: Optional[str] = None,
) -> Any:
	"""
	Creates a native Purchase Order with company, warehouse, and supplier validations.
	PO is a financial commitment only:
	- Zero physical stock movement
	- Zero Stock Ledger Entries
	- Zero GL Entries
	- Zero inventory publication
	"""
	PURCHASING_COUNTERS["purchase_order_requests"] += 1

	company = data.get("company")
	supplier = data.get("supplier")
	if not company:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise CompanyMismatchError("Company is required for Purchase Order.")
	if not supplier:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise PurchaseOrderError("Supplier is required for Purchase Order.")

	if operation_key:
		replay = check_purchase_operation_replay(
			operation_key, PurchaseOperation.CREATE_PURCHASE_ORDER, data, company=company
		)
		if replay:
			PURCHASING_COUNTERS["duplicate_operations_converged"] += 1
			return frappe.get_doc(replay[0], replay[1])

	# Validate default warehouse if present
	default_wh = data.get("set_warehouse")
	if default_wh:
		validate_company_warehouse(company, default_wh)

	items = data.get("items") or []
	if not items:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise PurchaseOrderError("Purchase Order must have at least one item.")

	for item in items:
		wh = item.get("warehouse") or default_wh
		if wh:
			validate_company_warehouse(company, wh)

	try:
		po_dict = {
			"doctype": "Purchase Order",
			"company": company,
			"supplier": supplier,
			"transaction_date": data.get("transaction_date") or nowdate(),
			"schedule_date": data.get("schedule_date") or data.get("transaction_date") or nowdate(),
			"currency": data.get("currency"),
			"conversion_rate": data.get("conversion_rate"),
			"set_warehouse": default_wh,
			"payment_terms_template": data.get("payment_terms_template"),
			"tc_name": data.get("tc_name"),
			"taxes_and_charges": data.get("taxes_and_charges"),
			"apply_discount_on": data.get("apply_discount_on"),
			"discount_amount": data.get("discount_amount"),
			"items": [],
		}

		for item in items:
			item_row = {
				"item_code": item.get("item_code"),
				"item_name": item.get("item_name"),
				"qty": flt(item.get("qty")),
				"rate": flt(item.get("rate")),
				"uom": item.get("uom"),
				"stock_uom": item.get("stock_uom"),
				"conversion_factor": flt(item.get("conversion_factor")) or 1.0,
				"warehouse": item.get("warehouse") or default_wh,
				"schedule_date": item.get("schedule_date") or po_dict["schedule_date"],
			}
			po_dict["items"].append(item_row)

		if data.get("taxes"):
			po_dict["taxes"] = data["taxes"]

		po_doc = frappe.get_doc(po_dict)
		po_doc.insert()

		if submit:
			po_doc.submit()

		if operation_key:
			record_purchase_operation(
				operation_key=operation_key,
				operation_type=PurchaseOperation.CREATE_PURCHASE_ORDER,
				payload=data,
				erp_doctype="Purchase Order",
				erp_document=po_doc.name,
				company=company,
			)

		PURCHASING_COUNTERS["purchase_orders_created"] += 1
		return po_doc

	except Exception as e:
		PURCHASING_COUNTERS["purchase_failures"] += 1
		raise


# ==============================================================================
# PURCHASE RECEIPT / GOODS RECEIPT
# ==============================================================================

def receive_purchase_order(
	po_name: str,
	items_to_receive: Optional[List[Dict[str, Any]]] = None,
	submit: bool = True,
	operation_key: Optional[str] = None,
	posting_date: Optional[str] = None,
	target_warehouse: Optional[str] = None,
) -> Any:
	"""
	Receives physical stock against a Purchase Order via native make_purchase_receipt.
	Enforces:
	- Row-level MariaDB locking on Purchase Order (FOR UPDATE)
	- Fresh state inspection to prevent concurrent race conditions
	- Tolerance-respecting over-receipt validation
	- Single stock movement via native SLE on submit
	- Affected sales channel discovery and durable outbox publication intent
	- Zero publication if warehouse is non-sellable/quarantine
	"""
	payload = {
		"po_name": po_name,
		"items_to_receive": items_to_receive,
		"target_warehouse": target_warehouse,
		"posting_date": posting_date,
	}

	# Row-level lock on Purchase Order to serialize concurrent receipt attempts
	po_locked = frappe.db.sql(
		"SELECT name, docstatus, company FROM `tabPurchase Order` WHERE name = %s FOR UPDATE",
		(po_name,),
		as_dict=True,
	)
	if not po_locked:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise PurchaseOrderError(f"Purchase Order '{po_name}' not found.")

	po_row = po_locked[0]
	company = _get_val(po_row, "company")

	if operation_key:
		replay = check_purchase_operation_replay(
			operation_key, PurchaseOperation.RECEIVE_PURCHASE_ORDER, payload, company=company
		)
		if replay:
			PURCHASING_COUNTERS["duplicate_operations_converged"] += 1
			return frappe.get_doc(replay[0], replay[1])

	po_docstatus = _get_val(po_row, "docstatus")
	if po_docstatus == 0:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise PurchaseOrderError(f"Purchase Order '{po_name}' must be submitted before receiving.")
	if po_docstatus == 2:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise DocumentCancelledError(f"Purchase Order '{po_name}' is cancelled.")

	if target_warehouse:
		validate_company_warehouse(company, target_warehouse)

	# Fetch fresh unreceived quantities from database
	po_items = frappe.db.sql(
		"""
		SELECT name, item_code, qty, received_qty, warehouse
		FROM `tabPurchase Order Item`
		WHERE parent = %s
		""",
		(po_name,),
		as_dict=True,
	)
	po_item_map = {_get_val(item, "name"): item for item in po_items}
	po_item_code_map = {_get_val(item, "item_code"): item for item in po_items}

	# Map from PO to PR via native helper
	pr_doc = po_module.make_purchase_receipt(po_name)
	if posting_date:
		pr_doc.posting_date = posting_date

	if target_warehouse:
		pr_doc.set_warehouse = target_warehouse

	# Check over-receipt against locked PO items first
	for req in (items_to_receive or []):
		req_key = req.get("purchase_order_item") or req.get("item_code")
		req_qty = flt(req.get("qty"))
		po_item = po_item_map.get(req_key) or po_item_code_map.get(req_key)
		if po_item:
			ordered_qty = flt(_get_val(po_item, "qty"))
			already_received = flt(_get_val(po_item, "received_qty"))
			item_code = _get_val(po_item, "item_code")
			tolerance = (
				frappe.db.get_value("Item", item_code, "over_delivery_receipt_allowance")
				or frappe.db.get_single_value("Stock Settings", "over_delivery_receipt_allowance")
				or 0.0
			)
			max_allowed = ordered_qty * (1.0 + flt(tolerance) / 100.0)
			if (already_received + req_qty) > (max_allowed + 1e-6):
				PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
				raise OverReceiptBlockedError(
					f"Over-receipt blocked for item '{item_code}'. "
					f"Ordered: {ordered_qty}, already received: {already_received}, "
					f"requested: {req_qty}, max allowed: {max_allowed}."
				)

	# If specific item quantities requested, adjust receipt items
	if items_to_receive is not None:
		receipt_requests = {}
		for req in items_to_receive:
			key = req.get("purchase_order_item") or req.get("item_code")
			receipt_requests[key] = flt(req.get("qty"))

		filtered_items = []
		for row in pr_doc.items:
			req_qty = None
			if row.purchase_order_item in receipt_requests:
				req_qty = receipt_requests[row.purchase_order_item]
			elif row.item_code in receipt_requests:
				req_qty = receipt_requests[row.item_code]

			if req_qty is not None and req_qty > 0:
				row.qty = req_qty
				row.stock_qty = req_qty * (flt(row.conversion_factor) or 1.0)
				row.amount = req_qty * flt(row.rate)
				if target_warehouse:
					row.warehouse = target_warehouse
				validate_company_warehouse(company, row.warehouse)
				filtered_items.append(row)

		if not filtered_items:
			PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
			raise PurchaseReceiptError("No matching items to receive with positive quantity.")

		pr_doc.items = filtered_items

	# Over-receipt validation under lock
	for row in pr_doc.items:
		po_item = po_item_map.get(row.purchase_order_item) or po_item_code_map.get(row.item_code)
		if po_item:
			ordered_qty = flt(_get_val(po_item, "qty"))
			already_received = flt(_get_val(po_item, "received_qty"))
			current_receive = flt(row.qty)

			tolerance = (
				frappe.db.get_value("Item", row.item_code, "over_delivery_receipt_allowance")
				or frappe.db.get_single_value("Stock Settings", "over_delivery_receipt_allowance")
				or 0.0
			)
			max_allowed = ordered_qty * (1.0 + flt(tolerance) / 100.0)

			if (already_received + current_receive) > (max_allowed + 1e-6):
				PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
				raise OverReceiptBlockedError(
					f"Over-receipt blocked for item '{row.item_code}'. "
					f"Ordered: {ordered_qty}, already received: {already_received}, "
					f"requested: {current_receive}, max allowed: {max_allowed}."
				)

	try:
		pr_doc.insert()

		if submit:
			pr_doc.submit()

			# Determine affected scopes for durable inventory publication
			scopes = []
			for row in pr_doc.items:
				is_stock = frappe.db.get_value("Item", row.item_code, "is_stock_item")
				if is_stock:
					scopes.append((row.item_code, row.warehouse))

			if scopes:
				channel_items_map = find_affected_channel_items_for_scopes(scopes)
				if channel_items_map:
					schedule_post_commit_publication(channel_items_map)

		if operation_key:
			record_purchase_operation(
				operation_key=operation_key,
				operation_type=PurchaseOperation.RECEIVE_PURCHASE_ORDER,
				payload=payload,
				erp_doctype="Purchase Receipt",
				erp_document=pr_doc.name,
				company=company,
			)

		PURCHASING_COUNTERS["purchase_receipts_created"] += 1
		return pr_doc

	except Exception as e:
		PURCHASING_COUNTERS["purchase_failures"] += 1
		raise


# ==============================================================================
# PURCHASE INVOICE / ACCOUNTS PAYABLE
# ==============================================================================

def create_purchase_invoice(
	pr_name: Optional[str] = None,
	po_name: Optional[str] = None,
	items_to_invoice: Optional[List[Dict[str, Any]]] = None,
	payable_account: Optional[str] = None,
	submit: bool = True,
	operation_key: Optional[str] = None,
	posting_date: Optional[str] = None,
	bill_no: Optional[str] = None,
) -> Any:
	"""
	Creates a native Purchase Invoice.
	Rules:
	- When mapped from Purchase Receipt, update_stock=0 (no duplicate stock movement)
	- AP liability created natively with balancing GL Entries
	- Rejects overbilling beyond native/configured allowance
	- Company matching on payable account strictly enforced
	"""
	payload = {
		"pr_name": pr_name,
		"po_name": po_name,
		"items_to_invoice": items_to_invoice,
		"payable_account": payable_account,
		"bill_no": bill_no,
		"posting_date": posting_date,
	}

	if not pr_name and not po_name:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise PurchaseInvoiceError("Either Purchase Receipt or Purchase Order must be provided.")

	if pr_name:
		# Lock Purchase Receipt
		pr_locked = frappe.db.sql(
			"SELECT name, docstatus, company, supplier FROM `tabPurchase Receipt` WHERE name = %s FOR UPDATE",
			(pr_name,),
			as_dict=True,
		)
		if not pr_locked:
			PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
			raise PurchaseReceiptError(f"Purchase Receipt '{pr_name}' not found.")

		company = _get_val(pr_locked[0], "company")

		if operation_key:
			replay = check_purchase_operation_replay(
				operation_key, PurchaseOperation.CREATE_PURCHASE_INVOICE, payload, company=company
			)
			if replay:
				PURCHASING_COUNTERS["duplicate_operations_converged"] += 1
				return frappe.get_doc(replay[0], replay[1])

		if _get_val(pr_locked[0], "docstatus") != 1:
			PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
			raise PurchaseInvoiceError(f"Purchase Receipt '{pr_name}' must be submitted before invoicing.")

		try:
			pi_doc = pr_module.make_purchase_invoice(pr_name)
		except frappe.ValidationError as e:
			if "already been Invoiced" in str(e) or "already been billed" in str(e).lower():
				PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
				raise OverBillingBlockedError(
					f"Over-billing blocked: All items on Purchase Receipt '{pr_name}' have already been invoiced."
				)
			raise
		# Crucial: stock was already moved by PR, so PI must NOT move stock again
		pi_doc.update_stock = 0

	else:
		# Direct from PO (for non-stock/service procurement or PO-direct flow)
		po_locked = frappe.db.sql(
			"SELECT name, docstatus, company, supplier FROM `tabPurchase Order` WHERE name = %s FOR UPDATE",
			(po_name,),
			as_dict=True,
		)
		if not po_locked:
			PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
			raise PurchaseOrderError(f"Purchase Order '{po_name}' not found.")

		company = _get_val(po_locked[0], "company")

		if operation_key:
			replay = check_purchase_operation_replay(
				operation_key, PurchaseOperation.CREATE_PURCHASE_INVOICE, payload, company=company
			)
			if replay:
				PURCHASING_COUNTERS["duplicate_operations_converged"] += 1
				return frappe.get_doc(replay[0], replay[1])

		if _get_val(po_locked[0], "docstatus") != 1:
			PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
			raise PurchaseInvoiceError(f"Purchase Order '{po_name}' must be submitted before invoicing.")

		try:
			pi_doc = po_module.make_purchase_invoice(po_name)
		except frappe.ValidationError as e:
			if "already been Invoiced" in str(e) or "already been billed" in str(e).lower():
				PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
				raise OverBillingBlockedError(
					f"Over-billing blocked: All items on Purchase Order '{po_name}' have already been invoiced."
				)
			raise
		pi_doc.update_stock = 0

	if bill_no:
		pi_doc.bill_no = bill_no
	if posting_date:
		pi_doc.posting_date = posting_date

	if payable_account:
		validate_company_account(company, payable_account, "Payable Account")
		pi_doc.credit_to = payable_account
	else:
		validate_company_account(company, pi_doc.credit_to, "Payable Account")

	# If specific item quantities requested for partial invoice
	if items_to_invoice is not None:
		invoice_requests = {}
		for req in items_to_invoice:
			key = req.get("pr_detail") or req.get("po_detail") or req.get("item_code")
			invoice_requests[key] = flt(req.get("qty"))

		filtered_items = []
		for row in pi_doc.items:
			req_qty = None
			if getattr(row, "pr_detail", None) in invoice_requests:
				req_qty = invoice_requests[row.pr_detail]
			elif getattr(row, "po_detail", None) in invoice_requests:
				req_qty = invoice_requests[row.po_detail]
			elif row.item_code in invoice_requests:
				req_qty = invoice_requests[row.item_code]

			if req_qty is not None and req_qty > 0:
				row.qty = req_qty
				row.amount = req_qty * flt(row.rate)
				filtered_items.append(row)

		if not filtered_items:
			PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
			raise PurchaseInvoiceError("No matching items to invoice with positive quantity.")

		pi_doc.items = filtered_items

	# Over-billing validation under lock
	for row in pi_doc.items:
		if getattr(row, "pr_detail", None):
			pr_item = frappe.db.get_value(
				"Purchase Receipt Item",
				row.pr_detail,
				["qty", "billed_amt", "amount"],
				as_dict=True,
			)
			if pr_item:
				received_qty = flt(_get_val(pr_item, "qty"))
				billed_qty_sum = frappe.db.sql(
					"""
					SELECT SUM(qty) FROM `tabPurchase Invoice Item`
					WHERE pr_detail = %s AND docstatus = 1
					""",
					(row.pr_detail,),
				)[0][0] or 0.0

				allowance = (
					frappe.db.get_value("Item", row.item_code, "over_billing_allowance")
					or frappe.db.get_single_value("Accounts Settings", "over_billing_allowance")
					or 0.0
				)
				max_billable = received_qty * (1.0 + flt(allowance) / 100.0)

				if (flt(billed_qty_sum) + flt(row.qty)) > (max_billable + 1e-6):
					PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
					raise OverBillingBlockedError(
						f"Over-billing blocked for item '{row.item_code}'. "
						f"Received: {received_qty}, already billed: {billed_qty_sum}, "
						f"requested: {row.qty}, max billable: {max_billable}."
					)

	try:
		pi_doc.insert()

		if submit:
			pi_doc.submit()

		if operation_key:
			record_purchase_operation(
				operation_key=operation_key,
				operation_type=PurchaseOperation.CREATE_PURCHASE_INVOICE,
				payload=payload,
				erp_doctype="Purchase Invoice",
				erp_document=pi_doc.name,
				company=company,
			)

		PURCHASING_COUNTERS["purchase_invoices_created"] += 1
		return pi_doc

	except Exception as e:
		PURCHASING_COUNTERS["purchase_failures"] += 1
		raise


# ==============================================================================
# VENDOR PAYMENT
# ==============================================================================

def pay_purchase_invoice(
	pi_name: str,
	paid_amount: Optional[float] = None,
	bank_account: Optional[str] = None,
	mode_of_payment: Optional[str] = None,
	submit: bool = True,
	operation_key: Optional[str] = None,
	posting_date: Optional[str] = None,
) -> Any:
	"""
	Settle AP liability via native Payment Entry.
	Enforces:
	- Row-level lock on Purchase Invoice (FOR UPDATE)
	- Fresh outstanding amount inspection
	- Overpayment blocked (paid_amount cannot exceed outstanding)
	- Company matching on bank/cash account
	- Native GL Entry posting and native outstanding_amount settlement
	"""
	payload = {
		"pi_name": pi_name,
		"paid_amount": paid_amount,
		"bank_account": bank_account,
		"mode_of_payment": mode_of_payment,
		"posting_date": posting_date,
	}

	# Lock Purchase Invoice
	pi_locked = frappe.db.sql(
		"SELECT name, docstatus, company, outstanding_amount, supplier, credit_to FROM `tabPurchase Invoice` WHERE name = %s FOR UPDATE",
		(pi_name,),
		as_dict=True,
	)
	if not pi_locked:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise PurchaseInvoiceError(f"Purchase Invoice '{pi_name}' not found.")

	pi_row = pi_locked[0]
	company = _get_val(pi_row, "company")

	if operation_key:
		replay = check_purchase_operation_replay(
			operation_key, PurchaseOperation.PAY_PURCHASE_INVOICE, payload, company=company
		)
		if replay:
			PURCHASING_COUNTERS["duplicate_operations_converged"] += 1
			return frappe.get_doc(replay[0], replay[1])

	if _get_val(pi_row, "docstatus") != 1:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise PurchaseInvoiceError(f"Purchase Invoice '{pi_name}' must be submitted before payment.")
	outstanding = flt(_get_val(pi_row, "outstanding_amount"))

	if outstanding <= 0:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise OverPaymentBlockedError(f"Purchase Invoice '{pi_name}' has zero outstanding amount.")

	target_amount = flt(paid_amount) if paid_amount is not None else outstanding
	if target_amount <= 0:
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise VendorPaymentError("Payment amount must be greater than zero.")

	if target_amount > (outstanding + 1e-6):
		PURCHASING_COUNTERS["purchase_operations_blocked"] += 1
		raise OverPaymentBlockedError(
			f"Payment amount {target_amount} exceeds outstanding amount {outstanding} on Purchase Invoice '{pi_name}'."
		)

	if bank_account:
		validate_company_account(company, bank_account, "Bank/Cash Account")

	# Native payment entry mapper
	pe_doc = pe_module.get_payment_entry(
		dt="Purchase Invoice",
		dn=pi_name,
		party_amount=target_amount,
		bank_account=bank_account,
	)

	if mode_of_payment:
		pe_doc.mode_of_payment = mode_of_payment
	if posting_date:
		pe_doc.posting_date = posting_date

	# Explicitly assign paid and received amounts to ensure exact allocation
	pe_doc.paid_amount = target_amount
	pe_doc.received_amount = target_amount

	# Ensure references reflect target allocation
	if pe_doc.references:
		for ref in pe_doc.references:
			if ref.reference_name == pi_name:
				ref.allocated_amount = target_amount

	validate_company_account(company, pe_doc.paid_from, "Bank/Cash Account")
	validate_company_account(company, pe_doc.paid_to, "Payable Account")

	if not getattr(pe_doc, "reference_no", None):
		pe_doc.reference_no = operation_key or f"PAY-{pi_name}"
	if not getattr(pe_doc, "reference_date", None):
		pe_doc.reference_date = pe_doc.posting_date or nowdate()

	try:
		pe_doc.insert()

		if submit:
			pe_doc.submit()

		if operation_key:
			record_purchase_operation(
				operation_key=operation_key,
				operation_type=PurchaseOperation.PAY_PURCHASE_INVOICE,
				payload=payload,
				erp_doctype="Payment Entry",
				erp_document=pe_doc.name,
				company=company,
			)

		PURCHASING_COUNTERS["vendor_payments_created"] += 1
		return pe_doc

	except Exception as e:
		PURCHASING_COUNTERS["purchase_failures"] += 1
		raise


# ==============================================================================
# CANCELLATION & TRACEABILITY
# ==============================================================================

def cancel_vendor_payment(pe_name: str) -> Any:
	"""
	Natively cancels a Payment Entry.
	Guarantees:
	- Restores Purchase Invoice outstanding_amount natively
	- Reverses GL Entries natively
	- Leaves Purchase Invoice, Purchase Receipt, and Purchase Order intact
	"""
	pe_doc = frappe.get_doc("Payment Entry", pe_name)
	if pe_doc.docstatus != 1:
		raise VendorPaymentError(f"Payment Entry '{pe_name}' is not submitted; cannot cancel.")

	pe_doc.cancel()
	return pe_doc


def cancel_purchase_document(doctype: str, docname: str) -> Any:
	"""
	Safely cancels a purchasing document respecting native link validation.
	Enforces downstream dependency blocking:
	- Purchase Order cannot be cancelled if submitted Purchase Receipt or Invoice exists.
	- Purchase Receipt cannot be cancelled if submitted Purchase Invoice exists.
	- Purchase Invoice cannot be cancelled if submitted Payment Entry exists.
	Does NOT use ignore_links or broad bypasses.
	"""
	if doctype not in ("Purchase Order", "Purchase Receipt", "Purchase Invoice", "Payment Entry"):
		raise PurchasingError(f"Invalid purchasing doctype: {doctype}")

	doc = frappe.get_doc(doctype, docname)
	if doc.docstatus != 1:
		raise PurchasingError(f"Document '{doctype}' '{docname}' is not submitted; cannot cancel.")

	if doctype == "Purchase Order":
		prs = frappe.db.sql(
			"SELECT DISTINCT parent FROM `tabPurchase Receipt Item` WHERE purchase_order = %s AND docstatus = 1",
			(docname,),
			pluck="parent",
		)
		if prs:
			raise DownstreamCancellationBlockedError(
				f"Cannot cancel Purchase Order '{docname}': submitted Purchase Receipt '{prs[0]}' exists."
			)
		pis = frappe.db.sql(
			"SELECT DISTINCT parent FROM `tabPurchase Invoice Item` WHERE purchase_order = %s AND docstatus = 1",
			(docname,),
			pluck="parent",
		)
		if pis:
			raise DownstreamCancellationBlockedError(
				f"Cannot cancel Purchase Order '{docname}': submitted Purchase Invoice '{pis[0]}' exists."
			)

	elif doctype == "Purchase Receipt":
		pis = frappe.db.sql(
			"SELECT DISTINCT parent FROM `tabPurchase Invoice Item` WHERE purchase_receipt = %s AND docstatus = 1",
			(docname,),
			pluck="parent",
		)
		if pis:
			raise DownstreamCancellationBlockedError(
				f"Cannot cancel Purchase Receipt '{docname}': submitted Purchase Invoice '{pis[0]}' exists."
			)

	elif doctype == "Purchase Invoice":
		pes = frappe.db.sql(
			"SELECT DISTINCT parent FROM `tabPayment Entry Reference` WHERE reference_doctype = 'Purchase Invoice' AND reference_name = %s AND docstatus = 1",
			(docname,),
			pluck="parent",
		)
		if pes:
			raise DownstreamCancellationBlockedError(
				f"Cannot cancel Purchase Invoice '{docname}': submitted Payment Entry '{pes[0]}' exists."
			)

	doc.cancel()
	return doc


def get_three_way_traceability(
	po_name: Optional[str] = None,
	pr_name: Optional[str] = None,
	pi_name: Optional[str] = None,
) -> Dict[str, Any]:
	"""
	Audit helper providing three-way traceability across PO, PR, and PI:
	- Ordered vs Received vs Billed quantities
	- Rates and amounts
	- Document reference chains
	"""
	# Resolve PO if PR or PI provided
	if not po_name:
		if pr_name:
			po_name = frappe.db.get_value("Purchase Receipt Item", {"parent": pr_name}, "purchase_order")
		elif pi_name:
			po_name = frappe.db.get_value("Purchase Invoice Item", {"parent": pi_name}, "purchase_order")

	if not po_name:
		return {"error": "Could not resolve Purchase Order for traceability."}

	po_doc = frappe.get_doc("Purchase Order", po_name)

	# Fetch linked Purchase Receipts
	pr_names = frappe.db.sql(
		"""
		SELECT DISTINCT parent FROM `tabPurchase Receipt Item`
		WHERE purchase_order = %s AND docstatus = 1
		""",
		(po_name,),
		pluck="parent",
	)

	# Fetch linked Purchase Invoices
	pi_names = frappe.db.sql(
		"""
		SELECT DISTINCT parent FROM `tabPurchase Invoice Item`
		WHERE purchase_order = %s AND docstatus = 1
		""",
		(po_name,),
		pluck="parent",
	)

	items_summary = []
	for po_item in po_doc.items:
		items_summary.append({
			"item_code": po_item.item_code,
			"ordered_qty": flt(po_item.qty),
			"received_qty": flt(po_item.received_qty),
			"billed_qty": flt(po_item.billed_amt) / flt(po_item.rate) if flt(po_item.rate) else 0.0,
			"po_rate": flt(po_item.rate),
			"po_amount": flt(po_item.amount),
		})

	return {
		"purchase_order": po_name,
		"supplier": po_doc.supplier,
		"company": po_doc.company,
		"docstatus": po_doc.docstatus,
		"linked_purchase_receipts": pr_names,
		"linked_purchase_invoices": pi_names,
		"items": items_summary,
	}
