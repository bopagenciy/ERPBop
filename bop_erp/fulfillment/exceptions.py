# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe


class FulfillmentError(frappe.ValidationError):
	"""Base exception for all Bop fulfillment operations."""
	pass


class OrderNotReadyForPickingError(FulfillmentError):
	"""Raised when a Sales Order is not in a valid state to create a Pick Ticket."""
	pass


class WarehouseAllocationMismatchError(FulfillmentError):
	"""Raised when requested picking warehouse does not match existing SRE or stock allocation."""
	pass


class PartialPickBlockedError(FulfillmentError):
	"""Raised when partial picking is requested or required but allow_partial=False."""
	pass


class DuplicatePickTicketError(FulfillmentError):
	"""Raised when duplicate pick ticket creation is attempted concurrently or without reuse."""
	pass


class InsufficientStockError(FulfillmentError):
	"""Raised when required pick stock is insufficient in the designated warehouse."""
	pass


class NonStockItemPickError(FulfillmentError):
	"""Raised when an attempt is made to pick non-stock or service items."""
	pass
