# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

class PurchasingError(Exception):
	"""Base exception for all purchasing domain errors."""
	pass


class CompanyMismatchError(PurchasingError):
	"""Raised when cross-company entities or accounts are combined in a purchasing flow."""
	pass


class WarehouseMismatchError(PurchasingError):
	"""Raised when a warehouse does not belong to the transaction company or is invalid."""
	pass


class AccountMismatchError(PurchasingError):
	"""Raised when a bank, cash, or payable account does not match the company."""
	pass


class OverReceiptBlockedError(PurchasingError):
	"""Raised when an attempt is made to receive more quantity than ordered plus allowed tolerance."""
	pass


class OverBillingBlockedError(PurchasingError):
	"""Raised when an attempt is made to bill more quantity or amount than received / ordered."""
	pass


class OverPaymentBlockedError(PurchasingError):
	"""Raised when an attempt is made to pay more than the outstanding liability."""
	pass


class PurchaseDriftError(PurchasingError):
	"""Raised when an idempotent purchase operation is replayed with a mutated payload."""
	pass


class ConcurrentReceiptConflictError(PurchasingError):
	"""Raised when a concurrent receipt attempt conflicts with committed state."""
	pass


class PurchaseOrderError(PurchasingError):
	"""Raised when an error occurs during Purchase Order operations."""
	pass


class PurchaseReceiptError(PurchasingError):
	"""Raised when an error occurs during Purchase Receipt operations."""
	pass


class PurchaseInvoiceError(PurchasingError):
	"""Raised when an error occurs during Purchase Invoice operations."""
	pass


class VendorPaymentError(PurchasingError):
	"""Raised when an error occurs during Vendor Payment operations."""
	pass


class DocumentCancelledError(PurchasingError):
	"""Raised when an operation is attempted against a cancelled purchasing document."""
	pass


class CurrencyMismatchError(PurchasingError):
	"""Raised when currencies mismatch without valid conversion."""
	pass
