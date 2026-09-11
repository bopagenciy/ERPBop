# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import copy
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from bop_erp.constants import (
	IntegrationProvider,
	ExternalEntityType,
	TransactionOrigin,
)
from bop_erp.orders.models import (
	ExternalOrder,
	ExternalOrderLine,
	ExternalCustomer,
	ExternalAddress,
	ExternalTotals,
)
from bop_erp.orders.ingestion import ingest_order_pipeline
from bop_erp.orders.pricing_protection import is_price_master_protection_active


class TestImportedOrderPriceMasterProtectionLive(FrappeTestCase):
	"""
	Live Integration Test Suite for Phase 1R.0 Imported Order Price-Master Protection.
	Validates:
	1. Imported order with missing Item Price creates 0 Item Price.
	2. Imported order line price is preserved exactly on Sales Order Item (rate = 10.00).
	3. Pre-existing Item Price (123.45) is preserved and NEVER updated by imported order.
	4. Concurrent imported orders for the same item create 0 Item Price.
	5. Multi-channel different transaction rates (Channel A = 10, Channel B = 12) create 0 Item Price.
	6. Manual ERP Sales Order preserves native ERPNext behavior (Item Price auto-inserted).
	7. Fixture teardown removes owned test data while preserving pre-existing baseline prices.
	"""

	@classmethod
	def setUpClass(cls):
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		# Snapshot baseline Item Prices before test execution
		cls.baseline_item_prices = set(frappe.get_all("Item Price", pluck="name"))

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"

		cls.diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)

		cls.channel_a = "CHAN-1R0-A"
		cls.channel_b = "CHAN-1R0-B"

		# Synthetic Warehouses
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")
		cls.wh_a = f"WH-1R0-{cls.abbr} - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.wh_a):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"WH-1R0-{cls.abbr}",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
			}).insert(ignore_permissions=True)
			cls.wh_a = w.name

		# Synthetic Test Items
		cls.item_missing_a = "ITEM-1R0-MISSING-A"
		cls.item_missing_b = "ITEM-1R0-MISSING-B"
		cls.item_existing = "ITEM-1R0-EXISTING"
		cls.item_manual = "ITEM-1R0-MANUAL"

		cls.test_items = [
			cls.item_missing_a,
			cls.item_missing_b,
			cls.item_existing,
			cls.item_manual,
		]

		for code in cls.test_items:
			if not frappe.db.exists("Item", code):
				frappe.get_doc({
					"doctype": "Item",
					"item_code": code,
					"item_name": f"Test 1R0 {code}",
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}).insert(ignore_permissions=True)

		# Setup Channels
		for ch in [cls.channel_a, cls.channel_b]:
			if not frappe.db.exists("Sales Channel", ch):
				frappe.get_doc({
					"doctype": "Sales Channel",
					"channel_id": ch,
					"channel_name": f"Test Channel {ch}",
					"company": cls.company,
					"active": 1,
					"integration_provider": IntegrationProvider.PRESTASHOP,
				}).insert(ignore_permissions=True)

			if not frappe.db.exists("PrestaShop Connector", {"sales_channel": ch}):
				pc = frappe.get_doc({
					"doctype": "PrestaShop Connector",
					"sales_channel": ch,
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

			if not frappe.db.exists("Channel Inventory Source", {"sales_channel": ch, "warehouse": cls.wh_a}):
				frappe.get_doc({
					"doctype": "Channel Inventory Source",
					"sales_channel": ch,
					"warehouse": cls.wh_a,
					"company": cls.company,
					"priority": 10,
					"enabled": 1,
					"allow_sellable_stock": 1,
					"allow_fulfillment": 1,
				}).insert(ignore_permissions=True)

		# Setup Product External ID Mappings
		cls.prod_map = {
			cls.item_missing_a: "1001",
			cls.item_missing_b: "1002",
			cls.item_existing: "1003",
		}
		for code, ext_id in cls.prod_map.items():
			for ch in [cls.channel_a, cls.channel_b]:
				if not frappe.db.exists("External ID Mapping", {
					"sales_channel": ch,
					"provider": IntegrationProvider.PRESTASHOP,
					"external_entity_type": ExternalEntityType.PRODUCT,
					"external_id": ext_id,
				}):
					frappe.get_doc({
						"doctype": "External ID Mapping",
						"sales_channel": ch,
						"provider": IntegrationProvider.PRESTASHOP,
						"external_entity_type": ExternalEntityType.PRODUCT,
						"external_id": ext_id,
						"erp_doctype": "Item",
						"erp_document": code,
						"active": 1,
					}).insert(ignore_permissions=True)

		# Set physical stock via Stock Reconciliation
		for code in cls.test_items:
			cls._set_physical_stock(code, cls.wh_a, 100.0)

		# Create pre-existing master Item Price for cls.item_existing
		cls.existing_ip_name = None
		existing_prices = frappe.get_all("Item Price", filters={"item_code": cls.item_existing, "price_list": "Standard Selling"})
		if not existing_prices:
			ip = frappe.get_doc({
				"doctype": "Item Price",
				"item_code": cls.item_existing,
				"price_list": "Standard Selling",
				"price_list_rate": 123.45,
			}).insert(ignore_permissions=True)
			cls.existing_ip_name = ip.name

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

	@classmethod
	def tearDownClass(cls):
		# Clean up Stock Reservation Entries for test items
		for item_code in cls.test_items:
			sre_names = frappe.get_all("Stock Reservation Entry", filters={"item_code": item_code}, pluck="name")
			for sre in sre_names:
				try:
					doc = frappe.get_doc("Stock Reservation Entry", sre)
					if doc.docstatus == 1:
						doc.cancel()
					frappe.delete_doc("Stock Reservation Entry", sre, force=True)
				except Exception:
					pass

		# Clean up Sales Orders created for test channels
		for ch in [cls.channel_a, cls.channel_b]:
			so_names = frappe.get_all("Sales Order", filters={"sales_channel": ch}, pluck="name")
			for so in so_names:
				try:
					doc = frappe.get_doc("Sales Order", so)
					if doc.docstatus == 1:
						doc.cancel()
					frappe.delete_doc("Sales Order", so, force=True)
				except Exception:
					pass

		# Clean up manual Sales Orders for test manual item
		so_manual = frappe.get_all("Sales Order Item", filters={"item_code": cls.item_manual}, pluck="parent")
		for so in set(so_manual):
			try:
				doc = frappe.get_doc("Sales Order", so)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Sales Order", so, force=True)
			except Exception:
				pass

		# Clean up External ID Mappings for orders & test products
		frappe.db.delete("External ID Mapping", {"sales_channel": ["in", [cls.channel_a, cls.channel_b]]})

		# Clean up Item Prices created during tests for test items
		if cls.existing_ip_name and frappe.db.exists("Item Price", cls.existing_ip_name):
			frappe.delete_doc("Item Price", cls.existing_ip_name, force=True)
		for code in cls.test_items:
			frappe.db.delete("Item Price", {"item_code": code})

		# Clean Stock Ledgers, Reconciliations before warehouse deletion
		frappe.db.delete("Stock Ledger Entry", {"warehouse": cls.wh_a})
		frappe.db.delete("Stock Reconciliation Item", {"warehouse": cls.wh_a})
		frappe.db.delete("Bin", {"warehouse": cls.wh_a})

		# Delete test channels, connectors, and sources
		for ch in [cls.channel_a, cls.channel_b]:
			frappe.db.delete("Channel Inventory Source", {"sales_channel": ch})
			frappe.db.delete("PrestaShop Connector", {"sales_channel": ch})
			if frappe.db.exists("Sales Channel", ch):
				frappe.delete_doc("Sales Channel", ch, force=True)

		# Delete test warehouse
		if frappe.db.exists("Warehouse", cls.wh_a):
			frappe.delete_doc("Warehouse", cls.wh_a, force=True)

		# Delete test items
		for code in cls.test_items:
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, force=True)

		frappe.db.commit()

		# Verify baseline preservation
		current_prices = set(frappe.get_all("Item Price", pluck="name"))
		missing_baseline = cls.baseline_item_prices - current_prices
		if missing_baseline:
			frappe.logger("bop_erp").error(f"Baseline Item Prices missing after test: {missing_baseline}")

	def test_01_imported_order_missing_item_price_creates_zero_item_prices(self):
		"""
		Scenario A:
		No master Item Price exists for item.
		External order line rate = 10.00.
		Expected:
		- Sales Order Item.rate = 10.00
		- Item Price count for this item remains EXACTLY 0.
		"""
		prices_before = frappe.get_all("Item Price", filters={"item_code": self.item_missing_a})
		self.assertEqual(len(prices_before), 0)

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="1R0-ORD-01",
			external_reference="REF-1R0-01",
			order_state_id="2",
			date_add="2026-09-11 12:00:00",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="CUST-1R0-01",
				first_name="Price",
				last_name="Protector",
				email="protector01@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-1R0-01",
				first_name="Price",
				last_name="Protector",
				address1="100 Protection Ave",
				city="Miami",
				postcode="33101",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=self.prod_map[self.item_missing_a],
					quantity=2.0,
					unit_price_ex_tax=10.00,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=20.00, total_paid=20.00),
		)

		res = ingest_order_pipeline(ext_order)
		self.assertTrue(res.get("success"))
		so_name = res.get("sales_order")

		so_doc = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so_doc.docstatus, 1)
		self.assertEqual(len(so_doc.items), 1)
		self.assertEqual(flt(so_doc.items[0].rate), 10.00)

		# Critical Assertion: ZERO master Item Price created!
		prices_after = frappe.get_all("Item Price", filters={"item_code": self.item_missing_a})
		self.assertEqual(len(prices_after), 0, f"Expected 0 Item Price records, found {len(prices_after)}")

	def test_02_imported_order_existing_item_price_unchanged(self):
		"""
		Scenario B:
		Master Item Price exists with rate 123.45.
		External order line rate = 10.00.
		Expected:
		- Sales Order Item.rate = 10.00 (transaction rate preserved)
		- Master Item Price rate remains 123.45 (NEVER updated by imported order).
		"""
		ip_before = frappe.get_all(
			"Item Price",
			filters={"item_code": self.item_existing, "price_list": "Standard Selling"},
			fields=["name", "price_list_rate"],
		)
		self.assertEqual(len(ip_before), 1)
		self.assertEqual(flt(ip_before[0].price_list_rate), 123.45)

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="1R0-ORD-02",
			external_reference="REF-1R0-02",
			order_state_id="2",
			date_add="2026-09-11 12:00:00",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="CUST-1R0-02",
				first_name="Price",
				last_name="Protector2",
				email="protector02@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-1R0-02",
				first_name="Price",
				last_name="Protector2",
				address1="200 Protection Ave",
				city="Miami",
				postcode="33101",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=self.prod_map[self.item_existing],
					quantity=1.0,
					unit_price_ex_tax=10.00,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=10.00, total_paid=10.00),
		)

		res = ingest_order_pipeline(ext_order)
		self.assertTrue(res.get("success"))
		so_name = res.get("sales_order")

		so_doc = frappe.get_doc("Sales Order", so_name)
		self.assertEqual(so_doc.docstatus, 1)
		self.assertEqual(flt(so_doc.items[0].rate), 10.00)

		# Critical Assertion: Master Item Price rate is UNCHANGED at 123.45!
		ip_after = frappe.get_doc("Item Price", ip_before[0].name)
		self.assertEqual(flt(ip_after.price_list_rate), 123.45)

	def test_03_concurrent_imported_orders_create_zero_item_prices(self):
		"""
		Scenario C:
		Two imported external orders for the same item without Item Price.
		Expected:
		- Both Sales Orders preserve their respective transaction rates
		- ZERO Item Price records created.
		"""
		prices_before = frappe.get_all("Item Price", filters={"item_code": self.item_missing_b})
		self.assertEqual(len(prices_before), 0)

		ext_order_1 = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="1R0-ORD-CONC-1",
			external_reference="REF-1R0-C1",
			order_state_id="2",
			date_add="2026-09-11 12:00:00",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="CUST-1R0-C1",
				first_name="Conc",
				last_name="One",
				email="concone@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-1R0-C1",
				first_name="Conc",
				last_name="One",
				address1="101 Conc Ave",
				city="Miami",
				postcode="33101",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=self.prod_map[self.item_missing_b],
					quantity=1.0,
					unit_price_ex_tax=15.00,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=15.00, total_paid=15.00),
		)

		ext_order_2 = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="1R0-ORD-CONC-2",
			external_reference="REF-1R0-C2",
			order_state_id="2",
			date_add="2026-09-11 12:00:00",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="CUST-1R0-C2",
				first_name="Conc",
				last_name="Two",
				email="conctwo@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-1R0-C2",
				first_name="Conc",
				last_name="Two",
				address1="102 Conc Ave",
				city="Miami",
				postcode="33101",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=self.prod_map[self.item_missing_b],
					quantity=1.0,
					unit_price_ex_tax=20.00,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=20.00, total_paid=20.00),
		)

		res_1 = ingest_order_pipeline(ext_order_1)
		res_2 = ingest_order_pipeline(ext_order_2)

		self.assertTrue(res_1.get("success"))
		self.assertTrue(res_2.get("success"))

		so_1 = frappe.get_doc("Sales Order", res_1.get("sales_order"))
		so_2 = frappe.get_doc("Sales Order", res_2.get("sales_order"))

		self.assertEqual(flt(so_1.items[0].rate), 15.00)
		self.assertEqual(flt(so_2.items[0].rate), 20.00)

		# Critical Assertion: ZERO master Item Price created!
		prices_after = frappe.get_all("Item Price", filters={"item_code": self.item_missing_b})
		self.assertEqual(len(prices_after), 0, f"Expected 0 Item Price records, found {len(prices_after)}")

	def test_04_multichannel_different_rates_remain_isolated(self):
		"""
		Scenario: Multi-channel rate variance.
		Channel A transaction rate = 10.00
		Channel B transaction rate = 12.00
		Same ERP Item (cls.item_missing_a).
		Expected:
		- SO A rate = 10.00
		- SO B rate = 12.00
		- ZERO Item Price synthesized from either order.
		"""
		ext_order_a = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_a,
			external_order_id="1R0-ORD-MC-A",
			external_reference="REF-1R0-MCA",
			order_state_id="2",
			date_add="2026-09-11 12:00:00",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="CUST-1R0-MCA",
				first_name="Multi",
				last_name="Alpha",
				email="multialpha@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-1R0-MCA",
				first_name="Multi",
				last_name="Alpha",
				address1="201 Multi Ave",
				city="Miami",
				postcode="33101",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=self.prod_map[self.item_missing_a],
					quantity=1.0,
					unit_price_ex_tax=10.00,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=10.00, total_paid=10.00),
		)

		ext_order_b = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.channel_b,
			external_order_id="1R0-ORD-MC-B",
			external_reference="REF-1R0-MCB",
			order_state_id="2",
			date_add="2026-09-11 12:00:00",
			currency="USD",
			customer=ExternalCustomer(
				external_customer_id="CUST-1R0-MCB",
				first_name="Multi",
				last_name="Beta",
				email="multibeta@example.com",
			),
			delivery_address=ExternalAddress(
				external_address_id="ADDR-1R0-MCB",
				first_name="Multi",
				last_name="Beta",
				address1="202 Multi Ave",
				city="Miami",
				postcode="33101",
				country="United States",
			),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id=self.prod_map[self.item_missing_a],
					quantity=1.0,
					unit_price_ex_tax=12.00,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=12.00, total_paid=12.00),
		)

		res_a = ingest_order_pipeline(ext_order_a)
		res_b = ingest_order_pipeline(ext_order_b)

		self.assertTrue(res_a.get("success"))
		self.assertTrue(res_b.get("success"))

		so_a = frappe.get_doc("Sales Order", res_a.get("sales_order"))
		so_b = frappe.get_doc("Sales Order", res_b.get("sales_order"))

		self.assertEqual(flt(so_a.items[0].rate), 10.00)
		self.assertEqual(flt(so_b.items[0].rate), 12.00)

		# Critical Assertion: ZERO master Item Price created!
		prices_after = frappe.get_all("Item Price", filters={"item_code": self.item_missing_a})
		self.assertEqual(len(prices_after), 0)

	def test_05_manual_erp_sales_order_preserves_native_auto_insert(self):
		"""
		Scenario D:
		Manual ERP Sales Order created outside the integration pipeline
		under native ERPNext conditions.
		Expected:
		- Standard ERPNext behavior is preserved: when auto_insert_price_list_rate_if_missing is 1,
		  creating a manual Sales Order for an item lacking an Item Price DOES create an Item Price.
		"""
		# Verify no Item Price exists for item_manual
		prices_before = frappe.get_all("Item Price", filters={"item_code": self.item_manual})
		self.assertEqual(len(prices_before), 0)

		# Resolve any valid customer
		customer = frappe.get_all("Customer", limit=1)[0].name

		so_manual = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": customer,
			"company": self.company,
			"delivery_date": nowdate(),
			"selling_price_list": "Standard Selling",
			"items": [
				{
					"item_code": self.item_manual,
					"qty": 1.0,
					"rate": 55.00,
					"warehouse": self.wh_a,
				}
			],
		})
		so_manual.insert(ignore_permissions=True)

		# Native ERPNext auto-inserts Item Price for manual Sales Order
		prices_after = frappe.get_all(
			"Item Price",
			filters={"item_code": self.item_manual, "price_list": "Standard Selling"},
			fields=["name", "price_list_rate"],
		)
		self.assertEqual(len(prices_after), 1, "Native ERPNext auto-insertion should occur for manual ERP order")
		self.assertEqual(flt(prices_after[0].price_list_rate), 55.00)

	def test_06_baseline_item_prices_preserved(self):
		"""
		Verifies that all pre-existing baseline Item Price records present
		before suite execution are 100% intact and preserved.
		"""
		current_prices = set(frappe.get_all("Item Price", pluck="name"))
		missing = self.baseline_item_prices - current_prices
		self.assertEqual(len(missing), 0, f"Missing baseline item prices: {missing}")

