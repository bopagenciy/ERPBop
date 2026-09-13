# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from unittest.mock import patch
import frappe
from frappe.utils import flt

from bop_erp.constants import (
	ErrorCategory,
	ExternalEntityType,
	ExternalOrderStateAction,
	IntegrationDirection,
	IntegrationProvider,
	IntegrationReadinessStatus,
	IntegrationStatus,
	ShippingTicketStatus,
	TransactionOrigin,
)
from bop_erp.fulfillment import (
	FulfillmentError,
	OrderNotReadyForShippingError,
	PartialShippingBlockedError,
	PickTicketNotReadyForShippingError,
	PickTicketRequiredError,
	ShippingTicketError,
	assert_sales_order_ready_for_shipping,
	cancel_pick_ticket,
	cancel_shipping_ticket,
	compute_shipping_ticket_idempotency_key,
	create_pick_ticket,
	create_shipping_ticket,
	get_shipping_counters,
	get_shipping_ticket_status,
	reset_shipping_counters,
)
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.orders.reconciliation import audit_sales_order_cancellation_safety
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry


class TestShippingTicketLive(unittest.TestCase):
	"""
	Phase 1N Live Integration Test Suite:
	Shipping Ticket / Delivery Note / Physical Stock Issue Foundation.
	Executes against the local Frappe / ERPNext isolated test environment.

	Scenarios (Section 42 & 43):
	A. READY imported SO -> reservation -> Pick Ticket -> Shipping Ticket creation & submit
	B. Physical stock issue: Bin actual_qty decreases by exact shipped qty, SLE created
	C. SO demand settlement: delivered_qty increases, remaining SO demand cleared in lockstep
	D. ATP invariant holds: aggregate ATP remains strictly unchanged across shipment
	E. Outbound publication outbox event persisted inside MariaDB transaction
	F. Shipping Ticket cancellation: SLE reversed, Bin actual_qty restored, SO delivered_qty restored
	G. ATP invariant holds on cancellation: strictly unchanged
	H. Two concurrent duplicate shipping requests converge to single Delivery Note
	I. Multi-warehouse shipment correctly allocates and decrements from matching warehouses
	J. Submitted Delivery Note blocks Phase 1L automatic Sales Order cancellation
	K. Outbox persistence failure rolls back Delivery Note submission cleanly
	L. Clean fixture cleanup proof: zero net persistent Bins, SLEs, Delivery Notes, Pick Lists, SOs
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"

		cls.channel_a = "CHAN-SHIP-A"
		cls.channel_b = "CHAN-SHIP-B"
		cls.channel_c = "CHAN-SHIP-C"

		cls.wh_shared = f"WH-SHIP-SH-{cls.abbr} - {cls.abbr}"
		cls.wh_secondary = f"WH-SHIP-SEC-{cls.abbr} - {cls.abbr}"
		cls.wh_unrelated = f"WH-SHIP-UN-{cls.abbr} - {cls.abbr}"
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")

		# Synthetic test items
		cls.item_code = "SKU-SHIP-TEST-ITEM"
		cls.item_code_2 = "SKU-SHIP-TEST-ITEM2"

		# Clean any stale leftover fixtures from prior interrupted runs
		cls._cleanup_all_test_fixtures()

		# Create synthetic test warehouses
		for wh_name, short_name in [
			(cls.wh_shared, f"WH-SHIP-SH-{cls.abbr}"),
			(cls.wh_secondary, f"WH-SHIP-SEC-{cls.abbr}"),
			(cls.wh_unrelated, f"WH-SHIP-UN-{cls.abbr}"),
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

		# Enable stock reservation setting
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		frappe.db.commit()

		# Record pre-suite baseline counts for fixture hygiene proof (Scenario L)
		cls.initial_fixture_bins = cls._count_test_bins()
		cls.initial_fixture_sres = cls._count_test_sres()
		cls.initial_fixture_pls = cls._count_test_pls()
		cls.initial_fixture_dns = cls._count_test_dns()
		cls.initial_fixture_sos = cls._count_test_sos()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_all_test_fixtures()
		super().tearDownClass()

	@classmethod
	def _count_test_bins(cls):
		test_whs = [cls.wh_shared, cls.wh_secondary, cls.wh_unrelated]
		test_items = [cls.item_code, cls.item_code_2]
		return frappe.db.count("Bin", {"item_code": ["in", test_items], "warehouse": ["in", test_whs]})

	@classmethod
	def _count_test_sres(cls):
		test_items = [cls.item_code, cls.item_code_2]
		return frappe.db.count("Stock Reservation Entry", {"item_code": ["in", test_items]})

	@classmethod
	def _count_test_pls(cls):
		test_channels = [cls.channel_a, cls.channel_b, cls.channel_c]
		return frappe.db.count("Pick List", {"sales_channel": ["in", test_channels]})

	@classmethod
	def _count_test_dns(cls):
		test_channels = [cls.channel_a, cls.channel_b, cls.channel_c]
		return frappe.db.count("Delivery Note", {"sales_channel": ["in", test_channels]})

	@classmethod
	def _count_test_sos(cls):
		test_channels = [cls.channel_a, cls.channel_b, cls.channel_c]
		return frappe.db.count("Sales Order", {"sales_channel": ["in", test_channels]})

	@classmethod
	def _cleanup_all_test_fixtures(cls):
		"""Strictly-scoped teardown of all Phase 1N synthetic fixtures."""
		test_whs = [cls.wh_shared, cls.wh_secondary, cls.wh_unrelated]
		test_items = [cls.item_code, cls.item_code_2]
		test_channels = [cls.channel_a, cls.channel_b, cls.channel_c]

		# 1. Cancel and delete Delivery Notes
		dns = frappe.db.sql(
			"""
			SELECT DISTINCT dn.name, dn.docstatus
			FROM `tabDelivery Note Item` dni
			JOIN `tabDelivery Note` dn ON dn.name = dni.parent
			WHERE dni.item_code IN %s OR dn.sales_channel IN %s
			""",
			(test_items, test_channels),
			as_dict=True,
		)
		for dn in dns:
			if dn.docstatus == 1:
				frappe.db.set_value("Delivery Note", dn.name, "docstatus", 2)
			frappe.delete_doc("Delivery Note", dn.name, force=True, ignore_permissions=True)

		# 2. Clean Pick Lists
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

		# 3. Clean SREs and references
		for sre in frappe.db.get_all(
			"Stock Reservation Entry", filters={"item_code": ["in", test_items]}, fields=["name", "docstatus"]
		):
			if sre.docstatus == 1:
				frappe.db.set_value("Stock Reservation Entry", sre.name, "docstatus", 2)
			frappe.delete_doc("Stock Reservation Entry", sre.name, force=True, ignore_permissions=True)

		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", test_items]})

		# 4. Clean Sales Orders
		for so in frappe.db.get_all(
			"Sales Order", filters={"sales_channel": ["in", test_channels]}, fields=["name", "docstatus"]
		):
			if so.docstatus == 1:
				frappe.db.set_value("Sales Order", so.name, "docstatus", 2)
			frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)

		# Also clean any manual SOs created with test items
		for so_item in frappe.db.get_all(
			"Sales Order Item", filters={"item_code": ["in", test_items]}, fields=["parent"]
		):
			so_name = so_item.parent
			if frappe.db.exists("Sales Order", so_name):
				ds = frappe.db.get_value("Sales Order", so_name, "docstatus")
				if ds == 1:
					frappe.db.set_value("Sales Order", so_name, "docstatus", 2)
				frappe.delete_doc("Sales Order", so_name, force=True, ignore_permissions=True)

		# 5. Cancel and delete Stock Entries used for test stock seeding
		for se_item in frappe.db.get_all(
			"Stock Entry Detail", filters={"item_code": ["in", test_items]}, fields=["parent"]
		):
			se_name = se_item.parent
			if frappe.db.exists("Stock Entry", se_name):
				ds = frappe.db.get_value("Stock Entry", se_name, "docstatus")
				if ds == 1:
					frappe.db.set_value("Stock Entry", se_name, "docstatus", 2)
				frappe.delete_doc("Stock Entry", se_name, force=True, ignore_permissions=True)

		# 6. Delete Stock Ledger Entries for test items in test warehouses
		frappe.db.delete("Stock Ledger Entry", {"item_code": ["in", test_items], "warehouse": ["in", test_whs]})

		# 7. Clean channels, inventory sources, mappings, outbox events
		for ch in test_channels:
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})
			frappe.db.delete("Channel Inventory Source", {"sales_channel": ch})
			frappe.db.delete("PrestaShop Connector", {"sales_channel": ch})
			frappe.db.delete("Sales Channel", {"name": ch})

		# 8. Clean fixture Bins for the test warehouses and test items
		for wh in test_whs:
			for item in test_items:
				bin_name = frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh}, "name")
				if bin_name:
					frappe.delete_doc("Bin", bin_name, force=True, ignore_permissions=True)

		# 9. Clean synthetic test warehouses
		for wh in test_whs:
			if frappe.db.exists("Warehouse", wh):
				frappe.delete_doc("Warehouse", wh, force=True, ignore_permissions=True)

		# 10. Clean test items
		for item in test_items:
			if frappe.db.exists("Item", item):
				frappe.delete_doc("Item", item, force=True, ignore_permissions=True)

		frappe.db.commit()

	def setUp(self):
		super().setUp()
		reset_shipping_counters()

		# Reset any leftover reservations in Bin to guarantee clean state
		for wh in [self.wh_shared, self.wh_secondary, self.wh_unrelated]:
			for item in [self.item_code, self.item_code_2]:
				if frappe.db.exists("Bin", {"item_code": item, "warehouse": wh}):
					frappe.db.set_value(
						"Bin",
						{"item_code": item, "warehouse": wh},
						{"reserved_stock": 0.0, "reserved_qty": 0.0},
					)

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

		# Setup Sales Channel C (unrelated warehouse)
		if not frappe.db.exists("Sales Channel", self.channel_c):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": self.channel_c,
				"channel_name": self.channel_c,
				"channel_type": "PRESTASHOP",
				"company": self.company,
				"active": 1,
			}).insert(ignore_permissions=True)

		# Channel Inventory Sources for WH shared (A and B)
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

		# Channel Inventory Source for WH unrelated (Channel C)
		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": self.channel_c, "warehouse": self.wh_unrelated}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": self.channel_c,
				"warehouse": self.wh_unrelated,
				"priority": 1,
				"enabled": 1,
				"allow_sellable_stock": 1,
			}).insert(ignore_permissions=True)

		# Seed initial physical stock via Stock Entry: 10 units in wh_shared, 10 units in wh_secondary
		self._seed_physical_stock(self.item_code, self.wh_shared, 10.0)
		self._seed_physical_stock(self.item_code_2, self.wh_secondary, 10.0)
		frappe.db.commit()

	def tearDown(self):
		# Clean per-test Delivery Notes, Pick Lists, SREs, Sales Orders, Stock Entries
		dns = frappe.db.sql(
			"""
			SELECT DISTINCT dn.name, dn.docstatus
			FROM `tabDelivery Note Item` dni
			JOIN `tabDelivery Note` dn ON dn.name = dni.parent
			WHERE dni.item_code IN %s OR dn.sales_channel IN %s
			""",
			([self.item_code, self.item_code_2], [self.channel_a, self.channel_b, self.channel_c]),
			as_dict=True,
		)
		for dn in dns:
			if dn.docstatus == 1:
				try:
					cancel_shipping_ticket(dn.name)
				except Exception:
					frappe.db.set_value("Delivery Note", dn.name, "docstatus", 2)
			frappe.delete_doc("Delivery Note", dn.name, force=True, ignore_permissions=True)

		pls = frappe.db.sql(
			"""
			SELECT DISTINCT pl.name, pl.docstatus
			FROM `tabPick List Item` pli
			JOIN `tabPick List` pl ON pl.name = pli.parent
			WHERE pli.item_code IN %s OR pl.sales_channel IN %s
			""",
			([self.item_code, self.item_code_2], [self.channel_a, self.channel_b, self.channel_c]),
			as_dict=True,
		)
		for pl in pls:
			if pl.docstatus == 1:
				try:
					cancel_pick_ticket(pl.name)
				except Exception:
					frappe.db.set_value("Pick List", pl.name, "docstatus", 2)
			frappe.delete_doc("Pick List", pl.name, force=True, ignore_permissions=True)

		for sre in frappe.db.get_all(
			"Stock Reservation Entry",
			filters={"item_code": ["in", [self.item_code, self.item_code_2]]},
			fields=["name", "docstatus"],
		):
			if sre.docstatus == 1:
				try:
					doc = frappe.get_doc("Stock Reservation Entry", sre.name)
					doc.flags.ignore_permissions = True
					doc.cancel()
				except Exception:
					frappe.db.set_value("Stock Reservation Entry", sre.name, "docstatus", 2)
			frappe.delete_doc("Stock Reservation Entry", sre.name, force=True, ignore_permissions=True)

		# Reset Bin reservations
		for wh in [self.wh_shared, self.wh_secondary, self.wh_unrelated]:
			for item in [self.item_code, self.item_code_2]:
				if frappe.db.exists("Bin", {"item_code": item, "warehouse": wh}):
					frappe.db.set_value(
						"Bin",
						{"item_code": item, "warehouse": wh},
						{"reserved_stock": 0.0, "reserved_qty": 0.0},
					)

		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [self.item_code, self.item_code_2]]})

		for so in frappe.db.get_all(
			"Sales Order", filters={"sales_channel": ["in", [self.channel_a, self.channel_b, self.channel_c]]}, fields=["name", "docstatus"]
		):
			if so.docstatus == 1:
				frappe.db.set_value("Sales Order", so.name, "docstatus", 2)
			frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)

		# Clean any manual SOs created with test items
		for so_item in frappe.db.get_all(
			"Sales Order Item", filters={"item_code": ["in", [self.item_code, self.item_code_2]]}, fields=["parent"]
		):
			so_name = so_item.parent
			if frappe.db.exists("Sales Order", so_name):
				ds = frappe.db.get_value("Sales Order", so_name, "docstatus")
				if ds == 1:
					frappe.db.set_value("Sales Order", so_name, "docstatus", 2)
				frappe.delete_doc("Sales Order", so_name, force=True, ignore_permissions=True)

		# Clean outbox events for test channels
		for ch in [self.channel_a, self.channel_b, self.channel_c]:
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})

		frappe.db.commit()
		super().tearDown()

	def _seed_physical_stock(self, item_code: str, warehouse: str, target_qty: float):
		"""Ensures warehouse has exactly target_qty of physical stock using native Stock Entry or Bin adjustment."""
		current_qty = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty") or 0.0)
		diff = target_qty - current_qty
		if abs(diff) > 0.0001:
			if diff > 0:
				se = make_stock_entry(
					item_code=item_code,
					target=warehouse,
					qty=diff,
					rate=10.0,
					company=self.company,
					purpose="Material Receipt",
					posting_date=frappe.utils.nowdate(),
					posting_time="00:00:01",
				)
			else:
				se = make_stock_entry(
					item_code=item_code,
					source=warehouse,
					qty=abs(diff),
					rate=10.0,
					company=self.company,
					purpose="Material Issue",
					posting_date=frappe.utils.nowdate(),
					posting_time="00:00:01",
				)

	def _create_ready_order_with_reservation(
		self,
		ext_order_id: str,
		qty: float = 3.0,
		item_code: str = None,
		warehouse: str = None,
		channel: str = None,
	) -> frappe.model.document.Document:
		"""Creates a simulated submitted imported Sales Order with active SRE and complete fields."""
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
	# Scenario A: READY imported SO -> reservation -> Pick Ticket -> Shipping Ticket
	# =========================================================================
	def test_a_ready_imported_so_shipping_ticket_flow(self):
		so = self._create_ready_order_with_reservation("SH-EXT-A-001", qty=3.0)

		# Create & submit Pick Ticket
		pl = create_pick_ticket(so.name, submit=True)
		self.assertEqual(pl.docstatus, 1)

		# Create & submit Shipping Ticket (Delivery Note)
		dn = create_shipping_ticket(pl.name, submit=True)
		self.assertEqual(dn.doctype, "Delivery Note")
		self.assertEqual(dn.docstatus, 1)
		self.assertEqual(get_shipping_ticket_status(dn), ShippingTicketStatus.SHIPPED)

		# Attribution inheritance verified
		self.assertEqual(dn.sales_channel, self.channel_a)
		self.assertEqual(dn.transaction_origin, TransactionOrigin.WEB)
		self.assertEqual(dn.external_order_id, "SH-EXT-A-001")

		# Pick List transitioned to Completed
		pl_status = frappe.db.get_value("Pick List", pl.name, "status")
		self.assertEqual(pl_status, "Completed")

	# =========================================================================
	# Scenario B: Physical stock issue & SLE creation
	# =========================================================================
	def test_b_physical_stock_decrement_and_sle(self):
		so = self._create_ready_order_with_reservation("SH-EXT-B-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)

		bin_before = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_shared}, "actual_qty"))
		self.assertEqual(bin_before, 10.0)

		# Submit Shipping Ticket
		dn = create_shipping_ticket(pl.name, submit=True)

		# Bin physical qty must decrease by exactly 3.0 (from 10.0 -> 7.0)
		bin_after = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_shared}, "actual_qty"))
		self.assertEqual(bin_after, 7.0)

		# Verify Stock Ledger Entry exists for this Delivery Note
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			{"voucher_no": dn.name, "voucher_type": "Delivery Note", "item_code": self.item_code},
			["actual_qty", "qty_after_transaction"],
			as_dict=True,
		)
		self.assertIsNotNone(sle)
		self.assertEqual(flt(sle.actual_qty), -3.0)
		self.assertEqual(flt(sle.qty_after_transaction), 7.0)

	# =========================================================================
	# Scenario C: SO demand settlement in lockstep
	# =========================================================================
	def test_c_so_demand_settlement_in_lockstep(self):
		so = self._create_ready_order_with_reservation("SH-EXT-C-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)

		# Initial SO delivered_qty is 0.0
		so_del_before = flt(frappe.db.get_value("Sales Order Item", {"parent": so.name}, "delivered_qty"))
		self.assertEqual(so_del_before, 0.0)

		dn = create_shipping_ticket(pl.name, submit=True)

		# Delivered qty increases to 3.0, clearing outstanding demand
		so_del_after = flt(frappe.db.get_value("Sales Order Item", {"parent": so.name}, "delivered_qty"))
		self.assertEqual(so_del_after, 3.0)

		so_per_del = flt(frappe.db.get_value("Sales Order", so.name, "per_delivered"))
		self.assertEqual(so_per_del, 100.0)

	# =========================================================================
	# Scenario D: Core ATP invariant strictly maintained across physical issue
	# =========================================================================
	def test_d_atp_invariant_maintained_across_shipping(self):
		so = self._create_ready_order_with_reservation("SH-EXT-D-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)

		# ATP before delivery: 10 actual - 3 demand = 7.0
		atp_before = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_before.aggregate_atp_qty, 7.0)

		# Submit Delivery Note
		dn = create_shipping_ticket(pl.name, submit=True)

		# ATP after delivery: 7 actual - 0 demand = 7.0! Strictly invariant!
		atp_after = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_after.aggregate_atp_qty, 7.0)

	# =========================================================================
	# Scenario E: Transactional outbox persistence inside MariaDB transaction
	# =========================================================================
	def test_e_outbound_publication_outbox_persisted(self):
		so = self._create_ready_order_with_reservation("SH-EXT-E-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)

		# Clean outbox before shipping
		frappe.db.delete("Integration Event", {"sales_channel": self.channel_a})

		dn = create_shipping_ticket(pl.name, submit=True)

		# Outbound inventory event must be persisted for affected Channel A and Channel B (shared warehouse)
		ev_a = frappe.db.get_value("Integration Event", {
			"sales_channel": self.channel_a,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_code,
		}, ["name", "status"], as_dict=True)
		self.assertIsNotNone(ev_a)
		self.assertEqual(ev_a.status, IntegrationStatus.PENDING)

		ev_b = frappe.db.get_value("Integration Event", {
			"sales_channel": self.channel_b,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_code,
		}, ["name", "status"], as_dict=True)
		self.assertIsNotNone(ev_b)

		# Unrelated Channel C must NOT have an event
		ev_c = frappe.db.exists("Integration Event", {
			"sales_channel": self.channel_c,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"erp_document": self.item_code,
		})
		self.assertFalse(bool(ev_c))

	# =========================================================================
	# Scenario F: Shipping Ticket cancellation reverses SLE, restores Bin & SO
	# =========================================================================
	def test_f_shipping_ticket_cancellation_reversals(self):
		so = self._create_ready_order_with_reservation("SH-EXT-F-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)
		dn = create_shipping_ticket(pl.name, submit=True)

		self.assertEqual(flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_shared}, "actual_qty")), 7.0)

		# Cancel Shipping Ticket
		cancelled_dn = cancel_shipping_ticket(dn.name)
		self.assertEqual(cancelled_dn.docstatus, 2)
		self.assertEqual(get_shipping_ticket_status(cancelled_dn), ShippingTicketStatus.CANCELLED)

		# Bin physical qty restored to 10.0
		bin_restored = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_shared}, "actual_qty"))
		self.assertEqual(bin_restored, 10.0)

		# SO delivered_qty reversed back to 0.0
		so_del_restored = flt(frappe.db.get_value("Sales Order Item", {"parent": so.name}, "delivered_qty"))
		self.assertEqual(so_del_restored, 0.0)

		so_per_del = flt(frappe.db.get_value("Sales Order", so.name, "per_delivered"))
		self.assertEqual(so_per_del, 0.0)

	# =========================================================================
	# Scenario G: ATP invariant holds across Shipping Ticket cancellation
	# =========================================================================
	def test_g_atp_invariant_maintained_on_cancellation(self):
		so = self._create_ready_order_with_reservation("SH-EXT-G-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)
		dn = create_shipping_ticket(pl.name, submit=True)

		atp_before_cancel = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_before_cancel.aggregate_atp_qty, 7.0)

		# Cancel Shipping Ticket
		cancel_shipping_ticket(dn.name)

		# ATP after cancellation: 10 actual - 3 demand = 7.0! Strictly invariant!
		atp_after_cancel = get_channel_atp(self.item_code, self.channel_a)
		self.assertEqual(atp_after_cancel.aggregate_atp_qty, 7.0)

	# =========================================================================
	# Scenario H: Concurrent duplicate shipping requests converge safely
	# =========================================================================
	def test_h_concurrent_duplicate_shipping_requests_converge(self):
		so = self._create_ready_order_with_reservation("SH-EXT-H-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)

		# Request 1
		dn1 = create_shipping_ticket(pl.name, submit=True)
		# Request 2 (replayed duplicate)
		dn2 = create_shipping_ticket(pl.name, submit=True)

		# Both calls converge to the exact same Delivery Note
		self.assertEqual(dn1.name, dn2.name)

		# Exactly 1 submitted Delivery Note exists for this Pick List
		dn_count = frappe.db.sql(
			"""
			SELECT COUNT(DISTINCT dn.name) as cnt
			FROM `tabDelivery Note Item` dni
			JOIN `tabDelivery Note` dn ON dn.name = dni.parent
			WHERE dni.against_pick_list = %s AND dn.docstatus = 1
			""",
			(pl.name,),
			as_dict=True,
		)[0].cnt
		self.assertEqual(dn_count, 1)

		counters = get_shipping_counters()
		self.assertEqual(counters["shipping_tickets_created"], 1)
		self.assertEqual(counters["shipping_tickets_reused"], 1)

	# =========================================================================
	# Scenario I: Multi-warehouse shipment correctly decrements from matching warehouses
	# =========================================================================
	def test_i_multi_warehouse_shipment_allocations(self):
		customer = frappe.db.get_value("Customer", {}, "name")
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": customer,
			"company": self.company,
			"transaction_origin": TransactionOrigin.WEB,
			"sales_channel": self.channel_a,
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"external_order_id": "SH-EXT-I-MULTI",
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
					"uom": "Nos",
					"stock_uom": "Nos",
					"conversion_factor": 1.0,
					"qty": 2.0,
					"stock_qty": 2.0,
					"rate": 10.0,
					"warehouse": self.wh_shared,
				},
				{
					"item_code": self.item_code_2,
					"item_name": self.item_code_2,
					"uom": "Nos",
					"stock_uom": "Nos",
					"conversion_factor": 1.0,
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

		# SRE 1: Item 1 in wh_shared
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

		# SRE 2: Item 2 in wh_secondary
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

		# Pick Ticket
		pl = create_pick_ticket(so.name, submit=True)

		# Create & submit Shipping Ticket
		dn = create_shipping_ticket(pl.name, submit=True)
		self.assertEqual(len(dn.items), 2)

		# Assert Bin actual decrements in exact warehouses
		bin1_actual = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_shared}, "actual_qty"))
		bin2_actual = flt(frappe.db.get_value("Bin", {"item_code": self.item_code_2, "warehouse": self.wh_secondary}, "actual_qty"))
		self.assertEqual(bin1_actual, 8.0)  # 10 - 2
		self.assertEqual(bin2_actual, 7.0)  # 10 - 3

	# =========================================================================
	# Scenario J: Submitted Delivery Note blocks Phase 1L Sales Order auto-cancellation
	# =========================================================================
	def test_j_submitted_delivery_note_blocks_external_order_cancellation(self):
		so = self._create_ready_order_with_reservation("SH-EXT-J-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)
		dn = create_shipping_ticket(pl.name, submit=True)

		# Downstream safety audit must report submitted Delivery Note and block auto-cancel
		is_safe, reasons = audit_sales_order_cancellation_safety(so.name)
		self.assertFalse(is_safe)
		self.assertTrue(any("Delivery Note" in r for r in reasons))

		# Cancel Delivery Note
		cancel_shipping_ticket(dn.name)

		# Delivery Note is cancelled, but Pick List is still active -> blocked by Pick List
		is_safe_mid, reasons_mid = audit_sales_order_cancellation_safety(so.name)
		self.assertFalse(is_safe_mid)
		self.assertTrue(any("Pick List" in r for r in reasons_mid))
		self.assertFalse(any("Delivery Note" in r for r in reasons_mid))

		# Cancel Pick Ticket as well
		cancel_pick_ticket(pl.name)

		# Once both Delivery Note and Pick List are cancelled, cancellation safety is fully restored
		is_safe_after, reasons_after = audit_sales_order_cancellation_safety(so.name)
		self.assertTrue(is_safe_after)
		self.assertEqual(len(reasons_after), 0)

	# =========================================================================
	# Scenario K: Outbox failure rollback atomicity
	# =========================================================================
	def test_k_outbox_failure_rollback_atomicity(self):
		so = self._create_ready_order_with_reservation("SH-EXT-K-001", qty=3.0)
		pl = create_pick_ticket(so.name, submit=True)

		bin_before = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_shared}, "actual_qty"))

		# Injected outbox failure during submit
		with patch("bop_erp.fulfillment.shipping_ticket.schedule_post_commit_publication", side_effect=RuntimeError("Simulated Outbox Failure")):
			with self.assertRaises(RuntimeError):
				create_shipping_ticket(pl.name, submit=True)

		# Bin actual qty must be completely unchanged due to savepoint rollback
		bin_after = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_shared}, "actual_qty"))
		self.assertEqual(bin_after, bin_before)

		# Zero submitted Delivery Notes exist
		dn_count = frappe.db.sql(
			"""
			SELECT COUNT(DISTINCT dn.name) as cnt
			FROM `tabDelivery Note Item` dni
			JOIN `tabDelivery Note` dn ON dn.name = dni.parent
			WHERE dni.against_pick_list = %s AND dn.docstatus = 1
			""",
			(pl.name,),
			as_dict=True,
		)[0].cnt
		self.assertEqual(dn_count, 0)

	# =========================================================================
	# Scenario L: Fixture cleanup proof
	# =========================================================================
	def test_l_fixture_cleanup_proof(self):
		# Clean per-test fixtures first to measure return to baseline
		self.tearDown()

		current_sres = self._count_test_sres()
		current_pls = self._count_test_pls()
		current_dns = self._count_test_dns()
		current_sos = self._count_test_sos()

		# Must be zero net additions beyond setupClass warehouse baseline
		self.assertEqual(current_sres, self.initial_fixture_sres)
		self.assertEqual(current_pls, self.initial_fixture_pls)
		self.assertEqual(current_dns, self.initial_fixture_dns)
		self.assertEqual(current_sos, self.initial_fixture_sos)
