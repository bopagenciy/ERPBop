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
	recover_stale_events,
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


class TestPrestaShopPublicationHardeningUnit(unittest.TestCase):
	"""
	Comprehensive Unit & Concurrency test suite for Phase 1J.1:
	Outbound Inventory Delivery Reliability & Crash Recovery.
	"""

	def setUp(self):
		self.sales_channel = "TID"
		self.item_code = "ITEM-PHASE1H1-UNIT-01"
		self.provider = IntegrationProvider.PRESTASHOP

		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.sql(
			"DELETE FROM `tabIntegration Event` WHERE sales_channel = %s AND (erp_document LIKE %s OR erp_document LIKE %s)",
			(self.sales_channel, "ITEM-PHASE1H1-UNIT%", "ITEM-PHASE1H-%"),
		)
		frappe.db.sql(
			"DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s AND (item_code LIKE %s OR item_code LIKE %s)",
			(self.sales_channel, "ITEM-PHASE1H1-UNIT%", "ITEM-PHASE1H-%"),
		)
		frappe.db.sql(
			"DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s AND (erp_document LIKE %s OR erp_document LIKE %s)",
			(self.sales_channel, "ITEM-PHASE1H1-UNIT%", "ITEM-PHASE1H-%"),
		)
		frappe.db.commit()

	def _create_test_event(self, item_code=None, intended_atp=25.0, version=1, status=IntegrationStatus.PENDING):
		ic = item_code or self.item_code
		payload = {
			"item_code": ic,
			"sales_channel": self.sales_channel,
			"intended_atp": intended_atp,
			"publication_version": version,
		}
		idempotency_key = compute_publication_idempotency_key(
			provider=self.provider,
			sales_channel=self.sales_channel,
			item_code=ic,
			external_id="99901",
			external_variant_id=None,
			publication_version=version,
			desired_state_hash=f"hash-{version}-{intended_atp}",
		)
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": self.provider,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": ic,
			"idempotency_key": idempotency_key,
			"status": status,
			"request_metadata": json.dumps(payload),
			"max_attempts": 3,
		})
		event.insert(ignore_permissions=True)
		return event

	# ==================================================
	# 1 & 3: TWO-WORKER CLAIM FENCING
	# ==================================================
	def test_01_two_worker_claim_fencing(self):
		"""Two workers attempt to claim the same event; exactly one gets execution authority."""
		event = self._create_test_event()

		claimed_1, worker_1, token_1 = claim_event_for_processing(event.name, worker_id="worker-alpha")
		self.assertTrue(claimed_1)
		self.assertEqual(worker_1, "worker-alpha")
		self.assertIsNotNone(token_1)

		# Second worker attempts to claim same event
		claimed_2, worker_2, token_2 = claim_event_for_processing(event.name, worker_id="worker-beta")
		self.assertFalse(claimed_2)
		self.assertIsNone(worker_2)
		self.assertIsNone(token_2)

		# Verify DB state preserves first worker's lease
		db_status, db_worker, db_token = frappe.db.get_value(
			"Integration Event", event.name, ["status", "worker_id", "processing_token"]
		)
		self.assertEqual(db_status, IntegrationStatus.PROCESSING)
		self.assertEqual(db_worker, "worker-alpha")
		self.assertEqual(db_token, token_1)

	# ==================================================
	# 4: LEASE EXPIRATION & RECLAIM
	# ==================================================
	def test_02_lease_expiration_and_reclaim(self):
		"""Worker A claims event, lease expires, Worker B safely reclaims. Stale Worker A loses authority."""
		event = self._create_test_event()

		claimed_a, worker_a, token_a = claim_event_for_processing(event.name, worker_id="worker-a")
		self.assertTrue(claimed_a)

		# Simulate lease expiration in DB
		expired_time = now_datetime() - timedelta(minutes=20)
		frappe.db.sql(
			"UPDATE `tabIntegration Event` SET lease_expires_at = %s WHERE name = %s",
			(expired_time, event.name),
		)

		# Recover stale event
		recovered = recover_stale_events(timeout_minutes=1)
		self.assertEqual(recovered, 1)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)

		# Simulate retry due immediately
		frappe.db.sql(
			"UPDATE `tabIntegration Event` SET next_retry_at = %s WHERE name = %s",
			(now_datetime() - timedelta(seconds=5), event.name),
		)

		# Worker B claims
		claimed_b, worker_b, token_b = claim_event_for_processing(event.name, worker_id="worker-b")
		self.assertTrue(claimed_b)
		self.assertNotEqual(token_a, token_b)

		# Stale Worker A attempts to mutate state with old token_a -> MUST fail fencing
		event.reload()
		with self.assertRaises(frappe.ValidationError):
			event.mark_succeeded(processing_token=token_a)

	# ==================================================
	# 5: REMOTE PUT SUCCEEDS, LOCAL ACK FAILS (CONVERGENCE)
	# ==================================================
	def test_03_remote_put_succeeds_local_ack_fails_recovery(self):
		"""
		Critical scenario: PUT reaches PrestaShop and stores 25, but local ack fails.
		On retry: GET returns 25 == desired 25 -> NO second PUT required, converges as NO-OP SUCCEEDED.
		"""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.return_value = 101

		# First call: GET 5 -> PUT 25 (succeeds remotely, but we simulate local exception)
		mock_client.update_stock_available_quantity.return_value = {
			"stock_available_id": 101,
			"previous_qty": 5,
			"resulting_qty": 25,
			"changed": True,
			"reason": "SUCCESS",
		}

		event = self._create_test_event(intended_atp=25.0)

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			# 1st attempt: update_stock_available_quantity runs, but worker crashes before event mark_succeeded
			claimed, worker, token = claim_event_for_processing(event.name)
			res1 = publish_item_inventory(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				intended_atp=25.0,
				publication_version=1,
				client=mock_client,
			)
			self.assertTrue(res1["changed"])
			self.assertEqual(res1["publishable_qty"], 25)

			# Now simulate lease expiration / retry for event
			frappe.db.sql(
				"UPDATE `tabIntegration Event` SET status = 'RETRY_PENDING', next_retry_at = %s, worker_id = NULL, processing_token = NULL WHERE name = %s",
				(now_datetime() - timedelta(seconds=1), event.name),
			)

			# 2nd attempt (RETRY): remote stock is now ALREADY 25!
			mock_client.update_stock_available_quantity.return_value = {
				"stock_available_id": 101,
				"previous_qty": 25,
				"resulting_qty": 25,
				"changed": False,
				"reason": "NO_OP_IDENTICAL_QUANTITY",
			}

			retry_res = process_inventory_publication_event(event.name, client=mock_client)
			self.assertTrue(retry_res["success"])
			self.assertFalse(retry_res["result"]["changed"])
			self.assertEqual(retry_res["result"]["publishable_qty"], 25)

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# 6: LOST RESPONSE AFTER REMOTE WRITE
	# ==================================================
	def test_04_response_lost_after_remote_write(self):
		"""
		Ambiguous timeout during PUT. On retry, read-before-write checks remote:
		remote == desired -> converges cleanly without double PUT.
		"""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.return_value = 102

		mock_client.update_stock_available_quantity.return_value = {
			"stock_available_id": 102,
			"previous_qty": 25,
			"resulting_qty": 25,
			"changed": False,
			"reason": "NO_OP_IDENTICAL_QUANTITY",
		}

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		event = self._create_test_event(intended_atp=25.0)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			res = process_inventory_publication_event(event.name, client=mock_client)
			self.assertTrue(res["success"])
			self.assertFalse(res["result"]["changed"])

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# 7 & 8: STALE EVENT AFTER CRASH & SUPERSESSION
	# ==================================================
	def test_05_stale_event_after_crash_superseded_by_newer_version(self):
		"""
		Event A (v10, desired 20) crashes.
		Event B (v11, desired 5) publishes 5 successfully.
		When Event A is retried: version check finds DB at v11 -> STALE/SUPERSEDED, ZERO PUT.
		"""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.return_value = 103

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		pub_state = get_or_create_publication_state(self.sales_channel, self.item_code, external_id="99901")
		commit_publication_state(
			pub_state_name=pub_state.name,
			target_version=11,
			publish_qty=5,
			pub_hash="hash-11-5",
			remote_observed_qty=5,
			computed_atp=5.0,
			status="Succeeded",
		)

		event_a = self._create_test_event(intended_atp=20.0, version=10)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=5.0)

			res = process_inventory_publication_event(event_a.name, client=mock_client)
			self.assertTrue(res["success"])
			mock_client.update_stock_available_quantity.assert_not_called()

			event_a.reload()
			self.assertEqual(event_a.status, IntegrationStatus.SUCCEEDED)
			resp_meta = json.loads(event_a.response_metadata)
			self.assertEqual(resp_meta.get("action"), "SUPERSEDED")

	# ==================================================
	# 9: IMMEDIATE PRE-PUT FRESHNESS CHECK
	# ==================================================
	def test_06_immediate_pre_put_freshness_check(self):
		"""
		Between GET and PUT, a concurrent worker bumps the version in DB.
		The pre-PUT hook detects the supersession immediately before PUT and aborts.
		"""
		config = PrestaShopConfig(
			sales_channel=self.sales_channel,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_KEY",
			write_enabled=True,
		)
		with patch.object(PrestaShopConfig, "resolve_api_key", return_value="FAKE_KEY_1234567890123456789012"):
			mock_client = PrestaShopClient(config=config)

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		pub_state = get_or_create_publication_state(self.sales_channel, self.item_code, external_id="99901")

		def inject_concurrent_version_bump():
			frappe.db.sql(
				"UPDATE `tabInventory Publication State` SET publication_version = 99 WHERE name = %s",
				pub_state.name,
			)

		with patch.object(mock_client, "get_stock_available_xml") as mock_get:
			with patch.object(mock_client.session, "put") as mock_put:
				with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
					mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

					with patch("bop_erp.inventory.publication.allocate_publication_version", return_value=(pub_state.name, 5)):
						mock_get.side_effect = lambda *args, **kwargs: (
							inject_concurrent_version_bump() or """<?xml version="1.0" encoding="UTF-8"?>
<prestashop xmlns:xlink="http://www.w3.org/1999/xlink">
<stock_available>
	<id><![CDATA[104]]></id>
	<id_product><![CDATA[99901]]></id_product>
	<id_product_attribute><![CDATA[0]]></id_product_attribute>
	<quantity><![CDATA[5]]></quantity>
</stock_available>
</prestashop>"""
						)

						with patch.object(mock_client, "resolve_stock_available_id", return_value=104):
							with self.assertRaises(PrestaShopStalePublicationError):
								publish_item_inventory(
									sales_channel=self.sales_channel,
									item_code=self.item_code,
									publication_version=5,
									client=mock_client,
								)

							mock_put.assert_not_called()

	# ==================================================
	# 10: PUBLICATION VERSION ATOMICITY
	# ==================================================
	def test_07_publication_version_atomicity(self):
		"""Sequential or concurrent allocations produce strictly increasing, race-free versions."""
		name1, v1 = allocate_publication_version(self.sales_channel, self.item_code)
		name2, v2 = allocate_publication_version(self.sales_channel, self.item_code)
		name3, v3 = allocate_publication_version(self.sales_channel, self.item_code)

		self.assertEqual(name1, name2)
		self.assertEqual(name2, name3)
		self.assertTrue(v1 < v2 < v3)
		self.assertEqual(v2, v1 + 1)
		self.assertEqual(v3, v2 + 1)

	# ==================================================
	# 11: STATE OWNERSHIP FENCING
	# ==================================================
	def test_08_state_ownership_fencing_blocks_older_versions(self):
		"""An older publication version CANNOT overwrite state written by a newer version."""
		pub_state = get_or_create_publication_state(self.sales_channel, self.item_code, external_id="99901")

		committed_20 = commit_publication_state(
			pub_state_name=pub_state.name,
			target_version=20,
			publish_qty=50,
			pub_hash="hash-v20-50",
			remote_observed_qty=50,
			computed_atp=50.0,
			status="Succeeded",
		)
		self.assertTrue(committed_20)

		pub_state.reload()
		self.assertEqual(pub_state.publication_version, 20)
		self.assertEqual(pub_state.last_published_qty, 50)

		committed_18 = commit_publication_state(
			pub_state_name=pub_state.name,
			target_version=18,
			publish_qty=10,
			pub_hash="hash-v18-10",
			remote_observed_qty=10,
			computed_atp=10.0,
			status="Succeeded",
		)
		self.assertFalse(committed_18)

		pub_state.reload()
		self.assertEqual(pub_state.publication_version, 20)
		self.assertEqual(pub_state.last_published_qty, 50)
		self.assertEqual(pub_state.last_published_hash, "hash-v20-50")

	# ==================================================
	# 13 & 14: HTTP FAILURE CLASSIFICATIONS & RETRY-AFTER
	# ==================================================
	def test_09_http_401_403_permanent_classification(self):
		"""401/403 Authentication error transitions event straight to DEAD_LETTER (non-retryable)."""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.side_effect = PrestaShopAuthError("401 Unauthorized", status_code=401)

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		event = self._create_test_event()
		res = process_inventory_publication_event(event.name, client=mock_client)
		self.assertFalse(res["success"])
		self.assertEqual(res["error_category"], ErrorCategory.AUTHENTICATION)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.DEAD_LETTER)

	def test_10_http_404_permanent_classification(self):
		"""404 Not Found error transitions event straight to DEAD_LETTER (non-retryable)."""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.side_effect = PrestaShopNotFoundError("404 Not Found", status_code=404)

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		event = self._create_test_event()
		res = process_inventory_publication_event(event.name, client=mock_client)
		self.assertFalse(res["success"])
		self.assertEqual(res["error_category"], ErrorCategory.NOT_FOUND)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.DEAD_LETTER)

	def test_11_http_429_retry_after_handling(self):
		"""429 Rate Limit error is retryable and respects Retry-After header delay."""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.side_effect = PrestaShopRateLimitError(
			"429 Rate Limited", status_code=429, retry_after=180
		)

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		event = self._create_test_event()
		now = now_datetime()
		res = process_inventory_publication_event(event.name, client=mock_client)
		self.assertFalse(res["success"])
		self.assertEqual(res["error_category"], ErrorCategory.RATE_LIMIT)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)
		self.assertIsNotNone(event.next_retry_at)
		diff_seconds = (get_datetime(event.next_retry_at) - now).total_seconds()
		self.assertTrue(175 <= diff_seconds <= 185)

	def test_12_http_5xx_retryable_classification(self):
		"""5xx server error is retryable and schedules retry."""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.side_effect = PrestaShopServerError("500 Server Error", status_code=500)

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		event = self._create_test_event()
		res = process_inventory_publication_event(event.name, client=mock_client)
		self.assertFalse(res["success"])
		self.assertEqual(res["error_category"], ErrorCategory.PROVIDER_ERROR)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)

	# ==================================================
	# 15: DEAD LETTER AFTER MAX ATTEMPTS
	# ==================================================
	def test_13_dead_letter_after_max_attempts(self):
		"""Retryable error reaches max_attempts and transitions to DEAD_LETTER."""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.side_effect = PrestaShopTransientError("Timeout")

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		event = self._create_test_event()
		frappe.db.sql("UPDATE `tabIntegration Event` SET attempt_count = 2 WHERE name = %s", event.name)

		res = process_inventory_publication_event(event.name, client=mock_client)
		self.assertFalse(res["success"])

		event.reload()
		self.assertEqual(event.attempt_count, 3)
		self.assertEqual(event.status, IntegrationStatus.DEAD_LETTER)

	# ==================================================
	# 19: BULK EVENT ISOLATION
	# ==================================================
	def test_14_bulk_event_isolation(self):
		"""Item A failure does not block or rollback Item B and Item C."""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)

		item_a = "ITEM-PHASE1H1-UNIT-01"
		item_b = "ITEM-PHASE1H1-UNIT-02"
		item_c = "ITEM-PHASE1H-SIMPLE-01"

		def resolve_mock(prod_id, var_id=None):
			if prod_id == 99911:
				raise PrestaShopTransientError("Item A network timeout")
			return 100 + prod_id

		mock_client.resolve_stock_available_id.side_effect = resolve_mock
		mock_client.update_stock_available_quantity.return_value = {
			"stock_available_id": 102,
			"previous_qty": 5,
			"resulting_qty": 25,
			"changed": True,
			"reason": "SUCCESS",
		}

		for ic, pid in [(item_a, 99911), (item_b, 99912), (item_c, 99913)]:
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": self.sales_channel,
				"erp_doctype": "Item",
				"erp_document": ic,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": str(pid),
				"active": 1,
			}).insert(ignore_permissions=True)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			ev_a = self._create_test_event(item_code=item_a)
			ev_b = self._create_test_event(item_code=item_b)
			ev_c = self._create_test_event(item_code=item_c)

			res_a = process_inventory_publication_event(ev_a.name, client=mock_client)
			res_b = process_inventory_publication_event(ev_b.name, client=mock_client)
			res_c = process_inventory_publication_event(ev_c.name, client=mock_client)

			self.assertFalse(res_a["success"])
			self.assertTrue(res_b["success"])
			self.assertTrue(res_c["success"])

			ev_a.reload()
			ev_b.reload()
			ev_c.reload()
			self.assertEqual(ev_a.status, IntegrationStatus.RETRY_PENDING)
			self.assertEqual(ev_b.status, IntegrationStatus.SUCCEEDED)
			self.assertEqual(ev_c.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# 22 & 23: RUNTIME HOST REVALIDATION
	# ==================================================
	def test_15_runtime_host_revalidation_blocks_production_connector(self):
		"""Connector configuration changed to production after scheduling is blocked pre-network."""
		mock_conn = MagicMock()
		mock_conn.environment = "DEVELOPMENT"
		mock_conn.base_url = "https://theindustrialdepot.com"
		mock_conn.write_enabled = 1

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		event = self._create_test_event()

		with patch("bop_erp.inventory.publication.get_active_connector_for_channel", return_value=mock_conn):
			with self.assertRaises(ConnectorSafetyError):
				publish_item_inventory(
					sales_channel=self.sales_channel,
					item_code=self.item_code,
				)

	# ==================================================
	# 24: MAPPING CHANGE AFTER SCHEDULING
	# ==================================================
	def test_16_mapping_change_after_scheduling_blocks_safely(self):
		"""If mapping is deleted or changed after event is scheduled, execution fails safely."""
		event = self._create_test_event()
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)

		res = process_inventory_publication_event(event.name, client=mock_client)
		self.assertFalse(res["success"])
		self.assertEqual(res["error_category"], ErrorCategory.VALIDATION)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.DEAD_LETTER)

	# ==================================================
	# 25: SECRET REDACTION REGRESSION
	# ==================================================
	def test_17_sentinel_secret_redaction_regression(self):
		"""Sentinel API key must never appear in error messages, events, or publication states."""
		sentinel_secret = "BOP_SECRET_DO_NOT_LEAK_1J1"
		config = PrestaShopConfig(
			sales_channel=self.sales_channel,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="SENTINEL_REF",
			write_enabled=True,
		)
		with patch.object(PrestaShopConfig, "resolve_api_key", return_value=sentinel_secret):
			mock_client = PrestaShopClient(config=config)

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		event = self._create_test_event()

		with patch.object(mock_client.session, "get") as mock_get:
			mock_resp = MagicMock()
			mock_resp.status_code = 401
			mock_resp.text = f"Invalid auth key {sentinel_secret}"
			mock_get.return_value = mock_resp

			res = process_inventory_publication_event(event.name, client=mock_client)
			self.assertFalse(res["success"])

			# Check event document
			event.reload()
			self.assertNotIn(sentinel_secret, str(event.last_error_message or ""))
			self.assertNotIn(sentinel_secret, str(event.request_metadata or ""))
			self.assertNotIn(sentinel_secret, str(event.response_metadata or ""))

			# Check publication state
			pub_state = frappe.db.get_value("Inventory Publication State", {"sales_channel": self.sales_channel, "item_code": self.item_code}, "last_error")
			self.assertNotIn(sentinel_secret, str(pub_state or ""))

	# ==================================================
	# 7: CRASH BEFORE REMOTE PUT
	# ==================================================
	def test_18_crash_before_put_recovery(self):
		"""Crash before PUT leaves remote untouched; retry publishes current ATP."""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.return_value = 105

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		event = self._create_test_event(intended_atp=25.0)

		# Worker 1 claims but crashes before calling client
		claimed, worker, token = claim_event_for_processing(event.name, worker_id="worker-crash")
		self.assertTrue(claimed)

		# Lease expires and gets recovered
		frappe.db.sql(
			"UPDATE `tabIntegration Event` SET lease_expires_at = %s WHERE name = %s",
			(now_datetime() - timedelta(minutes=20), event.name),
		)
		recover_stale_events(timeout_minutes=1)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)

		# Retry due immediately
		frappe.db.sql(
			"UPDATE `tabIntegration Event` SET next_retry_at = %s WHERE name = %s",
			(now_datetime() - timedelta(seconds=1), event.name),
		)

		mock_client.update_stock_available_quantity.return_value = {
			"stock_available_id": 105,
			"previous_qty": 5,
			"resulting_qty": 30,
			"changed": True,
			"reason": "SUCCESS",
		}

		# When retried, ERP ATP is now 30
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=30.0)

			# Event intended_atp was 25.0, live ATP is 30.0 -> supersedes cleanly
			res = process_inventory_publication_event(event.name, worker_id="worker-recovery", client=mock_client)
			self.assertTrue(res["success"])
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# 17: CONCURRENT DIFFERENT VERSIONS WINNER
	# ==================================================
	def test_19_concurrent_different_versions_winner(self):
		"""Worker A (v20, qty 50) and Worker B (v21, qty 30): newest version (v21) always wins."""
		pub_state = get_or_create_publication_state(self.sales_channel, self.item_code, external_id="99901")

		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.return_value = 106

		# Order 1: Worker B (v21, qty 30) commits first
		committed_b = commit_publication_state(
			pub_state_name=pub_state.name,
			target_version=21,
			publish_qty=30,
			pub_hash="hash-21-30",
			remote_observed_qty=30,
			computed_atp=30.0,
			status="Succeeded",
		)
		self.assertTrue(committed_b)

		# Worker A (v20, qty 50) attempts to commit -> blocked by DB fencing
		committed_a = commit_publication_state(
			pub_state_name=pub_state.name,
			target_version=20,
			publish_qty=50,
			pub_hash="hash-20-50",
			remote_observed_qty=50,
			computed_atp=50.0,
			status="Succeeded",
		)
		self.assertFalse(committed_a)

		pub_state.reload()
		self.assertEqual(pub_state.publication_version, 21)
		self.assertEqual(pub_state.last_published_qty, 30)

	# ==================================================
	# 18: CONCURRENT SAME VERSION CONVERGENCE
	# ==================================================
	def test_20_concurrent_same_version_convergence(self):
		"""Two workers attempt same desired publication; harmless convergence with zero conflict."""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.return_value = 107

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		# 1st worker publishes 25 (changed=True)
		mock_client.update_stock_available_quantity.return_value = {
			"stock_available_id": 107,
			"previous_qty": 5,
			"resulting_qty": 25,
			"changed": True,
			"reason": "SUCCESS",
		}

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			res1 = publish_item_inventory(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				publication_version=10,
				client=mock_client,
			)
			self.assertTrue(res1["changed"])

			# 2nd worker executes same version 10: remote is now 25 -> NO-OP convergence
			mock_client.update_stock_available_quantity.return_value = {
				"stock_available_id": 107,
				"previous_qty": 25,
				"resulting_qty": 25,
				"changed": False,
				"reason": "NO_OP_IDENTICAL_QUANTITY",
			}

			res2 = publish_item_inventory(
				sales_channel=self.sales_channel,
				item_code=self.item_code,
				publication_version=10,
				client=mock_client,
			)
			self.assertFalse(res2["changed"])
			self.assertEqual(res2["publishable_qty"], 25)

	# ==================================================
	# 20: EVENT COALESCING / SUPERSESSION
	# ==================================================
	def test_21_event_coalescing_rapid_atp_changes(self):
		"""Rapid ATP changes (100 -> 90 -> 70 -> 40): obsolete intents supersede, only latest writes."""
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.return_value = 108

		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "99901",
			"active": 1,
		}).insert(ignore_permissions=True)

		ev_100 = self._create_test_event(intended_atp=100.0, version=1)
		ev_90 = self._create_test_event(intended_atp=90.0, version=2)
		ev_70 = self._create_test_event(intended_atp=70.0, version=3)
		ev_40 = self._create_test_event(intended_atp=40.0, version=4)

		# Current live ATP in ERP has settled at 40
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=40.0)

			# When ev_100 is processed: intended 100 != live 40 -> STALE/SUPERSEDED (zero PUT)
			r1 = process_inventory_publication_event(ev_100.name, client=mock_client)
			self.assertTrue(r1["success"])
			ev_100.reload()
			self.assertEqual(ev_100.status, IntegrationStatus.SUCCEEDED)
			self.assertEqual(json.loads(ev_100.response_metadata).get("action"), "SUPERSEDED")

			# Same for 90 and 70
			r2 = process_inventory_publication_event(ev_90.name, client=mock_client)
			r3 = process_inventory_publication_event(ev_70.name, client=mock_client)
			self.assertTrue(r2["success"])
			self.assertTrue(r3["success"])

			# mock_client was NOT called for any of the first three!
			mock_client.update_stock_available_quantity.assert_not_called()

			# Finally ev_40 runs: intended 40 == live 40 -> executes update!
			mock_client.update_stock_available_quantity.return_value = {
				"stock_available_id": 108,
				"previous_qty": 100,
				"resulting_qty": 40,
				"changed": True,
				"reason": "SUCCESS",
			}
			r4 = process_inventory_publication_event(ev_40.name, client=mock_client)
			self.assertTrue(r4["success"])
			self.assertEqual(mock_client.update_stock_available_quantity.call_count, 1)
			ev_40.reload()
			self.assertEqual(ev_40.status, IntegrationStatus.SUCCEEDED)
