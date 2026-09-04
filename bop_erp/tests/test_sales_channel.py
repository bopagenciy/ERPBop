# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from bop_erp.constants import ChannelType

class TestSalesChannel(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")

	def test_channel_creation_and_rename(self):
		channel_id = "TEST_CH_1"
		if frappe.db.exists("Sales Channel", channel_id):
			frappe.delete_doc("Sales Channel", channel_id, force=True)

		doc = frappe.get_doc({
			"doctype": "Sales Channel",
			"channel_id": channel_id,
			"channel_name": "Test Channel One",
			"channel_type": ChannelType.COUNTER,
			"company": self.company,
			"active": 1,
		}).insert()

		self.assertEqual(doc.channel_id, "TEST_CH_1")
		self.assertEqual(doc.channel_name, "Test Channel One")

		# Renaming channel_name is permitted
		doc.channel_name = "Test Channel Renamed"
		doc.save()
		self.assertEqual(frappe.db.get_value("Sales Channel", channel_id, "channel_name"), "Test Channel Renamed")

	def test_channel_id_uniqueness(self):
		channel_id = "TEST_CH_DUP"
		if frappe.db.exists("Sales Channel", channel_id):
			frappe.delete_doc("Sales Channel", channel_id, force=True)

		frappe.get_doc({
			"doctype": "Sales Channel",
			"channel_id": channel_id,
			"channel_name": "Channel DUP",
			"channel_type": ChannelType.PHONE,
			"company": self.company,
			"active": 1,
		}).insert()

		# Attempt duplicate
		dup = frappe.get_doc({
			"doctype": "Sales Channel",
			"channel_id": channel_id,
			"channel_name": "Channel DUP 2",
			"channel_type": ChannelType.PHONE,
			"company": self.company,
			"active": 1,
		})
		self.assertRaises((frappe.DuplicateEntryError, frappe.ValidationError), dup.insert)

	def test_channel_disable(self):
		channel_id = "TEST_CH_DIS"
		if frappe.db.exists("Sales Channel", channel_id):
			frappe.delete_doc("Sales Channel", channel_id, force=True)

		doc = frappe.get_doc({
			"doctype": "Sales Channel",
			"channel_id": channel_id,
			"channel_name": "Channel To Disable",
			"channel_type": ChannelType.INTERNAL,
			"company": self.company,
			"active": 1,
		}).insert()

		doc.active = 0
		doc.save()
		self.assertEqual(frappe.db.get_value("Sales Channel", channel_id, "active"), 0)

	def test_channel_delete_protection_when_referenced(self):
		channel_id = "TEST_CH_REF"
		if frappe.db.exists("Sales Channel", channel_id):
			frappe.delete_doc("Sales Channel", channel_id, force=True)

		doc = frappe.get_doc({
			"doctype": "Sales Channel",
			"channel_id": channel_id,
			"channel_name": "Referenced Channel",
			"channel_type": ChannelType.OTHER,
			"company": self.company,
			"active": 1,
		}).insert()

		# Reference in External ID Mapping
		mapping = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": channel_id,
			"external_entity_type": "CUSTOMER",
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "EXT-REF-TEST",
			"active": 1,
		}).insert()

		# Attempt to delete channel should be rejected
		self.assertRaises(frappe.ValidationError, frappe.delete_doc, "Sales Channel", channel_id)

		# Clean up mapping first, then delete should succeed
		frappe.delete_doc("External ID Mapping", mapping.name, force=True)
		frappe.delete_doc("Sales Channel", channel_id)
		self.assertFalse(frappe.db.exists("Sales Channel", channel_id))

	def test_channel_delete_blocked_by_all_transaction_types(self):
		from unittest.mock import patch

		channel_id = "TEST_CH_ALL_TX"
		if frappe.db.exists("Sales Channel", channel_id):
			frappe.delete_doc("Sales Channel", channel_id, force=True)

		doc = frappe.get_doc({
			"doctype": "Sales Channel",
			"channel_id": channel_id,
			"channel_name": "All Tx Channel",
			"channel_type": ChannelType.OTHER,
			"company": self.company,
			"active": 1,
		}).insert()

		transaction_doctypes = [
			"Sales Order",
			"Pick List",
			"Delivery Note",
			"Shipment",
			"Sales Invoice",
			"Payment Entry",
		]

		for dt in transaction_doctypes:
			with patch("frappe.db.count", side_effect=lambda doctype, filters: 1 if doctype == dt else 0):
				self.assertTrue(doc.has_transaction_references(channel_id))
				self.assertRaises(frappe.ValidationError, frappe.delete_doc, "Sales Channel", channel_id)

		frappe.delete_doc("Sales Channel", channel_id)

