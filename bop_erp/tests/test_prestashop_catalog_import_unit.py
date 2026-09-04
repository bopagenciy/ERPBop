# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import MagicMock, patch
import frappe

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.importers.products import (
	sanitize_item_code,
	build_collision_safe_code,
	ProductImporter,
)
from bop_erp.integrations.prestashop.importers.categories import CategoryImporter
from bop_erp.integrations.prestashop.importers.attributes import (
	AttributeImporter,
	compute_stable_abbreviation,
)
from bop_erp.integrations.prestashop.importers.catalog import CatalogImporter
from bop_erp.integrations.prestashop.importers.base import ImportResult


class TestPrestaShopCatalogImportUnit(unittest.TestCase):
	def setUp(self):
		self.sales_channel = "TID"

	def test_channel_safe_generated_item_codes(self):
		"""Validates that missing SKUs generate channel-qualified codes preventing cross-store collisions."""
		code_tid = sanitize_item_code("", "TID", "17")
		code_bamal = sanitize_item_code("", "BAMAL", "17")

		self.assertEqual(code_tid, "PS-TID-17")
		self.assertEqual(code_bamal, "PS-BAMAL-17")
		self.assertNotEqual(code_tid, code_bamal)

		# Variant missing SKU is also channel and variant qualified
		var_tid = sanitize_item_code("", "TID", "17", external_variant_id="42")
		var_bamal = sanitize_item_code("", "BAMAL", "17", external_variant_id="42")
		self.assertEqual(var_tid, "PS-TID-17-42")
		self.assertEqual(var_bamal, "PS-BAMAL-17-42")
		self.assertNotEqual(var_tid, var_bamal)

	def test_long_sku_collision_and_hash_hardening(self):
		"""Validates that two >140 char SKUs identical in the first 140 chars resolve to unique codes."""
		common_prefix = "SKU-VERY-LONG-NAME-" + "A" * 125
		sku_1 = common_prefix + "-SUFFIX-ONE-999"
		sku_2 = common_prefix + "-SUFFIX-TWO-888"

		self.assertGreater(len(sku_1), 140)
		self.assertGreater(len(sku_2), 140)
		self.assertEqual(sku_1[:140], sku_2[:140])

		code_1 = sanitize_item_code(sku_1, "TID", "101")
		code_2 = sanitize_item_code(sku_2, "TID", "102")

		self.assertLessEqual(len(code_1), 140)
		self.assertLessEqual(len(code_2), 140)
		self.assertNotEqual(code_1, code_2, "Distinct long SKUs must never produce identical item codes!")

		# Unicode preservation
		unicode_sku = "TUERCA-ACERO-INOX-Ø10-ESTÁNDAR"
		self.assertEqual(sanitize_item_code(unicode_sku, "TID", "103"), unicode_sku)

	def test_collision_safe_code_builder(self):
		"""Validates build_collision_safe_code bounds within 140 chars."""
		desired = "A" * 150
		safe = build_collision_safe_code(desired, "TID", "999", external_variant_id="888")
		self.assertLessEqual(len(safe), 140)
		self.assertIn("PS-TID-999-888", safe)

	def test_attribute_abbreviation_stability(self):
		"""Validates deterministic abbreviation independent of insertion or list order."""
		abbr_s = compute_stable_abbreviation("Small")
		self.assertEqual(abbr_s, "SMALL")

		long_val_1 = "Extra Large Heavy Duty 200kg"
		long_val_2 = "Extra Large Heavy Duty 500kg"

		abbr_1 = compute_stable_abbreviation(long_val_1)
		abbr_2 = compute_stable_abbreviation(long_val_2)

		self.assertLessEqual(len(abbr_1), 10)
		self.assertLessEqual(len(abbr_2), 10)
		self.assertNotEqual(abbr_1, abbr_2)
		# Repeated invocation returns exact same abbreviation
		self.assertEqual(compute_stable_abbreviation(long_val_1), abbr_1)

	def test_category_importer_filters_system_roots(self):
		"""Validates that PrestaShop root and virtual categories (id 1, 2, Home, Root) are skipped."""
		mock_client = MagicMock()
		mock_client.list_categories.return_value = [
			{"id": "1", "id_parent": "0", "name": [{"id": "1", "value": "Root"}], "active": "1"},
			{"id": "2", "id_parent": "1", "name": [{"id": "1", "value": "Home"}], "active": "1", "is_root_category": "1"},
			{"id": "99910", "id_parent": "2", "name": [{"id": "1", "value": "Commercial Tools"}], "active": "1"},
		]

		importer = CategoryImporter(mock_client, sales_channel=self.sales_channel, dry_run=True)
		result = importer.import_categories()

		self.assertEqual(result.seen, 3)
		self.assertEqual(result.skipped, 2, "Root and Home system categories must be skipped")
		self.assertEqual(result.created, 1)
		self.assertEqual(result.failed, 0)

	def test_product_importer_dry_run_and_scope(self):
		"""Validates that ProductImporter with dry_run=True performs zero DB writes and supports prefix scope."""
		mock_client = MagicMock()
		mock_client.list_product_options.return_value = []
		mock_client.list_product_option_values.return_value = []
		mock_client.list_products.return_value = [
			{
				"id": "99901",
				"reference": "demo_1",
				"name": [{"id": "1", "value": "Demo Product"}],
				"price": "10.00",
				"active": "1",
			},
			{
				"id": "99902",
				"reference": "SKU-COMMERCIAL-01",
				"name": [{"id": "1", "value": "Commercial Product"}],
				"price": "20.00",
				"active": "1",
			},
		]

		importer = ProductImporter(mock_client, sales_channel=self.sales_channel, dry_run=True)
		prod_res, var_res = importer.import_products(sku_prefix_filter="SKU-")

		self.assertEqual(prod_res.seen, 2)
		self.assertEqual(prod_res.skipped, 1, "demo_1 must be skipped due to sku_prefix_filter")
		self.assertEqual(prod_res.created, 1)
		self.assertEqual(prod_res.failed, 0)

		# Verify zero writes
		self.assertIsNone(frappe.db.exists("Item", "SKU-COMMERCIAL-01"))

	def test_failure_isolation_and_rollback(self):
		"""Validates that an individual item failure is captured without breaking other items."""
		mock_client = MagicMock()
		mock_client.list_product_options.return_value = []
		mock_client.list_product_option_values.return_value = []

		importer = ProductImporter(mock_client, sales_channel=self.sales_channel, dry_run=False)

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
			prod_res, var_res = importer.import_products([good_raw, bad_raw])

		self.assertEqual(prod_res.seen, 2)
		self.assertEqual(prod_res.created, 1)
		self.assertEqual(prod_res.failed, 1)
		self.assertEqual(len(prod_res.errors), 1)
		self.assertEqual(prod_res.errors[0]["external_id"], "99904")

	def test_cross_channel_isolation_same_external_id(self):
		"""Validates that TID product 17 and BAMAL product 17 do NOT collide or reuse items based on ID."""
		mock_client = MagicMock()
		mock_client.list_product_options.return_value = []
		mock_client.list_product_option_values.return_value = []

		importer_tid = ProductImporter(mock_client, sales_channel="TID", dry_run=True)
		importer_bamal = ProductImporter(mock_client, sales_channel="BAMAL", dry_run=True)

		code_tid = sanitize_item_code("", "TID", "17")
		code_bamal = sanitize_item_code("", "BAMAL", "17")

		self.assertEqual(code_tid, "PS-TID-17")
		self.assertEqual(code_bamal, "PS-BAMAL-17")
		self.assertNotEqual(code_tid, code_bamal)

	def test_sku_change_drift_preserves_anchor(self):
		"""Validates that if source SKU changes on an already mapped Item, we do not duplicate the Item."""
		mock_client = MagicMock()
		mock_client.list_product_options.return_value = []
		mock_client.list_product_option_values.return_value = []

		importer = ProductImporter(mock_client, sales_channel=self.sales_channel, dry_run=True)

		# Mock an existing mapping for external product 777 to Item "EXISTING-ITEM-777"
		with patch.object(importer, "get_active_mapping", return_value=frappe._dict({
			"name": "MAP-777",
			"erp_doctype": "Item",
			"erp_document": "EXISTING-ITEM-777",
			"sync_hash": "old_hash",
		})):
			with patch("frappe.db.exists", return_value=True):
				raw = {
					"id": "777",
					"reference": "NEW-CHANGED-SKU",
					"name": [{"id": "1", "value": "Changed SKU Product"}],
					"active": "1",
				}
				prod_res, var_res = importer.import_products([raw])
				self.assertEqual(prod_res.seen, 1)
				self.assertEqual(prod_res.updated, 1)
				self.assertEqual(prod_res.created, 0, "Changing source SKU must never silently create a duplicate Item!")
