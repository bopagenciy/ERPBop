# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from unittest.mock import patch
import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.inventory.item_lifecycle import (
	ItemLifecycleError,
	check_item_deletion_safety,
	safe_delete_or_disable_item,
	verify_item_not_locked_by_provenance,
)


class TestItemLifecycleUnit(FrappeTestCase):
	"""
	Tests for Bop ERP Item Lifecycle & Delete Safety Policy:
	1. Manual Item creation remains supported.
	2. Manual Item editing remains supported.
	3. Unused Item may follow native delete rules.
	4. Item with transactional history cannot be force-deleted.
	5. Disabling an item with history remains supported.
	6. Import provenance does not block manual editing.
	7. No validation bypass or history deletion.
	"""

	def setUp(self):
		super().setUp()
		self.test_item_code = f"TEST-LC-ITEM-{frappe.generate_hash(length=6)}"

	def tearDown(self):
		if frappe.db.exists("Item", self.test_item_code):
			frappe.db.delete("Item", {"name": self.test_item_code})
		super().tearDown()

	def test_01_manual_item_creation_supported(self):
		"""Manual Item creation in Bop ERP remains fully supported."""
		item = frappe.get_doc({
			"doctype": "Item",
			"item_code": self.test_item_code,
			"item_name": "Test Lifecycle Item",
			"item_group": "Products",
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)
		self.assertTrue(frappe.db.exists("Item", self.test_item_code))
		self.assertEqual(item.item_name, "Test Lifecycle Item")

	def test_02_manual_item_editing_supported(self):
		"""Manual editing of standard Item fields remains fully supported."""
		item = frappe.get_doc({
			"doctype": "Item",
			"item_code": self.test_item_code,
			"item_name": "Initial Name",
			"item_group": "Products",
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)

		item.item_name = "Updated Lifecycle Name"
		item.save(ignore_permissions=True)

		refreshed = frappe.get_doc("Item", self.test_item_code)
		self.assertEqual(refreshed.item_name, "Updated Lifecycle Name")

	def test_03_unused_item_may_follow_native_delete_rules(self):
		"""Unused items with zero transaction history can be deleted safely."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": self.test_item_code,
			"item_name": "Unused Item",
			"item_group": "Products",
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)

		is_safe, msg, refs = check_item_deletion_safety(self.test_item_code)
		self.assertTrue(is_safe)
		self.assertEqual(refs, [])

		res = safe_delete_or_disable_item(self.test_item_code)
		self.assertEqual(res["action_taken"], "DELETED")
		self.assertFalse(frappe.db.exists("Item", self.test_item_code))

	def test_04_item_with_history_cannot_be_force_deleted(self):
		"""Items with transactional history (e.g. Stock Ledger Entries) cannot be physically deleted."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": self.test_item_code,
			"item_name": "Historic Item",
			"item_group": "Products",
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)

		# Mock transactional reference in Stock Ledger Entry
		with patch.object(frappe.db, "count", side_effect=lambda dt, flt: 5 if dt == "Stock Ledger Entry" else 0):
			is_safe, msg, refs = check_item_deletion_safety(self.test_item_code)
			self.assertFalse(is_safe)
			self.assertTrue(any("Stock Ledger Entry" in r for r in refs))

			# Hard delete without force_disable raises ItemLifecycleError
			with self.assertRaises(ItemLifecycleError):
				safe_delete_or_disable_item(self.test_item_code, force_disable_if_history=False)

	def test_05_item_with_history_can_be_disabled(self):
		"""Items with history are disabled rather than physically deleted."""
		frappe.get_doc({
			"doctype": "Item",
			"item_code": self.test_item_code,
			"item_name": "Historic Item",
			"item_group": "Products",
			"stock_uom": "Nos",
			"disabled": 0,
		}).insert(ignore_permissions=True)

		with patch.object(frappe.db, "count", side_effect=lambda dt, flt: 10 if dt == "Sales Order Item" else 0):
			res = safe_delete_or_disable_item(self.test_item_code, force_disable_if_history=True)
			self.assertEqual(res["action_taken"], "DISABLED")
			item_doc = frappe.get_doc("Item", self.test_item_code)
			self.assertEqual(item_doc.disabled, 1)

	def test_06_import_provenance_does_not_lock_item(self):
		"""Items with migration provenance / external mappings remain editable."""
		item = frappe.get_doc({
			"doctype": "Item",
			"item_code": self.test_item_code,
			"item_name": "Imported Provenance Item",
			"item_group": "Products",
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)

		self.assertTrue(verify_item_not_locked_by_provenance(self.test_item_code))
