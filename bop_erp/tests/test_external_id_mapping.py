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
		self.assertEqual(doc.active_external_key, f"TID::{ExternalEntityType.CUSTOMER}::CUST-9999")
		self.assertEqual(doc.active_erp_key, f"TID::{ExternalEntityType.CUSTOMER}::Company::{self.company}")
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

	def test_concurrent_duplicate_external_key_db_constraint(self):
		m1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-CONC-1",
			"active": 1,
		}).insert()

		# Bypass Frappe validate() to simulate a concurrent race condition at the DB layer
		m2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-CONC-1",
			"active_external_key": m1.active_external_key,
			"active_erp_key": "DIFFERENT-ERP-KEY",
			"active": 1,
		})
		self.assertRaises(Exception, m2.db_insert)
		frappe.delete_doc("External ID Mapping", m1.name)

	def test_concurrent_duplicate_erp_key_db_constraint(self):
		m1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-CONC-A",
			"active": 1,
		}).insert()

		# Bypass Frappe validate() to simulate concurrent insert on active_erp_key
		m2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-CONC-B",
			"active_external_key": "DIFFERENT-EXT-KEY",
			"active_erp_key": m1.active_erp_key,
			"active": 1,
		})
		self.assertRaises(Exception, m2.db_insert)
		frappe.delete_doc("External ID Mapping", m1.name)

	def test_deactivating_allows_new_active_mapping(self):
		m1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "CUST-OLD-01",
			"active": 1,
		}).insert()

		# Deactivate m1
		m1.active = 0
		m1.save()
		self.assertIsNone(m1.active_external_key)
		self.assertIsNone(m1.active_erp_key)

		# Now create new active mapping m2 for same ERP document
		m2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "CUST-NEW-01",
			"active": 1,
		}).insert()

		self.assertTrue(m2.name)
		self.assertEqual(m2.active, 1)

		# Also verify multiple inactive mappings can coexist without uniqueness clash
		m3 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "CUST-OLD-01",
			"active": 0,
		}).insert()
		self.assertTrue(m3.name)

		frappe.delete_doc("External ID Mapping", m1.name)
		frappe.delete_doc("External ID Mapping", m2.name)
		frappe.delete_doc("External ID Mapping", m3.name)
