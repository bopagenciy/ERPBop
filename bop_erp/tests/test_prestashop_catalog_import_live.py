# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.importers.catalog import CatalogImporter


class TestPrestaShopCatalogImportLive(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.sales_channel = "TID"
		# Resolve client using test key from site_config.json
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

	def test_01_catalog_import_pipeline_and_idempotency(self):
		"""
		Tests complete catalog import against local PrestaShop test instance:
		1. Imports categories, attributes, products, variants with explicit 4-tier accounting.
		2. Verifies native ERPNext Item, Item Group, Item Attribute, and External ID Mapping exist.
		3. Verifies PrestaShop virtual/system root categories are skipped.
		4. Re-runs import and verifies 100% idempotency (created=0 across all tiers).
		5. Verifies inventory is untouched (0 Stock Ledger Entries, 0 Bins).
		"""
		importer = CatalogImporter(self.client, sales_channel=self.sales_channel, dry_run=False)

		# Run catalog import
		run1 = importer.run()
		self.assertTrue(run1["success"], f"Run 1 failed: {run1}")
		self.assertEqual(run1["total_failed"], 0)

		# Verify explicit 4-tier counters are present
		for key in ("categories", "attributes", "products", "variants", "mappings"):
			self.assertIn(key, run1)
			self.assertIsInstance(run1[key], dict)

		# Verify categories: total 14 in PrestaShop, 2 system roots skipped, 12 business categories
		self.assertEqual(run1["categories"]["seen"], 14)
		self.assertEqual(run1["categories"]["skipped"], 2, "PrestaShop root categories (1 and 2) must be skipped")

		# Verify mappings exist
		cat_maps = frappe.db.get_all(
			"External ID Mapping",
			filters={"sales_channel": self.sales_channel, "external_entity_type": ExternalEntityType.CATEGORY, "active": 1},
			fields=["name", "erp_document", "external_id"],
		)
		self.assertGreater(len(cat_maps), 0)
		for cm in cat_maps:
			self.assertTrue(frappe.db.exists("Item Group", cm.erp_document))

		# Verify products & variants
		prod_maps = frappe.db.get_all(
			"External ID Mapping",
			filters={"sales_channel": self.sales_channel, "external_entity_type": ExternalEntityType.PRODUCT, "active": 1},
			fields=["name", "erp_document", "external_id"],
		)
		self.assertGreater(len(prod_maps), 0)
		for pm in prod_maps:
			self.assertTrue(frappe.db.exists("Item", pm.erp_document))

		# Second run: Idempotency verification
		run2 = importer.run()
		self.assertTrue(run2["success"], f"Run 2 failed: {run2}")
		self.assertEqual(run2["total_failed"], 0)
		self.assertEqual(run2["categories"]["created"], 0, "Idempotency violated: categories created on re-run")
		self.assertEqual(run2["attributes"]["created"], 0, "Idempotency violated: attributes created on re-run")
		self.assertEqual(run2["products"]["created"], 0, "Idempotency violated: products created on re-run")
		self.assertEqual(run2["variants"]["created"], 0, "Idempotency violated: variants created on re-run")
		self.assertEqual(run2["mappings"]["created"], 0, "Idempotency violated: mappings created on re-run")

		# Inventory Safety: Ensure ZERO Stock Ledger Entries and ZERO Bins
		sle_count = frappe.db.count("Stock Ledger Entry")
		bin_count = frappe.db.count("Bin")
		self.assertEqual(sle_count, 0, f"Critical Safety Violation: {sle_count} Stock Ledger Entries created during catalog import!")
		self.assertEqual(bin_count, 0, f"Critical Safety Violation: {bin_count} Bins created during catalog import!")
