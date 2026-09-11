# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import dataclasses
import json
import unittest
from unittest.mock import patch, MagicMock
import frappe
from frappe.utils import flt

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
	assert_safe_connector_target,
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
)
from bop_erp.orders.ingestion import (
	ingest_order_pipeline,
	process_order_ingestion_event,
	claim_event_for_processing,
	find_existing_order_mapping,
	is_order_ingestion_complete,
	compute_order_idempotency_key,
)
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.inventory.publication import (
	schedule_channel_inventory_publication,
	compute_publication_idempotency_key,
)
from bop_erp.inventory.scheduler import (
	process_multichannel_inventory_publications,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig


class TestPostcommitAndOperationalGuardLive(unittest.TestCase):
	"""
	Live Integration Test Suite for Phase 1K.3:
	Post-Commit Publication & Incomplete-Order Operational Guard.
	Executes against the local disposable PrestaShop test instance (http://prestashop-test).

	Verifies:
	1. Two-channel shared inventory publication flow (both channels triggered and updated).
	2. Crash-after-submit recovery to READY (guard blocks during incomplete, allows after recovery).
	3. Crash-after-reservation recovery to READY (guard blocks during incomplete, allows after recovery).
	4. True end-to-end PrestaShop test chain (order -> commit -> status READY -> after_commit publication -> PrestaShop API updated).
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
		cls.company_currency = frappe.get_cached_value("Company", cls.company, "default_currency") or "COP"

		# Phase 1Q.2: Test-owned Currency Exchange fixture for USD -> company_currency
		cls.owned_currency_exchange = None
		if not frappe.db.exists("Currency Exchange", {"from_currency": "USD", "to_currency": cls.company_currency, "for_selling": 1}):
			ce = frappe.get_doc({
				"doctype": "Currency Exchange",
				"date": frappe.utils.nowdate(),
				"from_currency": "USD",
				"to_currency": cls.company_currency,
				"exchange_rate": 3000.0,
				"for_buying": 1,
				"for_selling": 1,
			})
			ce.flags.ignore_permissions = True
			ce.insert(ignore_permissions=True)
			cls.owned_currency_exchange = ce.name
			frappe.db.commit()

		cls.diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)
		cls.cust_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "Government"

		cls.channel_a = "CHAN-LIVE-A"
		cls.channel_b = "CHAN-LIVE-B"
		cls.channel_tid = "TID"

		# Synthetic Warehouse
		cls.wh_a = f"WH-LIVE-PC-{cls.abbr} - {cls.abbr}"
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")
		if not frappe.db.exists("Warehouse", cls.wh_a):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"WH-LIVE-PC-{cls.abbr}",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
				"is_group": 0,
			})
			w.flags.ignore_permissions = True
			w.insert(ignore_permissions=True)

		# Items
		cls.item_a = "demo_11"  # PrestaShop product 6
		cls.product_id_a = 6
		cls.stock_available_id_a = 6
		cls.baseline_qty_a = 300

		cls.item_pliers = "SKU-TOOL-PLIERS-8IN"  # PrestaShop product 21
		cls.product_id_pliers = 21
		cls.stock_available_id_pliers = 60
		cls.baseline_qty_pliers = 120

		for ic in [cls.item_a, cls.item_pliers]:
			if not frappe.db.exists("Item", ic):
				frappe.get_doc({
					"doctype": "Item",
					"item_code": ic,
					"item_name": ic,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}).insert(ignore_permissions=True)

		# Dual clients: read_client for orders, write_client for stock_availables
		cls.read_config = PrestaShopConfig(
			sales_channel=cls.channel_tid,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_PRESTASHOP_KEY",
			verify_tls=False,
			read_enabled=True,
			write_enabled=False,
		)
		cls.read_client = PrestaShopClient(config=cls.read_config)

		cls.write_config = PrestaShopConfig(
			sales_channel=cls.channel_tid,
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_PRESTASHOP_WRITE_KEY",
			verify_tls=False,
			read_enabled=True,
			write_enabled=True,
		)
		cls.write_client = PrestaShopClient(config=cls.write_config)

		# Record and assert baseline remote stock
		cls.write_client.update_stock_available_quantity(cls.stock_available_id_a, cls.baseline_qty_a, cls.product_id_a, None)
		cls.write_client.update_stock_available_quantity(cls.stock_available_id_pliers, cls.baseline_qty_pliers, cls.product_id_pliers, None)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._full_cleanup()
		try:
			cls.write_client.update_stock_available_quantity(cls.stock_available_id_a, cls.baseline_qty_a, cls.product_id_a, None)
			cls.write_client.update_stock_available_quantity(cls.stock_available_id_pliers, cls.baseline_qty_pliers, cls.product_id_pliers, None)
		except Exception:
			pass

		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", cls.orig_stock_res)

		# Clean warehouses & reconciliation
		frappe.db.delete("Stock Ledger Entry", {"warehouse": cls.wh_a})
		frappe.db.delete("Stock Reconciliation Item", {"warehouse": cls.wh_a})
		frappe.db.delete("Stock Reconciliation", {"company": cls.company})
		frappe.db.delete("Bin", {"warehouse": cls.wh_a})
		if hasattr(cls, "wh_a") and frappe.db.exists("Warehouse", cls.wh_a):
			frappe.delete_doc("Warehouse", cls.wh_a, force=True, ignore_permissions=True)

		for ic in [cls.item_a, cls.item_pliers]:
			frappe.db.delete("Bin", {"item_code": ic})

		# Ensure persistent TID mapping for product 21 is restored
		if not frappe.db.exists("External ID Mapping", {"sales_channel": cls.channel_tid, "erp_document": cls.item_pliers, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": cls.channel_tid,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": str(cls.product_id_pliers),
				"erp_doctype": "Item",
				"erp_document": cls.item_pliers,
				"active": 1,
			}).insert(ignore_permissions=True)

		# Clean test-owned Currency Exchange fixture
		if getattr(cls, "owned_currency_exchange", None) and frappe.db.exists("Currency Exchange", cls.owned_currency_exchange):
			frappe.delete_doc("Currency Exchange", cls.owned_currency_exchange, force=True, ignore_permissions=True)
			cls.owned_currency_exchange = None

		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _full_cleanup(cls):
		test_channels = [cls.channel_a, cls.channel_b, cls.channel_tid]
		for ch in test_channels:
			# Cancel and delete Sales Orders
			sos = frappe.get_all("Sales Order", filters={"sales_channel": ch}, fields=["name", "docstatus"])
			for so in sos:
				if so.docstatus == 1:
					try:
						doc = frappe.get_doc("Sales Order", so.name)
						doc.cancel()
					except Exception:
						pass
				frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)

			# Cancel and delete SREs
			sres = frappe.get_all(
				"Stock Reservation Entry",
				filters={"item_code": ["in", [cls.item_a, cls.item_pliers]]},
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

			frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [cls.item_a, cls.item_pliers]]})
			frappe.db.sql("DELETE FROM `tabIntegration Event` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s", (ch,))
			if ch in [cls.channel_a, cls.channel_b]:
				frappe.db.sql("DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s", (ch,))
				frappe.db.sql("DELETE FROM `tabChannel Inventory Source` WHERE sales_channel = %s", (ch,))
				frappe.db.sql("DELETE FROM `tabPrestaShop Connector` WHERE sales_channel = %s", (ch,))
				frappe.db.sql("DELETE FROM `tabSales Channel` WHERE name = %s", (ch,))

		# Clean TID connector/sources added for tests and test order mappings
		frappe.db.sql("DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s AND external_entity_type = 'ORDER' AND external_id = '8'", (cls.channel_tid,))
		frappe.db.sql("DELETE FROM `tabChannel Inventory Source` WHERE sales_channel = %s AND warehouse = %s", (cls.channel_tid, cls.wh_a))
		frappe.db.sql("DELETE FROM `tabPrestaShop Connector` WHERE sales_channel = %s", (cls.channel_tid,))

		# Clean test customers and addresses
		for c in frappe.get_all("Customer", filters={"name": ["in", ["Brenda Vance"]]}, pluck="name") + frappe.get_all("Customer", filters={"name": ["like", "%Live Guard%"]}, pluck="name"):
			frappe.db.delete("External ID Mapping", {"erp_document": c})
			for addr in frappe.get_all("Dynamic Link", filters={"link_doctype": "Customer", "link_name": c, "parenttype": "Address"}, pluck="parent"):
				frappe.db.delete("External ID Mapping", {"erp_document": addr})
				frappe.delete_doc("Address", addr, force=True, ignore_permissions=True)
			frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)

		for a in frappe.get_all("Address", filters={"name": ["in", ["Alex Mercer-Shipping"]]}, pluck="name") + frappe.get_all("Address", filters={"address_title": ["like", "%Live Guard%"]}, pluck="name"):
			frappe.db.delete("External ID Mapping", {"erp_document": a})
			frappe.delete_doc("Address", a, force=True, ignore_permissions=True)

		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self._full_cleanup()
		if not frappe.db.exists("Warehouse", self.wh_a):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"WH-LIVE-PC-{self.abbr}",
				"company": self.company,
				"parent_warehouse": self.wh_parent,
				"is_group": 0,
			})
			w.flags.ignore_permissions = True
			w.insert(ignore_permissions=True)

		self._setup_channels()
		self._set_physical_stock(self.item_a, self.wh_a, 50.0)
		self._set_physical_stock(self.item_pliers, self.wh_a, 120.0)
		frappe.db.commit()

	def tearDown(self):
		self._full_cleanup()
		super().tearDown()

	def _setup_channels(self):
		for ch in [self.channel_a, self.channel_b]:
			if not frappe.db.exists("Sales Channel", ch):
				frappe.get_doc({
					"doctype": "Sales Channel",
					"channel_id": ch,
					"channel_name": ch,
					"channel_type": "PRESTASHOP",
					"company": self.company,
					"active": 1,
				}).insert(ignore_permissions=True)

			if not frappe.db.exists("PrestaShop Connector", {"sales_channel": ch}):
				pc = frappe.get_doc({
					"doctype": "PrestaShop Connector",
					"sales_channel": ch,
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

			if not frappe.db.exists("Channel Inventory Source", {"sales_channel": ch, "warehouse": self.wh_a}):
				frappe.get_doc({
					"doctype": "Channel Inventory Source",
					"sales_channel": ch,
					"warehouse": self.wh_a,
					"company": self.company,
					"enabled": 1,
					"allow_sellable_stock": 1,
					"allow_fulfillment": 1,
				}).insert(ignore_permissions=True)

			# Map item_a to product 6 in both channels
			if not frappe.db.exists("External ID Mapping", {"sales_channel": ch, "erp_document": self.item_a, "active": 1}):
				frappe.get_doc({
					"doctype": "External ID Mapping",
					"sales_channel": ch,
					"provider": IntegrationProvider.PRESTASHOP,
					"external_entity_type": ExternalEntityType.PRODUCT,
					"external_id": str(self.product_id_a),
					"erp_doctype": "Item",
					"erp_document": self.item_a,
					"active": 1,
				}).insert(ignore_permissions=True)

	def _setup_tid_channel(self):
		"""Configures TID channel for PrestaShop test."""
		if not frappe.db.exists("PrestaShop Connector", {"sales_channel": self.channel_tid}):
			pc = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": self.channel_tid,
				"environment": "DEVELOPMENT",
				"base_url": "http://prestashop-test",
				"credential_reference": "TEST_PRESTASHOP_KEY",
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 1,
				"eligible_order_states": "2,3,4,Payment accepted",
			})
			pc.flags.ignore_validate = True
			pc.insert(ignore_permissions=True)

		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": self.channel_tid, "warehouse": self.wh_a}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": self.channel_tid,
				"warehouse": self.wh_a,
				"company": self.company,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("External ID Mapping", {"sales_channel": self.channel_tid, "erp_document": self.item_pliers, "active": 1}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": self.channel_tid,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": str(self.product_id_pliers),
				"erp_doctype": "Item",
				"erp_document": self.item_pliers,
				"active": 1,
			}).insert(ignore_permissions=True)

		frappe.db.commit()

	def _set_physical_stock(self, item_code, warehouse, qty):
		try:
			reco = frappe.get_doc({
				"doctype": "Stock Reconciliation",
				"company": self.company,
				"purpose": "Opening Stock",
				"expense_account": self.diff_account,
				"items": [
					{
						"item_code": item_code,
						"warehouse": warehouse,
						"qty": qty,
						"valuation_rate": 10.0,
					}
				],
			})
			reco.insert(ignore_permissions=True)
			reco.submit()
			frappe.db.commit()
			return reco.name
		except Exception:
			return None

	# ==================================================
	# 1. TWO-CHANNEL SHARED INVENTORY PUBLICATION FLOW
	# ==================================================
	def test_01_two_channel_shared_inventory_publication_flow(self):
		"""
		Verifies that an order on Channel A reduces inventory in the shared warehouse,
		and post-commit hooks schedule publication for BOTH Channel A and Channel B.
		Executing the publication worker successfully updates remote PrestaShop stock.
		"""
		atp_a_before = get_channel_atp(self.item_a, self.channel_a).aggregate_atp_qty
		atp_b_before = get_channel_a_b = get_channel_atp(self.item_a, self.channel_b).aggregate_atp_qty
		self.assertEqual(atp_a_before, 50.0)
		self.assertEqual(atp_b_before, 50.0)

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="LIVE-ORD-01",
			external_reference="REF-LIVE-01",
			order_state_id="2",
			currency=self.company_currency,
			customer=ExternalCustomer(
				external_customer_id="CUST-LIVE-01",
				first_name="Live Guard",
				last_name="Customer",
				email="liveguard@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-LIVE-01",
				address_type="Shipping",
				first_name="Live Guard",
				last_name="Customer",
				address1="123 Test Street",
				city="Miami",
				postcode="33101",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=str(self.product_id_a),
					sku=self.item_a,
					quantity=5.0,
					unit_price_ex_tax=10.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=50.0, total_paid=50.0),
		)

		res = ingest_order_pipeline(ext_order)
		self.assertTrue(res["success"])
		so_name = res["sales_order"]

		# Explicit durable commit to trigger after_commit callbacks
		frappe.db.commit()

		# 1. Inbound commit verification
		so = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so.docstatus, 1)
		self.assertEqual(so.integration_status, IntegrationReadinessStatus.READY)
		self.assertEqual(so.integration_provider, IntegrationProvider.PRESTASHOP)

		# SRE allocated
		sre_count = frappe.db.count("Stock Reservation Entry", {
			"voucher_type": "Sales Order",
			"voucher_no": so_name,
			"docstatus": 1,
		})
		self.assertEqual(sre_count, 1)

		# 2. Verify outbound publication events were scheduled for BOTH Channel A and Channel B
		events_a = frappe.get_all("Integration Event", filters={
			"sales_channel": self.channel_a,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_a,
		})
		self.assertGreaterEqual(len(events_a), 1, "Channel A must have scheduled outbound publication")

		events_b = frappe.get_all("Integration Event", filters={
			"sales_channel": self.channel_b,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_a,
		})
		self.assertGreaterEqual(len(events_b), 1, "Channel B (shared warehouse) must have scheduled outbound publication")

		# 3. Process publications via worker using write client
		pub_res = process_multichannel_inventory_publications(client=self.write_client)
		self.assertGreaterEqual(pub_res["published"] + pub_res["no_op"], 2)

		# Both events must have finished with Succeeded
		ev_a = frappe.get_doc("Integration Event", events_a[0].name)
		ev_b = frappe.get_doc("Integration Event", events_b[0].name)
		self.assertEqual(ev_a.status, IntegrationStatus.SUCCEEDED)
		self.assertEqual(ev_b.status, IntegrationStatus.SUCCEEDED)

		# 4. Remote PrestaShop stock reflects the 5-unit reduction (50 -> 45)
		sa = self.write_client.get_stock_available(self.stock_available_id_a)
		self.assertEqual(int(sa.get("quantity")), 45)

		# 5. Local ATP reflects 45 on both channels
		self.assertEqual(get_channel_atp(self.item_a, self.channel_a).aggregate_atp_qty, 45.0)
		self.assertEqual(get_channel_atp(self.item_a, self.channel_b).aggregate_atp_qty, 45.0)

		# Restore remote stock back to baseline 300
		self.write_client.update_stock_available_quantity(self.stock_available_id_a, self.baseline_qty_a, self.product_id_a, None)

	# ==================================================
	# 2. CRASH-AFTER-SUBMIT RECOVERY TO READY
	# ==================================================
	def test_02_crash_after_submit_recovery_to_ready(self):
		"""
		Simulates crash immediately after Sales Order submit, before SRE reservation.
		Verifies:
		1. Order status is RESERVATION_PENDING.
		2. Operational guard BLOCKS fulfillment (Pick List / Delivery Note).
		3. Replaying the event recovers missing SREs and advances status to READY.
		4. Operational guard now PERMITS fulfillment.
		"""
		cust_name = frappe.db.get_value("Customer", {"name": ["like", "%Live Guard%"]}, "name")
		if not cust_name:
			c = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": "Live Guard CrashSubmit",
				"customer_group": self.cust_group,
				"customer_type": "Individual",
			})
			c.insert(ignore_permissions=True)
			cust_name = c.name

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="LIVE-CRASH-SUBMIT-01",
			external_reference="REF-CRASH-SUBMIT-01",
			order_state_id="2",
			currency=self.company_currency,
			customer=ExternalCustomer(
				external_customer_id="CUST-LIVE-02",
				first_name="Live Guard",
				last_name="CrashSubmit",
				email="crashsubmit@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-LIVE-02",
				first_name="Live Guard",
				last_name="CrashSubmit",
				address1="123 Test Street",
				city="Miami",
				postcode="33101",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=str(self.product_id_a),
					sku=self.item_a,
					quantity=3.0,
					unit_price_ex_tax=10.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=30.0, total_paid=30.0),
		)

		# Create an order in RESERVATION_PENDING (simulating crash right after submit)
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"company": self.company,
			"customer": cust_name,
			"transaction_date": frappe.utils.nowdate(),
			"delivery_date": frappe.utils.nowdate(),
			"sales_channel": self.channel_a,
			"transaction_origin": TransactionOrigin.WEB,
			"integration_status": IntegrationReadinessStatus.RESERVATION_PENDING,
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"currency": "USD",
			"conversion_rate": 1.0,
			"price_list_currency": "USD",
			"plc_conversion_rate": 1.0,
			"items": [
				{
					"item_code": self.item_a,
					"item_name": self.item_a,
					"uom": "Nos",
					"conversion_factor": 1.0,
					"warehouse": self.wh_a,
					"qty": 3.0,
					"rate": 10.0,
					"delivery_date": frappe.utils.nowdate(),
				}
			],
		})
		so.flags.ignore_permissions = True
		so.flags.ignore_validate = True
		so.insert(ignore_permissions=True)
		so.submit()

		# Mapping created pointing to this SO
		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_a,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.ORDER,
			"external_id": "LIVE-CRASH-SUBMIT-01",
			"erp_doctype": "Sales Order",
			"erp_document": so.name,
			"active": 1,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Verify incomplete state
		self.assertFalse(is_order_ingestion_complete(so.name)[0])
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "integration_status"), IntegrationReadinessStatus.RESERVATION_PENDING)

		# Operational guard MUST BLOCK
		with self.assertRaises(OperationalGuardError) as ctx:
			assert_sales_order_ready_for_fulfillment(so.name)
		self.assertIn("RESERVATION_PENDING", str(ctx.exception))

		# Now create inbound event for replay
		inbound_event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"direction": IntegrationDirection.INBOUND,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"external_id": "LIVE-CRASH-SUBMIT-01",
			"idempotency_key": compute_order_idempotency_key(IntegrationProvider.PRESTASHOP, self.channel_a, "LIVE-CRASH-SUBMIT-01"),
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({"normalized_order": dataclasses.asdict(ext_order)}),
			"max_attempts": 3,
		})
		inbound_event.insert(ignore_permissions=True)
		frappe.db.commit()

		# Process the recovery
		rec_res = process_order_ingestion_event(inbound_event.name, worker_id="worker-live-02")
		self.assertTrue(rec_res["success"])
		self.assertTrue(rec_res.get("is_replay"))

		# Verify recovery results
		self.assertTrue(is_order_ingestion_complete(so.name)[0])
		so.reload()
		self.assertEqual(so.integration_status, IntegrationReadinessStatus.READY)
		self.assertEqual(so.latest_integration_event, inbound_event.name)

		# Operational guard MUST NOW PERMIT
		assert_sales_order_ready_for_fulfillment(so.name)

	# ==================================================
	# 3. CRASH-AFTER-RESERVATION RECOVERY TO READY
	# ==================================================
	def test_03_crash_after_reservation_recovery_to_ready(self):
		"""
		Simulates crash after SRE reservation is created but before final commit/status transition to READY.
		Verifies:
		1. Existing reservations are detected and preserved.
		2. Status transitions to READY.
		3. Operational guard allows fulfillment.
		"""
		cust = frappe.db.get_value("Customer", {"name": ["like", "%Live Guard%"]}, "name")
		if not cust:
			c = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": "Live Guard ResRecovery",
				"customer_group": self.cust_group,
				"customer_type": "Individual",
			})
			c.insert(ignore_permissions=True)
			cust = c.name

		so = frappe.get_doc({
			"doctype": "Sales Order",
			"company": self.company,
			"customer": cust,
			"transaction_date": frappe.utils.nowdate(),
			"delivery_date": frappe.utils.nowdate(),
			"sales_channel": self.channel_a,
			"transaction_origin": TransactionOrigin.WEB,
			"integration_status": IntegrationReadinessStatus.RESERVATION_PENDING,
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"currency": "USD",
			"conversion_rate": 1.0,
			"price_list_currency": "USD",
			"plc_conversion_rate": 1.0,
			"items": [
				{
					"item_code": self.item_a,
					"item_name": self.item_a,
					"uom": "Nos",
					"conversion_factor": 1.0,
					"warehouse": self.wh_a,
					"qty": 2.0,
					"rate": 10.0,
					"delivery_date": frappe.utils.nowdate(),
				}
			],
		})
		so.flags.ignore_permissions = True
		so.flags.ignore_validate = True
		so.insert(ignore_permissions=True)
		so.submit()

		# Create SRE
		sre = frappe.get_doc({
			"doctype": "Stock Reservation Entry",
			"company": self.company,
			"voucher_type": "Sales Order",
			"voucher_no": so.name,
			"voucher_detail_no": so.items[0].name,
			"item_code": self.item_a,
			"warehouse": self.wh_a,
			"available_qty": 50.0,
			"voucher_qty": 2.0,
			"reserved_qty": 2.0,
			"stock_uom": "Nos",
		})
		sre.flags.ignore_permissions = True
		sre.insert(ignore_permissions=True)
		sre.submit()

		# Mapping created pointing to this SO
		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_a,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.ORDER,
			"external_id": "LIVE-CRASH-RES-01",
			"erp_doctype": "Sales Order",
			"erp_document": so.name,
			"active": 1,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Incomplete status even though SRE exists
		self.assertEqual(frappe.db.get_value("Sales Order", so.name, "integration_status"), IntegrationReadinessStatus.RESERVATION_PENDING)
		with self.assertRaises(OperationalGuardError):
			assert_sales_order_ready_for_fulfillment(so.name)

		# Now replay
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="LIVE-CRASH-RES-01",
			external_reference="REF-CRASH-RES-01",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="CUST-1"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id=str(self.product_id_a), quantity=2.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)
		inbound_event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"direction": IntegrationDirection.INBOUND,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"external_id": "LIVE-CRASH-RES-01",
			"idempotency_key": compute_order_idempotency_key(IntegrationProvider.PRESTASHOP, self.channel_a, "LIVE-CRASH-RES-01"),
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({"normalized_order": dataclasses.asdict(ext_order)}),
			"max_attempts": 3,
		})
		inbound_event.insert(ignore_permissions=True)
		frappe.db.commit()

		rec_res = process_order_ingestion_event(inbound_event.name, worker_id="worker-live-03")
		self.assertTrue(rec_res["success"])

		so.reload()
		self.assertEqual(so.integration_status, IntegrationReadinessStatus.READY)
		self.assertEqual(so.latest_integration_event, inbound_event.name)
		assert_sales_order_ready_for_fulfillment(so.name)

	# ==================================================
	# 4. TRUE END-TO-END PRESTASHOP TEST CHAIN
	# ==================================================
	def test_04_true_end_to_end_prestashop_test_chain(self):
		"""
		Verifies the full end-to-end chain against local PrestaShop TEST instance:
		- Uses Order 8 (state 2, product 21 / SKU-TOOL-PLIERS-8IN).
		- Inbound order event processed -> fresh GET -> SO created & submitted -> SRE allocated (qty=1).
		- Durable commit occurs -> integration_status becomes READY.
		- Post-commit hook schedules outbound publication for channel TID.
		- Outbound worker process_multichannel_inventory_publications executes.
		- Live PrestaShop API confirms remote stock updated from 120 to 119!
		- Zero Sales Invoices, Zero Payment Entries.
		- Restores PrestaShop stock back to 120.
		"""
		self._setup_tid_channel()

		initial_remote_sa = self.write_client.get_stock_available(self.stock_available_id_pliers)
		self.assertEqual(int(initial_remote_sa.get("quantity")), 120)

		initial_invoices = frappe.db.count("Sales Invoice")
		initial_payments = frappe.db.count("Payment Entry")

		# Create inbound event for Order 8 (live order in PrestaShop for product 21)
		inbound_event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_tid,
			"direction": IntegrationDirection.INBOUND,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.INGEST_ORDER,
			"external_id": "8",
			"idempotency_key": compute_order_idempotency_key(IntegrationProvider.PRESTASHOP, self.channel_tid, "8"),
			"status": IntegrationStatus.PENDING,
			"max_attempts": 3,
		})
		inbound_event.insert(ignore_permissions=True)
		frappe.db.commit()

		# Ingest through process_order_ingestion_event with read_client
		ingest_res = process_order_ingestion_event(inbound_event.name, worker_id="worker-live-04", client=self.read_client)
		self.assertTrue(ingest_res["success"], f"Ingestion failed: {ingest_res}")
		so_name = ingest_res["sales_order"]

		# Verify Sales Order readiness
		so = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so.docstatus, 1)
		self.assertEqual(so.integration_status, IntegrationReadinessStatus.READY)
		self.assertEqual(so.latest_integration_event, inbound_event.name)
		self.assertEqual(so.integration_provider, IntegrationProvider.PRESTASHOP)

		# Operational guard passes
		assert_sales_order_ready_for_fulfillment(so_name)

		# Verify outbound publication event scheduled post-commit
		pub_events = frappe.get_all("Integration Event", filters={
			"sales_channel": self.channel_tid,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_pliers,
		})
		self.assertGreaterEqual(len(pub_events), 1, "Outbound publication event must be scheduled post-commit")

		# Execute publication worker using write_client
		pub_res = process_multichannel_inventory_publications(client=self.write_client)
		self.assertGreaterEqual(pub_res["published"], 1)

		# Query live PrestaShop API to verify stock updated to 119 (120 - 1 = 119)!
		updated_remote_sa = self.write_client.get_stock_available(self.stock_available_id_pliers)
		self.assertEqual(int(updated_remote_sa.get("quantity")), 119, "PrestaShop TEST stock must be updated to 119 (120 - 1)")

		# Verify ZERO sales invoices and ZERO payment entries created
		self.assertEqual(frappe.db.count("Sales Invoice"), initial_invoices)
		self.assertEqual(frappe.db.count("Payment Entry"), initial_payments)

		# Restore PrestaShop stock back to baseline 120
		restore_res = self.write_client.update_stock_available_quantity(self.stock_available_id_pliers, 120, self.product_id_pliers, None)
		restored_remote_sa = self.write_client.get_stock_available(self.stock_available_id_pliers)
		self.assertEqual(int(restored_remote_sa.get("quantity")), 120, "PrestaShop TEST stock must be restored to 120")

		# Clean up created customer, address and mappings from Order 8
		if so.customer and so.customer != "Test ATP Cust":
			cust = so.customer
			frappe.db.delete("External ID Mapping", {"erp_document": cust})
			if so.customer_address:
				frappe.db.delete("External ID Mapping", {"erp_document": so.customer_address})
				frappe.delete_doc("Address", so.customer_address, force=True, ignore_permissions=True)
			if so.shipping_address_name and so.shipping_address_name != so.customer_address:
				frappe.db.delete("External ID Mapping", {"erp_document": so.shipping_address_name})
				frappe.delete_doc("Address", so.shipping_address_name, force=True, ignore_permissions=True)
			frappe.delete_doc("Customer", cust, force=True, ignore_permissions=True)
			frappe.db.commit()

	# ==================================================
	# 5. TRANSACTIONAL OUTBOX LIVE CRASH SURVIVAL & RECOVERY
	# ==================================================
	def test_05_transactional_outbox_survives_process_crash_and_publishes_to_prestashop(self):
		"""
		Phase 1K.4 Core Acceptance Test:
		Simulates a process crash / worker loss after SQL commit by suppressing
		post-commit dispatcher wake.
		Proves:
		1. Outbound publication event was persisted in MariaDB in the SAME transaction as SO/SRE.
		2. Even without any wake callback or dispatcher running at commit time, the outbox event
		   survives durably in MariaDB with status = PENDING.
		3. An independent scheduled publication worker claims the event, calculates ATP, and
		   publishes the new stock level to the live PrestaShop TEST instance.
		4. PrestaShop stock level accurately decrements from 120 to 119.
		5. Restores PrestaShop stock cleanly back to 120.
		"""
		self._setup_tid_channel()

		initial_remote_sa = self.write_client.get_stock_available(self.stock_available_id_pliers)
		self.assertEqual(int(initial_remote_sa.get("quantity")), 120)

		initial_invoices = frappe.db.count("Sales Invoice")
		initial_payments = frappe.db.count("Payment Entry")

		# Create simulated inbound order for Order 8
		inbound_event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_tid,
			"direction": IntegrationDirection.INBOUND,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.INGEST_ORDER,
			"external_id": "8",
			"idempotency_key": compute_order_idempotency_key(IntegrationProvider.PRESTASHOP, self.channel_tid, "8-CRASH-TEST"),
			"status": IntegrationStatus.PENDING,
			"max_attempts": 3,
		})
		inbound_event.insert(ignore_permissions=True)
		frappe.db.commit()

		# Explicitly suppress post-commit wake to simulate total loss of in-memory callbacks / process crash
		frappe.flags.suppress_outbox_wake = True
		try:
			ingest_res = process_order_ingestion_event(inbound_event.name, worker_id="worker-crash-test", client=self.read_client)
			self.assertTrue(ingest_res["success"], f"Ingestion failed: {ingest_res}")
			so_name = ingest_res["sales_order"]
		finally:
			frappe.flags.suppress_outbox_wake = False

		# Confirm SO is committed and READY
		so = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so.docstatus, 1)
		self.assertEqual(so.integration_status, IntegrationReadinessStatus.READY)

		# Verify that outbox event EXISTS in MariaDB with status = PENDING
		# (Proving it was persisted transactionally before commit, not in RAM callback)
		outbox_events = frappe.get_all("Integration Event", filters={
			"sales_channel": self.channel_tid,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_pliers,
			"status": IntegrationStatus.PENDING,
		})
		self.assertGreaterEqual(len(outbox_events), 1, "Transactional outbox event must exist in MariaDB as PENDING")
		outbox_ev_name = outbox_events[0].name

		# Simulate recovery worker picking up the dormant outbox event
		pub_res = process_multichannel_inventory_publications(client=self.write_client)
		self.assertGreaterEqual(pub_res["published"], 1)

		# Verify outbox event is now SUCCEEDED in MariaDB
		outbox_ev = frappe.get_doc("Integration Event", outbox_ev_name)
		self.assertEqual(outbox_ev.status, IntegrationStatus.SUCCEEDED)

		# Verify PrestaShop TEST API shows updated stock (119)
		updated_remote_sa = self.write_client.get_stock_available(self.stock_available_id_pliers)
		self.assertEqual(int(updated_remote_sa.get("quantity")), 119, "PrestaShop TEST stock must be updated to 119")

		# Verify ZERO sales invoices and ZERO payment entries created
		self.assertEqual(frappe.db.count("Sales Invoice"), initial_invoices)
		self.assertEqual(frappe.db.count("Payment Entry"), initial_payments)

		# Clean up: restore stock back to 120
		restore_res = self.write_client.update_stock_available_quantity(self.stock_available_id_pliers, 120, self.product_id_pliers, None)
		restored_remote_sa = self.write_client.get_stock_available(self.stock_available_id_pliers)
		self.assertEqual(int(restored_remote_sa.get("quantity")), 120, "PrestaShop TEST stock must be restored to 120")

		# Clean up created customer, address and mappings from Order 8
		if so.customer and so.customer != "Test ATP Cust":
			cust = so.customer
			frappe.db.delete("External ID Mapping", {"erp_document": cust})
			if so.customer_address:
				frappe.db.delete("External ID Mapping", {"erp_document": so.customer_address})
				frappe.delete_doc("Address", so.customer_address, force=True, ignore_permissions=True)
			if so.shipping_address_name and so.shipping_address_name != so.customer_address:
				frappe.db.delete("External ID Mapping", {"erp_document": so.shipping_address_name})
				frappe.delete_doc("Address", so.shipping_address_name, force=True, ignore_permissions=True)
			frappe.delete_doc("Customer", cust, force=True, ignore_permissions=True)
			frappe.db.commit()

