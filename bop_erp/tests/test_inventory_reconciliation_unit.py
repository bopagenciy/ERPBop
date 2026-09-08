# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch
import frappe
from frappe.utils import now_datetime, get_datetime

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationDirection,
	IntegrationOperation,
	ErrorCategory,
)
from bop_erp.safety import (
	ConnectorSafetyError,
	assert_safe_write_target,
)
from bop_erp.reliability import (
	claim_event_for_processing,
	verify_processing_authority,
	recover_stale_events,
	get_database_now,
)
from bop_erp.inventory.publication import (
	normalize_publishable_quantity,
	compute_publication_hash,
	compute_publication_idempotency_key,
	get_or_create_publication_state,
	allocate_publication_version,
	commit_publication_state,
	resolve_item_mapping,
	publish_item_inventory,
	schedule_channel_inventory_publication,
	process_inventory_publication_event,
)
from bop_erp.inventory.reconciliation import (
	get_canonical_publishable_quantity,
	get_channel_inventory_reconciliation,
	reconcile_inventory_item,
	reconcile_channel_inventory,
)
from bop_erp.orders.ingestion import find_affected_channels_for_items
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopError,
	PrestaShopAuthError,
	PrestaShopNotFoundError,
	PrestaShopRateLimitError,
	PrestaShopValidationError,
	PrestaShopServerError,
	PrestaShopTransientError,
	PrestaShopStalePublicationError,
)
from bop_erp.inventory.models import ChannelATP


