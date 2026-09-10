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


class ShippingTicketError(FulfillmentError):
	"""Base exception for Shipping Ticket operations."""
	pass


class OrderNotReadyForShippingError(ShippingTicketError):
	"""Raised when a Sales Order is not in a valid state to create a Shipping Ticket."""
	pass


class PickTicketRequiredError(ShippingTicketError):
	"""Raised when an automated imported order shipment is requested without a Pick Ticket."""
	pass


class PickTicketNotReadyForShippingError(ShippingTicketError):
	"""Raised when the associated Pick Ticket is not submitted or is in an invalid state."""
	pass


class PartialShippingBlockedError(ShippingTicketError):
	"""Raised when partial shipping is attempted under a full-scope shipping policy."""
	pass


class DuplicateShippingTicketError(ShippingTicketError):
	"""Raised when duplicate shipping ticket creation is attempted concurrently or without reuse."""
	pass


class WarehouseShippingMismatchError(ShippingTicketError):
	"""Raised when shipping warehouse allocation deviates from picked allocation."""
	pass
