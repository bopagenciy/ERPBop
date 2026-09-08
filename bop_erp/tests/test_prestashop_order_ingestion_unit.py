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
	ExternalEntityType,
	ErrorCategory,
	TransactionOrigin,
)
from bop_erp.safety import ConnectorSafetyError, assert_safe_connector_target
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
	OrderAlreadyImportedError,
	OrderTotalMismatchError,
	InsufficientOrderStockError,
	InvalidOrderQuantityError,
	CustomerMappingError,
	AddressMappingError,
	OrderReservationFailedError,
)
from bop_erp.integrations.prestashop.adapters.order_normalizer import (
	normalize_prestashop_order,
	normalize_prestashop_customer,
	normalize_prestashop_address,
)
from bop_erp.orders.ingestion import (
	compute_order_idempotency_key,
	get_eligible_order_states,
	find_existing_order_mapping,
	resolve_or_create_customer,
	resolve_or_create_address,
	resolve_order_line_item,
	find_affected_channels_for_items,
	ingest_order_pipeline,
	process_order_ingestion_event,
	is_order_ingestion_complete,
)
from bop_erp.reliability import (
	claim_event_for_processing,
	verify_processing_authority,
)
from bop_erp.orders.discovery import (
	discover_eligible_connectors,
	get_fair_channel_order,
	discover_channel_orders,
	discover_multichannel_orders,
)


