# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from bop_erp.constants import ExternalEntityType
from bop_erp.bop_erp.doctype.external_id_mapping.external_id_mapping import (
	compute_active_external_key,
	compute_active_erp_key,
)

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
		self.assertEqual(len(doc.active_external_key), 64)
		self.assertEqual(len(doc.active_erp_key), 64)
		self.assertEqual(
			doc.active_external_key,
			compute_active_external_key("TID", ExternalEntityType.CUSTOMER, "CUST-9999"),
		)
		self.assertEqual(
			doc.active_erp_key,
			compute_active_erp_key("TID", ExternalEntityType.CUSTOMER, "Company", self.company),
		)
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

		# Bypass Frappe validate() to simulate concurrent race condition at DB layer
		m2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-CONC-1",
			"active_external_key": m1.active_external_key,
			"active_erp_key": "1111111111222222222233333333334444444444555555555566666666667777",
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
			"active_external_key": "1111111111222222222233333333334444444444555555555566666666667777",
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

	def test_product_duplicate_cannot_bypass_uniqueness_using_external_variant_id(self):
		doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-123",
			"external_variant_id": "VAR-SUFFIX-A",
			"active": 1,
		})
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_order_duplicate_cannot_bypass_uniqueness_with_variant_field(self):
		doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.ORDER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "ORD-999",
			"external_variant_id": "VAR-ORDER-X",
			"active": 1,
		})
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_product_variant_ab_coexist_correctly(self):
		v1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-PARENT-100",
			"external_variant_id": "COMBINATION-A",
			"active": 1,
		}).insert()

		v2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"erp_doctype": "DocType",
			"erp_document": "Company",
			"external_id": "PROD-PARENT-100",
			"external_variant_id": "COMBINATION-B",
			"active": 1,
		}).insert()

		self.assertTrue(v1.name and v2.name)
		self.assertNotEqual(v1.active_external_key, v2.active_external_key)
		self.assertNotEqual(v1.active_erp_key, v2.active_erp_key)

		# Duplicate active variant A on PROD-PARENT-100 must be rejected
		v_dup = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"erp_doctype": "DocType",
			"erp_document": "Sales Channel",
			"external_id": "PROD-PARENT-100",
			"external_variant_id": "COMBINATION-A",
			"active": 1,
		})
		self.assertRaises(frappe.ValidationError, v_dup.insert)

		frappe.delete_doc("External ID Mapping", v1.name)
		frappe.delete_doc("External ID Mapping", v2.name)

	def test_product_variant_requires_external_variant_id(self):
		v = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-PARENT-100",
			"external_variant_id": None,
			"active": 1,
		})
		self.assertRaises(frappe.ValidationError, v.insert)

	def test_product_variant_erp_inverse_identity_rules(self):
		# 6. PRODUCT_VARIANT A -> ITEM-001 (Company)
		v1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "BAMAL",
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-100",
			"external_variant_id": "Variant-A",
			"active": 1,
		}).insert()
		self.assertTrue(v1.name)

		# 7. PRODUCT_VARIANT B -> ITEM-001 (same channel + same ERP doc) MUST BE REJECTED
		v2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "BAMAL",
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-100",
			"external_variant_id": "Variant-B",
			"active": 1,
		})
		self.assertRaises(frappe.ValidationError, v2.insert)

		# 8. PRODUCT_VARIANT B -> ITEM-002 (different ERP doc) MUST BE ACCEPTED
		v3 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "BAMAL",
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"erp_doctype": "DocType",
			"erp_document": "Company",
			"external_id": "PROD-100",
			"external_variant_id": "Variant-B",
			"active": 1,
		}).insert()
		self.assertTrue(v3.name)

		# 9. Same ERP Item may map independently in different Sales Channels (e.g. TID)
		v4 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": "PROD-100",
			"external_variant_id": "Variant-A",
			"active": 1,
		}).insert()
		self.assertTrue(v4.name)

		frappe.delete_doc("External ID Mapping", v1.name)
		frappe.delete_doc("External ID Mapping", v3.name)
		frappe.delete_doc("External ID Mapping", v4.name)

	def test_exact_casing_and_whitespace_preservation(self):
		# 1. "ABC" != "abc"
		# 2. "ABC" != " ABC"
		# 3. "ABC" != "ABC "
		k_plain = compute_active_external_key("TID", ExternalEntityType.PRODUCT, "ABC")
		k_lower = compute_active_external_key("TID", ExternalEntityType.PRODUCT, "abc")
		k_lead_space = compute_active_external_key("TID", ExternalEntityType.PRODUCT, " ABC")
		k_trail_space = compute_active_external_key("TID", ExternalEntityType.PRODUCT, "ABC ")

		self.assertNotEqual(k_plain, k_lower)
		self.assertNotEqual(k_plain, k_lead_space)
		self.assertNotEqual(k_plain, k_trail_space)
		self.assertNotEqual(k_lead_space, k_trail_space)

		# Persist with leading space
		doc_lead = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": " ABC",
			"active": 1,
		}).insert()
		self.assertEqual(doc_lead.external_id, " ABC")

		# Persist with trailing space (different ERP doc to avoid ERP doc uniqueness clash)
		doc_trail = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "DocType",
			"erp_document": "Company",
			"external_id": "ABC ",
			"active": 1,
		}).insert()
		self.assertEqual(doc_trail.external_id, "ABC ")

		frappe.delete_doc("External ID Mapping", doc_lead.name)
		frappe.delete_doc("External ID Mapping", doc_trail.name)

	def test_separator_containing_ids_remain_exact(self):
		# 4. separator-containing IDs remain exact
		sep_id = 'PART::100,200/TEST#"SPECIAL"::SUBKEY'
		doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": sep_id,
			"active": 1,
		}).insert()

		self.assertTrue(doc.name)
		self.assertEqual(doc.external_id, sep_id)
		self.assertEqual(len(doc.active_external_key), 64)
		frappe.delete_doc("External ID Mapping", doc.name)

	def test_long_external_ids_beyond_255_chars(self):
		# 5. long external ID beyond 255 chars where supported (e.g. 500 chars)
		long_id = "VERY-LONG-EXTERNAL-ID-" + ("Z" * 478) # Exactly 500 chars
		self.assertEqual(len(long_id), 500)

		doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": long_id,
			"active": 1,
		}).insert()

		self.assertTrue(doc.name)
		self.assertEqual(len(doc.external_id), 500)
		self.assertEqual(doc.external_id, long_id)
		self.assertEqual(len(doc.active_external_key), 64)
		frappe.delete_doc("External ID Mapping", doc.name)

	def test_unbounded_payload_rejected(self):
		# IDs exceeding MAX_EXTERNAL_ID_LENGTH (1000) are rejected
		oversized_id = "X" * 1001
		doc = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": "TID",
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"erp_doctype": "Company",
			"erp_document": self.company,
			"external_id": oversized_id,
			"active": 1,
		})
		self.assertRaises(frappe.ValidationError, doc.insert)
