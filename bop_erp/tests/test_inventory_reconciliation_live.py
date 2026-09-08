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
from bop_erp.reliability import (
	claim_event_for_processing,
	verify_processing_authority,
	recover_stale_events,
)
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
from bop_erp.inventory.scheduler import (
	process_pending_inventory_publications,
	process_multichannel_inventory_publications,
)
from bop_erp.inventory.reconciliation import (
	get_canonical_publishable_quantity,
	get_channel_inventory_reconciliation,
	reconcile_inventory_item,
	reconcile_channel_inventory,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopValidationError,
	PrestaShopStalePublicationError,
	PrestaShopServerError,
)


class TestInventoryReconciliationLive(unittest.TestCase):
	"""
	Live Integration Test Suite for Phase 1L:
	Outbound Inventory Publication Reconciliation, Drift Detection & Crash-Safe Recovery.
	Executes against the local disposable PrestaShop test instance (http://prestashop-test).

	Verifies:
	TEST A — Normal Convergence (Outbox intent -> independent worker -> PrestaShop converges)
	TEST B — Lost Wake Signal (Suppressed after_commit wake -> periodic scheduler discovers and drains -> converges)
	TEST C — Worker Crash & Lease Recovery (Worker A stalls -> lease expires -> Worker B reclaims and publishes -> stale Worker A rejected)
	TEST D — Manual External Drift (Remote altered out-of-band -> reconciler detects DRIFTED -> durable outbox scheduled -> worker converges)
	TEST E — Rapid Successive ERP Changes (Multiple rapid changes -> outbox coalesces -> final remote equals latest ERP truth)
	TEST F — Two-Channel Independence (One channel fails safety/network -> healthy channel still converges)
	TEST G — No Residual Test Data (Clean DB hygiene and baseline stock restored)
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

		cls.product_id_pliers = 21
		cls.stock_available_id_pliers = 60
		cls.baseline_qty_pliers = 120

		# Ensure TID sales channel and PrestaShop connector exist and are enabled
		if not frappe.db.exists("Sales Channel", cls.sales_channel):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.sales_channel,
				"channel_name": "TID Channel",
				"active": 1,
				"company": "Industrial DP",
				"integration_provider": cls.provider,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("PrestaShop Connector", {"sales_channel": cls.sales_channel}):
			pc = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": cls.sales_channel,
				"environment": "DEVELOPMENT",
				"base_url": "http://prestashop-test",
				"credential_reference": "TEST_PRESTASHOP_WRITE_KEY",
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 1,
			})
			pc.flags.ignore_validate = True
			pc.insert(ignore_permissions=True)
		else:
			frappe.db.set_value("PrestaShop Connector", {"sales_channel": cls.sales_channel}, {"write_enabled": 1, "enabled": 1})

		if not frappe.db.exists("External ID Mapping", {"sales_channel": cls.sales_channel, "erp_document": cls.item_code, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": cls.sales_channel,
				"entity_type": ExternalEntityType.INVENTORY,
				"erp_doctype": "Item",
				"erp_document": cls.item_code,
				"external_id": str(cls.stock_available_id),
				"external_parent_id": str(cls.product_id),
				"active": 1,
			}).insert(ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		"""Restores synthetic test stock and verifies zero persistent ERP table contamination."""
		try:
			cls.client.update_stock_available_quantity(cls.stock_available_id, cls.baseline_qty, cls.product_id, None)
			cls.client.update_stock_available_quantity(cls.stock_available_id_pliers, cls.baseline_qty_pliers, cls.product_id_pliers, None)
		except Exception:
			pass

		# Clean up test events and publication states
		frappe.db.sql(
			"DELETE FROM `tabIntegration Event` WHERE sales_channel = %s AND (erp_document IN (%s, %s) OR request_metadata LIKE %s)",
			(cls.sales_channel, cls.item_code, "SKU-TOOL-PLIERS-8IN", f"%{cls.item_code}%"),
		)
		frappe.db.sql(
			"DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s AND item_code IN (%s, %s)",
			(cls.sales_channel, cls.item_code, "SKU-TOOL-PLIERS-8IN"),
		)
		frappe.db.commit()
		super().tearDownClass()

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

	def _create_live_event(self, intended_atp: float = 50.0, version: int = 1):
		item_code = self.item_code
		sales_channel = self.sales_channel
		pub_qty = normalize_publishable_quantity(intended_atp)
		desired_hash = compute_publication_hash(
			provider=self.provider,
			sales_channel=sales_channel,
			item_code=item_code,
			external_product_id=self.product_id,
			external_variant_id=None,
			stock_available_id=self.stock_available_id,
			publishable_quantity=pub_qty,
		)
		idempotency_key = compute_publication_idempotency_key(
			provider=self.provider,
			sales_channel=sales_channel,
			item_code=item_code,
			external_id=str(self.product_id),
			external_variant_id=None,
			publication_version=version,
			desired_state_hash=desired_hash,
		)
		payload_dict = {
			"item_code": item_code,
			"sales_channel": sales_channel,
			"intended_atp": intended_atp,
			"publication_version": version,
			"desired_state_hash": desired_hash,
		}
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": self.provider,
			"sales_channel": sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": item_code,
			"idempotency_key": idempotency_key,
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps(payload_dict),
			"max_attempts": 3,
		})
		event.flags.ignore_permissions = True
		event.insert()
		frappe.db.commit()
		return event

	# ==================================================
	# TEST A — NORMAL CONVERGENCE
	# ==================================================
	def test_a_normal_convergence(self):
		"""
		1. Remote stock = 10. Desired ERP ATP = 25.
		2. Outbox event scheduled.
		3. Independent worker processes event.
		4. PrestaShop stock is verified at 25.
		5. Event marked SUCCEEDED.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 10, self.product_id, None)

		event = self._create_live_event(intended_atp=25.0, version=1)
		self.assertEqual(event.status, IntegrationStatus.PENDING)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			res = process_inventory_publication_event(event.name, client=self.client)
			self.assertTrue(res["success"])
			self.assertEqual(res["result"]["publishable_qty"], 25)

			# Remote stock updated to 25
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 25)

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# TEST B — LOST WAKE SIGNAL RECOVERY
	# ==================================================
	def test_b_lost_wake_signal_recovery(self):
		"""
		1. Remote stock = 10. Desired ERP ATP = 40.
		2. Persist durable outbox event in MariaDB without waking dispatcher.
		3. Run periodic recovery scheduler (process_multichannel_inventory_publications).
		4. Event is discovered from DB, worker publishes.
		5. Remote PrestaShop stock converges to 40.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 10, self.product_id, None)

		event = self._create_live_event(intended_atp=40.0, version=2)
		self.assertEqual(event.status, IntegrationStatus.PENDING)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=40.0)

			# Periodic scheduler discovers pending outbox event and processes it
			sched_res = process_multichannel_inventory_publications(client=self.client)
			self.assertGreaterEqual(sched_res["published"], 1)

			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 40)

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# TEST C — WORKER CRASH / LEASE RECOVERY
	# ==================================================
	def test_c_worker_crash_and_lease_recovery(self):
		"""
		1. Worker A claims event.
		2. Simulate crash / stall by expiring lease in DB.
		3. recover_stale_events or second claim reclaims event.
		4. Worker B publishes successfully (remote = 35).
		5. Stale Worker A resumes and attempts to mark Completed -> Fencing rejects Worker A.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 10, self.product_id, None)

		event = self._create_live_event(intended_atp=35.0, version=3)

		# Worker A claims
		claimed_a, worker_a, token_a = claim_event_for_processing(event.name, worker_id="worker-crash-A")
		self.assertTrue(claimed_a)

		# Simulate Worker A stall / crash: expire lease in DB
		frappe.db.sql(
			"UPDATE `tabIntegration Event` SET lease_expires_at = %s WHERE name = %s",
			(now_datetime() - timedelta(minutes=5), event.name),
		)
		frappe.db.commit()

		# Run recover_stale_events
		recover_stale_events()
		event.reload()
		self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)

		# Make due for immediate retry
		frappe.db.sql(
			"UPDATE `tabIntegration Event` SET next_retry_at = %s WHERE name = %s",
			(now_datetime() - timedelta(seconds=1), event.name),
		)
		frappe.db.commit()

		# Worker B reclaims and publishes
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=35.0)

			res_b = process_inventory_publication_event(event.name, worker_id="worker-B", client=self.client)
			self.assertTrue(res_b["success"])

			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 35)

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

			# Now stale Worker A resumes and attempts to write state
			is_auth, auth_reason = verify_processing_authority(event.name, token_a)
			self.assertFalse(is_auth, "Stale Worker A must not possess authority")
			self.assertTrue("SUCCEEDED" in auth_reason or "Fencing violation" in auth_reason, f"Expected authority loss, got: {auth_reason}")

	# ==================================================
	# TEST D — MANUAL EXTERNAL DRIFT DETECTION & REPAIR
	# ==================================================
	def test_d_manual_external_drift_detection_and_repair(self):
		"""
		1. Canonical ERP ATP = 50.
		2. Manually alter PrestaShop TEST to 22 (DRIFT).
		3. Reconciler detects DRIFTED (delta = +28).
		4. Reconciler enqueues repair into durable outbox.
		5. Worker publishes outbox event.
		6. PrestaShop returns to 50; reconciler confirms IN_SYNC.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 22, self.product_id, None)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp, 		     patch("bop_erp.inventory.reconciliation.get_channel_atp") as mock_atp_rec:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=50.0)
			mock_atp_rec.return_value = MagicMock(aggregate_atp_qty=50.0)

			# Reconciler detects drift and schedules repair through outbox
			rec_result = reconcile_inventory_item(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				repair=True,
				client=self.client,
			)

			self.assertEqual(rec_result["stock_status"], "DRIFTED")
			self.assertEqual(rec_result["delta"], 28)
			self.assertTrue(rec_result["repair_enqueued"])
			repair_ev_name = rec_result["repair_event"]
			self.assertIsNotNone(repair_ev_name)

			# Worker executes the repair event
			res = process_inventory_publication_event(repair_ev_name, client=self.client)
			self.assertTrue(res["success"])

			# PrestaShop remote stock is now restored to 50
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 50)

			# Re-check reconciler confirms IN_SYNC
			re_check = reconcile_inventory_item(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				repair=False,
				client=self.client,
			)
			self.assertEqual(re_check["stock_status"], "IN_SYNC")
			self.assertEqual(re_check["delta"], 0)

	# ==================================================
	# TEST E — RAPID SUCCESSIVE ERP CHANGES CONVERGENCE
	# ==================================================
	def test_e_rapid_successive_erp_changes_convergence(self):
		"""
		ERP desired states: 120 -> 119 -> 118 while work remains pending.
		Outbox coalesces to latest intent.
		Worker executes: final PrestaShop value MUST equal 118.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 120, self.product_id, None)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			# State 1: 120
			mock_atp.return_value = MagicMock(aggregate_atp_qty=120.0)
			evs1 = schedule_channel_inventory_publication(self.sales_channel, [self.item_code])

			# State 2: 119
			mock_atp.return_value = MagicMock(aggregate_atp_qty=119.0)
			evs2 = schedule_channel_inventory_publication(self.sales_channel, [self.item_code])

			# State 3: 118
			mock_atp.return_value = MagicMock(aggregate_atp_qty=118.0)
			evs3 = schedule_channel_inventory_publication(self.sales_channel, [self.item_code])

			# Coalescing: all three calls coalesce to the same single pending event
			self.assertEqual(evs1[0], evs2[0])
			self.assertEqual(evs2[0], evs3[0])
			coalesced_ev = evs3[0]

			# Run worker
			res = process_inventory_publication_event(coalesced_ev, client=self.client)
			self.assertTrue(res["success"])
			self.assertEqual(res["result"]["publishable_qty"], 118)

			# Final PrestaShop remote stock is exactly 118
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 118)

	# ==================================================
	# TEST F — TWO CHANNEL INDEPENDENCE
	# ==================================================
	def test_f_two_channel_independence(self):
		"""
		Channel A and Channel TID both have pending publication intents.
		Channel A fails transiently (simulated 503 error).
		Channel TID succeeds.
		Channel TID converges cleanly despite Channel A's failure.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 10, self.product_id, None)

		# Ensure UNHEALTHY-CHAN exists for foreign key link
		if not frappe.db.exists("Sales Channel", "UNHEALTHY-CHAN"):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": "UNHEALTHY-CHAN",
				"channel_name": "Unhealthy Channel",
				"active": 1,
				"company": "Industrial DP",
				"integration_provider": self.provider,
			}).insert(ignore_permissions=True)
			frappe.db.commit()

		# Create pending event for Channel TID
		event_tid = self._create_live_event(intended_atp=25.0, version=10)

		# Create failing event for Channel UNHEALTHY
		event_unhealthy = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": self.provider,
			"sales_channel": "UNHEALTHY-CHAN",
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"idempotency_key": "KEY-UNHEALTHY-CHAN-FAIL",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({"item_code": self.item_code, "sales_channel": "UNHEALTHY-CHAN"}),
			"max_attempts": 3,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		try:
			# Mock publish_item_inventory to fail only on UNHEALTHY-CHAN
			orig_publish = publish_item_inventory

			def selective_publish(*args, **kwargs):
				ch = kwargs.get("sales_channel") or (args[0] if len(args) > 0 else "")
				if ch == "UNHEALTHY-CHAN":
					raise PrestaShopServerError("Channel temporary 503 unavailable", status_code=503)
				return orig_publish(*args, **kwargs)

			with patch("bop_erp.inventory.publication.publish_item_inventory", side_effect=selective_publish), 			     patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
				mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

				# Process unhealthy event -> fails into RETRY_PENDING
				res_unhealthy = process_inventory_publication_event(event_unhealthy.name)
				self.assertFalse(res_unhealthy["success"])
				event_unhealthy.reload()
				self.assertEqual(event_unhealthy.status, IntegrationStatus.RETRY_PENDING)

				# Process healthy TID event -> succeeds!
				res_tid = process_inventory_publication_event(event_tid.name, client=self.client)
				self.assertTrue(res_tid["success"])
				event_tid.reload()
				self.assertEqual(event_tid.status, IntegrationStatus.SUCCEEDED)

				# Healthy channel reached expected 25
				sa_data = self.client.get_stock_available(self.stock_available_id)
				self.assertEqual(int(sa_data.get("quantity")), 25)

		finally:
			frappe.db.sql("DELETE FROM `tabIntegration Event` WHERE sales_channel = 'UNHEALTHY-CHAN'")
			frappe.db.sql("DELETE FROM `tabSales Channel` WHERE name = 'UNHEALTHY-CHAN'")
			frappe.db.commit()

	# ==================================================
	# TEST G — DATABASE HYGIENE & BASELINE STOCK RESTORATION
	# ==================================================
	def test_g_database_hygiene_and_baseline_stock_restoration(self):
		"""
		Restores baseline test stock in PrestaShop TEST (300 for product 6, 120 for product 21).
		Verifies residual test Integration Events are cleaned up.
		"""
		# Restore product 6 stock to 300
		self.client.update_stock_available_quantity(self.stock_available_id, self.baseline_qty, self.product_id, None)
		sa_6 = self.client.get_stock_available(self.stock_available_id)
		self.assertEqual(int(sa_6.get("quantity")), 300)

		# Restore product 21 stock to 120
		self.client.update_stock_available_quantity(self.stock_available_id_pliers, self.baseline_qty_pliers, self.product_id_pliers, None)
		sa_21 = self.client.get_stock_available(self.stock_available_id_pliers)
		self.assertEqual(int(sa_21.get("quantity")), 120)

		# Verify DB hygiene for test entities
		pending_test_events = frappe.db.count("Integration Event", {
			"sales_channel": self.sales_channel,
			"erp_document": self.item_code,
			"status": ["in", [IntegrationStatus.PENDING, IntegrationStatus.PROCESSING]],
		})
		self.assertEqual(pending_test_events, 0, "No pending or processing test events should remain")
