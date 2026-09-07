# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe


class InventoryError(frappe.ValidationError):
	"""Base exception for Bop ERP inventory operations."""
	pass


class ChannelCompanyMismatchError(InventoryError):
	"""Raised when a Channel Inventory Source warehouse company does not match channel company."""
	pass


class WarehouseNotFoundError(InventoryError):
	"""Raised when a specified warehouse does not exist."""
	pass


class DuplicateInventorySourceError(frappe.DuplicateEntryError):
	"""Raised when an active channel inventory source already exists for the channel and warehouse."""
	pass


class InsufficientStockToReserveError(InventoryError):
	"""Raised when requested reservation exceeds available ATP."""
	pass


class ReservationConflictError(InventoryError):
	"""Raised when a concurrent reservation race condition or locking conflict occurs."""
	pass


class ReservationNotFoundError(InventoryError):
	"""Raised when a requested Stock Reservation Entry cannot be found."""
	pass


class InvalidReservationRequestError(InventoryError):
	"""Raised when reservation parameters are malformed or invalid."""
	pass


class PolicyValidationError(InventoryError):
	"""Raised when an Inventory Availability Policy is invalid."""
	pass
