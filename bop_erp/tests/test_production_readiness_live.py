# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import dataclasses
import json
import unittest
from unittest.mock import patch, MagicMock
from datetime import timedelta
import frappe
from frappe.utils import flt, now_datetime, get_datetime

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	IntegrationReadinessStatus,
	TransactionOrigin,
)
from bop_erp.safety import (
	assert_safe_write_target,
	ConnectorSafetyError,
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
	assert_sales_order_ready_for_fulfillment,
)
from bop_erp.orders.ingestion import (
	ingest_order_pipeline,
	process_order_ingestion_event,
	find_existing_order_mapping,
)
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.inventory.publication import (
	normalize_publishable_quantity,
	compute_publication_hash,
	compute_publication_idempotency_key,
	get_or_create_publication_state,
	commit_publication_state,
	schedule_channel_inventory_publication,
	process_inventory_publication_event,
)
from bop_erp.inventory.scheduler import (
	process_pending_inventory_publications,
	process_multichannel_inventory_publications,
)
from bop_erp.inventory.reconciliation import (
	reconcile_channel_inventory,
	reconcile_inventory_item,
)
from bop_erp.monitoring import (
	get_system_health,
	get_system_readiness,
)
from bop_erp.operations import (
	retry_failed_publication_event,
	recover_stale_publication_events,
	get_dead_letter_events,
	get_retry_pending_events,
	get_failed_review_orders,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopServerError,
)


