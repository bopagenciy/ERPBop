# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from unittest.mock import patch, MagicMock
import frappe

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
)
from bop_erp.safety import (
	ConnectorSafetyError,
	assert_safe_write_target,
)
from bop_erp.inventory.publication import (
	compute_publication_idempotency_key,
)
from bop_erp.inventory.scheduler import (
	process_multichannel_inventory_publications,
	discover_eligible_publication_channels,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig


class TestMultichannelPublicationSchedulerLive(unittest.TestCase):
	"""
	Live Integration Test Suite for Phase 1J.3:
	Multi-Channel Publication Scheduler Finalization.
	Executes against the local disposable PrestaShop test instance (http://prestashop-test).
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.sales_channel_a = "TEST-A"
		cls.sales_channel_b = "TEST-B"
		cls.sales_channel_c = "TEST-C"

		cls.item_code_a = "demo_11"       # maps to PrestaShop product 6
		cls.product_id_a = 6
		cls.stock_available_id_a = 6
		cls.baseline_qty_a = 300

		cls.item_code_b = "demo_3-PS-2-9"  # maps to PrestaShop combination 9 (product 2)
		cls.product_id_b = 2
		cls.variant_id_b = 9
		cls.stock_available_id_b = 28
		cls.baseline_qty_b = 1200

		cls.item_code_c = "demo_3-PS-2-10" # maps to PrestaShop combination 10 (product 2)
		cls.product_id_c = 2
		cls.variant_id_c = 10
		cls.stock_available_id_c = 29
		cls.baseline_qty_c = 300

		cls.unrelated_product_id = 7
		cls.unrelated_stock_available_id = 7
		cls.unrelated_baseline_qty = 300

		cls.config = PrestaShopConfig(
			sales_channel="TID",
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_PRESTASHOP_WRITE_KEY",
			verify_tls=False,
			read_enabled=True,
			write_enabled=True,
		)
		cls.client = PrestaShopClient(config=cls.config)

	@classmethod
	def tearDownClass(cls):
		"""Restores PrestaShop synthetic test stock and cleans up synthetic channels/events."""
		try:
			cls.client.update_stock_available_quantity(cls.stock_available_id_a, cls.baseline_qty_a, cls.product_id_a, None)
		except Exception:
			pass

		try:
			cls.client.update_stock_available_quantity(cls.stock_available_id_b, cls.baseline_qty_b, cls.product_id_b, cls.variant_id_b)
		except Exception:
			pass

		try:
			cls.client.update_stock_available_quantity(cls.stock_available_id_c, cls.baseline_qty_c, cls.product_id_c, cls.variant_id_c)
		except Exception:
			pass

		try:
			cls.client.update_stock_available_quantity(cls.unrelated_stock_available_id, cls.unrelated_baseline_qty, cls.unrelated_product_id, None)
		except Exception:
			pass

		cls._static_cleanup()

	@classmethod
	def _static_cleanup(cls):
		for ch in [cls.sales_channel_a, cls.sales_channel_b, cls.sales_channel_c]:
			frappe.db.sql("DELETE FROM `tabIntegration Event` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabPrestaShop Connector` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabSales Channel` WHERE name = %s", (ch,))
		frappe.db.commit()

	def setUp(self):
		self._static_cleanup()

	def tearDown(self):
		self._static_cleanup()

	def _setup_channel(
		self,
		sales_channel: str,
		item_code: str,
		product_id: int,
		variant_id: int = None,
		company: str = "Industrial DP",
		base_url: str = "http://prestashop-test",
		write_enabled: int = 1,
	):
		if not frappe.db.exists("Sales Channel", sales_channel):
			sc = frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": sales_channel,
				"channel_name": f"Test {sales_channel}",
				"active": 1,
				"company": company,
				"integration_provider": IntegrationProvider.PRESTASHOP,
			})
			sc.insert(ignore_permissions=True)

		if not frappe.db.exists("PrestaShop Connector", {"sales_channel": sales_channel}):
			pc = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": sales_channel,
				"environment": "DEVELOPMENT",
				"base_url": base_url,
				"credential_reference": "TEST_PRESTASHOP_WRITE_KEY",
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": write_enabled,
			})
			pc.flags.ignore_validate = True
			pc.insert(ignore_permissions=True)

		if not frappe.db.exists("External ID Mapping", {"sales_channel": sales_channel, "erp_document": item_code, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": sales_channel,
				"erp_doctype": "Item",
				"erp_document": item_code,
				"external_entity_type": ExternalEntityType.PRODUCT_VARIANT if variant_id else ExternalEntityType.PRODUCT,
				"external_id": str(product_id),
				"external_variant_id": str(variant_id) if variant_id else None,
				"active": 1,
			}).insert(ignore_permissions=True)

		frappe.db.commit()

	def _create_event(self, sales_channel: str, item_code: str, product_id: int, variant_id: int = None, intended_atp: float = 25.0):
		payload = {
			"item_code": item_code,
			"sales_channel": sales_channel,
			"intended_atp": intended_atp,
			"publication_version": 1,
		}
		idempotency_key = compute_publication_idempotency_key(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=sales_channel,
			item_code=item_code,
			external_id=str(product_id),
			external_variant_id=str(variant_id) if variant_id else None,
			publication_version=1,
			desired_state_hash=f"hash-{sales_channel}-{intended_atp}",
		)
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": item_code,
			"idempotency_key": idempotency_key,
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps(payload),
			"max_attempts": 3,
		})
		event.insert(ignore_permissions=True)
		frappe.db.commit()
		return event

	# ==================================================
	# 1. TWO CHANNELS LIVE TEST
	# ==================================================
	def test_01_two_channels_live_execution(self):
		"""
		1. Set initial stock for Product 6 to 5, and Combination 9 to 10.
		2. Set up TEST-A (Product 6) and TEST-B (Combination 9).
		3. Create outbound events: TEST-A -> desired 25, TEST-B -> desired 40.
		4. Run process_multichannel_inventory_publications WITHOUT passing explicit channels.
		5. Verify both channels execute and remote stock becomes 25 and 40.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id_a, 5, self.product_id_a, None)
		self.client.update_stock_available_quantity(self.stock_available_id_b, 10, self.product_id_b, self.variant_id_b)

		self._setup_channel(self.sales_channel_a, self.item_code_a, self.product_id_a, None)
		self._setup_channel(self.sales_channel_b, self.item_code_b, self.product_id_b, self.variant_id_b)

		ev_a = self._create_event(self.sales_channel_a, self.item_code_a, self.product_id_a, None, intended_atp=25.0)
		ev_b = self._create_event(self.sales_channel_b, self.item_code_b, self.product_id_b, self.variant_id_b, intended_atp=40.0)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			def mock_get_channel_atp(item_code, sales_channel):
				qty = 25.0 if sales_channel == self.sales_channel_a else 40.0
				return MagicMock(aggregate_atp_qty=qty)

			mock_atp.side_effect = mock_get_channel_atp

			res = process_multichannel_inventory_publications(client=self.client)

			self.assertEqual(res["channels_processed"], 2)
			self.assertEqual(res["published"], 2)

			# Verify remote PrestaShop quantities
			sa_a = self.client.get_stock_available(self.stock_available_id_a)
			self.assertEqual(int(sa_a.get("quantity")), 25)

			sa_b = self.client.get_stock_available(self.stock_available_id_b)
			self.assertEqual(int(sa_b.get("quantity")), 40)

			ev_a.reload()
			self.assertEqual(ev_a.status, IntegrationStatus.SUCCEEDED)
			ev_b.reload()
			self.assertEqual(ev_b.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# 2. DYNAMIC CHANNEL C ADDED AT RUNTIME WITHOUT CODE CHANGES
	# ==================================================
	def test_02_dynamic_channel_c_added_at_runtime(self):
		"""
		Proves data-driven multi-channel architecture:
		Channel TEST-C is created dynamically in MariaDB.
		Zero Python source code modifications.
		Global dispatcher discovers TEST-C and processes event successfully to remote Combination 10 = 55.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id_c, 15, self.product_id_c, self.variant_id_c)

		# Add Channel TEST-C entirely via DB
		self._setup_channel(self.sales_channel_c, self.item_code_c, self.product_id_c, self.variant_id_c)
		ev_c = self._create_event(self.sales_channel_c, self.item_code_c, self.product_id_c, self.variant_id_c, intended_atp=55.0)

		# Discover channels dynamically
		eligible = discover_eligible_publication_channels()
		self.assertIn(self.sales_channel_c, [ch["sales_channel"] for ch in eligible])

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=55.0)

			res = process_multichannel_inventory_publications(client=self.client)
			self.assertIn(self.sales_channel_c, res["channel_results"])
			self.assertEqual(res["channel_results"][self.sales_channel_c]["published"], 1)

			# Verify remote PrestaShop quantity
			sa_c = self.client.get_stock_available(self.stock_available_id_c)
			self.assertEqual(int(sa_c.get("quantity")), 55)

			ev_c.reload()
			self.assertEqual(ev_c.status, IntegrationStatus.SUCCEEDED)
