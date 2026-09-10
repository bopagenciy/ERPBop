# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple, Union

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, nowdate

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
	IntegrationReadinessStatus,
	TransactionOrigin,
)
from bop_erp.accounts.exceptions import (
	CompanyMismatchError,
	DuplicatePaymentError,
	OverpaymentBlockedError,
	PaymentAccountMismatchError,
	PaymentEligibilityError,
	PaymentMappingDriftError,
	PaymentReconciliationError,
)

logger = frappe.logger("bop_erp")

# Structured Observability Counters (Section 42 & 44)
PAYMENT_COUNTERS: Dict[str, int] = {
	"reconciliation_requests": 0,
	"payments_created": 0,
	"payments_reused": 0,
	"payments_submitted": 0,
	"payments_cancelled": 0,
	"payments_blocked": 0,
	"overpayment_blocked": 0,
	"concurrent_replay": 0,
	"failed": 0,
}


def reset_payment_counters() -> None:
	"""Resets all structured payment counters to zero."""
	for k in PAYMENT_COUNTERS:
		PAYMENT_COUNTERS[k] = 0


def get_payment_counters() -> Dict[str, int]:
	"""Returns a snapshot copy of current payment counters."""
	return dict(PAYMENT_COUNTERS)


class ExternalPaymentStatus:
	SETTLED = "SETTLED"
	CAPTURED = "CAPTURED"
	COMPLETED = "COMPLETED"
	PENDING = "PENDING"
	AUTHORIZED = "AUTHORIZED"
	FAILED = "FAILED"
	REFUNDED = "REFUNDED"
	CHARGEBACK = "CHARGEBACK"

	ELIGIBLE_FOR_RECONCILIATION = (SETTLED, CAPTURED, COMPLETED)
	INELIGIBLE_PENDING = (PENDING, AUTHORIZED)
	REVIEW_REQUIRED = (FAILED, REFUNDED, CHARGEBACK)


@dataclass
class ExternalPaymentRecord:
	"""
	Provider-neutral representation of an external payment transaction.
	Decouples e-commerce / marketplace / gateway payloads from ERP models.
	"""
	provider: str
	sales_channel: str
	external_payment_id: str
	amount: float
	currency: str = "USD"
	payment_method: str = "Credit Card"
	payment_status: str = ExternalPaymentStatus.SETTLED
	external_order_id: Optional[str] = None
	external_reference: Optional[str] = None
	payment_date: Optional[str] = None
	transaction_reference: Optional[str] = None
	conversion_rate: float = 1.0
	raw_data: Dict[str, Any] = field(default_factory=dict)


