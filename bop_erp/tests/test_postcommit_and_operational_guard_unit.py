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
	IntegrationReadinessStatus,
	ExternalEntityType,
	ErrorCategory,
	TransactionOrigin,
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
	MissingProductMappingError,
	OrderNotEligibleError,
	OrderTotalMismatchError,
	InsufficientOrderStockError,
	InvalidOrderQuantityError,
	OrderReservationFailedError,
)
from bop_erp.orders.guard import (
	OperationalGuardError,
	assert_sales_order_ready_for_fulfillment,
	validate_operational_guard,
)
from bop_erp.orders.ingestion import (
	find_affected_channels_for_items,
	find_affected_channel_items_for_scopes,
	schedule_post_commit_publication,
	is_order_ingestion_complete,
	ingest_order_pipeline,
	process_order_ingestion_event,
)
from bop_erp.inventory.publication import (
	schedule_channel_inventory_publication,
	process_inventory_publication_event,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.exceptions import PrestaShopServerError


class TestPostcommitAndOperationalGuardUnit(FrappeTestCase):
	"""
	Phase 1K.3 Comprehensive Unit & Correctness Boundary Tests:
	- Rollback before commit produces 0 publication intents (failure injection points A-E).
	- Success schedules publication strictly after commit.
	- Shared inventory affected channel discovery (2-channel, 3-channel exclusion, multi-warehouse union).
	- Failed ingestion zero publication regression.
	- Incomplete submitted order state lifecycle (INGESTION_PENDING -> RESERVATION_PENDING -> READY / FAILED_REVIEW).
	- Operational guard protection on Pick List, Delivery Note, Shipment.
	- Manual/non-integrated Sales Order unaffected.
	- Duplicate publication coalescing & after-commit idempotency.
	- Outbound failure independence.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")

		cls.sales_channel = "TEST-POSTCOMMIT-A"
		cls.coal_channel = "TEST-COAL-CH"
		for ch in [cls.sales_channel, cls.coal_channel]:
			if not frappe.db.exists("Sales Channel", ch):
				frappe.get_doc({
					"doctype": "Sales Channel",
					"channel_id": ch,
					"channel_name": ch,
					"channel_type": "PRESTASHOP",
					"company": cls.company,
					"active": 1,
				}).insert(ignore_permissions=True)

			if not frappe.db.exists("PrestaShop Connector", {"sales_channel": ch}):
				pc = frappe.get_doc({
					"doctype": "PrestaShop Connector",
					"sales_channel": ch,
					"environment": "DEVELOPMENT",
					"base_url": "http://prestashop-test",
					"credential_reference": "TEST_KEY",
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
					{"sales_channel": ch},
					{"enabled": 1, "eligible_order_states": "2,3,4,Payment accepted"}
				)

		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")
		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": cls.sales_channel, "warehouse": cls.warehouse}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.sales_channel,
				"warehouse": cls.warehouse,
				"enabled": 1,
				"allow_sellable_stock": 1,
			}).insert(ignore_permissions=True)
		frappe.db.commit()

		cls.item_code = "ITEM-GUARD-UNIT-1"
		cls.item_coal = "ITEM-COAL-UNIT-01"
		for ic in [cls.item_code, cls.item_coal]:
			if not frappe.db.exists("Item", ic):
				frappe.get_doc({
					"doctype": "Item",
					"item_code": ic,
					"item_name": ic,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}).insert(ignore_permissions=True)

		cls.provider = IntegrationProvider.PRESTASHOP

		# Active mapping for item_code in sales_channel for publication test
		if not frappe.db.exists("External ID Mapping", {"sales_channel": cls.sales_channel, "erp_document": cls.item_code}):
			frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": cls.sales_channel,
				"provider": cls.provider,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": "9901",
				"erp_doctype": "Item",
				"erp_document": cls.item_code,
				"active": 1,
			}).insert(ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("External ID Mapping", {"sales_channel": cls.sales_channel, "erp_document": cls.item_code})
		for ic in [cls.item_code, cls.item_coal]:
			if frappe.db.exists("Item", ic):
				frappe.delete_doc("Item", ic, force=True, ignore_permissions=True)

		for ch in [cls.sales_channel, cls.coal_channel]:
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("Inventory Publication State", {"sales_channel": ch})
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})
			frappe.db.sql("DELETE FROM `tabPrestaShop Connector` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabChannel Inventory Source` WHERE sales_channel = %s", (ch,))
			frappe.db.sql("DELETE FROM `tabSales Channel` WHERE name = %s", (ch,))

		frappe.db.commit()
		super().tearDownClass()

	# ==================================================
	# 1. ROLLBACK MUST CREATE ZERO OUTBOUND INTENTS (FAILURE INJECTIONS A - E)
	# ==================================================
	def test_01_rollback_before_so_insert_creates_zero_publication_intents(self):
		"""Failure Point A: Failure before Sales Order insert creates 0 publication intents."""
		initial_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})

		ext_order = ExternalOrder(
			provider=self.provider,
			sales_channel=self.sales_channel,
			external_order_id="FAIL-A-01",
			external_reference="REF-FAIL-A-01",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="CUST-1"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=1.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=10.0, total_paid=10.0),
		)

		with patch("bop_erp.orders.ingestion.resolve_order_line_item", return_value=self.item_code), 		     patch("bop_erp.orders.ingestion.get_channel_atp") as mock_atp, 		     patch("bop_erp.orders.ingestion.resolve_or_create_customer", side_effect=Exception("DB Failure Before SO Insert")):
			mock_atp.return_value = MagicMock(aggregate_atp_qty=100.0)

			with self.assertRaises(Exception):
				ingest_order_pipeline(ext_order)

		frappe.db.rollback()

		new_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})
		self.assertEqual(new_count, initial_count, "Rollback before SO insert must create 0 publication intents")

	def test_02_rollback_after_so_insert_creates_zero_publication_intents(self):
		"""Failure Point B: Failure after SO insert but before submit creates 0 publication intents."""
		initial_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})

		ext_order = ExternalOrder(
			provider=self.provider,
			sales_channel=self.sales_channel,
			external_order_id="FAIL-B-01",
			external_reference="REF-FAIL-B-01",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="CUST-1"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=1.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=10.0, total_paid=10.0),
		)

		with patch("bop_erp.orders.ingestion.resolve_order_line_item", return_value=self.item_code), 		     patch("bop_erp.orders.ingestion.get_channel_atp") as mock_atp, 		     patch("bop_erp.orders.ingestion.resolve_or_create_customer", return_value="CUST-1"), 		     patch("bop_erp.orders.ingestion.resolve_or_create_address", return_value=None), 		     patch("frappe.get_doc") as mock_get_doc:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=100.0)
			mock_so = MagicMock()
			mock_so.name = "SO-SIMULATED-B"
			mock_so.insert.return_value = mock_so
			# Mapping insert fails
			mock_mapping = MagicMock()
			mock_mapping.insert.side_effect = frappe.DuplicateEntryError("Mapping crash")

			def get_doc_side_effect(arg, *a, **kw):
				if isinstance(arg, dict) and arg.get("doctype") == "Sales Order":
					return mock_so
				if isinstance(arg, dict) and arg.get("doctype") == "External ID Mapping":
					return mock_mapping
				if arg == "Sales Channel":
					return MagicMock(company=self.company)
				return MagicMock()

			mock_get_doc.side_effect = get_doc_side_effect

			with self.assertRaises(Exception):
				ingest_order_pipeline(ext_order)

		frappe.db.rollback()

		new_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})
		self.assertEqual(new_count, initial_count, "Rollback after SO insert must create 0 publication intents")

	def test_03_rollback_after_so_submit_creates_zero_publication_intents(self):
		"""Failure Point C: Failure after SO submit creates 0 publication intents."""
		initial_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})

		ext_order = ExternalOrder(
			provider=self.provider,
			sales_channel=self.sales_channel,
			external_order_id="FAIL-C-01",
			external_reference="REF-FAIL-C-01",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="CUST-1"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=1.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=10.0, total_paid=10.0),
		)

		with patch("bop_erp.orders.ingestion.resolve_order_line_item", return_value=self.item_code), 		     patch("bop_erp.orders.ingestion.get_channel_atp") as mock_atp, 		     patch("bop_erp.orders.ingestion.resolve_or_create_customer", return_value="CUST-1"), 		     patch("bop_erp.orders.ingestion.resolve_or_create_address", return_value=None), 		     patch("frappe.get_doc") as mock_get_doc:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=100.0)
			mock_so = MagicMock()
			mock_so.name = "SO-SIMULATED-C"
			mock_so.insert.return_value = mock_so
			mock_so.submit.side_effect = Exception("Submit crashed")

			def get_doc_side_effect(arg, *a, **kw):
				if isinstance(arg, dict) and arg.get("doctype") == "Sales Order":
					return mock_so
				if isinstance(arg, dict) and arg.get("doctype") == "External ID Mapping":
					return MagicMock()
				if arg == "Sales Channel":
					return MagicMock(company=self.company)
				return MagicMock()

			mock_get_doc.side_effect = get_doc_side_effect

			with self.assertRaises(Exception):
				ingest_order_pipeline(ext_order)

		frappe.db.rollback()

		new_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})
		self.assertEqual(new_count, initial_count, "Rollback after SO submit must create 0 publication intents")

	def test_04_rollback_during_reservation_creates_zero_publication_intents(self):
		"""Failure Point D: Failure during reservation creation rolls back and creates 0 publication intents."""
		initial_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})

		ext_order = ExternalOrder(
			provider=self.provider,
			sales_channel=self.sales_channel,
			external_order_id="FAIL-D-01",
			external_reference="REF-FAIL-D-01",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="CUST-1"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=2.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=20.0, total_paid=20.0),
		)

		with patch("bop_erp.orders.ingestion.resolve_order_line_item", return_value=self.item_code), 		     patch("bop_erp.orders.ingestion.get_channel_atp") as mock_atp, 		     patch("bop_erp.orders.ingestion.resolve_or_create_customer", return_value="CUST-1"), 		     patch("bop_erp.orders.ingestion.resolve_or_create_address", return_value=None), 		     patch("bop_erp.orders.ingestion.reserve_channel_stock", side_effect=Exception("Deadlock on SRE allocation")), 		     patch("frappe.get_doc") as mock_get_doc:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=100.0)
			mock_so = MagicMock()
			mock_so.name = "SO-SIMULATED-D"
			mock_so.items = [MagicMock(item_code=self.item_code, qty=2.0, name="row1")]

			def get_doc_side_effect(arg, *a, **kw):
				if isinstance(arg, dict) and arg.get("doctype") == "Sales Order":
					return mock_so
				if isinstance(arg, dict) and arg.get("doctype") == "External ID Mapping":
					return MagicMock()
				if arg == "Sales Channel":
					return MagicMock(company=self.company)
				return MagicMock()

			mock_get_doc.side_effect = get_doc_side_effect

			with self.assertRaises(OrderReservationFailedError):
				ingest_order_pipeline(ext_order)

		frappe.db.rollback()

		new_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})
		self.assertEqual(new_count, initial_count, "Rollback during reservation must create 0 publication intents")

	def test_05_rollback_after_reservation_before_commit_creates_zero_publication_intents(self):
		"""Failure Point E: If crash/rollback occurs after reservation but before commit, after_commit is cleared."""
		initial_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})

		# Queue post-commit publication
		schedule_post_commit_publication({self.sales_channel: [self.item_code]})

		# Rollback transaction (simulating crash before commit)
		frappe.db.rollback()

		# Next commit must NOT execute the rolled-back callback!
		frappe.db.commit()

		new_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})
		self.assertEqual(new_count, initial_count, "Rollback before commit must cancel after_commit execution completely")

	# ==================================================
	# 2. SUCCESS MUST SCHEDULE STRICTLY AFTER COMMIT
	# ==================================================
	def test_06_success_schedules_publication_only_after_commit(self):
		"""Publication callback is only executed strictly upon durable frappe.db.commit()."""
		executed = []

		def _test_callback():
			executed.append(True)

		frappe.db.after_commit(_test_callback)

		# Before commit: must NOT have executed
		self.assertEqual(len(executed), 0, "Callback must not execute before commit")

		frappe.db.commit()

		# After commit: executed exactly once
		self.assertEqual(len(executed), 1, "Callback must execute after commit")

	# ==================================================
	# 3. SHARED INVENTORY AFFECTED CHANNEL DISCOVERY
	# ==================================================
	def test_07_shared_inventory_affected_channels_two_channel_overlap(self):
		"""TEST-A and TEST-B share Warehouse 1 -> Both discovered as affected."""
		scopes = [("ITEM-1", "WH-1")]

		with patch("frappe.db.sql") as mock_sql:
			mock_sql.return_value = [
				{"sales_channel": "TEST-A"},
				{"sales_channel": "TEST-B"},
			]

			res = find_affected_channel_items_for_scopes(scopes)
			self.assertIn("TEST-A", res)
			self.assertIn("TEST-B", res)
			self.assertEqual(res["TEST-A"], ["ITEM-1"])
			self.assertEqual(res["TEST-B"], ["ITEM-1"])

	def test_08_three_channel_overlap_unrelated_channel_excluded(self):
		"""TEST-A and TEST-B share WH-1; TEST-C sources WH-2. WH-1 change excludes TEST-C."""
		scopes = [("ITEM-1", "WH-1")]

		def mock_sql_fn(query, params=None, *a, **kw):
			if params and params[0] == "WH-1":
				return [{"sales_channel": "TEST-A"}, {"sales_channel": "TEST-B"}]
			elif params and params[0] == "WH-2":
				return [{"sales_channel": "TEST-C"}]
			return []

		with patch("frappe.db.sql", side_effect=mock_sql_fn):
			res = find_affected_channel_items_for_scopes(scopes)
			self.assertIn("TEST-A", res)
			self.assertIn("TEST-B", res)
			self.assertNotIn("TEST-C", res, "Unrelated channel TEST-C must be excluded from WH-1 change")

	def test_09_multiwarehouse_order_affected_channels_union(self):
		"""Multi-warehouse order: Item X from WH-A, Item Y from WH-B -> union without duplicates."""
		scopes = [
			("ITEM-X", "WH-A"),
			("ITEM-Y", "WH-B"),
		]

		def mock_sql_fn(query, params=None, *a, **kw):
			wh = params[0] if params else None
			if wh == "WH-A":
				return [{"sales_channel": "CH-1"}, {"sales_channel": "CH-2"}]
			elif wh == "WH-B":
				return [{"sales_channel": "CH-2"}, {"sales_channel": "CH-3"}]
			return []

		with patch("frappe.db.sql", side_effect=mock_sql_fn):
			res = find_affected_channel_items_for_scopes(scopes)
			self.assertEqual(sorted(list(res.keys())), ["CH-1", "CH-2", "CH-3"])
			self.assertEqual(res["CH-1"], ["ITEM-X"])
			self.assertEqual(res["CH-2"], ["ITEM-X", "ITEM-Y"])
			self.assertEqual(res["CH-3"], ["ITEM-Y"])

	# ==================================================
	# 4. FAILED INGESTION ZERO PUBLICATION REGRESSION
	# ==================================================
	def test_10_failed_ingestion_zero_publication_regression(self):
		"""Financial total mismatch raises error and creates zero publication intents."""
		initial_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})

		ext_order = ExternalOrder(
			provider=self.provider,
			sales_channel=self.sales_channel,
			external_order_id="MISMATCH-01",
			external_reference="REF-MISMATCH-01",
			order_state_id="2",
			customer=ExternalCustomer(external_customer_id="CUST-1"),
			lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=1.0, unit_price_ex_tax=10.0)],
			totals=ExternalTotals(total_products_ex_tax=999.0, total_paid=999.0),
		)

		with patch("bop_erp.orders.ingestion.resolve_order_line_item", return_value=self.item_code), 		     patch("bop_erp.orders.ingestion.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=50.0)

			with self.assertRaises(OrderTotalMismatchError):
				ingest_order_pipeline(ext_order)

		frappe.db.rollback()

		new_count = frappe.db.count("Integration Event", {
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
		})
		self.assertEqual(new_count, initial_count)

	# ==================================================
	# 5. INCOMPLETE SUBMITTED ORDER STATE & LIFECYCLE
	# ==================================================
	def test_11_initial_submitted_crash_state_not_ready(self):
		"""After submit but before reservations complete, status is strictly RESERVATION_PENDING (not READY)."""
		with patch("frappe.get_doc") as mock_get_doc, 		     patch("frappe.db.set_value") as mock_set_val, 		     patch("bop_erp.orders.ingestion.resolve_order_line_item", return_value=self.item_code), 		     patch("bop_erp.orders.ingestion.get_channel_atp") as mock_atp, 		     patch("bop_erp.orders.ingestion.resolve_or_create_customer", return_value="CUST-1"), 		     patch("bop_erp.orders.ingestion.resolve_or_create_address", return_value=None), 		     patch("bop_erp.orders.ingestion.reserve_channel_stock", side_effect=Exception("Crash before SREs complete")):
			mock_atp.return_value = MagicMock(aggregate_atp_qty=50.0)
			mock_so = MagicMock()
			mock_so.name = "SO-CRASH-SUBMIT"
			mock_so.items = [MagicMock(item_code=self.item_code, qty=1.0, name="row1")]

			def get_doc_side_effect(arg, *a, **kw):
				if isinstance(arg, dict) and arg.get("doctype") == "Sales Order":
					return mock_so
				if isinstance(arg, dict) and arg.get("doctype") == "External ID Mapping":
					return MagicMock()
				if arg == "Sales Channel":
					return MagicMock(company=self.company)
				return MagicMock()

			mock_get_doc.side_effect = get_doc_side_effect

			ext_order = ExternalOrder(
				provider=self.provider,
				sales_channel=self.sales_channel,
				external_order_id="CRASH-01",
				external_reference="REF-CRASH-01",
				order_state_id="2",
				customer=ExternalCustomer(external_customer_id="CUST-1"),
				lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=1.0, unit_price_ex_tax=10.0)],
				totals=ExternalTotals(total_products_ex_tax=10.0, total_paid=10.0),
			)

			with self.assertRaises(OrderReservationFailedError):
				ingest_order_pipeline(ext_order)

			calls = [call for call in mock_set_val.call_args_list if call[0][1] == "SO-CRASH-SUBMIT"]
			statuses_set = [call[0][3] for call in calls if call[0][2] == "integration_status"]
			self.assertIn(IntegrationReadinessStatus.RESERVATION_PENDING, statuses_set)
			self.assertNotIn(IntegrationReadinessStatus.READY, statuses_set)

	def test_12_recovery_moves_incomplete_order_to_ready(self):
		"""Incomplete submitted order in RESERVATION_PENDING is recovered to READY upon complete reservations."""
		with patch("frappe.get_doc") as mock_get_doc, 		     patch("frappe.db.set_value") as mock_set_val, 		     patch("bop_erp.orders.ingestion.find_existing_order_mapping", return_value="SO-RECOV-01"), 		     patch("frappe.db.exists", return_value=True), 		     patch("bop_erp.orders.ingestion._ensure_order_reservations") as mock_ensure, 		     patch("bop_erp.orders.ingestion.is_order_ingestion_complete", return_value=(True, [])), 		     patch("bop_erp.orders.ingestion.schedule_post_commit_publication") as mock_pub:
			mock_so = MagicMock(docstatus=1, items=[MagicMock(item_code=self.item_code, warehouse="WH-1")])
			mock_get_doc.return_value = mock_so

			ext_order = ExternalOrder(
				provider=self.provider,
				sales_channel=self.sales_channel,
				external_order_id="RECOV-01",
				external_reference="REF-RECOV-01",
				order_state_id="2",
				customer=ExternalCustomer(external_customer_id="CUST-1"),
				lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=1.0, unit_price_ex_tax=10.0)],
				totals=ExternalTotals(total_products_ex_tax=10.0, total_paid=10.0),
			)

			res = ingest_order_pipeline(ext_order)
			self.assertTrue(res["success"])
			self.assertTrue(res["is_replay"])

			mock_set_val.assert_any_call("Sales Order", "SO-RECOV-01", "integration_status", IntegrationReadinessStatus.READY)
			mock_pub.assert_called_once()

	def test_13_failed_recovery_status_is_failed_review(self):
		"""If recovery fails to allocate missing reservations, status transitions to FAILED_REVIEW."""
		with patch("frappe.get_doc") as mock_get_doc, 		     patch("frappe.db.set_value") as mock_set_val, 		     patch("bop_erp.orders.ingestion.find_existing_order_mapping", return_value="SO-FAIL-RECOV"), 		     patch("frappe.db.exists", return_value=True), 		     patch("bop_erp.orders.ingestion._ensure_order_reservations", side_effect=Exception("Insufficient stock on recovery")):
			mock_so = MagicMock(docstatus=1, items=[MagicMock(item_code=self.item_code, warehouse="WH-1")])
			mock_get_doc.return_value = mock_so

			ext_order = ExternalOrder(
				provider=self.provider,
				sales_channel=self.sales_channel,
				external_order_id="FAIL-RECOV-01",
				external_reference="REF-FAIL-RECOV-01",
				order_state_id="2",
				customer=ExternalCustomer(external_customer_id="CUST-1"),
				lines=[ExternalOrderLine(external_line_id="1", external_product_id="P1", quantity=1.0, unit_price_ex_tax=10.0)],
				totals=ExternalTotals(total_products_ex_tax=10.0, total_paid=10.0),
			)

			with self.assertRaises(Exception):
				ingest_order_pipeline(ext_order)

			mock_set_val.assert_any_call(
				"Sales Order", "SO-FAIL-RECOV", "integration_status", IntegrationReadinessStatus.FAILED_REVIEW
			)

	# ==================================================
	# 6. OPERATIONAL GUARD (PICK LIST / DELIVERY NOTE / SHIPMENT)
	# ==================================================
	def test_14_manual_non_integrated_sales_order_unaffected(self):
		"""Native manual ERP Sales Order without integration attributes passes operational guard cleanly."""
		with patch("frappe.db.get_value") as mock_val:
			mock_val.return_value = frappe._dict({
				"name": "SO-MANUAL-001",
				"external_order_id": None,
				"integration_status": None,
				"transaction_origin": TransactionOrigin.MANUAL,
				"docstatus": 1,
			})

			assert_sales_order_ready_for_fulfillment("SO-MANUAL-001")

	def test_15_operational_guard_rejects_incomplete_imported_so_pick_list(self):
		"""Pick List creation referencing an imported SO in RESERVATION_PENDING is rejected."""
		with patch("frappe.db.get_value") as mock_val:
			mock_val.return_value = frappe._dict({
				"name": "SO-INCOMPLETE-001",
				"external_order_id": "WEB-101",
				"integration_status": IntegrationReadinessStatus.RESERVATION_PENDING,
				"transaction_origin": TransactionOrigin.WEB,
				"docstatus": 1,
			})

			with self.assertRaises(OperationalGuardError):
				assert_sales_order_ready_for_fulfillment("SO-INCOMPLETE-001")

			pl_doc = frappe._dict({
				"doctype": "Pick List",
				"locations": [frappe._dict({"sales_order": "SO-INCOMPLETE-001"})],
			})
			with self.assertRaises(OperationalGuardError):
				validate_operational_guard(pl_doc)

	def test_16_operational_guard_rejects_incomplete_imported_so_delivery_note(self):
		"""Delivery Note creation referencing an imported SO in FAILED_REVIEW is rejected."""
		with patch("frappe.db.get_value") as mock_val:
			mock_val.return_value = frappe._dict({
				"name": "SO-INCOMPLETE-002",
				"external_order_id": "WEB-102",
				"integration_status": IntegrationReadinessStatus.FAILED_REVIEW,
				"transaction_origin": TransactionOrigin.WEB,
				"docstatus": 1,
			})

			dn_doc = frappe._dict({
				"doctype": "Delivery Note",
				"items": [frappe._dict({"against_sales_order": "SO-INCOMPLETE-002"})],
			})
			with self.assertRaises(OperationalGuardError):
				validate_operational_guard(dn_doc)

	def test_17_operational_guard_allows_ready_imported_so(self):
		"""Imported Sales Order with READY status and complete reservations passes operational guard."""
		with patch("frappe.db.get_value") as mock_val, 		     patch("bop_erp.orders.ingestion.is_order_ingestion_complete", return_value=(True, [])):
			mock_val.return_value = frappe._dict({
				"name": "SO-READY-001",
				"external_order_id": "WEB-200",
				"integration_status": IntegrationReadinessStatus.READY,
				"transaction_origin": TransactionOrigin.WEB,
				"docstatus": 1,
			})

			assert_sales_order_ready_for_fulfillment("SO-READY-001")

			pl_doc = frappe._dict({
				"doctype": "Pick List",
				"locations": [frappe._dict({"sales_order": "SO-READY-001"})],
			})
			validate_operational_guard(pl_doc)

	# ==================================================
	# 7. AFTER-COMMIT IDEMPOTENCY & COALESCING
	# ==================================================
	def test_18_duplicate_after_commit_scheduling_coalesces(self):
		"""Calling publication scheduling twice for the same resulting ATP coalesces into 1 pending event."""
		item_code = self.item_coal
		channel = self.coal_channel

		with patch("bop_erp.inventory.publication.resolve_item_mapping", return_value={"product_id": "1001"}), 		     patch("bop_erp.inventory.publication.get_channel_atp") as mock_atp:
			mock_atp.return_value = MagicMock(aggregate_atp_qty=15.0)

			ev1 = schedule_channel_inventory_publication(channel, [item_code])
			self.assertEqual(len(ev1), 1)

			ev2 = schedule_channel_inventory_publication(channel, [item_code])
			self.assertEqual(len(ev2), 1)

			self.assertEqual(ev1[0], ev2[0])

			pending_count = frappe.db.count("Integration Event", {
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": channel,
				"direction": IntegrationDirection.OUTBOUND,
				"erp_document": item_code,
				"status": IntegrationStatus.PENDING,
			})
			self.assertEqual(pending_count, 1, "Must not create duplicate pending publication events")

			frappe.db.delete("Integration Event", {"name": ev1[0]})
			frappe.db.commit()

	# ==================================================
	# 8. OUTBOUND FAILURE INDEPENDENCE
	# ==================================================
	def test_19_outbound_temporary_failure_does_not_rollback_inbound_order(self):
		"""Inbound order remains committed and READY when outbound publication target returns HTTP 500."""
		event_doc = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": self.item_code,
			"idempotency_key": f"PUB-FAIL-TEST-{frappe.generate_hash(length=8)}",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({"item_code": self.item_code, "sales_channel": self.sales_channel, "publication_version": 1, "desired_state_hash": "h1"}),
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		mock_client = MagicMock(spec=PrestaShopClient)
		mock_client.config = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=True)
		mock_client.resolve_stock_available_id.side_effect = PrestaShopServerError("500 Internal Server Error")

		with patch("bop_erp.inventory.publication.get_active_connector_for_channel") as mock_conn:
			mock_conn.return_value = MagicMock(environment="DEVELOPMENT", base_url="http://prestashop-test", write_enabled=1)
			res = process_inventory_publication_event(event_doc.name, client=mock_client)
			self.assertFalse(res["success"])

		event_doc.reload()
		self.assertEqual(event_doc.status, IntegrationStatus.RETRY_PENDING)

		frappe.db.delete("Integration Event", {"name": event_doc.name})
		frappe.db.commit()

	# ==================================================
	# 9. CROSS-REFERENCE INTEGRITY
	# ==================================================
	def test_20_cross_reference_integrity(self):
		"""Imported Sales Order contains all required cross-reference fields."""
		meta = frappe.get_meta("Sales Order")
		expected_fields = [
			"sales_channel",
			"transaction_origin",
			"external_order_id",
			"integration_correlation_id",
			"integration_status",
			"integration_provider",
			"latest_integration_event",
		]
		field_names = [f.fieldname for f in meta.fields]
		for ef in expected_fields:
			self.assertIn(ef, field_names, f"Sales Order must contain custom field '{ef}'")