class TestPrestaShopOrderIngestionUnit(FrappeTestCase):
	"""
	Comprehensive Unit Test Suite for PrestaShop Inbound Order Ingestion & Reservation Pipeline.
	Verifies normalization, mapping resolution, idempotency, customer identity,
	financial reconciliation, safety, fairness, and bounds.
	"""

	def setUp(self):
		frappe.flags.in_test = True

	# ==================================================
	# 1. NORMALIZED ORDER PARSING
	# ==================================================
	def test_01_normalized_order_parsing(self):
		raw_order = {
			"id": 101,
			"reference": "REF-101",
			"current_state": 2,
			"id_customer": 42,
			"id_address_delivery": 10,
			"id_address_invoice": 11,
			"total_products": "100.00",
			"total_products_wt": "110.00",
			"total_shipping_tax_excl": "15.00",
			"total_shipping_tax_incl": "15.00",
			"total_discounts_tax_excl": "5.00",
			"total_discounts_tax_incl": "5.00",
			"total_paid": "120.00",
			"date_add": "2026-09-07 10:00:00",
			"payment": "ps_wirepayment",
		}
		raw_lines = [
			{
				"id": 1,
				"product_id": 20,
				"product_attribute_id": 0,
				"product_reference": "SKU-A",
				"product_name": "Product A",
				"product_quantity": 2,
				"unit_price_tax_excl": "50.000000",
				"unit_price_tax_incl": "55.000000",
				"total_price_tax_excl": "100.000000",
				"total_price_tax_incl": "110.000000",
			}
		]
		raw_cust = {
			"id": 42,
			"firstname": "John",
			"lastname": "Doe",
			"email": "john.doe@example.com",
			"company": "Acme Corp",
			"phone": "555-1234",
			"is_guest": 0,
		}
		raw_deliv = {
			"id": 10,
			"firstname": "John",
			"lastname": "Doe",
			"address1": "123 Main St",
			"city": "Dallas",
			"postcode": "75001",
			"id_country": "21",
		}
		raw_inv = {
			"id": 11,
			"firstname": "Jane",
			"lastname": "Doe",
			"address1": "456 Oak Ave",
			"city": "Austin",
			"postcode": "73301",
			"id_country": "21",
		}

		norm = normalize_prestashop_order(
			raw_order=raw_order,
			raw_lines=raw_lines,
			raw_customer=raw_cust,
			raw_delivery_address=raw_deliv,
			raw_invoice_address=raw_inv,
			sales_channel="TEST-A",
			currency_iso="USD",
			state_name="Payment accepted",
		)

		self.assertEqual(norm.external_order_id, "101")
		self.assertEqual(norm.external_reference, "REF-101")
		self.assertEqual(norm.customer.external_customer_id, "42")
		self.assertEqual(norm.customer.first_name, "John")
		self.assertEqual(norm.customer.last_name, "Doe")
		self.assertEqual(norm.customer.company, "Acme Corp")
		self.assertEqual(norm.delivery_address.city, "Dallas")
		self.assertEqual(norm.invoice_address.city, "Austin")
		self.assertEqual(len(norm.lines), 1)
		self.assertEqual(norm.lines[0].external_product_id, "20")
		self.assertIsNone(norm.lines[0].external_variant_id)
		self.assertEqual(norm.lines[0].quantity, 2.0)
		self.assertEqual(norm.lines[0].unit_price_ex_tax, 50.0)
		self.assertEqual(norm.totals.total_paid, 120.0)

	# ==================================================
	# 2. SIMPLE PRODUCT MAPPING RESOLUTION
	# ==================================================
	@patch("frappe.db.get_value")
	@patch("frappe.db.exists")
	def test_02_simple_product_mapping_resolution(self, mock_exists, mock_get_value):
		mock_get_value.return_value = {"erp_doctype": "Item", "erp_document": "ITEM-SIMPLE-1"}
		mock_exists.return_value = True

		line = ExternalOrderLine(
			external_line_id="1",
			external_product_id="25",
			external_variant_id=None,
			quantity=1.0,
		)
		item_code = resolve_order_line_item(line, sales_channel="TEST-A")
		self.assertEqual(item_code, "ITEM-SIMPLE-1")

	# ==================================================
	# 3. VARIANT PRODUCT MAPPING RESOLUTION
	# ==================================================
	@patch("frappe.db.get_value")
	@patch("frappe.db.exists")
	def test_03_variant_product_mapping_resolution(self, mock_exists, mock_get_value):
		mock_get_value.return_value = {"erp_doctype": "Item", "erp_document": "ITEM-VAR-BLUE"}
		mock_exists.return_value = True

		line = ExternalOrderLine(
			external_line_id="2",
			external_product_id="25",
			external_variant_id="9",  # Combination 9
			quantity=3.0,
		)
		item_code = resolve_order_line_item(line, sales_channel="TEST-A")
		self.assertEqual(item_code, "ITEM-VAR-BLUE")

	# ==================================================
	# 4. MISSING MAPPING FAILS FAST
	# ==================================================
	@patch("frappe.db.get_value")
	def test_04_missing_mapping_fails_fast(self, mock_get_value):
		mock_get_value.return_value = None

		line = ExternalOrderLine(
			external_line_id="3",
			external_product_id="999",
			external_variant_id=None,
			quantity=1.0,
		)
		with self.assertRaises(MissingProductMappingError):
			resolve_order_line_item(line, sales_channel="TEST-A")

	# ==================================================
	# 5. EXTERNAL ORDER IDENTITY & IDEMPOTENCY KEY
	# ==================================================
	def test_05_external_order_identity_and_idempotency_key(self):
		key1 = compute_order_idempotency_key("PRESTASHOP", "TEST-A", "101")
		key2 = compute_order_idempotency_key("PRESTASHOP", "TEST-A", "101")
		key_other_channel = compute_order_idempotency_key("PRESTASHOP", "TEST-B", "101")

		self.assertEqual(key1, key2)
		self.assertNotEqual(key1, key_other_channel)
		self.assertEqual(len(key1), 64)  # SHA-256

	# ==================================================
	# 6. CUSTOMER IDENTITY DEDUPLICATION
	# ==================================================
	@patch("frappe.db.get_value")
	@patch("frappe.db.exists")
	def test_06_customer_identity_deduplication(self, mock_exists, mock_get_value):
		# Customer already mapped to CUST-EXISTING
		mock_get_value.return_value = "CUST-EXISTING"
		mock_exists.return_value = True

		cust = ExternalCustomer(external_customer_id="55", first_name="Alice", last_name="Smith")
		resolved = resolve_or_create_customer(cust, sales_channel="TEST-A")
		self.assertEqual(resolved, "CUST-EXISTING")

	# ==================================================
	# 7. GUEST CUSTOMER HANDLING
	# ==================================================
	@patch("frappe.db.exists")
	@patch("frappe.get_doc")
	def test_07_guest_customer_handling(self, mock_get_doc, mock_exists):
		mock_exists.return_value = True
		cust = ExternalCustomer(external_customer_id="", is_guest=True)
		resolved = resolve_or_create_customer(cust, sales_channel="TEST-A")
		self.assertEqual(resolved, "Guest Customer - TEST-A")

	# ==================================================
	# 8. ADDRESS IDENTITY MAPPING
	# ==================================================
	@patch("frappe.db.get_value")
	@patch("frappe.db.exists")
	def test_08_address_identity_mapping(self, mock_exists, mock_get_value):
		mock_get_value.return_value = "ADDR-EXISTING"
		mock_exists.return_value = True

		addr = ExternalAddress(external_address_id="77", city="Orlando")
		resolved = resolve_or_create_address(addr, customer_name="CUST-1", sales_channel="TEST-A")
		self.assertEqual(resolved, "ADDR-EXISTING")

	# ==================================================
	# 9. FINANCIAL TOTAL RECONCILIATION
	# ==================================================
	@patch("bop_erp.orders.ingestion.get_eligible_order_states")
	@patch("bop_erp.orders.ingestion.find_existing_order_mapping")
	@patch("bop_erp.orders.ingestion.resolve_order_line_item")
	@patch("bop_erp.orders.ingestion.get_channel_atp")
	@patch("bop_erp.orders.ingestion.resolve_or_create_customer")
	@patch("bop_erp.orders.ingestion.resolve_or_create_address")
	@patch("frappe.get_doc")
	@patch("frappe.db.get_value")
	def test_09_financial_total_reconciliation(
		self, mock_db_val, mock_get_doc, mock_res_addr, mock_res_cust, mock_atp, mock_res_item, mock_find_map, mock_states
	):
		mock_states.return_value = ["2", "3", "11"]
		mock_find_map.return_value = None
		mock_res_item.return_value = "ITEM-1"
		mock_atp.return_value = MagicMock(aggregate_atp_qty=100.0)
		def mock_db_val_fn(doctype, filters=None, fieldname=None, *args, **kwargs):
			if doctype == "PrestaShop Connector" and fieldname == "eligible_order_states":
				return "2,3,11"
			if doctype == "Channel Inventory Source" and fieldname == "warehouse":
				return "Stores - TC"
			return None

		mock_db_val.side_effect = mock_db_val_fn

		orig_get_doc = frappe.get_doc
		def mock_get_doc_fn(doctype, *args, **kwargs):
			if doctype == "Sales Channel":
				return MagicMock(company="Test Company")
			return orig_get_doc(doctype, *args, **kwargs)

		mock_get_doc.side_effect = mock_get_doc_fn

		# Order line total is 50.0, but external total claims 100.0 (mismatch beyond tolerance)
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel="TEST-A",
			external_order_id="202",
			external_reference="REF-202",
			order_state_id="2",
			date_add="2026-09-07 10:00:00",
			customer=ExternalCustomer(external_customer_id="1"),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id="10",
					quantity=1.0,
					unit_price_ex_tax=50.0,
				)
			],
			totals=ExternalTotals(total_products_ex_tax=100.0, total_paid=100.0),
		)

		with self.assertRaises(OrderTotalMismatchError):
			ingest_order_pipeline(ext_order)

	# ==================================================
	# 10. QUANTITY VALIDATION
	# ==================================================
	@patch("bop_erp.orders.ingestion.get_eligible_order_states")
	@patch("bop_erp.orders.ingestion.find_existing_order_mapping")
	def test_10_quantity_validation(self, mock_find_map, mock_states):
		mock_states.return_value = ["2", "3", "11"]
		mock_find_map.return_value = None
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel="TEST-A",
			external_order_id="203",
			external_reference="REF-203",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="1"),
			lines=[
				ExternalOrderLine(
					external_line_id="1",
					external_product_id="10",
					quantity=-2.0,  # Invalid quantity
				)
			],
		)
		with self.assertRaises(InvalidOrderQuantityError):
			ingest_order_pipeline(ext_order)

	# ==================================================
	# 11. ELIGIBLE STATES FILTER
	# ==================================================
	@patch("bop_erp.orders.ingestion.get_eligible_order_states")
	@patch("bop_erp.orders.ingestion.find_existing_order_mapping")
	def test_11_eligible_states_filter(self, mock_find_map, mock_states):
		mock_find_map.return_value = None
		mock_states.return_value = ["2", "3", "11"]
		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel="TEST-A",
			external_order_id="204",
			external_reference="REF-204",
			order_state_id="6",  # Canceled state
			customer=ExternalCustomer(external_customer_id="1"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="10", quantity=1.0)],
		)
		with self.assertRaises(OrderNotEligibleError):
			ingest_order_pipeline(ext_order)

	# ==================================================
	# 12. EXTERNAL STATE REFRESH BEHAVIOR
	# ==================================================
	@patch("bop_erp.orders.ingestion.get_eligible_order_states")
	@patch("bop_erp.orders.ingestion.find_existing_order_mapping")
	def test_12_external_state_refresh(self, mock_find_map, mock_states):
		mock_find_map.return_value = None
		mock_states.return_value = ["2", "3", "11"]
		mock_client = MagicMock()
		# Client reports order has been changed to state 7 (Refunded) on PrestaShop
		mock_client.get_order.return_value = {"id": 205, "current_state": 7}

		ext_order = ExternalOrder(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel="TEST-A",
			external_order_id="205",
			external_reference="REF-205",
			order_state_id="2",  # Stale initial state
			customer=ExternalCustomer(external_customer_id="1"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="10", quantity=1.0)],
		)
		with self.assertRaises(OrderNotEligibleError):
			ingest_order_pipeline(ext_order, client=mock_client)

	# ==================================================
	# 13. AFFECTED CHANNEL DISCOVERY FOR SHARED INVENTORY
	# ==================================================
	@patch("frappe.get_all")
	def test_13_affected_channel_discovery(self, mock_get_all):
		def mock_get_all_fn(doctype, filters=None, pluck=None):
			if filters.get("sales_channel") == "TEST-A":
				return ["Warehouse Orlando"]
			if "warehouse" in filters:
				return ["TEST-A", "TEST-B"]
			return []

		mock_get_all.side_effect = mock_get_all_fn

		affected = find_affected_channels_for_items("TEST-A", ["ITEM-1"])
		self.assertIn("TEST-A", affected)
		self.assertIn("TEST-B", affected)
		self.assertEqual(len(affected), 2)

	# ==================================================
	# 14. CONNECTOR HOST SAFETY ASSERTION
	# ==================================================
	def test_14_connector_host_safety_enforced(self):
		# Safe local hosts
		assert_safe_connector_target("DEVELOPMENT", "http://127.0.0.1:8082")
		assert_safe_connector_target("DEVELOPMENT", "http://localhost:8082")
		assert_safe_connector_target("DEVELOPMENT", "http://prestashop-test")

		# Production host must be blocked
		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("DEVELOPMENT", "https://theindustrialdepot.com")

		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("DEVELOPMENT", "https://www.theindustrialdepot.com")

	# ==================================================
	# 15. DISCOVERY BOUNDS & FAIR ROTATING CURSOR
	# ==================================================
	@patch("frappe.cache")
	def test_15_discovery_bounds_and_fairness(self, mock_cache):
		cache_store = {"order_discovery_cursor": 0}
		mock_cache_obj = MagicMock()
		mock_cache_obj.get_value.side_effect = lambda k: cache_store.get(k)
		mock_cache_obj.set_value.side_effect = lambda k, v, expires_in_sec=None: cache_store.update({k: v})
		mock_cache.return_value = mock_cache_obj

		channels = [
			{"sales_channel": "TEST-A"},
			{"sales_channel": "TEST-B"},
			{"sales_channel": "TEST-C"},
		]

		# Cursor at 0 -> next run starts at index (0 + 1) % 3 = 1 (TEST-B)
		fair_order = get_fair_channel_order(channels)
		self.assertEqual([c["sales_channel"] for c in fair_order], ["TEST-B", "TEST-C", "TEST-A"])

	# ==================================================
	# 16. DYNAMIC CURRENCY TOLERANCE
	# ==================================================
	def test_16_dynamic_currency_tolerance(self):
		from bop_erp.orders.ingestion import get_currency_tolerance
		# Standard 2-decimal precision (e.g. USD, EUR) -> 0.01 tolerance
		tol = get_currency_tolerance("USD")
		self.assertLessEqual(tol, 0.05)
		self.assertGreater(tol, 0.0)

	# ==================================================
	# 17. CUSTOMER EMAIL NON-MERGING INDEPENDENCE
	# ==================================================
	@patch("bop_erp.orders.ingestion.frappe.db.get_value")
	@patch("bop_erp.orders.ingestion.frappe.db.exists")
	def test_17_customer_email_non_merging(self, mock_exists, mock_get_value):
		# Two customers sharing the same email but different external IDs
		cust_a = ExternalCustomer(external_customer_id="101", first_name="Alice", email="shared@example.com")
		cust_b = ExternalCustomer(external_customer_id="102", first_name="Bob", email="shared@example.com")

		# When resolving cust_a with existing mapping
		mock_get_value.return_value = "CUST-ALICE"
		mock_exists.return_value = True
		res_a = resolve_or_create_customer(cust_a, sales_channel="TEST-A")
		self.assertEqual(res_a, "CUST-ALICE")

		# When resolving cust_b without existing mapping
		mock_get_value.return_value = None
		mock_exists.return_value = False
		with patch("bop_erp.orders.ingestion.frappe.get_doc") as mock_get_doc:
			mock_doc = MagicMock(name="CUST-BOB")
			mock_get_doc.return_value = mock_doc
			res_b = resolve_or_create_customer(cust_b, sales_channel="TEST-A")
			# Verify mapping was created with external_id="102", NOT merged to Alice
			self.assertNotEqual(res_b, "CUST-ALICE")

	# ==================================================
	# 18. INCOMING STOCK EXCLUDED FROM CHANNEL ATP
	# ==================================================
	@patch("bop_erp.inventory.availability.get_warehouse_reservable_capacity")
	@patch("bop_erp.inventory.availability.get_channel_uncovered_sales_order_demand")
	@patch("frappe.get_all")
	@patch("frappe.db.get_value")
	def test_18_incoming_stock_excluded_from_atp(self, mock_val, mock_get_all, mock_uncovered, mock_cap):
		from bop_erp.inventory.availability import get_channel_atp
		# Channel has warehouse with actual_qty = 0, but ordered_qty = 100
		mock_get_all.return_value = [{"warehouse": "Stores - TC", "allow_sellable_stock": 1, "allow_fulfillment": 1}]
		def _mock_db_val(doctype, *args, **kwargs):
			if doctype in ("Sales Channel", "Warehouse"):
				return frappe._dict(company="Test Company")
			if doctype == "Bin":
				return frappe._dict(actual_qty=0.0, reserved_qty=0.0)
			return "Nos"
		mock_val.side_effect = _mock_db_val
		mock_cap.return_value = 0.0  # physical capacity is 0 (excludes ordered_qty)
		mock_uncovered.return_value = (0.0, [])

		atp = get_channel_atp("ITEM-TEST", "TEST-A")
		# Immediate ATP must be 0.0, not 100.0
		self.assertEqual(atp.aggregate_atp_qty, 0.0)

	# ==================================================
	# 19. INBOUND EVENT PENDING ONLY (NO DIRECT PROCESSING)
	# ==================================================
	@patch("bop_erp.orders.discovery.frappe.db.commit")
	@patch("bop_erp.orders.discovery.frappe.get_doc")
	@patch("bop_erp.orders.discovery.get_existing_idempotent_event")
	@patch("bop_erp.orders.discovery.find_existing_order_mapping")
	def test_19_inbound_event_pending_only_no_direct_processing(self, mock_mapping, mock_existing_event, mock_get_doc, mock_commit):
		mock_mapping.return_value = None
		mock_existing_event.return_value = None

		created_events = []
		def _mock_doc(d):
			doc = MagicMock()
			doc.doctype = d.get("doctype")
			doc.status = d.get("status")
			doc.insert = MagicMock()
			created_events.append(d)
			return doc

		mock_get_doc.side_effect = _mock_doc

		mock_client = MagicMock()
		mock_client._request.return_value = {
			"orders": [
				{"id": "501", "current_state": "2", "date_upd": "2026-09-08 10:00:00"}
			]
		}

		connector = {
			"name": "PS-TEST-CONNECTOR",
			"sales_channel": "TEST-A",
			"environment": "DEVELOPMENT",
			"base_url": "http://prestashop-test",
			"eligible_order_states": "2,3,11",
		}

		res = discover_channel_orders(connector, client=mock_client)
		self.assertEqual(res["events_created"], 1)
		self.assertEqual(len(created_events), 1)
		# Crucial: Event MUST be created as PENDING, never directly PROCESSING
		self.assertEqual(created_events[0]["status"], IntegrationStatus.PENDING)

	# ==================================================
	# 20. WORKER CLAIM FENCING & AUTHORITY LOSS
	# ==================================================
	@patch("bop_erp.orders.ingestion.verify_processing_authority")
	@patch("bop_erp.orders.ingestion.claim_event_for_processing")
	def test_20_worker_claim_fencing_and_authority_loss(self, mock_claim, mock_verify):
		# Case 1: Claim fails (already claimed by another worker)
		mock_claim.return_value = (False, None, None)
		res1 = process_order_ingestion_event("EVT-TEST-1", worker_id="worker-b")
		self.assertFalse(res1["success"])
		self.assertEqual(res1["reason"], "CLAIM_REJECTED_ALREADY_CLAIMED_OR_TERMINAL")

		# Case 2: Claim succeeds, but authority check fails (lease expired or worker fenced)
		mock_claim.return_value = (True, "worker-a", "token-xyz")
		mock_verify.return_value = (False, "Lease expired")
		res2 = process_order_ingestion_event("EVT-TEST-2", worker_id="worker-a")
		self.assertFalse(res2["success"])
		self.assertEqual(res2["reason"], "LOST_PROCESSING_AUTHORITY")

	# ==================================================
	# 21. DISCOVERY DURABLE WATERMARK AND OVERLAP
	# ==================================================
	@patch("bop_erp.orders.discovery.frappe.db.commit")
	@patch("bop_erp.orders.discovery.frappe.db.set_value")
	@patch("bop_erp.orders.discovery.frappe.get_doc")
	@patch("bop_erp.orders.discovery.get_existing_idempotent_event")
	@patch("bop_erp.orders.discovery.find_existing_order_mapping")
	def test_21_discovery_durable_watermark_and_overlap(self, mock_map, mock_exist, mock_get_doc, mock_set_val, mock_commit):
		mock_map.return_value = None
		mock_exist.return_value = None
		mock_get_doc.return_value = MagicMock()

		mock_client = MagicMock()
		mock_client._request.return_value = {
			"orders": [
				{"id": "505", "current_state": "2", "date_upd": "2026-09-08 12:45:00"},
				{"id": "506", "current_state": "2", "date_upd": "2026-09-08 13:00:00"},
			]
		}

		connector = {
			"name": "PS-TEST-CONNECTOR",
			"sales_channel": "TEST-A",
			"environment": "DEVELOPMENT",
			"base_url": "http://prestashop-test",
			"eligible_order_states": "2,3,11",
			"last_order_watermark": "2026-09-08 12:00:00",
			"last_order_id": "500",
		}

		res = discover_channel_orders(connector, client=mock_client)
		self.assertEqual(res["orders_seen"], 2)

		# Verify request used overlap window (last_watermark - 30 minutes = 11:30:00)
		call_params = mock_client._request.call_args[1]["params"]
		self.assertIn("11:30:00", call_params["filter[date_upd]"])

		# Verify connector watermark persisted with max observed date_upd
		mock_set_val.assert_called_once_with(
			"PrestaShop Connector",
			"PS-TEST-CONNECTOR",
			{
				"last_order_watermark": "2026-09-08 13:00:00",
				"last_order_id": "506",
			},
			update_modified=False,
		)

	# ==================================================
	# 22. DISCOVERY DETERMINISTIC PAGINATION SAFETY & TIE BREAKING
	# ==================================================
	def test_22_discovery_pagination_safety_and_tie_breaking(self):
		mock_client = MagicMock()
		# Page 1 returns 2 orders, Page 2 returns 1 order
		mock_client._request.side_effect = [
			{
				"orders": [
					{"id": "10", "current_state": "2", "date_upd": "2026-09-08 12:00:00"},
					{"id": "11", "current_state": "2", "date_upd": "2026-09-08 12:00:00"},
				]
			},
			{
				"orders": [
					{"id": "12", "current_state": "2", "date_upd": "2026-09-08 12:05:00"},
				]
			},
		]

		connector = {
			"name": "PS-TEST-CONNECTOR",
			"sales_channel": "TEST-A",
			"environment": "DEVELOPMENT",
			"base_url": "http://prestashop-test",
			"eligible_order_states": "2,3,11",
		}

		with patch("bop_erp.orders.discovery.frappe.get_doc") as mock_get_doc, \
		     patch("bop_erp.orders.discovery.find_existing_order_mapping", return_value=None), \
		     patch("bop_erp.orders.discovery.get_existing_idempotent_event", return_value=None), \
		     patch("bop_erp.orders.discovery.frappe.db.set_value") as mock_set_val, \
		     patch("bop_erp.orders.discovery.frappe.db.commit"):
			mock_get_doc.return_value = MagicMock()
			res = discover_channel_orders(connector, client=mock_client, page_size=2, max_orders=10)

		self.assertEqual(res["orders_seen"], 3)
		# Verify sort parameter enforces deterministic tie-breaking [date_upd_ASC,id_ASC]
		call_1_params = mock_client._request.call_args_list[0][1]["params"]
		self.assertEqual(call_1_params["sort"], "[date_upd_ASC,id_ASC]")
		self.assertEqual(call_1_params["limit"], "0,2")

		call_2_params = mock_client._request.call_args_list[1][1]["params"]
		self.assertEqual(call_2_params["limit"], "2,2")

	# ==================================================
	# 23. EMPTY ELIGIBLE STATES FAILS SAFELY
	# ==================================================
	def test_23_empty_eligible_states_fails_safely(self):
		connector = {
			"name": "PS-UNCONFIGURED",
			"sales_channel": "TEST-EMPTY",
			"environment": "DEVELOPMENT",
			"base_url": "http://prestashop-test",
			"eligible_order_states": "",  # Empty
		}

		mock_client = MagicMock()
		res = discover_channel_orders(connector, client=mock_client)
		# Must safely skip without calling PrestaShop API or guessing fallback states
		self.assertEqual(res["events_created"], 0)
		self.assertEqual(res["orders_seen"], 0)
		mock_client._request.assert_not_called()

	# ==================================================
	# 24. INCOMPLETE ORDER IDENTIFICATION
	# ==================================================
	@patch("bop_erp.orders.ingestion.frappe.get_all")
	@patch("bop_erp.orders.ingestion.frappe.get_doc")
	@patch("bop_erp.orders.ingestion.frappe.db.exists")
	def test_24_incomplete_order_identification(self, mock_exists, mock_get_doc, mock_get_all):
		mock_exists.return_value = True

		# Mock a Sales Order with 2 line items (qty=2 each)
		mock_so = MagicMock()
		mock_so.docstatus = 1
		mock_item1 = MagicMock(name="item1", item_code="ITEM-1", qty=2.0)
		mock_item1.name = "row-1"
		mock_item2 = MagicMock(name="item2", item_code="ITEM-2", qty=2.0)
		mock_item2.name = "row-2"
		mock_so.items = [mock_item1, mock_item2]
		mock_get_doc.return_value = mock_so

		# Row 1 has full reservation (2.0), row 2 has partial reservation (1.0)
		def _mock_sres(doctype, filters=None, fields=None):
			if filters.get("voucher_detail_no") == "row-1":
				return [{"name": "SRE-1", "reserved_qty": 2.0}]
			if filters.get("voucher_detail_no") == "row-2":
				return [{"name": "SRE-2", "reserved_qty": 1.0}]
			return []

		mock_get_all.side_effect = _mock_sres

		is_complete, missing = is_order_ingestion_complete("SO-TEST-INCOMPLETE")
		self.assertFalse(is_complete)
		self.assertEqual(len(missing), 1)
		self.assertIn("ITEM-2", missing[0])

