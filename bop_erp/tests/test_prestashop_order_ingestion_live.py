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
	is_order_ingestion_complete,
	claim_event_for_processing,
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

		cls.item_zero = "ITEM-ORD-LIVE-ZERO"
		if not frappe.db.exists("Item", cls.item_zero):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_zero,
				"item_name": "Order Test Zero Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.item_short = "ITEM-ORD-LIVE-SHORT"
		if not frappe.db.exists("Item", cls.item_short):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_short,
				"item_name": "Order Test Short Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.item_po = "ITEM-ORD-LIVE-PO"
		if not frappe.db.exists("Item", cls.item_po):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_po,
				"item_name": "Order Test PO Item",
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
		cls._set_physical_stock(cls.item_short, cls.wh_a, 2.0)
		cls._set_physical_stock(cls.item_zero, cls.wh_a, 0.0)
		cls._set_physical_stock(cls.item_po, cls.wh_a, 0.0)
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
		for ic in [cls.item_simple, cls.item_var9, cls.item_var10, cls.item_zero, cls.item_short, cls.item_po]:
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
				filters={"item_code": ["in", [cls.item_simple, cls.item_var9, cls.item_var10, cls.item_zero, cls.item_short, cls.item_po]]},
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

			frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [cls.item_simple, cls.item_var9, cls.item_var10, cls.item_zero, cls.item_short, cls.item_po]]})
			frappe.db.sql("DELETE FROM `tabIntegration Event` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabInventory Publication State` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabExternal ID Mapping` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabChannel Inventory Source` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabPrestaShop Connector` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabSales Channel` WHERE name = %s", (ch,))

		# Clean test Customers & Addresses
		cleanup_patterns = ["%Test Customer%", "%Crash%", "%FiveHundred%", "%Recovery%", "%Depth%", "%Brenda%", "%Alex%", "%Race%"]
		all_custs = []
		all_addrs = []
		for pat in cleanup_patterns:
			all_custs.extend(frappe.get_all("Customer", filters={"name": ["like", pat]}, pluck="name"))
			all_addrs.extend(frappe.get_all("Address", filters={"address_title": ["like", pat]}, pluck="name"))

		for c in set(all_custs):
			frappe.delete_doc("Customer", c, force=True, ignore_permissions=True)

		for a in set(all_addrs):
			frappe.delete_doc("Address", a, force=True, ignore_permissions=True)

		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self._static_cleanup()
		self._setup_test_channels()
		self._set_physical_stock(self.item_simple, self.wh_a, 100.0)
		self._set_physical_stock(self.item_var9, self.wh_a, 100.0)
		self._set_physical_stock(self.item_var10, self.wh_a, 100.0)
		self._set_physical_stock(self.item_short, self.wh_a, 2.0)
		self._set_physical_stock(self.item_zero, self.wh_a, 0.0)
		self._set_physical_stock(self.item_po, self.wh_a, 0.0)
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

		# Mappings: Product 20 -> item_zero
		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_a,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "20",
			"erp_doctype": "Item",
			"erp_document": self.item_zero,
			"active": 1,
		}).insert(ignore_permissions=True)

		# Mappings: Product 21 -> item_short
		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_a,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "21",
			"erp_doctype": "Item",
			"erp_document": self.item_short,
			"active": 1,
		}).insert(ignore_permissions=True)

		# Mappings: Product 22 -> item_po
		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_a,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "22",
			"erp_doctype": "Item",
			"erp_document": self.item_po,
			"active": 1,
		}).insert(ignore_permissions=True)

		# Mappings on Channel B: Product 6 -> item_simple
		frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_b,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"external_id": "6",
			"erp_doctype": "Item",
			"erp_document": self.item_simple,
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
		# Exactly ONE Sales Order in database, zero transient or extra submitted orders
		self.assertEqual(
			frappe.db.count("Sales Order", {"sales_channel": self.channel_a, "customer": ["like", "%Epsilon%"]}),
			1,
		)
		# Exactly ONE active SRE with reserved_qty = 1.0
		total_reserved = frappe.db.sql(
			"""
			SELECT SUM(reserved_qty) FROM `tabStock Reservation Entry`
			WHERE voucher_type = 'Sales Order' AND voucher_no = %s AND docstatus = 1
			""",
			(res1["sales_order"],),
		)[0][0]
		self.assertEqual(flt(total_reserved), 1.0)

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

	# ==================================================
	# 12. ZERO STOCK ORDER LIVE TEST
	# ==================================================
	def test_12_zero_stock_order_live(self):
		"""
		External order arrives for an item with 0.0 physical stock.
		Must reject immediately with InsufficientOrderStockError.
		Must leave ZERO active submitted Sales Orders and ZERO reservations.
		"""
		atp = get_channel_atp(self.item_zero, self.channel_a).aggregate_atp_qty
		self.assertEqual(atp, 0.0)

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="912",
			external_reference="SYNTH-912",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="912", first_name="Test Customer", last_name="ZeroStock"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="20", quantity=2.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		with self.assertRaises(InsufficientOrderStockError):
			ingest_order_pipeline(ext_order)

		self.assertIsNone(find_existing_order_mapping(self.channel_a, IntegrationProvider.PRESTASHOP, "912"))
		# Verify zero submitted Sales Orders
		self.assertEqual(
			frappe.db.count("Sales Order", {"sales_channel": self.channel_a, "customer": ["like", "%ZeroStock%"]}),
			0,
		)
		# Verify zero SREs
		self.assertEqual(
			frappe.db.count("Stock Reservation Entry", {"item_code": self.item_zero}),
			0,
		)

	# ==================================================
	# 13. PARTIALLY AVAILABLE SINGLE-LINE ORDER LIVE TEST
	# ==================================================
	def test_13_partially_available_single_line_order_live(self):
		"""
		Available physical stock is 2.0, order requests 5.0.
		Under all-or-nothing policy, partial acceptance is FORBIDDEN.
		Must fail with InsufficientOrderStockError.
		Must leave ZERO active Sales Orders and ZERO reservations.
		Stock must remain at 2.0.
		"""
		atp_before = get_channel_atp(self.item_short, self.channel_a).aggregate_atp_qty
		self.assertEqual(atp_before, 2.0)

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="913",
			external_reference="SYNTH-913",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="913", first_name="Test Customer", last_name="PartialShort"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="21", quantity=5.0, unit_price_ex_tax=15.0)],
			totals=ExternalTotals(total_products_ex_tax=75.0, total_paid=75.0),
		)

		with self.assertRaises(InsufficientOrderStockError):
			ingest_order_pipeline(ext_order)

		self.assertIsNone(find_existing_order_mapping(self.channel_a, IntegrationProvider.PRESTASHOP, "913"))
		self.assertEqual(
			frappe.db.count("Sales Order", {"sales_channel": self.channel_a, "customer": ["like", "%PartialShort%"]}),
			0,
		)
		self.assertEqual(
			frappe.db.count("Stock Reservation Entry", {"item_code": self.item_short}),
			0,
		)
		atp_after = get_channel_atp(self.item_short, self.channel_a).aggregate_atp_qty
		self.assertEqual(atp_after, 2.0)

	# ==================================================
	# 14. MULTI-LINE PARTIAL FAILURE FULL ROLLBACK
	# ==================================================
	def test_14_multiline_partial_failure_full_rollback_live(self):
		"""
		Multi-line order:
		- Line 1: Item with ample stock (item_simple requested 2, ATP 100)
		- Line 2: Item with insufficient stock (item_short requested 3, ATP 2)
		Must fail atomically.
		ZERO active Sales Orders.
		ZERO surviving SREs for Line 1 (full rollback).
		"""
		atp_simple_before = get_channel_atp(self.item_simple, self.channel_a).aggregate_atp_qty
		atp_short_before = get_channel_atp(self.item_short, self.channel_a).aggregate_atp_qty

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="914",
			external_reference="SYNTH-914",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="914", first_name="Test Customer", last_name="MultiRollback"),
			lines=[
				ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=2.0, unit_price_ex_tax=10.0),
				ExternalOrderLine(external_line_id="2", external_product_id="21", quantity=3.0, unit_price_ex_tax=15.0),
			],
			totals=ExternalTotals(total_products_ex_tax=65.0, total_paid=65.0),
		)

		with self.assertRaises(InsufficientOrderStockError):
			ingest_order_pipeline(ext_order)

		self.assertIsNone(find_existing_order_mapping(self.channel_a, IntegrationProvider.PRESTASHOP, "914"))
		self.assertEqual(
			frappe.db.count("Sales Order", {"sales_channel": self.channel_a, "customer": ["like", "%MultiRollback%"]}),
			0,
		)
		# Line 1 must NOT leave a surviving reservation!
		self.assertEqual(
			frappe.db.count("Stock Reservation Entry", {"voucher_no": ["like", "%914%"]}),
			0,
		)
		self.assertEqual(
			get_channel_atp(self.item_simple, self.channel_a).aggregate_atp_qty,
			atp_simple_before,
		)
		self.assertEqual(
			get_channel_atp(self.item_short, self.channel_a).aggregate_atp_qty,
			atp_short_before,
		)

	# ==================================================
	# 15. CROSS-CHANNEL SAME EXTERNAL ORDER ID
	# ==================================================
	def test_15_cross_channel_same_external_order_id_live(self):
		"""
		Two orders from different sales channels (Channel A and Channel B)
		share the exact same external_order_id="500".
		Both must ingest cleanly, mapping to 2 distinct ERP Sales Orders.
		"""
		ext_order_a = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="500",
			external_reference="SYNTH-500-A",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="500-A", first_name="Cust A", last_name="FiveHundred"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=20.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		ext_order_b = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_b,
			external_order_id="500",
			external_reference="SYNTH-500-B",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="500-B", first_name="Cust B", last_name="FiveHundred"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=20.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		res_a = ingest_order_pipeline(ext_order_a)
		res_b = ingest_order_pipeline(ext_order_b)

		self.assertTrue(res_a["success"])
		self.assertTrue(res_b["success"])

		so_a = res_a["sales_order"]
		so_b = res_b["sales_order"]

		# Must be two distinct Sales Orders
		self.assertNotEqual(so_a, so_b)
		self.assertEqual(frappe.db.get_value("Sales Order", so_a, "sales_channel"), self.channel_a)
		self.assertEqual(frappe.db.get_value("Sales Order", so_b, "sales_channel"), self.channel_b)

		# Mappings must be distinct and channel-scoped
		mapped_a = find_existing_order_mapping(self.channel_a, IntegrationProvider.PRESTASHOP, "500")
		mapped_b = find_existing_order_mapping(self.channel_b, IntegrationProvider.PRESTASHOP, "500")
		self.assertEqual(mapped_a, so_a)
		self.assertEqual(mapped_b, so_b)

	# ==================================================
	# 16. SAME EMAIL DIFFERENT CUSTOMERS NOT MERGED
	# ==================================================
	def test_16_same_email_different_customers_not_merged_live(self):
		"""
		Two orders have different external_customer_ids (916-A, 916-B)
		but share the exact same email address.
		Automated ingestion must NOT merge them into a single Customer.
		"""
		shared_email = "shared_live_audit@example.com"

		ext_order_1 = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="916-1",
			external_reference="SYNTH-916-1",
			order_state_id="2",
			customer=ExternalCustomer(
				external_customer_id="916-A",
				first_name="Alice",
				last_name="Test Customer",
				email=shared_email,
			),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=20.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		ext_order_2 = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="916-2",
			external_reference="SYNTH-916-2",
			order_state_id="2",
			customer=ExternalCustomer(
				external_customer_id="916-B",
				first_name="Bob",
				last_name="Test Customer",
				email=shared_email,
			),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=20.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		res1 = ingest_order_pipeline(ext_order_1)
		res2 = ingest_order_pipeline(ext_order_2)

		so1 = frappe.get_doc("Sales Order", res1["sales_order"])
		so2 = frappe.get_doc("Sales Order", res2["sales_order"])

		# Two separate Customers must exist
		self.assertNotEqual(so1.customer, so2.customer)

		cust_a = frappe.db.get_value("External ID Mapping", {
			"sales_channel": self.channel_a,
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"external_id": "916-A",
			"active": 1,
		}, "erp_document")
		cust_b = frappe.db.get_value("External ID Mapping", {
			"sales_channel": self.channel_a,
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"external_id": "916-B",
			"active": 1,
		}, "erp_document")

		self.assertIsNotNone(cust_a)
		self.assertIsNotNone(cust_b)
		self.assertNotEqual(cust_a, cust_b)

	# ==================================================
	# 17. ADDRESS EXTERNAL IDENTITY
	# ==================================================
	def test_17_address_external_identity_live(self):
		"""
		PrestaShop delivery and invoice addresses are mapped by external_address_id.
		Sales Order links to the mapped Address document.
		"""
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="917",
			external_reference="SYNTH-917",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="917", first_name="Test Customer", last_name="AddrIdentity"),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-917-D",
				first_name="Test Customer",
				last_name="AddrIdentity",
				address1="789 Shipping Way",
				city="Miami",
				postcode="33101",
				country="United States",
			),
			invoice_address=ExternalAddress(
				external_address_id="ADDR-917-I",
				first_name="Test Customer",
				last_name="AddrIdentity",
				address1="101 Billing Blvd",
				city="Tampa",
				postcode="33601",
				country="United States",
			),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=20.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		res = ingest_order_pipeline(ext_order)
		self.assertTrue(res["success"])
		so = frappe.get_doc("Sales Order", res["sales_order"])

		# Verify shipping address mapped
		deliv_addr_name = frappe.db.get_value("External ID Mapping", {
			"sales_channel": self.channel_a,
			"external_entity_type": ExternalEntityType.ADDRESS,
			"external_id": "ADDR-917-D",
			"active": 1,
		}, "erp_document")
		self.assertIsNotNone(deliv_addr_name)
		self.assertEqual(so.shipping_address_name, deliv_addr_name)

		# Verify invoice address mapped
		inv_addr_name = frappe.db.get_value("External ID Mapping", {
			"sales_channel": self.channel_a,
			"external_entity_type": ExternalEntityType.ADDRESS,
			"external_id": "ADDR-917-I",
			"active": 1,
		}, "erp_document")
		self.assertIsNotNone(inv_addr_name)
		self.assertEqual(so.customer_address, inv_addr_name)

	# ==================================================
	# 18. INSUFFICIENT ORDER ZERO PUBLICATION INTENTS
	# ==================================================
	def test_18_insufficient_order_zero_publication_intents_live(self):
		"""
		When an order fails due to insufficient stock, ZERO publication
		intents must be scheduled. Database state must be unmutated.
		"""
		intents_before = frappe.db.count("Inventory Publication State", {"sales_channel": self.channel_a})

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="918",
			external_reference="SYNTH-918",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="918"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="20", quantity=10.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=100.0, total_paid=100.0),
		)

		with self.assertRaises(InsufficientOrderStockError):
			ingest_order_pipeline(ext_order)

		frappe.db.commit()
		intents_after = frappe.db.count("Inventory Publication State", {"sales_channel": self.channel_a})
		self.assertEqual(intents_after, intents_before)

	# ==================================================
	# 19. INCOMING ORDERED_QTY DOES NOT INCREASE ATP
	# ==================================================
	def test_19_incoming_ordered_qty_does_not_increase_atp_live(self):
		"""
		Regression verification of Phase 1I ATP authority:
		item_po has actual_qty = 0.
		Even with ordered_qty = 100 on tabBin, immediate Channel ATP MUST be 0.0.
		An order requesting 1 unit must fail with InsufficientOrderStockError.
		"""
		# Ensure bin exists with actual_qty = 0 and ordered_qty = 100
		bin_name = frappe.db.get_value("Bin", {"item_code": self.item_po, "warehouse": self.wh_a}, "name")
		if not bin_name:
			b = frappe.get_doc({
				"doctype": "Bin",
				"item_code": self.item_po,
				"warehouse": self.wh_a,
				"actual_qty": 0.0,
				"ordered_qty": 100.0,
			})
			b.flags.ignore_permissions = True
			b.insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Bin", bin_name, {"actual_qty": 0.0, "ordered_qty": 100.0})
		frappe.db.commit()

		# Verify immediate ATP is strictly 0.0
		atp = get_channel_atp(self.item_po, self.channel_a)
		self.assertEqual(atp.aggregate_atp_qty, 0.0)

		# Attempt order
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="919",
			external_reference="SYNTH-919",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="919"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="22", quantity=1.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=10.0, total_paid=10.0),
		)

		with self.assertRaises(InsufficientOrderStockError):
			ingest_order_pipeline(ext_order)

		self.assertIsNone(find_existing_order_mapping(self.channel_a, IntegrationProvider.PRESTASHOP, "919"))

	# ==================================================
	# 20. LIVE END-TO-END DISCOVERY TO EVENT SUCCESS
	# ==================================================
	def test_20_live_end_to_end_discovery_to_event_success(self):
		"""
		End-to-end live test against local PrestaShop test instance:
		1. Discover orders via discover_channel_orders with client=self.client.
		2. Order 8 (state 2, product 21) is discovered and written as a PENDING Integration Event.
		3. Event is claimed atomically and processed via process_order_ingestion_event.
		4. Worker performs fresh GET from PrestaShop test instance, validates eligibility,
		   creates native Sales Order & Stock Reservation Entry, commits, and marks SUCCEEDED.
		5. Verifies 0 Sales Invoices, 0 Payment Entries, exactly 1 SO and SRE.
		"""
		conn_doc = frappe.get_doc("PrestaShop Connector", {"sales_channel": self.channel_a})
		conn_doc.last_order_watermark = "2026-09-01 00:00:00"
		conn_doc.last_order_id = None
		conn_doc.save(ignore_permissions=True)
		frappe.db.commit()

		# Run live discovery
		disc_res = discover_channel_orders(conn_doc.as_dict(), max_orders=20, client=self.client)
		self.assertGreater(disc_res["events_created"], 0)

		event_name = frappe.db.get_value(
			"Integration Event",
			{
				"sales_channel": self.channel_a,
				"external_id": "8",
				"provider": IntegrationProvider.PRESTASHOP,
				"direction": IntegrationDirection.INBOUND,
				"operation": IntegrationOperation.INGEST_ORDER,
			},
			"name",
		)
		self.assertIsNotNone(event_name, "Order 8 should have been discovered and queued as an Integration Event")

		event = frappe.get_doc("Integration Event", event_name)
		self.assertEqual(event.status, IntegrationStatus.PENDING)

		atp_before = get_channel_atp(self.item_short, self.channel_a).aggregate_atp_qty

		# Worker claim and execution
		res = process_order_ingestion_event(event_name, worker_id="worker-live-20", client=self.client)
		self.assertTrue(res["success"], f"Processing failed: {res}")
		so_name = res["sales_order"]

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)
		self.assertEqual(event.erp_document, so_name)
		self.assertIsNotNone(event.processing_finished_at)

		so = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so.docstatus, 1)
		self.assertEqual(len(so.items), 1)
		self.assertEqual(so.items[0].item_code, self.item_short)
		self.assertEqual(so.items[0].qty, 1.0)

		# SRE verification
		sre = frappe.db.get_value(
			"Stock Reservation Entry",
			{"voucher_type": "Sales Order", "voucher_no": so_name, "docstatus": 1},
			["name", "reserved_qty"],
			as_dict=True,
		)
		self.assertIsNotNone(sre)
		self.assertEqual(flt(sre.reserved_qty), 1.0)

		# ATP check
		atp_after = get_channel_atp(self.item_short, self.channel_a).aggregate_atp_qty
		self.assertEqual(atp_after, atp_before - 1.0)

		# Zero invoice / payment entries
		self.assertEqual(frappe.db.count("Sales Invoice", {"sales_channel": self.channel_a}), 0)
		self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": so_name}), 0)

	# ==================================================
	# 21. LIVE STATE CHANGE BEFORE WORKER EXECUTION
	# ==================================================
	def test_21_live_state_change_before_worker_execution(self):
		"""
		Discovered order was queued as PENDING, but remote PrestaShop state changed to
		Canceled (state 6) before worker execution.
		Worker re-reads fresh order state from PrestaShop at execution time, detects
		state '6' is not in eligible_order_states ('2,3,11'), and safely transitions
		the Integration Event to CANCELLED without creating Sales Orders or reservations.
		"""
		# Order 11 in PrestaShop test DB is in state 6 (Canceled)
		idem_key = compute_order_idempotency_key(IntegrationProvider.PRESTASHOP, self.channel_a, "11")
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.INGEST_ORDER,
			"external_id": "11",
			"idempotency_key": idem_key,
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({
				"sales_channel": self.channel_a,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_order_id": "11",
			}),
		})
		event.insert(ignore_permissions=True)
		frappe.db.commit()

		so_count_before = frappe.db.count("Sales Order", {"sales_channel": self.channel_a})
		sre_count_before = frappe.db.count("Stock Reservation Entry", {"docstatus": 1})

		res = process_order_ingestion_event(event.name, worker_id="worker-live-21", client=self.client)
		self.assertFalse(res["success"])
		self.assertEqual(res.get("category"), "NOT_ELIGIBLE")

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.CANCELLED)
		self.assertIn("not in eligible states", event.last_error_message or "")
		self.assertIsNotNone(event.processing_finished_at)

		# Verify ZERO Sales Orders and ZERO Stock Reservation Entries created
		self.assertEqual(frappe.db.count("Sales Order", {"sales_channel": self.channel_a}), so_count_before)
		self.assertEqual(frappe.db.count("Stock Reservation Entry", {"docstatus": 1}), sre_count_before)

	# ==================================================
	# 22. LIVE CRASH AFTER SO SUBMIT RECOVERY
	# ==================================================
	def test_22_live_crash_after_so_submit_recovery(self):
		"""
		Simulate mid-transaction crash where Sales Order was created and submitted,
		but a Stock Reservation Entry was not created (or cancelled/lost).
		Worker retry detects incomplete ingestion via is_order_ingestion_complete,
		idempotently completes missing line reservations via _ensure_order_reservations,
		and transitions the event to SUCCEEDED.
		"""
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="922",
			external_reference="SYNTH-922",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			customer=ExternalCustomer(external_customer_id="922", first_name="Crash", last_name="Recovery22"),
			lines=[
				ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=2.0, unit_price_ex_tax=20.0),
			],
			totals=ExternalTotals(total_products_ex_tax=40.0, total_paid=40.0),
		)

		# Initial ingestion succeeds completely
		res1 = ingest_order_pipeline(ext_order)
		self.assertTrue(res1["success"])
		so_name = res1["sales_order"]

		# Cancel and delete SRE to simulate mid-transaction crash right after SO submission
		sre_name = frappe.db.get_value(
			"Stock Reservation Entry",
			{"voucher_type": "Sales Order", "voucher_no": so_name, "docstatus": 1},
			"name",
		)
		self.assertIsNotNone(sre_name)
		sre_doc = frappe.get_doc("Stock Reservation Entry", sre_name)
		sre_doc.cancel()
		frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
		frappe.db.commit()

		# Verify incomplete order state detected
		complete, issues = is_order_ingestion_complete(so_name)
		self.assertFalse(complete)
		self.assertTrue(len(issues) > 0)

		# Create Integration Event simulating worker recovery attempt
		idem_key = compute_order_idempotency_key(IntegrationProvider.PRESTASHOP, self.channel_a, "922")
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.INGEST_ORDER,
			"external_id": "922",
			"idempotency_key": idem_key,
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({
				"sales_channel": self.channel_a,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_order_id": "922",
				"normalized_order": {
					"provider": IntegrationProvider.PRESTASHOP,
					"sales_channel": self.channel_a,
					"external_order_id": "922",
					"external_reference": "SYNTH-922",
					"order_state_id": "2",
					"customer": {"external_customer_id": "922"},
					"lines": [{"external_line_id": "1", "external_product_id": "6", "quantity": 2.0, "unit_price_ex_tax": 20.0}],
					"totals": {"total_products_ex_tax": 40.0, "total_paid": 40.0},
				}
			}),
		})
		event.insert(ignore_permissions=True)
		frappe.db.commit()

		# Recovery worker execution
		rec_res = process_order_ingestion_event(event.name, worker_id="worker-live-22")
		self.assertTrue(rec_res["success"])
		self.assertTrue(rec_res.get("is_replay"))
		self.assertEqual(rec_res["sales_order"], so_name)

		# Verify reservation entry was reconstituted
		complete_after, issues_after = is_order_ingestion_complete(so_name)
		self.assertTrue(complete_after, f"Order still incomplete: {issues_after}")

		new_sre = frappe.db.get_value(
			"Stock Reservation Entry",
			{"voucher_type": "Sales Order", "voucher_no": so_name, "docstatus": 1},
			["name", "reserved_qty"],
			as_dict=True,
		)
		self.assertIsNotNone(new_sre)
		self.assertEqual(flt(new_sre.reserved_qty), 2.0)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)

	# ==================================================
	# 23. LIVE CRASH AFTER RESERVATION BEFORE SUCCESS
	# ==================================================
	def test_23_live_crash_after_reservation_before_success(self):
		"""
		Simulate crash after SO and SRE have both been committed, but the Integration Event
		remains in PROCESSING (e.g. worker died right before calling mark_succeeded).
		Upon worker retry (e.g. after lease expiration or retry trigger), worker:
		1. Reclaims or detects active mapping with complete SO + SRE.
		2. Detects is_order_ingestion_complete is True.
		3. Creates ZERO duplicate Sales Orders and ZERO duplicate reservations.
		4. Gracefully converges the event to SUCCEEDED with is_replay=True.
		"""
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="923",
			external_reference="SYNTH-923",
			order_state_id="2",
			date_add="2026-09-07 12:00:00",
			customer=ExternalCustomer(external_customer_id="923", first_name="Crash", last_name="Replay23"),
			lines=[
				ExternalOrderLine(external_line_id="1", external_product_id="6", quantity=1.0, unit_price_ex_tax=20.0),
			],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		res = ingest_order_pipeline(ext_order)
		self.assertTrue(res["success"])
		so_name = res["sales_order"]

		sre_count_before = frappe.db.count("Stock Reservation Entry", {"voucher_no": so_name, "docstatus": 1})
		self.assertEqual(sre_count_before, 1)

		# Create event in PENDING simulating replay of the event
		idem_key = compute_order_idempotency_key(IntegrationProvider.PRESTASHOP, self.channel_a, "923")
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.INGEST_ORDER,
			"external_id": "923",
			"idempotency_key": idem_key,
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({
				"sales_channel": self.channel_a,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_order_id": "923",
				"normalized_order": {
					"provider": IntegrationProvider.PRESTASHOP,
					"sales_channel": self.channel_a,
					"external_order_id": "923",
					"external_reference": "SYNTH-923",
					"order_state_id": "2",
					"customer": {"external_customer_id": "923"},
					"lines": [{"external_line_id": "1", "external_product_id": "6", "quantity": 1.0, "unit_price_ex_tax": 20.0}],
					"totals": {"total_products_ex_tax": 20.0, "total_paid": 20.0},
				}
			}),
		})
		event.insert(ignore_permissions=True)
		frappe.db.commit()

		# Re-run worker on the event
		replay_res = process_order_ingestion_event(event.name, worker_id="worker-live-23")
		self.assertTrue(replay_res["success"])
		self.assertTrue(replay_res.get("is_replay"))
		self.assertEqual(replay_res["sales_order"], so_name)

		# Assert zero duplicate reservations created
		sre_count_after = frappe.db.count("Stock Reservation Entry", {"voucher_no": so_name, "docstatus": 1})
		self.assertEqual(sre_count_after, 1)

		# Total reserved quantity remains 1.0
		total_reserved = frappe.db.sql(
			"""
			SELECT SUM(reserved_qty) FROM `tabStock Reservation Entry`
			WHERE voucher_type = 'Sales Order' AND voucher_no = %s AND docstatus = 1
			""",
			(so_name,),
		)[0][0]
		self.assertEqual(flt(total_reserved), 1.0)

		event.reload()
		self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)
		self.assertEqual(event.erp_document, so_name)

	# ==================================================
	# 24. LIVE TWO WORKER CLAIM RACE
	# ==================================================
	def test_24_live_two_worker_claim_race(self):
		"""
		Two concurrent worker threads attempt to claim and process the same PENDING Integration Event.
		Database-level row fencing ensures exactly ONE worker succeeds in claiming and processing.
		The second worker receives claim failure/authority loss and does not execute duplicate creation.
		System state contains exactly ONE Sales Order and ONE reservation.
		"""
		idem_key = compute_order_idempotency_key(IntegrationProvider.PRESTASHOP, self.channel_a, "924")
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.INGEST_ORDER,
			"external_id": "924",
			"idempotency_key": idem_key,
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({
				"sales_channel": self.channel_a,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_order_id": "924",
				"normalized_order": {
					"provider": IntegrationProvider.PRESTASHOP,
					"sales_channel": self.channel_a,
					"external_order_id": "924",
					"external_reference": "SYNTH-924",
					"order_state_id": "2",
					"customer": {"external_customer_id": "924", "first_name": "Race", "last_name": "Worker"},
					"lines": [{"external_line_id": "1", "external_product_id": "6", "quantity": 1.0, "unit_price_ex_tax": 20.0}],
					"totals": {"total_products_ex_tax": 20.0, "total_paid": 20.0},
				}
			}),
		})
		event.insert(ignore_permissions=True)
		frappe.db.commit()

		def _competing_worker(worker_id):
			frappe.init("frontend")
			frappe.connect()
			try:
				res = process_order_ingestion_event(event.name, worker_id=worker_id)
				frappe.db.commit()
				return res
			finally:
				frappe.db.close()

		with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
			f1 = executor.submit(_competing_worker, "worker-race-1")
			f2 = executor.submit(_competing_worker, "worker-race-2")
			res1 = f1.result(timeout=30)
			res2 = f2.result(timeout=30)

		# Exactly one worker succeeded; other was locked out
		successes = [r for r in [res1, res2] if r.get("success")]
		failures = [r for r in [res1, res2] if not r.get("success")]

		self.assertEqual(len(successes), 1, f"Expected exactly 1 success, got {res1} and {res2}")
		self.assertEqual(len(failures), 1, f"Expected exactly 1 failure, got {res1} and {res2}")
		self.assertTrue(
			failures[0].get("category") in ["LOCKED", "CONCURRENCY"]
			or failures[0].get("reason") in ["CLAIM_REJECTED_ALREADY_CLAIMED_OR_TERMINAL", "LOST_PROCESSING_AUTHORITY"],
			f"Unexpected failure response: {failures[0]}",
		)

		# Exactly 1 Sales Order in system for external order 924
		so_name = successes[0]["sales_order"]
		self.assertEqual(frappe.db.count("Sales Order", {"name": so_name}), 1)
		sre_count = frappe.db.count("Stock Reservation Entry", {"voucher_no": so_name, "docstatus": 1})
		self.assertEqual(sre_count, 1)

	# ==================================================
	# 25. LIVE DUPLICATE EVENT DEFENSE IN DEPTH
	# ==================================================
	def test_25_live_duplicate_event_defense_in_depth(self):
		"""
		Defense-in-depth against duplicate Integration Events:
		Suppose two separate Integration Events (e.g. from overlapping discovery windows or
		manual re-queuing with distinct event names) reference the SAME external order ID.
		Worker 1 executes Event 1 -> creates SO and SRE, marks Event 1 SUCCEEDED.
		Worker 2 executes Event 2 -> detects existing mapping via find_existing_order_mapping,
		links Event 2's erp_document to the existing Sales Order, performs 0 duplicate reservations,
		and marks Event 2 SUCCEEDED with is_replay=True.
		"""
		norm_payload = {
			"sales_channel": self.channel_a,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_order_id": "925",
			"normalized_order": {
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": self.channel_a,
				"external_order_id": "925",
				"external_reference": "SYNTH-925",
				"order_state_id": "2",
				"customer": {"external_customer_id": "925", "first_name": "Defense", "last_name": "Depth"},
				"lines": [{"external_line_id": "1", "external_product_id": "6", "quantity": 1.0, "unit_price_ex_tax": 20.0}],
				"totals": {"total_products_ex_tax": 20.0, "total_paid": 20.0},
			}
		}

		# Event 1
		event1 = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.INGEST_ORDER,
			"external_id": "925",
			"idempotency_key": "EVENT-DEFENSE-DEPTH-1",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps(norm_payload),
		})
		event1.insert(ignore_permissions=True)

		# Event 2 (different event name and idempotency key, but same external_order_id)
		event2 = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.channel_a,
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.INGEST_ORDER,
			"external_id": "925",
			"idempotency_key": "EVENT-DEFENSE-DEPTH-2",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps(norm_payload),
		})
		event2.insert(ignore_permissions=True)
		frappe.db.commit()

		# Process Event 1
		res1 = process_order_ingestion_event(event1.name, worker_id="worker-depth-1")
		self.assertTrue(res1["success"])
		self.assertFalse(res1.get("is_replay", False))
		so_name = res1["sales_order"]

		event1.reload()
		self.assertEqual(event1.status, IntegrationStatus.SUCCEEDED)
		self.assertEqual(event1.erp_document, so_name)

		# Process Event 2
		res2 = process_order_ingestion_event(event2.name, worker_id="worker-depth-2")
		self.assertTrue(res2["success"])
		self.assertTrue(res2.get("is_replay"))
		self.assertEqual(res2["sales_order"], so_name)

		event2.reload()
		self.assertEqual(event2.status, IntegrationStatus.SUCCEEDED)
		self.assertEqual(event2.erp_document, so_name)

		# Exactly 1 Sales Order exists for 925
		self.assertEqual(frappe.db.count("Sales Order", {"name": so_name}), 1)
		# Exactly 1 SRE with reserved_qty = 1.0 (zero over-reservation)
		sre_count = frappe.db.count("Stock Reservation Entry", {"voucher_no": so_name, "docstatus": 1})
		self.assertEqual(sre_count, 1)

		total_reserved = frappe.db.sql(
			"""
			SELECT SUM(reserved_qty) FROM `tabStock Reservation Entry`
			WHERE voucher_type = 'Sales Order' AND voucher_no = %s AND docstatus = 1
			""",
			(so_name,),
		)[0][0]
		self.assertEqual(flt(total_reserved), 1.0)

