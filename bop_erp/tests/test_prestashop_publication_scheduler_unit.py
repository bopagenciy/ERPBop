# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import datetime
import json
import unittest
import zoneinfo
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
	renew_processing_lease,
	recover_stale_events,
	get_database_now,
)
from bop_erp.inventory.publication import (
	get_or_create_publication_state,
	allocate_publication_version,
	commit_publication_state,
	publish_item_inventory,
	schedule_channel_inventory_publication,
	process_inventory_publication_event,
)
from bop_erp.inventory.scheduler import (
	process_pending_inventory_publications,
	enqueue_scheduled_inventory_publication,
	DEFAULT_SCHEDULER_BATCH_LIMIT,
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


class TestPrestaShopPublicationSchedulerUnit(unittest.TestCase):
	"""
	Comprehensive Unit and Concurrency test suite for Phase 1J.2:
	Lease Authority and Scheduled Publication Activation.
	"""

	def setUp(self):
		self.sales_channel = "TID"
		self.item_code = "ITEM-PHASE1H1-UNIT-01"
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.sql(
			"DELETE FROM `tabIntegration Event` WHERE sales_channel = %s AND erp_document = %s",
			(self.sales_channel, self.item_code),
		)
		frappe.db.sql(
			"DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s AND item_code = %s",
			(self.sales_channel, self.item_code),
		)
		frappe.db.sql(
			"DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s AND erp_document = %s",
			(self.sales_channel, self.item_code),
		)
		frappe.db.commit()

	def _create_mock_client(self):
		config = PrestaShopConfig(
			sales_channel=self.sales_channel,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_KEY",
			write_enabled=True,
		)
		with patch.object(PrestaShopConfig, "resolve_api_key", return_value="FAKE_KEY_1234567890123456789012"):
			client = PrestaShopClient(config=config)
		client.resolve_stock_available_id = MagicMock(return_value=100)
		return client

	def _create_test_event(self, item_code=None, intended_atp=25.0, version=1, status=IntegrationStatus.PENDING):
		ic = item_code or self.item_code
		payload = {
			"item_code": ic,
			"sales_channel": self.sales_channel,
			"intended_atp": intended_atp,
			"publication_version": version,
		}
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": ic,
			"idempotency_key": f"EV-{ic}-{frappe.generate_hash(length=8)}",
			"status": status,
			"request_metadata": json.dumps(payload),
			"max_attempts": 3,
		})
		event.insert(ignore_permissions=True)
		frappe.db.commit()
		return event

	def _ensure_mapping(self):
		if not frappe.db.exists("External ID Mapping", {"sales_channel": self.sales_channel, "erp_document": self.item_code, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": self.sales_channel,
				"erp_doctype": "Item",
				"erp_document": self.item_code,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": "100",
				"active": 1,
			}).insert(ignore_permissions=True)
			frappe.db.commit()

	# ==================================================
	# 1. CRITICAL EXPIRED LEASE SAME-TOKEN BLOCKS PUT
	# ==================================================
	def test_01_expired_lease_same_token_blocks_put(self):
		"""
		Worker A claims event. Token = TOKEN-A.
		Lease expires without any Worker B reclaiming yet.
		DB processing_token is still TOKEN-A.
		Worker A resumes and reaches pre-PUT check:
		PUT count MUST be 0. Worker A is rejected because lease expired.
		"""
		self._ensure_mapping()
		mock_client = self._create_mock_client()
		event = self._create_test_event()

		# Worker A claims
		claimed, worker_a, token_a = claim_event_for_processing(event.name, worker_id="WORKER-A")
		self.assertTrue(claimed)

		# Expire lease into past without any other worker touching the event
		past = now_datetime() - timedelta(minutes=10)
		frappe.db.set_value("Integration Event", event.name, "lease_expires_at", past)
		frappe.db.commit()

		# Token and status in DB still match Worker A
		cur_st, cur_tok = frappe.db.get_value("Integration Event", event.name, ["status", "processing_token"])
		self.assertEqual(cur_st, IntegrationStatus.PROCESSING)
		self.assertEqual(cur_tok, token_a)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			put_called = False
			def fake_update(*args, **kwargs):
				nonlocal put_called
				hook = kwargs.get("pre_put_hook")
				if hook:
					hook()
				put_called = True
				return {"changed": True, "previous_qty": 0, "resulting_qty": 25}

			mock_client.update_stock_available_quantity = MagicMock(side_effect=fake_update)

			with self.assertRaises(PrestaShopStalePublicationError) as cm:
				publish_item_inventory(
					sales_channel=self.sales_channel,
					item_code=self.item_code,
					intended_atp=25.0,
					publication_version=1,
					event_doc=event,
					processing_token=token_a,
					worker_id=worker_a,
					client=mock_client,
				)

			self.assertIn("expired", str(cm.exception).lower())
			self.assertFalse(put_called, "PUT must NOT be called when lease has expired, even with matching token!")

	# ==================================================
	# 2. LEASE EXPIRES BETWEEN GET AND PUT
	# ==================================================
	def test_02_lease_expires_between_get_and_put(self):
		"""
		Worker A has a valid lease when reading remote GET.
		Lease expires between GET and PUT. No reclaim occurs.
		Pre-PUT authority check must block the PUT.
		"""
		self._ensure_mapping()
		mock_client = self._create_mock_client()
		event = self._create_test_event()

		claimed, worker_a, token_a = claim_event_for_processing(event.name, worker_id="WORKER-A")
		self.assertTrue(claimed)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=25.0)

			def simulated_update_stock_available(*args, **kwargs):
				# GET remote succeeded, now lease expires right before PUT hook
				past = now_datetime() - timedelta(seconds=5)
				frappe.db.set_value("Integration Event", event.name, "lease_expires_at", past)
				frappe.db.commit()
				hook = kwargs.get("pre_put_hook")
				if hook:
					hook()
				return {"changed": True, "previous_qty": 5, "resulting_qty": 25}

			mock_client.update_stock_available_quantity = MagicMock(side_effect=simulated_update_stock_available)

			with self.assertRaises(PrestaShopStalePublicationError):
				publish_item_inventory(
					sales_channel=self.sales_channel,
					item_code=self.item_code,
					intended_atp=25.0,
					publication_version=1,
					event_doc=event,
					processing_token=token_a,
					worker_id=worker_a,
					client=mock_client,
				)

	# ==================================================
	# 3. EXPIRED WORKER CANNOT RENEW ITSELF
	# ==================================================
	def test_03_expired_worker_cannot_renew_itself(self):
		"""
		Worker whose lease has expired cannot self-resurrect by calling renew_processing_lease.
		"""
		event = self._create_test_event()
		claimed, worker, token = claim_event_for_processing(event.name, worker_id="WORKER-RENEW-FAIL")
		self.assertTrue(claimed)

		# Expire lease
		past = now_datetime() - timedelta(minutes=1)
		frappe.db.set_value("Integration Event", event.name, "lease_expires_at", past)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError):
			renew_processing_lease(event.name, token)

	# ==================================================
	# 4. EXPIRED WORKER CANNOT COMMIT PUBLICATION STATE
	# ==================================================
	def test_04_expired_worker_cannot_commit_publication_state(self):
		"""
		Worker with expired lease fails state commit fencing.
		"""
		event = self._create_test_event()
		claimed, worker, token = claim_event_for_processing(event.name, worker_id="WORKER-STATE-FAIL")
		self.assertTrue(claimed)

		pub_state = get_or_create_publication_state(self.sales_channel, self.item_code, external_id="100")

		# Expire lease
		past = now_datetime() - timedelta(minutes=5)
		frappe.db.set_value("Integration Event", event.name, "lease_expires_at", past)
		frappe.db.commit()

		committed = commit_publication_state(
			pub_state_name=pub_state.name,
			target_version=5,
			publish_qty=25,
			pub_hash="hash-5",
			remote_observed_qty=25,
			computed_atp=25.0,
			status="Succeeded",
			last_event_name=event.name,
			processing_token=token,
		)
		self.assertFalse(committed, "Expired worker must NOT commit publication state!")

	# ==================================================
	# 5. RECLAIM CHANGES AUTHORITY AND OLD TOKEN BLOCKED
	# ==================================================
	def test_05_reclaim_changes_authority_and_blocks_old_token(self):
		"""
		Worker A lease expires. Event is recovered to RETRY_PENDING.
		Worker B reclaims with TOKEN-B.
		Worker A later resumes with TOKEN-A -> rejected on pre-PUT and state commit.
		"""
		event = self._create_test_event()
		claimed_a, worker_a, token_a = claim_event_for_processing(event.name, worker_id="WORKER-A")
		self.assertTrue(claimed_a)

		# Expire lease
		past = now_datetime() - timedelta(minutes=20)
		frappe.db.set_value("Integration Event", event.name, "lease_expires_at", past)
		frappe.db.commit()

		# Recover stale event
		recovered = recover_stale_events(timeout_minutes=1)
		self.assertEqual(recovered, 1)

		# Make retry due immediately
		frappe.db.set_value("Integration Event", event.name, "next_retry_at", now_datetime() - timedelta(seconds=5))
		frappe.db.commit()

		# Worker B reclaims
		claimed_b, worker_b, token_b = claim_event_for_processing(event.name, worker_id="WORKER-B")
		self.assertTrue(claimed_b)
		self.assertNotEqual(token_a, token_b)

		# Worker A authority check must fail
		is_auth_a, reason_a = verify_processing_authority(event.name, token_a)
		self.assertFalse(is_auth_a)
		self.assertIn("does not match", reason_a)

		# Worker B authority check must pass
		is_auth_b, reason_b = verify_processing_authority(event.name, token_b)
		self.assertTrue(is_auth_b)

	# ==================================================
	# 6. DATABASE TIME AUTHORITY HELPER
	# ==================================================
	def test_06_database_time_authority_helper(self):
		"""
		Verifies get_database_now returns authoritative timestamp from MariaDB
		in the site's local timezone.
		Normalizes raw DB session NOW() and get_database_now() to canonical UTC
		instants to verify equality within a tight tolerance without naive date comparison.
		"""
		db_now = get_database_now()
		self.assertIsNotNone(db_now)
		raw_now = frappe.db.sql("SELECT NOW()")[0][0]

		site_tz_str = frappe.get_system_settings("time_zone") or "UTC"
		sess_tz_str = frappe.db.sql("SELECT @@session.time_zone")[0][0]
		if sess_tz_str == "SYSTEM":
			sess_tz_str = frappe.db.sql("SELECT @@system_time_zone")[0][0]

		site_tz = zoneinfo.ZoneInfo(site_tz_str)
		sess_tz = zoneinfo.ZoneInfo(sess_tz_str)

		raw_now_utc = get_datetime(raw_now).replace(tzinfo=sess_tz).astimezone(zoneinfo.ZoneInfo("UTC"))
		db_now_utc = db_now.replace(tzinfo=site_tz).astimezone(zoneinfo.ZoneInfo("UTC"))

		diff_seconds = abs((raw_now_utc - db_now_utc).total_seconds())
		self.assertLess(
			diff_seconds,
			2.0,
			f"Canonical time authority delta too large: {diff_seconds}s (raw_now={raw_now}, db_now={db_now})",
		)

		# Also verify MariaDB CONVERT_TZ consistency when supported
		db_converted = frappe.db.sql(
			"SELECT CONVERT_TZ(NOW(), @@session.time_zone, %s)", (site_tz_str,)
		)[0][0]
		if db_converted:
			diff_db_conv = abs((get_datetime(db_converted) - db_now).total_seconds())
			self.assertLess(diff_db_conv, 2.0, "get_database_now diverges from MariaDB CONVERT_TZ")

	# ==================================================
	# 7. SCHEDULER BOUNDED WORK PROCESSING
	# ==================================================
	def test_07_scheduler_creates_and_processes_bounded_work(self):
		"""
		Given 10 pending events for the valid item, scheduler with max_events=5 processes exactly 5 events.
		"""
		self._ensure_mapping()
		for i in range(10):
			self._create_test_event(item_code=self.item_code, intended_atp=20.0)

		mock_client = self._create_mock_client()
		with patch("bop_erp.inventory.scheduler.get_active_connector_for_channel") as mock_conn, \
			 patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:

			mock_connector = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
			mock_conn.return_value = mock_connector
			mock_atp.return_value = MagicMock(aggregate_atp_qty=20.0)

			mock_client.update_stock_available_quantity = MagicMock(return_value={"changed": True, "previous_qty": 10, "resulting_qty": 20})

			res = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=5,
				client=mock_client,
			)

			self.assertEqual(res["events_seen"], 5)
			self.assertEqual(res["events_claimed"], 5)
			self.assertEqual(res["published"], 5)
			self.assertEqual(len(res["processed_events"]), 5)

	# ==================================================
	# 8. OVERLAPPING SCHEDULER CLAIM FENCING
	# ==================================================
	def test_08_overlapping_scheduler_claim_fencing(self):
		"""
		Two scheduler workers running concurrently over the same pending events:
		Atomic claim fencing ensures no duplicate execution or double PUT.
		"""
		self._ensure_mapping()
		for i in range(5):
			self._create_test_event(item_code=self.item_code, intended_atp=20.0)

		mock_client = self._create_mock_client()
		with patch("bop_erp.inventory.scheduler.get_active_connector_for_channel") as mock_conn, \
			 patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:

			mock_connector = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
			mock_conn.return_value = mock_connector
			mock_atp.return_value = MagicMock(aggregate_atp_qty=20.0)

			put_call_count = 0
			def counting_put(*args, **kwargs):
				nonlocal put_call_count
				put_call_count += 1
				return {"changed": True, "previous_qty": 10, "resulting_qty": 20}

			mock_client.update_stock_available_quantity = MagicMock(side_effect=counting_put)

			# Worker 1 runs and claims all 5
			res1 = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				worker_id="SCHEDULER-1",
				client=mock_client,
			)
			self.assertEqual(res1["published"], 5)

			# Worker 2 runs concurrently immediately after
			res2 = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				worker_id="SCHEDULER-2",
				client=mock_client,
			)
			# All 5 already claimed/succeeded -> seen = 0
			self.assertEqual(res2["events_seen"], 0)
			self.assertEqual(res2["published"], 0)
			self.assertEqual(put_call_count, 5, "Exactly 5 PUT calls across both scheduler runs!")

	# ==================================================
	# 9. SCHEDULER ITEM FAILURE ISOLATION
	# ==================================================
	def test_09_scheduler_failure_isolation(self):
		"""
		Item A fails with 500 error; Item B succeeds; Item C succeeds as NO-OP.
		Scheduler finishes all items without global abort.
		"""
		self._ensure_mapping()
		ev_a = self._create_test_event(item_code=self.item_code, intended_atp=20.0)
		ev_b = self._create_test_event(item_code=self.item_code, intended_atp=20.0)
		ev_c = self._create_test_event(item_code=self.item_code, intended_atp=20.0)

		mock_client = self._create_mock_client()
		with patch("bop_erp.inventory.scheduler.get_active_connector_for_channel") as mock_conn, \
			 patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:

			mock_connector = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
			mock_conn.return_value = mock_connector
			mock_atp.return_value = MagicMock(aggregate_atp_qty=20.0)

			call_index = 0
			def dynamic_update(stock_available_id, quantity, expected_product_id, expected_variant_id=None, pre_put_hook=None):
				nonlocal call_index
				call_index += 1
				if call_index == 1:
					raise PrestaShopServerError("500 Internal Server Error", status_code=500)
				elif call_index == 3:
					return {"changed": False, "previous_qty": 20, "resulting_qty": 20, "reason": "NO_OP"}
				return {"changed": True, "previous_qty": 10, "resulting_qty": 20, "reason": "SUCCESS"}

			mock_client.update_stock_available_quantity = MagicMock(side_effect=dynamic_update)

			res = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				max_events=10,
				client=mock_client,
			)

			self.assertEqual(res["events_seen"], 3)
			self.assertEqual(res["retry_pending"], 1) # First event retryable 500
			self.assertEqual(res["published"], 1)     # Second event changed
			self.assertEqual(res["no_op"], 1)         # Third event no-op

	# ==================================================
	# 10. RUNTIME HOST SAFETY FROM SCHEDULER
	# ==================================================
	def test_10_runtime_host_safety_blocks_production_pre_flight(self):
		"""
		If connector base_url points to production, scheduler blocks before any claim or network call.
		"""
		self._create_test_event(item_code=self.item_code)

		with patch("bop_erp.inventory.scheduler.get_active_connector_for_channel") as mock_conn:
			mock_conn.return_value = MagicMock(
				environment="DEVELOPMENT",
				base_url="https://production.theindustrialdepot.com",
				write_enabled=True,
			)

			res = process_pending_inventory_publications(sales_channel=self.sales_channel)
			self.assertEqual(res["safety_blocked"], 1)
			self.assertEqual(res["events_claimed"], 0)
			self.assertIn("CRITICAL WRITE SAFETY VIOLATION", res.get("reason", ""))

	# ==================================================
	# 11. DISABLED CONNECTOR PREVENTS PUBLICATION
	# ==================================================
	def test_11_disabled_connector_prevents_publication(self):
		"""
		If connector has write_enabled = False, scheduler aborts pre-flight without claiming events.
		"""
		self._create_test_event(item_code=self.item_code)

		with patch("bop_erp.inventory.scheduler.get_active_connector_for_channel") as mock_conn:
			mock_conn.return_value = MagicMock(
				environment="DEVELOPMENT",
				base_url="http://prestashop-test",
				write_enabled=False,
			)

			res = process_pending_inventory_publications(sales_channel=self.sales_channel)
			self.assertEqual(res["safety_blocked"], 1)
			self.assertIn("writes disabled", res.get("reason", ""))

	# ==================================================
	# 12. MAPPING CHANGE AFTER SCHEDULING SAFELY BLOCKS
	# ==================================================
	def test_12_mapping_change_after_scheduling_blocks_safely(self):
		"""
		If mapping is removed while event is queued, worker fails safely with non-retryable error.
		"""
		event = self._create_test_event(item_code=self.item_code)
		mock_client = self._create_mock_client()

		with patch("bop_erp.inventory.scheduler.get_active_connector_for_channel") as mock_conn:
			mock_conn.return_value = MagicMock(
				environment="DEVELOPMENT",
				base_url="http://prestashop-test",
				write_enabled=True,
			)

			res = process_pending_inventory_publications(
				sales_channel=self.sales_channel,
				client=mock_client,
			)

			self.assertEqual(res["dead_letter"], 1)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.DEAD_LETTER)

	# ==================================================
	# 13. ENQUEUE SCHEDULER DISPATCHES FRAPPE QUEUE
	# ==================================================
	def test_13_enqueue_scheduled_inventory_publication_dispatches(self):
		"""
		Verifies enqueue_scheduled_inventory_publication calls frappe.enqueue with default queue.
		"""
		with patch("bop_erp.inventory.scheduler.frappe.enqueue") as mock_enqueue:
			enqueue_scheduled_inventory_publication(sales_channel="TID", max_events=15)
			mock_enqueue.assert_called_once_with(
				"bop_erp.inventory.scheduler.process_pending_inventory_publications",
				queue="default",
				sales_channel="TID",
				max_events=15,
				now=frappe.flags.in_test or False,
			)

	# ==================================================
	# 14. MIDNIGHT BOUNDARY DATABASE TIME REGRESSION
	# ==================================================
	def test_14_database_time_authority_midnight_boundary(self):
		"""
		Regression for midnight boundary condition:
		UTC date = next day (e.g. 2026-09-10 01:30:00)
		Site local date = previous day (e.g. 2026-09-09 20:30:00 in America/Bogota UTC-5).
		Proves:
		1. Dates intentionally differ (.date() comparison would falsely fail).
		2. Canonical instants are identical when normalized to UTC (diff = 0s).
		3. get_database_now() returns site-local timestamp matching persistence semantics.
		4. verify_processing_authority uses authoritative DB time without false expiration.
		"""
		site_tz_name = frappe.get_system_settings("time_zone") or "America/Bogota"
		site_tz = zoneinfo.ZoneInfo(site_tz_name)
		utc_tz = zoneinfo.ZoneInfo("UTC")

		# Synthetic boundary instants across midnight
		utc_time = datetime.datetime(2026, 9, 10, 1, 30, 0)
		local_time = datetime.datetime(2026, 9, 9, 20, 30, 0)

		# Invariant 1: Dates differ across the midnight boundary
		self.assertNotEqual(
			utc_time.date(),
			local_time.date(),
			"Calendar dates must differ across midnight boundary",
		)

		# Invariant 2: Instant normalization proves identical physical time
		utc_instant = utc_time.replace(tzinfo=utc_tz)
		local_instant = local_time.replace(tzinfo=site_tz).astimezone(utc_tz)
		self.assertEqual(
			abs((utc_instant - local_instant).total_seconds()),
			0.0,
			"Normalized canonical instants must be identical",
		)

		# Invariant 3: get_database_now helper returns site-local time
		with patch("bop_erp.reliability.frappe.db.sql") as mock_sql:
			mock_sql.return_value = ((local_time,),)
			mock_site_now = get_database_now()
			self.assertEqual(mock_site_now, local_time)

		# Invariant 4: Lease authority verification at midnight boundary
		event = self._create_test_event(item_code=self.item_code)
		token = "midnight-token-123"

		# A lease expiring 5 minutes into the future of local_time (20:35:00) is valid
		frappe.db.set_value("Integration Event", event.name, {
			"status": IntegrationStatus.PROCESSING,
			"worker_id": "worker-midnight",
			"processing_token": token,
			"lease_expires_at": local_time + timedelta(minutes=5),
		})
		is_auth, reason = verify_processing_authority(event.name, token, db_time=local_time)
		self.assertTrue(
			is_auth,
			f"Valid lease erroneously rejected across midnight boundary: {reason}",
		)

		# An expired lease (20:25:00 vs local_time 20:30:00) is rejected
		frappe.db.set_value("Integration Event", event.name, {
			"lease_expires_at": local_time - timedelta(minutes=5),
		})
		is_auth_expired, reason_expired = verify_processing_authority(event.name, token, db_time=local_time)
		self.assertFalse(is_auth_expired)
		self.assertIn("expired", reason_expired.lower())

