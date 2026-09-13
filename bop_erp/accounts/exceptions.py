# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe


class SalesInvoiceError(frappe.ValidationError):
	"""Base exception for all Bop Sales Invoice operations."""
	pass


class OrderNotEligibleForInvoicingError(SalesInvoiceError):
	"""Raised when a Sales Order is not in a valid state to be invoiced."""
	pass


class MissingFulfillmentEvidenceError(SalesInvoiceError):
	"""Raised when an imported order is missing submitted Delivery Note fulfillment evidence."""
	pass


class DeliveryNoteNotReadyForInvoicingError(SalesInvoiceError):
	"""Raised when a Delivery Note is not submitted or in an invalid state for invoicing."""
	pass


class OverbillingBlockedError(SalesInvoiceError):
	"""Raised when invoicing exceeds delivered or unbilled quantity."""
	pass


class DuplicateSalesInvoiceError(SalesInvoiceError):
	"""Raised when duplicate sales invoice creation is attempted without reuse."""
	pass


class CompanyMismatchError(SalesInvoiceError):
	"""Raised when financial components or documents belong to different companies."""
	pass


class InvoicingFinancialReconciliationError(SalesInvoiceError):
	"""Raised when financial reconciliation against upstream document totals fails."""
	pass


class PaymentReconciliationError(frappe.ValidationError):
	"""Base exception for all Bop Payment Entry and External Payment Reconciliation operations."""
	pass


class PaymentEligibilityError(PaymentReconciliationError):
	"""Raised when an external payment or its target invoice/order is not eligible for reconciliation."""
	pass


class DuplicatePaymentError(PaymentReconciliationError):
	"""Raised when duplicate payment entry creation is attempted without convergence."""
	pass


class PaymentAccountMismatchError(PaymentReconciliationError):
	"""Raised when clearing accounts, party accounts, or company bounds do not match."""
	pass


class OverpaymentBlockedError(PaymentReconciliationError):
	"""Raised when payment allocation exceeds eligible invoice outstanding amount."""
	pass


class PaymentMappingDriftError(PaymentReconciliationError):
	"""Raised when canonical External ID Mapping drifts or conflicts during payment processing."""
	pass


class CustomerMismatchError(PaymentReconciliationError):
	"""Raised when customer bounds or parties do not match target invoices."""
	pass


class PaymentAuthorityLostError(PaymentReconciliationError):
	"""Raised when worker lease expires or processing token is stale before financial mutation."""
	pass


class RefundError(frappe.ValidationError):
	"""Base exception for all Bop Sales Return, Credit Note, and External Refund operations."""
	pass


class RefundEligibilityError(RefundError):
	"""Raised when a refund request or its target invoice/order/channel is ineligible for processing."""
	pass


class RefundStatusIneligibleError(RefundError):
	"""Raised when an external refund status is not eligible for accounting mutation (PENDING, FAILED, etc.)."""
	pass


class DuplicateRefundError(RefundError):
	"""Raised when duplicate refund processing is attempted without safe reuse."""
	pass


class OverRefundBlockedError(RefundError):
	"""Raised when requested refund amount or line returned quantity exceeds remaining eligible bounds."""
	pass


class RefundDriftError(RefundError):
	"""Raised when a replayed external refund identity has conflicting or drifted payload data."""
	pass


class RefundReplayCancelledError(RefundError):
	"""Raised when an external refund identity whose Credit Note was cancelled is replayed."""
	pass


class CurrencyMismatchError(RefundError):
	"""Raised when currency of the refund does not match the original invoice or company."""
	pass


class MaterialTaxMismatchError(frappe.ValidationError):
	"""Raised when external imported tax totals materially deviate from native ERPNext calculations beyond currency tolerance."""
	pass


class CreditLimitExceededError(frappe.ValidationError):
	"""Raised when a customer's outstanding balance plus transaction amount exceeds their credit limit."""
	pass


class FinancialTraceabilityError(frappe.ValidationError):
	"""Raised when financial traceability resolution cannot locate or link accounting records."""
	pass


class FinancialInvariantViolationError(frappe.ValidationError):
	"""Raised when an accounting invariant check detects inconsistent GL, balance, or document states."""
	pass






