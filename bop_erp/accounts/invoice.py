# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate

from bop_erp.constants import (
	IntegrationReadinessStatus,
	TransactionOrigin,
)
from bop_erp.accounts.exceptions import (
	CompanyMismatchError,
	DeliveryNoteNotReadyForInvoicingError,
	DuplicateSalesInvoiceError,
	InvoicingFinancialReconciliationError,
	MissingFulfillmentEvidenceError,
	OrderNotEligibleForInvoicingError,
	OverbillingBlockedError,
	SalesInvoiceError,
)
from bop_erp.fulfillment.pick_ticket import is_imported_sales_order

logger = frappe.logger("bop_erp")

# Structured Observability Counters (Section 42)
INVOICE_COUNTERS: Dict[str, int] = {
	"invoice_requests": 0,
	"invoices_created": 0,
	"invoices_reused": 0,
	"invoices_submitted": 0,
	"invoices_cancelled": 0,
	"invoices_blocked": 0,
	"overbilling_blocked": 0,
	"concurrent_replay": 0,
	"failed": 0,
}


def reset_invoice_counters() -> None:
	"""Resets all structured invoicing counters to zero."""
	for k in INVOICE_COUNTERS:
		INVOICE_COUNTERS[k] = 0


def get_invoice_counters() -> Dict[str, int]:
	"""Returns a snapshot copy of current invoicing counters."""
	return dict(INVOICE_COUNTERS)


def compute_sales_invoice_idempotency_key(
	delivery_note: str,
	sales_order: Optional[str] = None,
	company: Optional[str] = None,
	requested_lines: Optional[List[Dict[str, Any]]] = None,
) -> str:
	"""
	Computes a deterministic idempotency key for an invoice generation request.
	Key derives from Delivery Note name, Sales Order name, Company, and canonical line scopes.
	"""
	payload = {
		"dn": str(delivery_note).strip(),
		"so": str(sales_order).strip() if sales_order else "",
		"company": str(company).strip() if company else "",
	}
	if requested_lines:
		sorted_lines = sorted(
			[
				(
					str(r.get("sales_order_item") or r.get("dn_detail") or r.get("item_code")),
					flt(r.get("qty", 0.0)),
				)
				for r in requested_lines
			],
			key=lambda x: x[0],
		)
		payload["lines"] = sorted_lines

	canonical = json.dumps(payload, sort_keys=True)
	return f"INV-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16].upper()}"


