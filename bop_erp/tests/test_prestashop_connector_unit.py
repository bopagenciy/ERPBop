# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from unittest.mock import MagicMock, patch
import requests
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.translate import get_translations_from_apps

from bop_erp.constants import (
	ErrorCategory,
	ExternalEntityType,
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
)
from bop_erp.safety import (
	IntegrationEnvironment,
	assert_safe_connector_target,
	is_forbidden_production_host,
	sanitize_url_for_logging,
)
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopError,
	PrestaShopAuthError,
	PrestaShopNotFoundError,
	PrestaShopRateLimitError,
	PrestaShopValidationError,
	PrestaShopServerError,
	PrestaShopTransientError,
	PrestaShopMalformedResponseError,
	mask_sensitive_strings,
)
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.client import PrestaShopClient, MAX_PAGE_SIZE
from bop_erp.integrations.prestashop.sync import map_prestashop_exception_to_error_category
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


class TestPrestaShopConnectorUnit(FrappeTestCase):
	def setUp(self):
		super().setUp()
		# Ensure test sales channel exists
		if not frappe.db.exists("Sales Channel", "UNIT-TEST-TID"):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": "UNIT-TEST-TID",
				"channel_name": "Unit Test TID Channel",
				"channel_type": "PRESTASHOP",
				"active": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("Sales Channel", "UNIT-TEST-BAMAL"):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": "UNIT-TEST-BAMAL",
				"channel_name": "Unit Test BAMAL Channel",
				"channel_type": "PRESTASHOP",
				"active": 1,
			}).insert(ignore_permissions=True)

	def tearDown(self):
		for ch in ("UNIT-TEST-TID", "UNIT-TEST-BAMAL"):
			conn_name = frappe.db.get_value("PrestaShop Connector", {"sales_channel": ch}, "name")
			if conn_name:
				frappe.delete_doc("PrestaShop Connector", conn_name, ignore_permissions=True)
			if frappe.db.exists("Sales Channel", ch):
				frappe.delete_doc("Sales Channel", ch, ignore_permissions=True)
		super().tearDown()

	def test_production_target_rejected(self):
		"""Production host must be strictly rejected with ValidationError."""
		forbidden_hosts = [
			"https://theindustrialdepot.com",
			"https://www.theindustrialdepot.com",
			"https://api.theindustrialdepot.com",
			"http://theindustrialdepot.com:8080",
		]
		for url in forbidden_hosts:
			with self.assertRaises(frappe.ValidationError):
				assert_safe_connector_target(
					environment=IntegrationEnvironment.DEVELOPMENT,
					base_url=url,
				)

	def test_phase_1d_blocks_production_environment(self):
		"""Phase 1D explicitly disables PRODUCTION environment execution."""
		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target(
				environment=IntegrationEnvironment.PRODUCTION,
				base_url="http://127.0.0.1:8082",
			)

	def test_local_target_accepted(self):
		"""Local development targets must be permitted."""
		safe_urls = [
			"http://127.0.0.1:8082",
			"http://localhost:8082",
			"http://192.168.1.100:8082",
		]
		for url in safe_urls:
			self.assertTrue(
				assert_safe_connector_target(
					environment=IntegrationEnvironment.DEVELOPMENT,
					base_url=url,
				)
			)

	def test_connector_doctype_rejects_raw_credential_key(self):
		"""PrestaShop Connector DocType must reject raw 32-char secret keys stored as reference."""
		raw_key = "abcdef1234567890abcdef1234567890"
		doc = frappe.get_doc({
			"doctype": "PrestaShop Connector",
			"sales_channel": "UNIT-TEST-TID",
			"environment": "DEVELOPMENT",
			"base_url": "http://127.0.0.1:8082",
			"credential_reference": raw_key,
			"enabled": 1,
		})
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_connector_doctype_rejects_write_enabled_in_phase_1d(self):
		"""Write operations must be strictly rejected in Phase 1D."""
		doc = frappe.get_doc({
			"doctype": "PrestaShop Connector",
			"sales_channel": "UNIT-TEST-TID",
			"environment": "DEVELOPMENT",
			"base_url": "http://127.0.0.1:8082",
			"credential_reference": "TEST_PRESTASHOP_KEY",
			"write_enabled": 1,
		})
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_credential_sanitization(self):
		"""API keys must never be exposed in URLs, logs, or exception messages."""
		raw_key = "SECRETAPIKEY12345678901234567890"
		url_with_key = f"http://{raw_key}:@127.0.0.1:8082/api/products"
		sanitized_url = sanitize_url_for_logging(url_with_key)
		self.assertNotIn(raw_key, sanitized_url)
		self.assertIn("***", sanitized_url)

		msg = f"Failed to authenticate with key {raw_key} on server"
		masked = mask_sensitive_strings(msg, [raw_key])
		self.assertNotIn(raw_key, masked)
		self.assertIn("***", masked)

		exc = PrestaShopAuthError(msg, sensitive_token=raw_key)
		self.assertNotIn(raw_key, str(exc))
		self.assertNotIn(raw_key, repr(exc))

	def test_timeout_handling(self):
		"""Network timeouts must raise PrestaShopTransientError and map to TRANSIENT."""
		config = PrestaShopConfig(
			sales_channel="UNIT-TEST-TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_PRESTASHOP_KEY",
			timeout_seconds=5,
		)
		mock_session = MagicMock()
		mock_session.request.side_effect = requests.Timeout("Connection timed out")

		client = PrestaShopClient(config=config, session=mock_session)
		with self.assertRaises(PrestaShopTransientError):
			client.get_product(1)

		try:
			client.get_product(1)
		except Exception as e:
			err_cat, err_code = map_prestashop_exception_to_error_category(e)
			self.assertEqual(err_cat, ErrorCategory.TRANSIENT)
			self.assertEqual(err_code, "PS_NETWORK_TIMEOUT")

	def test_auth_error_classification(self):
		"""HTTP 401/403 must raise PrestaShopAuthError and map to AUTHENTICATION."""
		config = PrestaShopConfig(
			sales_channel="UNIT-TEST-TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_PRESTASHOP_KEY",
		)
		mock_session = MagicMock()
		mock_resp = MagicMock()
		mock_resp.status_code = 403
		mock_resp.text = '{"errors":[{"code":132,"message":"No permission"}]}'
		mock_session.request.return_value = mock_resp

		client = PrestaShopClient(config=config, session=mock_session)
		with self.assertRaises(PrestaShopAuthError):
			client.get_product(1)

		try:
			client.get_product(1)
		except Exception as e:
			err_cat, err_code = map_prestashop_exception_to_error_category(e)
			self.assertEqual(err_cat, ErrorCategory.AUTHENTICATION)
			self.assertEqual(err_code, "PS_AUTH_FAILED")

	def test_not_found_classification(self):
		"""HTTP 404 must raise PrestaShopNotFoundError and map to NOT_FOUND."""
		config = PrestaShopConfig(
			sales_channel="UNIT-TEST-TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_PRESTASHOP_KEY",
		)
		mock_session = MagicMock()
		mock_resp = MagicMock()
		mock_resp.status_code = 404
		mock_resp.text = '{"errors":[{"code":32,"message":"Resource not found"}]}'
		mock_session.request.return_value = mock_resp

		client = PrestaShopClient(config=config, session=mock_session)
		with self.assertRaises(PrestaShopNotFoundError):
			client.get_product(9999)

		try:
			client.get_product(9999)
		except Exception as e:
			err_cat, err_code = map_prestashop_exception_to_error_category(e)
			self.assertEqual(err_cat, ErrorCategory.NOT_FOUND)
			self.assertEqual(err_code, "PS_NOT_FOUND")

	def test_server_error_classification(self):
		"""HTTP 500 must raise PrestaShopServerError and map to PROVIDER_ERROR."""
		config = PrestaShopConfig(
			sales_channel="UNIT-TEST-TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_PRESTASHOP_KEY",
		)
		mock_session = MagicMock()
		mock_resp = MagicMock()
		mock_resp.status_code = 500
		mock_resp.text = "Internal Server Error"
		mock_session.request.return_value = mock_resp

		client = PrestaShopClient(config=config, session=mock_session)
		with self.assertRaises(PrestaShopServerError):
			client.get_product(1)

		try:
			client.get_product(1)
		except Exception as e:
			err_cat, err_code = map_prestashop_exception_to_error_category(e)
			self.assertEqual(err_cat, ErrorCategory.PROVIDER_ERROR)
			self.assertEqual(err_code, "PS_SERVER_ERROR")

	def test_malformed_response_classification(self):
		"""Malformed JSON must raise PrestaShopMalformedResponseError and map to INTERNAL_ERROR."""
		config = PrestaShopConfig(
			sales_channel="UNIT-TEST-TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_PRESTASHOP_KEY",
		)
		mock_session = MagicMock()
		mock_resp = MagicMock()
		mock_resp.status_code = 200
		mock_resp.text = "<html>PHP Fatal error: array_filter()</html>"
		mock_resp.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
		mock_session.request.return_value = mock_resp

		client = PrestaShopClient(config=config, session=mock_session)
		with self.assertRaises(PrestaShopMalformedResponseError):
			client.get_product(1)

		try:
			client.get_product(1)
		except Exception as e:
			err_cat, err_code = map_prestashop_exception_to_error_category(e)
			self.assertEqual(err_cat, ErrorCategory.INTERNAL_ERROR)
			self.assertEqual(err_code, "PS_MALFORMED_RESPONSE")

	def test_pagination_bounding(self):
		"""Client must bound requested limit to MAX_PAGE_SIZE."""
		config = PrestaShopConfig(
			sales_channel="UNIT-TEST-TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_PRESTASHOP_KEY",
		)
		mock_session = MagicMock()
		mock_resp = MagicMock()
		mock_resp.status_code = 200
		mock_resp.json.return_value = {"products": []}
		mock_session.request.return_value = mock_resp

		client = PrestaShopClient(config=config, session=mock_session)
		# Request excessively large page size (1000)
		client.list_products(limit=1000, offset=0)

		# Verify passed params were capped
		call_args = mock_session.request.call_args
		params = call_args[1].get("params", {})
		self.assertEqual(params.get("limit"), f"0,{MAX_PAGE_SIZE}")

	def test_domain_adapter_normalization(self):
		"""Normalizers must extract exact external IDs and normalized fields."""
		raw_cat = {
			"id": "10",
			"name": [{"id": "1", "value": "Industrial Fasteners"}],
			"description": "High-tensile bolts and nuts",
			"id_parent": "2",
			"active": "1",
		}
		cat = normalize_category(raw_cat)
		self.assertEqual(cat.external_id, "10")
		self.assertEqual(cat.name, "Industrial Fasteners")
		self.assertEqual(cat.parent_id, "2")
		self.assertTrue(cat.active)

		raw_prod = {
			"id": "25",
			"reference": "SKU-FAST-HEXBOLT-SS",
			"name": [{"id": "1", "value": "Stainless Steel Hex Bolt"}],
			"price": "5.500000",
			"wholesale_price": "2.200000",
			"id_category_default": "10",
			"active": "1",
			"associations": {
				"combinations": [{"id": "40"}, {"id": "41"}]
			},
		}
		prod = normalize_product(raw_prod)
		self.assertEqual(prod.external_id, "25")
		self.assertEqual(prod.sku, "SKU-FAST-HEXBOLT-SS")
		self.assertEqual(prod.name, "Stainless Steel Hex Bolt")
		self.assertEqual(prod.price, 5.5)
		self.assertEqual(prod.combination_ids, ["40", "41"])

		raw_comb = {
			"id": "40",
			"id_product": "25",
			"reference": "SKU-FAST-HEXBOLT-SS-M6X20",
			"price": "0.000000",
			"quantity": "500",
			"associations": {
				"product_option_values": [{"id": "1"}, {"id": "2"}]
			}
		}
		comb = normalize_combination(raw_comb)
		self.assertEqual(comb.external_id, "40")
		self.assertEqual(comb.parent_product_id, "25")
		self.assertEqual(comb.sku, "SKU-FAST-HEXBOLT-SS-M6X20")
		self.assertEqual(comb.quantity, 500)

		raw_stock = {
			"id": "150",
			"id_product": "25",
			"id_product_attribute": "40",
			"quantity": "500",
		}
		stock = normalize_stock(raw_stock)
		self.assertEqual(stock.external_id, "150")
		self.assertEqual(stock.product_id, "25")
		self.assertEqual(stock.combination_id, "40")
		self.assertEqual(stock.quantity, 500)

		raw_order = {
			"id": "7",
			"reference": "SYNTH-ORD",
			"id_customer": "3",
			"id_address_delivery": "7",
			"id_address_invoice": "7",
			"current_state": "2",
			"total_paid": "249.900000",
			"total_products": "249.900000",
		}
		order = normalize_order(raw_order)
		self.assertEqual(order.external_id, "7")
		self.assertEqual(order.reference, "SYNTH-ORD")
		self.assertEqual(order.customer_id, "3")
		self.assertEqual(order.order_state_id, "2")
		self.assertEqual(order.total_paid, 249.9)

	def test_multi_channel_connector_isolation(self):
		"""Two distinct sales channels must have separate independent connectors."""
		conn1 = frappe.get_doc({
			"doctype": "PrestaShop Connector",
			"sales_channel": "UNIT-TEST-TID",
			"environment": "DEVELOPMENT",
			"base_url": "http://127.0.0.1:8082",
			"credential_reference": "TEST_PRESTASHOP_KEY",
			"enabled": 1,
		}).insert(ignore_permissions=True)

		conn2 = frappe.get_doc({
			"doctype": "PrestaShop Connector",
			"sales_channel": "UNIT-TEST-BAMAL",
			"environment": "DEVELOPMENT",
			"base_url": "http://127.0.0.1:8082",
			"credential_reference": "TEST_PRESTASHOP_KEY",
			"enabled": 1,
		}).insert(ignore_permissions=True)

		self.assertNotEqual(conn1.name, conn2.name)
		self.assertEqual(conn1.sales_channel, "UNIT-TEST-TID")
		self.assertEqual(conn2.sales_channel, "UNIT-TEST-BAMAL")

		# Cannot insert a second connector for the same sales channel (unique constraint)
		with self.assertRaises(frappe.DuplicateEntryError):
			frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"sales_channel": "UNIT-TEST-TID",
				"environment": "DEVELOPMENT",
				"base_url": "http://127.0.0.1:8082",
				"credential_reference": "TEST_PRESTASHOP_KEY",
			}).insert(ignore_permissions=True)

	def test_spanish_translations_loaded(self):
		"""All new connector visible messages must be present in Spanish translations."""
		translations = get_translations_from_apps("es", ["bop_erp"])
		self.assertIn("PrestaShop Connector", translations)
		self.assertEqual(translations["PrestaShop Connector"], "Conector PrestaShop")
		self.assertIn("Credential Reference is required.", translations)
		self.assertIn("Write operations are strictly disabled in Phase 1D (Read-Only connector core).", translations)
		self.assertIn("Base URL is required to validate connector target.", translations)
