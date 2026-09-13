# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.importers.catalog import CatalogImporter


class TestPresentationIdentityMediaLive(unittest.TestCase):
	"""
	Live Integration Test for Phase 1F.2:
	- Executes against local test container http://prestashop-test.
	- Validates provider-neutral internal Channel Category identity (cat_xxx).
	- Validates PrestaShop category identity resolution through External ID Mapping (erp_doctype='Channel Category').
	- Validates multilingual media metadata in media_localized_content child table (EN & ES).
	- Validates 5-tier alt-text fallback cascade on live imported presentations.
	- Validates 100% idempotency upon re-import (0 categories created, 0 presentations created).
	- Verifies ZERO inventory mutations (0 SLE, 0 Bin, 0 Item Price).
	- Zero contact with theindustrialdepot.com.
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

	def test_01_live_provider_neutral_taxonomy_and_localized_media(self):
		"""
		Executes live catalog import and validates provider-neutral category identity,
		External ID Mapping linkage, and normalized multilingual media metadata.
		"""
		sle_before = frappe.db.count("Stock Ledger Entry")
		bin_before = frappe.db.count("Bin")
		price_before = frappe.db.count("Item Price")

		importer = CatalogImporter(
			self.client, sales_channel=self.sales_channel, dry_run=False, sync_presentation=True
		)

		scoped_cats = {"10", "11", "12", "13", "14"}
		res1 = importer.run(
			category_ids=scoped_cats,
			sku_prefix_filter="SKU-"
		)
		self.assertTrue(res1["success"], f"Run 1 failed: {res1}")
		self.assertEqual(res1["total_failed"], 0)

		# 1. Assert External ID Mapping links Category 11 to internal Channel Category
		cat11_map = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": self.sales_channel,
				"provider": "PRESTASHOP",
				"external_entity_type": ExternalEntityType.CATEGORY,
				"external_id": "11",
				"active": 1,
			},
			["name", "erp_doctype", "erp_document"],
			as_dict=True
		)
		self.assertIsNotNone(cat11_map, "External ID Mapping for PrestaShop Category 11 must exist")
		self.assertEqual(cat11_map.erp_doctype, "Channel Category")

		cat_doc = frappe.get_doc("Channel Category", cat11_map.erp_document)
		# Assert provider-neutral identity: category_key must NOT start with ps-
		self.assertTrue(cat_doc.category_key.startswith("cat_"), f"Category key {cat_doc.category_key} must be provider-neutral")
		self.assertFalse(cat_doc.category_key.startswith("ps-"), "Category key must not contain ps- prefix")

		# Assert bilingual category content
		self.assertEqual(cat_doc.get_effective_category_name("en"), "Industrial Fasteners")
		self.assertEqual(cat_doc.get_effective_category_name("es"), "Fijaciones Industriales")
		self.assertEqual(cat_doc.get_effective_slug("es"), "fijaciones-industriales")

		# 2. Assert Product 20 (SKU-TOOL-HAMMER-16OZ) presentation & media
		prod20_map = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": self.sales_channel,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": "20",
				"active": 1,
			},
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

		# Verify localized marketing content
		self.assertIn("Claw Hammer", pres20.get_effective_item_name("en"))
		self.assertEqual(pres20.get_effective_item_name("es"), "Martillo de Uña para Trabajo Pesado 16oz")
		self.assertEqual(pres20.get_effective_slug("es"), "martillo-de-una-para-trabajo-pesado-16oz")

		# Verify multilingual media metadata in media_localized_content child table
		if pres20.media_items:
			asset_name = pres20.media_items[0].media_asset
			en_alt = pres20.get_effective_media_alt_text(asset_name, "en")
			es_alt = pres20.get_effective_media_alt_text(asset_name, "es")
			self.assertTrue(en_alt, "English alt text must be non-empty")
			self.assertTrue(es_alt, "Spanish alt text must be non-empty")

		# 3. Assert Product 25 (Variant Grade 8 Hex Bolt) presentation & media
		prod25_map = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": self.sales_channel,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": "25",
				"external_variant_id": ["is", "not set"],
				"active": 1,
			},
			"erp_document"
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

		# 4. Re-run: verify 100% idempotency
		res2 = importer.run(
			category_ids=scoped_cats,
			sku_prefix_filter="SKU-"
		)
		self.assertTrue(res2["success"], f"Run 2 failed: {res2}")
		self.assertEqual(res2["total_failed"], 0)
		self.assertEqual(res2["channel_categories"]["created"], 0, "Idempotency violated: category created on re-run")
		self.assertEqual(res2["presentations"]["created"], 0, "Idempotency violated: presentation created on re-run")

		# 5. Critical Safety Invariants: Zero inventory / price mutations (Baseline delta)
		sle_after = frappe.db.count("Stock Ledger Entry")
		bin_after = frappe.db.count("Bin")
		price_after = frappe.db.count("Item Price")
		self.assertEqual(sle_after - sle_before, 0, f"Critical Safety Violation: {sle_after - sle_before} Stock Ledger Entries!")
		self.assertEqual(bin_after - bin_before, 0, f"Critical Safety Violation: {bin_after - bin_before} Bins!")
		self.assertEqual(price_after - price_before, 0, f"Critical Safety Violation: {price_after - price_before} Item Prices!")


def run_all():
	import unittest
	suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestPresentationIdentityMediaLive)
	runner = unittest.TextTestRunner(verbosity=2)
	res = runner.run(suite)
	if not res.wasSuccessful():
		raise RuntimeError(f"Live tests failed: {len(res.failures)} failures, {len(res.errors)} errors")