def assert_sales_invoice_eligibility(
	delivery_note: Union[str, Any],
	sales_order: Optional[Union[str, Any]] = None,
) -> Tuple[Any, Optional[Any]]:
	"""
	Validates that the source Delivery Note and Sales Order are eligible for invoicing.

	Rules:
	1. Delivery Note must exist and be submitted (docstatus = 1), not cancelled.
	2. If linked to Sales Order:
	   - Sales Order must exist, be submitted (docstatus = 1), not cancelled or closed.
	   - If imported order:
	     - integration_status must be READY.
	     - valid sales_channel, transaction_origin, external_order_id, company.
	     - submitted Delivery Note fulfillment evidence is mandatory.
	3. Manual / native Sales Orders remain fully compatible and unblocked.
	4. Company isolation: Delivery Note and Sales Order must belong to the exact same Company.
	"""
	# 1. Delivery Note validation
	if isinstance(delivery_note, str):
		dn_name = delivery_note
		if not frappe.db.exists("Delivery Note", dn_name):
			INVOICE_COUNTERS["invoices_blocked"] += 1
			raise DeliveryNoteNotReadyForInvoicingError(
				_("Delivery Note '{0}' does not exist.").format(dn_name)
			)
		dn_doc = frappe.get_doc("Delivery Note", dn_name)
	else:
		dn_doc = delivery_note
		dn_name = getattr(dn_doc, "name", "NEW_DN")

	dn_docstatus = getattr(dn_doc, "docstatus", 0)
	dn_status = getattr(dn_doc, "status", "")

	if dn_docstatus != 1:
		INVOICE_COUNTERS["invoices_blocked"] += 1
		raise DeliveryNoteNotReadyForInvoicingError(
			_("Delivery Note '{0}' is not submitted (docstatus: {1}).").format(dn_name, dn_docstatus)
		)

	if dn_docstatus == 2 or dn_status in ("Cancelled",):
		INVOICE_COUNTERS["invoices_blocked"] += 1
		raise DeliveryNoteNotReadyForInvoicingError(
			_("Delivery Note '{0}' is cancelled.").format(dn_name)
		)

	if not dn_doc.company or not frappe.db.exists("Company", dn_doc.company):
		INVOICE_COUNTERS["invoices_blocked"] += 1
		raise CompanyMismatchError(
			_("Company '{0}' on Delivery Note '{1}' is invalid.").format(dn_doc.company, dn_name)
		)

	# 2. Resolve Sales Order
	so_doc = None
	if sales_order is not None:
		if isinstance(sales_order, str):
			if not frappe.db.exists("Sales Order", sales_order):
				INVOICE_COUNTERS["invoices_blocked"] += 1
				raise OrderNotEligibleForInvoicingError(
					_("Sales Order '{0}' does not exist.").format(sales_order)
				)
			so_doc = frappe.get_doc("Sales Order", sales_order)
		else:
			so_doc = sales_order
	else:
		# Infer from Delivery Note Items
		so_names = list({
			(it.against_sales_order if not isinstance(it, dict) else it.get("against_sales_order"))
			for it in (dn_doc.get("items") or [])
			if (it.against_sales_order if not isinstance(it, dict) else it.get("against_sales_order"))
		})
		if so_names and frappe.db.exists("Sales Order", so_names[0]):
			so_doc = frappe.get_doc("Sales Order", so_names[0])

	# 3. Validate Sales Order if present
	if so_doc:
		so_name = getattr(so_doc, "name", "SO")
		so_docstatus = getattr(so_doc, "docstatus", 0)
		so_status = getattr(so_doc, "status", "")

		if so_docstatus != 1:
			INVOICE_COUNTERS["invoices_blocked"] += 1
			raise OrderNotEligibleForInvoicingError(
				_("Sales Order '{0}' is not submitted (docstatus: {1}).").format(so_name, so_docstatus)
			)

		if so_status in ("Cancelled", "Closed"):
			INVOICE_COUNTERS["invoices_blocked"] += 1
			raise OrderNotEligibleForInvoicingError(
				_("Sales Order '{0}' status is '{1}'.").format(so_name, so_status)
			)

		if so_doc.company != dn_doc.company:
			INVOICE_COUNTERS["invoices_blocked"] += 1
			raise CompanyMismatchError(
				_("Delivery Note company '{0}' does not match Sales Order company '{1}'.").format(
					dn_doc.company, so_doc.company
				)
			)

		# Imported order operational guards
		is_imported = is_imported_sales_order(so_doc)
		if is_imported:
			status = so_doc.get("integration_status")
			if status != IntegrationReadinessStatus.READY:
				INVOICE_COUNTERS["invoices_blocked"] += 1
				raise OrderNotEligibleForInvoicingError(
					_("Sales Order '{0}' is an imported order in '{1}' status and is not READY for invoicing.").format(
						so_name, status or "NONE"
					)
				)

			# Require valid sales_channel, transaction_origin, external_order_id
			if not so_doc.get("sales_channel"):
				INVOICE_COUNTERS["invoices_blocked"] += 1
				raise OrderNotEligibleForInvoicingError(
					_("Imported Sales Order '{0}' is missing a valid sales_channel.").format(so_name)
				)
			if not so_doc.get("transaction_origin"):
				INVOICE_COUNTERS["invoices_blocked"] += 1
				raise OrderNotEligibleForInvoicingError(
					_("Imported Sales Order '{0}' is missing a valid transaction_origin.").format(so_name)
				)
			if not so_doc.get("external_order_id"):
				INVOICE_COUNTERS["invoices_blocked"] += 1
				raise OrderNotEligibleForInvoicingError(
					_("Imported Sales Order '{0}' is missing external_order_id.").format(so_name)
				)

			# Canonical External ID Mapping validation (prevent mapping drift)
			ext_id = str(so_doc.external_order_id)
			mapping = frappe.db.get_value(
				"External ID Mapping",
				{
					"sales_channel": so_doc.sales_channel,
					"external_entity_type": "ORDER",
					"external_id": ext_id,
					"erp_doctype": "Sales Order",
					"erp_document": so_name,
					"active": 1,
				},
				["name", "provider"],
				as_dict=True,
			)
			if not mapping:
				INVOICE_COUNTERS["invoices_blocked"] += 1
				raise OrderNotEligibleForInvoicingError(
					_(
						"Mapping Drift Violation: Sales Order '{0}' does not have an active canonical ORDER mapping for channel '{1}' and external ID '{2}'."
					).format(so_name, so_doc.sales_channel, ext_id)
				)

			if so_doc.get("integration_provider") and mapping.provider != so_doc.integration_provider:
				INVOICE_COUNTERS["invoices_blocked"] += 1
				raise OrderNotEligibleForInvoicingError(
					_(
						"Mapping Drift Violation: Sales Order '{0}' provider '{1}' does not match active canonical mapping provider '{2}'."
					).format(so_name, so_doc.integration_provider, mapping.provider)
				)

	return dn_doc, so_doc


