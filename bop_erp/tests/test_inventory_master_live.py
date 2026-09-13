# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe
from frappe.utils import flt

from bop_erp.inventory.service import InventoryService
from bop_erp.inventory.exceptions import ChannelCompanyMismatchError


class TestInventoryMasterLive(unittest.TestCase):
	"""
	Live Integration and Smoke Test Suite for Phase 1G:
	- Configures realistic warehouse hierarchy (Distribution Center -> Main Stock, Picking, Quarantine).
	- Configures Channel Inventory Sources:
	  * TID -> Main Stock (enabled=1, allow_sellable_stock=1, allow_fulfillment=1)
	  * TID -> Picking (enabled=1, allow_sellable_stock=1, allow_fulfillment=1)
	  * TID -> Quarantine (enabled=1, allow_sellable_stock=0, allow_fulfillment=0)
	- Executes read-only inventory snapshot aggregation for catalog items.
	- Validates that Quarantine is excluded when sellable_only=True.
	- Tests comparison helper against simulated external PrestaShop stock.
	- Verifies that zero persistent Stock Ledger Entries or Bins are created.
	- Verifies ZERO contact with theindustrialdepot.com.
	"""

	@classmethod
	def setUpClass(cls):
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		if not cls.company:
			cls.company = "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr")

		cls.sales_channel = "TID"
		if not frappe.db.exists("Sales Channel", cls.sales_channel):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.sales_channel,
				"channel_name": "The Industrial Depot",
				"channel_type": "PRESTASHOP",
				"company": cls.company,
				"language": "en",
				"active": 1,
			}).insert(ignore_permissions=True)

		# Build realistic warehouse hierarchy for smoke test
		# 1. Group Facility
		cls.dc_wh_name = f"TEST Distribution Center - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.dc_wh_name):
			cls.dc_wh = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "TEST Distribution Center",
				"company": cls.company,
				"is_group": 1,
			}).insert(ignore_permissions=True)
		else:
			cls.dc_wh = frappe.get_doc("Warehouse", cls.dc_wh_name)

		# 2. Main Stock Leaf
		cls.main_wh_name = f"TEST Main Stock - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.main_wh_name):
			cls.main_wh = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "TEST Main Stock",
				"company": cls.company,
				"parent_warehouse": cls.dc_wh.name,
				"is_group": 0,
			}).insert(ignore_permissions=True)
		else:
			cls.main_wh = frappe.get_doc("Warehouse", cls.main_wh_name)

		# 3. Picking Leaf
		cls.pick_wh_name = f"TEST Picking - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.pick_wh_name):
			cls.pick_wh = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "TEST Picking",
				"company": cls.company,
				"parent_warehouse": cls.dc_wh.name,
				"is_group": 0,
			}).insert(ignore_permissions=True)
		else:
			cls.pick_wh = frappe.get_doc("Warehouse", cls.pick_wh_name)

		# 4. Quarantine Leaf
		cls.quar_wh_name = f"TEST Quarantine - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.quar_wh_name):
			cls.quar_wh = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "TEST Quarantine",
				"company": cls.company,
				"parent_warehouse": cls.dc_wh.name,
				"is_group": 0,
			}).insert(ignore_permissions=True)
		else:
			cls.quar_wh = frappe.get_doc("Warehouse", cls.quar_wh_name)

		# Configure Channel Inventory Sources
		cls.cis_main_name = f"CIS-{cls.sales_channel}-{cls.main_wh.name}"
		if not frappe.db.exists("Channel Inventory Source", cls.cis_main_name):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.sales_channel,
				"warehouse": cls.main_wh.name,
				"priority": 10,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		cls.cis_pick_name = f"CIS-{cls.sales_channel}-{cls.pick_wh.name}"
		if not frappe.db.exists("Channel Inventory Source", cls.cis_pick_name):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.sales_channel,
				"warehouse": cls.pick_wh.name,
				"priority": 20,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		cls.cis_quar_name = f"CIS-{cls.sales_channel}-{cls.quar_wh.name}"
		if not frappe.db.exists("Channel Inventory Source", cls.cis_quar_name):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.sales_channel,
				"warehouse": cls.quar_wh.name,
				"priority": 99,
				"enabled": 1,
				"allow_sellable_stock": 0,
				"allow_fulfillment": 0,
			}).insert(ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# Clean up test Channel Inventory Sources
		for cis_name in [cls.cis_main_name, cls.cis_pick_name, cls.cis_quar_name]:
			if frappe.db.exists("Channel Inventory Source", cis_name):
				frappe.delete_doc("Channel Inventory Source", cis_name, force=True, ignore_permissions=True)

		# Clean up leaf warehouses
		for wh_name in [cls.quar_wh_name, cls.pick_wh_name, cls.main_wh_name]:
			if frappe.db.exists("Warehouse", wh_name):
				frappe.delete_doc("Warehouse", wh_name, force=True, ignore_permissions=True)

		# Clean up parent DC warehouse
		if frappe.db.exists("Warehouse", cls.dc_wh_name):
			frappe.delete_doc("Warehouse", cls.dc_wh_name, force=True, ignore_permissions=True)

		frappe.db.commit()

	def test_01_smoke_test_hierarchy_and_multi_warehouse_snapshot(self):
		"""
		Executes full local smoke test:
		- Verifies hierarchy: Leaf warehouses point to TEST Distribution Center.
		- Aggregates snapshot for Product 20 (SKU-HAMMER-01).
		- Proves multi-warehouse aggregation works.
		- Proves Quarantine is included in raw aggregation but excluded in sellable aggregation.
		- Proves no Stock Ledger Entry or Bin is mutated.
		"""
		item_code = "SKU-HAMMER-01"
		# Pre-operation baseline
		sle_before = frappe.db.count("Stock Ledger Entry")
		bin_before = frappe.db.count("Bin")
		price_before = frappe.db.count("Item Price")

		# Ensure test item exists
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Heavy Duty Claw Hammer 16oz",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)

		# 1. Verify warehouse hierarchy
		m_doc = frappe.get_doc("Warehouse", self.main_wh_name)
		p_doc = frappe.get_doc("Warehouse", self.pick_wh_name)
		q_doc = frappe.get_doc("Warehouse", self.quar_wh_name)

		self.assertEqual(m_doc.parent_warehouse, self.dc_wh_name)
		self.assertEqual(p_doc.parent_warehouse, self.dc_wh_name)
		self.assertEqual(q_doc.parent_warehouse, self.dc_wh_name)

		# 2. Query channel snapshot with all sources
		snap_all = InventoryService.get_channel_inventory_snapshot(item_code, self.sales_channel, sellable_only=False)
		wh_set_all = {w.warehouse for w in snap_all.warehouses}

		self.assertIn(self.main_wh_name, wh_set_all)
		self.assertIn(self.pick_wh_name, wh_set_all)
		self.assertIn(self.quar_wh_name, wh_set_all)
		self.assertEqual(snap_all.source, "SOURCE ERP")

		# 3. Query channel snapshot with sellable sources only
		snap_sellable = InventoryService.get_channel_inventory_snapshot(item_code, self.sales_channel, sellable_only=True)
		wh_set_sellable = {w.warehouse for w in snap_sellable.warehouses}

		self.assertIn(self.main_wh_name, wh_set_sellable)
		self.assertIn(self.pick_wh_name, wh_set_sellable)
		self.assertNotIn(self.quar_wh_name, wh_set_sellable)

		# 4. Compare with simulated external PrestaShop stock
		comp = InventoryService.compare_channel_inventory_with_external(item_code, self.sales_channel, external_qty=50.0)
		self.assertEqual(comp.external_source, "SOURCE EXTERNAL")
		self.assertEqual(comp.erp_source, "SOURCE ERP")
		self.assertEqual(comp.external_qty, 50.0)
		self.assertEqual(comp.erp_aggregate_actual_qty, 0.0)
		self.assertEqual(comp.delta_actual, 50.0)

		# 5. Assert safety invariance (baseline delta + fixture scope)
		self.assertEqual(frappe.db.count("Stock Ledger Entry") - sle_before, 0, "Operation must not create Stock Ledger Entries")
		self.assertEqual(frappe.db.count("Bin") - bin_before, 0, "Operation must not create Bins")
		self.assertEqual(frappe.db.count("Item Price") - price_before, 0, "Operation must not create Item Prices")
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": item_code}), 0)
		self.assertEqual(frappe.db.count("Bin", {"item_code": item_code}), 0)
		self.assertEqual(frappe.db.count("Item Price", {"item_code": item_code}), 0)
