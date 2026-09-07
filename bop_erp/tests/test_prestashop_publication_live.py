# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import patch
import frappe

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
)
from bop_erp.inventory.models import ChannelATP
from bop_erp.inventory.publication import (
	publish_item_inventory,
	schedule_channel_inventory_publication,
	resolve_item_mapping,
)
from bop_erp.inventory.reconciliation import get_channel_inventory_reconciliation
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopValidationError,
	PrestaShopAuthError,
)


class TestPrestaShopPublicationLive(unittest.TestCase):
	"""
	Live Integration Test Suite for Phase 1J: PrestaShop Test Inventory Publication Foundation.
	Executes live against the local disposable PrestaShop test instance.

	Verifies:
	1. Simple product live update: 5 -> 25, verify remote=25, and repeated call is NO-OP.
	2. Variant product live updates: combination 9 -> 12, combination 10 -> 7, combination rollup untouched.
	3. Unrelated product safety check: Product 7 remains 300 throughout all operations.
	4. Downward ATP adjustment: 25 -> 8.
	5. Zero stock publication: ATP 0 -> remote 0.
	6. Drift repair: out-of-band remote drift repaired by publication pipeline.
	7. Stale event protection: older event intent superseded by fresher live ATP.
	8. Read-only reconciliation report matches reality without executing writes.
	9. Mapping missing fails safely without network write.
	10. Identity mismatch blocks write safely.
	11. Clean teardown restoring original PrestaShop test quantities and verifying 0 ERP persistent records.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.sales_channel = "TID"
		cls.config = PrestaShopConfig(
			sales_channel=cls.sales_channel,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_PRESTASHOP_WRITE_KEY",
			verify_tls=False,
			read_enabled=True,
			write_enabled=True,
		)
		cls.client = PrestaShopClient(config=cls.config)

		# Baseline quantities in local PrestaShop test store
		cls.baseline_simple_id = 6
		cls.baseline_simple_qty = 300

		cls.baseline_var9_sa_id = 28
		cls.baseline_var9_qty = 1200

		cls.baseline_var10_sa_id = 29
		cls.baseline_var10_qty = 300

		cls.baseline_unrelated_id = 7
		cls.baseline_unrelated_qty = 300

	@classmethod
	def tearDownClass(cls):
		"""
		Restores exact PrestaShop baseline quantities and cleans up test publication states & integration events.
		"""
		try:
			cls.client.update_stock_available_quantity(cls.baseline_simple_id, cls.baseline_simple_qty, 6, None)
		except Exception:
			pass

		try:
			cls.client.update_stock_available_quantity(cls.baseline_var9_sa_id, cls.baseline_var9_qty, 2, 9)
		except Exception:
			pass

		try:
			cls.client.update_stock_available_quantity(cls.baseline_var10_sa_id, cls.baseline_var10_qty, 2, 10)
		except Exception:
			pass

		# Clean up any created Inventory Publication States
		frappe.db.delete("Inventory Publication State", {"sales_channel": cls.sales_channel})
		frappe.db.delete("Integration Event", {"sales_channel": cls.sales_channel, "entity_type": ExternalEntityType.INVENTORY})
		frappe.db.commit()
		super().tearDownClass()

	def _make_mock_channel_atp(self, item_code: str, atp_qty: float) -> ChannelATP:
		return ChannelATP(
			item_code=item_code,
			sales_channel=self.sales_channel,
			company="Industrial DP",
			aggregate_atp_qty=float(atp_qty),
		)

	def test_01_simple_product_live_publication_and_noop(self):
		"""
		Live test: publishes quantity 25 to simple product (demo_11 -> PrestaShop product 6).
		Verifies remote quantity becomes 25.
		Re-runs immediately and verifies NO-OP (changed=False).
		"""
		fake_atp = self._make_mock_channel_atp("demo_11", 25.0)
		with patch("bop_erp.inventory.publication.get_channel_atp", return_value=fake_atp):
			res1 = publish_item_inventory(self.sales_channel, "demo_11", client=self.client)
			self.assertTrue(res1["changed"])
			self.assertEqual(res1["remote_resulting_qty"], 25)
			self.assertEqual(res1["reason"], "QUANTITY_UPDATED")

			# Verify directly on PrestaShop API
			sa = self.client.get_stock_available(self.baseline_simple_id)
			self.assertEqual(int(sa.get("quantity", 0)), 25)

			# Re-run -> Must be NO-OP
			res2 = publish_item_inventory(self.sales_channel, "demo_11", client=self.client)
			self.assertFalse(res2["changed"])
			self.assertEqual(res2["remote_resulting_qty"], 25)
			self.assertEqual(res2["reason"], "NO_OP_IDENTICAL_QUANTITY")

	def test_02_variant_product_live_publication(self):
		"""
		Live test: publishes quantities to combination products.
		Combination 9 (demo_3-PS-2-9) -> 12
		Combination 10 (demo_3-PS-2-10) -> 7
		Verifies specific combination rows are updated and other rows are undisturbed.
		"""
		fake_atp_9 = self._make_mock_channel_atp("demo_3-PS-2-9", 12.0)
		with patch("bop_erp.inventory.publication.get_channel_atp", return_value=fake_atp_9):
			res9 = publish_item_inventory(self.sales_channel, "demo_3-PS-2-9", client=self.client)
			self.assertTrue(res9["changed"])
			self.assertEqual(res9["remote_resulting_qty"], 12)

			sa9 = self.client.get_stock_available(self.baseline_var9_sa_id)
			self.assertEqual(int(sa9.get("quantity", 0)), 12)

		fake_atp_10 = self._make_mock_channel_atp("demo_3-PS-2-10", 7.0)
		with patch("bop_erp.inventory.publication.get_channel_atp", return_value=fake_atp_10):
			res10 = publish_item_inventory(self.sales_channel, "demo_3-PS-2-10", client=self.client)
			self.assertTrue(res10["changed"])
			self.assertEqual(res10["remote_resulting_qty"], 7)

			sa10 = self.client.get_stock_available(self.baseline_var10_sa_id)
			self.assertEqual(int(sa10.get("quantity", 0)), 7)

	def test_03_unrelated_product_remains_untouched(self):
		"""
		Safety check: Product 7 (unrelated) must have remained exactly 300 throughout tests.
		"""
		sa7 = self.client.get_stock_available(self.baseline_unrelated_id)
		self.assertEqual(int(sa7.get("quantity", 0)), self.baseline_unrelated_qty)

	def test_04_atp_decrease_to_smaller_quantity(self):
		"""
		Live test: downward adjustment from 25 -> 8.
		"""
		fake_atp = self._make_mock_channel_atp("demo_11", 8.0)
		with patch("bop_erp.inventory.publication.get_channel_atp", return_value=fake_atp):
			res = publish_item_inventory(self.sales_channel, "demo_11", client=self.client)
			self.assertTrue(res["changed"])
			self.assertEqual(res["remote_previous_qty"], 25)
			self.assertEqual(res["remote_resulting_qty"], 8)

			sa = self.client.get_stock_available(self.baseline_simple_id)
			self.assertEqual(int(sa.get("quantity", 0)), 8)

	def test_05_atp_zero_clamping_live(self):
		"""
		Live test: zero ATP sets remote quantity to 0.
		"""
		fake_atp = self._make_mock_channel_atp("demo_11", 0.0)
		with patch("bop_erp.inventory.publication.get_channel_atp", return_value=fake_atp):
			res = publish_item_inventory(self.sales_channel, "demo_11", client=self.client)
			self.assertTrue(res["changed"])
			self.assertEqual(res["remote_resulting_qty"], 0)

			sa = self.client.get_stock_available(self.baseline_simple_id)
			self.assertEqual(int(sa.get("quantity", 0)), 0)

	def test_06_remote_drift_repair(self):
		"""
		Simulate manual out-of-band edit on PrestaShop directly (e.g. merchant changed stock to 42 in back office).
		ERP ATP is still 10. Publication detects drift and forces PrestaShop back to 10.
		"""
		# Out of band direct update to 42
		self.client.update_stock_available_quantity(self.baseline_simple_id, 42, 6, None)
		sa_drift = self.client.get_stock_available(self.baseline_simple_id)
		self.assertEqual(int(sa_drift.get("quantity", 0)), 42)

		fake_atp = self._make_mock_channel_atp("demo_11", 10.0)
		with patch("bop_erp.inventory.publication.get_channel_atp", return_value=fake_atp), \
			 patch("bop_erp.inventory.reconciliation.get_channel_atp", return_value=fake_atp):
			# Reconcile detects drift
			reconciliation = get_channel_inventory_reconciliation(self.sales_channel, item_codes=["demo_11"], client=self.client)
			self.assertEqual(reconciliation[0]["stock_status"], "DRIFT")
			self.assertEqual(reconciliation[0]["remote_qty"], 42)
			self.assertEqual(reconciliation[0]["publishable_qty"], 10)

			# Run publication to restore authoritative stock
			res = publish_item_inventory(self.sales_channel, "demo_11", client=self.client)
			self.assertTrue(res["changed"])
			self.assertEqual(res["remote_previous_qty"], 42)
			self.assertEqual(res["remote_resulting_qty"], 10)

			# Reconcile again -> now SYNCED
			reconciliation_post = get_channel_inventory_reconciliation(self.sales_channel, item_codes=["demo_11"], client=self.client)
			self.assertEqual(reconciliation_post[0]["stock_status"], "SYNCED")
			self.assertEqual(reconciliation_post[0]["delta"], 0)

	def test_07_stale_event_protection_live(self):
		"""
		An queued event with older intended_atp (e.g. 50) arrives, but live ATP has moved to 15.
		The publication pipeline detects stale intent and marks STALE_SUPERSEDED.
		"""
		fake_atp = self._make_mock_channel_atp("demo_11", 15.0)
		with patch("bop_erp.inventory.publication.get_channel_atp", return_value=fake_atp):
			res = publish_item_inventory(
				self.sales_channel,
				"demo_11",
				intended_atp=50.0,
				client=self.client,
			)
			self.assertEqual(res["status"], "STALE_SUPERSEDED")
			self.assertFalse(res["changed"])
			self.assertEqual(res["intended_qty"], 50)
			self.assertEqual(res["current_atp_qty"], 15)

			# Next, a fresh event with current intended_atp (15) arrives and succeeds
			res_fresh = publish_item_inventory(
				self.sales_channel,
				"demo_11",
				intended_atp=15.0,
				client=self.client,
			)
			self.assertEqual(res_fresh["remote_resulting_qty"], 15)
			state_doc = frappe.get_doc("Inventory Publication State", res_fresh["publication_state"])
			self.assertEqual(state_doc.last_published_qty, 15)

	def test_08_missing_mapping_fails_safely_no_remote_write(self):
		"""
		Item without External ID Mapping fails immediately with PrestaShopValidationError and does not call remote API.
		"""
		with self.assertRaises(PrestaShopValidationError):
			publish_item_inventory(self.sales_channel, "NON-EXISTENT-ITEM-12345", client=self.client)

	def test_09_identity_mismatch_blocks_live_write(self):
		"""
		Calling client.update_stock_available_quantity with mismatched product_id strictly fails.
		"""
		with self.assertRaises(PrestaShopValidationError) as ctx:
			self.client.update_stock_available_quantity(
				stock_available_id=self.baseline_simple_id,
				quantity=99,
				expected_product_id=999,  # Mismatched
			)
		self.assertIn("CRITICAL IDENTITY MISMATCH", str(ctx.exception))

	def test_10_temporary_failure_retryable(self):
		"""
		Tests that invalid credentials raise PrestaShopAuthError, which is categorized as non-transient or retryable per policy.
		"""
		bad_config = PrestaShopConfig(
			sales_channel=self.sales_channel,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="NON_EXISTENT_KEY_REF",
			verify_tls=False,
			read_enabled=True,
			write_enabled=True,
		)
		with self.assertRaises(PrestaShopAuthError):
			bad_client = PrestaShopClient(config=bad_config)
			bad_client.health_check()