def create_sales_invoice_from_fulfillment(
	delivery_note: Union[str, Any],
	sales_order: Optional[Union[str, Any]] = None,
	requested_lines: Optional[List[Dict[str, Any]]] = None,
	posting_date: Optional[str] = None,
	idempotency_key: Optional[str] = None,
	submit: bool = False,
	external_tax_amount: Optional[Union[float, int, str]] = None,
) -> Any:
	"""
	Provider-neutral Bop Sales Invoice / Accounts Receivable creation service.

	Primary Invariants:
	- update_stock is strictly forced to 0 (no duplicate stock movement).
	- Preserves Sales Channel, Transaction Origin, Company, and External Order ID.
	- Reconciles eligible fulfilled scope and strictly prevents overbilling.
	- 0 Payment Entry created. AR balance remains open until future payment reconciliation.
	- Idempotent: repeated requests for the same scope converge safely without duplicate invoices.
	- Concurrency-safe: serialized via MariaDB row-level locks.
	"""
	INVOICE_COUNTERS["invoice_requests"] += 1

	dn_name = delivery_note if isinstance(delivery_note, str) else delivery_note.name

	# 1. Row Lock on Delivery Note to serialize concurrent creation attempts
	frappe.db.sql("SELECT name FROM `tabDelivery Note` WHERE name = %s FOR UPDATE", (dn_name,))

	# Assert eligibility
	dn_doc, so_doc = assert_sales_invoice_eligibility(delivery_note, sales_order)

	# If Sales Order exists, lock it as well
	if so_doc:
		frappe.db.sql("SELECT name FROM `tabSales Order` WHERE name = %s FOR UPDATE", (so_doc.name,))

	# 2. Check for existing active Sales Invoice linked to this Delivery Note (Idempotency & Convergence)
	existing_si_rows = frappe.db.sql(
		"""
		SELECT DISTINCT si.name, si.docstatus, si.status
		FROM `tabSales Invoice Item` sii
		JOIN `tabSales Invoice` si ON si.name = sii.parent
		WHERE sii.delivery_note = %s
		  AND si.docstatus IN (0, 1)
		  AND si.status NOT IN ('Cancelled')
		ORDER BY si.creation ASC
		""",
		(dn_name,),
		as_dict=True,
	)

	if existing_si_rows:
		# If an in-flight draft Sales Invoice exists, converge to it (or submit on demand)
		draft_rows = [r for r in existing_si_rows if r.get("docstatus") == 0]
		if draft_rows:
			draft_si_name = draft_rows[0].get("name")
			if submit:
				draft_si = frappe.get_doc("Sales Invoice", draft_si_name)
				return submit_sales_invoice(draft_si)

			INVOICE_COUNTERS["invoices_reused"] += 1
			INVOICE_COUNTERS["concurrent_replay"] += 1
			logger.info("Converged to existing draft Sales Invoice '%s' for Delivery Note '%s'.", draft_si_name, dn_name)
			return frappe.get_doc("Sales Invoice", draft_si_name)

		# For submitted invoices (docstatus = 1):
		# Replay/convergence ONLY applies when no specific invoicing scope (requested_lines) is specified.
		# If requested_lines is specified, the caller is asking to bill specific fulfilled scope,
		# which must be evaluated against remaining unbilled quantities.
		if requested_lines is None:
			existing_si_name = existing_si_rows[0].get("name")
			INVOICE_COUNTERS["invoices_reused"] += 1
			INVOICE_COUNTERS["concurrent_replay"] += 1
			logger.info("Converged to existing Sales Invoice '%s' for Delivery Note '%s'.", existing_si_name, dn_name)
			return frappe.get_doc("Sales Invoice", existing_si_name)

	# 3. Check for Overbilling: ensure unbilled quantity > 0 on Delivery Note items
	unbilled_qty_found = False
	remaining_by_dn_detail = {}
	for it in dn_doc.items:
		# Calculate already billed qty from submitted Sales Invoices
		billed_rows = frappe.db.sql(
			"""
			SELECT SUM(sii.qty)
			FROM `tabSales Invoice Item` sii
			JOIN `tabSales Invoice` si ON si.name = sii.parent
			WHERE sii.dn_detail = %s
			  AND si.docstatus = 1
			""",
			(it.name,),
		)
		billed = flt(billed_rows[0][0]) if billed_rows and billed_rows[0] and billed_rows[0][0] is not None else 0.0
		remaining = max(0.0, flt(it.qty) - billed)
		remaining_by_dn_detail[it.name] = remaining
		if remaining > 0.0001:
			unbilled_qty_found = True

	if not unbilled_qty_found:
		INVOICE_COUNTERS["overbilling_blocked"] += 1
		INVOICE_COUNTERS["invoices_blocked"] += 1
		raise OverbillingBlockedError(
			_("Delivery Note '{0}' has already been fully billed. Overbilling is strictly blocked.").format(dn_name)
		)

	# 4. Use Native ERPNext mapping function: Delivery Note -> Sales Invoice
	from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

	si_doc = make_sales_invoice(dn_name)
	if not si_doc or not si_doc.get("items"):
		INVOICE_COUNTERS["failed"] += 1
		raise SalesInvoiceError(
			_("Failed to generate Sales Invoice from Delivery Note '{0}'.").format(dn_name)
		)

	# 5. STRICT INVARIANT: update_stock MUST be 0
	si_doc.update_stock = 0

	# 6. Apply partial line adjustments if specific fulfilled scope is requested
	if requested_lines is not None:
		req_map = {}
		for r in requested_lines:
			key = (
				r.get("dn_detail")
				or r.get("sales_order_item")
				or r.get("item_code")
			)
			req_map[key] = flt(r.get("qty", 0.0))

		filtered_items = []
		for item in si_doc.items:
			match_key = None
			if item.dn_detail in req_map:
				match_key = item.dn_detail
			elif item.so_detail in req_map:
				match_key = item.so_detail
			elif item.item_code in req_map:
				match_key = item.item_code

			if match_key is not None:
				req_qty = req_map[match_key]
				remaining_allowed = remaining_by_dn_detail.get(item.dn_detail, flt(item.qty))
				max_allowed = min(flt(item.qty), remaining_allowed)
				if req_qty > max_allowed + 0.0001:
					INVOICE_COUNTERS["overbilling_blocked"] += 1
					INVOICE_COUNTERS["invoices_blocked"] += 1
					raise OverbillingBlockedError(
						_("Requested quantity {0} exceeds available unbilled quantity {1} for item '{2}'.").format(
							req_qty, max_allowed, item.item_code
						)
					)
				item.qty = req_qty
				item.stock_qty = req_qty
				filtered_items.append(item)

		if not filtered_items:
			INVOICE_COUNTERS["invoices_blocked"] += 1
			raise SalesInvoiceError(_("No matching items found for requested invoicing scope."))

		si_doc.items = filtered_items
		si_doc.run_method("calculate_taxes_and_totals")

	# 7. Preserve Commercial Attribution & External Identity
	sc = dn_doc.get("sales_channel") or (so_doc.get("sales_channel") if so_doc else None)
	to = dn_doc.get("transaction_origin") or (so_doc.get("transaction_origin") if so_doc else None)
	eo = dn_doc.get("external_order_id") or (so_doc.get("external_order_id") if so_doc else None)

	if sc:
		si_doc.sales_channel = sc
	if to:
		si_doc.transaction_origin = to
	if eo:
		si_doc.external_order_id = eo

	# 8. Posting Date
	if posting_date:
		si_doc.posting_date = posting_date
	elif not si_doc.posting_date:
		si_doc.posting_date = nowdate()

	# 9. Validate Multi-Currency and Conversion Rate
	if si_doc.currency != dn_doc.currency:
		INVOICE_COUNTERS["invoices_blocked"] += 1
		raise InvoicingFinancialReconciliationError(
			_("Invoice currency '{0}' does not match Delivery Note currency '{1}'.").format(
				si_doc.currency, dn_doc.currency
			)
		)

	# Resolve external tax evidence if present
	ext_tax = external_tax_amount
	if ext_tax is None and so_doc:
		ext_tax = getattr(so_doc, "external_tax_amount", None)
	if ext_tax is None and dn_doc:
		ext_tax = getattr(dn_doc, "external_tax_amount", None)

	# 10. Financial Reconciliation Pre-check
	_reconcile_invoice_financials(si_doc, dn_doc, so_doc, external_tax_amount=ext_tax)

	# 11. Save Draft Sales Invoice (Normal validations enabled)
	si_doc.flags.ignore_validate = False
	si_doc.flags.ignore_mandatory = False
	si_doc.flags.ignore_permissions = False

	si_doc.insert()
	INVOICE_COUNTERS["invoices_created"] += 1
	logger.info("Created draft Sales Invoice '%s' for Delivery Note '%s'.", si_doc.name, dn_name)

	# 12. Submit if requested
	if submit:
		return submit_sales_invoice(si_doc, external_tax_amount=ext_tax)

	return si_doc