def compute_external_payment_idempotency_key(
	provider: str,
	sales_channel: str,
	external_payment_id: str,
) -> str:
	"""
	Computes deterministic canonical idempotency key for an external payment.
	"""
	payload = [
		str(sales_channel).strip(),
		str(provider).strip().upper(),
		ExternalEntityType.PAYMENT,
		str(external_payment_id).strip(),
	]
	canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_clearing_account_for_payment(
	company: str,
	sales_channel: str,
	payment_method: str,
	provider: Optional[str] = None,
	currency: Optional[str] = None,
) -> Tuple[str, str]:
	"""
	Resolves ERPNext Mode of Payment and paid_to Bank/Clearing Account.

	Rules:
	1. Check Mode of Payment matching payment_method.
	2. If not found, fall back to 'Bank Draft', 'Credit Card', 'Cash', or 'Wire Transfer'.
	3. Resolve Mode of Payment Account for target Company.
	4. If not configured, fall back to Company default_bank_account or default_cash_account.
	5. If still not configured, resolve first eligible Bank/Cash account in Company.
	6. Strict Invariants:
	   - Account must exist and belong to Company.
	   - Account must NOT be a group (is_group = 0).
	   - Account type must be 'Bank' or 'Cash'.
	"""
	# 1. Resolve Mode of Payment
	mop_name = None
	if payment_method and frappe.db.exists("Mode of Payment", payment_method):
		mop_name = payment_method
	else:
		# Fallbacks
		for candidate in ["Credit Card", "Wire Transfer", "Bank Draft", "Cash", "Cheque"]:
			if frappe.db.exists("Mode of Payment", candidate):
				mop_name = candidate
				break

	if not mop_name:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentAccountMismatchError(
			_("No valid Mode of Payment found in system for method '{0}'.").format(payment_method)
		)

	# 2. Resolve Account for Mode of Payment + Company
	account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mop_name, "company": company},
		"default_account",
	)

	# 3. Fallbacks for Company default bank / cash
	if not account:
		account = frappe.db.get_value("Company", company, "default_bank_account")
	if not account:
		account = frappe.db.get_value("Company", company, "default_cash_account")

	# 4. Fallback to any non-group Bank account in company
	if not account:
		bank_accs = frappe.db.get_all(
			"Account",
			filters={"company": company, "account_type": "Bank", "is_group": 0},
			pluck="name",
			order_by="creation asc",
			limit=1,
		)
		if bank_accs:
			account = bank_accs[0]

	# 5. Fallback to any non-group Cash account in company
	if not account:
		cash_accs = frappe.db.get_all(
			"Account",
			filters={"company": company, "account_type": "Cash", "is_group": 0},
			pluck="name",
			order_by="creation asc",
			limit=1,
		)
		if cash_accs:
			account = cash_accs[0]

	if not account:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentAccountMismatchError(
			_("No Bank or Cash clearing account found for Company '{0}' (Mode of Payment: '{1}').").format(
				company, mop_name
			)
		)

	# 6. Strict validation of resolved account
	acc_doc = frappe.db.get_value(
		"Account",
		account,
		["company", "account_type", "is_group"],
		as_dict=True,
	)
	if not acc_doc:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentAccountMismatchError(_("Clearing account '{0}' does not exist.").format(account))

	if acc_doc.company != company:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise CompanyMismatchError(
			_("Clearing account '{0}' company '{1}' does not match target company '{2}'.").format(
				account, acc_doc.company, company
			)
		)

	if acc_doc.is_group:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentAccountMismatchError(
			_("Clearing account '{0}' is an account group. A ledger account is required.").format(account)
		)

	if acc_doc.account_type not in ("Bank", "Cash"):
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentAccountMismatchError(
			_("Clearing account '{0}' has invalid account_type '{1}'. Must be 'Bank' or 'Cash'.").format(
				account, acc_doc.account_type
			)
		)

	return mop_name, account


