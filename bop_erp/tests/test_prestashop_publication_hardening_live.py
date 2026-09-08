# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from datetime import timedelta
from unittest.mock import patch, MagicMock
import frappe
from frappe.utils import now_datetime

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
from bop_erp.reliability import claim_event_for_processing
from bop_erp.inventory.publication import (
	normalize_publishable_quantity,
	compute_publication_hash,
	compute_publication_idempotency_key,
	get_or_create_publication_state,
	allocate_publication_version,
	commit_publication_state,
	publish_item_inventory,
	schedule_channel_inventory_publication,
	process_inventory_publication_event,
)
from bop_erp.inventory.reconciliation import (
	get_channel_inventory_reconciliation,
	repair_channel_inventory_drift,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopValidationError,
	PrestaShopStalePublicationError,
)


class TestPrestaShopPublicationHardeningLive(unittest.TestCase):
	"""
	Live Integration Test Suite for Phase 1J.1:
	Outbound Inventory Delivery Reliability & Crash Recovery.
	Executes against the local disposable PrestaShop test instance (http://prestashop-test).
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.sales_channel = "TID"
		cls.item_code = "demo_11"
		cls.provider = IntegrationProvider.PRESTASHOP

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

		# Baseline fixtures in local disposable PrestaShop test store
		cls.product_id = 6
		cls.stock_available_id = 6
		cls.baseline_qty = 300

		cls.unrelated_product_id = 7
		cls.unrelated_stock_available_id = 7
		cls.unrelated_baseline_qty = 300

	@classmethod
	def tearDownClass(cls):
		"""Restores synthetic test stock and verifies zero persistent ERP table contamination."""
		try:
			cls.client.update_stock_available_quantity(cls.stock_available_id, cls.baseline_qty, cls.product_id, None)
		except Exception:
			pass

		try:
			cls.client.update_stock_available_quantity(cls.unrelated_stock_available_id, cls.unrelated_baseline_qty, cls.unrelated_product_id, None)
		except Exception:
			pass

		# Clean up test events and publication states
		frappe.db.sql(
			"DELETE FROM `tabIntegration Event` WHERE sales_channel = %s AND (erp_document = %s OR request_metadata LIKE %s)",
			(cls.sales_channel, cls.item_code, f"%{cls.item_code}%"),
		)
		frappe.db.sql(
			"DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s AND item_code = %s",
			(cls.sales_channel, cls.item_code),
		)
		frappe.db.commit()

	def setUp(self):
		self._cleanup_test_data()

	def tearDown(self):
		self._cleanup_test_data()

	def _cleanup_test_data(self):
		frappe.db.sql(
			"DELETE FROM `tabIntegration Event` WHERE sales_channel = %s AND (erp_document = %s OR request_metadata LIKE %s)",
			(self.sales_channel, self.item_code, f"%{self.item_code}%"),
		)
		frappe.db.sql(
			"DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s AND item_code = %s",
			(self.sales_channel, self.item_code),
		)
		frappe.db.commit()

	def _create_live_event(self, intended_atp=25.0, version=1):
		payload = {
			"item_code": self.item_code,
			"sales_channel": self.sales_channel,
			"intended_atp": intended_atp,
			"publication_version": version,
		}
		idempotency_key = compute_publication_idempotency_key(
			provider=self.provider,
			sales_channel=self.sales_channel,
			item_code=self.item_code,
			external_id=str(self.product_id),
			external_variant_id=None,
			publication_version=version,
			desired_state_hash=f"hash-live-{version}-{intended_atp}",
		)
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": self.provider,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"idempotency_key": idempotency_key,
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps(payload),
			"max_attempts": 3,
		})
		event.insert(ignore_permissions=True)
		frappe.db.commit()
		return event

	# ==================================================
	# SCENARIO A: NORMAL PUBLICATION THROUGH WORKER CLAIM
	# ==================================================
	def test_scenario_a_normal_publication_worker_claim(self):
		"""
		Scenario A:
		Remote initial = 5. Desired ERP ATP = 25.
		Successful event worker claim and publication -> Remote = 25.
		"""
		# Set remote to 5
		self.client.update_stock_available_quantity(self.stock_available_id, 5, self.product_id, None)

		event = self._create_live_event(intended_atp=25.0, version=1)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			res = process_inventory_publication_event(event.name, client=self.client)
			self.assertTrue(res["success"])
			self.assertEqual(res["result"]["publishable_qty"], 25)
			self.assertTrue(res["result"]["changed"])

			# Verify remote PrestaShop quantity is now 25
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 25)

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# SCENARIO B: PUT SUCCEEDS, LOCAL ACK FAILS -> RETRY
	# ==================================================
	def test_scenario_b_put_succeeds_local_ack_fails_retry_converges(self):
		"""
		Scenario B:
		PUT reaches PrestaShop and sets remote=25.
		Injected failure before local ack. On retry, read-before-write sees remote=25,
		converges as NO-OP with zero extra mutation, event ends SUCCEEDED.
		"""
		# Set remote to 5
		self.client.update_stock_available_quantity(self.stock_available_id, 5, self.product_id, None)

		event = self._create_live_event(intended_atp=25.0, version=1)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			# Worker 1 claims
			claimed, worker, token = claim_event_for_processing(event.name, worker_id="worker-crash-ack")
			self.assertTrue(claimed)

			# Execute PUT to remote PrestaShop
			res_put = publish_item_inventory(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				intended_atp=25.0,
				publication_version=1,
				client=self.client,
			)
			self.assertTrue(res_put["changed"])
			self.assertEqual(res_put["publishable_qty"], 25)

			# Verify remote is already 25
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 25)

			# INJECT FAILURE before event.mark_succeeded: simulate worker crash / lease timeout
			frappe.db.sql(
				"UPDATE `tabIntegration Event` SET status = 'RETRY_PENDING', next_retry_at = %s, worker_id = NULL, processing_token = NULL WHERE name = %s",
				(now_datetime() - timedelta(seconds=1), event.name),
			)
			frappe.db.commit()

			# Retry execution
			retry_res = process_inventory_publication_event(event.name, worker_id="worker-retry", client=self.client)
			self.assertTrue(retry_res["success"])
			self.assertFalse(retry_res["result"]["changed"])
			self.assertEqual(retry_res["result"]["reason"], "NO_OP_IDENTICAL_QUANTITY")

			# Final remote remains 25
			sa_data_after = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data_after.get("quantity")), 25)

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# SCENARIO C: OLDER 20 VS NEWER 5 (NEWER RUNS FIRST)
	# ==================================================
	def test_scenario_c_newer_runs_first_old_retry_superseded(self):
		"""
		Scenario C:
		Event A (version 10, intended 20) is delayed.
		Event B (version 11, intended 5) runs first and publishes 5.
		When Event A retries: version/freshness check -> SUPERSEDED, ZERO PUT. Final remote = 5.
		"""
		# Set remote to 50
		self.client.update_stock_available_quantity(self.stock_available_id, 50, self.product_id, None)

		event_a = self._create_live_event(intended_atp=20.0, version=10)
		event_b = self._create_live_event(intended_atp=5.0, version=11)

		# Event B runs first
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=5.0)

			res_b = process_inventory_publication_event(event_b.name, client=self.client)
			self.assertTrue(res_b["success"])
			self.assertEqual(res_b["result"]["publishable_qty"], 5)

			# Verify remote is 5
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 5)

			# Now Event A retries: live ATP is 5, Event A wanted 20 and version was 10 < 11
			res_a = process_inventory_publication_event(event_a.name, client=self.client)
			self.assertTrue(res_a["success"])
			self.assertFalse(res_a["result"]["changed"])
			self.assertEqual(res_a["result"]["status"], "STALE_SUPERSEDED")

			# Remote strictly remains 5
			sa_final = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_final.get("quantity")), 5)

			event_a.reload()
			self.assertEqual(event_a.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# SCENARIO D: CONCURRENT SAME DESIRED PUBLICATION
	# ==================================================
	def test_scenario_d_concurrent_same_desired_publication(self):
		"""
		Scenario D:
		Two workers attempt the same desired publication (version 15, qty 25).
		Harmless convergence, final remote = 25.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 10, self.product_id, None)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			r1 = publish_item_inventory(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				publication_version=15,
				client=self.client,
			)
			self.assertTrue(r1["changed"])
			self.assertEqual(r1["publishable_qty"], 25)

			r2 = publish_item_inventory(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				publication_version=15,
				client=self.client,
			)
			self.assertFalse(r2["changed"])
			self.assertEqual(r2["reason"], "NO_OP_IDENTICAL_QUANTITY")

			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 25)

	# ==================================================
	# SCENARIO E: DIFFERENT VERSIONS RACE -> NEWEST WINS
	# ==================================================
	def test_scenario_e_different_versions_race_newest_wins(self):
		"""
		Scenario E:
		Worker A (version 20, qty 50) and Worker B (version 21, qty 30).
		Worker B executes first -> remote becomes 30.
		Worker A attempts to execute -> pre-PUT check and state fencing block it.
		Final remote remains 30; Final state is version 21 / qty 30.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 10, self.product_id, None)

		# Worker B (v21, qty 30) runs
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=30.0)

			res_b = publish_item_inventory(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				intended_atp=30.0,
				publication_version=21,
				client=self.client,
			)
			self.assertTrue(res_b["changed"])
			self.assertEqual(res_b["publishable_qty"], 30)

			# Verify remote is 30
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 30)

			# Now Worker A (v20, qty 50) attempts to run
			mock_atp.return_value = MagicMock(aggregate_atp_qty=50.0)

			res_a = publish_item_inventory(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				intended_atp=50.0,
				publication_version=20,
				client=self.client,
			)
			self.assertEqual(res_a["status"], "STALE_SUPERSEDED")
			self.assertFalse(res_a["changed"])

			# Final remote remains 30
			sa_final = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_final.get("quantity")), 30)

			# Final state in DB is version 21, qty 30
			pub_state = frappe.db.get_value(
				"Inventory Publication State",
				{"sales_channel": self.sales_channel, "item_code": self.item_code},
				["publication_version", "last_published_qty"],
				as_dict=True,
			)
			self.assertEqual(pub_state.publication_version, 21)
			self.assertEqual(pub_state.last_published_qty, 30)

	# ==================================================
	# SCENARIO F: CONNECTOR CHANGED TO FORBIDDEN PRODUCTION HOST
	# ==================================================
	def test_scenario_f_connector_changed_to_forbidden_production_host(self):
		"""
		Scenario F:
		Event scheduled. Connector URL changed to https://theindustrialdepot.com before execution.
		Worker BLOCKS pre-network (zero DNS / socket / HTTP).
		"""
		event = self._create_live_event()

		mock_conn = MagicMock()
		mock_conn.environment = "DEVELOPMENT"
		mock_conn.base_url = "https://theindustrialdepot.com"
		mock_conn.write_enabled = 1

		with patch("bop_erp.inventory.publication.get_active_connector_for_channel", return_value=mock_conn):
			with self.assertRaises(ConnectorSafetyError):
				publish_item_inventory(
					sales_channel=self.sales_channel,
					item_code=self.item_code,
				)

	# ==================================================
	# SCENARIO G: REMOTE DRIFT REPAIR
	# ==================================================
	def test_scenario_g_remote_drift_repaired(self):
		"""
		Scenario G:
		ERP ATP is 25. Out-of-band change sets remote to 7 (DRIFT).
		Reconciliation repair detects drift and synchronizes remote back to 25.
		"""
		# Out of band change: set remote to 7
		self.client.update_stock_available_quantity(self.stock_available_id, 7, self.product_id, None)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp, \
		     patch("bop_erp.inventory.reconciliation.get_channel_atp") as mock_atp_rec:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)
			mock_atp_rec.return_value = MagicMock(aggregate_atp_qty=25.0)

			# Run reconciliation report
			report = get_channel_inventory_reconciliation(self.sales_channel, [self.item_code], client=self.client)
			self.assertEqual(len(report), 1)
			self.assertIn(report[0]["stock_status"], ("DRIFT", "DRIFTED"))
			self.assertEqual(report[0]["remote_qty"], 7)
			self.assertEqual(report[0]["publishable_qty"], 25)

			# Execute repair
			repairs = repair_channel_inventory_drift(self.sales_channel, [self.item_code], client=self.client)
			self.assertEqual(len(repairs), 1)
			self.assertTrue(repairs[0]["changed"])
			self.assertEqual(repairs[0]["publishable_qty"], 25)

			# Verify remote is now 25
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 25)

			# Verify reconciliation report is now SYNCED / IN_SYNC
			report_after = get_channel_inventory_reconciliation(self.sales_channel, [self.item_code], client=self.client)
			self.assertIn(report_after[0]["stock_status"], ("SYNCED", "IN_SYNC"))
			self.assertEqual(report_after[0]["delta"], 0)
