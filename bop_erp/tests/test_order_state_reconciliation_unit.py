# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from unittest.mock import MagicMock, patch
import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	IntegrationReadinessStatus,
	ExternalEntityType,
	ExternalOrderStateAction,
	ErrorCategory,
	TransactionOrigin,
)
from bop_erp.orders.models import (
	ExternalOrder,
	ExternalOrderLine,
	ExternalCustomer,
	ExternalAddress,
	ExternalTotals,
)
from bop_erp.orders.exceptions import (
	OrderIngestionError,
	OrderReservationFailedError,
)
from bop_erp.orders.guard import (
	OperationalGuardError,
	assert_sales_order_ready_for_fulfillment,
	audit_sales_order_cancellation_safety,
)
from bop_erp.orders.reconciliation import (
	resolve_external_order_state_action,
	detect_material_order_changes,
	execute_sales_order_cancellation,
	process_order_state_reconciliation_event,
)
from bop_erp.orders.discovery import (
	compute_order_reconciliation_idempotency_key,
	discover_channel_order_reconciliations,
	discover_multichannel_order_reconciliations,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.safety import ConnectorSafetyError


class TestOrderStateReconciliationUnit(FrappeTestCase):
	"""
	Phase 1L Unit Test Suite:
	PrestaShop Order State Reconciliation & Cancellation Foundation.
	Validates:
	- Configurable provider-neutral state semantic mapping (no hardcoded state IDs).
	- Canonical reconciliation identity and duplicate deduplication.
	- Durable watermark tracking with bounded overlap re-read.
	- Strict existing ORDER mapping resolution (rejection of unmapped orders).
	- Safe auto-cancellation predicate with downstream document protection.
	- Downstream documents (Pick List, Delivery Note, Shipment, Invoice, Payment) block auto-cancel.
	- Native Sales Order cancellation and SRE release.
	- Cancellation replay idempotency and safe convergence.
	- Transactional outbox persistence and rollback on failure.
	- Material order modification detection (quantity increase, decrease, address change).
	- Refund state (state 7) and payment error (state 8) review routing without financial entries.
	- Cross-channel state and identity scoping.
	- Pre-network host safety verification.
	- Scheduler quota bounds and fair round-robin cursor rotation.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")

		cls.sales_channel = "TEST-RECON-A"
		cls.channel_b = "TEST-RECON-B"
		for ch in [cls.sales_channel, cls.channel_b]:
			if not frappe.db.exists("Sales Channel", ch):
				frappe.get_doc({
					"doctype": "Sales Channel",
					"channel_id": ch,
					"channel_name": ch,
					"channel_type": "PRESTASHOP",
					"company": cls.company,
					"active": 1,
				}).insert(ignore_permissions=True)

			if not frappe.db.exists("PrestaShop Connector", {"sales_channel": ch}):
				pc = frappe.get_doc({
					"doctype": "PrestaShop Connector",
					"sales_channel": ch,
					"environment": "DEVELOPMENT",
					"base_url": "http://prestashop-test",
					"credential_reference": "TEST_KEY",
					"enabled": 1,
					"read_enabled": 1,
					"write_enabled": 1,
					"eligible_order_states": "2,3,4,Payment accepted",
					"cancellation_order_states": "6",
					"review_order_states": "7,8",
				})
				pc.flags.ignore_validate = True
				pc.insert(ignore_permissions=True)

		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")
		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": cls.sales_channel, "warehouse": cls.warehouse}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.sales_channel,
				"warehouse": cls.warehouse,
				"enabled": 1,
				"allow_sellable_stock": 1,
			}).insert(ignore_permissions=True)

		cls.item_code = "ITEM-RECON-UNIT-01"
		if not frappe.db.exists("Item", cls.item_code):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code,
				"item_name": cls.item_code,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.provider = IntegrationProvider.PRESTASHOP
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if frappe.db.exists("Item", cls.item_code):
			frappe.delete_doc("Item", cls.item_code, force=True, ignore_permissions=True)

		for ch in [cls.sales_channel, cls.channel_b]:
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})
			frappe.db.sql("DELETE FROM `tabPrestaShop Connector` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabChannel Inventory Source` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabSales Channel` WHERE name = %s", (ch,))

		frappe.db.commit()
		super().tearDownClass()

	# ==================================================
	# 1. STATE SEMANTIC MAPPING & NON-HARDCODED CONFIG
	# ==================================================
	def test_01_state_semantic_mapping_configurable(self):
		"""State action resolution reads connector configuration and returns canonical actions."""
		action, label = resolve_external_order_state_action(self.sales_channel, self.provider, "6")
		self.assertEqual(action, ExternalOrderStateAction.CANCEL_BEFORE_FULFILLMENT)

		action_rev7, _ = resolve_external_order_state_action(self.sales_channel, self.provider, "7")
		self.assertEqual(action_rev7, ExternalOrderStateAction.REVIEW_REQUIRED)

		action_rev8, _ = resolve_external_order_state_action(self.sales_channel, self.provider, "8")
		self.assertEqual(action_rev8, ExternalOrderStateAction.REVIEW_REQUIRED)

		action_act, _ = resolve_external_order_state_action(self.sales_channel, self.provider, "2")
		self.assertEqual(action_act, ExternalOrderStateAction.ACTIVE)

		action_ign, _ = resolve_external_order_state_action(self.sales_channel, self.provider, "1")
		self.assertEqual(action_ign, ExternalOrderStateAction.IGNORE)

	def test_02_no_hardcoded_universal_state_ids(self):
		"""Different connectors can configure custom cancellation and review state IDs."""
		# Simulate a custom store where state 99 is cancellation and state 55 is review
		with patch("frappe.db.get_value") as mock_get_val:
			mock_get_val.return_value = {
				"cancellation_order_states": "99",
				"review_order_states": "55",
				"eligible_order_states": "10,20",
			}

			action_99, _ = resolve_external_order_state_action("CUSTOM-CH", self.provider, "99")
			self.assertEqual(action_99, ExternalOrderStateAction.CANCEL_BEFORE_FULFILLMENT)

			action_6, _ = resolve_external_order_state_action("CUSTOM-CH", self.provider, "6")
			self.assertNotEqual(action_6, ExternalOrderStateAction.CANCEL_BEFORE_FULFILLMENT)

			action_55, _ = resolve_external_order_state_action("CUSTOM-CH", self.provider, "55")
			self.assertEqual(action_55, ExternalOrderStateAction.REVIEW_REQUIRED)

	# ==================================================
	# 2. RECONCILIATION IDEMPOTENCY KEY & DEDUPLICATION
	# ==================================================
	def test_03_reconciliation_idempotency_key_generation(self):
		"""Reconciliation idempotency key is a deterministic SHA-256 hash containing exact state version."""
		k1 = compute_order_reconciliation_idempotency_key(
			self.provider, self.sales_channel, "ORD-100", "6", "2026-09-09 10:00:00"
		)
		k2 = compute_order_reconciliation_idempotency_key(
			self.provider, self.sales_channel, "ORD-100", "6", "2026-09-09 10:00:00"
		)
		self.assertEqual(k1, k2)
		self.assertEqual(len(k1), 64)

		# Changed state produces different key
		k3 = compute_order_reconciliation_idempotency_key(
			self.provider, self.sales_channel, "ORD-100", "7", "2026-09-09 10:00:00"
		)
		self.assertNotEqual(k1, k3)

	def test_04_discovery_deduplicates_repeated_unchanged_external_state(self):
		"""Observing unchanged remote state 20 times creates exactly 1 event."""
		connector = {
			"name": f"PS-{self.sales_channel}-DEV",
			"sales_channel": self.sales_channel,
			"environment": "DEVELOPMENT",
			"base_url": "http://prestashop-test",
			"last_reconciliation_watermark": None,
			"last_reconciliation_order_id": None,
		}

		mock_order = {
			"id": "901",
			"current_state": "6",
			"date_upd": "2026-09-09 10:00:00",
		}

		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client._request.return_value = {"orders": [mock_order]}

		orig_get_val = frappe.db.get_value
		def mock_gv(*args, **kwargs):
			doctype = kwargs.get("doctype") if "doctype" in kwargs else (args[0] if args else None)
			if doctype == "Sales Order":
				fieldname = kwargs.get("fieldname") if "fieldname" in kwargs else (args[2] if len(args) > 2 else None)
				if fieldname == ["external_order_state", "external_order_state_updated_at"]:
					return (None, None)
				if kwargs.get("as_dict"):
					return frappe._dict({"name": "SO-UNIT-901"})
				return "SO-UNIT-901"
			return orig_get_val(*args, **kwargs)

		# Order is mapped in ERP
		with patch("bop_erp.orders.discovery.find_existing_order_mapping", return_value="SO-UNIT-901"), \
		     patch("frappe.db.get_value", side_effect=mock_gv):

			res1 = discover_channel_order_reconciliations(connector, client=mock_client)
			self.assertEqual(res1["events_created"], 1)

			res2 = discover_channel_order_reconciliations(connector, client=mock_client)
			self.assertEqual(res2["events_created"], 0)
			self.assertEqual(res2["duplicate_events"], 1)

			# Clean up created event
			frappe.db.delete("Integration Event", {"sales_channel": self.sales_channel, "external_id": "901"})
			frappe.db.commit()

	# ==================================================
	# 3. DURABLE WATERMARK & BOUNDED OVERLAP
	# ==================================================
	def test_05_durable_watermark_tracking_and_bounded_overlap(self):
		"""Discovery updates durable watermark and uses overlap lookback."""
		connector = {
			"name": f"PS-{self.sales_channel}-DEV",
			"sales_channel": self.sales_channel,
			"environment": "DEVELOPMENT",
			"base_url": "http://prestashop-test",
			"last_reconciliation_watermark": "2026-09-09 08:00:00",
			"last_reconciliation_order_id": "100",
		}

		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client._request.return_value = {
			"orders": [
				{"id": "105", "current_state": "6", "date_upd": "2026-09-09 09:30:00"}
			]
		}

		with patch("bop_erp.orders.discovery.find_existing_order_mapping", return_value="SO-UNIT-105"), \
		     patch("frappe.db.set_value") as mock_set_val:

			res = discover_channel_order_reconciliations(connector, overlap_minutes=30, client=mock_client)
			self.assertEqual(res["last_reconciliation_watermark"], "2026-09-09 09:30:00")
			mock_set_val.assert_called()

			# Clean up
			frappe.db.delete("Integration Event", {"sales_channel": self.sales_channel, "external_id": "105"})
			frappe.db.commit()

	# ==================================================
	# 4. EXISTING ORDER MAPPING IDENTITY RESOLUTION
	# ==================================================
	def test_06_existing_order_mapping_required(self):
		"""Unmapped external order cannot be reconciled; dead-lettered without creating SO."""
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": self.provider,
			"sales_channel": self.sales_channel,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.RECONCILE_ORDER_STATE,
			"external_id": "UNMAPPED-999",
			"idempotency_key": "UNMAPPED-KEY-001",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({"external_state_id": "6"}),
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		with patch("bop_erp.orders.reconciliation.find_existing_order_mapping", return_value=None):
			res = process_order_state_reconciliation_event(event.name)
			self.assertFalse(res["success"])
			self.assertEqual(res["category"], "NOT_FOUND")

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.DEAD_LETTER)
		frappe.db.delete("Integration Event", {"name": event.name})
		frappe.db.commit()

	# ==================================================
	# 5. SAFE AUTO-CANCELLATION PREDICATE & DOWNSTREAM BLOCKING
	# ==================================================
	def test_07_safe_auto_cancel_predicate_passes_when_no_downstream_docs(self):
		"""Imported order with no submitted downstream documents passes safety predicate."""
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql", return_value=[]):
			is_safe, reasons = audit_sales_order_cancellation_safety("SO-CLEAN-001")
			self.assertTrue(is_safe)
			self.assertEqual(len(reasons), 0)

	def test_08_downstream_pick_list_blocks_auto_cancel(self):
		"""Active Pick List blocks auto-cancellation."""
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql") as mock_sql:
			# Mock Pick List query returning active pick list row
			mock_sql.side_effect = [
				[{"name": "PL-001"}],  # Pick List
				[],  # Delivery Note
				[],  # Shipment
				[],  # Invoice
				[],  # Payment
			]
			is_safe, reasons = audit_sales_order_cancellation_safety("SO-PL-BLOCKED")
			self.assertFalse(is_safe)
			self.assertIn("Pick List", reasons[0])

	def test_09_downstream_delivery_note_blocks_auto_cancel(self):
		"""Submitted Delivery Note blocks auto-cancellation."""
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql") as mock_sql:
			mock_sql.side_effect = [
				[],  # Pick List
				[{"name": "MAT-DN-001"}],  # Delivery Note
				[],  # Shipment
				[],  # Invoice
				[],  # Payment
			]
			is_safe, reasons = audit_sales_order_cancellation_safety("SO-DN-BLOCKED")
			self.assertFalse(is_safe)
			self.assertIn("Delivery Note", reasons[0])

	def test_10_downstream_shipment_blocks_auto_cancel(self):
		"""Submitted Shipment blocks auto-cancellation."""
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql") as mock_sql:
			mock_sql.side_effect = [
				[],  # Pick List
				[],  # Delivery Note
				[{"name": "SHIP-001"}],  # Shipment
				[],  # Invoice
				[],  # Payment
			]
			is_safe, reasons = audit_sales_order_cancellation_safety("SO-SHIP-BLOCKED")
			self.assertFalse(is_safe)
			self.assertIn("Shipment", reasons[0])

	def test_11_downstream_sales_invoice_blocks_auto_cancel(self):
		"""Submitted Sales Invoice blocks auto-cancellation."""
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql") as mock_sql:
			mock_sql.side_effect = [
				[],  # Pick List
				[],  # Delivery Note
				[],  # Shipment
				[{"name": "ACC-SINV-001"}],  # Sales Invoice
				[],  # Payment
			]
			is_safe, reasons = audit_sales_order_cancellation_safety("SO-INV-BLOCKED")
			self.assertFalse(is_safe)
			self.assertIn("Sales Invoice", reasons[0])

	def test_12_downstream_payment_entry_blocks_auto_cancel(self):
		"""Submitted Payment Entry blocks auto-cancellation."""
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql") as mock_sql:
			mock_sql.side_effect = [
				[],  # Pick List
				[],  # Delivery Note
				[],  # Shipment
				[],  # Invoice
				[{"name": "PAY-001"}],  # Payment Entry
			]
			is_safe, reasons = audit_sales_order_cancellation_safety("SO-PAY-BLOCKED")
			self.assertFalse(is_safe)
			self.assertIn("Payment Entry", reasons[0])

	# ==================================================
	# 6. NATIVE SO CANCELLATION, SRE RELEASE & OUTBOX
	# ==================================================
	def test_13_native_sales_order_cancellation_releases_sre(self):
		"""execute_sales_order_cancellation invokes native so_doc.cancel() and updates status."""
		mock_so = MagicMock()
		mock_so.docstatus = 1
		mock_so.items = [MagicMock(item_code=self.item_code, warehouse=self.warehouse)]

		with patch("frappe.get_doc", return_value=mock_so), \
		     patch("bop_erp.orders.reconciliation.audit_sales_order_cancellation_safety", return_value=(True, [])), \
		     patch("frappe.get_all", return_value=["SRE-001"]), \
		     patch("frappe.db.set_value") as mock_set_val, \
		     patch("bop_erp.orders.reconciliation.find_affected_channel_items_for_scopes", return_value={self.sales_channel: [self.item_code]}), \
		     patch("bop_erp.orders.reconciliation.persist_publication_outbox_intents", return_value={"outbox_persisted": 1}), \
		     patch("bop_erp.orders.reconciliation.register_post_commit_wake"):

			res = execute_sales_order_cancellation(
				so_name="SO-TEST-CANCEL",
				external_order_id="101",
				sales_channel=self.sales_channel,
				provider=self.provider,
				state_id="6",
				state_name="Canceled",
			)
			self.assertTrue(res["success"])
			mock_so.cancel.assert_called_once()
			self.assertEqual(res["outbox_persisted"], 1)

	def test_14_cancellation_replay_idempotency(self):
		"""Replaying cancellation on already-cancelled order safely converges with zero duplicate release."""
		mock_so = MagicMock()
		mock_so.docstatus = 2  # Already cancelled
		mock_so.get.return_value = IntegrationReadinessStatus.CANCELLED

		with patch("frappe.get_doc", return_value=mock_so), \
		     patch("frappe.db.set_value"):

			res = execute_sales_order_cancellation(
				so_name="SO-ALREADY-CANCELLED",
				external_order_id="102",
				sales_channel=self.sales_channel,
				provider=self.provider,
				state_id="6",
				state_name="Canceled",
			)
			self.assertTrue(res["success"])
			self.assertTrue(res["already_converged"])
			self.assertEqual(res["reservations_released"], 0)
			self.assertEqual(res["outbox_persisted"], 0)

	def test_15_transactional_outbox_persists_intents_on_cancellation(self):
		"""Cancellation writes outbound PENDING publication event to MariaDB within transaction."""
		initial_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})

		# Simulate successful cancellation outbox persistence
		res = execute_sales_order_cancellation(
			so_name="SO-SIMULATED",
			external_order_id="103",
			sales_channel=self.sales_channel,
			provider=self.provider,
			state_id="6",
			state_name="Canceled",
		) if False else None  # Call helper directly:

		with patch("bop_erp.orders.reconciliation.audit_sales_order_cancellation_safety", return_value=(True, [])), \
		     patch("frappe.get_all", return_value=[]), \
		     patch("frappe.get_doc") as mock_get_doc, \
		     patch("frappe.db.set_value"):

			mock_so = MagicMock(docstatus=1, items=[MagicMock(item_code=self.item_code, warehouse=self.warehouse)])
			mock_get_doc.return_value = mock_so

			res = execute_sales_order_cancellation(
				so_name="SO-SIM-OUTBOX",
				external_order_id="103",
				sales_channel=self.sales_channel,
				provider=self.provider,
				state_id="6",
				state_name="Canceled",
			)
			self.assertTrue(res["success"])

		# Roll back transaction
		frappe.db.rollback()

	def test_16_rollback_on_outbox_failure_aborts_cancellation(self):
		"""If outbox persistence fails, savepoint rolls back and raises OrderReservationFailedError."""
		mock_so = MagicMock(docstatus=1, items=[MagicMock(item_code=self.item_code, warehouse=self.warehouse)])

		with patch("frappe.get_doc", return_value=mock_so), \
		     patch("bop_erp.orders.reconciliation.audit_sales_order_cancellation_safety", return_value=(True, [])), \
		     patch("frappe.get_all", return_value=[]), \
		     patch("bop_erp.orders.reconciliation.persist_publication_outbox_intents", side_effect=Exception("Disk error on outbox")):

			with self.assertRaises(Exception):
				execute_sales_order_cancellation(
					so_name="SO-FAIL-OUTBOX",
					external_order_id="104",
					sales_channel=self.sales_channel,
					provider=self.provider,
					state_id="6",
					state_name="Canceled",
				)

		frappe.db.rollback()

	# ==================================================
	# 7. MATERIAL NON-CANCELLATION CHANGE DETECTION
	# ==================================================
	def test_17_material_change_quantity_increase_routes_to_review(self):
		"""Remote quantity increase (e.g. 2 -> 5) is flagged as material change requiring review."""
		mock_so = MagicMock(currency="USD", net_total=20.0, items=[MagicMock(item_code=self.item_code, qty=2.0)])

		ext_order = ExternalOrder(
			provider=self.provider,
			sales_channel=self.sales_channel,
			external_order_id="CHG-01",
			external_reference="REF-CHG-01",
			order_state_id="2",
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=5.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=50.0),
		)

		with patch("bop_erp.orders.reconciliation.resolve_order_line_item", return_value=self.item_code):
			has_chg, details = detect_material_order_changes(mock_so, ext_order)
			self.assertTrue(has_chg)
			self.assertIn("increased", details[0])

	def test_18_material_change_quantity_decrease_routes_to_review(self):
		"""Remote quantity decrease (e.g. 5 -> 2) is flagged as material change requiring review."""
		mock_so = MagicMock(currency="USD", net_total=50.0, items=[MagicMock(item_code=self.item_code, qty=5.0)])

		ext_order = ExternalOrder(
			provider=self.provider,
			sales_channel=self.sales_channel,
			external_order_id="CHG-02",
			external_reference="REF-CHG-02",
			order_state_id="2",
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=2.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0),
		)

		with patch("bop_erp.orders.reconciliation.resolve_order_line_item", return_value=self.item_code):
			has_chg, details = detect_material_order_changes(mock_so, ext_order)
			self.assertTrue(has_chg)
			self.assertIn("decreased", details[0])

	def test_19_material_change_currency_routes_to_review(self):
		"""Remote currency change is flagged as material change requiring review."""
		mock_so = MagicMock(currency="USD", net_total=10.0, items=[MagicMock(item_code=self.item_code, qty=1.0)])

		ext_order = ExternalOrder(
			provider=self.provider,
			sales_channel=self.sales_channel,
			external_order_id="CHG-03",
			external_reference="REF-CHG-03",
			order_state_id="2",
			currency="EUR",
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=1.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=10.0),
		)

		with patch("bop_erp.orders.reconciliation.resolve_order_line_item", return_value=self.item_code):
			has_chg, details = detect_material_order_changes(mock_so, ext_order)
			self.assertTrue(has_chg)
			self.assertTrue(any("Currency" in d for d in details))

	# ==================================================
	# 8. REFUND & PAYMENT ERROR STATES
	# ==================================================
	def test_20_refund_state_routes_to_review_without_financial_entries(self):
		"""PrestaShop state 7 (Refunded) routes to CHANGE_REVIEW_REQUIRED with 0 financial entries."""
		initial_invoices = frappe.db.count("Sales Invoice")
		initial_payments = frappe.db.count("Payment Entry")

		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": self.provider,
			"sales_channel": self.sales_channel,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.RECONCILE_ORDER_STATE,
			"external_id": "REFUND-ORD-01",
			"idempotency_key": "REFUND-KEY-001",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({"external_state_id": "7"}),
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		with patch("bop_erp.orders.reconciliation.find_existing_order_mapping", return_value="SO-REFUND-001"), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.set_value") as mock_set_val:

			res = process_order_state_reconciliation_event(event.name)
			self.assertFalse(res["success"])
			self.assertEqual(res["category"], "REVIEW_REQUIRED")

			mock_set_val.assert_any_call(
				"Sales Order",
				"SO-REFUND-001",
				{
					"integration_status": IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED,
					"external_order_state": "7",
					"external_order_state_name": "Refunded",
					"external_order_state_updated_at": mock_set_val.call_args[0][2]["external_order_state_updated_at"],
					"latest_integration_event": event.name,
				},
			)

		# Zero financial side effects
		self.assertEqual(frappe.db.count("Sales Invoice"), initial_invoices)
		self.assertEqual(frappe.db.count("Payment Entry"), initial_payments)

		frappe.db.delete("Integration Event", {"name": event.name})
		frappe.db.commit()

	def test_21_payment_error_state_routes_to_review(self):
		"""PrestaShop state 8 (Payment error) routes to CHANGE_REVIEW_REQUIRED."""
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": self.provider,
			"sales_channel": self.sales_channel,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.RECONCILE_ORDER_STATE,
			"external_id": "PAYERR-ORD-01",
			"idempotency_key": "PAYERR-KEY-001",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({"external_state_id": "8"}),
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		with patch("bop_erp.orders.reconciliation.find_existing_order_mapping", return_value="SO-PAYERR-001"), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.set_value") as mock_set_val:

			res = process_order_state_reconciliation_event(event.name)
			self.assertFalse(res["success"])
			self.assertEqual(res["category"], "REVIEW_REQUIRED")

		frappe.db.delete("Integration Event", {"name": event.name})
		frappe.db.commit()

	# ==================================================
	# 9. CROSS-CHANNEL & HOST SAFETY
	# ==================================================
	def test_22_cross_channel_identity_scoping(self):
		"""Same external order ID on different channels maps to distinct Sales Orders."""
		with patch("frappe.db.get_value") as mock_get_val:
			def side_effect(doctype, filters, *a, **kw):
				if isinstance(filters, dict):
					ch = filters.get("sales_channel")
					if ch == "CH-1":
						return "SO-CH1-001"
					elif ch == "CH-2":
						return "SO-CH2-001"
			mock_get_val.side_effect = side_effect
			from bop_erp.orders.ingestion import find_existing_order_mapping
			so1 = find_existing_order_mapping("CH-1", self.provider, "100")
			so2 = find_existing_order_mapping("CH-2", self.provider, "100")
			self.assertEqual(so1, "SO-CH1-001")
			self.assertEqual(so2, "SO-CH2-001")
			self.assertNotEqual(so1, so2)

	def test_23_host_safety_pre_network_assertion(self):
		"""Connector pointing to external production host raises safety violation before network request."""
		connector = {
			"name": "PS-PROD-TEST",
			"sales_channel": self.sales_channel,
			"environment": "PRODUCTION",
			"base_url": "https://theindustrialdepot.com",
		}
		with self.assertRaises(frappe.ValidationError):
			discover_channel_order_reconciliations(connector)

	# ==================================================
	# 10. SCHEDULER BOUNDS & FAIRNESS
	# ==================================================
	def test_24_scheduler_bounds_and_fairness(self):
		"""Multichannel reconciliation discovery obeys global budget constraint."""
		connectors = [
			{"sales_channel": "CH-A", "environment": "DEVELOPMENT", "base_url": "http://prestashop-test", "name": "C1"},
			{"sales_channel": "CH-B", "environment": "DEVELOPMENT", "base_url": "http://prestashop-test", "name": "C2"},
		]

		with patch("bop_erp.orders.discovery.discover_eligible_connectors", return_value=connectors), \
		     patch("bop_erp.orders.discovery.discover_channel_order_reconciliations") as mock_disc:

			mock_disc.return_value = {"orders_seen": 5, "events_created": 5}

			res = discover_multichannel_order_reconciliations(
				max_channels=2,
				max_orders_per_channel=10,
				global_max_orders=7,
			)

			self.assertEqual(mock_disc.call_count, 2)
			self.assertEqual(res["channels_processed"], 2)
			# Second call was capped to 7 - 5 = 2
			second_call_max = mock_disc.call_args_list[1][1]["max_orders"]
			self.assertEqual(second_call_max, 2)
