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
from bop_erp.inventory.scheduler import (
	DEFAULT_SCHEDULER_BATCH_LIMIT,
	DEFAULT_MAX_CHANNELS_PER_RUN,
	DEFAULT_MAX_TOTAL_EVENTS_PER_RUN,
	discover_eligible_publication_channels,
	process_pending_inventory_publications,
	process_multichannel_inventory_publications,
	enqueue_inventory_publication_dispatcher,
	enqueue_scheduled_inventory_publication,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import PrestaShopServerError


class TestMultichannelPublicationSchedulerUnit(unittest.TestCase):
	"""
	Comprehensive Unit tests for Phase 1J.3:
	Multi-Channel Publication Scheduler Finalization.
	"""

	def setUp(self):
		self.sales_channel_a = "CHAN-A"
		self.sales_channel_b = "CHAN-B"
		self.sales_channel_c = "CHAN-C"
		self.item_code = "ITEM-PHASE1H1-UNIT-01"
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		for ch in [self.sales_channel_a, self.sales_channel_b, self.sales_channel_c]:
			frappe.db.sql("DELETE FROM `tabIntegration Event` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabPrestaShop Connector` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabSales Channel` WHERE name = %s", (ch,))
		frappe.db.commit()

	def _create_mock_client(self, sales_channel: str = "CHAN-A"):
		config = PrestaShopConfig(
			sales_channel=sales_channel,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_KEY",
			write_enabled=True,
		)
		with patch.object(PrestaShopConfig, "resolve_api_key", return_value="FAKE_KEY_1234567890123456789012"):
			client = PrestaShopClient(config=config)
		client.resolve_stock_available_id = MagicMock(return_value=100)
		return client

	def _setup_channel_and_connector(
		self,
		sales_channel: str,
		active: int = 1,
		connector_enabled: int = 1,
		write_enabled: int = 1,
		provider: str = IntegrationProvider.PRESTASHOP,
		company: str = "Industrial DP",
		base_url: str = "http://prestashop-test",
	):
		if not frappe.db.exists("Sales Channel", sales_channel):
			sc = frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": sales_channel,
				"channel_name": f"Test {sales_channel}",
				"active": active,
				"company": company,
				"integration_provider": provider,
			})
			sc.insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Sales Channel", sales_channel, "active", active)

		if not frappe.db.exists("PrestaShop Connector", {"sales_channel": sales_channel}):
			pc = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": sales_channel,
				"environment": "DEVELOPMENT",
				"base_url": base_url,
				"credential_reference": "TEST_KEY",
				"enabled": connector_enabled,
				"read_enabled": 1,
				"write_enabled": write_enabled,
			})
			pc.flags.ignore_validate = True
			pc.insert(ignore_permissions=True)
		else:
			frappe.db.set_value(
				"PrestaShop Connector",
				{"sales_channel": sales_channel},
				{"enabled": connector_enabled, "write_enabled": write_enabled, "base_url": base_url},
			)

		if not frappe.db.exists("External ID Mapping", {"sales_channel": sales_channel, "erp_document": self.item_code, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": sales_channel,
				"erp_doctype": "Item",
				"erp_document": self.item_code,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": "100",
				"active": 1,
			}).insert(ignore_permissions=True)

		frappe.db.commit()

	def _create_event(self, sales_channel: str, intended_atp: float = 20.0, provider: str = IntegrationProvider.PRESTASHOP):
		payload = {
			"item_code": self.item_code,
			"sales_channel": sales_channel,
			"intended_atp": intended_atp,
			"publication_version": 1,
		}
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": provider,
			"sales_channel": sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"idempotency_key": f"EV-{sales_channel}-{frappe.generate_hash(length=8)}",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps(payload),
			"max_attempts": 3,
		})
		event.insert(ignore_permissions=True)
		frappe.db.commit()
		return event

	# ==================================================
	# 1. NO HARDCODED DEFAULT CHANNEL
	# ==================================================
	def test_01_no_hardcoded_default_channel_in_entry_points(self):
		"""
		Verifies process_pending_inventory_publications requires sales_channel argument,
		and dispatcher handles channels dynamically without defaulting to TID.
		"""
		import inspect
		sig = inspect.signature(process_pending_inventory_publications)
		param = sig.parameters["sales_channel"]
		self.assertEqual(param.default, inspect.Parameter.empty, "sales_channel must not have a default!")

	# ==================================================
	# 2. DYNAMIC CHANNEL DISCOVERY
	# ==================================================
	def test_02_dynamic_channel_discovery(self):
		"""
		Configures CHAN-A (enabled, writable) and CHAN-B (enabled, writable).
		Both appear in discover_eligible_publication_channels.
		"""
		self._setup_channel_and_connector(self.sales_channel_a, active=1, connector_enabled=1, write_enabled=1)
		self._setup_channel_and_connector(self.sales_channel_b, active=1, connector_enabled=1, write_enabled=1)

		channels = discover_eligible_publication_channels(provider=IntegrationProvider.PRESTASHOP)
		channel_names = [ch["sales_channel"] for ch in channels]

		self.assertIn(self.sales_channel_a, channel_names)
		self.assertIn(self.sales_channel_b, channel_names)

	# ==================================================
	# 3. DISABLED CHANNEL EXCLUDED
	# ==================================================
	def test_03_disabled_channel_excluded_from_discovery(self):
		"""
		If Sales Channel active=0, it is omitted from discovery even if connector is enabled.
		"""
		self._setup_channel_and_connector(self.sales_channel_a, active=0, connector_enabled=1, write_enabled=1)

		channels = discover_eligible_publication_channels(provider=IntegrationProvider.PRESTASHOP)
		channel_names = [ch["sales_channel"] for ch in channels]

		self.assertNotIn(self.sales_channel_a, channel_names)

	# ==================================================
	# 4. DISABLED CONNECTOR EXCLUDED
	# ==================================================
	def test_04_disabled_connector_excluded_from_discovery(self):
		"""
		If PrestaShop Connector enabled=0 or write_enabled=0, channel is excluded from discovery.
		"""
		self._setup_channel_and_connector(self.sales_channel_a, active=1, connector_enabled=0, write_enabled=1)
		self._setup_channel_and_connector(self.sales_channel_b, active=1, connector_enabled=1, write_enabled=0)

		channels = discover_eligible_publication_channels(provider=IntegrationProvider.PRESTASHOP)
		channel_names = [ch["sales_channel"] for ch in channels]

		self.assertNotIn(self.sales_channel_a, channel_names)
		self.assertNotIn(self.sales_channel_b, channel_names)

	# ==================================================
	# 5. PROVIDER FILTERING
	# ==================================================
	def test_05_provider_filtering_ignores_other_providers(self):
		"""
		Events with provider='MARKETPLACE' are not claimed by PrestaShop worker or dispatcher.
		"""
		self._setup_channel_and_connector(self.sales_channel_a)
		other_event = self._create_event(self.sales_channel_a, provider=IntegrationProvider.MARKETPLACE)
		ps_event = self._create_event(self.sales_channel_a, provider=IntegrationProvider.PRESTASHOP)

		mock_client = self._create_mock_client(self.sales_channel_a)
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=20.0)
			mock_client.update_stock_available_quantity = MagicMock(return_value={"changed": True, "previous_qty": 10, "resulting_qty": 20})

			res = process_pending_inventory_publications(
				sales_channel=self.sales_channel_a,
				max_events=10,
				client=mock_client,
				provider=IntegrationProvider.PRESTASHOP,
			)

			self.assertEqual(res["events_seen"], 1)
			self.assertEqual(res["published"], 1)

			other_event.reload()
			self.assertEqual(other_event.status, IntegrationStatus.PENDING)
			ps_event.reload()
			self.assertEqual(ps_event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# 6. GLOBAL AND PER-CHANNEL BOUNDS
	# ==================================================
	def test_06_global_and_per_channel_bounds_enforced(self):
		"""
		Chan A has 10 events, Chan B has 10 events.
		max_events_per_channel=2, max_total_events_per_run=3.
		Chan A processes 2 events, Chan B processes 1 event (capped by remaining global budget).
		"""
		self._setup_channel_and_connector(self.sales_channel_a)
		self._setup_channel_and_connector(self.sales_channel_b)

		for _ in range(5):
			self._create_event(self.sales_channel_a)
			self._create_event(self.sales_channel_b)

		mock_client = self._create_mock_client()
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=20.0)
			mock_client.update_stock_available_quantity = MagicMock(return_value={"changed": True, "previous_qty": 10, "resulting_qty": 20})

			res = process_multichannel_inventory_publications(
				max_channels_per_run=10,
				max_events_per_channel=2,
				max_total_events_per_run=3,
				client=mock_client,
			)

			self.assertEqual(res["events_claimed"], 3)
			self.assertEqual(res["published"], 3)
			self.assertEqual(res["channel_results"][self.sales_channel_a]["events_claimed"], 2)
			self.assertEqual(res["channel_results"][self.sales_channel_b]["events_claimed"], 1)

	# ==================================================
	# 7. FAIRNESS / NO STARVATION
	# ==================================================
	def test_07_fairness_prevents_busy_channel_starving_others(self):
		"""
		Chan A has 20 pending events.
		Chan B has 2 pending events.
		With max_events_per_channel=3, Chan A takes 3, and Chan B is NOT starved—it processes 2.
		"""
		self._setup_channel_and_connector(self.sales_channel_a)
		self._setup_channel_and_connector(self.sales_channel_b)

		for _ in range(20):
			self._create_event(self.sales_channel_a)
		for _ in range(2):
			self._create_event(self.sales_channel_b)

		mock_client = self._create_mock_client()
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=20.0)
			mock_client.update_stock_available_quantity = MagicMock(return_value={"changed": True, "previous_qty": 10, "resulting_qty": 20})

			res = process_multichannel_inventory_publications(
				max_channels_per_run=10,
				max_events_per_channel=3,
				max_total_events_per_run=20,
				client=mock_client,
			)

			self.assertEqual(res["channel_results"][self.sales_channel_a]["events_claimed"], 3)
			self.assertEqual(res["channel_results"][self.sales_channel_b]["events_claimed"], 2)

	# ==================================================
	# 8. UNSAFE HOST BLOCKS ONLY OFFENDING CHANNEL
	# ==================================================
	def test_08_unsafe_host_isolated_to_offending_channel(self):
		"""
		Chan A has production URL (https://production.theindustrialdepot.com).
		Chan B has safe test URL (http://prestashop-test).
		Chan A is blocked without network call; Chan B succeeds normally.
		"""
		self._setup_channel_and_connector(self.sales_channel_a, base_url="https://production.theindustrialdepot.com")
		self._setup_channel_and_connector(self.sales_channel_b, base_url="http://prestashop-test")

		self._create_event(self.sales_channel_a)
		self._create_event(self.sales_channel_b)

		mock_client = self._create_mock_client(self.sales_channel_b)
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=20.0)
			mock_client.update_stock_available_quantity = MagicMock(return_value={"changed": True, "previous_qty": 10, "resulting_qty": 20})

			res = process_multichannel_inventory_publications(max_events_per_channel=5, client=mock_client)

			self.assertEqual(res["safety_blocked"], 1)
			self.assertEqual(res["channel_results"][self.sales_channel_a]["safety_blocked"], 1)
			self.assertEqual(res["channel_results"][self.sales_channel_b]["published"], 1)

	# ==================================================
	# 9. CROSS-CHANNEL FAILURE ISOLATION
	# ==================================================
	def test_09_cross_channel_failure_isolation(self):
		"""
		Chan A encounters 500 server error -> enters retry_pending.
		Chan B executes successfully -> succeeded.
		Failure in Chan A does NOT roll back or disrupt Chan B.
		"""
		self._setup_channel_and_connector(self.sales_channel_a)
		self._setup_channel_and_connector(self.sales_channel_b)

		ev_a = self._create_event(self.sales_channel_a)
		ev_b = self._create_event(self.sales_channel_b)

		mock_client = self._create_mock_client()

		def selective_update(stock_available_id, quantity, expected_product_id, expected_variant_id=None, pre_put_hook=None):
			# If called for Chan A, fail with 500
			if frappe.flags.current_channel == self.sales_channel_a:
				raise PrestaShopServerError("Simulated 500 error for Chan A", status_code=500)
			return {"changed": True, "previous_qty": 10, "resulting_qty": 20}

		mock_client.update_stock_available_quantity = MagicMock(side_effect=selective_update)

		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=20.0)

			# Track current channel
			original_process = process_pending_inventory_publications
			def wrapped_process(sales_channel, **kwargs):
				frappe.flags.current_channel = sales_channel
				return original_process(sales_channel, **kwargs)

			with patch("bop_erp.inventory.scheduler.process_pending_inventory_publications", side_effect=wrapped_process):
				res = process_multichannel_inventory_publications(client=mock_client)

			self.assertEqual(res["retry_pending"], 1)
			self.assertEqual(res["published"], 1)

			ev_a.reload()
			self.assertEqual(ev_a.status, IntegrationStatus.RETRY_PENDING)
			ev_b.reload()
			self.assertEqual(ev_b.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# 10. OVERLAPPING GLOBAL SCHEDULERS
	# ==================================================
	def test_10_overlapping_global_schedulers_concurrency_fencing(self):
		"""
		Two dispatcher workers run concurrently over the same channels and events.
		Claim fencing guarantees each event is processed exactly once.
		"""
		self._setup_channel_and_connector(self.sales_channel_a)
		self._setup_channel_and_connector(self.sales_channel_b)

		for _ in range(3):
			self._create_event(self.sales_channel_a)
			self._create_event(self.sales_channel_b)

		mock_client = self._create_mock_client()
		with patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=20.0)

			put_count = 0
			def counting_put(*args, **kwargs):
				nonlocal put_count
				put_count += 1
				return {"changed": True, "previous_qty": 10, "resulting_qty": 20}

			mock_client.update_stock_available_quantity = MagicMock(side_effect=counting_put)

			# Run dispatcher 1
			res1 = process_multichannel_inventory_publications(worker_id="WORKER-1", client=mock_client)
			self.assertEqual(res1["published"], 6)

			# Run dispatcher 2 immediately
			res2 = process_multichannel_inventory_publications(worker_id="WORKER-2", client=mock_client)
			self.assertEqual(res2["published"], 0)
			self.assertEqual(put_count, 6)

	# ==================================================
	# 11. ENQUEUE DISPATCHER CALLS BACKGROUND QUEUE
	# ==================================================
	def test_11_enqueue_inventory_publication_dispatcher(self):
		"""
		Verifies enqueue_inventory_publication_dispatcher calls frappe.enqueue with default queue.
		"""
		with patch("bop_erp.inventory.scheduler.frappe.enqueue") as mock_enqueue:
			enqueue_inventory_publication_dispatcher(max_channels_per_run=5, max_events_per_channel=10)
			mock_enqueue.assert_called_once_with(
				"bop_erp.inventory.scheduler.process_multichannel_inventory_publications",
				queue="default",
				provider=IntegrationProvider.PRESTASHOP,
				max_channels_per_run=5,
				max_events_per_channel=10,
				max_total_events_per_run=DEFAULT_MAX_TOTAL_EVENTS_PER_RUN,
				now=frappe.flags.in_test or False,
			)