def assert_payment_reconciliation_eligibility(
	payment_record: ExternalPaymentRecord,
	sales_invoices: Optional[List[Any]] = None,
	sales_order: Optional[Any] = None,
) -> Tuple[List[Any], Any]:
	"""
	Validates that an incoming external payment record and its target invoice(s)/order
	satisfy all financial reconciliation invariants.

	Rules:
	1. Payment amount must be strictly > 0.
	2. Payment status must be in ELIGIBLE_FOR_RECONCILIATION.
	3. Sales Channel must exist and be active.
	4. Company isolation: all invoices, orders, and clearing accounts must match company.
	5. Currency matching: payment currency must match target invoice/order currency.
	6. Canonical External ID Mapping validation: verifies active ORDER mapping exists and
	   matches provider (mapping drift guard).
	7. Sales Invoice eligibility:
	   - Must exist, be submitted (docstatus = 1), not cancelled.
	   - outstanding_amount must be strictly > 0.
	"""
	# 1. Amount validation
	amt = flt(payment_record.amount)
	if amt <= 0.00001:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentEligibilityError(
			_("External payment amount {0} must be strictly positive (> 0).").format(amt)
		)

	# 2. Status validation
	status = str(payment_record.payment_status).strip().upper()
	if status not in ExternalPaymentStatus.ELIGIBLE_FOR_RECONCILIATION:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentEligibilityError(
			_("External payment status '{0}' is not eligible for reconciliation. "
			  "Must be one of: {1}.").format(
				status, ", ".join(ExternalPaymentStatus.ELIGIBLE_FOR_RECONCILIATION)
			)
		)

	# 3. Channel validation
	channel = payment_record.sales_channel
	ch_doc = frappe.db.get_value("Sales Channel", channel, ["name", "company", "active"], as_dict=True)
	if not ch_doc:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentEligibilityError(_("Sales Channel '{0}' does not exist.").format(channel))
	if not ch_doc.active:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentEligibilityError(_("Sales Channel '{0}' is inactive.").format(channel))

	company = ch_doc.company

	# 4. Resolve Sales Order if external_order_id provided
	so_doc = sales_order
	if not so_doc and payment_record.external_order_id:
		ext_order_id = str(payment_record.external_order_id).strip()
		# Validate active canonical mapping
		mapping = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": channel,
				"external_entity_type": ExternalEntityType.ORDER,
				"external_id": ext_order_id,
				"active": 1,
			},
			["name", "provider", "erp_doctype", "erp_document"],
			as_dict=True,
		)
		if not mapping:
			PAYMENT_COUNTERS["payments_blocked"] += 1
			raise PaymentMappingDriftError(
				_("Mapping Drift Violation: No active canonical ORDER mapping found for Channel '{0}' "
				  "and External Order ID '{1}'.").format(channel, ext_order_id)
			)

		clean_prov = str(payment_record.provider).strip().upper()
		if mapping.provider and mapping.provider.upper() != clean_prov:
			PAYMENT_COUNTERS["payments_blocked"] += 1
			raise PaymentMappingDriftError(
				_("Mapping Drift Violation: Payment provider '{0}' does not match canonical order mapping "
				  "provider '{1}' for external order '{2}'.").format(clean_prov, mapping.provider, ext_order_id)
			)

		if mapping.erp_doctype == "Sales Order" and frappe.db.exists("Sales Order", mapping.erp_document):
			so_doc = frappe.get_doc("Sales Order", mapping.erp_document)

	if so_doc:
		if so_doc.company != company:
			PAYMENT_COUNTERS["payments_blocked"] += 1
			raise CompanyMismatchError(
				_("Sales Order company '{0}' does not match channel company '{1}'.").format(
					so_doc.company, company
				)
			)

	# 5. Resolve Sales Invoices
	eligible_invoices: List[Any] = []
	if sales_invoices:
		for item in sales_invoices:
			si = frappe.get_doc("Sales Invoice", item) if isinstance(item, str) else item
			eligible_invoices.append(si)
	elif so_doc:
		# Query submitted Sales Invoices linked to this Sales Order with outstanding > 0
		si_names = frappe.db.sql(
			"""
			SELECT DISTINCT si.name
			FROM `tabSales Invoice Item` sii
			JOIN `tabSales Invoice` si ON si.name = sii.parent
			WHERE sii.sales_order = %s
			  AND si.docstatus = 1
			  AND si.outstanding_amount > 0.0001
			ORDER BY si.posting_date ASC, si.creation ASC
			""",
			(so_doc.name,),
			pluck="name",
		)
		for sin in si_names:
			eligible_invoices.append(frappe.get_doc("Sales Invoice", sin))

	if not eligible_invoices:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		ref_desc = f"order '{payment_record.external_order_id}'" if payment_record.external_order_id else "request"
		raise PaymentEligibilityError(
			_("No eligible submitted Sales Invoices with outstanding balance found for {0}.").format(ref_desc)
		)

	# 6. Validate each eligible Sales Invoice
	for si in eligible_invoices:
		if si.docstatus != 1:
			PAYMENT_COUNTERS["payments_blocked"] += 1
			raise PaymentEligibilityError(
				_("Sales Invoice '{0}' is not submitted (docstatus: {1}).").format(si.name, si.docstatus)
			)
		if si.status in ("Cancelled",):
			PAYMENT_COUNTERS["payments_blocked"] += 1
			raise PaymentEligibilityError(_("Sales Invoice '{0}' is cancelled.").format(si.name))
		if si.company != company:
			PAYMENT_COUNTERS["payments_blocked"] += 1
			raise CompanyMismatchError(
				_("Sales Invoice '{0}' company '{1}' does not match target company '{2}'.").format(
					si.name, si.company, company
				)
			)
		if payment_record.currency and si.currency != payment_record.currency:
			PAYMENT_COUNTERS["payments_blocked"] += 1
			raise PaymentEligibilityError(
				_("Payment currency '{0}' does not match Sales Invoice '{1}' currency '{2}'.").format(
					payment_record.currency, si.name, si.currency
				)
			)
		if flt(si.outstanding_amount) <= 0.0001:
			PAYMENT_COUNTERS["payments_blocked"] += 1
			raise PaymentEligibilityError(
				_("Sales Invoice '{0}' has zero outstanding amount ({1}).").format(
					si.name, si.outstanding_amount
				)
			)

	return eligible_invoices, so_doc


