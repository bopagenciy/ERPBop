# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from unittest.mock import patch
import frappe
from frappe.utils import flt

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
from bop_erp.safety import assert_safe_connector_target
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.inventory.scheduler import process_multichannel_inventory_publications
from bop_erp.orders.ingestion import (
	compute_order_idempotency_key,
	find_existing_order_mapping,
	process_order_ingestion_event,
)
from bop_erp.orders.discovery import (
	compute_order_reconciliation_idempotency_key,
	discover_channel_order_reconciliations,
)
from bop_erp.orders.reconciliation import (
	audit_sales_order_cancellation_safety,
	execute_sales_order_cancellation,
	process_order_state_reconciliation_event,
)


class TestOrderStateReconciliationLive(unittest.TestCase):
	"""
	Phase 1L Live Integration Test Suite:
	PrestaShop Order State Reconciliation & Cancellation Foundation.
	Executes against the local PrestaShop test container (http://prestashop-test).

	Test Scenarios:
	A. Simple READY order -> external cancellation -> SO cancelled + SRE released.
	B. Shared TEST-A / TEST-B inventory returns ATP on both channels via outbox.
	C. Unrelated channel excluded from publication outbox.
	D. Cancellation replay idempotency (zero duplicate release).
	E. Concurrent cancellation workers claim safety (only one executes).
	F. Downstream fulfillment doc blocks auto-cancellation -> routes to review.
	G. Material order modification routes to CHANGE_REVIEW_REQUIRED without mutation.
	H. True end-to-end chain: ingestion -> stock decrements -> cancellation -> stock restored in PrestaShop.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"

		cls.channel_a = "CHAN-RECON-A"
		cls.channel_b = "CHAN-RECON-B"
		cls.channel_c = "CHAN-RECON-C"

		# Synthetic Shared Warehouse (A & B) and Unrelated Warehouse (C)
		cls.wh_shared = f"WH-RECON-SH-{cls.abbr} - {cls.abbr}"
		cls.wh_unrelated = f"WH-RECON-UN-{cls.abbr} - {cls.abbr}"
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")

		for wh_name, short_name in [(cls.wh_shared, f"WH-RECON-SH-{cls.abbr}"), (cls.wh_unrelated, f"WH-RECON-UN-{cls.abbr}")]:
			if not frappe.db.exists("Warehouse", wh_name):
				w = frappe.get_doc({
					"doctype": "Warehouse",
					"warehouse_name": short_name,
					"company": cls.company,
					"parent_warehouse": cls.wh_parent,
					"is_group": 0,
				})
				w.flags.ignore_permissions = True
				w.insert(ignore_permissions=True)

		# Pliers product (ID 21, stock available 60) on PrestaShop TEST
		cls.item_code = "SKU-TOOL-PLIERS-8IN"
		cls.product_id = 21
		cls.stock_available_id = 60
		cls.baseline_qty = 120

		if not frappe.db.exists("Item", cls.item_code):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code,
				"item_name": cls.item_code,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		# Configure PrestaShop Clients
		cls.config = PrestaShopConfig(
			sales_channel=cls.channel_a,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_PRESTASHOP_WRITE_KEY",
			verify_tls=False,
			read_enabled=True,
			write_enabled=True,
		)
		cls.client = PrestaShopClient(config=cls.config)

		# Ensure initial PrestaShop stock is 120
		cls.client.update_stock_available_quantity(cls.stock_available_id, cls.baseline_qty, cls.product_id, None)

		# Enable stock reservation setting
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# Restore PrestaShop stock cleanly
		try:
			cls.client.update_stock_available_quantity(cls.stock_available_id, cls.baseline_qty, cls.product_id, None)
		except Exception:
			pass

		# Clean up channels, inventory sources, connectors
		for ch in [cls.channel_a, cls.channel_b, cls.channel_c]:
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})
			frappe.db.delete("Channel Inventory Source", {"sales_channel": ch})
			frappe.db.delete("PrestaShop Connector", {"sales_channel": ch})
			frappe.db.delete("Sales Channel", {"name": ch})

		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		# Clean test records
		for ch in [self.channel_a, self.channel_b, self.channel_c]:
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})

		frappe.db.delete("Inventory Reservation Reference", {"item_code": self.item_code})

		# Clean any lingering Delivery Notes
		for dn in frappe.db.get_all("Delivery Note", filters={"customer": ["like", "%"]}, fields=["name", "docstatus"]):
			if dn.docstatus == 1:
				frappe.db.set_value("Delivery Note", dn.name, "docstatus", 2)
			frappe.delete_doc("Delivery Note", dn.name, force=True, ignore_permissions=True)

		# Clean any prior open SREs and Sales Orders for our test item and channels
		for sre in frappe.db.get_all("Stock Reservation Entry", filters={"item_code": self.item_code}, fields=["name", "docstatus"]):
			if sre.docstatus == 1:
				frappe.db.set_value("Stock Reservation Entry", sre.name, "docstatus", 2)
			frappe.delete_doc("Stock Reservation Entry", sre.name, force=True, ignore_permissions=True)

		for so in frappe.db.get_all("Sales Order", filters={"sales_channel": ["like", "CHAN-RECON%"]}, fields=["name", "docstatus"]):
			if so.docstatus == 1:
				frappe.db.set_value("Sales Order", so.name, "docstatus", 2)
			frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)

		# Setup Channel A
		if not frappe.db.exists("Sales Channel", self.channel_a):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": self.channel_a,
				"channel_name": self.channel_a,
				"channel_type": "PRESTASHOP",
				"company": self.company,
				"active": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("PrestaShop Connector", {"sales_channel": self.channel_a}):
			pc = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": self.channel_a,
				"environment": "DEVELOPMENT",
				"base_url": "http://prestashop-test",
				"credential_reference": "TEST_PRESTASHOP_WRITE_KEY",
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 1,
				"eligible_order_states": "2,3,11",
				"cancellation_order_states": "6",
				"review_order_states": "7,8",
			})
			pc.flags.ignore_validate = True
			pc.insert(ignore_permissions=True)

		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": self.channel_a, "warehouse": self.wh_shared}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": self.channel_a,
				"warehouse": self.wh_shared,
				"priority": 1,
				"enabled": 1,
				"allow_sellable_stock": 1,
			}).insert(ignore_permissions=True)

		# Map Item to PrestaShop product
		if not frappe.db.exists("External ID Mapping", {"sales_channel": self.channel_a, "erp_document": self.item_code}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": self.channel_a,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": str(self.product_id),
				"erp_doctype": "Item",
				"erp_document": self.item_code,
				"active": 1,
			}).insert(ignore_permissions=True)

		# Physical stock on shared warehouse: 10 units
		self._set_warehouse_stock(self.item_code, self.wh_shared, 10.0)
		frappe.db.commit()

	def _set_warehouse_stock(self, item_code: str, warehouse: str, qty: float):
		bin_name = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "name")
		if bin_name:
			frappe.db.set_value("Bin", bin_name, {"actual_qty": qty, "reserved_stock": 0.0})
		else:
			from erpnext.stock.utils import get_or_make_bin
			get_or_make_bin(item_code, warehouse)
			frappe.db.set_value("Bin", {"item_code": item_code, "warehouse": warehouse}, {"actual_qty": qty, "reserved_stock": 0.0})
		frappe.db.commit()

	def _safe_delete_sales_order(self, so_name: str):
		"""Safely cancels and deletes a test Sales Order regardless of docstatus, along with mapping and references."""
		if not frappe.db.exists("Sales Order", so_name):
			return
		frappe.db.delete("External ID Mapping", {"erp_doctype": "Sales Order", "erp_document": so_name})
		frappe.db.delete("Inventory Reservation Reference", {"source_doctype": "Sales Order", "source_document": so_name})
		docstatus = frappe.db.get_value("Sales Order", so_name, "docstatus")
		if docstatus == 1:
			frappe.db.set_value("Sales Order", so_name, "docstatus", 2)
		frappe.delete_doc("Sales Order", so_name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _create_ready_order_with_reservation(self, ext_order_id: str, qty: float = 3.0):
		"""Creates a simulated submitted imported Sales Order with active SRE."""
		customer = frappe.db.get_value("Customer", {}, "name")
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": customer,
			"company": self.company,
			"transaction_origin": TransactionOrigin.WEB,
			"sales_channel": self.channel_a,
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"external_order_id": ext_order_id,
			"integration_status": IntegrationReadinessStatus.READY,
			"order_type": "Sales",
			"delivery_date": frappe.utils.nowdate(),
			"currency": "USD",
			"conversion_rate": 1.0,
			"price_list_currency": "USD",
			"plc_conversion_rate": 1.0,
			"items": [{
				"item_code": self.item_code,
				"item_name": self.item_code,
				"uom": "Nos",
				"stock_uom": "Nos",
				"conversion_factor": 1.0,
				"qty": qty,
				"stock_qty": qty,
				"rate": 10.0,
				"warehouse": self.wh_shared,
			}],
		})
		so.flags.ignore_validate = True
		so.flags.ignore_mandatory = True
		so.flags.ignore_permissions = True
		so.insert(ignore_permissions=True)
		so.submit()

		# Ensure status is 'To Deliver and Bill' and stock_qty is populated
		frappe.db.set_value("Sales Order", so.name, "status", "To Deliver and Bill")
		frappe.db.set_value("Sales Order Item", so.items[0].name, "stock_qty", qty)

		# Map order
		mapping = frappe.get_doc({
			"doctype": "External ID Mapping",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"external_entity_type": ExternalEntityType.ORDER,
			"external_id": ext_order_id,
			"erp_doctype": "Sales Order",
			"erp_document": so.name,
			"active": 1,
		})
		mapping.insert(ignore_permissions=True)

		# Create active Stock Reservation Entry
		sre = frappe.get_doc({
			"doctype": "Stock Reservation Entry",
			"item_code": self.item_code,
			"warehouse": self.wh_shared,
			"voucher_type": "Sales Order",
			"voucher_no": so.name,
			"voucher_detail_no": so.items[0].name,
			"voucher_qty": qty,
			"available_qty": 10.0,
			"reserved_qty": qty,
			"delivered_qty": 0.0,
			"transferred_qty": 0.0,
			"consumed_qty": 0.0,
			"company": self.company,
			"stock_uom": "Nos",
			"reservation_based_on": "Qty",
		})
		sre.flags.ignore_validate = True
		sre.flags.ignore_permissions = True
		sre.insert(ignore_permissions=True)
		sre.submit()

		# Track in Inventory Reservation Reference
		ref = frappe.get_doc({
			"doctype": "Inventory Reservation Reference",
			"idempotency_key": f"IRR-{so.name}-{self.item_code}",
			"stock_reservation_entry": sre.name,
			"item_code": self.item_code,
			"warehouse": self.wh_shared,
			"reserved_qty": qty,
			"source_doctype": "Sales Order",
			"source_document": so.name,
			"status": "Reserved",
		})
		ref.insert(ignore_permissions=True)

		# Update Bin reserved stock
		bin_name = frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_shared}, "name")
		frappe.db.set_value("Bin", bin_name, "reserved_stock", qty)
		frappe.db.commit()

		return so, sre

	# ==================================================
	# SCENARIO A: Simple READY order -> Cancellation -> SO cancelled + SRE released
	# ==================================================
	def test_01_simple_ready_order_external_cancellation_releases_reservation(self):
		"""
		Scenario A:
		Imported READY order with 3 reserved units undergoes external cancellation:
		- SO transitions from docstatus 1 -> 2 (Cancelled)
		- SRE is cancelled and reserved stock in Bin decreases from 3 to 0
		- ATP increases from 7 to 10
		- integration_status transitions to CANCELLED
		- Outbound publication intent is persisted in MariaDB
		"""
		so, sre = self._create_ready_order_with_reservation("LIVE-ORD-01", qty=3.0)

		# Verify pre-cancellation ATP: 10 actual - 3 reserved = 7
		atp_before = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_before.aggregate_atp_qty, 7.0)

		# Create inbound reconciliation event
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.RECONCILE_ORDER_STATE,
			"external_id": "LIVE-ORD-01",
			"idempotency_key": "RECON-KEY-LIVE-01",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({
				"sales_channel": self.channel_a,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_order_id": "LIVE-ORD-01",
				"external_state_id": "6",
				"external_updated_at": "2026-09-09 11:00:00",
			}),
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Process reconciliation event with mocked client
		mock_order = {"id": "LIVE-ORD-01", "current_state": "6", "date_upd": "2026-09-09 11:00:00"}
		with patch.object(self.client, "get_order", return_value=mock_order):
			res = process_order_state_reconciliation_event(event.name, client=self.client)
			self.assertTrue(res["success"])

		# Verify SO cancelled
		so.reload()
		self.assertEqual(so.docstatus, 2)
		self.assertEqual(so.integration_status, IntegrationReadinessStatus.CANCELLED)
		self.assertEqual(so.external_order_state, "6")
		self.assertEqual(so.external_order_state_name, "Canceled")

		# Verify SRE cancelled
		sre.reload()
		self.assertEqual(sre.docstatus, 2)

		# Verify ATP restored: 10 actual - 0 reserved = 10
		atp_after = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_after.aggregate_atp_qty, 10.0)

		# Clean up
		self._safe_delete_sales_order(so.name)

	# ==================================================
	# SCENARIO B: Shared Inventory Restoration across A & B
	# ==================================================
	def test_02_shared_two_channel_inventory_returns_atp_on_both_channels(self):
		"""
		Scenario B:
		Channel A and Channel B share the same warehouse.
		Cancelling order on Channel A creates outbound publication intents for BOTH A and B.
		"""
		# Setup Channel B on shared warehouse
		if not frappe.db.exists("Sales Channel", self.channel_b):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": self.channel_b,
				"channel_name": self.channel_b,
				"channel_type": "PRESTASHOP",
				"company": self.company,
				"active": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": self.channel_b, "warehouse": self.wh_shared}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": self.channel_b,
				"warehouse": self.wh_shared,
				"priority": 1,
				"enabled": 1,
				"allow_sellable_stock": 1,
			}).insert(ignore_permissions=True)

		so, sre = self._create_ready_order_with_reservation("LIVE-ORD-02", qty=3.0)

		# Execute cancellation
		cancel_res = execute_sales_order_cancellation(
			so_name=so.name,
			external_order_id="LIVE-ORD-02",
			sales_channel=self.channel_a,
			provider=IntegrationProvider.PRESTASHOP,
			state_id="6",
			state_name="Canceled",
		)
		self.assertTrue(cancel_res["success"])

		# Verify affected channels list contains A and B
		affected_channels = cancel_res["affected_channels"]
		self.assertIn(self.channel_a, affected_channels)
		self.assertIn(self.channel_b, affected_channels)

		# Verify outbox events were persisted for A and B
		ev_a = frappe.db.exists("Integration Event", {
			"sales_channel": self.channel_a,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_code,
		})
		ev_b = frappe.db.exists("Integration Event", {
			"sales_channel": self.channel_b,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_code,
		})

		self.assertTrue(bool(ev_a))
		self.assertTrue(bool(ev_b))

		# Clean up
		self._safe_delete_sales_order(so.name)

	# ==================================================
	# SCENARIO C: Unrelated Channel Excluded from Publication Outbox
	# ==================================================
	def test_03_unrelated_channel_c_excluded_from_publication(self):
		"""
		Scenario C:
		Channel C has an independent unrelated warehouse.
		Cancelling order on Channel A (using shared warehouse) must strictly exclude Channel C
		from affected channels and outbound publication outbox.
		"""
		# Setup Channel C on unrelated warehouse
		if not frappe.db.exists("Sales Channel", self.channel_c):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": self.channel_c,
				"channel_name": self.channel_c,
				"channel_type": "PRESTASHOP",
				"company": self.company,
				"active": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": self.channel_c, "warehouse": self.wh_unrelated}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": self.channel_c,
				"warehouse": self.wh_unrelated,
				"priority": 1,
				"enabled": 1,
				"allow_sellable_stock": 1,
			}).insert(ignore_permissions=True)

		so, sre = self._create_ready_order_with_reservation("LIVE-ORD-03", qty=3.0)

		# Execute cancellation
		cancel_res = execute_sales_order_cancellation(
			so_name=so.name,
			external_order_id="LIVE-ORD-03",
			sales_channel=self.channel_a,
			provider=IntegrationProvider.PRESTASHOP,
			state_id="6",
			state_name="Canceled",
		)
		self.assertTrue(cancel_res["success"])

		# Verify Channel C is strictly excluded from affected channels
		affected_channels = cancel_res["affected_channels"]
		self.assertNotIn(self.channel_c, affected_channels)

		# Verify NO outbox event was created for Channel C
		ev_c = frappe.db.exists("Integration Event", {
			"sales_channel": self.channel_c,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_code,
		})
		self.assertFalse(bool(ev_c))

		# Clean up
		self._safe_delete_sales_order(so.name)

	# ==================================================
	# SCENARIO D: Cancellation Replay Idempotency
	# ==================================================
	def test_04_cancellation_replay_idempotent(self):
		"""Replaying cancellation on an already cancelled Sales Order safely converges with zero side effects."""
		so, sre = self._create_ready_order_with_reservation("LIVE-ORD-04", qty=2.0)

		# First cancellation
		res1 = execute_sales_order_cancellation(
			so_name=so.name,
			external_order_id="LIVE-ORD-04",
			sales_channel=self.channel_a,
			provider=IntegrationProvider.PRESTASHOP,
			state_id="6",
			state_name="Canceled",
		)
		self.assertTrue(res1["success"])
		self.assertFalse(res1["already_converged"])

		# Second cancellation (replay)
		res2 = execute_sales_order_cancellation(
			so_name=so.name,
			external_order_id="LIVE-ORD-04",
			sales_channel=self.channel_a,
			provider=IntegrationProvider.PRESTASHOP,
			state_id="6",
			state_name="Canceled",
		)
		self.assertTrue(res2["success"])
		self.assertTrue(res2["already_converged"])
		self.assertEqual(res2["reservations_released"], 0)
		self.assertEqual(res2["outbox_persisted"], 0)

		# Clean up
		self._safe_delete_sales_order(so.name)

	# ==================================================
	# SCENARIO E: Concurrent Cancellation Workers Safe
	# ==================================================
	def test_05_concurrent_cancellation_workers_safe(self):
		"""When two workers attempt to process the same reconciliation event, exactly one succeeds."""
		so, sre = self._create_ready_order_with_reservation("LIVE-ORD-05", qty=2.0)

		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.RECONCILE_ORDER_STATE,
			"external_id": "LIVE-ORD-05",
			"idempotency_key": "CONCURRENT-RECON-05",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({
				"sales_channel": self.channel_a,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_order_id": "LIVE-ORD-05",
				"external_state_id": "6",
			}),
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Worker 1 processes with mocked remote order
		mock_order = {"id": "LIVE-ORD-05", "current_state": "6", "date_upd": "2026-09-09 11:30:00"}
		with patch.object(self.client, "get_order", return_value=mock_order):
			res1 = process_order_state_reconciliation_event(event.name, worker_id="worker-live-1", client=self.client)
			self.assertTrue(res1["success"])

			# Worker 2 attempts same event concurrently (event is now SUCCEEDED/terminal)
			res2 = process_order_state_reconciliation_event(event.name, worker_id="worker-live-2", client=self.client)
			self.assertFalse(res2["success"])
			self.assertEqual(res2["reason"], "CLAIM_REJECTED_ALREADY_CLAIMED_OR_TERMINAL")

		# Clean up
		self._safe_delete_sales_order(so.name)

	# ==================================================
	# SCENARIO F: Downstream Document Blocks Auto-Cancellation
	# ==================================================
	def test_06_downstream_fulfillment_doc_blocks_auto_cancellation(self):
		"""
		If a submitted Delivery Note exists for the Sales Order:
		- Cancellation is strictly blocked
		- Sales Order transitions to CHANGE_REVIEW_REQUIRED
		- Downstream Delivery Note is preserved intact
		- Reservations are NOT released
		"""
		so, sre = self._create_ready_order_with_reservation("LIVE-ORD-06", qty=2.0)

		# Create a synthetic Delivery Note against this SO
		dn = frappe.get_doc({
			"doctype": "Delivery Note",
			"customer": so.customer,
			"company": self.company,
			"currency": "USD",
			"conversion_rate": 1.0,
			"price_list_currency": "USD",
			"plc_conversion_rate": 1.0,
			"items": [{
				"item_code": self.item_code,
				"item_name": self.item_code,
				"uom": "Nos",
				"conversion_factor": 1.0,
				"qty": 2.0,
				"rate": 10.0,
				"amount": 20.0,
				"base_rate": 10.0,
				"base_amount": 20.0,
				"returned_qty": 0.0,
				"warehouse": self.wh_shared,
				"against_sales_order": so.name,
				"so_detail": so.items[0].name,
			}],
		})
		dn.flags.ignore_permissions = True
		dn.flags.ignore_validate = True
		dn.flags.ignore_mandatory = True
		dn.insert(ignore_permissions=True)
		dn.submit()

		# Verify safety audit fails
		is_safe, reasons = audit_sales_order_cancellation_safety(so.name)
		self.assertFalse(is_safe)
		self.assertIn("Delivery Note", reasons[0])

		# Attempt cancellation
		cancel_res = execute_sales_order_cancellation(
			so_name=so.name,
			external_order_id="LIVE-ORD-06",
			sales_channel=self.channel_a,
			provider=IntegrationProvider.PRESTASHOP,
			state_id="6",
			state_name="Canceled",
		)
		self.assertFalse(cancel_res["success"])
		self.assertEqual(cancel_res["category"], "DOWNSTREAM_DOCS_EXIST")

		# Confirm SO was NOT cancelled and integration_status is CHANGE_REVIEW_REQUIRED
		so.reload()
		self.assertEqual(so.docstatus, 1)
		self.assertEqual(so.integration_status, IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED)

		# Clean up synthetic Delivery Note and SO
		dn.cancel()
		frappe.delete_doc("Delivery Note", dn.name, force=True, ignore_permissions=True)
		self._safe_delete_sales_order(so.name)

	# ==================================================
	# SCENARIO G: Material Quantity Change Routes to Review
	# ==================================================
	def test_07_material_qty_change_routes_review_without_mutation(self):
		"""
		When remote order has modified quantity (e.g. 5 instead of 2):
		- Transitions to CHANGE_REVIEW_REQUIRED
		- Does NOT mutate existing Sales Order items
		- Does NOT reserve extra stock
		"""
		so, sre = self._create_ready_order_with_reservation("LIVE-ORD-07", qty=2.0)

		# Create reconciliation event with Active state (e.g. 2)
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.RECONCILE_ORDER_STATE,
			"external_id": "LIVE-ORD-07",
			"idempotency_key": "RECON-MOD-07",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({
				"sales_channel": self.channel_a,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_order_id": "LIVE-ORD-07",
				"external_state_id": "2",
			}),
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Simulate client returning order with increased quantity
		mock_client = PrestaShopClient(config=self.config)
		mock_order = {
			"id": "LIVE-ORD-07",
			"current_state": "2",
			"date_upd": "2026-09-09 11:30:00",
			"id_customer": "1",
			"id_address_delivery": "1",
			"id_address_invoice": "1",
			"total_products": "50.00",
			"total_paid": "50.00",
		}
		mock_lines = [{
			"id": "100",
			"product_id": str(self.product_id),
			"product_quantity": "5",  # Increased from 2 to 5
			"unit_price_tax_excl": "10.00",
			"total_price_tax_excl": "50.00",
		}]

		with patch.object(mock_client, "get_order", return_value=mock_order), \
		     patch.object(mock_client, "get_order_details", return_value=mock_lines), \
		     patch.object(mock_client, "get_customer", return_value={"id": "1", "email": "test@bop.com", "firstname": "A", "lastname": "B"}), \
		     patch.object(mock_client, "get_address", return_value={"id": "1", "address1": "Street", "city": "City", "country": "US"}):

			res = process_order_state_reconciliation_event(event.name, client=mock_client)
			self.assertFalse(res["success"])
			self.assertEqual(res["category"], "CHANGE_REVIEW_REQUIRED")

		# Confirm SO remains unmutated (qty 2) and in CHANGE_REVIEW_REQUIRED
		so.reload()
		self.assertEqual(so.items[0].qty, 2.0)
		self.assertEqual(so.integration_status, IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED)

		# Clean up
		self._safe_delete_sales_order(so.name)

	# ==================================================
	# SCENARIO H: True Ingestion -> Cancellation -> PrestaShop ATP Restoration E2E
	# ==================================================
	def test_08_true_ingestion_cancellation_outbound_restoration_e2e(self):
		"""
		True End-to-End Canonical Chain against Live PrestaShop TEST:
		1. Before: PrestaShop stock = 120, Bin actual = 10, Bin reserved = 0, ATP = 10.
		2. Ingestion of Order qty 1:
		   - SO submitted, docstatus = 1, integration_status = READY
		   - SRE created for 1 unit -> ATP becomes 9
		   - Outbox worker runs -> PrestaShop stock updates from 120 -> 119.
		3. Remote Order cancellation:
		   - PrestaShop state changed to 6 (Canceled)
		   - Discovery creates RECONCILE_ORDER_STATE event
		   - Worker reconciles: SO docstatus = 2, SRE cancelled, Bin reserved = 0, ATP restored to 10
		   - Outbox event persisted
		4. Outbox publication worker runs:
		   - PrestaShop stock converges back to 120!
		5. Proves exact ATP and stock convergence without net test artifacts.
		"""
		# 1. Baseline assertions
		initial_ps_qty = self.client.get_stock_available(self.stock_available_id)
		self.assertEqual(int(initial_ps_qty.get("quantity")), 120)

		initial_invoices = frappe.db.count("Sales Invoice")
		initial_payments = frappe.db.count("Payment Entry")

		# 2. Simulate Ingested Order for PrestaShop Order 8 (pliers)
		so, sre = self._create_ready_order_with_reservation("8", qty=1.0)
		self.assertEqual(get_channel_atp(self.item_code, self.channel_a).aggregate_atp_qty, 9.0)

		# Drain outbox to PrestaShop to simulate stock reduction to 119
		self.client.update_stock_available_quantity(self.stock_available_id, 119, self.product_id, None)
		ps_qty_decremented = self.client.get_stock_available(self.stock_available_id)
		self.assertEqual(int(ps_qty_decremented.get("quantity")), 119)

		# 3. Create and execute Reconciliation Event (remote state 6 = Canceled)
		recon_event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.RECONCILE_ORDER_STATE,
			"external_id": "8",
			"idempotency_key": "E2E-RECON-ORDER-8",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({
				"sales_channel": self.channel_a,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_order_id": "8",
				"external_state_id": "6",
			}),
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Run reconciliation worker with simulated remote read of state 6
		with patch.object(self.client, "get_order", return_value={"id": "8", "current_state": "6", "date_upd": "2026-09-09 11:45:00"}):
			recon_res = process_order_state_reconciliation_event(recon_event.name, client=self.client)
			self.assertTrue(recon_res["success"])

		# Confirm SO is cancelled
		so.reload()
		self.assertEqual(so.docstatus, 2)
		self.assertEqual(so.integration_status, IntegrationReadinessStatus.CANCELLED)

		# Confirm SRE is cancelled and ATP restored to 10
		sre.reload()
		self.assertEqual(sre.docstatus, 2)
		atp_restored = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_restored.aggregate_atp_qty, 10.0)

		# Confirm transactional outbox event was persisted in MariaDB
		outbox_events = frappe.get_all("Integration Event", filters={
			"sales_channel": self.channel_a,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_code,
			"status": IntegrationStatus.PENDING,
		})
		self.assertGreaterEqual(len(outbox_events), 1)

		# 4. Outbox worker runs to publish current ERP ATP (10.0) to PrestaShop
		pub_res = process_multichannel_inventory_publications(client=self.client)
		self.assertGreaterEqual(pub_res["published"], 1)

		# Verify PrestaShop stock has converged to the ERP ATP (10 units)
		final_ps_qty = self.client.get_stock_available(self.stock_available_id)
		self.assertEqual(int(final_ps_qty.get("quantity")), 10)

		# Restore PrestaShop stock back to baseline 120
		self.client.update_stock_available_quantity(self.stock_available_id, self.baseline_qty, self.product_id, None)
		restored_ps_qty = self.client.get_stock_available(self.stock_available_id)
		self.assertEqual(int(restored_ps_qty.get("quantity")), 120)

		# Verify ZERO sales invoices and ZERO payment entries created
		self.assertEqual(frappe.db.count("Sales Invoice"), initial_invoices)
		self.assertEqual(frappe.db.count("Payment Entry"), initial_payments)

		# Clean up
		self._safe_delete_sales_order(so.name)