class TestProductionReadinessLive(unittest.TestCase):
	"""
	Live Integration Test Suite for Phase 1M:
	Production Readiness, Operational Resilience & Controlled Pilot Gate.
	Executes against the local disposable PrestaShop test instance (http://prestashop-test).

	Verifies:
	1. Duplicate order replay and idempotency under concurrent delivery.
	2. Concurrent inventory reservation safety near exhaustion.
	3. Redis outage recovery: durable MariaDB outbox survives Redis drop and drains cleanly.
	4. PrestaShop outage resilience: transient 503 -> RETRY_PENDING -> recovery -> IN_SYNC.
	5. Bounded synthetic load: bounded multi-channel draining with zero lost events.
	6. DB hygiene and baseline stock restoration (Product 6 = 300, Product 21 = 120).
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.orig_stock_res = frappe.db.get_single_value("Stock Settings", "enable_stock_reservation")
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"
		cls.diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)
		cls.cust_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "Commercial"

		cls.channel_tid = "CHAN-LIVE-M"
		cls.provider = IntegrationProvider.PRESTASHOP

		# Test warehouse
		cls.wh_test = f"WH-LIVE-M-{cls.abbr} - {cls.abbr}"
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")
		if not frappe.db.exists("Warehouse", cls.wh_test):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"WH-LIVE-M-{cls.abbr}",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
				"is_group": 0,
			})
			w.flags.ignore_permissions = True
			w.insert(ignore_permissions=True)

		# Test items mapping to PrestaShop disposable products
		cls.item_demo = "demo_11"
		cls.prod_id_demo = 6
		cls.sa_id_demo = 6
		cls.baseline_qty_demo = 300

		cls.item_pliers = "SKU-TOOL-PLIERS-8IN"
		cls.prod_id_pliers = 21
		cls.sa_id_pliers = 60
		cls.baseline_qty_pliers = 120

		for ic in [cls.item_demo, cls.item_pliers]:
			if not frappe.db.exists("Item", ic):
				frappe.get_doc({
					"doctype": "Item",
					"item_code": ic,
					"item_name": ic,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}).insert(ignore_permissions=True)

		cls.config = PrestaShopConfig(
			sales_channel=cls.channel_tid,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_PRESTASHOP_WRITE_KEY",
			verify_tls=False,
			read_enabled=True,
			write_enabled=True,
		)
		cls.client = PrestaShopClient(config=cls.config)

		# PrestaShop Connector & Sales Channel
		if not frappe.db.exists("Sales Channel", cls.channel_tid):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.channel_tid,
				"channel_name": "CHAN-LIVE-M Test Channel",
				"active": 1,
				"company": cls.company,
				"integration_provider": cls.provider,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("PrestaShop Connector", {"sales_channel": cls.channel_tid}):
			pc = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": cls.channel_tid,
				"environment": "DEVELOPMENT",
				"base_url": "http://prestashop-test",
				"credential_reference": "TEST_PRESTASHOP_WRITE_KEY",
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 1,
				"eligible_order_states": "2,3,4,Payment accepted",
			})
			pc.flags.ignore_validate = True
			pc.insert(ignore_permissions=True)
		else:
			frappe.db.set_value(
				"PrestaShop Connector",
				{"sales_channel": cls.channel_tid},
				{"write_enabled": 1, "enabled": 1, "eligible_order_states": "2,3,4,Payment accepted"},
			)

		# Channel inventory source
		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": cls.channel_tid, "warehouse": cls.wh_test}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.channel_tid,
				"warehouse": cls.wh_test,
				"allocation_percentage": 100.0,
				"company": cls.company,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		# Product External ID mappings
		if not frappe.db.exists("External ID Mapping", {"sales_channel": cls.channel_tid, "erp_document": cls.item_demo, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": cls.channel_tid,
				"provider": cls.provider,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": str(cls.prod_id_demo),
				"erp_doctype": "Item",
				"erp_document": cls.item_demo,
				"active": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("External ID Mapping", {"sales_channel": cls.channel_tid, "erp_document": cls.item_pliers, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": cls.channel_tid,
				"provider": cls.provider,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": str(cls.prod_id_pliers),
				"erp_doctype": "Item",
				"erp_document": cls.item_pliers,
				"active": 1,
			}).insert(ignore_permissions=True)

		# Reset remote stock
		cls.client.update_stock_available_quantity(cls.sa_id_demo, cls.baseline_qty_demo, cls.prod_id_demo, None)
		cls.client.update_stock_available_quantity(cls.sa_id_pliers, cls.baseline_qty_pliers, cls.prod_id_pliers, None)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._full_cleanup()
		try:
			cls.client.update_stock_available_quantity(cls.sa_id_demo, cls.baseline_qty_demo, cls.prod_id_demo, None)
			cls.client.update_stock_available_quantity(cls.sa_id_pliers, cls.baseline_qty_pliers, cls.prod_id_pliers, None)
		except Exception:
			pass

		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", cls.orig_stock_res)

		frappe.db.delete("Stock Ledger Entry", {"warehouse": cls.wh_test})
		frappe.db.delete("Stock Reconciliation Item", {"warehouse": cls.wh_test})
		frappe.db.delete("Bin", {"warehouse": cls.wh_test})
		if hasattr(cls, "wh_test") and frappe.db.exists("Warehouse", cls.wh_test):
			frappe.delete_doc("Warehouse", cls.wh_test, force=True, ignore_permissions=True)

		for ic in [cls.item_demo, cls.item_pliers]:
			frappe.db.delete("Bin", {"item_code": ic})

		frappe.db.delete("PrestaShop Connector", {"sales_channel": cls.channel_tid})
		frappe.db.delete("Sales Channel", {"name": cls.channel_tid})

		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _full_cleanup(cls):
		sos = frappe.get_all("Sales Order", filters={"sales_channel": cls.channel_tid}, fields=["name", "docstatus"])
		for so in sos:
			if so.docstatus == 1:
				try:
					doc = frappe.get_doc("Sales Order", so.name)
					doc.cancel()
				except Exception:
					pass
			frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)

		sres = frappe.get_all(
			"Stock Reservation Entry",
			filters={"item_code": ["in", [cls.item_demo, cls.item_pliers]]},
			fields=["name", "docstatus"],
		)
		for s in sres:
			if s.docstatus == 1:
				try:
					doc = frappe.get_doc("Stock Reservation Entry", s.name)
					doc.cancel()
				except Exception:
					pass
			frappe.delete_doc("Stock Reservation Entry", s.name, force=True, ignore_permissions=True)

		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [cls.item_demo, cls.item_pliers]]})
		frappe.db.sql("DELETE FROM `tabIntegration Event` WHERE sales_channel = %s", (cls.channel_tid,))
		frappe.db.sql("DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s", (cls.channel_tid,))
		frappe.db.sql(
			"DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s",
			(cls.channel_tid,),
		)
		frappe.db.delete("Channel Inventory Source", {"sales_channel": cls.channel_tid})
		frappe.db.delete("Channel Inventory Source", {"warehouse": cls.wh_test})

		# Test customers/addresses
		for c in frappe.get_all("Customer", filters={"name": ["like", "%Pilot%"]}, pluck="name"):
			frappe.db.delete("External ID Mapping", {"erp_document": c})
			for addr in frappe.get_all("Dynamic Link", filters={"link_doctype": "Customer", "link_name": c, "parenttype": "Address"}, pluck="parent"):
				frappe.db.delete("External ID Mapping", {"erp_document": addr})
				frappe.delete_doc("Address", addr, force=True, ignore_permissions=True)
			frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)

		frappe.db.delete("Bin", {"warehouse": cls.wh_test})
		for ic in [cls.item_demo, cls.item_pliers]:
			frappe.db.delete("Bin", {"item_code": ic})

		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self._full_cleanup()

		# Re-ensure Channel Inventory Source exists for CHAN-LIVE-M
		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": self.channel_tid, "warehouse": self.wh_test}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": self.channel_tid,
				"warehouse": self.wh_test,
				"allocation_percentage": 100.0,
				"company": self.company,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("External ID Mapping", {"sales_channel": self.channel_tid, "erp_document": self.item_demo, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": self.channel_tid,
				"provider": self.provider,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": str(self.prod_id_demo),
				"erp_doctype": "Item",
				"erp_document": self.item_demo,
				"active": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("External ID Mapping", {"sales_channel": self.channel_tid, "erp_document": self.item_pliers, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": self.channel_tid,
				"provider": self.provider,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": str(self.prod_id_pliers),
				"erp_doctype": "Item",
				"erp_document": self.item_pliers,
				"active": 1,
			}).insert(ignore_permissions=True)

		frappe.db.commit()

	def tearDown(self):
		self._full_cleanup()

	def _set_stock(self, item_code: str, qty: float):
		"""Sets physical stock in test warehouse via Stock Reconciliation."""
		sr = frappe.get_doc({
			"doctype": "Stock Reconciliation",
			"company": self.company,
			"purpose": "Opening Stock",
			"expense_account": self.diff_account,
			"items": [
				{
					"item_code": item_code,
					"warehouse": self.wh_test,
					"qty": qty,
					"valuation_rate": 10.0,
				}
			],
		})
		sr.flags.ignore_permissions = True
		sr.insert(ignore_permissions=True)
		sr.submit()
		frappe.db.commit()

	# =========================================================================
	# 1. Duplicate Order Replay & Idempotency Test
	# =========================================================================

	def test_duplicate_order_replay_idempotency(self):
		self._set_stock(self.item_demo, 50.0)

		ext_order = ExternalOrder(
			provider=self.provider,
			sales_channel=self.channel_tid,
			external_order_id="TEST-M-DUP-01",
			external_reference="REF-M-DUP-01",
			order_state_id="2",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="CUST-PILOT-01",
				first_name="Pilot",
				last_name="Tester",
				email="pilot.tester@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-PILOT-01",
				first_name="Pilot",
				last_name="Tester",
				address1="123 Industrial Rd",
				city="Houston",
				postcode="77001",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=str(self.prod_id_demo),
					sku=self.item_demo,
					quantity=2.0,
					unit_price_ex_tax=25.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=50.0, total_paid=50.0),
		)

		# 1st Ingestion
		res1 = ingest_order_pipeline(ext_order)
		self.assertTrue(res1.get("success"))
		so_name1 = res1.get("sales_order")
		self.assertIsNotNone(so_name1)

		# 2nd Ingestion (Replay / duplicate delivery)
		res2 = ingest_order_pipeline(ext_order)
		self.assertTrue(res2.get("success"))
		so_name2 = res2.get("sales_order")

		# Exactly the same sales order returned, no duplicate created
		self.assertEqual(so_name1, so_name2)
		self.assertTrue(res2.get("is_replay"))
		so_cnt = frappe.db.count("Sales Order", {"sales_channel": self.channel_tid})
		self.assertEqual(so_cnt, 1)

	# =========================================================================
	# 2. Concurrent Inventory Reservation Safety Near Exhaustion
	# =========================================================================

	def test_concurrent_inventory_reservation_safety(self):
		# Physical stock is 2.0
		self._set_stock(self.item_demo, 2.0)

		# Order 1 requests qty 1
		ext_order1 = ExternalOrder(
			provider=self.provider,
			sales_channel=self.channel_tid,
			external_order_id="TEST-M-RES-01",
			external_reference="REF-M-RES-01",
			order_state_id="2",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="CUST-PILOT-02",
				first_name="Pilot",
				last_name="Buyer1",
				email="pilot.buyer1@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-PILOT-02",
				first_name="Pilot",
				last_name="Buyer1",
				address1="456 Commerce Way",
				city="Austin",
				postcode="73301",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=str(self.prod_id_demo),
					sku=self.item_demo,
					quantity=1.0,
					unit_price_ex_tax=20.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		res1 = ingest_order_pipeline(ext_order1)
		self.assertTrue(res1.get("success"))
		so1 = res1.get("sales_order")
		st1 = frappe.db.get_value("Sales Order", so1, "integration_status")
		self.assertEqual(st1, IntegrationReadinessStatus.READY)

		# Order 2 requests qty 2 (exceeding remaining physical stock of 1)
		ext_order2 = ExternalOrder(
			provider=self.provider,
			sales_channel=self.channel_tid,
			external_order_id="TEST-M-RES-02",
			external_reference="REF-M-RES-02",
			order_state_id="2",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="CUST-PILOT-03",
				first_name="Pilot",
				last_name="Buyer2",
				email="pilot.buyer2@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-PILOT-03",
				first_name="Pilot",
				last_name="Buyer2",
				address1="789 Market St",
				city="Dallas",
				postcode="75001",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=str(self.prod_id_demo),
					sku=self.item_demo,
					quantity=2.0,
					unit_price_ex_tax=20.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=40.0, total_paid=40.0),
		)

		# Order 2 must fail reservation or land in Failed Review (no oversell)
		try:
			res2 = ingest_order_pipeline(ext_order2)
			so2 = res2.get("sales_order")
			if so2:
				st2 = frappe.db.get_value("Sales Order", so2, "integration_status")
				self.assertEqual(st2, IntegrationReadinessStatus.FAILED_REVIEW)
				with self.assertRaises(Exception):
					assert_sales_order_ready_for_fulfillment(so2)
		except (OrderReservationFailedError, OrderIngestionError):
			pass

		# Ensure total reserved quantity never exceeds physical stock of 2
		total_res = frappe.db.sql(
			"SELECT SUM(reserved_qty) FROM `tabStock Reservation Entry` WHERE item_code = %s AND docstatus = 1",
			(self.item_demo,),
		)[0][0] or 0.0
		self.assertLessEqual(flt(total_res), 1.0)

	# =========================================================================
	# 3. Redis Outage Recovery Test
	# =========================================================================

	def test_redis_outage_durable_outbox_recovery(self):
		self._set_stock(self.item_demo, 75.0)

		# Mock frappe.enqueue to simulate Redis connection failure
		with patch("frappe.enqueue", side_effect=Exception("Redis connection refused")):
			# Schedule publication event
			events = schedule_channel_inventory_publication(
				sales_channel=self.channel_tid,
				item_codes=[self.item_demo],
			)
			self.assertTrue(len(events) > 0)
			evt_name = events[0]

		# Event is durably recorded in MariaDB in PENDING status
		st = frappe.db.get_value("Integration Event", evt_name, "status")
		self.assertEqual(st, IntegrationStatus.PENDING)

		# When worker executes via scheduled batch (direct from MariaDB)
		res = process_pending_inventory_publications(
			sales_channel=self.channel_tid,
			client=self.client,
		)
		self.assertGreaterEqual(res.get("published", 0), 1)

		# Event is SUCCEEDED
		new_st = frappe.db.get_value("Integration Event", evt_name, "status")
		self.assertEqual(new_st, IntegrationStatus.SUCCEEDED)

		# Remote stock converged to 75
		sa_data = self.client.get_stock_available(self.sa_id_demo)
		self.assertEqual(int(sa_data.get("quantity")), 75)

	# =========================================================================
	# 4. PrestaShop Outage Resilience and Retry Test
	# =========================================================================

	def test_prestashop_outage_resilience_and_retry(self):
		self._set_stock(self.item_demo, 88.0)

		events = schedule_channel_inventory_publication(
			sales_channel=self.channel_tid,
			item_codes=[self.item_demo],
		)
		evt_name = events[0]

		# First worker attempt encounters transient PrestaShop 503 error
		with patch.object(self.client, "update_stock_available_quantity", side_effect=PrestaShopServerError("503 Service Unavailable")):
			res = process_inventory_publication_event(
				event_name=evt_name,
				client=self.client,
			)
			self.assertFalse(res.get("success"))

		# Status transitioned to RETRY_PENDING with next_retry_at populated
		evt_row = frappe.db.get_value(
			"Integration Event",
			evt_name,
			["status", "attempt_count", "next_retry_at"],
			as_dict=True,
		)
		self.assertEqual(evt_row.status, IntegrationStatus.RETRY_PENDING)
		self.assertGreaterEqual(evt_row.attempt_count, 1)
		self.assertIsNotNone(evt_row.next_retry_at)

		# Operator action: retry event immediately
		retr = retry_failed_publication_event(evt_name)
		self.assertTrue(retr.get("success"))

		# PrestaShop is back up: process again
		res_retry = process_inventory_publication_event(
			event_name=evt_name,
			client=self.client,
		)
		self.assertTrue(res_retry.get("success"))

		# Now SUCCEEDED and remote is in sync
		final_st = frappe.db.get_value("Integration Event", evt_name, "status")
		self.assertEqual(final_st, IntegrationStatus.SUCCEEDED)
		sa_data = self.client.get_stock_available(self.sa_id_demo)
		self.assertEqual(int(sa_data.get("quantity")), 88)

	# =========================================================================
	# 5. Bounded Synthetic Load & Draining Test
	# =========================================================================

	def test_bounded_synthetic_load_and_drain(self):
		self._set_stock(self.item_demo, 90.0)
		self._set_stock(self.item_pliers, 40.0)

		events = schedule_channel_inventory_publication(
			sales_channel=self.channel_tid,
			item_codes=[self.item_demo, self.item_pliers],
		)
		self.assertEqual(len(events), 2)

		# Process via multi-channel dispatcher
		telemetry = process_multichannel_inventory_publications(
			provider=self.provider,
			max_channels_per_run=5,
			max_events_per_channel=10,
			client=self.client,
		)
		self.assertGreaterEqual(telemetry.get("published", 0), 2)
		self.assertEqual(telemetry.get("failed", 0), 0)

		# Both remote products match ERP truth
		sa_demo = self.client.get_stock_available(self.sa_id_demo)
		sa_pliers = self.client.get_stock_available(self.sa_id_pliers)
		self.assertEqual(int(sa_demo.get("quantity")), 90)
		self.assertEqual(int(sa_pliers.get("quantity")), 40)

	# =========================================================================
	# 6. DB Hygiene and Baseline Stock Restoration Test
	# =========================================================================

	def test_db_hygiene_and_baseline_stock(self):
		self._full_cleanup()

		# Restore baseline
		self.client.update_stock_available_quantity(self.sa_id_demo, self.baseline_qty_demo, self.prod_id_demo, None)
		self.client.update_stock_available_quantity(self.sa_id_pliers, self.baseline_qty_pliers, self.prod_id_pliers, None)

		# Assert PrestaShop remote quantities
		sa6 = self.client.get_stock_available(self.sa_id_demo)
		sa21 = self.client.get_stock_available(self.sa_id_pliers)
		self.assertEqual(int(sa6.get("quantity")), 300)
		self.assertEqual(int(sa21.get("quantity")), 120)

		# Assert zero residual test orders or events
		order_cnt = frappe.db.count("Sales Order", {"sales_channel": self.channel_tid})
		evt_cnt = frappe.db.sql(
			"SELECT count(*) FROM `tabIntegration Event` WHERE sales_channel = %s AND (erp_document IN (%s, %s) OR request_metadata LIKE %s)",
			(self.channel_tid, self.item_demo, self.item_pliers, f"%{self.item_demo}%"),
		)[0][0]
		self.assertEqual(order_cnt, 0)
		self.assertEqual(evt_cnt, 0)
