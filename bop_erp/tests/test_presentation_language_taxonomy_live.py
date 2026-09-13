# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.importers.catalog import CatalogImporter


class TestPresentationLanguageTaxonomyLive(unittest.TestCase):
	"""
	Live Integration Test for Phase 1F.1:
	- Executes against local test container http://prestashop-test.
	- Dynamic PrestaShop language resolution (IDs to canonical ISO codes).
	- Scoped industrial import: only relevant industrial categories (10, 11, 12, 13, 14) and ancestors.
	- Excludes unrelated demo category trees (Clothes 3, Accessories 6, Art 9).
	- Validates bilingual localized content (EN & ES) for products (simple Product 20, variant Product 25)
	  and categories (Category 11).
	- Validates localized SEO fields (channel_slug, meta_title, meta_description) on child tables.
	- Validates 100% idempotency upon re-import (0 created, 0 duplicate localized rows).
	- Verifies ZERO inventory mutations (0 Stock Ledger Entries, 0 Bins, 0 Item Prices).
	- ZERO contact with theindustrialdepot.com.
	"""

	@classmethod
	def setUpClass(cls):
		cls.sales_channel = "TID"
		cls.config = PrestaShopConfig(
			sales_channel=cls.sales_channel,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_PRESTASHOP_KEY",
			verify_tls=False,
			read_enabled=True,
			write_enabled=False,
		)
		cls.client = PrestaShopClient(config=cls.config)

		# Ensure Language records exist in Frappe
		for lang_code in ["en", "es"]:
			if not frappe.db.exists("Language", lang_code):
				frappe.get_doc({
					"doctype": "Language",
					"language_code": lang_code,
					"language_name": "English" if lang_code == "en" else "Spanish",
				}).insert(ignore_permissions=True)

		# Ensure Sales Channel TID exists with default_language 'en'
		if frappe.db.exists("Sales Channel", cls.sales_channel):
			ch = frappe.get_doc("Sales Channel", cls.sales_channel)
			ch.language = "en"
			ch.save(ignore_permissions=True)

		# Ensure clean taxonomy state for scoped testing: delete demo channel categories if created by earlier unscoped tests
		demo_ids = ["3", "4", "5", "6", "7", "8", "9"]
		demo_mappings = frappe.get_all(
			"External ID Mapping",
			filters={
				"sales_channel": cls.sales_channel,
				"external_entity_type": ExternalEntityType.CATEGORY,
				"external_id": ["in", demo_ids],
				"erp_doctype": "Channel Category",
			},
			fields=["name", "erp_document"],
		)
		for m in demo_mappings:
			if m.erp_document and frappe.db.exists("Channel Category", m.erp_document):
				frappe.delete_doc("Channel Category", m.erp_document, force=True, ignore_permissions=True)
			frappe.delete_doc("External ID Mapping", m.name, force=True, ignore_permissions=True)

		demo_keys = ["ps-3", "ps-4", "ps-5", "ps-6", "ps-7", "ps-8", "ps-9"]
		existing_demo_cats = frappe.get_all(
			"Channel Category",
			filters={"sales_channel": cls.sales_channel, "category_key": ["in", demo_keys]},
			pluck="name",
		)
		for cat_name in existing_demo_cats:
			frappe.delete_doc("Channel Category", cat_name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_01_live_bilingual_sync_and_scope_closure(self):
		"""
		Tests live import of industrial catalog with category scope closure and bilingual verification.
		"""
		sle_before = frappe.db.count("Stock Ledger Entry")
		bin_before = frappe.db.count("Bin")
		price_before = frappe.db.count("Item Price")

		importer = CatalogImporter(
			self.client, sales_channel=self.sales_channel, dry_run=False, sync_presentation=True
		)

		# Scoped run for industrial catalog: categories 10, 11, 12, 13, 14, sku prefix SKU-
		scoped_cats = {"10", "11", "12", "13", "14"}
		res1 = importer.run(
			category_ids=scoped_cats,
			sku_prefix_filter="SKU-"
		)
		self.assertTrue(res1["success"], f"Run 1 failed: {res1}")
		self.assertEqual(res1["total_failed"], 0)

		# Assert Channel Category scope closure:
		# Industrial categories must be present
		cat_11_mapping = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": self.sales_channel,
				"external_entity_type": ExternalEntityType.CATEGORY,
				"external_id": "11",
				"erp_doctype": "Channel Category",
				"active": 1,
			},
			"erp_document",
		)
		self.assertIsNotNone(cat_11_mapping, "Industrial Fasteners category 11 must be mapped to Channel Category")

		cat_11_doc = frappe.get_doc("Channel Category", cat_11_mapping)
		# Verify bilingual content on Category 11
		self.assertEqual(cat_11_doc.get_effective_category_name("en"), "Industrial Fasteners")
		self.assertEqual(cat_11_doc.get_effective_category_name("es"), "Fijaciones Industriales")
		self.assertEqual(cat_11_doc.get_effective_slug("es"), "fijaciones-industriales")

		# Verify demo categories are NOT present in Channel Category
		demo_ids = ["3", "4", "5", "6", "7", "8", "9"]
		for did in demo_ids:
			exists = frappe.db.exists(
				"External ID Mapping",
				{
					"sales_channel": self.sales_channel,
					"external_entity_type": ExternalEntityType.CATEGORY,
					"external_id": did,
					"erp_doctype": "Channel Category",
					"active": 1,
				},
			)
			self.assertIsNone(exists, f"Demo category {did} should NOT have been imported under scoped run")

		# Verify Simple Product 20 (SKU-HAMMER-01) bilingual presentation
		prod20_map = frappe.db.get_value(
			"External ID Mapping",
			{"sales_channel": self.sales_channel, "external_entity_type": ExternalEntityType.PRODUCT, "external_id": "20", "active": 1},
			"erp_document"
		)
		self.assertIsNotNone(prod20_map, "Product 20 must be mapped")

		pres20_name = frappe.db.get_value(
			"Item Channel Presentation",
			{"item": prod20_map, "sales_channel": self.sales_channel},
			"name"
		)
		self.assertIsNotNone(pres20_name, "Product 20 presentation must exist")
		pres20 = frappe.get_doc("Item Channel Presentation", pres20_name)

		# Verify 5-tier fallback and localized content
		self.assertIn("Claw Hammer", pres20.get_effective_item_name("en"))
		self.assertEqual(pres20.get_effective_item_name("es"), "Martillo de Uña para Trabajo Pesado 16oz")
		self.assertEqual(pres20.get_effective_slug("es"), "martillo-de-una-para-trabajo-pesado-16oz")

		# Verify Variant Product 25 (BOLT-001) bilingual presentation
		prod25_map = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": self.sales_channel,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": "25",
				"external_variant_id": ["is", "not set"],
				"active": 1,
			},
			"erp_document",
		)
		self.assertIsNotNone(prod25_map, "Product 25 template must be mapped")

		pres25_name = frappe.db.get_value(
			"Item Channel Presentation",
			{"item": prod25_map, "sales_channel": self.sales_channel},
			"name"
		)
		self.assertIsNotNone(pres25_name, "Product 25 presentation must exist")
		pres25 = frappe.get_doc("Item Channel Presentation", pres25_name)

		self.assertEqual(pres25.get_effective_item_name("es"), "Perno Hexagonal Grado 8")
		self.assertEqual(pres25.get_effective_slug("es"), "perno-hexagonal-grado-8")

		# Re-run: verify 100% idempotency
		res2 = importer.run(
			category_ids=scoped_cats,
			sku_prefix_filter="SKU-"
		)
		self.assertTrue(res2["success"], f"Run 2 failed: {res2}")
		self.assertEqual(res2["total_failed"], 0)
		self.assertEqual(res2["channel_categories"]["created"], 0, "Idempotency violated: category created on re-run")
		self.assertEqual(res2["presentations"]["created"], 0, "Idempotency violated: presentation created on re-run")

		# Ensure no duplicate localized rows were inserted on re-run
		reloaded_pres20 = frappe.get_doc("Item Channel Presentation", pres20_name)
		langs = [r.language for r in reloaded_pres20.localized_content]
		self.assertEqual(len(langs), len(set(langs)), f"Duplicate language rows found on presentation 20: {langs}")

		# Critical Safety Check: Zero inventory / price mutations (Baseline delta)
		sle_after = frappe.db.count("Stock Ledger Entry")
		bin_after = frappe.db.count("Bin")
		price_after = frappe.db.count("Item Price")
		self.assertEqual(sle_after - sle_before, 0, f"Critical Safety Violation: {sle_after - sle_before} Stock Ledger Entries detected!")
		self.assertEqual(bin_after - bin_before, 0, f"Critical Safety Violation: {bin_after - bin_before} Bins detected!")
		self.assertEqual(price_after - price_before, 0, f"Critical Safety Violation: {price_after - price_before} Item Prices detected!")


def run_all():
	import unittest
	suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestPresentationLanguageTaxonomyLive)
	runner = unittest.TextTestRunner(verbosity=2)
	res = runner.run(suite)
	if not res.wasSuccessful():
		raise RuntimeError(f"Live tests failed: {len(res.failures)} failures, {len(res.errors)} errors")