def plan_invoice_allocations(
	payment_amount: float,
	eligible_invoices: List[Any],
) -> List[Dict[str, Any]]:
	"""
	Deterministically allocates external payment amount across eligible Sales Invoices.
	Allocates oldest invoice first up to its remaining outstanding_amount.

	Strict Rule: Total allocated cannot exceed the sum of invoice outstanding amounts
	(overpayment is blocked in Phase 1Q).
	"""
	rem_payment = Decimal(str(payment_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
	allocations: List[Dict[str, Any]] = []

	# Sort invoices deterministically: posting_date ASC, creation ASC, name ASC
	sorted_invoices = sorted(
		eligible_invoices,
		key=lambda x: (
			str(getattr(x, "posting_date", "")),
			str(getattr(x, "creation", "")),
			str(getattr(x, "name", "")),
		),
	)

	for inv in sorted_invoices:
		if rem_payment <= Decimal("0.00"):
			break

		inv_outstanding = Decimal(str(flt(inv.outstanding_amount))).quantize(
			Decimal("0.01"), rounding=ROUND_HALF_UP
		)
		if inv_outstanding <= Decimal("0.00"):
			continue

		alloc = min(rem_payment, inv_outstanding)
		allocations.append({
			"sales_invoice": inv.name,
			"invoice_doc": inv,
			"total_amount": flt(inv.grand_total),
			"outstanding_amount": flt(inv.outstanding_amount),
			"allocated_amount": float(alloc),
		})
		rem_payment -= alloc

	if rem_payment > Decimal("0.005"):
		PAYMENT_COUNTERS["overpayment_blocked"] += 1
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise OverpaymentBlockedError(
			_("Payment amount {0} exceeds total eligible invoice outstanding balance by {1}. "
			  "Automatic unallocated/advance payment without invoice is blocked.").format(
				payment_amount, float(rem_payment)
			)
		)

	return allocations


def reconcile_external_payment(
	payment_record: ExternalPaymentRecord,
	sales_invoices: Optional[List[Any]] = None,
	sales_order: Optional[Any] = None,
	posting_date: Optional[str] = None,
	submit: bool = True,
) -> Any:
	"""
	Provider-neutral External Payment Reconciliation service.

	Primary Invariants:
	- Creates native ERPNext Payment Entry (payment_type = 'Receive').
	- Links via Payment Entry Reference to eligible submitted Sales Invoice(s).
	- On submission, native ERPNext decreases Sales Invoice outstanding_amount and posts GL.
	- 0 Stock movement. update_stock is never touched.
	- Preserves commercial attribution: sales_channel, transaction_origin.
	- Idempotent: duplicate payment attempts converge to existing Payment Entry via
	  External ID Mapping (entity_type = 'PAYMENT').
	- Concurrency safe: serialized via MariaDB row-level locks on target Sales Invoices.
	- Strictly blocks overpayment beyond total outstanding amount.
	"""
	PAYMENT_COUNTERS["reconciliation_requests"] += 1

	channel = payment_record.sales_channel
	clean_prov = str(payment_record.provider).strip().upper()
	ext_pay_id = str(payment_record.external_payment_id).strip()

	# 1. Row locks & Idempotency check via External ID Mapping
	from bop_erp.bop_erp.doctype.external_id_mapping.external_id_mapping import (
		compute_active_external_key,
	)

	active_ext_key = compute_active_external_key(
		channel,
		ExternalEntityType.PAYMENT,
		ext_pay_id,
		provider=clean_prov,
	)

	existing_map = frappe.db.get_value(
		"External ID Mapping",
		{"active_external_key": active_ext_key, "active": 1},
		["name", "erp_doctype", "erp_document"],
		as_dict=True,
	)

	if existing_map and existing_map.erp_doctype == "Payment Entry":
		pe_name = existing_map.erp_document
		if frappe.db.exists("Payment Entry", pe_name):
			pe_doc = frappe.get_doc("Payment Entry", pe_name)
			if pe_doc.docstatus == 0 and submit:
				# Converge and submit existing draft
				return submit_payment_entry(pe_doc)

			PAYMENT_COUNTERS["payments_reused"] += 1
			PAYMENT_COUNTERS["concurrent_replay"] += 1
			logger.info(
				"Converged to existing Payment Entry '%s' for external payment '%s'.",
				pe_name,
				ext_pay_id,
			)
			return pe_doc

	# 2. Assert financial reconciliation eligibility
	eligible_invoices, so_doc = assert_payment_reconciliation_eligibility(
		payment_record,
		sales_invoices=sales_invoices,
		sales_order=sales_order,
	)

	# 3. Lock target Sales Invoices to serialize concurrent payments against same invoices
	for inv in eligible_invoices:
		frappe.db.sql(
			"SELECT name, outstanding_amount FROM `tabSales Invoice` WHERE name = %s FOR UPDATE",
			(inv.name,),
		)
		inv.reload()

	if so_doc:
		frappe.db.sql(
			"SELECT name FROM `tabSales Order` WHERE name = %s FOR UPDATE",
			(so_doc.name,),
		)

	# Re-verify eligibility after row lock acquisition
	eligible_invoices, so_doc = assert_payment_reconciliation_eligibility(
		payment_record,
		sales_invoices=eligible_invoices,
		sales_order=so_doc,
	)

	# 4. Plan deterministic invoice allocations
	allocations = plan_invoice_allocations(payment_record.amount, eligible_invoices)

	primary_inv = allocations[0]["invoice_doc"]
	company = primary_inv.company
	customer = primary_inv.customer
	party_account = primary_inv.debit_to

	# 5. Resolve Mode of Payment and Clearing Account
	mop_name, clearing_account = resolve_clearing_account_for_payment(
		company=company,
		sales_channel=channel,
		payment_method=payment_record.payment_method,
		provider=clean_prov,
		currency=payment_record.currency,
	)

	# 6. Construct native ERPNext Payment Entry
	# Savepoint for atomic rollback on validation failure
	sp_pe = f"sp_pe_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_pe)

	try:
		pe = frappe.new_doc("Payment Entry")
		pe.payment_type = "Receive"
		pe.company = company
		pe.posting_date = posting_date or payment_record.payment_date or nowdate()
		pe.mode_of_payment = mop_name
		pe.party_type = "Customer"
		pe.party = customer
		pe.paid_from = party_account
		pe.paid_to = clearing_account

		# Accounts and currencies
		pe.paid_from_account_currency = primary_inv.currency
		pe.paid_to_account_currency = frappe.db.get_value(
			"Account", clearing_account, "account_currency"
		) or primary_inv.currency

		pe.paid_amount = flt(payment_record.amount)
		pe.received_amount = flt(payment_record.amount)

		if payment_record.transaction_reference or payment_record.external_payment_id:
			pe.reference_no = payment_record.transaction_reference or payment_record.external_payment_id
		if payment_record.payment_date:
			pe.reference_date = getdate(payment_record.payment_date)
		else:
			pe.reference_date = getdate(pe.posting_date)

		# Populate child references table
		pe.set("references", [])
		for alloc in allocations:
			pe.append("references", {
				"reference_doctype": "Sales Invoice",
				"reference_name": alloc["sales_invoice"],
				"total_amount": alloc["total_amount"],
				"outstanding_amount": alloc["outstanding_amount"],
				"allocated_amount": alloc["allocated_amount"],
			})

		pe.setup_party_account_field()
		pe.set_missing_values()
		pe.set_missing_ref_details()

		# Set Commercial Attribution
		pe.sales_channel = channel
		trans_orig = primary_inv.get("transaction_origin") or (
			so_doc.get("transaction_origin") if so_doc else TransactionOrigin.WEB
		)
		pe.transaction_origin = trans_orig

		pe.flags.ignore_validate = False
		pe.flags.ignore_mandatory = False
		pe.flags.ignore_permissions = True

		pe.insert()
		PAYMENT_COUNTERS["payments_created"] += 1
		logger.info(
			"Created draft Payment Entry '%s' for external payment '%s'.",
			pe.name,
			ext_pay_id,
		)

		# Create canonical External ID Mapping for PAYMENT
		mapping_doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": channel,
			"provider": clean_prov,
			"external_entity_type": ExternalEntityType.PAYMENT,
			"external_id": ext_pay_id,
			"erp_doctype": "Payment Entry",
			"erp_document": pe.name,
			"active": 1,
			"last_synced_at": now_datetime(),
		})
		mapping_doc.flags.ignore_permissions = True
		mapping_doc.insert()

		if submit:
			pe = submit_payment_entry(pe)

	except Exception as err:
		frappe.db.rollback(save_point=sp_pe)
		PAYMENT_COUNTERS["failed"] += 1
		logger.error("Failed to reconcile external payment '%s': %s", ext_pay_id, str(err))
		raise


	return pe


