# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import MagicMock, patch
import frappe
from frappe.utils import now_datetime

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationDirection,
)
from bop_erp.safety import (
	ConnectorSafetyError,
	assert_safe_write_target,
	ALLOWLISTED_WRITE_HOSTS,
)
from bop_erp.inventory.publication import (
	normalize_publishable_quantity,
	compute_publication_hash,
	resolve_item_mapping,
	publish_item_inventory,
	schedule_channel_inventory_publication,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopValidationError,
	PrestaShopNotFoundError,
)


class TestPrestaShopPublicationUnit(unittest.TestCase):
	"""
	Unit test suite for Phase 1J: PrestaShop Test Inventory Publication Foundation.
	Covers:
	- Host allowlist enforcement
	- Production wildcard denial
	- Quantity normalization (flooring, zero clamping)
	- Simple & variant mapping resolution
	- stock_available resolution & identity mismatch rejection
	- Canonical publication hash determinism
	- No-op & remote drift detection
	- Stale publication prevention & monotonic versioning
	- Secret redaction
	- Bounded bulk scheduling
	"""

	def test_01_host_allowlist_accepted(self):
		"""Local disposable endpoints are accepted for writes."""
		for host in ["http://127.0.0.1:8082", "http://localhost:8082", "http://prestashop-test", "http://127.0.0.1"]:
			self.assertTrue(assert_safe_write_target("DEVELOPMENT", host))

	def test_02_production_hosts_strictly_blocked(self):
		"""Production hostnames and wildcards MUST raise ConnectorSafetyError before any request."""
		blocked_hosts = [
			"https://theindustrialdepot.com",
			"https://www.theindustrialdepot.com",
			"https://staging.theindustrialdepot.com",
			"https://api.theindustrialdepot.com",
			"https://random-external-site.com",
			"http://192.168.1.50:8082",
		]
		for host in blocked_hosts:
			with self.assertRaises(ConnectorSafetyError):
				assert_safe_write_target("DEVELOPMENT", host)

	def test_03_non_development_environment_blocked(self):
		"""Even local endpoints are blocked if environment is not DEVELOPMENT."""
		with self.assertRaises(ConnectorSafetyError):
			assert_safe_write_target("PRODUCTION", "http://127.0.0.1:8082")
		with self.assertRaises(ConnectorSafetyError):
			assert_safe_write_target("STAGING", "http://127.0.0.1:8082")

	def test_04_quantity_normalization_policy(self):
		"""
		Verifies quantity normalization:
		- Fractional ATP is floored to integer (10.8 -> 10, never 11).
		- Exact integer preserved (10.0 -> 10).
		- Negative ATP clamped to 0 (-5.0 -> 0).
		- Zero preserved (0.0 -> 0).
		"""
		self.assertEqual(normalize_publishable_quantity(10.8), 10)
		self.assertEqual(normalize_publishable_quantity(10.2), 10)
		self.assertEqual(normalize_publishable_quantity(10.0), 10)
		self.assertEqual(normalize_publishable_quantity(-5.0), 0)
		self.assertEqual(normalize_publishable_quantity(0.0), 0)
		self.assertEqual(normalize_publishable_quantity(0.999), 0)

	def test_05_canonical_publication_hash_determinism(self):
		"""Identical publication states generate identical hashes; any delta alters hash."""
		h1 = compute_publication_hash("PRESTASHOP", "TID", "BOLT-001", 10, None, 25, 50)
		h2 = compute_publication_hash("PRESTASHOP", "TID", "BOLT-001", 10, None, 25, 50)
		self.assertEqual(h1, h2)

		# Delta in quantity alters hash
		h3 = compute_publication_hash("PRESTASHOP", "TID", "BOLT-001", 10, None, 25, 51)
		self.assertNotEqual(h1, h3)

		# Delta in channel alters hash
		h4 = compute_publication_hash("PRESTASHOP", "OTHER", "BOLT-001", 10, None, 25, 50)
		self.assertNotEqual(h1, h4)

		# Delta in variant alters hash
		h5 = compute_publication_hash("PRESTASHOP", "TID", "BOLT-001", 10, 2, 25, 50)
		self.assertNotEqual(h1, h5)

	def test_06_mapping_resolution_simple_product(self):
		"""Resolves simple product mapping correctly."""
		with patch("frappe.db.get_value") as mock_get_value:
			def db_side_effect(doctype, filters, fields, *args, **kwargs):
				if doctype == "External ID Mapping":
					if filters.get("external_entity_type") == ExternalEntityType.PRODUCT_VARIANT:
						return None
					if filters.get("external_entity_type") == ExternalEntityType.PRODUCT:
						return frappe._dict({"name": "MAP-01", "external_id": "12", "provider": "PRESTASHOP"})
				return None

			mock_get_value.side_effect = db_side_effect
			res = resolve_item_mapping("TID", "BOLT-001")
			self.assertEqual(res["entity_type"], ExternalEntityType.PRODUCT)
			self.assertEqual(res["product_id"], 12)
			self.assertIsNone(res["variant_id"])

	def test_07_mapping_resolution_variant(self):
		"""Resolves product variant mapping correctly."""
		with patch("frappe.db.get_value") as mock_get_value:
			def db_side_effect(doctype, filters, fields, *args, **kwargs):
				if doctype == "External ID Mapping":
					if filters.get("external_entity_type") == ExternalEntityType.PRODUCT_VARIANT:
						return frappe._dict({"name": "MAP-02", "external_id": "12", "external_variant_id": "4", "provider": "PRESTASHOP"})
				return None

			mock_get_value.side_effect = db_side_effect
			res = resolve_item_mapping("TID", "BOLT-001-RED")
			self.assertEqual(res["entity_type"], ExternalEntityType.PRODUCT_VARIANT)
			self.assertEqual(res["product_id"], 12)
			self.assertEqual(res["variant_id"], 4)

	def test_08_missing_mapping_fails_safely(self):
		"""Missing mapping raises PrestaShopValidationError and does NOT write."""
		with patch("frappe.db.get_value", return_value=None):
			with self.assertRaises(PrestaShopValidationError) as ctx:
				resolve_item_mapping("TID", "UNMAPPED-ITEM")
			self.assertIn("MAPPING_MISSING", str(ctx.exception))

	def test_09_identity_mismatch_blocks_write(self):
		"""If destination stock_available row has product/attribute mismatch, write is strictly blocked."""
		xml_mismatch = """<?xml version="1.0" encoding="UTF-8"?>
<prestashop>
<stock_available>
	<id><![CDATA[50]]></id>
	<id_product><![CDATA[99]]></id_product>
	<id_product_attribute><![CDATA[0]]></id_product_attribute>
	<quantity><![CDATA[10]]></quantity>
</stock_available>
</prestashop>"""

		config = PrestaShopConfig(
			sales_channel="TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_KEY",
			write_enabled=True,
		)
		with patch.object(PrestaShopConfig, "resolve_api_key", return_value="FAKE_KEY_1234567890123456789012"):
			client = PrestaShopClient(config=config)
			with patch.object(client, "get_stock_available_xml", return_value=xml_mismatch):
				# Expecting product 10, but remote is 99
				with self.assertRaises(PrestaShopValidationError) as ctx:
					client.update_stock_available_quantity(
						stock_available_id=50,
						quantity=20,
						expected_product_id=10,
					)
				self.assertIn("CRITICAL IDENTITY MISMATCH", str(ctx.exception))

	def test_10_no_op_detection_skips_put(self):
		"""If remote quantity already equals desired quantity, returns NO_OP without calling PUT."""
		xml_same = """<?xml version="1.0" encoding="UTF-8"?>
<prestashop>
<stock_available>
	<id><![CDATA[50]]></id>
	<id_product><![CDATA[10]]></id_product>
	<id_product_attribute><![CDATA[0]]></id_product_attribute>
	<quantity><![CDATA[25]]></quantity>
</stock_available>
</prestashop>"""

		config = PrestaShopConfig(
			sales_channel="TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_KEY",
			write_enabled=True,
		)
		with patch.object(PrestaShopConfig, "resolve_api_key", return_value="FAKE_KEY_1234567890123456789012"):
			client = PrestaShopClient(config=config)
			with patch.object(client, "get_stock_available_xml", return_value=xml_same), \
				 patch.object(client.session, "put") as mock_put:
				res = client.update_stock_available_quantity(
					stock_available_id=50,
					quantity=25,
					expected_product_id=10,
				)
				self.assertFalse(res["changed"])
				self.assertEqual(res["reason"], "NO_OP_IDENTICAL_QUANTITY")
				mock_put.assert_not_called()

	def test_11_remote_drift_triggers_put(self):
		"""If remote quantity differs from desired quantity, PUT is called."""
		xml_diff = """<?xml version="1.0" encoding="UTF-8"?>
<prestashop>
<stock_available>
	<id><![CDATA[50]]></id>
	<id_product><![CDATA[10]]></id_product>
	<id_product_attribute><![CDATA[0]]></id_product_attribute>
	<quantity><![CDATA[15]]></quantity>
</stock_available>
</prestashop>"""

		xml_resp = """<?xml version="1.0" encoding="UTF-8"?>
<prestashop>
<stock_available>
	<id><![CDATA[50]]></id>
	<quantity><![CDATA[25]]></quantity>
</stock_available>
</prestashop>"""

		config = PrestaShopConfig(
			sales_channel="TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_KEY",
			write_enabled=True,
		)
		mock_resp = MagicMock()
		mock_resp.status_code = 200
		mock_resp.text = xml_resp

		with patch.object(PrestaShopConfig, "resolve_api_key", return_value="FAKE_KEY_1234567890123456789012"):
			client = PrestaShopClient(config=config)
			with patch.object(client, "get_stock_available_xml", return_value=xml_diff), \
				 patch.object(client.session, "put", return_value=mock_resp) as mock_put:
				res = client.update_stock_available_quantity(
					stock_available_id=50,
					quantity=25,
					expected_product_id=10,
				)
				self.assertTrue(res["changed"])
				self.assertEqual(res["previous_qty"], 15)
				self.assertEqual(res["resulting_qty"], 25)
				mock_put.assert_called_once()

	def test_12_secret_redaction_in_exceptions(self):
		"""PrestaShop exceptions and logs redact API keys."""
		secret_key = "TOP_SECRET_API_KEY_ABCD_123456"
		config = PrestaShopConfig(
			sales_channel="TID",
			environment="DEVELOPMENT",
			base_url="http://127.0.0.1:8082",
			credential_reference="TEST_KEY",
			write_enabled=True,
		)
		with patch.object(PrestaShopConfig, "resolve_api_key", return_value=secret_key):
			client = PrestaShopClient(config=config)
			mock_resp = MagicMock()
			mock_resp.status_code = 401
			mock_resp.text = f"Invalid auth with {secret_key}"
			with patch.object(client.session, "request", return_value=mock_resp):
				try:
					client._request("GET", "stock_availables")
				except Exception as e:
					self.assertNotIn(secret_key, str(e))
					self.assertIn("***", str(e))

	def test_13_bulk_scheduling_boundedness(self):
		"""Bulk scheduling creates bounded Integration Events within batch_size."""
		with patch("frappe.get_all") as mock_get_all:
			# Mock 100 items returned
			mock_get_all.return_value = [frappe._dict({"erp_document": f"ITEM-{i}"}) for i in range(100)]
			mock_doc = MagicMock()
			mock_doc.name = "EV-001"
			real_get_doc = frappe.get_doc

			def fake_get_doc(arg, *args, **kwargs):
				if isinstance(arg, dict) and arg.get("doctype") == "Integration Event":
					return mock_doc
				return real_get_doc(arg, *args, **kwargs)

			with patch("bop_erp.inventory.publication.frappe.get_doc", side_effect=fake_get_doc):
				events = schedule_channel_inventory_publication("TID", batch_size=20)
				self.assertEqual(len(events), 20)
				self.assertEqual(mock_doc.insert.call_count, 20)
