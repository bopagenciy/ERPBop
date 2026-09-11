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

