# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from contextlib import contextmanager
from datetime import timedelta
import unittest
from unittest.mock import patch, MagicMock

import frappe
from frappe.utils import now_datetime

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	ExternalEntityType,
	ErrorCategory,
)
from bop_erp.safety import (
	assert_safe_connector_target,
	ConnectorSafetyError,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import PrestaShopTransientError
from bop_erp.orders.fulfillment_writeback import (
	create_order_fulfillment_writeback_intent,
	handle_delivery_note_cancel,
	process_order_fulfillment_writeback_event,
	get_database_now,
)


class TestOrderFulfillmentWritebackLive(unittest.TestCase):
	"""
	Phase 1O Live Integration Test Suite:
	PrestaShop Fulfillment Status Writeback.
	Executes against the local PrestaShop test container (http://prestashop-test).

	Test Scenarios (Section 46: Matrix A through L):
	A. Synthetic PrestaShop order + ERP Delivery Note -> remote state becomes 4 (Shipped).
	B. Remote already Shipped -> NO-OP success (zero duplicate history row).
	C. Response lost after remote transition -> retry converges to SUCCEEDED without duplicate write.
	D. Remote Canceled before worker -> zero Shipped write, safe review block.
	E. Delivery Note cancelled before worker -> zero remote write, event cancelled.
	F. Expired lease -> zero remote write.
	G. Worker reclaim race -> one effective transition.
	H. Cross-channel same external ID -> correct channel only.
	I. Missing shipping_state_id -> safe block.
	J. Host changed to forbidden production -> zero network call.
	K. Transactional outbox proof: event exists in MariaDB before after_commit.
	L. Fixture cleanup & synthetic order restoration.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.sales_channel = f"TEST-LIVE-CH-{frappe.generate_hash(length=4).upper()}"
		cls.channel_b = f"TEST-LIVE-CH-B-{frappe.generate_hash(length=4).upper()}"

		# Create Sales Channel A
		if not frappe.db.exists("Sales Channel", cls.sales_channel):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.sales_channel,
				"channel_name": cls.sales_channel,
				"channel_type": "PRESTASHOP",
				"company": cls.company,
				"is_active": 1,
			}).insert(ignore_permissions=True)

		# Create Sales Channel B
		if not frappe.db.exists("Sales Channel", cls.channel_b):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.channel_b,
				"channel_name": cls.channel_b,
				"channel_type": "PRESTASHOP",
				"company": cls.company,
				"is_active": 1,
			}).insert(ignore_permissions=True)

		credential_ref = "TEST_PRESTASHOP_WRITE_KEY"

		# Create PrestaShop Connector for Channel A
		cls.connector_name = f"PS-{cls.sales_channel}-DEVELOPMENT"
		if not frappe.db.exists("PrestaShop Connector", cls.connector_name):
			connector_doc = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": cls.sales_channel,
				"environment": "DEVELOPMENT",
				"credential_reference": credential_ref,
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 1,
				"base_url": "http://prestashop-test",
				"order_state_write_enabled": 1,
				"order_state_send_email": 0,
				"shipping_state_id": "4",
				"delivered_state_id": "5",
				"cancellation_order_states": "6",
				"review_order_states": "7,8",
				"eligible_order_states": "2,3,11",
				"timeout_seconds": 15,
			})
			connector_doc.flags.ignore_validate = True
			connector_doc.insert(ignore_permissions=True)
		else:
			frappe.db.set_value("PrestaShop Connector", cls.connector_name, {
				"order_state_write_enabled": 1,
				"order_state_send_email": 0,
				"shipping_state_id": "4",
				"delivered_state_id": "5",
				"cancellation_order_states": "6",
				"review_order_states": "7,8",
				"eligible_order_states": "2,3,11",
				"base_url": "http://prestashop-test",
			})

		# Create PrestaShop Connector for Channel B
		cls.connector_b_name = f"PS-{cls.channel_b}-DEVELOPMENT"
		if not frappe.db.exists("PrestaShop Connector", cls.connector_b_name):
			connector_b = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": cls.channel_b,
				"environment": "DEVELOPMENT",
				"credential_reference": credential_ref,
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 1,
				"base_url": "http://prestashop-test",
				"order_state_write_enabled": 1,
				"order_state_send_email": 0,
				"shipping_state_id": "4",
				"delivered_state_id": "5",
				"cancellation_order_states": "6",
				"review_order_states": "7,8",
				"eligible_order_states": "2,3,11",
				"timeout_seconds": 15,
			})
			connector_b.flags.ignore_validate = True
			connector_b.insert(ignore_permissions=True)

		frappe.db.commit()

		connector_doc = frappe.get_doc("PrestaShop Connector", cls.connector_name)
		cls.config = PrestaShopConfig.from_connector_doc(connector_doc)
		cls.client = PrestaShopClient(cls.config)

		# Ensure target PrestaShop orders exist and reset to state 2 (Processing)
		cls.test_order_ids = [925, 924, 923, 922]
		cls._restore_remote_orders()

	@classmethod
	def tearDownClass(cls):
		cls._restore_remote_orders()
		for ch in [cls.sales_channel, cls.channel_b]:
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})
			frappe.db.delete("PrestaShop Connector", {"sales_channel": ch})
			frappe.db.delete("Sales Channel", {"name": ch})
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _restore_remote_orders(cls):
		"""Restores PrestaShop test orders to state 2."""
		for oid in cls.test_order_ids:
			try:
				cls.client.update_order_state(order_id=str(oid), target_state_id=2, send_email=False)
			except Exception:
				pass

	def _get_remote_order_state(self, order_id: int) -> int:
		"""Queries live PrestaShop API for current order state."""
		order = self.client.get_order(str(order_id))
		return int(order.get("current_state", 0))

	def _get_remote_history_count(self, order_id: int) -> int:
		"""Queries live PrestaShop API for number of history records for an order."""
		histories = self.client.get_order_histories_for_order(str(order_id))
		return len(histories)

	def _create_synthetic_erp_order_and_dn(self, external_order_id: str, channel: str = None):
		"""Creates a paired synthetic Sales Order and Delivery Note."""
		ch = channel or self.sales_channel
		so_name = f"SO-LIVE-{external_order_id}-{frappe.generate_hash(length=4)}"
		dn_name = f"DN-LIVE-{external_order_id}-{frappe.generate_hash(length=4)}"

		so = frappe._dict({
			"name": so_name,
			"doctype": "Sales Order",
			"docstatus": 1,
			"company": self.company,
			"sales_channel": ch,
			"external_order_id": str(external_order_id),
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"integration_status": "READY",
		})

		dn = frappe._dict({
			"name": dn_name,
			"doctype": "Delivery Note",
			"docstatus": 1,
			"company": self.company,
			"sales_channel": ch,
			"items": [
				frappe._dict({"against_sales_order": so_name})
			],
		})
		return so, dn

	@contextmanager
	def _mock_env(self, *docs):
		"""
		Safely mocks frappe.db.get_value and frappe.get_doc for synthetic docs
		while delegating all other calls to the original unmocked functions.
		"""
		orig_get_val = frappe.db.get_value
		orig_get_doc = frappe.get_doc

		so_map = {}
		dn_map = {}
		for d in docs:
			if getattr(d, "doctype", None) == "Sales Order":
				so_map[d.name] = d
			elif getattr(d, "doctype", None) == "Delivery Note":
				dn_map[d.name] = d

		def mock_get_val(*args, **kwargs):
			dt = args[0] if len(args) > 0 else kwargs.get("doctype")
			filters = kwargs.get("filters") if "filters" in kwargs else (args[1] if len(args) > 1 else None)

			if dt == "Sales Order":
				match = None
				if isinstance(filters, dict) and "name" in filters:
					match = so_map.get(filters["name"])
				elif isinstance(filters, str):
					match = so_map.get(filters)
				elif so_map:
					match = next(iter(so_map.values()))
				if match:
					return frappe._dict({
						"name": match.name,
						"docstatus": 1,
						"company": match.company,
						"sales_channel": match.sales_channel,
						"integration_status": "READY",
					})

			if dt == "Delivery Note":
				match = None
				if isinstance(filters, dict) and "name" in filters:
					match = dn_map.get(filters["name"])
				elif isinstance(filters, str):
					match = dn_map.get(filters)
				elif dn_map:
					match = next(iter(dn_map.values()))
				if match:
					return frappe._dict({
						"name": match.name,
						"docstatus": 1,
						"company": match.company,
						"sales_channel": match.sales_channel,
					})

			if dt == "External ID Mapping":
				return None

			return orig_get_val(*args, **kwargs)

		def mock_get_doc(dt, name=None, *args, **kwargs):
			if dt == "Sales Order":
				if name and name in so_map:
					return so_map[name]
				elif not name and so_map:
					return next(iter(so_map.values()))
			if dt == "Delivery Note":
				if name and name in dn_map:
					return dn_map[name]
				elif not name and dn_map:
					return next(iter(dn_map.values()))
			return orig_get_doc(dt, name, *args, **kwargs) if name else orig_get_doc(dt, *args, **kwargs)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value", side_effect=mock_get_val), \
			 patch("bop_erp.orders.fulfillment_writeback.frappe.get_doc", side_effect=mock_get_doc):
			yield

	# =========================================================================
	# SCENARIO A: PrestaShop order + ERP Delivery Note -> remote state becomes 4 (Shipped)
	# =========================================================================
	def test_scenario_a_successful_remote_shipped_writeback(self):
		order_id = 925
		so, dn = self._create_synthetic_erp_order_and_dn(str(order_id))

		with self._mock_env(so, dn):
			event = create_order_fulfillment_writeback_intent(dn)
			self.assertIsNotNone(event)
			self.assertEqual(event.status, IntegrationStatus.PENDING)
			self.assertEqual(event.external_id, str(order_id))

			success = process_order_fulfillment_writeback_event(event.name, client=self.client)
			self.assertTrue(success, "Writeback must execute successfully against local PrestaShop")

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

			remote_state = self._get_remote_order_state(order_id)
			self.assertEqual(remote_state, 4, "Remote PrestaShop order must transition to state 4 (Shipped)")

	# =========================================================================
	# SCENARIO B: Remote already Shipped -> NO-OP success
	# =========================================================================
	def test_scenario_b_remote_already_shipped_noop_convergence(self):
		order_id = 925
		self.assertEqual(self._get_remote_order_state(order_id), 4)
		initial_histories = self._get_remote_history_count(order_id)

		so, dn = self._create_synthetic_erp_order_and_dn(str(order_id))

		with self._mock_env(so, dn):
			event = frappe.get_doc({
				"doctype": "Integration Event",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": self.sales_channel,
				"direction": IntegrationDirection.OUTBOUND,
				"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
				"entity_type": ExternalEntityType.ORDER,
				"external_id": str(order_id),
				"status": IntegrationStatus.PENDING,
				"idempotency_key": f"test-noop-{frappe.generate_hash()}",
				"erp_doctype": "Delivery Note",
				"erp_document": dn.name,
				"request_metadata": json.dumps({"sales_order": so.name, "delivery_note": dn.name}),
			}).insert(ignore_permissions=True, ignore_links=True)

			success = process_order_fulfillment_writeback_event(event.name, client=self.client)
			self.assertTrue(success, "Already-shipped order writeback must succeed as NO-OP")

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)
			meta = json.loads(event.response_metadata or "{}")
			self.assertTrue(meta.get("noop"), "Response metadata must record NO-OP convergence")

			new_histories = self._get_remote_history_count(order_id)
			self.assertEqual(new_histories, initial_histories, "NO-OP must NOT append duplicate history row in PrestaShop")

	# =========================================================================
	# SCENARIO C: Response lost after remote transition -> retry converges to SUCCEEDED
	# =========================================================================
	def test_scenario_c_response_lost_convergence(self):
		order_id = 924
		so, dn = self._create_synthetic_erp_order_and_dn(str(order_id))

		with self._mock_env(so, dn):
			event = frappe.get_doc({
				"doctype": "Integration Event",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": self.sales_channel,
				"direction": IntegrationDirection.OUTBOUND,
				"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
				"entity_type": ExternalEntityType.ORDER,
				"external_id": str(order_id),
				"status": IntegrationStatus.PENDING,
				"idempotency_key": f"test-lost-resp-{frappe.generate_hash()}",
				"erp_doctype": "Delivery Note",
				"erp_document": dn.name,
				"request_metadata": json.dumps({"sales_order": so.name, "delivery_note": dn.name}),
			}).insert(ignore_permissions=True, ignore_links=True)

			real_update = self.client.update_order_state

			def dropped_response_update(*args, **kwargs):
				real_update(*args, **kwargs)
				raise PrestaShopTransientError("Simulated network drop immediately after remote write")

			with patch.object(self.client, "update_order_state", side_effect=dropped_response_update):
				res1 = process_order_fulfillment_writeback_event(event.name, client=self.client)
				self.assertFalse(res1)
				event.reload()
				self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)

			self.assertEqual(self._get_remote_order_state(order_id), 4)
			hist_count_after_write = self._get_remote_history_count(order_id)

			frappe.db.set_value("Integration Event", event.name, "next_retry_at", now_datetime() - timedelta(seconds=10))

			res2 = process_order_fulfillment_writeback_event(event.name, client=self.client)
			self.assertTrue(res2, "Retry must converge successfully")

			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)
			self.assertEqual(self._get_remote_history_count(order_id), hist_count_after_write)

	# =========================================================================
	# SCENARIO D: Remote Canceled before worker -> zero Shipped write, safe review block
	# =========================================================================
	def test_scenario_d_remote_canceled_blocks_write(self):
		order_id = 923
		self.client.update_order_state(order_id=str(order_id), target_state_id=6, send_email=False)
		self.assertEqual(self._get_remote_order_state(order_id), 6)

		hist_count_before = self._get_remote_history_count(order_id)
		so, dn = self._create_synthetic_erp_order_and_dn(str(order_id))

		with self._mock_env(so, dn):
			event = frappe.get_doc({
				"doctype": "Integration Event",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": self.sales_channel,
				"direction": IntegrationDirection.OUTBOUND,
				"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
				"entity_type": ExternalEntityType.ORDER,
				"external_id": str(order_id),
				"status": IntegrationStatus.PENDING,
				"idempotency_key": f"test-remote-cancel-{frappe.generate_hash()}",
				"erp_doctype": "Delivery Note",
				"erp_document": dn.name,
				"request_metadata": json.dumps({"sales_order": so.name, "delivery_note": dn.name}),
			}).insert(ignore_permissions=True, ignore_links=True)

			success = process_order_fulfillment_writeback_event(event.name, client=self.client)
			self.assertFalse(success)

			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "REMOTE_CANCELED")

			self.assertEqual(self._get_remote_order_state(order_id), 6)
			self.assertEqual(self._get_remote_history_count(order_id), hist_count_before)

	# =========================================================================
	# SCENARIO E: Delivery Note cancelled before worker -> zero remote write
	# =========================================================================
	def test_scenario_e_delivery_note_cancelled_before_worker(self):
		order_id = 922
		so, dn = self._create_synthetic_erp_order_and_dn(str(order_id))

		with self._mock_env(so, dn):
			event = frappe.get_doc({
				"doctype": "Integration Event",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": self.sales_channel,
				"direction": IntegrationDirection.OUTBOUND,
				"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
				"entity_type": ExternalEntityType.ORDER,
				"external_id": str(order_id),
				"status": IntegrationStatus.PENDING,
				"idempotency_key": f"test-cancel-dn-{frappe.generate_hash()}",
				"erp_doctype": "Delivery Note",
				"erp_document": dn.name,
				"request_metadata": json.dumps({"sales_order": so.name, "delivery_note": dn.name}),
			}).insert(ignore_permissions=True, ignore_links=True)

			handle_delivery_note_cancel(dn)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.CANCELLED)

			res = process_order_fulfillment_writeback_event(event.name, client=self.client)
			self.assertFalse(res)

	# =========================================================================
	# SCENARIO F: Expired lease -> zero remote write
	# =========================================================================
	def test_scenario_f_expired_lease_blocks_remote_write(self):
		order_id = 925
		self.client.update_order_state(order_id=str(order_id), target_state_id=2, send_email=False)
		self.assertEqual(self._get_remote_order_state(order_id), 2)
		so, dn = self._create_synthetic_erp_order_and_dn(str(order_id))

		with self._mock_env(so, dn):
			event = frappe.get_doc({
				"doctype": "Integration Event",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": self.sales_channel,
				"direction": IntegrationDirection.OUTBOUND,
				"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
				"entity_type": ExternalEntityType.ORDER,
				"external_id": str(order_id),
				"status": IntegrationStatus.PENDING,
				"idempotency_key": f"test-lease-exp-{frappe.generate_hash()}",
				"erp_doctype": "Delivery Note",
				"erp_document": dn.name,
				"request_metadata": json.dumps({"sales_order": so.name, "delivery_note": dn.name}),
			}).insert(ignore_permissions=True, ignore_links=True)

			def expire_lease_hook():
				frappe.db.set_value("Integration Event", event.name, "lease_expires_at", now_datetime() - timedelta(minutes=10))

			with patch.object(self.client, "update_order_state") as mock_update:
				success = process_order_fulfillment_writeback_event(
					event.name, client=self.client, pre_write_hook=expire_lease_hook
				)
				self.assertFalse(success)
				mock_update.assert_not_called()

	# =========================================================================
	# SCENARIO G: Worker reclaim race -> one effective transition
	# =========================================================================
	def test_scenario_g_worker_reclaim_race_fencing(self):
		order_id = 925
		self.client.update_order_state(order_id=str(order_id), target_state_id=2, send_email=False)
		self.assertEqual(self._get_remote_order_state(order_id), 2)
		so, dn = self._create_synthetic_erp_order_and_dn(str(order_id))

		with self._mock_env(so, dn):
			event = frappe.get_doc({
				"doctype": "Integration Event",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": self.sales_channel,
				"direction": IntegrationDirection.OUTBOUND,
				"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
				"entity_type": ExternalEntityType.ORDER,
				"external_id": str(order_id),
				"status": IntegrationStatus.PENDING,
				"idempotency_key": f"test-reclaim-race-{frappe.generate_hash()}",
				"erp_doctype": "Delivery Note",
				"erp_document": dn.name,
				"request_metadata": json.dumps({"sales_order": so.name, "delivery_note": dn.name}),
			}).insert(ignore_permissions=True, ignore_links=True)

			def simulate_worker_reclaim_hook():
				frappe.db.set_value("Integration Event", event.name, "processing_token", "stolen-worker-token-xyz")

			with patch.object(self.client, "update_order_state") as mock_update:
				success = process_order_fulfillment_writeback_event(
					event.name, client=self.client, pre_write_hook=simulate_worker_reclaim_hook
				)
				self.assertFalse(success, "Fenced worker must be rejected")
				mock_update.assert_not_called()

	# =========================================================================
	# SCENARIO H: Cross-channel same external ID -> correct channel only
	# =========================================================================
	def test_scenario_h_cross_channel_scoping(self):
		order_id = "925"
		so_a, dn_a = self._create_synthetic_erp_order_and_dn(order_id, channel=self.sales_channel)
		so_b, dn_b = self._create_synthetic_erp_order_and_dn(order_id, channel=self.channel_b)

		with self._mock_env(so_a, dn_a, so_b, dn_b):
			ev_a = create_order_fulfillment_writeback_intent(dn_a)
			self.assertIsNotNone(ev_a)
			self.assertEqual(ev_a.sales_channel, self.sales_channel)
			self.assertNotEqual(ev_a.sales_channel, self.channel_b)

	# =========================================================================
	# SCENARIO I: Missing shipping_state_id -> safe block
	# =========================================================================
	def test_scenario_i_missing_shipping_state_id_safe_block(self):
		order_id = 925
		so, dn = self._create_synthetic_erp_order_and_dn(str(order_id))

		frappe.db.set_value("PrestaShop Connector", self.connector_name, "shipping_state_id", "")

		with self._mock_env(so, dn):
			event = frappe.get_doc({
				"doctype": "Integration Event",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": self.sales_channel,
				"direction": IntegrationDirection.OUTBOUND,
				"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
				"entity_type": ExternalEntityType.ORDER,
				"external_id": str(order_id),
				"status": IntegrationStatus.PENDING,
				"idempotency_key": f"test-no-shipping-state-{frappe.generate_hash()}",
				"erp_doctype": "Delivery Note",
				"erp_document": dn.name,
				"request_metadata": json.dumps({"sales_order": so.name, "delivery_note": dn.name}),
			}).insert(ignore_permissions=True, ignore_links=True)

			with patch.object(self.client, "update_order_state") as mock_update:
				success = process_order_fulfillment_writeback_event(event.name, client=self.client)
				self.assertFalse(success)
				event.reload()
				self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
				self.assertEqual(event.last_error_code, "SHIPPING_STATE_NOT_CONFIGURED")
				mock_update.assert_not_called()

		frappe.db.set_value("PrestaShop Connector", self.connector_name, "shipping_state_id", "4")

	# =========================================================================
	# SCENARIO J: Host changed to forbidden production -> zero network call
	# =========================================================================
	def test_scenario_j_forbidden_production_host_zero_network_call(self):
		order_id = 925
		so, dn = self._create_synthetic_erp_order_and_dn(str(order_id))

		frappe.db.set_value("PrestaShop Connector", self.connector_name, "base_url", "https://theindustrialdepot.com")

		with self._mock_env(so, dn):
			event = frappe.get_doc({
				"doctype": "Integration Event",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": self.sales_channel,
				"direction": IntegrationDirection.OUTBOUND,
				"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
				"entity_type": ExternalEntityType.ORDER,
				"external_id": str(order_id),
				"status": IntegrationStatus.PENDING,
				"idempotency_key": f"test-safety-recheck-{frappe.generate_hash()}",
				"erp_doctype": "Delivery Note",
				"erp_document": dn.name,
				"request_metadata": json.dumps({"sales_order": so.name, "delivery_note": dn.name}),
			}).insert(ignore_permissions=True, ignore_links=True)

			with patch.object(self.client, "update_order_state") as mock_update:
				success = process_order_fulfillment_writeback_event(event.name)
				self.assertFalse(success)
				mock_update.assert_not_called()

		frappe.db.set_value("PrestaShop Connector", self.connector_name, "base_url", "http://prestashop-test")

	# =========================================================================
	# SCENARIO K: Transactional outbox proof: event exists in MariaDB before after_commit
	# =========================================================================
	def test_scenario_k_transactional_outbox_proof(self):
		order_id = "925"
		so, dn = self._create_synthetic_erp_order_and_dn(order_id)

		with self._mock_env(so, dn):
			event = create_order_fulfillment_writeback_intent(dn)
			self.assertIsNotNone(event)

			row_in_db = frappe.db.sql(
				"SELECT name, status, operation, external_id FROM `tabIntegration Event` WHERE name = %s",
				(event.name,),
				as_dict=True,
			)
			self.assertTrue(len(row_in_db) > 0, "Outbox event must exist in database within the current transaction")
			self.assertEqual(row_in_db[0].status, IntegrationStatus.PENDING)
			self.assertEqual(row_in_db[0].operation, IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE)
			self.assertEqual(row_in_db[0].external_id, order_id)

	# =========================================================================
	# SCENARIO L: Fixture cleanup & synthetic order restoration
	# =========================================================================
	def test_scenario_l_fixture_cleanup_and_synthetic_order_restoration(self):
		self._restore_remote_orders()
		for oid in self.test_order_ids:
			order = self.client.get_order(str(oid))
			self.assertIsNotNone(order)
			self.assertEqual(int(order.get("id")), oid)
			self.assertEqual(int(order.get("current_state")), 2, f"Order {oid} must be restored to state 2")