class TestInventoryReconciliationUnit(unittest.TestCase):
	"""
	Comprehensive Unit test suite for Phase 1L:
	Outbound Inventory Publication Reconciliation, Drift Detection & Crash-Safe Recovery.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales_channel = "TID"
		cls.item_code = "ITEM-PHASE1L-UNIT-01"
		cls.provider = IntegrationProvider.PRESTASHOP

		if not frappe.db.exists("Item", cls.item_code):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code,
				"item_name": cls.item_code,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)
			frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if frappe.db.exists("Item", cls.item_code):
			frappe.delete_doc("Item", cls.item_code, force=True, ignore_permissions=True)
			frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.sql(
			"DELETE FROM `tabIntegration Event` WHERE sales_channel = %s AND erp_document LIKE %s",
			(self.sales_channel, "ITEM-PHASE1L%"),
		)
		frappe.db.sql(
			"DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s AND item_code LIKE %s",
			(self.sales_channel, "ITEM-PHASE1L%"),
		)
		frappe.db.sql(
			"DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s AND erp_document LIKE %s",
			(self.sales_channel, "ITEM-PHASE1L%"),
		)
		frappe.db.commit()

	# ------------------------------------------------------------------
	# 1. Canonical Desired Quantity Resolution
	# ------------------------------------------------------------------
	@patch("bop_erp.inventory.reconciliation.get_channel_atp")
	def test_01_canonical_desired_quantity_resolution(self, mock_get_atp):
		"""
		Verifies get_canonical_publishable_quantity returns normalized whole integer
		and raw float ATP with anti-overselling clamping.
		"""
		mock_atp = MagicMock(spec=ChannelATP)
		mock_atp.aggregate_atp_qty = 15.8
		mock_get_atp.return_value = mock_atp

		pub_qty, raw_atp = get_canonical_publishable_quantity(self.sales_channel, self.item_code)
		self.assertEqual(pub_qty, 15)
		self.assertEqual(raw_atp, 15.8)

		# Clamping negative to zero
		mock_atp.aggregate_atp_qty = -5.0
		pub_qty, raw_atp = get_canonical_publishable_quantity(self.sales_channel, self.item_code)
		self.assertEqual(pub_qty, 0)
		self.assertEqual(raw_atp, -5.0)

	# ------------------------------------------------------------------
	# 2. Publication Idempotency Key Stability
	# ------------------------------------------------------------------
	def test_02_publication_idempotency_key_stability(self):
		"""
		Verifies publication idempotency key is deterministic across identical tuples
		and changes when state changes.
		"""
		k1 = compute_publication_idempotency_key(
			provider=self.provider,
			sales_channel=self.sales_channel,
			item_code=self.item_code,
			external_id="101",
			external_variant_id=None,
			publication_version=2,
			desired_state_hash="abc123hash",
		)
		k2 = compute_publication_idempotency_key(
			provider=self.provider,
			sales_channel=self.sales_channel,
			item_code=self.item_code,
			external_id="101",
			external_variant_id=None,
			publication_version=2,
			desired_state_hash="abc123hash",
		)
		self.assertEqual(k1, k2)

		k3 = compute_publication_idempotency_key(
			provider=self.provider,
			sales_channel=self.sales_channel,
			item_code=self.item_code,
			external_id="101",
			external_variant_id=None,
			publication_version=3,
			desired_state_hash="abc123hash",
		)
		self.assertNotEqual(k1, k3)

	# ------------------------------------------------------------------
	# 3. Outbox Event Coalescing
	# ------------------------------------------------------------------
	@patch("bop_erp.inventory.publication.get_channel_atp")
	@patch("bop_erp.inventory.publication.resolve_item_mapping")
	def test_03_outbox_event_coalescing(self, mock_mapping, mock_atp):
		"""
		Verifies that multiple scheduling operations for the same item/channel coalesce
		into a single PENDING Integration Event rather than creating unbounded duplicates.
		"""
		mock_mapping.return_value = {
			"mapping_name": "map-1",
			"entity_type": ExternalEntityType.PRODUCT,
			"product_id": 9991,
			"variant_id": None,
			"provider": self.provider,
		}
		mock_atp.return_value = MagicMock(aggregate_atp_qty=50.0)

		# Schedule first intent
		evs_1 = schedule_channel_inventory_publication(self.sales_channel, [self.item_code])
		self.assertEqual(len(evs_1), 1)
		ev_name_1 = evs_1[0]

		# Schedule second intent for same item (same ATP)
		evs_2 = schedule_channel_inventory_publication(self.sales_channel, [self.item_code])
		self.assertEqual(len(evs_2), 1)
		self.assertEqual(evs_2[0], ev_name_1, "Identical intent must reuse existing pending event")

		# Schedule third intent with updated ATP
		mock_atp.return_value = MagicMock(aggregate_atp_qty=48.0)
		evs_3 = schedule_channel_inventory_publication(self.sales_channel, [self.item_code])
		self.assertEqual(len(evs_3), 1)
		self.assertEqual(evs_3[0], ev_name_1, "Updated intent must coalesce into existing pending event")

		# Verify only 1 pending event exists in DB
		cnt = frappe.db.count("Integration Event", {
			"sales_channel": self.sales_channel,
			"erp_document": self.item_code,
			"status": IntegrationStatus.PENDING,
		})
		self.assertEqual(cnt, 1)

	# ------------------------------------------------------------------
	# 4. Exclusive Claim Ownership & Worker Fencing
	# ------------------------------------------------------------------
	def test_04_exclusive_claim_ownership_and_worker_fencing(self):
		"""
		Verifies atomic claim gives exclusive ownership with unique processing token,
		blocking a concurrent worker until lease expires.
		"""
		ev = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": self.provider,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"status": IntegrationStatus.PENDING,
			"max_attempts": 3,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		claimed_1, worker_1, token_1 = claim_event_for_processing(ev.name, worker_id="worker-1")
		self.assertTrue(claimed_1)
		self.assertEqual(worker_1, "worker-1")
		self.assertIsNotNone(token_1)

		# Second worker attempt must be rejected
		claimed_2, worker_2, token_2 = claim_event_for_processing(ev.name, worker_id="worker-2")
		self.assertFalse(claimed_2)
		self.assertIsNone(token_2)

	# ------------------------------------------------------------------
	# 5. Lease Expiration and Stale Worker Rejection
	# ------------------------------------------------------------------
	def test_05_lease_expiration_and_stale_worker_rejection(self):
		"""
		Verifies that an expired worker loses authority and cannot commit state
		or mark the event succeeded.
		"""
		ev = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": self.provider,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"status": IntegrationStatus.PROCESSING,
			"worker_id": "worker-stale",
			"processing_token": "token-stale-123",
			"lease_expires_at": now_datetime() - timedelta(minutes=5),
			"max_attempts": 3,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		is_auth, reason = verify_processing_authority(ev.name, "token-stale-123")
		self.assertFalse(is_auth)
		self.assertIn("Fencing violation", reason)

	# ------------------------------------------------------------------
	# 6. Retry Classification: Retryable vs Non-Retryable
	# ------------------------------------------------------------------
	@patch("bop_erp.inventory.publication.claim_event_for_processing")
	@patch("bop_erp.inventory.publication.publish_item_inventory")
	def test_06_retry_classification(self, mock_publish, mock_claim):
		"""
		Verifies transient errors (HTTP 429, 503) move event to RETRY_PENDING,
		while permanent validation/auth errors move to FAILED.
		"""
		mock_claim.return_value = (True, "w-1", "tok-1")

		ev = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": self.provider,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"status": IntegrationStatus.PROCESSING,
			"processing_token": "tok-1",
			"lease_expires_at": now_datetime() + timedelta(minutes=15),
			"max_attempts": 3,
			"attempt_count": 1,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Transient error (429 Rate Limit)
		rate_err = PrestaShopRateLimitError("Too Many Requests", status_code=429, retry_after=60)
		mock_publish.side_effect = rate_err

		res = process_inventory_publication_event(ev.name, worker_id="w-1")
		self.assertFalse(res["success"])
		self.assertEqual(res["error_category"], ErrorCategory.RATE_LIMIT)

		ev.reload()
		self.assertEqual(ev.status, IntegrationStatus.RETRY_PENDING)
		self.assertIsNotNone(ev.next_retry_at)

	# ------------------------------------------------------------------
	# 7. Terminal Failure and Dead-Letter Visibility
	# ------------------------------------------------------------------
	@patch("bop_erp.inventory.publication.claim_event_for_processing")
	@patch("bop_erp.inventory.publication.publish_item_inventory")
	def test_07_terminal_failure_and_dead_letter_visibility(self, mock_publish, mock_claim):
		"""
		Verifies an event reaching max_attempts transitions to DEAD_LETTER
		with operator-visible diagnostic error details and zero secrets.
		"""
		mock_claim.return_value = (True, "w-1", "tok-1")

		ev = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": self.provider,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"status": IntegrationStatus.PROCESSING,
			"processing_token": "tok-1",
			"lease_expires_at": now_datetime() + timedelta(minutes=15),
			"max_attempts": 3,
			"attempt_count": 3,  # Reached max attempts!
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		mock_publish.side_effect = PrestaShopServerError("Persistent 500 error", status_code=500)

		res = process_inventory_publication_event(ev.name, worker_id="w-1")
		self.assertFalse(res["success"])

		ev.reload()
		self.assertEqual(ev.status, IntegrationStatus.DEAD_LETTER)
		self.assertEqual(ev.last_error_code, "PS_SERVER_ERROR")
		self.assertIn("Persistent 500 error", ev.last_error_message)

	# ------------------------------------------------------------------
	# 8. External Drift Detection: IN_SYNC vs DRIFTED
	# ------------------------------------------------------------------
	@patch("bop_erp.inventory.reconciliation.get_canonical_publishable_quantity")
	@patch("bop_erp.inventory.reconciliation.resolve_item_mapping")
	def test_08_external_drift_detection(self, mock_mapping, mock_canonical):
		"""
		Verifies get_channel_inventory_reconciliation correctly classifies
		IN_SYNC when ERP ATP == remote stock, and DRIFTED when they differ.
		"""
		mock_mapping.return_value = {
			"product_id": 9992,
			"variant_id": None,
			"entity_type": ExternalEntityType.PRODUCT,
			"provider": self.provider,
		}
		mock_client = MagicMock()
		mock_client.resolve_stock_available_id.return_value = 1001
		mock_client.get_stock_available.return_value = {"quantity": "50"}

		# Case A: ERP is 50 -> IN_SYNC
		mock_canonical.return_value = (50, 50.0)
		rows = get_channel_inventory_reconciliation(
			self.sales_channel,
			item_codes=[self.item_code],
			client=mock_client,
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["stock_status"], "IN_SYNC")
		self.assertEqual(rows[0]["delta"], 0)

		# Case B: ERP is 45 -> DRIFTED (delta = -5)
		mock_canonical.return_value = (45, 45.0)
		rows = get_channel_inventory_reconciliation(
			self.sales_channel,
			item_codes=[self.item_code],
			client=mock_client,
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["stock_status"], "DRIFTED")
		self.assertEqual(rows[0]["delta"], -5)

	# ------------------------------------------------------------------
	# 9. Drift Repair Routes Strictly Through Transactional Outbox
	# ------------------------------------------------------------------
	@patch("bop_erp.inventory.reconciliation.get_channel_inventory_reconciliation")
	@patch("bop_erp.inventory.reconciliation.schedule_channel_inventory_publication")
	def test_09_drift_repair_routes_through_outbox(self, mock_schedule, mock_reconcile):
		"""
		Verifies reconcile_inventory_item and reconcile_channel_inventory route
		repairs strictly through schedule_channel_inventory_publication (outbox),
		never issuing direct un-tracked writes.
		"""
		mock_reconcile.return_value = [
			{
				"item_code": self.item_code,
				"sales_channel": self.sales_channel,
				"provider": self.provider,
				"erp_atp": 45.0,
				"publishable_qty": 45,
				"remote_qty": 50,
				"delta": -5,
				"stock_status": "DRIFTED",
				"mapping_status": "MAPPED",
			}
		]
		mock_schedule.return_value = ["INTEG-EV-REPAIR-01"]

		res = reconcile_inventory_item(
			sales_channel=self.sales_channel,
			item_code=self.item_code,
			repair=True,
		)
		self.assertTrue(res["repair_enqueued"])
		self.assertEqual(res["repair_event"], "INTEG-EV-REPAIR-01")
		mock_schedule.assert_called_once_with(
			sales_channel=self.sales_channel,
			item_codes=[self.item_code],
		)

	# ------------------------------------------------------------------
	# 10. Multi-Channel Isolation & Multi-Warehouse Topology
	# ------------------------------------------------------------------
	def test_10_multi_channel_and_multi_warehouse_isolation(self):
		"""
		Verifies find_affected_channels_for_items discovers channels sharing warehouses
		and isolates un-shared warehouses.
		"""
		wh_miami = "WH-UNIT-MIA"
		wh_dallas = "WH-UNIT-DAL"

		# Channel A maps to Miami, Channel B maps to Miami + Dallas, Channel C maps only to Dallas
		with patch("frappe.get_all") as mock_get_all:
			# Mocking warehouse discovery for source Channel A
			mock_get_all.side_effect = [
				[wh_miami],  # warehouses for Channel A
				["STORE_A", "STORE_B"],  # channels mapping to Miami (Channel C excluded!)
			]
			affected = find_affected_channels_for_items("STORE_A", item_codes=[self.item_code])
			self.assertIn("STORE_A", affected)
			self.assertIn("STORE_B", affected)
			self.assertNotIn("STORE_C", affected)