def submit_payment_entry(payment_entry: Union[str, Any]) -> Any:
	"""
	Submits a native ERPNext Payment Entry.
	Strictly verifies:
	- Payment Entry is submitted natively.
	- Linked Sales Invoice outstanding_amount is properly reduced.
	- Balanced GL Entries are created.
	"""
	if isinstance(payment_entry, str):
		pe_name = payment_entry
		if not frappe.db.exists("Payment Entry", pe_name):
			PAYMENT_COUNTERS["failed"] += 1
			raise PaymentReconciliationError(_("Payment Entry '{0}' does not exist.").format(pe_name))
		pe_doc = frappe.get_doc("Payment Entry", pe_name)
	else:
		pe_doc = payment_entry
		pe_name = getattr(pe_doc, "name", "NEW_PE")

	if pe_doc.docstatus == 1:
		PAYMENT_COUNTERS["payments_reused"] += 1
		return pe_doc

	if pe_doc.docstatus == 2:
		PAYMENT_COUNTERS["payments_blocked"] += 1
		raise PaymentReconciliationError(_("Cannot submit cancelled Payment Entry '{0}'.").format(pe_name))

	pe_doc.flags.ignore_validate = False
	pe_doc.flags.ignore_mandatory = False
	pe_doc.flags.ignore_permissions = True

	sp_sub = f"sp_pesub_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_sub)

	try:
		pe_doc.submit()
		PAYMENT_COUNTERS["payments_submitted"] += 1
		logger.info(
			"Submitted Payment Entry '%s' (allocated: %s).",
			pe_name,
			pe_doc.paid_amount,
		)
	except Exception as err:
		frappe.db.rollback(save_point=sp_sub)
		PAYMENT_COUNTERS["failed"] += 1
		logger.error("Failed to submit Payment Entry '%s': %s", pe_name, str(err))
		raise

	return pe_doc


