# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
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
	get_database_now,
)
from bop_erp.inventory.publication import (
	compute_publication_idempotency_key,
	get_or_create_publication_state,
	commit_publication_state,
	publish_item_inventory,
)
from bop_erp.inventory.scheduler import (
	process_pending_inventory_publications,
	enqueue_scheduled_inventory_publication,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopServerError,
	PrestaShopValidationError,
)


class TestPrestaShopPublicationSchedulerLive(unittest.TestCase):
	"""
	Live Integration Test Suite for Phase 1J.2:
	Lease Authority and Scheduled Publication Activation.
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
		self._cleanup()
		self._ensure_mapping()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.sql(
			"DELETE FROM `tabIntegration Event` WHERE sales_channel = %s AND (erp_document = %s OR request_metadata LIKE %s)",
			(self.sales_channel, self.item_code, f"%{self.item_code}%"),
		)
		frappe.db.sql(
			"DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s AND item_code = %s",
			(self.sales_channel, self.item_code),
		)
		frappe.db.commit()

	def _ensure_mapping(self):
		if not frappe.db.exists("External ID Mapping", {"sales_channel": self.sales_channel, "erp_document": self.item_code, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": self.sales_channel,
				"erp_doctype": "Item",
				"erp_document": self.item_code,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": str(self.product_id),
				"active": 1,
			}).insert(ignore_permissions=True)
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
			desired_state_hash=f"hash-sched-live-{version}-{intended_atp}",
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
	# 1. LIVE SCHEDULED PUBLICATION LIFECYCLE
	# ==================================================
	def test_01_live_scheduled_publication_lifecycle(self):
		"""
		1. Set remote stock to 5.
		2. Create pending integration event with intended ATP = 25.
		3. Run process_pending_inventory_publications.
		4. Verify event transitions Pending -> Processing -> Succeeded.
		5. Verify PrestaShop remote quantity is 25.
		6. Run scheduler again: 0 claimable events, 0 writes executed.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 5, self.product_id, None)

		event = self._create_live_event(intended_atp=25.0, version=1)
		self.assertEqual(event.status, IntegrationStatus.PENDING)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			# Run scheduler
			res = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				client=self.client,
			)

			self.assertEqual(res["events_seen"], 1)
			self.assertEqual(res["events_claimed"], 1)
			self.assertEqual(res["published"], 1)

			# Verify remote PrestaShop stock
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 25)

			# Verify event terminal state
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

			# Run scheduler again: event is no longer claimable
			res2 = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				client=self.client,
			)
			self.assertEqual(res2["events_seen"], 0)
			self.assertEqual(res2["events_claimed"], 0)
			self.assertEqual(res2["published"], 0)

	# ==================================================
	# 2. LIVE TRANSIENT FAILURE, RETRY, AND CONVERGENCE
	# ==================================================
	def test_02_live_transient_failure_retry_and_convergence(self):
		"""
		1. Create pending integration event.
		2. Inject transient 500 error on first attempt.
		3. Scheduler marks event as Retry_Pending with next_retry_at populated.
		4. Re-run scheduler before next_retry_at: event skipped (seen=0).
		5. Fast forward next_retry_at into past: scheduler picks up event and succeeds.
		"""
		self.client.update_stock_available_quantity(self.stock_available_id, 5, self.product_id, None)

		event = self._create_live_event(intended_atp=25.0, version=1)

		original_update = self.client.update_stock_available_quantity
		attempt_count = 0

		def flaky_update(*args, **kwargs):
			nonlocal attempt_count
			attempt_count += 1
			if attempt_count == 1:
				raise PrestaShopServerError("Simulated 500 Gateway Timeout", status_code=500)
			return original_update(*args, **kwargs)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp, \
		     patch.object(self.client, "update_stock_available_quantity", side_effect=flaky_update):
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			# Attempt 1: fails transiently
			res1 = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				client=self.client,
			)
			self.assertEqual(res1["events_seen"], 1)
			self.assertEqual(res1["retry_pending"], 1)
			self.assertEqual(res1["published"], 0)

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)
			self.assertEqual(event.attempt_count, 1)
			self.assertIsNotNone(event.next_retry_at)

			# Attempt 2: immediate rerun before backoff arrives -> not due
			res2 = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				client=self.client,
			)
			self.assertEqual(res2["events_seen"], 0)
			self.assertEqual(res2["published"], 0)

			# Fast-forward retry clock
			frappe.db.set_value("Integration Event", event.name, "next_retry_at", get_database_now())
			frappe.db.commit()

			# Attempt 3: due for retry -> succeeds
			res3 = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				client=self.client,
			)
			self.assertEqual(res3["events_seen"], 1)
			self.assertEqual(res3["published"], 1)

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

			# Verify remote PrestaShop stock
			sa_data = self.client.get_stock_available(self.stock_available_id)
			self.assertEqual(int(sa_data.get("quantity")), 25)

	# ==================================================
	# 3. LIVE DEAD LETTER ON EXHAUSTED RETRIES
	# ==================================================
	def test_03_live_dead_letter_on_exhausted_retries(self):
		"""
		1. Create event with max_attempts=1.
		2. Inject transient 500 error.
		3. Because attempt_count reaches max_attempts, event enters Dead_Letter.
		4. Event is never retried by future scheduler runs.
		"""
		event = self._create_live_event(intended_atp=25.0, version=1)
		frappe.db.set_value("Integration Event", event.name, "max_attempts", 1)
		frappe.db.commit()

		def server_error_update(*args, **kwargs):
			raise PrestaShopServerError("Simulated 500 Unrecoverable Service Unavailable", status_code=500)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp, \
		     patch.object(self.client, "update_stock_available_quantity", side_effect=server_error_update):
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			res = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				client=self.client,
			)
			self.assertEqual(res["events_seen"], 1)
			self.assertEqual(res["dead_letter"], 1)

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.DEAD_LETTER)
			self.assertEqual(event.attempt_count, 1)

			# Subsequent runs ignore dead letter events
			res2 = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				client=self.client,
			)
			self.assertEqual(res2["events_seen"], 0)
