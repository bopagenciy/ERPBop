# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe


class OrderIngestionError(frappe.ValidationError):
	"""Base exception for Bop ERP order ingestion operations."""
	pass


class MissingProductMappingError(OrderIngestionError):
	"""Raised when an order line item or variant has no active External ID Mapping in ERP."""
	pass


class OrderNotEligibleError(OrderIngestionError):
	"""Raised when an order is in a non-eligible state (e.g. cancelled, payment error, refunded)."""
	pass


class OrderAlreadyImportedError(OrderIngestionError):
	"""Raised when an order has already been imported into ERP."""
	pass


class OrderTotalMismatchError(OrderIngestionError):
	"""Raised when ERP computed totals deviate from external totals beyond acceptable currency rounding tolerance."""
	pass


class InsufficientOrderStockError(OrderIngestionError):
	"""Raised when one or more order lines cannot be fully reserved due to insufficient ATP."""
	pass


class InvalidOrderQuantityError(OrderIngestionError):
	"""Raised when an order line has invalid quantity (e.g. <= 0 or non-integer for serialized items)."""
	pass


class CustomerMappingError(OrderIngestionError):
	"""Raised when customer identity resolution or creation fails."""
	pass


class AddressMappingError(OrderIngestionError):
	"""Raised when address identity resolution or creation fails."""
	pass


class OrderReservationFailedError(OrderIngestionError):
	"""Raised when creating native Stock Reservation Entries for a Sales Order fails."""
	pass