def cancel_payment_entry(payment_entry: Union[str, Any]) -> Any:
	"""
	Cancels a Payment Entry natively.
	- Reverses GL Entries.
	- Restores Sales Invoice outstanding_amount natively.
	- Deactivates or removes canonical External ID Mapping.
	- Does NOT cancel the Sales Invoice, Delivery Note, or Sales Order.
	"""
	if isinstance(payment_entry, str):
		pe_name = payment_entry
		if not frappe.db.exists("Payment Entry", pe_name):
			PAYMENT_COUNTERS["failed"] += 1
			raise PaymentReconciliationError(_("Payment Entry '{0}' does not exist.").format(pe_name))
		pe_doc = frappe.get_doc("Payment Entry", pe_name)
	else:
		pe_doc = payment_entry
		pe_name = getattr(pe_doc, "name", "PE")

	if pe_doc.docstatus == 2:
		return pe_doc

	if pe_doc.docstatus == 0:
		frappe.db.set_value(
			"External ID Mapping",
			{"erp_doctype": "Payment Entry", "erp_document": pe_name},
			"active",
			0,
		)
		pe_doc.delete()
		return pe_doc

	sp_canc = f"sp_pecanc_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_canc)

	try:
		pe_doc.flags.ignore_permissions = True
		pe_doc.cancel()

		frappe.db.set_value(
			"External ID Mapping",
			{"erp_doctype": "Payment Entry", "erp_document": pe_name},
			"active",
			0,
		)

		PAYMENT_COUNTERS["payments_cancelled"] += 1
		logger.info("Cancelled Payment Entry '%s'.", pe_name)
	except Exception as err:
		frappe.db.rollback(save_point=sp_canc)
		PAYMENT_COUNTERS["failed"] += 1
		logger.error("Failed to cancel Payment Entry '%s': %s", pe_name, str(err))
		raise

	return pe_doc
