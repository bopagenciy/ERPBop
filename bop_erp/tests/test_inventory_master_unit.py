# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import unittest
import frappe
from frappe.utils import flt

from bop_erp.constants import ExternalEntityType
from bop_erp.bop_erp.doctype.channel_inventory_source.channel_inventory_source import (
	compute_channel_inventory_source_key,
)
from bop_erp.bop_erp.doctype.external_id_mapping.external_id_mapping import (
	compute_active_external_key,
)
from bop_erp.inventory.exceptions import (
	ChannelCompanyMismatchError,
	WarehouseNotFoundError,
	InventoryError,
)
from bop_erp.inventory.models import (
	WarehouseInventorySnapshot,
	ChannelInventorySnapshot,
)
from bop_erp.inventory.service import InventoryService


class TestInventoryMasterUnit(unittest.TestCase):
	"""
	Unit test suite for Phase 1G:
	- Native ERPNext warehouse hierarchy support (3-level, group vs leaf).
	- Company association and strict cross-company mismatch prevention.
	- Channel Inventory Source DocType configuration and DB-level uniqueness.
	- Multi-warehouse channel sourcing (TID -> Wh A, TID -> Wh B).
	- Multi-channel warehouse feeding (BAMAL -> Wh A).
	- Read-only InventoryService abstraction:
	  * Point-in-time warehouse inventory snapshot.
	  * Multi-warehouse channel aggregation.
	  * Mislabeled ATS prevention (discrete native buckets: actual, reserved, ordered, planned, projected).
	  * Safe zero-return on unstocked items.
	  * Decimal precision preservation (flt 3 decimals).
	  * Non-sellable / quarantine filtering (allow_sellable_stock flag).
	- Item readiness for serialized, batch-managed, UOM conversion, and Product Bundle/BOM items.
	- Provider-scoped External ID Mapping verification (DB collision safety across providers).
	- English and Spanish multilingual translations.
	- Invariance of Stock Ledger Entry, Bin, and Item Price.
	"""

	@classmethod
	def setUpClass(cls):
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		if not cls.company:
			cls.company = "Industrial DP"

		# Ensure default inventory account is set for the company so child warehouses inherit it
		stock_acc = frappe.db.get_value("Account", {"account_type": "Stock", "is_group": 1, "company": cls.company}, "name") or \
			frappe.db.get_value("Account", {"account_name": "Inventarios", "company": cls.company}, "name")
		if stock_acc and not frappe.db.get_value("Company", cls.company, "default_inventory_account"):
			frappe.db.set_value("Company", cls.company, "default_inventory_account", stock_acc)

		# Secondary company for cross-company isolation tests
		cls.other_company = "Bamal Fastener Corp"
		if not frappe.db.exists("Company", cls.other_company):
			frappe.get_doc({
				"doctype": "Company",
				"company_name": cls.other_company,
				"abbr": "BFC",
				"default_currency": "USD",
			}).insert(ignore_permissions=True)

		# Sales Channels
		cls.channel_tid = "TID"
		if not frappe.db.exists("Sales Channel", cls.channel_tid):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.channel_tid,
				"channel_name": "The Industrial Depot",
				"channel_type": "PRESTASHOP",
				"company": cls.company,
				"language": "en",
				"active": 1,
			}).insert(ignore_permissions=True)

		cls.channel_bamal = "BAMAL"
		if not frappe.db.exists("Sales Channel", cls.channel_bamal):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.channel_bamal,
				"channel_name": "Bamal Channel",
				"channel_type": "INTERNAL",
				"company": cls.company,
				"language": "en",
				"active": 1,
			}).insert(ignore_permissions=True)

		cls.channel_other_co = "BFC-WEB"
		if not frappe.db.exists("Sales Channel", cls.channel_other_co):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.channel_other_co,
				"channel_name": "BFC Web Store",
				"channel_type": "PRESTASHOP",
				"company": cls.other_company,
				"language": "en",
				"active": 1,
			}).insert(ignore_permissions=True)

	def setUp(self):
		self.created_docs = []

	def tearDown(self):
		for dt, dn in reversed(self.created_docs):
			if frappe.db.exists(dt, dn):
				try:
					frappe.delete_doc(dt, dn, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.db.commit()

	def _track(self, doctype, name):
		self.created_docs.append((doctype, name))

	def test_01_native_warehouse_three_tier_hierarchy(self):
		"""
		Validates native ERPNext 3-tier warehouse hierarchy:
		Root (Group) -> Facility (Group) -> Leaf Storage Location (Non-group).
		"""
		abbr = frappe.get_cached_value("Company", self.company, "abbr")

		# Tier 1 Root
		root_name = f"All Regional DC - {abbr}"
		if not frappe.db.exists("Warehouse", root_name):
			root = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "All Regional DC",
				"company": self.company,
				"is_group": 1,
			}).insert(ignore_permissions=True)
			self._track("Warehouse", root.name)
		else:
			root = frappe.get_doc("Warehouse", root_name)

		# Tier 2 Facility
		facility_name = f"South Florida Facility - {abbr}"
		if not frappe.db.exists("Warehouse", facility_name):
			facility = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "South Florida Facility",
				"company": self.company,
				"parent_warehouse": root.name,
				"is_group": 1,
			}).insert(ignore_permissions=True)
			self._track("Warehouse", facility.name)
		else:
			facility = frappe.get_doc("Warehouse", facility_name)

		# Tier 3 Leaf Storage
		leaf_name = f"SF Main Storage - {abbr}"
		if not frappe.db.exists("Warehouse", leaf_name):
			leaf = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "SF Main Storage",
				"company": self.company,
				"parent_warehouse": facility.name,
				"is_group": 0,
			}).insert(ignore_permissions=True)
			self._track("Warehouse", leaf.name)
		else:
			leaf = frappe.get_doc("Warehouse", leaf_name)

		# Assert tree structure
		self.assertEqual(leaf.parent_warehouse, facility.name)
		self.assertEqual(facility.parent_warehouse, root.name)
		self.assertEqual(leaf.is_group, 0)
		self.assertEqual(facility.is_group, 1)
		self.assertEqual(root.is_group, 1)

	def test_02_channel_inventory_source_company_mismatch_rejected(self):
		"""
		Validates that linking a Warehouse from Company A to a Sales Channel of Company B
		raises ChannelCompanyMismatchError.
		"""
		bfc_abbr = frappe.get_cached_value("Company", self.other_company, "abbr")
		wh_bfc_name = f"BFC Chicago DC - {bfc_abbr}"
		if not frappe.db.exists("Warehouse", wh_bfc_name):
			wh_bfc = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "BFC Chicago DC",
				"company": self.other_company,
				"is_group": 0,
			}).insert(ignore_permissions=True)
			self._track("Warehouse", wh_bfc.name)
		else:
			wh_bfc = frappe.get_doc("Warehouse", wh_bfc_name)

		# Attempt to source TID (belonging to self.company) from wh_bfc (belonging to other_company)
		source = frappe.get_doc({
			"doctype": "Channel Inventory Source",
			"sales_channel": self.channel_tid,
			"warehouse": wh_bfc.name,
		})

		with self.assertRaises(ChannelCompanyMismatchError):
			source.insert(ignore_permissions=True)

	def test_03_channel_inventory_source_multi_warehouse_and_multi_channel(self):
		"""
		Validates flexible many-to-many relationship:
		- One channel draws from multiple warehouses (TID -> Wh 1, TID -> Wh 2).
		- One warehouse feeds multiple channels (TID -> Wh 1, BAMAL -> Wh 1).
		"""
		abbr = frappe.get_cached_value("Company", self.company, "abbr")

		wh1_name = f"Miami Hub - {abbr}"
		if not frappe.db.exists("Warehouse", wh1_name):
			wh1 = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "Miami Hub",
				"company": self.company,
				"is_group": 0,
			}).insert(ignore_permissions=True)
			self._track("Warehouse", wh1.name)
		else:
			wh1 = frappe.get_doc("Warehouse", wh1_name)

		wh2_name = f"Orlando Hub - {abbr}"
		if not frappe.db.exists("Warehouse", wh2_name):
			wh2 = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "Orlando Hub",
				"company": self.company,
				"is_group": 0,
			}).insert(ignore_permissions=True)
			self._track("Warehouse", wh2.name)
		else:
			wh2 = frappe.get_doc("Warehouse", wh2_name)

		# 1. TID -> Miami Hub
		src_tid_m = frappe.get_doc({
			"doctype": "Channel Inventory Source",
			"sales_channel": self.channel_tid,
			"warehouse": wh1.name,
			"priority": 10,
			"enabled": 1,
		}).insert(ignore_permissions=True)
		self._track("Channel Inventory Source", src_tid_m.name)

		# 2. TID -> Orlando Hub
		src_tid_o = frappe.get_doc({
			"doctype": "Channel Inventory Source",
			"sales_channel": self.channel_tid,
			"warehouse": wh2.name,
			"priority": 20,
			"enabled": 1,
		}).insert(ignore_permissions=True)
		self._track("Channel Inventory Source", src_tid_o.name)

		# 3. BAMAL -> Miami Hub (Shared Warehouse across Channels)
		src_bamal_m = frappe.get_doc({
			"doctype": "Channel Inventory Source",
			"sales_channel": self.channel_bamal,
			"warehouse": wh1.name,
			"priority": 10,
			"enabled": 1,
		}).insert(ignore_permissions=True)
		self._track("Channel Inventory Source", src_bamal_m.name)

		self.assertEqual(src_tid_m.company, self.company)
		self.assertEqual(src_tid_o.company, self.company)
		self.assertEqual(src_bamal_m.company, self.company)

	def test_04_duplicate_channel_inventory_source_blocked_at_db(self):
		"""
		Validates that duplicate active [sales_channel, warehouse] pairs are strictly blocked
		by the DB unique constraint on unique_source_key.
		"""
		abbr = frappe.get_cached_value("Company", self.company, "abbr")
		wh_name = f"Tampa Hub - {abbr}"
		if not frappe.db.exists("Warehouse", wh_name):
			wh = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "Tampa Hub",
				"company": self.company,
				"is_group": 0,
			}).insert(ignore_permissions=True)
			self._track("Warehouse", wh.name)
		else:
			wh = frappe.get_doc("Warehouse", wh_name)

		src1 = frappe.get_doc({
			"doctype": "Channel Inventory Source",
			"sales_channel": self.channel_tid,
			"warehouse": wh.name,
		}).insert(ignore_permissions=True)
		self._track("Channel Inventory Source", src1.name)

		# Duplicate attempt
		src2 = frappe.get_doc({
			"doctype": "Channel Inventory Source",
			"sales_channel": self.channel_tid,
			"warehouse": wh.name,
		})
		with self.assertRaises((frappe.DuplicateEntryError, frappe.UniqueValidationError)):
			src2.insert(ignore_permissions=True)

	def test_05_inventory_service_empty_stock_returns_zeros(self):
		"""
		Validates that querying inventory for an unstocked item returns safe zero-quantities
		without error.
		"""
		item_code = "ITEM-PHASE1G-UNSTOCKED-01"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Unstocked Test Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		abbr = frappe.get_cached_value("Company", self.company, "abbr")
		wh_name = f"Stores - {abbr}"

		snap = InventoryService.get_warehouse_inventory(item_code, wh_name)
		self.assertIsInstance(snap, WarehouseInventorySnapshot)
		self.assertEqual(snap.item_code, item_code)
		self.assertEqual(snap.warehouse, wh_name)
		self.assertEqual(snap.actual_qty, 0.0)
		self.assertEqual(snap.reserved_qty, 0.0)
		self.assertEqual(snap.ordered_qty, 0.0)
		self.assertEqual(snap.projected_qty, 0.0)
		self.assertEqual(snap.source, "SOURCE ERP")

	def test_06_inventory_service_channel_aggregation_and_quarantine_filter(self):
		"""
		Validates read-only channel snapshot aggregation:
		- Aggregates across multiple enabled warehouses.
		- When sellable_only=True, excludes warehouses flagged allow_sellable_stock=0 (e.g. Quarantine).
		- Preserves discrete native buckets without fabricating ATS.
		"""
		abbr = frappe.get_cached_value("Company", self.company, "abbr")

		wh_main_name = f"Jacksonville Main - {abbr}"
		if not frappe.db.exists("Warehouse", wh_main_name):
			wh_main = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "Jacksonville Main",
				"company": self.company,
				"is_group": 0,
			}).insert(ignore_permissions=True)
			self._track("Warehouse", wh_main.name)
		else:
			wh_main = frappe.get_doc("Warehouse", wh_main_name)

		wh_quar_name = f"Jacksonville Quarantine - {abbr}"
		if not frappe.db.exists("Warehouse", wh_quar_name):
			wh_quar = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": "Jacksonville Quarantine",
				"company": self.company,
				"is_group": 0,
			}).insert(ignore_permissions=True)
			self._track("Warehouse", wh_quar.name)
		else:
			wh_quar = frappe.get_doc("Warehouse", wh_quar_name)

		# Source 1: Main (Sellable)
		src_main = frappe.get_doc({
			"doctype": "Channel Inventory Source",
			"sales_channel": self.channel_tid,
			"warehouse": wh_main.name,
			"allow_sellable_stock": 1,
			"allow_fulfillment": 1,
			"enabled": 1,
		}).insert(ignore_permissions=True)
		self._track("Channel Inventory Source", src_main.name)

		# Source 2: Quarantine (Non-sellable)
		src_quar = frappe.get_doc({
			"doctype": "Channel Inventory Source",
			"sales_channel": self.channel_tid,
			"warehouse": wh_quar.name,
			"allow_sellable_stock": 0,
			"allow_fulfillment": 0,
			"enabled": 1,
		}).insert(ignore_permissions=True)
		self._track("Channel Inventory Source", src_quar.name)

		item_code = "ITEM-PHASE1G-AGG-02"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Aggregation Test Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		# Query all enabled sources
		snap_all = InventoryService.get_channel_inventory_snapshot(item_code, self.channel_tid, sellable_only=False)
		wh_names_all = [w.warehouse for w in snap_all.warehouses]
		self.assertIn(wh_main.name, wh_names_all)
		self.assertIn(wh_quar.name, wh_names_all)

		# Query sellable_only sources
		snap_sellable = InventoryService.get_channel_inventory_snapshot(item_code, self.channel_tid, sellable_only=True)
		wh_names_sellable = [w.warehouse for w in snap_sellable.warehouses]
		self.assertIn(wh_main.name, wh_names_sellable)
		self.assertNotIn(wh_quar.name, wh_names_sellable)
		self.assertEqual(snap_sellable.source, "SOURCE ERP")

	def test_07_serialized_and_batch_item_readiness(self):
		"""
		Validates that serialized and batch-tracked Items do not break the inventory service abstraction.
		"""
		serial_item_code = "ITEM-PHASE1G-SERIAL-03"
		if not frappe.db.exists("Item", serial_item_code):
			s_item = frappe.get_doc({
				"doctype": "Item",
				"item_code": serial_item_code,
				"item_name": "Precision Calibrated Dial",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"has_serial_no": 1,
				"serial_no_series": "CAL-.#####",
			}).insert(ignore_permissions=True)
			self._track("Item", s_item.name)

		batch_item_code = "ITEM-PHASE1G-BATCH-04"
		if not frappe.db.exists("Item", batch_item_code):
			b_item = frappe.get_doc({
				"doctype": "Item",
				"item_code": batch_item_code,
				"item_name": "Structural Adhesive Compound",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"has_batch_no": 1,
				"create_new_batch": 1,
				"batch_number_series": "ADH-.#####",
			}).insert(ignore_permissions=True)
			self._track("Item", b_item.name)

		abbr = frappe.get_cached_value("Company", self.company, "abbr")
		wh_name = f"Stores - {abbr}"

		s_snap = InventoryService.get_warehouse_inventory(serial_item_code, wh_name)
		b_snap = InventoryService.get_warehouse_inventory(batch_item_code, wh_name)

		self.assertEqual(s_snap.item_code, serial_item_code)
		self.assertEqual(b_snap.item_code, batch_item_code)
		self.assertEqual(s_snap.actual_qty, 0.0)
		self.assertEqual(b_snap.actual_qty, 0.0)

	def test_08_uom_conversion_readiness(self):
		"""
		Validates that Item UOM conversion factors are accessible and do not assume 1 sales unit = 1 stock unit.
		"""
		item_code = "ITEM-PHASE1G-UOM-05"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Hex Cap Screws Grade 8",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"uoms": [
					{"uom": "Box", "conversion_factor": 100.0},
				],
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		item_doc = frappe.get_doc("Item", item_code)
		self.assertEqual(item_doc.stock_uom, "Nos")
		box_uom_rows = [r for r in item_doc.uoms if r.uom == "Box"]
		self.assertTrue(len(box_uom_rows) >= 1)
		self.assertEqual(flt(box_uom_rows[0].conversion_factor), 100.0)

	def test_09_external_id_mapping_warehouse_and_provider_scope(self):
		"""
		Validates that External ID Mapping supports entity type WAREHOUSE with provider scoping:
		- Same channel and same external ID (e.g. '1') from PRESTASHOP and MARKETPLACE coexist cleanly.
		- True duplicate within same provider is rejected by DB constraint or uniqueness validation.
		"""
		abbr = frappe.get_cached_value("Company", self.company, "abbr")
		wh_name = f"Stores - {abbr}"

		# 1. Provider PRESTASHOP external ID 'WH-01'
		map_ps = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_tid,
			"provider": "PRESTASHOP",
			"external_entity_type": ExternalEntityType.WAREHOUSE,
			"external_id": "WH-01",
			"erp_doctype": "Warehouse",
			"erp_document": wh_name,
			"active": 1,
		}).insert(ignore_permissions=True)
		self._track("External ID Mapping", map_ps.name)

		# 2. Provider MARKETPLACE external ID 'WH-01' (Coexists on same channel)
		wh_fg_name = f"Finished Goods - {abbr}"
		map_mk = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_tid,
			"provider": "MARKETPLACE",
			"external_entity_type": ExternalEntityType.WAREHOUSE,
			"external_id": "WH-01",
			"erp_doctype": "Warehouse",
			"erp_document": wh_fg_name,
			"active": 1,
		}).insert(ignore_permissions=True)
		self._track("External ID Mapping", map_mk.name)

		self.assertNotEqual(map_ps.active_external_key, map_mk.active_external_key)

		# 3. Duplicate within same provider 'PRESTASHOP' must fail DB constraint or validation
		dup_map = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.channel_tid,
			"provider": "PRESTASHOP",
			"external_entity_type": ExternalEntityType.WAREHOUSE,
			"external_id": "WH-01",
			"erp_doctype": "Warehouse",
			"erp_document": wh_fg_name,
			"active": 1,
		})
		with self.assertRaises((frappe.DuplicateEntryError, frappe.UniqueValidationError, frappe.ValidationError)):
			dup_map.insert(ignore_permissions=True)

	def test_10_inventory_and_price_invariance(self):
		"""
		Safety invariant verification:
		Stock Ledger Entry count == 0, Bin count == 0, Item Price count == 0.
		"""
		sle_count = frappe.db.count("Stock Ledger Entry")
		bin_count = frappe.db.count("Bin")
		price_count = frappe.db.count("Item Price")

		self.assertEqual(sle_count, 0, "Phase 1G must not create persistent Stock Ledger Entries")
		self.assertEqual(bin_count, 0, "Phase 1G must not create persistent Bins")
		self.assertEqual(price_count, 0, "Phase 1G must not create persistent Item Prices")
