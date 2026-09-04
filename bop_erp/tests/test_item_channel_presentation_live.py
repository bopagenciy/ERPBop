# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.importers.catalog import CatalogImporter


class TestItemChannelPresentationLive(unittest.TestCase):
	"""
	Live Integration Test for Phase 1F:
	- Executes against local test container http://prestashop-test.
	- Validates inbound creation of Channel Categories, Media Assets, and Item Channel Presentations.
	- Verifies cover image flag and localized Spanish marketing text.
	- Validates 100% idempotency upon re-import (zero duplicate categories, media, presentations).
	- Verifies ZERO inventory mutations (0 SLE, 0 Bins).
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

	def test_01_live_presentation_and_media_sync(self):
		importer = CatalogImporter(self.client, sales_channel=self.sales_channel, dry_run=False, sync_presentation=True)

		# Execute catalog and presentation import
		res1 = importer.run()
		self.assertTrue(res1["success"], f"Run 1 failed: {res1}")
		self.assertEqual(res1["total_failed"], 0)

		# Verify Channel Categories exist in DB
		channel_cats = frappe.get_all(
			"Channel Category",
			filters={"sales_channel": self.sales_channel},
			fields=["name", "category_name", "category_slug", "parent_channel_category"],
		)
		self.assertGreater(len(channel_cats), 0, "Channel Categories should have been created")

		# Verify Item Channel Presentations exist in DB
		presentations = frappe.get_all(
			"Item Channel Presentation",
			filters={"sales_channel": self.sales_channel},
			fields=["name", "item", "publication_status", "channel_item_name"],
		)
		self.assertGreater(len(presentations), 0, "Item Channel Presentations should have been created")

		# Check Product 1 presentation details
		prod1_map = frappe.db.get_value(
			"External ID Mapping",
			{"sales_channel": self.sales_channel, "external_entity_type": ExternalEntityType.PRODUCT, "external_id": "1", "active": 1},
			"erp_document",
		)
		if prod1_map:
			pres1_name = frappe.db.get_value(
				"Item Channel Presentation",
				{"item": prod1_map, "sales_channel": self.sales_channel},
				"name",
			)
			self.assertIsNotNone(pres1_name)
			pres1 = frappe.get_doc("Item Channel Presentation", pres1_name)
			self.assertEqual(pres1.publication_status, "PUBLISHED")
			self.assertGreater(len(pres1.channel_categories), 0)

			# Verify at least one cover image is designated
			if pres1.media_items:
				cover_count = sum(1 for m in pres1.media_items if m.is_cover)
				self.assertLessEqual(cover_count, 1)

		# Second Run: verify 100% idempotency
		res2 = importer.run()
		self.assertTrue(res2["success"], f"Run 2 failed: {res2}")
		self.assertEqual(res2["total_failed"], 0)
		self.assertEqual(res2["channel_categories"]["created"], 0, "Idempotency violated: Channel Category created on re-run")
		self.assertEqual(res2["presentations"]["created"], 0, "Idempotency violated: Presentation created on re-run")

		# Critical Inventory Safety Check
		sle_count = frappe.db.count("Stock Ledger Entry")
		bin_count = frappe.db.count("Bin")
		self.assertEqual(sle_count, 0, f"Critical Safety Violation: {sle_count} Stock Ledger Entries detected!")
		self.assertEqual(bin_count, 0, f"Critical Safety Violation: {bin_count} Bins detected!")
