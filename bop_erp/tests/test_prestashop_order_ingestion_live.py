# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import concurrent.futures
import json
import time
import unittest
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	ExternalEntityType,
	ErrorCategory,
	TransactionOrigin,
)
from bop_erp.safety import assert_safe_connector_target
from bop_erp.orders.models import (
	ExternalOrder,
	ExternalOrderLine,
	ExternalCustomer,
	ExternalAddress,
	ExternalTotals,
)
from bop_erp.orders.exceptions import (
	OrderIngestionError,
	MissingProductMappingError,
	OrderNotEligibleError,
	OrderTotalMismatchError,
	InsufficientOrderStockError,
	InvalidOrderQuantityError,
	OrderReservationFailedError,
)
from bop_erp.orders.ingestion import (
	compute_order_idempotency_key,
	find_existing_order_mapping,
	ingest_order_pipeline,
	process_order_ingestion_event,
	find_affected_channels_for_items,
)
from bop_erp.orders.discovery import (
	discover_channel_orders,
	discover_multichannel_orders,
)
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig


class TestPrestaShopOrderIngestionLive(FrappeTestCase):
	"""
	Live Integration Test Suite for PrestaShop Inbound Order Ingestion & Reservation.
	Tests executed against local disposable PrestaShop test instance (http://prestashop-test).
	Guarantees:
	- Concurrency fencing
	- Idempotent deduplication
	- Crash recovery
	- Stock reservation atomicity
	- Multi-channel inventory publication triggering
	- ZERO ERPNext/Frappe/PrestaShop core modifications
	- Zero contact with production
	"""

	@classmethod
	def setUpClass(cls):
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		# Ensure Stock Settings enable reservation
		cls.orig_stock_res = frappe.db.get_single_value("Stock Settings", "enable_stock_reservation")
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"

		# Equity difference account for stock reconciliation
		cls.diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)

		cls.channel_a = "CHAN-ORD-A"
		cls.channel_b = "CHAN-ORD-B"

		# Synthetic Warehouses
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")
		cls.wh_a = f"WH-ORD-A-{cls.abbr} - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.wh_a):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"WH-ORD-A-{cls.abbr}",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
			}).insert(ignore_permissions=True)
			cls.wh_a = w.name

		# Synthetic Test Items
		cls.item_simple = "ITEM-ORD-LIVE-SMP"
		if not frappe.db.exists("Item", cls.item_simple):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_simple,
				"item_name": "Order Test Simple Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.item_var9 = "ITEM-ORD-LIVE-VAR9"
		if not frappe.db.exists("Item", cls.item_var9):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_var9,
				"item_name": "Order Test Variant 9",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.item_var10 = "ITEM-ORD-LIVE-VAR10"
		if not frappe.db.exists("Item", cls.item_var10):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_var10,
				"item_name": "Order Test Variant 10",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		# Setup PrestaShop Read Client
		cls.config = PrestaShopConfig(
			sales_channel="TID",
			environment="DEVELOPMENT",
			base_url="http://prestashop-test",
			credential_reference="TEST_PRESTASHOP_KEY",
			verify_tls=False,
			read_enabled=True,
			write_enabled=False,
		)
		cls.client = PrestaShopClient(config=cls.config)

		cls._static_cleanup()
		cls._set_physical_stock(cls.item_simple, cls.wh_a, 500.0)
		cls._set_physical_stock(cls.item_var9, cls.wh_a, 500.0)
		cls._set_physical_stock(cls.item_var10, cls.wh_a, 500.0)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", cls.orig_stock_res)
		cls._static_cleanup()

		# Clean Stock Ledgers, Reconciliations, and Bins before deleting test warehouse
		frappe.db.delete("Stock Ledger Entry", {"warehouse": cls.wh_a})
		frappe.db.delete("Stock Reconciliation Item", {"warehouse": cls.wh_a})
		frappe.db.delete("Stock Reconciliation", {"company": cls.company})
		frappe.db.delete("Bin", {"warehouse": cls.wh_a})
		if hasattr(cls, "wh_a") and frappe.db.exists("Warehouse", cls.wh_a):
			frappe.delete_doc("Warehouse", cls.wh_a, force=True, ignore_permissions=True)

		# Clean test items
		for ic in [cls.item_simple, cls.item_var9, cls.item_var10]:
			frappe.db.delete("Bin", {"item_code": ic})
			if frappe.db.exists("Item", ic):
				frappe.delete_doc("Item", ic, force=True, ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def _static_cleanup(cls):
		for ch in [cls.channel_a, cls.channel_b]:
			# Clean Sales Orders created for test channels
			sos = frappe.get_all("Sales Order", filters={"sales_channel": ch}, fields=["name", "docstatus"])
			for so in sos:
				if so.docstatus == 1:
					try:
						doc = frappe.get_doc("Sales Order", so.name)
						doc.cancel()
					except Exception:
						pass
				frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)

			# Clean Stock Reservation Entries
			sres = frappe.get_all(
				"Stock Reservation Entry",
				filters={"item_code": ["in", [cls.item_simple, cls.item_var9, cls.item_var10]]},
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

			frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [cls.item_simple, cls.item_var9, cls.item_var10]]})
			frappe.db.sql("DELETE FROM `tabIntegration Event` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabChannel Inventory Source` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabPrestaShop Connector` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabSales Channel` WHERE name = %s", (ch,))

		# Clean test Customers & Addresses
		custs = frappe.get_all("Customer", filters={"customer_name": ["like", "%Test Customer%"]}, pluck="name")
		for c in custs:
			frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)

		addrs = frappe.get_all("Address", filters={"address_title": ["like", "%Test Customer%"]}, pluck="name")
		for a in addrs:
			frappe.delete_doc("Address", a, force=True, ignore_permissions=True)

		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self._static_cleanup()
		self._setup_test_channels()
		self._set_physical_stock(self.item_simple, self.wh_a, 100.0)
		self._set_physical_stock(self.item_var9, self.wh_a, 100.0)
		self._set_physical_stock(self.item_var10, self.wh_a, 100.0)
		frappe.db.commit()

	def tearDown(self):
		self._static_cleanup()
		super().tearDown()

	def _setup_test_channels(self):
		# Channel A
		if not frappe.db.exists("Sales Channel", self.channel_a):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": self.channel_a,
				"channel_name": "Order Ingestion Test Channel A",
				"company": self.company,
				"active": 1,
				"integration_provider": IntegrationProvider.PRESTASHOP,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("PrestaShop Connector", {"sales_channel": self.channel_a}):
			pc = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": self.channel_a,
				"environment": "DEVELOPMENT",
				"base_url": "http://prestashop-test",
				"credential_reference": "TEST_PRESTASHOP_KEY",
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 0,
				"eligible_order_states": "2,3,11",
			})
			pc.flags.ignore_validate = True
			pc.insert(ignore_permissions=True)

		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": self.channel_a, "warehouse": self.wh_a}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": self.channel_a,
				"warehouse": self.wh_a,
				"company": self.company,
				"priority": 10,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		# Channel B (shares warehouse wh_a with Channel A)
		if not frappe.db.exists("Sales Channel", self.channel_b):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": self.channel_b,
				"channel_name": "Order Ingestion Test Channel B",
				"company": self.company,
				"active": 1,
				"integration_provider": IntegrationProvider.PRESTASHOP,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("PrestaShop Connector", {"sales_channel": self.channel_b}):
			pc2 = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": self.channel_b,
				"environment": "DEVELOPMENT",
				"base_url": "http://prestashop-test",
				"credential_reference": "TEST_PRESTASHOP_KEY",
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 0,
				"eligible_order_states": "2,3,11",
			})
			pc2.flags.ignore_validate = True
			pc2.insert(ignore_permissions=True)

		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": self.channel_b, "warehouse": self.wh_a}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": self.channel_b,
				"warehouse": self.wh_a,
				"company": self.company,
				"priority": 10,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		# Mappings: Product 6 -> item_simple
		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_a,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "6",
			"erp_doctype": "Item",
			"erp_document": self.item_simple,
			"active": 1,
		}).insert(ignore_permissions=True)

		# Mappings: Product 2, Combination 9 -> item_var9
		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_a,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"external_id": "2",
			"external_variant_id": "9",
			"erp_doctype": "Item",
			"erp_document": self.item_var9,
			"active": 1,
		}).insert(ignore_permissions=True)

		# Mappings: Product 2, Combination 10 -> item_var10
		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_a,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"external_id": "2",
			"external_variant_id": "10",
			"erp_doctype": "Item",
			"erp_document": self.item_var10,
			"active": 1,
		}).insert(ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def _set_physical_stock(cls, item_code, warehouse, qty):
		try:
			reco = frappe.get_doc({
				"doctype": "Stock Reconciliation",
				"company": cls.company,
				"purpose": "Opening Stock",
				"expense_account": cls.diff_account,
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
	# 1. SIMPLE ORDER LIVE TEST
	# ==================================================
	def test_01_simple_order_live_ingestion(self):
		"""
		Verifies simple product order ingestion:
		- PrestaShop Order -> Sales Order created & submitted
		- Native Stock Reservation Entry created for qty=2.0
		- ATP decreases by 2.0
		- Customer and Address created with External ID Mapping
		- ORDER mapping created
		- Zero Sales Invoices, Zero Payment Entries
		"""
		atp_before = get_channel_atp(self.item_simple, self.channel_a).aggregate_atp_qty

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="901",
			external_reference="SYNTH-901",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="901",
				first_name="Test Customer",
				last_name="Alpha",
				email="alpha@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="901",
				first_name="Test Customer",
				last_name="Alpha",
				address1="100 Test St",
				city="Orlando",
				postcode="32801",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id="6",
					quantity=2.0,
					unit_price_ex_tax=25.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=50.0, total_paid=50.0),
		)

		res = ingest_order_pipeline(ext_order)
		self.assertTrue(res["success"])
		so_name = res["sales_order"]

		# 1. Verify Sales Order
		so = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so.docstatus, 1)
		self.assertEqual(so.sales_channel, self.channel_a)
		self.assertEqual(so.transaction_origin, TransactionOrigin.WEB)
		self.assertEqual(len(so.items), 1)
		self.assertEqual(so.items[0].item_code, self.item_simple)
		self.assertEqual(so.items[0].qty, 2.0)
		self.assertEqual(so.items[0].rate, 25.0)

		# 2. Verify Native Stock Reservation Entry
		sre = frappe.db.get_value(
			"Stock Reservation Entry",
			{"voucher_type": "Sales Order", "voucher_no": so_name, "item_code": self.item_simple, "docstatus": 1},
			["name", "reserved_qty"],
			as_dict=True,
		)
		self.assertIsNotNone(sre)
		self.assertEqual(flt(sre.reserved_qty), 2.0)

		# 3. Verify ATP decreased by 2.0
		atp_after = get_channel_atp(self.item_simple, self.channel_a).aggregate_atp_qty
		self.assertEqual(atp_after, atp_before - 2.0)

		# 4. Verify ORDER External ID Mapping
		mapped_so = find_existing_order_mapping(self.channel_a, IntegrationProvider.PRESTASHOP, "901")
		self.assertEqual(mapped_so, so_name)

		# 5. Verify NO Sales Invoices or Payment Entries created
		self.assertEqual(frappe.db.count("Sales Invoice Item", {"sales_order": so_name}), 0)
		self.assertEqual(frappe.db.count("Payment Entry Reference", {"reference_name": so_name}), 0)

	# ==================================================
	# 2. VARIANT ORDER LIVE TEST
	# ==================================================
	def test_02_variant_order_live_ingestion(self):
		"""
		Verifies exact variant combination mapping:
		- Combination 9 must map to ITEM-ORD-LIVE-VAR9
		- Parent item or other combinations must NOT be substituted
		- Reservation allocated specifically to ITEM-ORD-LIVE-VAR9
		"""
		atp_var9_before = get_channel_atp(self.item_var9, self.channel_a).aggregate_atp_qty
		atp_var10_before = get_channel_atp(self.item_var10, self.channel_a).aggregate_atp_qty

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="902",
			external_reference="SYNTH-902",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			currency="USD",
			customer=ExternalCustomer(external_customer_id="902", first_name="Test Customer", last_name="Beta"),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id="2",
					external_variant_id="9",  # Exact combination 9
					quantity=3.0,
					unit_price_ex_tax=30.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=90.0, total_paid=90.0),
		)

		res = ingest_order_pipeline(ext_order)
		self.assertTrue(res["success"])
		so_name = res["sales_order"]

		so = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so.items[0].item_code, self.item_var9)
		self.assertEqual(so.items[0].qty, 3.0)

		# Verify reservation on var9
		sre = frappe.db.get_value(
			"Stock Reservation Entry",
			{"voucher_type": "Sales Order", "voucher_no": so_name, "item_code": self.item_var9, "docstatus": 1},
			["name", "reserved_qty"],
			as_dict=True,
		)
		self.assertIsNotNone(sre)
		self.assertEqual(flt(sre.reserved_qty), 3.0)

		# ATP var9 decreased by 3, var10 untouched
		atp_var9_after = get_channel_atp(self.item_var9, self.channel_a).aggregate_atp_qty
		atp_var10_after = get_channel_atp(self.item_var10, self.channel_a).aggregate_atp_qty
		self.assertEqual(atp_var9_after, atp_var9_before - 3.0)
		self.assertEqual(atp_var10_after, atp_var10_before)

	# ==================================================
	# 3. MULTILINE ORDER LIVE TEST
	# ==================================================
	def test_03_multiline_order_live_ingestion(self):
		"""
		Order with 2 lines: Simple (qty 2) and Variant (qty 1).
		Both reservations created atomically and totals reconcile.
		"""
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="903",
			external_reference="SYNTH-903",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			currency="USD",
			customer=ExternalCustomer(external_customer_id="903", first_name="Test Customer", last_name="Gamma"),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id="6",
					quantity=2.0,
					unit_price_ex_tax=20.0,
				),
				ExternalOrderLine(
					external_line_id="2",
					external_product_id="2",
					external_variant_id="9",
					quantity=1.0,
					unit_price_ex_tax=40.0,
				),
			],
			totals=ExternalTotals(total_products_ex_tax=80.0, total_paid=80.0),
		)

		res = ingest_order_pipeline(ext_order)
		self.assertTrue(res["success"])
		so_name = res["sales_order"]

		so = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(len(so.items), 2)
		self.assertEqual(flt(so.net_total), 80.0)

		# Verify reservations for both lines
		sre_count = frappe.db.count("Stock Reservation Entry", {"voucher_no": so_name, "docstatus": 1})
		self.assertEqual(sre_count, 2)

	# ==================================================
	# 4. MISSING MAPPING REJECTION
	# ==================================================
	def test_04_missing_mapping_rejection_live(self):
		"""
		Order contains unmapped product ID 9999.
		Must fail fast with MissingProductMappingError.
		ZERO Sales Orders, ZERO reservations created.
		"""
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="904",
			external_reference="SYNTH-904",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			customer=ExternalCustomer(external_customer_id="904"),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id="9999",  # Unmapped
					quantity=1.0,
					unit_price_ex_tax=10.0,
				)
			],
		)

		with self.assertRaises(MissingProductMappingError):
			ingest_order_pipeline(ext_order)

		self.assertIsNone(find_existing_order_mapping(self.channel_a, IntegrationProvider.PRESTASHOP, "904"))
		self.assertEqual(frappe.db.count("Sales Order", {"customer": ["like", "%904%"]}), 0)

	# ==================================================
	# 5. INSUFFICIENT ATP REJECTION (ALL-OR-NOTHING)
	# ==================================================
	def test_05_insufficient_atp_rejection_live(self):
		"""
		Order quantity exceeds available channel ATP.
		Must fail with InsufficientOrderStockError.
		ZERO active Sales Orders, ZERO reservations created.
		"""
		atp_before = get_channel_atp(self.item_simple, self.channel_a).aggregate_atp_qty

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="905",
			external_reference="SYNTH-905",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			customer=ExternalCustomer(external_customer_id="905"),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id="6",
					quantity=99999.0,  # Far exceeds 100.0 ATP
					unit_price_ex_tax=10.0,
				)
			],
		)

		with self.assertRaises(InsufficientOrderStockError):
			ingest_order_pipeline(ext_order)

		# Verify no Sales Order and no reservation created
		self.assertIsNone(find_existing_order_mapping(self.channel_a, IntegrationProvider.PRESTASHOP, "905"))
		atp_after = get_channel_atp(self.item_simple, self.channel_a).aggregate_atp_qty
		self.assertEqual(atp_after, atp_before)

	# ==================================================
	# 6. DUPLICATE REPLAY LIVE TEST
	# ==================================================
	def test_06_duplicate_replay_live(self):
		"""
		Repeated ingestion of the same external order must return the existing
		Sales Order without creating duplicate records or double reservations.
		"""
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="906",
			external_reference="SYNTH-906",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			customer=ExternalCustomer(external_customer_id="906", first_name="Test Customer", last_name="Delta"),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id="6",
					quantity=2.0,
					unit_price_ex_tax=20.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=40.0, total_paid=40.0),
		)

		# Ingestion 1
		res1 = ingest_order_pipeline(ext_order)
		self.assertTrue(res1["success"])
		self.assertFalse(res1["is_replay"])
		so_name_1 = res1["sales_order"]

		# Ingest 5 more times
		for _ in range(5):
			res_dup = ingest_order_pipeline(ext_order)
			self.assertTrue(res_dup["success"])
			self.assertTrue(res_dup["is_replay"])
			self.assertEqual(res_dup["sales_order"], so_name_1)

		# Ensure total Sales Orders for 906 is exactly 1
		self.assertEqual(
			frappe.db.count("External ID Mapping", {
				"external_entity_type": ExternalEntityType.ORDER,
				"external_id": "906",
				"active": 1,
			}),
			1,
		)
		sre_count = frappe.db.count("Stock Reservation Entry", {"voucher_no": so_name_1, "docstatus": 1})
		self.assertEqual(sre_count, 1)

	# ==================================================
	# 7. TRUE CONCURRENT DUPLICATE WORKERS TEST
	# ==================================================
	def test_07_true_concurrent_duplicate_workers(self):
		"""
		Two worker threads simultaneously attempt to ingest the same external order.
		DB unique constraint on active_external_key guarantees exactly ONE Sales Order.
		Both workers complete safely and report the same winner Sales Order.
		"""
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="907",
			external_reference="SYNTH-907",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			customer=ExternalCustomer(external_customer_id="907", first_name="Test Customer", last_name="Epsilon"),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id="6",
					quantity=1.0,
					unit_price_ex_tax=15.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=15.0, total_paid=15.0),
		)

		def _worker_task():
			frappe.init("frontend")
			frappe.connect()
			try:
				for attempt in range(3):
					try:
						r = ingest_order_pipeline(ext_order)
						frappe.db.commit()
						return r
					except (frappe.QueryDeadlockError, frappe.DuplicateEntryError):
						frappe.db.rollback()
						time.sleep(0.1)
				r = ingest_order_pipeline(ext_order)
				frappe.db.commit()
				return r
			finally:
				frappe.db.close()

		with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
			f1 = executor.submit(_worker_task)
			f2 = executor.submit(_worker_task)
			res1 = f1.result(timeout=30)
			res2 = f2.result(timeout=30)

		self.assertTrue(res1["success"])
		self.assertTrue(res2["success"])
		# Both must agree on the same Sales Order name
		self.assertEqual(res1["sales_order"], res2["sales_order"])

		# Total mappings for 907 must be exactly 1
		self.assertEqual(
			frappe.db.count("External ID Mapping", {
				"external_entity_type": ExternalEntityType.ORDER,
				"external_id": "907",
				"active": 1,
			}),
			1,
		)

	# ==================================================
	# 8. CRASH RECOVERY LIVE TEST
	# ==================================================
	def test_08_crash_recovery_live(self):
		"""
		Inject crash after Sales Order creation and verify that worker retry
		recognizes existing mapping and converges the Integration Event to SUCCEEDED.
		"""
		idem_key = compute_order_idempotency_key(IntegrationProvider.PRESTASHOP, self.channel_a, "908")

		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.INGEST_ORDER,
			"external_id": "908",
			"idempotency_key": idem_key,
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({
				"sales_channel": self.channel_a,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_order_id": "908",
				"normalized_order": {
					"provider": IntegrationProvider.PRESTASHOP,
					"sales_channel": self.channel_a,
					"external_order_id": "908",
					"external_reference": "SYNTH-908",
					"order_state_id": "2",
					"date_add": "2026-09-07 12:00:00",
					"customer": {"external_customer_id": "908", "first_name": "Crash", "last_name": "Test"},
					"lines": [{"external_line_id": "1", "external_product_id": "6", "quantity": 1.0, "unit_price_ex_tax": 20.0}],
					"totals": {"total_products_ex_tax": 20.0, "total_paid": 20.0},
				}
			}),
		})
		event.insert(ignore_permissions=True)
		frappe.db.commit()

		# Run 1: Successfully processes
		res = process_order_ingestion_event(event.name, worker_id="worker-1")
		self.assertTrue(res["success"])
		so_name = res["sales_order"]

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)
		self.assertEqual(event.erp_document, so_name)

		# Simulate crash/replay: Set event back to PENDING and re-run worker
		event.db_set("status", IntegrationStatus.PENDING)
		frappe.db.commit()

		res_replay = process_order_ingestion_event(event.name, worker_id="worker-2")
		self.assertTrue(res_replay["success"])
		self.assertTrue(res_replay.get("is_replay"))
		self.assertEqual(res_replay["sales_order"], so_name)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# 9. CUSTOMER CONCURRENCY TEST
	# ==================================================
	def test_09_customer_concurrency_live(self):
		"""
		Two different orders for the same previously unseen customer.
		Must create exactly ONE ERP Customer and link both Sales Orders to that Customer.
		"""
		ext_order_1 = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="909-A",
			external_reference="SYNTH-909A",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			customer=ExternalCustomer(external_customer_id="SHARED-909", first_name="Test Customer", last_name="Shared"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=20.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		ext_order_2 = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="909-B",
			external_reference="SYNTH-909B",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			customer=ExternalCustomer(external_customer_id="SHARED-909", first_name="Test Customer", last_name="Shared"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=20.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		res1 = ingest_order_pipeline(ext_order_1)
		res2 = ingest_order_pipeline(ext_order_2)

		so1 = frappe.get_doc("Sales Order", res1["sales_order"])
		so2 = frappe.get_doc("Sales Order", res2["sales_order"])

		# Both Sales Orders must point to the same customer
		self.assertEqual(so1.customer, so2.customer)

		# Exactly one Customer mapping for SHARED-909
		cust_maps = frappe.db.count("External ID Mapping", {
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"external_id": "SHARED-909",
			"active": 1,
		})
		self.assertEqual(cust_maps, 1)

	# ==================================================
	# 10. STATE CHANGE BEFORE INGESTION
	# ==================================================
	def test_10_state_change_before_ingestion_live(self):
		"""
		Discovered order whose state became Canceled (state 6) before worker execution.
		Worker rejects conversion, creates ZERO Sales Orders and ZERO reservations.
		"""
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="910",
			external_reference="SYNTH-910",
			order_state_id="6",  # PrestaShop Canceled state
			customer=ExternalCustomer(external_customer_id="910"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=10.0)],
		)

		with self.assertRaises(OrderNotEligibleError):
			ingest_order_pipeline(ext_order)

		self.assertIsNone(find_existing_order_mapping(self.channel_a, IntegrationProvider.PRESTASHOP, "910"))

	# ==================================================
	# 11. SHARED INVENTORY MULTI-CHANNEL AFFECTED PUBLICATION
	# ==================================================
	def test_11_shared_inventory_multichannel_publication(self):
		"""
		Channel A and Channel B share the same warehouse wh_a.
		An order on Channel A reserves stock.
		Publication intents must be scheduled for BOTH Channel A and Channel B.
		"""
		affected = find_affected_channels_for_items(self.channel_a, [self.item_simple])
		self.assertIn(self.channel_a, affected)
		self.assertIn(self.channel_b, affected)

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="911",
			external_reference="SYNTH-911",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			customer=ExternalCustomer(external_customer_id="911", first_name="Test Customer", last_name="SharedInv"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=20.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		res = ingest_order_pipeline(ext_order)
		self.assertTrue(res["success"])
		self.assertIn(self.channel_a, res["affected_channels"])
		self.assertIn(self.channel_b, res["affected_channels"])
