# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from bop_erp.constants import ExternalEntityType

class TestExternalIDMapping(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		self.ensure_channels()

	def ensure_channels(self):
		for ch in ["TID", "BAMAL"]:
			if not frappe.db.exists("Sales Channel", ch):
				frappe.get_doc({
					"doctype": "Sales Channel",
					"channel_id": ch,
					"channel_name": ch,
					"channel_type": "PRESTASHOP",
					"company": self.company,
					"active": 1,
				}).insert()

	def test_valid_mapping(self):
		doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "CUST-9999",
			"active": 1,
		}).insert()

		self.assertTrue(doc.name)
		self.assertEqual(doc.external_id, "CUST-9999")
		frappe.delete_doc("External ID Mapping", doc.name)

	def test_duplicate_external_id_rejected(self):
		m1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.ORDER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "ORD-12345",
			"active": 1,
		}).insert()

		# Attempt another mapping in TID for ORDER with same external_id
		m2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.ORDER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "ORD-12345",
			"active": 1,
		})
		self.assertRaises(frappe.ValidationError, m2.insert)
		frappe.delete_doc("External ID Mapping", m1.name)

	def test_same_external_id_allowed_for_different_entity_types(self):
		m1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "SHARED-ID-1",
			"active": 1,
		}).insert()

		m2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.INVOICE,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "SHARED-ID-1",
			"active": 1,
		}).insert()

		self.assertTrue(m1.name and m2.name)
		frappe.delete_doc("External ID Mapping", m1.name)
		frappe.delete_doc("External ID Mapping", m2.name)

	def test_same_erp_document_mapped_to_different_channels(self):
		m1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CATEGORY,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "CAT-TID-1",
			"active": 1,
		}).insert()

		m2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "BAMAL",
			"external_entity_type": ExternalEntityType.CATEGORY,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "CAT-BAMAL-1",
			"active": 1,
		}).insert()

		self.assertTrue(m1.name and m2.name)
		frappe.delete_doc("External ID Mapping", m1.name)
		frappe.delete_doc("External ID Mapping", m2.name)

	def test_duplicate_active_erp_document_rejected(self):
		m1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "EXT-CUST-1",
			"active": 1,
		}).insert()

		# Attempting another active mapping for the same ERP document in the same channel and entity type
		m2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "EXT-CUST-2",
			"active": 1,
		})
		self.assertRaises(frappe.ValidationError, m2.insert)
		frappe.delete_doc("External ID Mapping", m1.name)

	def test_nonexistent_linked_document_rejected(self):
		doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Customer",
			"erp_document": "NONEXISTENT-CUSTOMER-123456",
			"external_id": "EXT-999",
			"active": 1,
		})
		self.assertRaises(frappe.ValidationError, doc.insert)
