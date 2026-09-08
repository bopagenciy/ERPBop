# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from datetime import datetime, timedelta
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
	get_database_now,
	claim_event_for_processing,
	recover_stale_events,
)
from bop_erp.monitoring import (
	get_system_health,
	get_system_readiness,
	DEFAULT_THRESHOLDS,
	ThresholdSeverity,
)
from bop_erp.operations import (
	get_failed_review_orders,
	get_dead_letter_events,
	get_retry_pending_events,
	get_stale_processing_events,
	inspect_order_failure_reason,
	retry_failed_publication_event,
	recover_stale_publication_events,
	reprocess_inbound_order_event,
)
import bop_erp.hooks as bop_hooks


class TestProductionReadinessUnit(unittest.TestCase):
	"""
	Phase 1M unit test suite validating:
	1. Health and readiness reporting with thresholds and severity classification.
	2. Operator inspection and manual recovery actions.
	3. Strict fail-closed environment security and host filtering.
	4. Secret hygiene (zero credential exposure).
	5. Scheduler configuration and hook audit.
	6. UTC/timezone consistency in reliability lease management.
	"""

	def setUp(self):
		pass

	def tearDown(self):
		pass

	# =========================================================================
	# 1. System Health and Readiness Tests
	# =========================================================================

	@patch("frappe.cache")
	@patch("frappe.db.sql")
	def test_system_health_healthy(self, mock_sql, mock_cache):
		mock_sql.return_value = [[1]]
		mock_redis = MagicMock()
		mock_redis.ping.return_value = True
		mock_cache.return_value = mock_redis

		with patch("redis.from_url") as mock_from_url:
			queue_redis = MagicMock()
			queue_redis.ping.return_value = True
			mock_from_url.return_value = queue_redis

			health = get_system_health()
			self.assertEqual(health["status"], "UP")
			self.assertTrue(health["database"]["alive"])
			self.assertTrue(health["redis_cache"]["alive"])
			self.assertTrue(health["redis_queue"]["alive"])

	@patch("frappe.cache")
	@patch("frappe.db.sql")
	def test_system_health_degraded_when_redis_down(self, mock_sql, mock_cache):
		mock_sql.return_value = [[1]]
		mock_redis = MagicMock()
		mock_redis.ping.side_effect = Exception("Connection refused")
		mock_cache.return_value = mock_redis

		with patch("redis.from_url") as mock_from_url:
			queue_redis = MagicMock()
			queue_redis.ping.return_value = True
			mock_from_url.return_value = queue_redis

			health = get_system_health()
			self.assertEqual(health["status"], "DOWN")
			self.assertTrue(health["database"]["alive"])
			self.assertFalse(health["redis_cache"]["alive"])

	@patch("frappe.db.sql")
	def test_system_health_unhealthy_when_db_down(self, mock_sql):
		mock_sql.side_effect = Exception("DB disconnected")

		health = get_system_health()
		self.assertEqual(health["status"], "DOWN")
		self.assertFalse(health["database"]["alive"])

	@patch("frappe.db.count")
	@patch("frappe.db.sql")
	@patch("bop_erp.monitoring.get_system_health")
	def test_system_readiness_all_ok(self, mock_health, mock_sql, mock_count):
		mock_health.return_value = {"status": "UP"}
		mock_sql.side_effect = [
			[],  # Integration Event group by status
			[[0]],  # stale processing count
			[],  # oldest pending
		]
		mock_count.return_value = 0  # failed review count

		readiness = get_system_readiness()
		self.assertTrue(readiness["ready"])
		self.assertEqual(readiness["severity"], ThresholdSeverity.OK)
		self.assertEqual(len(readiness["violations"]), 0)

	@patch("frappe.db.count")
	@patch("frappe.db.sql")
	@patch("bop_erp.monitoring.get_system_health")
	def test_system_readiness_critical_dead_letter(self, mock_health, mock_sql, mock_count):
		mock_health.return_value = {"status": "UP"}
		mock_sql.side_effect = [
			[{"status": IntegrationStatus.DEAD_LETTER, "cnt": 5}],
			[[0]],  # stale processing
			[],  # oldest pending
		]
		mock_count.return_value = 0

		readiness = get_system_readiness()
		self.assertFalse(readiness["ready"])
		self.assertEqual(readiness["severity"], ThresholdSeverity.CRITICAL)
		self.assertTrue(any("Dead letter" in v for v in readiness["violations"]))

	@patch("frappe.db.count")
	@patch("frappe.db.sql")
	@patch("bop_erp.monitoring.get_system_health")
	def test_system_readiness_warning_stale_processing(self, mock_health, mock_sql, mock_count):
		mock_health.return_value = {"status": "UP"}
		mock_sql.side_effect = [
			[],  # status counts
			[[2]],  # stale processing = 2 (stale threshold = 0)
			[],  # oldest pending
		]
		mock_count.return_value = 0

		readiness = get_system_readiness()
		self.assertFalse(readiness["ready"])
		self.assertEqual(readiness["severity"], ThresholdSeverity.WARNING)
		self.assertTrue(any("Stale processing" in v for v in readiness["violations"]))

	@patch("frappe.db.count")
	@patch("frappe.db.sql")
	@patch("bop_erp.monitoring.get_system_health")
	def test_system_readiness_custom_thresholds(self, mock_health, mock_sql, mock_count):
		mock_health.return_value = {"status": "UP"}
		mock_sql.side_effect = [
			[{"status": IntegrationStatus.PENDING, "cnt": 10}],
			[[0]],
			[],
		]
		mock_count.return_value = 0

		# Overriding pending_event_max_count = 5 (default is 50)
		custom_thresholds = {
			"pending_event_max_count": 5,
		}
		readiness = get_system_readiness(thresholds=custom_thresholds)
		self.assertFalse(readiness["ready"])
		self.assertEqual(readiness["severity"], ThresholdSeverity.WARNING)
		self.assertTrue(any("Pending event backlog high" in v for v in readiness["violations"]))

	# =========================================================================
	# 2. Strict Fail-Closed Environment Security Tests
	# =========================================================================

	def test_safe_target_valid_development(self):
		assert_safe_write_target("DEVELOPMENT", "http://127.0.0.1:8080/api")
		assert_safe_write_target("DEVELOPMENT", "http://localhost:8080")
		assert_safe_write_target("DEVELOPMENT", "http://prestashop-test:80")

	def test_safe_target_missing_environment(self):
		with self.assertRaises(ConnectorSafetyError) as ctx:
			assert_safe_write_target(None, "http://127.0.0.1:8080")
		self.assertIn("Missing environment", str(ctx.exception))

		with self.assertRaises(ConnectorSafetyError) as ctx:
			assert_safe_write_target("", "http://127.0.0.1:8080")
		self.assertIn("Missing environment", str(ctx.exception))

	def test_safe_target_unauthorized_environment(self):
		with self.assertRaises(ConnectorSafetyError) as ctx:
			assert_safe_write_target("PRODUCTION", "http://127.0.0.1:8080")
		self.assertIn("only authorized in DEVELOPMENT environment", str(ctx.exception))

		with self.assertRaises(ConnectorSafetyError) as ctx:
			assert_safe_write_target("STAGING", "http://127.0.0.1:8080")
		self.assertIn("only authorized in DEVELOPMENT environment", str(ctx.exception))

		with self.assertRaises(ConnectorSafetyError) as ctx:
			assert_safe_write_target("TEST", "http://127.0.0.1:8080")
		self.assertIn("only authorized in DEVELOPMENT environment", str(ctx.exception))

	def test_safe_target_forbidden_production_host(self):
		with self.assertRaises(ConnectorSafetyError) as ctx:
			assert_safe_write_target("DEVELOPMENT", "https://theindustrialdepot.com/api")
		self.assertIn("forbidden production domain", str(ctx.exception))

		with self.assertRaises(ConnectorSafetyError) as ctx:
			assert_safe_write_target("DEVELOPMENT", "http://192.168.1.100:8080")
		self.assertIn("not in the authorized local test write allowlist", str(ctx.exception))

	def test_safe_target_malformed_url(self):
		with self.assertRaises(ConnectorSafetyError) as ctx:
			assert_safe_write_target("DEVELOPMENT", "not-a-valid-url")
		self.assertIn("not in the authorized local test write allowlist", str(ctx.exception))

	# =========================================================================
	# 3. Secret Hygiene Tests
	# =========================================================================

	@patch("frappe.cache")
	@patch("frappe.db.sql")
	def test_monitoring_health_no_secrets_exposed(self, mock_sql, mock_cache):
		mock_sql.return_value = [[1]]
		mock_redis = MagicMock()
		mock_redis.ping.return_value = True
		mock_cache.return_value = mock_redis

		with patch("redis.from_url") as mock_from_url:
			queue_redis = MagicMock()
			queue_redis.ping.return_value = True
			mock_from_url.return_value = queue_redis

			health = get_system_health()
			dumped = json.dumps(health).lower()
			self.assertNotIn("secret", dumped)
			self.assertNotIn("password", dumped)
			self.assertNotIn("api_key", dumped)
			self.assertNotIn("token", dumped)

	@patch("frappe.db.count")
	@patch("frappe.db.sql")
	@patch("bop_erp.monitoring.get_system_health")
	def test_monitoring_readiness_no_secrets_exposed(self, mock_health, mock_sql, mock_count):
		mock_health.return_value = {"status": "UP"}
		mock_sql.side_effect = [[], [[0]], []]
		mock_count.return_value = 0
		readiness = get_system_readiness()
		dumped = json.dumps(readiness).lower()
		self.assertNotIn("secret", dumped)
		self.assertNotIn("password", dumped)
		self.assertNotIn("api_key", dumped)
		self.assertNotIn("token", dumped)

	# =========================================================================
	# 4. Operator Inspection and Recovery Helper Tests
	# =========================================================================

	@patch("frappe.get_all")
	def test_get_failed_review_orders(self, mock_get_all):
		mock_get_all.return_value = [
			{
				"name": "ORD-001",
				"customer": "CUST-001",
				"sales_channel": "PrestaShop Test",
				"integration_error": "Out of stock",
			}
		]
		res = get_failed_review_orders(limit=10)
		self.assertEqual(len(res), 1)
		self.assertEqual(res[0]["name"], "ORD-001")
		self.assertEqual(res[0]["integration_error"], "Out of stock")

	@patch("frappe.get_all")
	def test_get_dead_letter_events(self, mock_get_all):
		mock_get_all.return_value = [
			{
				"name": "EVT-DL-001",
				"sales_channel": "PrestaShop Test",
				"entity_type": "Inventory",
				"direction": "Outbound",
				"attempt_count": 5,
				"last_error_message": "Unrecoverable 400 bad request",
			}
		]
		res = get_dead_letter_events()
		self.assertEqual(len(res), 1)
		self.assertEqual(res[0]["name"], "EVT-DL-001")

	@patch("frappe.db.sql")
	def test_get_stale_processing_events(self, mock_sql):
		mock_sql.return_value = [
			{
				"name": "EVT-STALE-001",
				"sales_channel": "PrestaShop Test",
				"worker_id": "worker-dead-123",
				"lease_expires_at": "2026-09-08 10:00:00",
			}
		]
		res = get_stale_processing_events()
		self.assertEqual(len(res), 1)
		self.assertEqual(res[0]["name"], "EVT-STALE-001")

	@patch("frappe.get_doc")
	def test_inspect_order_failure_reason(self, mock_get_doc):
		mock_so = MagicMock()
		mock_so.name = "ORD-FAIL-01"
		mock_so.sales_channel = "PrestaShop Test"
		mock_so.integration_status = "Failed Review"
		mock_so.integration_error = "Reservation failure: item not found"
		mock_so.docstatus = 0
		mock_item = MagicMock()
		mock_item.item_code = "TEST-ITEM-1"
		mock_item.qty = 2
		mock_item.warehouse = "Stores - TC"
		mock_so.items = [mock_item]
		mock_get_doc.return_value = mock_so

		details = inspect_order_failure_reason("ORD-FAIL-01")
		self.assertEqual(details["name"], "ORD-FAIL-01")
		self.assertEqual(details["integration_error"], "Reservation failure: item not found")
		self.assertEqual(len(details["items"]), 1)

	@patch("frappe.db.commit")
	@patch("frappe.db.sql")
	@patch("frappe.get_doc")
	def test_retry_failed_publication_event(self, mock_get_doc, mock_sql, mock_commit):
		mock_ev = MagicMock()
		mock_ev.name = "EVT-PUB-01"
		mock_ev.direction = IntegrationDirection.OUTBOUND
		mock_ev.status = IntegrationStatus.DEAD_LETTER
		mock_get_doc.return_value = mock_ev

		res = retry_failed_publication_event("EVT-PUB-01")
		self.assertTrue(res["success"])
		self.assertEqual(res["status"], IntegrationStatus.RETRY_PENDING)
		mock_sql.assert_called_once()
		mock_commit.assert_called_once()

	@patch("bop_erp.operations.process_order_ingestion_event")
	@patch("frappe.get_doc")
	def test_reprocess_inbound_order_event(self, mock_get_doc, mock_process):
		mock_ev = MagicMock()
		mock_ev.name = "EVT-ORD-01"
		mock_ev.direction = IntegrationDirection.INBOUND
		mock_get_doc.return_value = mock_ev
		mock_process.return_value = {"success": True, "sales_order": "SO-001"}

		res = reprocess_inbound_order_event("EVT-ORD-01")
		self.assertTrue(res["success"])
		mock_process.assert_called_once_with("EVT-ORD-01")

	# =========================================================================
	# 5. Scheduler Hook Registration Tests
	# =========================================================================

	def test_scheduler_events_registration(self):
		cron_events = bop_hooks.scheduler_events.get("cron", {})
		self.assertIn("*/5 * * * *", cron_events)
		self.assertIn(
			"bop_erp.inventory.scheduler.enqueue_inventory_publication_dispatcher",
			cron_events["*/5 * * * *"],
		)

		self.assertIn("*/10 * * * *", cron_events)
		self.assertIn(
			"bop_erp.reliability.recover_stale_events",
			cron_events["*/10 * * * *"],
		)

		self.assertIn("0 */4 * * *", cron_events)
		self.assertIn(
			"bop_erp.inventory.scheduler.enqueue_periodic_channel_reconciliation",
			cron_events["0 */4 * * *"],
		)

	# =========================================================================
	# 6. Timezone and Reliability Lease Consistency Tests
	# =========================================================================

	def test_get_database_now_consistency(self):
		now = get_database_now()
		self.assertIsNotNone(now)
		dt = get_datetime(now)
		self.assertIsInstance(dt, datetime)
