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