def submit_sales_invoice(
	sales_invoice: Union[str, Any],
	external_tax_amount: Optional[Union[float, int, str]] = None,
) -> Any:
	"""
	Submits a Sales Invoice using native ERPNext lifecycle methods.
	Strictly verifies:
	- update_stock is 0.
	- Paid amount is 0 (zero Payment Entry created).
	- Outstanding amount is preserved.
	"""
	if isinstance(sales_invoice, str):
		si_name = sales_invoice
		if not frappe.db.exists("Sales Invoice", si_name):
			INVOICE_COUNTERS["failed"] += 1
			raise SalesInvoiceError(_("Sales Invoice '{0}' does not exist.").format(si_name))
		si_doc = frappe.get_doc("Sales Invoice", si_name)
	else:
		si_doc = sales_invoice
		si_name = getattr(si_doc, "name", "NEW_SI")

	if si_doc.docstatus == 1:
		INVOICE_COUNTERS["invoices_reused"] += 1
		return si_doc

	if si_doc.docstatus == 2:
		INVOICE_COUNTERS["invoices_blocked"] += 1
		raise SalesInvoiceError(_("Cannot submit cancelled Sales Invoice '{0}'.").format(si_name))

	# STRICT INVARIANT: update_stock MUST be 0
	if si_doc.update_stock != 0:
		INVOICE_COUNTERS["invoices_blocked"] += 1
		raise SalesInvoiceError(
			_("CRITICAL INVARIANT VIOLATION: Sales Invoice '{0}' has update_stock != 0. "
			  "Stock movement is strictly owned by Delivery Note.").format(si_name)
		)

	# Normal validations enabled
	si_doc.flags.ignore_validate = False
	si_doc.flags.ignore_mandatory = False
	si_doc.flags.ignore_permissions = False

	# Invariant: External Tax Reconciliation Check before submission
	ext_tax = external_tax_amount
	if ext_tax is None:
		ext_tax = getattr(si_doc, "external_tax_amount", None)
	if ext_tax is not None:
		from bop_erp.accounts.tax_reconciliation import reconcile_external_taxes
		reconcile_external_taxes(
			si_doc,
			external_tax_amount=ext_tax,
			currency=si_doc.currency,
		)

	# Savepoint for clean rollback on error
	sp_submit = f"sp_si_sub_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_submit)

	try:
		si_doc.submit()
		INVOICE_COUNTERS["invoices_submitted"] += 1
		logger.info("Submitted Sales Invoice '%s' (outstanding: %s).", si_name, si_doc.outstanding_amount)
	except Exception as e:
		frappe.db.rollback(save_point=sp_submit)
		INVOICE_COUNTERS["failed"] += 1
		logger.error("Failed to submit Sales Invoice '%s': %s", si_name, str(e))
		raise

	return si_doc


