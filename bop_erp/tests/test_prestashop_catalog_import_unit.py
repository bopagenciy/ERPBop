# See license.txt

import unittest
from unittest.mock import MagicMock, patch
import frappe

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.importers.products import sanitize_item_code, ProductImporter
from bop_erp.integrations.prestashop.importers.categories import CategoryImporter
from bop_erp.integrations.prestashop.importers.attributes import AttributeImporter
from bop_erp.integrations.prestashop.importers.catalog import CatalogImporter
from bop_erp.integrations.prestashop.importers.base import ImportResult


class TestPrestaShopCatalogImportUnit(unittest.TestCase):
	def setUp(self):
		self.sales_channel = "TID"

	def test_sanitize_item_code_policies(self):
		"""Validates deterministic item_code resolution across edge cases."""
		# Standard SKU
		self.assertEqual(sanitize_item_code("BOLT-1/4-20", "101"), "BOLT-1/4-20")

		# Missing/empty SKU fallback
		self.assertEqual(sanitize_item_code("", "101"), "PS-101")
		self.assertEqual(sanitize_item_code("   ", "102"), "PS-102")
		self.assertEqual(sanitize_item_code(None, "103"), "PS-103")

		# SKU truncation exceeding 140 chars
		long_sku = "SKU-" + "A" * 200
		sanitized = sanitize_item_code(long_sku, "104")
		self.assertEqual(len(sanitized), 140)
		self.assertTrue(sanitized.startswith("SKU-"))

		# Unicode SKU preserved
		unicode_sku = "TUERCA-ACERO-INOX-Ø10"
		self.assertEqual(sanitize_item_code(unicode_sku, "105"), unicode_sku)

	def test_category_importer_dry_run(self):
		"""Validates that CategoryImporter with dry_run=True performs zero DB writes."""
		mock_client = MagicMock()
		mock_client.list_categories.return_value = [
			{
				"id": "99901",
				"id_parent": "2",
				"name": [{"id": "1", "value": "Dry Run Category"}],
				"active": "1",
			}
		]

		importer = CategoryImporter(mock_client, sales_channel=self.sales_channel, dry_run=True)
		result = importer.import_categories()

		self.assertEqual(result.seen, 1)
		self.assertEqual(result.created, 1)
		self.assertEqual(result.failed, 0)

		# Verify nothing was written to DB
		exists = frappe.db.exists("Item Group", "Dry Run Category")
		self.assertIsNone(exists)

		mapping = frappe.db.exists(
			"External ID Mapping",
			{"sales_channel": self.sales_channel, "external_id": "99901"},
		)
		self.assertIsNone(mapping)

	def test_product_importer_dry_run(self):
		"""Validates that ProductImporter with dry_run=True performs zero DB writes."""
		mock_client = MagicMock()
		mock_client.list_product_options.return_value = []
		mock_client.list_product_option_values.return_value = []
		mock_client.list_products.return_value = [
			{
				"id": "99902",
				"reference": "DRY-RUN-SKU",
				"name": [{"id": "1", "value": "Dry Run Simple Product"}],
				"price": "19.99",
				"active": "1",
			}
		]

		importer = ProductImporter(mock_client, sales_channel=self.sales_channel, dry_run=True)
		result = importer.import_products()

		self.assertEqual(result.seen, 1)
		self.assertEqual(result.created, 1)
		self.assertEqual(result.failed, 0)

		# Verify nothing written to Item or Mapping
		self.assertIsNone(frappe.db.exists("Item", "DRY-RUN-SKU"))
		mapping = frappe.db.exists(
			"External ID Mapping",
			{"sales_channel": self.sales_channel, "external_id": "99902"},
		)
		self.assertIsNone(mapping)

	def test_failure_isolation_and_rollback(self):
		"""Validates that an individual item failure is captured without breaking other items."""
		mock_client = MagicMock()
		mock_client.list_product_options.return_value = []
		mock_client.list_product_option_values.return_value = []

		importer = ProductImporter(mock_client, sales_channel=self.sales_channel, dry_run=False)

		# Mock _import_single_product to fail on item 2
		good_raw = {
			"id": "99903",
			"reference": "GOOD-SKU",
			"name": [{"id": "1", "value": "Good Product"}],
			"active": "1",
		}
		bad_raw = {
			"id": "99904",
			"reference": "BAD-SKU",
			"name": [{"id": "1", "value": "Bad Product"}],
			"active": "1",
		}

		with patch.object(importer, "_sync_simple_product", side_effect=[
			"created",
			Exception("Simulated Database Error on Product 99904"),
		]):
			result = importer.import_products([good_raw, bad_raw])

		self.assertEqual(result.seen, 2)
		self.assertEqual(result.created, 1)
		self.assertEqual(result.failed, 1)
		self.assertEqual(len(result.errors), 1)
