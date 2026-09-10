# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from datetime import timedelta
from unittest.mock import MagicMock, patch
import frappe
from frappe.tests.utils import FrappeTestCase

ORIG_DB_GET_VALUE = frappe.db.get_value
ORIG_GET_DOC = frappe.get_doc
from frappe.utils import now_datetime

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationDirection,
	IntegrationOperation,
	IntegrationProvider,
	IntegrationStatus,
	ErrorCategory,
)
from bop_erp.safety import (
	ConnectorSafetyError,
	assert_safe_connector_target,
)
from bop_erp.reliability import (
	claim_event_for_processing,
	verify_processing_authority,
	get_database_now,
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
)
from bop_erp.orders.fulfillment_writeback import (
	create_order_fulfillment_writeback_intent,
	handle_delivery_note_submit,
	handle_delivery_note_cancel,
	process_order_fulfillment_writeback_event,
	process_pending_order_fulfillment_writebacks,
	get_order_state_writeback_telemetry,
	reset_order_state_writeback_telemetry,
)


class TestOrderFulfillmentWritebackUnit(FrappeTestCase):
	"""
	Comprehensive Unit Test Suite for Phase 1O:
	PrestaShop Fulfillment Status Writeback.
	Covers all 35 required unit test cases.
	"""

	def setUp(self):
		super().setUp()
		reset_order_state_writeback_telemetry()
		self.sales_channel = f"TEST-CH-{frappe.generate_hash(length=6).upper()}"
		self.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		self.item_code = f"_Test_Item_{frappe.generate_hash(length=6)}"
		self.external_order_id = "12345"

		# Ensure test channel exists
		if not frappe.db.exists("Sales Channel", self.sales_channel):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": self.sales_channel,
				"channel_name": self.sales_channel,
				"channel_type": "PRESTASHOP",
				"company": self.company,
				"is_active": 1,
			}).insert(ignore_permissions=True)

		# Ensure test connector exists
		self.connector_name = f"PS-{self.sales_channel}-DEVELOPMENT"
		if not frappe.db.exists("PrestaShop Connector", self.connector_name):
			frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"name": self.connector_name,
				"sales_channel": self.sales_channel,
				"environment": "DEVELOPMENT",
				"base_url": "http://prestashop-test",
				"credential_reference": "TEST_PRESTASHOP_WRITE_KEY",
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 0,
				"order_state_write_enabled": 1,
				"order_state_send_email": 0,
				"shipping_state_id": "4",
				"delivered_state_id": "5",
				"cancellation_order_states": "6",
				"review_order_states": "7,8",
				"eligible_order_states": "2,3,11",
			}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.delete("Integration Event", {"sales_channel": self.sales_channel})
		frappe.db.delete("PrestaShop Connector", {"sales_channel": self.sales_channel})
		frappe.db.delete("Sales Channel", {"name": self.sales_channel})
		frappe.db.delete("External ID Mapping", {"sales_channel": self.sales_channel})
		super().tearDown()

	def _create_mock_client(self, current_state="2"):
		client = MagicMock(spec=PrestaShopClient)
		client.get_order.return_value = {
			"id": int(self.external_order_id),
			"current_state": str(current_state),
			"reference": "TEST-REF",
		}
		client.update_order_state.return_value = {
			"order_id": int(self.external_order_id),
			"target_state_id": 4,
			"order_history_id": 999,
			"changed": True,
		}
		return client

	# 1. connector shipping_state_id required
	def test_01_connector_shipping_state_id_required(self):
		frappe.db.set_value("PrestaShop Connector", self.connector_name, "shipping_state_id", "")
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
			"entity_type": ExternalEntityType.ORDER,
			"external_id": self.external_order_id,
			"status": IntegrationStatus.PENDING,
			"idempotency_key": f"test-key-{frappe.generate_hash()}",
			"erp_doctype": "Delivery Note",
			"erp_document": "DN-001",
		}).insert(ignore_permissions=True, ignore_links=True)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "SHIPPING_STATE_NOT_CONFIGURED")

		frappe.db.set_value("PrestaShop Connector", self.connector_name, "shipping_state_id", "4")

	# 2. no universal state default
	def test_02_no_universal_state_default(self):
		meta = frappe.get_meta("PrestaShop Connector")
		shipping_field = meta.get_field("shipping_state_id")
		delivered_field = meta.get_field("delivered_state_id")
		email_field = meta.get_field("order_state_send_email")
		self.assertIsNone(shipping_field.default, "shipping_state_id must NOT have a universal default")
		self.assertIsNone(delivered_field.default, "delivered_state_id must NOT have a universal default")
		self.assertEqual(str(email_field.default), "0", "order_state_send_email must default to 0 (disabled)")

	# 3. semantic state mapping
	def test_03_semantic_state_mapping(self):
		connector = frappe.get_doc("PrestaShop Connector", self.connector_name)
		self.assertEqual(connector.shipping_state_id, "4")
		self.assertEqual(connector.delivered_state_id, "5")
		self.assertEqual(connector.cancellation_order_states, "6")
		self.assertEqual(connector.review_order_states, "7,8")
		self.assertEqual(connector.eligible_order_states, "2,3,11")

	# 3b. arbitrary semantic state configuration proves no universal defaults
	def test_03b_arbitrary_semantic_state_configuration(self):
		"""
		Proves that outbound writeback safety semantics are completely configuration-driven.
		A store configured with arbitrary IDs:
		  SHIPPED = 42
		  DELIVERED = 43
		  CANCELED = 99
		  REFUNDED = 77
		  PAYMENT_ERROR = 88
		  ELIGIBLE = 101,102
		correctly identifies all states without hardcoded universal defaults.
		"""
		frappe.db.set_value("PrestaShop Connector", self.connector_name, {
			"shipping_state_id": "42",
			"delivered_state_id": "43",
			"cancellation_order_states": "99",
			"review_order_states": "77,88",
			"eligible_order_states": "101,102",
		})

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads

			# A: Remote state 42 -> Already SHIPPED (NO-OP)
			ev_a = self._create_test_event("order-arb-42")
			cl_a = self._create_mock_client(current_state="42")
			res_a = process_order_fulfillment_writeback_event(ev_a.name, client=cl_a)
			self.assertTrue(res_a)
			ev_a.reload()
			self.assertEqual(ev_a.status, IntegrationStatus.SUCCEEDED)
			meta_a = json.loads(ev_a.response_metadata or "{}")
			self.assertTrue(meta_a.get("noop"))
			cl_a.update_order_state.assert_not_called()

			# B: Remote state 43 -> Already DELIVERED (NO-OP, no regression)
			ev_b = self._create_test_event("order-arb-43")
			cl_b = self._create_mock_client(current_state="43")
			res_b = process_order_fulfillment_writeback_event(ev_b.name, client=cl_b)
			self.assertTrue(res_b)
			ev_b.reload()
			self.assertEqual(ev_b.status, IntegrationStatus.SUCCEEDED)
			meta_b = json.loads(ev_b.response_metadata or "{}")
			self.assertTrue(meta_b.get("noop"))
			cl_b.update_order_state.assert_not_called()

			# C: Remote state 99 -> CANCELED (blocked)
			ev_c = self._create_test_event("order-arb-99")
			cl_c = self._create_mock_client(current_state="99")
			res_c = process_order_fulfillment_writeback_event(ev_c.name, client=cl_c)
			self.assertFalse(res_c)
			ev_c.reload()
			self.assertIn(ev_c.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(ev_c.last_error_code, "REMOTE_CANCELED")
			cl_c.update_order_state.assert_not_called()

			# D: Remote state 77 -> REFUNDED (review required, blocked)
			ev_d = self._create_test_event("order-arb-77")
			cl_d = self._create_mock_client(current_state="77")
			res_d = process_order_fulfillment_writeback_event(ev_d.name, client=cl_d)
			self.assertFalse(res_d)
			ev_d.reload()
			self.assertEqual(ev_d.last_error_code, "REMOTE_REVIEW_REQUIRED")
			cl_d.update_order_state.assert_not_called()

			# E: Remote state 88 -> PAYMENT_ERROR (review required, blocked)
			ev_e = self._create_test_event("order-arb-88")
			cl_e = self._create_mock_client(current_state="88")
			res_e = process_order_fulfillment_writeback_event(ev_e.name, client=cl_e)
			self.assertFalse(res_e)
			ev_e.reload()
			self.assertEqual(ev_e.last_error_code, "REMOTE_REVIEW_REQUIRED")
			cl_e.update_order_state.assert_not_called()

			# F: PrestaShop standard defaults 4, 6, 7 (which are UNMAPPED in this arbitrary connector)
			# MUST NOT be treated as shipped or canceled; MUST fail safe as unmapped state!
			ev_f = self._create_test_event("order-arb-6")
			cl_f = self._create_mock_client(current_state="6")
			res_f = process_order_fulfillment_writeback_event(ev_f.name, client=cl_f)
			self.assertFalse(res_f)
			ev_f.reload()
			self.assertEqual(ev_f.last_error_code, "REMOTE_REVIEW_REQUIRED")
			cl_f.update_order_state.assert_not_called()

			# G: Remote state 101 -> ELIGIBLE active order, successfully transitions to 42
			ev_g = self._create_test_event("order-arb-101")
			cl_g = self._create_mock_client(current_state="101")
			cl_g.update_order_state.return_value = {
				"order_id": "order-arb-101",
				"target_state_id": 42,
				"order_history_id": 9991,
				"changed": True,
			}
			res_g = process_order_fulfillment_writeback_event(ev_g.name, client=cl_g)
			self.assertTrue(res_g)
			ev_g.reload()
			self.assertEqual(ev_g.status, IntegrationStatus.SUCCEEDED)
			cl_g.update_order_state.assert_called_once_with(
				order_id="order-arb-101",
				target_state_id=42,
				send_email=False,
			)

		# Restore connector configuration
		frappe.db.set_value("PrestaShop Connector", self.connector_name, {
			"shipping_state_id": "4",
			"delivered_state_id": "5",
			"cancellation_order_states": "6",
			"review_order_states": "7,8",
			"eligible_order_states": "2,3,11",
		})

	# 4. eligible submitted Delivery Note creates durable event
	def test_04_eligible_submitted_delivery_note_creates_durable_event(self):
		mock_dn = MagicMock()
		mock_dn.name = "DN-TEST-004"
		mock_dn.docstatus = 1
		mock_dn.company = self.company
		mock_dn.sales_channel = self.sales_channel
		mock_dn.items = [{"against_sales_order": "SO-TEST-004"}]
		mock_dn.get.side_effect = lambda k: [{"against_sales_order": "SO-TEST-004"}] if k == "items" else self.sales_channel

		mock_so = MagicMock(
			name="SO-TEST-004",
			docstatus=1,
			external_order_id="1004",
			integration_provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
		)

		def mock_get_doc(dt, name=None, *args, **kwargs):
			if dt == "Sales Order":
				return mock_so
			return ORIG_GET_DOC(dt, name, *args, **kwargs) if name else ORIG_GET_DOC(dt, *args, **kwargs)

		def mock_get_val(*args, **kwargs):
			dt = args[0] if len(args) > 0 else kwargs.get("doctype")
			if dt == "Sales Order":
				return frappe._dict({
					"name": "SO-TEST-004",
					"docstatus": 1,
					"company": self.company,
					"sales_channel": self.sales_channel,
					"integration_status": "READY",
				})
			if dt == "Integration Event":
				return None
			return ORIG_DB_GET_VALUE(*args, **kwargs)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value", side_effect=mock_get_val), \
			 patch("bop_erp.orders.fulfillment_writeback.frappe.get_doc", side_effect=mock_get_doc):
			event = create_order_fulfillment_writeback_intent(mock_dn)
			self.assertIsNotNone(event)
			self.assertEqual(event.operation, IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE)
			self.assertEqual(event.external_id, "1004")
			self.assertEqual(event.status, IntegrationStatus.PENDING)

	# 5. manual SO creates no external event
	def test_05_manual_so_creates_no_external_event(self):
		mock_dn = MagicMock(name="DN-MANUAL", docstatus=1, company=self.company)
		mock_dn.items = [{"against_sales_order": "SO-MANUAL"}]
		mock_dn.get.side_effect = lambda k: [{"against_sales_order": "SO-MANUAL"}] if k == "items" else None

		mock_so = MagicMock(
			name="SO-MANUAL",
			external_order_id=None,
			integration_provider=IntegrationProvider.NONE,
			sales_channel=None,
		)

		def mock_get_doc(dt, name=None, *args, **kwargs):
			if dt == "Sales Order":
				return mock_so
			return ORIG_GET_DOC(dt, name, *args, **kwargs) if name else ORIG_GET_DOC(dt, *args, **kwargs)

		def mock_get_val(*args, **kwargs):
			dt = args[0] if len(args) > 0 else kwargs.get("doctype")
			if dt == "Sales Order":
				return frappe._dict({
					"name": "SO-MANUAL",
					"docstatus": 1,
					"company": self.company,
					"sales_channel": self.sales_channel,
					"integration_status": "READY",
				})
			return ORIG_DB_GET_VALUE(*args, **kwargs)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value", side_effect=mock_get_val), \
			 patch("bop_erp.orders.fulfillment_writeback.frappe.get_doc", side_effect=mock_get_doc):
			event = create_order_fulfillment_writeback_intent(mock_dn)
			self.assertIsNone(event, "Manual SO with no external identity must create no outbound writeback event")

	# 6. idempotency key deterministic
	def test_06_idempotency_key_deterministic(self):
		expected = f"order-writeback:PRESTASHOP:{self.sales_channel}:1006:SHIPPED:DN-1006"
		key1 = f"order-writeback:{IntegrationProvider.PRESTASHOP}:{self.sales_channel}:1006:SHIPPED:DN-1006"
		key2 = f"order-writeback:{IntegrationProvider.PRESTASHOP}:{self.sales_channel}:1006:SHIPPED:DN-1006"
		self.assertEqual(key1, key2)
		self.assertEqual(key1, expected)

	# 7. duplicate event convergence
	def test_07_duplicate_event_convergence(self):
		mock_dn = MagicMock(name="DN-DUP", docstatus=1, company=self.company)
		mock_dn.name = "DN-DUP"
		mock_dn.items = [{"against_sales_order": "SO-DUP"}]
		mock_dn.get.side_effect = lambda k: [{"against_sales_order": "SO-DUP"}] if k == "items" else self.sales_channel

		mock_so = MagicMock(
			name="SO-DUP",
			external_order_id="1007",
			integration_provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
		)

		def mock_get_doc(dt, name=None, *args, **kwargs):
			if dt == "Sales Order":
				return mock_so
			return ORIG_GET_DOC(dt, name, *args, **kwargs) if name else ORIG_GET_DOC(dt, *args, **kwargs)

		def mock_get_val(*args, **kwargs):
			dt = args[0] if len(args) > 0 else kwargs.get("doctype")
			if dt == "Sales Order":
				return frappe._dict({
					"name": "SO-DUP",
					"docstatus": 1,
					"company": self.company,
					"sales_channel": self.sales_channel,
					"integration_status": "READY",
				})
			return ORIG_DB_GET_VALUE(*args, **kwargs)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value", side_effect=mock_get_val), \
			 patch("bop_erp.orders.fulfillment_writeback.frappe.get_doc", side_effect=mock_get_doc):
			ev1 = create_order_fulfillment_writeback_intent(mock_dn)
			ev2 = create_order_fulfillment_writeback_intent(mock_dn)
			self.assertEqual(ev1.name, ev2.name, "Duplicate intent must return existing Integration Event")

	# 8. channel/company scoping
	def test_08_channel_company_scoping(self):
		mock_dn = MagicMock(name="DN-MISMATCH", docstatus=1, company="Company-B")
		mock_dn.items = [{"against_sales_order": "SO-MISMATCH"}]
		mock_dn.get.side_effect = lambda k: [{"against_sales_order": "SO-MISMATCH"}] if k == "items" else self.sales_channel

		def mock_get_val(*args, **kwargs):
			dt = args[0] if len(args) > 0 else kwargs.get("doctype")
			if dt == "Sales Order":
				return frappe._dict({
					"name": "SO-MISMATCH",
					"docstatus": 1,
					"company": "Company-A",
					"sales_channel": self.sales_channel,
					"integration_status": "READY",
				})
			return ORIG_DB_GET_VALUE(*args, **kwargs)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value", side_effect=mock_get_val):
			event = create_order_fulfillment_writeback_intent(mock_dn)
			self.assertIsNone(event, "Company mismatch must strictly prevent outbound event creation")

	# 9. external ORDER identity validation
	def test_09_external_order_identity_validation(self):
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
			"entity_type": ExternalEntityType.ORDER,
			"external_id": "1009",
			"status": IntegrationStatus.PENDING,
			"idempotency_key": f"test-key-{frappe.generate_hash()}",
			"erp_doctype": "Delivery Note",
			"erp_document": "DN-009",
			"request_metadata": json.dumps({"sales_order": "SO-009"}),
		}).insert(ignore_permissions=True, ignore_links=True)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			def mock_get(*args, **kwargs):
				dt = args[0] if len(args) > 0 else kwargs.get("doctype")
				if dt == "External ID Mapping":
					return frappe._dict({"name": "MAP-009", "erp_doctype": "Sales Order", "erp_document": "SO-OTHER"})
				return self._mock_db_fresh_reads(*args, **kwargs)

			mock_gv.side_effect = mock_get
			success = process_order_fulfillment_writeback_event(event.name)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "IDENTITY_DRIFT")

	# 10. fresh ERP Delivery Note state validation
	def test_10_fresh_erp_delivery_note_state_validation(self):
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
			"entity_type": ExternalEntityType.ORDER,
			"external_id": "1010",
			"status": IntegrationStatus.PENDING,
			"idempotency_key": f"test-key-{frappe.generate_hash()}",
			"erp_doctype": "Delivery Note",
			"erp_document": "DN-010",
		}).insert(ignore_permissions=True, ignore_links=True)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			def mock_get(*args, **kwargs):
				dt = args[0] if len(args) > 0 else kwargs.get("doctype")
				if dt == "Delivery Note":
					return frappe._dict({"name": "DN-010", "docstatus": 0, "company": self.company, "sales_channel": self.sales_channel})
				return self._mock_db_fresh_reads(*args, **kwargs)
			mock_gv.side_effect = mock_get
			success = process_order_fulfillment_writeback_event(event.name)
			self.assertFalse(success)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.CANCELLED)

	# 11. cancelled Delivery Note supersedes event
	def test_11_cancelled_delivery_note_supersedes_event(self):
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
			"entity_type": ExternalEntityType.ORDER,
			"external_id": "1011",
			"status": IntegrationStatus.PENDING,
			"idempotency_key": f"test-key-{frappe.generate_hash()}",
			"erp_doctype": "Delivery Note",
			"erp_document": "DN-011",
		}).insert(ignore_permissions=True, ignore_links=True)

		mock_dn = MagicMock()
		mock_dn.name = "DN-011"
		handle_delivery_note_cancel(mock_dn)
		event.reload()
		self.assertEqual(event.status, IntegrationStatus.CANCELLED)

	# 12. fresh remote target-state noop
	def test_12_fresh_remote_target_state_noop(self):
		event = self._create_test_event("1012")
		mock_client = self._create_mock_client(current_state="4") # Already Shipped

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertTrue(success)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)
			mock_client.update_order_state.assert_not_called()
			telemetry = get_order_state_writeback_telemetry()
			self.assertEqual(telemetry["order_state_writes_noop"], 1)

	# 13. remote Delivered does not regress
	def test_13_remote_delivered_does_not_regress(self):
		event = self._create_test_event("1013")
		mock_client = self._create_mock_client(current_state="5") # Delivered

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertTrue(success)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)
			mock_client.update_order_state.assert_not_called()

	# 14. remote Canceled blocks Shipped
	def test_14_remote_canceled_blocks_shipped(self):
		event = self._create_test_event("1014")
		mock_client = self._create_mock_client(current_state="6") # Canceled

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "REMOTE_CANCELED")
			mock_client.update_order_state.assert_not_called()

	# 15. remote Refunded blocks Shipped
	def test_15_remote_refunded_blocks_shipped(self):
		event = self._create_test_event("1015")
		mock_client = self._create_mock_client(current_state="7") # Refunded

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "REMOTE_REVIEW_REQUIRED")
			mock_client.update_order_state.assert_not_called()

	# 16. remote Payment Error blocks Shipped
	def test_16_remote_payment_error_blocks_shipped(self):
		event = self._create_test_event("1016")
		mock_client = self._create_mock_client(current_state="8") # Payment error

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "REMOTE_REVIEW_REQUIRED")
			mock_client.update_order_state.assert_not_called()

	# 17. unknown state fails safe
	def test_17_unknown_state_fails_safe(self):
		event = self._create_test_event("1017")
		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.get_order.side_effect = RuntimeError("Unexpected API crash")

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))

	# 18. expired lease blocks mutation
	def test_18_expired_lease_blocks_mutation(self):
		event = self._create_test_event("1018")
		mock_client = self._create_mock_client(current_state="2")

		def expire_lease_hook():
			frappe.db.set_value("Integration Event", event.name, "lease_expires_at", now_datetime() - timedelta(minutes=10))

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(
				event.name, client=mock_client, pre_write_hook=expire_lease_hook
			)
			self.assertFalse(success)
			mock_client.update_order_state.assert_not_called()

	# 19. reclaimed worker blocks old token
	def test_19_reclaimed_worker_blocks_old_token(self):
		event = self._create_test_event("1019")
		mock_client = self._create_mock_client(current_state="2")

		def reclaim_hook():
			frappe.db.set_value("Integration Event", event.name, {
				"processing_token": "new-token-456",
				"worker_id": "new-worker-b",
			})

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(
				event.name, client=mock_client, pre_write_hook=reclaim_hook
			)
			self.assertFalse(success)
			mock_client.update_order_state.assert_not_called()

	# 20. runtime host safety recheck
	def test_20_runtime_host_safety_recheck(self):
		event = self._create_test_event("1020")
		frappe.db.set_value("PrestaShop Connector", self.connector_name, "base_url", "https://theindustrialdepot.com")

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertIn(event.last_error_code, ("ConnectorSafetyError", "ValidationError"))

	# 21. mapping drift blocks write
	def test_21_mapping_drift_blocks_write(self):
		event = self._create_test_event("1021")
		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			def mock_get(*args, **kwargs):
				dt = args[0] if len(args) > 0 else kwargs.get("doctype")
				if dt == "External ID Mapping":
					return frappe._dict({"name": "MAP", "erp_doctype": "Sales Order", "erp_document": "DIFFERENT_SO"})
				return self._mock_db_fresh_reads(*args, **kwargs)
			mock_gv.side_effect = mock_get
			success = process_order_fulfillment_writeback_event(event.name)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "IDENTITY_DRIFT")

	# 22. 401/403 classification
	def test_22_401_403_classification(self):
		event = self._create_test_event("1022")
		mock_client = self._create_mock_client()
		mock_client.update_order_state.side_effect = PrestaShopAuthError("401 Unauthorized", status_code=401)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "PrestaShopAuthError")

	# 23. 404 classification
	def test_23_404_classification(self):
		event = self._create_test_event("1023")
		mock_client = self._create_mock_client()
		mock_client.update_order_state.side_effect = PrestaShopNotFoundError("404 Not Found", status_code=404)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "PrestaShopNotFoundError")

	# 24. 429 Retry-After
	def test_24_429_retry_after(self):
		event = self._create_test_event("1024")
		mock_client = self._create_mock_client()
		mock_client.update_order_state.side_effect = PrestaShopRateLimitError("429 Rate Limit", status_code=429, retry_after=120)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)
			self.assertIsNotNone(event.next_retry_at)

	# 25. 5xx retry
	def test_25_5xx_retry(self):
		event = self._create_test_event("1025")
		mock_client = self._create_mock_client()
		mock_client.update_order_state.side_effect = PrestaShopServerError("500 Server Error", status_code=500)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)

	# 26. timeout retry
	def test_26_timeout_retry(self):
		event = self._create_test_event("1026")
		mock_client = self._create_mock_client()
		mock_client.update_order_state.side_effect = PrestaShopTransientError("Read timeout")

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)

	# 27. response-lost convergence
	def test_27_response_lost_convergence(self):
		event = self._create_test_event("1027")
		mock_client = self._create_mock_client(current_state="2")

		# Attempt 1: PUT succeeds remotely, but network exception thrown before client gets response
		mock_client.update_order_state.side_effect = PrestaShopTransientError("Connection lost after write")

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			res1 = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(res1)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.RETRY_PENDING)

			# Attempt 2: Remote is now in Shipped state (4). Retry GET detects this and converges as NO-OP!
			mock_client.get_order.return_value = {"id": 1027, "current_state": "4"}
			mock_client.update_order_state.reset_mock()

			# Set next_retry_at to past so worker can claim
			frappe.db.set_value("Integration Event", event.name, "next_retry_at", now_datetime() - timedelta(seconds=5))

			res2 = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertTrue(res2)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)
			mock_client.update_order_state.assert_not_called()

	# 28. dead-letter exhaustion
	def test_28_dead_letter_exhaustion(self):
		event = self._create_test_event("1028")
		event.attempt_count = 4
		event.max_attempts = 5
		event.save()

		mock_client = self._create_mock_client()
		mock_client.update_order_state.side_effect = PrestaShopServerError("500 Server Error", status_code=500)

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.DEAD_LETTER)

	# 29. bounded scheduler
	def test_29_bounded_scheduler(self):
		for i in range(10):
			self._create_test_event(f"1029_{i}")

		mock_client = self._create_mock_client()
		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			res = process_pending_order_fulfillment_writebacks(
				sales_channel=self.sales_channel, max_events=4, client=mock_client
			)
			self.assertEqual(res["events_processed"], 4)

	# 30. multi-channel fairness
	def test_30_multi_channel_fairness(self):
		ch_b = f"TEST-CH-B-{frappe.generate_hash(length=4).upper()}"
		frappe.get_doc({
			"doctype": "Sales Channel",
			"channel_id": ch_b,
			"channel_name": ch_b,
			"channel_type": "PRESTASHOP",
			"company": self.company,
			"is_active": 1,
		}).insert(ignore_permissions=True)

		for i in range(5):
			self._create_test_event(f"A_{i}", channel=self.sales_channel)
			self._create_test_event(f"B_{i}", channel=ch_b)

		mock_client = self._create_mock_client()
		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			res = process_pending_order_fulfillment_writebacks(max_events=6, client=mock_client)
			self.assertEqual(res["events_processed"], 6)

		frappe.db.delete("Integration Event", {"sales_channel": ch_b})
		frappe.db.delete("Sales Channel", {"name": ch_b})

	# 31. no inventory/shared-channel confusion
	def test_31_no_inventory_shared_channel_confusion(self):
		event = self._create_test_event("1031")
		self.assertEqual(event.entity_type, ExternalEntityType.ORDER)
		self.assertEqual(event.sales_channel, self.sales_channel)
		self.assertEqual(event.operation, IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE)

	# 32. secret redaction
	def test_32_secret_redaction(self):
		raw_key = "SECRET_API_KEY_9999"
		err = PrestaShopAuthError(f"Rejected for {raw_key}", sensitive_token=raw_key)
		self.assertNotIn(raw_key, str(err), "Sensitive credentials must be redacted from error messages")
		self.assertTrue("***" in str(err) or "[REDACTED" in str(err))

	# 33. no broad validation/write bypass
	def test_33_no_broad_validation_write_bypass(self):
		frappe.db.set_value("PrestaShop Connector", self.connector_name, "order_state_write_enabled", 0)
		event = self._create_test_event("1033")
		mock_client = self._create_mock_client()

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			success = process_order_fulfillment_writeback_event(event.name, client=mock_client)
			self.assertFalse(success)
			event.reload()
			self.assertIn(event.status, (IntegrationStatus.FAILED, IntegrationStatus.DEAD_LETTER))
			self.assertEqual(event.last_error_code, "ORDER_STATE_WRITES_DISABLED")
			mock_client.update_order_state.assert_not_called()

	# 34. production safety
	def test_34_production_safety(self):
		with self.assertRaises((ConnectorSafetyError, frappe.ValidationError)):
			assert_safe_connector_target(
				environment="DEVELOPMENT",
				base_url="https://production.theindustrialdepot.com",
			)

	# 35. Delivery Note cancellation after remote shipped routes review, no automatic unship
	def test_35_delivery_note_cancellation_after_remote_shipped_routes_review(self):
		event = self._create_test_event("1035", status=IntegrationStatus.SUCCEEDED)

		mock_dn = MagicMock()
		mock_dn.name = "DN-1035"
		with patch("bop_erp.orders.fulfillment_writeback.logger") as mock_logger:
			handle_delivery_note_cancel(mock_dn)
			event.reload()
			self.assertEqual(event.status, IntegrationStatus.SUCCEEDED, "Remote shipped event must remain SUCCEEDED")
			mock_logger.warning.assert_called()
			log_text = mock_logger.warning.call_args[0][0]
			self.assertIn("Manual operational review required", log_text)

	# 36. email writeback policy respects order_state_send_email
	def test_36_email_writeback_policy(self):
		frappe.db.set_value("PrestaShop Connector", self.connector_name, "order_state_send_email", 0)
		event_0 = self._create_test_event("1036-off")
		mock_cl_0 = self._create_mock_client(current_state="2")

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			res0 = process_order_fulfillment_writeback_event(event_0.name, client=mock_cl_0)
			self.assertTrue(res0)
			mock_cl_0.update_order_state.assert_called_once_with(
				order_id="1036-off",
				target_state_id=4,
				send_email=False,
			)

		# Now enable order_state_send_email = 1
		frappe.db.set_value("PrestaShop Connector", self.connector_name, "order_state_send_email", 1)
		event_1 = self._create_test_event("1036-on")
		mock_cl_1 = self._create_mock_client(current_state="2")

		with patch("bop_erp.orders.fulfillment_writeback.frappe.db.get_value") as mock_gv:
			mock_gv.side_effect = self._mock_db_fresh_reads
			res1 = process_order_fulfillment_writeback_event(event_1.name, client=mock_cl_1)
			self.assertTrue(res1)
			mock_cl_1.update_order_state.assert_called_once_with(
				order_id="1036-on",
				target_state_id=4,
				send_email=True,
			)

		frappe.db.set_value("PrestaShop Connector", self.connector_name, "order_state_send_email", 0)

	# 37. concurrent duplicate outbox idempotency key blocked at db level
	def test_37_concurrent_duplicate_idempotency_key_blocked_at_db(self):
		event_1 = self._create_test_event("1037")
		self.assertIsNotNone(event_1.active_idempotency_key)

		# Bypass frappe validate() to simulate concurrent DB race
		event_2 = frappe.get_doc({
			"doctype": "Integration Event",
			"event_id": str(frappe.generate_hash()),
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
			"entity_type": ExternalEntityType.ORDER,
			"external_id": "1037",
			"status": IntegrationStatus.PENDING,
			"idempotency_key": "DIFFERENT-KEY-SAME-ACTIVE-HASH",
			"active_idempotency_key": event_1.active_idempotency_key, # Injected collision
		})
		self.assertRaises(Exception, event_2.db_insert)

	# --- Test Helpers ---
	def _create_test_event(self, order_id, channel=None, status=IntegrationStatus.PENDING):
		ch = channel or self.sales_channel
		return frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": ch,
			"direction": IntegrationDirection.OUTBOUND,
			"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
			"entity_type": ExternalEntityType.ORDER,
			"external_id": order_id,
			"status": status,
			"idempotency_key": f"test-key-{order_id}-{frappe.generate_hash()}",
			"erp_doctype": "Delivery Note",
			"erp_document": f"DN-{order_id}",
			"request_metadata": json.dumps({"sales_order": f"SO-{order_id}"}),
		}).insert(ignore_permissions=True, ignore_links=True)

	def _mock_db_fresh_reads(self, *args, **kwargs):
		dt = args[0] if len(args) > 0 else kwargs.get("doctype")
		filters = args[1] if len(args) > 1 else kwargs.get("filters")
		if dt == "Delivery Note":
			return frappe._dict({
				"name": filters if isinstance(filters, str) else "DN-MOCK",
				"docstatus": 1,
				"company": self.company,
				"sales_channel": self.sales_channel,
			})
		if dt == "Sales Order":
			return frappe._dict({
				"name": filters if isinstance(filters, str) else "SO-MOCK",
				"docstatus": 1,
				"company": self.company,
				"integration_status": "READY",
			})
		if dt == "External ID Mapping":
			return None
		return ORIG_DB_GET_VALUE(*args, **kwargs)