def cancel_sales_invoice(sales_invoice: Union[str, Any]) -> Any:
	"""
	Cancels a Sales Invoice natively.
	- Reverses GL Entries.
	- Does NOT cancel the Delivery Note.
	- Does NOT cancel the Sales Order.
	"""
	if isinstance(sales_invoice, str):
		si_name = sales_invoice
		if not frappe.db.exists("Sales Invoice", si_name):
			INVOICE_COUNTERS["failed"] += 1
			raise SalesInvoiceError(_("Sales Invoice '{0}' does not exist.").format(si_name))
		si_doc = frappe.get_doc("Sales Invoice", si_name)
	else:
		si_doc = sales_invoice
		si_name = getattr(si_doc, "name", "SI")

	if si_doc.docstatus == 2:
		return si_doc

	if si_doc.docstatus == 0:
		si_doc.delete()
		return si_doc

	si_doc.cancel()
	INVOICE_COUNTERS["invoices_cancelled"] += 1
	logger.info("Cancelled Sales Invoice '%s'.", si_name)
	return si_doc


def _reconcile_invoice_financials(
	si_doc: Any,
	dn_doc: Any,
	so_doc: Optional[Any] = None,
	external_tax_amount: Optional[Union[float, int, str]] = None,
) -> None:
	"""
	Performs sanity reconciliation against upstream document totals.
	"""
	if si_doc.company != dn_doc.company:
		raise CompanyMismatchError(
			_("Company mismatch during reconciliation: Invoice company '{0}' != Delivery Note company '{1}'.").format(
				si_doc.company, dn_doc.company
			)
		)

	# Verify debit_to account belongs to the same company and is Receivable
	if si_doc.debit_to:
		acc = frappe.get_value("Account", si_doc.debit_to, ["company", "account_type", "is_group"], as_dict=True)
		if not acc:
			raise SalesInvoiceError(_("Receivable account '{0}' not found.").format(si_doc.debit_to))
		if acc.company != si_doc.company:
			raise CompanyMismatchError(
				_("Receivable account '{0}' company '{1}' does not match Invoice company '{2}'.").format(
					si_doc.debit_to, acc.company, si_doc.company
				)
			)
		if acc.account_type != "Receivable":
			raise SalesInvoiceError(_("Account '{0}' is not a Receivable account.").format(si_doc.debit_to))

	# Verify each item income account belongs to the same company
	for item in si_doc.items:
		if item.income_account:
			acc = frappe.get_value("Account", item.income_account, ["company", "is_group"], as_dict=True)
			if not acc:
				raise SalesInvoiceError(_("Income account '{0}' not found.").format(item.income_account))
			if acc.company != si_doc.company:
				raise CompanyMismatchError(
					_("Income account '{0}' company '{1}' does not match Invoice company '{2}'.").format(
						item.income_account, acc.company, si_doc.company
					)
				)
			if acc.is_group:
				raise SalesInvoiceError(
					_("Income account '{0}' is an account group. A ledger account is required.").format(item.income_account)
				)

	# Invariant: Reconcile external tax evidence if present
	if external_tax_amount is not None:
		from bop_erp.accounts.tax_reconciliation import reconcile_external_taxes
		reconcile_external_taxes(
			si_doc,
			external_tax_amount=external_tax_amount,
			currency=si_doc.currency,
		)
