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
