# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import urllib.request
import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	ExternalEntityType,
)
from bop_erp.safety import IntegrationEnvironment
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.sync import run_prestashop_read_sync_job
from bop_erp.integrations.prestashop.adapters.normalizers import (
	normalize_category,
	normalize_product,
	normalize_combination,
	normalize_stock,
	normalize_customer,
	normalize_address,
	normalize_order,
	normalize_order_line,
)


def get_live_test_prestashop_url() -> str:
	"""Determines accessible local PrestaShop endpoint within the execution environment."""
	candidates = ["http://prestashop-test", "http://127.0.0.1:8082", "http://localhost:8082"]
	for url in candidates:
		try:
			with urllib.request.urlopen(f"{url}/api/categories?limit=1", timeout=2) as resp:
				if resp.status in (200, 401, 403):
					return url
		except Exception:
			continue
	return "http://prestashop-test"


class TestPrestaShopConnectorLive(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.base_url = get_live_test_prestashop_url()
		cls.sales_channel = "LIVE-TEST-TID"
		cls.cred_ref = "TEST_PRESTASHOP_KEY"

		# Ensure test sales channel exists
		if not frappe.db.exists("Sales Channel", cls.sales_channel):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.sales_channel,
				"channel_name": "Live Test Channel",
				"channel_type": "PRESTASHOP",
				"active": 1,
			}).insert(ignore_permissions=True)

		# Ensure PrestaShop Connector doc exists
		conn_name = frappe.db.get_value("PrestaShop Connector", {"sales_channel": cls.sales_channel}, "name")
		if not conn_name:
			cls.connector = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": cls.sales_channel,
				"environment": "DEVELOPMENT",
				"base_url": cls.base_url,
				"credential_reference": cls.cred_ref,
				"enabled": 1,
				"read_enabled": 1,
				"write_enabled": 0,
			}).insert(ignore_permissions=True)
		else:
			cls.connector = frappe.get_doc("PrestaShop Connector", conn_name)
			cls.connector.base_url = cls.base_url
			cls.connector.save(ignore_permissions=True)

		cls.config = PrestaShopConfig(
			sales_channel=cls.sales_channel,
			environment="DEVELOPMENT",
			base_url=cls.base_url,
			credential_reference=cls.cred_ref,
		)
		cls.client = PrestaShopClient(config=cls.config)

	@classmethod
	def tearDownClass(cls):
		conn_name = frappe.db.get_value("PrestaShop Connector", {"sales_channel": cls.sales_channel}, "name")
		if conn_name:
			frappe.delete_doc("PrestaShop Connector", conn_name, ignore_permissions=True)
		if frappe.db.exists("Sales Channel", cls.sales_channel):
			frappe.delete_doc("Sales Channel", cls.sales_channel, ignore_permissions=True)
		super().tearDownClass()

	def test_01_health_check(self):
		"""Live health check against local PrestaShop must succeed."""
		is_healthy = self.client.health_check()
		self.assertTrue(is_healthy)

		# Test DocType run_health_check method
		res = self.connector.run_health_check()
		self.assertTrue(res.get("success"))
		self.assertEqual(res.get("status"), "HEALTHY")

	def test_02_list_and_read_categories(self):
		"""Lists categories and normalizes a representative category."""
		categories = self.client.list_categories(limit=20)
		self.assertGreater(len(categories), 0)

		cat_id = categories[0]["id"]
		raw_cat = self.client.get_category(cat_id)
		cat = normalize_category(raw_cat)
		self.assertEqual(cat.external_id, str(cat_id))
		self.assertTrue(len(cat.name) > 0)

	def test_03_list_and_read_products_simple_and_variant(self):
		"""Reads products and verifies normalization of simple and variant products."""
		products = self.client.list_products(limit=50)
		self.assertGreaterEqual(len(products), 20)

		# Fetch full details of first 5 products to find simple and variant items
		simple_found = False
		variant_found = False

		for p in products:
			raw_prod = self.client.get_product(p["id"])
			norm_prod = normalize_product(raw_prod)
			self.assertEqual(norm_prod.external_id, str(p["id"]))
			self.assertTrue(len(norm_prod.name) > 0)
			if norm_prod.combination_ids:
				variant_found = True
			else:
				simple_found = True
			if simple_found and variant_found:
				break

		self.assertTrue(simple_found, "Expected at least one simple product in catalog")
		self.assertTrue(variant_found, "Expected at least one variant product with combinations")

	def test_04_list_and_read_combinations(self):
		"""Reads combinations and verifies exact combination IDs and parent linkage."""
		combinations = self.client.list_combinations(limit=10)
		self.assertGreater(len(combinations), 0)

		comb_id = combinations[0]["id"]
		raw_comb = self.client.get_combination(comb_id)
		comb = normalize_combination(raw_comb)
		self.assertEqual(comb.external_id, str(comb_id))
		self.assertTrue(len(comb.parent_product_id) > 0)

	def test_05_read_stock_availables(self):
		"""Reads stock_available records and verifies quantities."""
		stocks = self.client.list_stock_availables(limit=10)
		self.assertGreater(len(stocks), 0)

		stock_id = stocks[0]["id"]
		raw_stock = self.client.get_stock_available(stock_id)
		stock = normalize_stock(raw_stock)
		self.assertEqual(stock.external_id, str(stock_id))
		self.assertTrue(len(stock.product_id) > 0)
		self.assertIsInstance(stock.quantity, int)

	def test_06_list_and_read_customers(self):
		"""Reads synthetic customers and validates normalized customer model."""
		customers = self.client.list_customers(limit=10)
		self.assertGreater(len(customers), 0)

		cust_id = customers[0]["id"]
		raw_cust = self.client.get_customer(cust_id)
		cust = normalize_customer(raw_cust)
		self.assertEqual(cust.external_id, str(cust_id))
		self.assertIn("@", cust.email)

	def test_07_list_and_read_addresses(self):
		"""Reads synthetic addresses and validates normalized address model."""
		addresses = self.client.list_addresses(limit=10)
		self.assertGreater(len(addresses), 0)

		addr_id = addresses[0]["id"]
		raw_addr = self.client.get_address(addr_id)
		addr = normalize_address(raw_addr)
		self.assertEqual(addr.external_id, str(addr_id))
		self.assertTrue(len(addr.address1) > 0)

	def test_08_list_and_read_orders_and_lines(self):
		"""Reads synthetic orders and order details."""
		orders = self.client.list_orders(limit=10)
		self.assertGreater(len(orders), 0)

		order_id = orders[0]["id"]
		raw_order = self.client.get_order(order_id)
		order = normalize_order(raw_order)
		self.assertEqual(order.external_id, str(order_id))
		self.assertTrue(len(order.reference) > 0)
		self.assertGreater(order.total_paid, 0.0)

		details = self.client.get_order_details(order_id)
		self.assertIsInstance(details, list)
		if details:
			line = normalize_order_line(details[0])
			self.assertTrue(len(line.product_id) > 0)

	def test_09_pagination_across_multiple_pages(self):
		"""Tests pagination with limit and offset across multiple pages ensuring non-overlapping sets."""
		page1 = self.client.list_products(limit=3, offset=0)
		page2 = self.client.list_products(limit=3, offset=3)

		self.assertEqual(len(page1), 3)
		self.assertEqual(len(page2), 3)

		ids_page1 = {p["id"] for p in page1}
		ids_page2 = {p["id"] for p in page2}

		self.assertEqual(len(ids_page1.intersection(ids_page2)), 0, "Pages must not overlap")

	def test_10_integration_event_read_sync_lifecycle(self):
		"""Executes a read sync job through the Integration Event lifecycle and validates SUCCEEDED state."""
		result = run_prestashop_read_sync_job(
			sales_channel=self.sales_channel,
			entity_type=ExternalEntityType.PRODUCT,
			worker_id="WORKER-LIVE-TEST",
			limit=5,
		)
		self.assertEqual(result["status"], "SUCCESS")
		self.assertEqual(result["count"], 5)

		event_name = result["event_name"]
		event = frappe.get_doc("Integration Event", event_name)
		self.assertEqual(event.status, IntegrationStatus.SUCCEEDED)
		self.assertEqual(event.provider, IntegrationProvider.PRESTASHOP)
		self.assertEqual(event.sales_channel, self.sales_channel)
		self.assertEqual(event.entity_type, ExternalEntityType.PRODUCT)

		# Clean up test event
		frappe.delete_doc("Integration Event", event_name, ignore_permissions=True)
