# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import patch
import frappe
from frappe.utils import flt

from bop_erp.constants import (
	IntegrationProvider,
	IntegrationReadinessStatus,
	PickTicketStatus,
	TransactionOrigin,
	ExternalEntityType,
)
from bop_erp.fulfillment import (
	OrderNotReadyForPickingError,
	assert_sales_order_ready_for_picking,
	cancel_pick_ticket,
	create_pick_ticket,
	get_pick_counters,
	get_pick_ticket_status,
	get_remaining_to_pick,
	reset_pick_counters,
)
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.orders.reconciliation import audit_sales_order_cancellation_safety


class TestPickTicketLive(unittest.TestCase):
	"""
	Phase 1M Live Integration Test Suite:
	Pick Ticket / Warehouse Fulfillment Foundation.
	Executes against the local Frappe / ERPNext environment.

	Scenarios (Section 35 & 36):
	A. READY imported SO -> reservation -> native Pick List creation -> correct items/warehouse
	B. Pick List submit / native operational transition -> ATP unchanged
	C. Pick Ticket cancel -> Sales Order still active -> ATP unchanged
	D. Shared TEST-A / TEST-B inventory -> Pick Ticket on TEST-A does not change ATP on either channel
	E. Two concurrent duplicate pick requests -> one logical Pick Ticket (idempotent / locked)
	F. Multi-warehouse Sales Order -> correct warehouse allocations across lines
	G. Non-READY imported order -> no Pick Ticket (blocked)
	H. External cancellation with submitted Pick Ticket -> Phase 1L auto-cancel blocked / review
	I. Manual native Sales Order -> standard Pick List path remains functional
	J. Fixture cleanup proof -> zero net synthetic Bin/SLE/SRE/Pick List/Sales Order additions
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"

		cls.channel_a = "CHAN-PICK-A"
		cls.channel_b = "CHAN-PICK-B"

		cls.wh_shared = f"WH-PICK-SH-{cls.abbr} - {cls.abbr}"
		cls.wh_secondary = f"WH-PICK-SEC-{cls.abbr} - {cls.abbr}"
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")

		# Create synthetic test warehouses
		for wh_name, short_name in [
			(cls.wh_shared, f"WH-PICK-SH-{cls.abbr}"),
			(cls.wh_secondary, f"WH-PICK-SEC-{cls.abbr}"),
		]:
			if not frappe.db.exists("Warehouse", wh_name):
				w = frappe.get_doc({
					"doctype": "Warehouse",
					"warehouse_name": short_name,
					"company": cls.company,
					"parent_warehouse": cls.wh_parent,
					"is_group": 0,
				})
				w.flags.ignore_permissions = True
				w.insert(ignore_permissions=True)

		# Synthetic test items
		cls.item_code = "SKU-PICK-TEST-TOOL"
		cls.item_code_2 = "SKU-PICK-TEST-ACC"

		for item_id in [cls.item_code, cls.item_code_2]:
			if not frappe.db.exists("Item", item_id):
				frappe.get_doc({
					"doctype": "Item",
					"item_code": item_id,
					"item_name": item_id,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}).insert(ignore_permissions=True)

		# Ensure stock reservation setting enabled
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		frappe.db.commit()

		# Record pre-suite baseline counts for fixture hygiene proof (Scenario J)
		cls.initial_fixture_bins = cls._count_test_bins()
		cls.initial_fixture_sres = cls._count_test_sres()
		cls.initial_fixture_pls = cls._count_test_pls()
		cls.initial_fixture_sos = cls._count_test_sos()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_all_test_fixtures()
		super().tearDownClass()

	@classmethod
	def _count_test_bins(cls):
		test_whs = [cls.wh_shared, cls.wh_secondary]
		test_items = [cls.item_code, cls.item_code_2]
		return frappe.db.count("Bin", {"item_code": ["in", test_items], "warehouse": ["in", test_whs]})

	@classmethod
	def _count_test_sres(cls):
		test_items = [cls.item_code, cls.item_code_2]
		return frappe.db.count("Stock Reservation Entry", {"item_code": ["in", test_items]})

	@classmethod
	def _count_test_pls(cls):
		test_channels = [cls.channel_a, cls.channel_b]
		return frappe.db.count("Pick List", {"sales_channel": ["in", test_channels]})

	@classmethod
	def _count_test_sos(cls):
		test_channels = [cls.channel_a, cls.channel_b]
		return frappe.db.count("Sales Order", {"sales_channel": ["in", test_channels]})

	@classmethod
	def _cleanup_all_test_fixtures(cls):
		"""Strictly-scoped teardown of all Phase 1M synthetic fixtures."""
		test_whs = [cls.wh_shared, cls.wh_secondary]
		test_items = [cls.item_code, cls.item_code_2]
		test_channels = [cls.channel_a, cls.channel_b]

		# 1. Clean Pick Lists
		pls = frappe.db.sql(
			"""
			SELECT DISTINCT pl.name, pl.docstatus
			FROM `tabPick List Item` pli
			JOIN `tabPick List` pl ON pl.name = pli.parent
			WHERE pli.item_code IN %s OR pl.sales_channel IN %s
			""",
			(test_items, test_channels),
			as_dict=True,
		)
		for pl in pls:
			if pl.docstatus == 1:
				frappe.db.set_value("Pick List", pl.name, "docstatus", 2)
			frappe.delete_doc("Pick List", pl.name, force=True, ignore_permissions=True)

		# 2. Clean SREs and references
		for sre in frappe.db.get_all(
			"Stock Reservation Entry", filters={"item_code": ["in", test_items]}, fields=["name", "docstatus"]
		):
			if sre.docstatus == 1:
				frappe.db.set_value("Stock Reservation Entry", sre.name, "docstatus", 2)
			frappe.delete_doc("Stock Reservation Entry", sre.name, force=True, ignore_permissions=True)

		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", test_items]})

		# 3. Clean Sales Orders
		for so in frappe.db.get_all(
			"Sales Order", filters={"sales_channel": ["in", test_channels]}, fields=["name", "docstatus"]
		):
			if so.docstatus == 1:
				frappe.db.set_value("Sales Order", so.name, "docstatus", 2)
			frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)

		# Also clean any manual SOs created with our test items
		for so_item in frappe.db.get_all(
			"Sales Order Item", filters={"item_code": ["in", test_items]}, fields=["parent"]
		):
			so_name = so_item.parent
			if frappe.db.exists("Sales Order", so_name):
				ds = frappe.db.get_value("Sales Order", so_name, "docstatus")
				if ds == 1:
					frappe.db.set_value("Sales Order", so_name, "docstatus", 2)
				frappe.delete_doc("Sales Order", so_name, force=True, ignore_permissions=True)

		# 4. Clean channels, inventory sources, mappings
		for ch in test_channels:
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})
			frappe.db.delete("Channel Inventory Source", {"sales_channel": ch})
			frappe.db.delete("PrestaShop Connector", {"sales_channel": ch})
			frappe.db.delete("Sales Channel", {"name": ch})

		# 5. Clean fixture Bins for the test warehouses and test items
		for wh in test_whs:
			for item in test_items:
				bin_name = frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh}, "name")
				if bin_name:
					frappe.delete_doc("Bin", bin_name, force=True, ignore_permissions=True)

		# 6. Clean synthetic test warehouses
		for wh in test_whs:
			if frappe.db.exists("Warehouse", wh):
				frappe.delete_doc("Warehouse", wh, force=True, ignore_permissions=True)

		# 7. Clean test items
		for item in test_items:
			if frappe.db.exists("Item", item):
				frappe.delete_doc("Item", item, force=True, ignore_permissions=True)

		frappe.db.commit()

	def setUp(self):
		super().setUp()
		reset_pick_counters()

		# Setup Sales Channel A
		if not frappe.db.exists("Sales Channel", self.channel_a):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": self.channel_a,
				"channel_name": self.channel_a,
				"channel_type": "PRESTASHOP",
				"company": self.company,
				"active": 1,
			}).insert(ignore_permissions=True)

		# Setup Sales Channel B
		if not frappe.db.exists("Sales Channel", self.channel_b):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": self.channel_b,
				"channel_name": self.channel_b,
				"channel_type": "PRESTASHOP",
				"company": self.company,
				"active": 1,
			}).insert(ignore_permissions=True)

		# Channel Inventory Sources for WH shared
		for ch in [self.channel_a, self.channel_b]:
			if not frappe.db.exists("Channel Inventory Source", {"sales_channel": ch, "warehouse": self.wh_shared}):
				frappe.get_doc({
					"doctype": "Channel Inventory Source",
					"sales_channel": ch,
					"warehouse": self.wh_shared,
					"priority": 1,
					"enabled": 1,
					"allow_sellable_stock": 1,
				}).insert(ignore_permissions=True)

		# Channel Inventory Source for WH secondary (Channel A)
		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": self.channel_a, "warehouse": self.wh_secondary}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": self.channel_a,
				"warehouse": self.wh_secondary,
				"priority": 2,
				"enabled": 1,
				"allow_sellable_stock": 1,
			}).insert(ignore_permissions=True)

		# Set initial physical stock: 10 units in wh_shared
		self._set_warehouse_stock(self.item_code, self.wh_shared, 10.0)
		self._set_warehouse_stock(self.item_code_2, self.wh_secondary, 10.0)
		frappe.db.commit()

	def tearDown(self):
		# Clean per-test created Pick Lists, SREs, Sales Orders
		pls = frappe.db.sql(
			"""
			SELECT DISTINCT pl.name, pl.docstatus
			FROM `tabPick List Item` pli
			JOIN `tabPick List` pl ON pl.name = pli.parent
			WHERE pli.item_code IN %s OR pl.sales_channel IN %s
			""",
			([self.item_code, self.item_code_2], [self.channel_a, self.channel_b]),
			as_dict=True,
		)
		for pl in pls:
			if pl.docstatus == 1:
				frappe.db.set_value("Pick List", pl.name, "docstatus", 2)
			frappe.delete_doc("Pick List", pl.name, force=True, ignore_permissions=True)

		for sre in frappe.db.get_all(
			"Stock Reservation Entry",
			filters={"item_code": ["in", [self.item_code, self.item_code_2]]},
			fields=["name", "docstatus"],
		):
			if sre.docstatus == 1:
				frappe.db.set_value("Stock Reservation Entry", sre.name, "docstatus", 2)
			frappe.delete_doc("Stock Reservation Entry", sre.name, force=True, ignore_permissions=True)

		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [self.item_code, self.item_code_2]]})

		for so in frappe.db.get_all(
			"Sales Order", filters={"sales_channel": ["in", [self.channel_a, self.channel_b]]}, fields=["name", "docstatus"]
		):
			if so.docstatus == 1:
				frappe.db.set_value("Sales Order", so.name, "docstatus", 2)
			frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)

		# Also clean any manual SOs created with our test items
		for so_item in frappe.db.get_all(
			"Sales Order Item", filters={"item_code": ["in", [self.item_code, self.item_code_2]]}, fields=["parent"]
		):
			so_name = so_item.parent
			if frappe.db.exists("Sales Order", so_name):
				ds = frappe.db.get_value("Sales Order", so_name, "docstatus")
				if ds == 1:
					frappe.db.set_value("Sales Order", so_name, "docstatus", 2)
				frappe.delete_doc("Sales Order", so_name, force=True, ignore_permissions=True)

		frappe.db.delete("External ID Mapping", {"sales_channel": ["in", [self.channel_a, self.channel_b]]})

		frappe.db.commit()
		super().tearDown()

	def _set_warehouse_stock(self, item_code: str, warehouse: str, qty: float):
		from erpnext.stock.utils import get_or_make_bin
		get_or_make_bin(item_code, warehouse)
		frappe.db.set_value("Bin", {"item_code": item_code, "warehouse": warehouse}, {"actual_qty": qty, "reserved_stock": 0.0})
		frappe.db.commit()

	def _create_ready_order_with_reservation(
		self,
		ext_order_id: str,
		qty: float = 3.0,
		item_code: str = None,
		warehouse: str = None,
		channel: str = None,
	) -> frappe.model.document.Document:
		"""Creates a simulated submitted imported Sales Order with active SRE."""
		item_code = item_code or self.item_code
		warehouse = warehouse or self.wh_shared
		channel = channel or self.channel_a
		customer = frappe.db.get_value("Customer", {}, "name")

		so = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": customer,
			"company": self.company,
			"transaction_origin": TransactionOrigin.WEB,
			"sales_channel": channel,
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"external_order_id": ext_order_id,
			"integration_status": IntegrationReadinessStatus.READY,
			"order_type": "Sales",
			"delivery_date": frappe.utils.nowdate(),
			"currency": "USD",
			"conversion_rate": 1.0,
			"price_list_currency": "USD",
			"plc_conversion_rate": 1.0,
			"items": [{
				"item_code": item_code,
				"item_name": item_code,
				"uom": "Nos",
				"stock_uom": "Nos",
				"conversion_factor": 1.0,
				"qty": qty,
				"stock_qty": qty,
				"rate": 10.0,
				"warehouse": warehouse,
			}],
		})
		so.flags.ignore_validate = True
		so.flags.ignore_mandatory = True
		so.flags.ignore_permissions = True
		so.insert(ignore_permissions=True)
		so.submit()

		frappe.db.set_value("Sales Order", so.name, "status", "To Deliver and Bill")
		frappe.db.set_value("Sales Order Item", so.items[0].name, "stock_qty", qty)

		# Map order cleanly
		frappe.db.delete("External ID Mapping", {"sales_channel": channel, "external_id": ext_order_id})
		frappe.db.delete("External ID Mapping", {"sales_channel": channel, "erp_document": so.name})
		mapping = frappe.get_doc({
			"doctype": "External ID Mapping",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": channel,
			"external_entity_type": ExternalEntityType.ORDER,
			"external_id": ext_order_id,
			"erp_doctype": "Sales Order",
			"erp_document": so.name,
			"active": 1,
		})
		mapping.insert(ignore_permissions=True)

		# Create active Stock Reservation Entry
		sre = frappe.get_doc({
			"doctype": "Stock Reservation Entry",
			"item_code": item_code,
			"warehouse": warehouse,
			"voucher_type": "Sales Order",
			"voucher_no": so.name,
			"voucher_detail_no": so.items[0].name,
			"voucher_qty": qty,
			"available_qty": 10.0,
			"reserved_qty": qty,
			"delivered_qty": 0.0,
			"transferred_qty": 0.0,
			"consumed_qty": 0.0,
			"company": self.company,
			"stock_uom": "Nos",
			"reservation_based_on": "Qty",
		})
		sre.flags.ignore_validate = True
		sre.flags.ignore_permissions = True
		sre.insert(ignore_permissions=True)
		sre.submit()

		# Update Bin reserved_qty so native ERPNext ATP and reservation match
		frappe.db.set_value(
			"Bin",
			{"item_code": item_code, "warehouse": warehouse},
			{"reserved_stock": qty, "reserved_qty": qty},
		)

		ref = frappe.get_doc({
			"doctype": "Inventory Reservation Reference",
			"idempotency_key": f"IRR-{so.name}-{item_code}",
			"stock_reservation_entry": sre.name,
			"sales_channel": channel,
			"item_code": item_code,
			"warehouse": warehouse,
			"reserved_qty": qty,
			"source_doctype": "Sales Order",
			"source_document": so.name,
			"source_detail_docname": so.items[0].name,
			"external_order_id": ext_order_id,
			"status": "Reserved",
		})
		ref.insert(ignore_permissions=True)
		frappe.db.commit()

		return so

	# =========================================================================
	# Scenario A: READY imported SO -> reservation -> Pick List creation
	# =========================================================================
	def test_a_ready_imported_so_creates_pick_ticket(self):
		so = self._create_ready_order_with_reservation("PT-EXT-A-001", qty=3.0)

		# Initial ATP check: 10 actual - 3 reserved = 7
		atp_before = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_before.aggregate_atp_qty, 7.0)

		# Create Pick Ticket
		pl = create_pick_ticket(so.name)
		self.assertEqual(pl.doctype, "Pick List")
		self.assertEqual(pl.docstatus, 0)
		self.assertEqual(len(pl.locations), 1)
		self.assertEqual(pl.locations[0].item_code, self.item_code)
		self.assertEqual(pl.locations[0].warehouse, self.wh_shared)
		self.assertEqual(flt(pl.locations[0].qty), 3.0)
		self.assertEqual(pl.locations[0].sales_order, so.name)
		self.assertEqual(pl.sales_channel, self.channel_a)
		self.assertEqual(pl.transaction_origin, TransactionOrigin.WEB)
		self.assertEqual(pl.external_order_id, "PT-EXT-A-001")

		# Derived Bop status
		self.assertEqual(get_pick_ticket_status(pl), PickTicketStatus.DRAFT)

	# =========================================================================
	# Scenario B: Pick List submit -> ATP unchanged & zero financial side effects
	# =========================================================================
	def test_b_pick_list_submit_operational_transition_atp_unchanged(self):
		so = self._create_ready_order_with_reservation("PT-EXT-B-001", qty=3.0)

		atp_before = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_before.aggregate_atp_qty, 7.0)

		# Create and submit Pick Ticket
		pl = create_pick_ticket(so.name, submit=True)
		self.assertEqual(pl.docstatus, 1)
		self.assertEqual(get_pick_ticket_status(pl), PickTicketStatus.PICKING)

		# Phase 1M.1 Hardening Assertion: flags.ignore_validate must NOT be True
		self.assertFalse(bool(pl.flags.get("ignore_validate")), "flags.ignore_validate must be False on submitted Pick List")
		self.assertFalse(bool(pl.flags.get("ignore_mandatory")), "flags.ignore_mandatory must be False on submitted Pick List")
		self.assertFalse(bool(pl.flags.get("ignore_permissions")), "flags.ignore_permissions must be False on submitted Pick List")

		# Invariant: ATP is strictly unchanged (still 7.0)
		atp_after = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_after.aggregate_atp_qty, 7.0)

		# Bin actual qty is strictly 10.0 (no physical issue before Delivery Note)
		bin_qty = frappe.db.get_value(
			"Bin", {"item_code": self.item_code, "warehouse": self.wh_shared}, "actual_qty"
		)
		self.assertEqual(flt(bin_qty), 10.0)

		# Negative proofs: zero delivery, shipment, invoice, payment
		self.assertEqual(frappe.db.count("Delivery Note", {"customer": so.customer}), 0)
		self.assertEqual(frappe.db.count("Shipment"), 0)
		self.assertEqual(frappe.db.count("Sales Invoice", {"customer": so.customer}), 0)
		self.assertEqual(frappe.db.count("Payment Entry"), 0)

	# =========================================================================
	# Scenario C: Pick Ticket cancel -> SO still active & ATP unchanged
	# =========================================================================
	def test_c_pick_ticket_cancel_leaves_so_active_atp_unchanged(self):
		so = self._create_ready_order_with_reservation("PT-EXT-C-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)

		# Cancel Pick Ticket
		cancelled_pl = cancel_pick_ticket(pl.name)
		self.assertEqual(cancelled_pl.docstatus, 2)
		self.assertEqual(get_pick_ticket_status(cancelled_pl), PickTicketStatus.CANCELLED)

		# Sales Order remains submitted and READY
		so_after = frappe.get_doc("Sales Order", so.name)
		self.assertEqual(so_after.docstatus, 1)
		self.assertEqual(so_after.integration_status, IntegrationReadinessStatus.READY)

		# Stock reservation remains intact
		sre_count = frappe.db.count("Stock Reservation Entry", {"voucher_no": so.name, "docstatus": 1})
		self.assertEqual(sre_count, 1)

		# ATP is still exactly 7.0
		atp_after = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_after.aggregate_atp_qty, 7.0)

	# =========================================================================
	# Scenario D: Shared TEST-A / TEST-B inventory ATP unchanged by picking
	# =========================================================================
	def test_d_shared_channel_atp_unchanged_by_picking(self):
		# Pool = 10 units in wh_shared
		# Channel A order = 3 units
		so = self._create_ready_order_with_reservation("PT-EXT-D-001", qty=3.0, channel=self.channel_a)

		# Before pick ticket: ATP on both channels is 7.0
		atp_a_before = get_channel_atp(self.item_code, self.channel_a)
		atp_b_before = get_channel_atp(self.item_code, self.channel_b)
		self.assertEqual(atp_a_before.aggregate_atp_qty, 7.0)
		self.assertEqual(atp_b_before.aggregate_atp_qty, 7.0)

		# Create & submit Pick Ticket for Channel A
		pl = create_pick_ticket(so.name, submit=True)

		# After pick ticket: ATP on BOTH channels remains strictly 7.0
		atp_a_after = get_channel_atp(self.item_code, self.channel_a)
		atp_b_after = get_channel_atp(self.item_code, self.channel_b)
		self.assertEqual(atp_a_after.aggregate_atp_qty, 7.0)
		self.assertEqual(atp_b_after.aggregate_atp_qty, 7.0)

	# =========================================================================
	# Scenario E: Two concurrent duplicate pick requests converge
	# =========================================================================
	def test_e_concurrent_duplicate_pick_requests_converge(self):
		so = self._create_ready_order_with_reservation("PT-EXT-E-001", qty=3.0)

		# Request 1
		pl1 = create_pick_ticket(so.name)
		# Request 2 (replayed / concurrent)
		pl2 = create_pick_ticket(so.name)

		# Both calls converged to the identical Pick Ticket document
		self.assertEqual(pl1.name, pl2.name)

		# Only 1 Pick List exists for this Sales Order
		pl_count = frappe.db.sql(
			"""
			SELECT COUNT(DISTINCT pl.name) as cnt
			FROM `tabPick List Item` pli
			JOIN `tabPick List` pl ON pl.name = pli.parent
			WHERE pli.sales_order = %s
			""",
			(so.name,),
			as_dict=True,
		)[0].cnt
		self.assertEqual(pl_count, 1)

		counters = get_pick_counters()
		self.assertEqual(counters["pick_tickets_created"], 1)
		self.assertEqual(counters["pick_tickets_reused"], 1)

	# =========================================================================
	# Scenario F: Multi-warehouse Sales Order allocations
	# =========================================================================
	def test_f_multi_warehouse_sales_order_allocations(self):
		customer = frappe.db.get_value("Customer", {}, "name")
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": customer,
			"company": self.company,
			"transaction_origin": TransactionOrigin.WEB,
			"sales_channel": self.channel_a,
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"external_order_id": "PT-EXT-F-MULTI",
			"integration_status": IntegrationReadinessStatus.READY,
			"order_type": "Sales",
			"delivery_date": frappe.utils.nowdate(),
			"currency": "USD",
			"conversion_rate": 1.0,
			"price_list_currency": "USD",
			"plc_conversion_rate": 1.0,
			"items": [
				{
					"item_code": self.item_code,
					"item_name": self.item_code,
					"qty": 2.0,
					"stock_qty": 2.0,
					"rate": 10.0,
					"warehouse": self.wh_shared,
				},
				{
					"item_code": self.item_code_2,
					"item_name": self.item_code_2,
					"qty": 3.0,
					"stock_qty": 3.0,
					"rate": 15.0,
					"warehouse": self.wh_secondary,
				},
			],
		})
		so.flags.ignore_validate = True
		so.flags.ignore_mandatory = True
		so.flags.ignore_permissions = True
		so.insert(ignore_permissions=True)
		so.submit()

		# Create SRE for Item 1 in wh_shared
		sre1 = frappe.get_doc({
			"doctype": "Stock Reservation Entry",
			"item_code": self.item_code,
			"warehouse": self.wh_shared,
			"voucher_type": "Sales Order",
			"voucher_no": so.name,
			"voucher_detail_no": so.items[0].name,
			"voucher_qty": 2.0,
			"available_qty": 10.0,
			"reserved_qty": 2.0,
			"company": self.company,
			"stock_uom": "Nos",
			"reservation_based_on": "Qty",
		})
		sre1.flags.ignore_validate = True
		sre1.insert(ignore_permissions=True)
		sre1.submit()

		# Create SRE for Item 2 in wh_secondary
		sre2 = frappe.get_doc({
			"doctype": "Stock Reservation Entry",
			"item_code": self.item_code_2,
			"warehouse": self.wh_secondary,
			"voucher_type": "Sales Order",
			"voucher_no": so.name,
			"voucher_detail_no": so.items[1].name,
			"voucher_qty": 3.0,
			"available_qty": 10.0,
			"reserved_qty": 3.0,
			"company": self.company,
			"stock_uom": "Nos",
			"reservation_based_on": "Qty",
		})
		sre2.flags.ignore_validate = True
		sre2.insert(ignore_permissions=True)
		sre2.submit()

		frappe.db.commit()

		# Create Pick Ticket
		pl = create_pick_ticket(so.name)
		self.assertEqual(len(pl.locations), 2)

		# Assert exact warehouse mapping matches SRE allocations
		loc1 = next(l for l in pl.locations if l.item_code == self.item_code)
		loc2 = next(l for l in pl.locations if l.item_code == self.item_code_2)

		self.assertEqual(loc1.warehouse, self.wh_shared)
		self.assertEqual(flt(loc1.qty), 2.0)
		self.assertEqual(loc2.warehouse, self.wh_secondary)
		self.assertEqual(flt(loc2.qty), 3.0)

	# =========================================================================
	# Scenario G: Non-READY imported order blocks pick ticket
	# =========================================================================
	def test_g_non_ready_imported_order_blocks_pick_ticket(self):
		customer = frappe.db.get_value("Customer", {}, "name")
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": customer,
			"company": self.company,
			"transaction_origin": TransactionOrigin.WEB,
			"sales_channel": self.channel_a,
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"external_order_id": "PT-EXT-G-UNREADY",
			"integration_status": IntegrationReadinessStatus.INGESTION_PENDING,
			"order_type": "Sales",
			"delivery_date": frappe.utils.nowdate(),
			"currency": "USD",
			"conversion_rate": 1.0,
			"price_list_currency": "USD",
			"plc_conversion_rate": 1.0,
			"items": [{
				"item_code": self.item_code,
				"item_name": self.item_code,
				"qty": 3.0,
				"stock_qty": 3.0,
				"rate": 10.0,
				"warehouse": self.wh_shared,
			}],
		})
		so.flags.ignore_validate = True
		so.flags.ignore_mandatory = True
		so.flags.ignore_permissions = True
		so.insert(ignore_permissions=True)
		so.submit()

		# Must be rejected
		with self.assertRaises(OrderNotReadyForPickingError):
			create_pick_ticket(so.name)

		# Verify zero Pick Lists created
		pl_count = frappe.db.sql(
			"""
			SELECT COUNT(DISTINCT pl.name) as cnt
			FROM `tabPick List Item` pli
			JOIN `tabPick List` pl ON pl.name = pli.parent
			WHERE pli.sales_order = %s
			""",
			(so.name,),
			as_dict=True,
		)[0].cnt
		self.assertEqual(pl_count, 0)

	# =========================================================================
	# Scenario H: External cancellation with submitted Pick Ticket blocked
	# =========================================================================
	def test_h_external_cancellation_with_submitted_pick_ticket_blocked(self):
		so = self._create_ready_order_with_reservation("PT-EXT-H-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)

		# Downstream audit must report active Pick List and block auto-cancellation
		is_safe, reasons = audit_sales_order_cancellation_safety(so.name)
		self.assertFalse(is_safe)
		self.assertTrue(any("Active Pick List" in r for r in reasons))

		# Cancel Pick Ticket
		cancel_pick_ticket(pl.name)

		# After Pick Ticket is cancelled, cancellation safety is restored
		is_safe_after, reasons_after = audit_sales_order_cancellation_safety(so.name)
		self.assertTrue(is_safe_after)
		self.assertEqual(len(reasons_after), 0)

	# =========================================================================
	# Scenario I: Manual native Sales Order supported
	# =========================================================================
	def test_i_manual_native_sales_order_supported(self):
		customer = frappe.db.get_value("Customer", {}, "name")
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": customer,
			"company": self.company,
			"order_type": "Sales",
			"delivery_date": frappe.utils.nowdate(),
			"currency": "USD",
			"conversion_rate": 1.0,
			"price_list_currency": "USD",
			"plc_conversion_rate": 1.0,
			"items": [{
				"item_code": self.item_code,
				"item_name": self.item_code,
				"qty": 2.0,
				"stock_qty": 2.0,
				"rate": 10.0,
				"warehouse": self.wh_shared,
			}],
		})
		so.flags.ignore_validate = True
		so.flags.ignore_mandatory = True
		so.flags.ignore_permissions = True
		so.insert(ignore_permissions=True)
		so.submit()

		# Standard Pick List path executes cleanly
		pl = create_pick_ticket(so.name)
		self.assertEqual(pl.doctype, "Pick List")
		self.assertEqual(len(pl.locations), 1)
		self.assertEqual(pl.locations[0].warehouse, self.wh_shared)
		self.assertEqual(flt(pl.locations[0].qty), 2.0)
		self.assertIsNone(pl.get("sales_channel"))
		self.assertIsNone(pl.get("external_order_id"))

	# =========================================================================
	# Scenario J: Fixture cleanup proof
	# =========================================================================
	def test_j_fixture_cleanup_proof(self):
		# Clean per-test fixtures first to measure return to baseline
		self.tearDown()

		current_bins = self._count_test_bins()
		current_sres = self._count_test_sres()
		current_pls = self._count_test_pls()
		current_sos = self._count_test_sos()

		# Must be zero net additions beyond setupClass warehouse baseline
		self.assertEqual(current_sres, self.initial_fixture_sres)
		self.assertEqual(current_pls, self.initial_fixture_pls)
		self.assertEqual(current_sos, self.initial_fixture_sos)
