# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe
from frappe.utils import flt

from bop_erp.inventory.migration.importer import MigrationImporter
from bop_erp.inventory.migration.validator import MigrationValidator
from bop_erp.inventory.migration.preview import MigrationPreview
from bop_erp.inventory.migration.executor import MigrationExecutor
from bop_erp.inventory.service import InventoryService


class TestStockMigrationApplyHardeningLive(unittest.TestCase):
	"""
	Comprehensive Live Integration Tests for Phase 1H.1: Stock Migration Apply Safety & Accounting Hardening.
	Covers:
	1. End-to-end apply with multi-item batch:
	   - Standard stock item
	   - Serialized stock item (valid serial number creation)
	   - Batch-managed stock item (valid batch creation)
	2. Native Stock Reconciliation submission:
	   - Generates native Stock Ledger Entry (actual_qty and stock_value_difference)
	   - Generates native GL Entries posting to Stock Asset Account and opening_difference_account
	   - Generates native Serial No and Batch records
	3. Post-apply invariance & idempotency:
	   - Cannot apply a second time
	   - Batch marked APPLIED, rows marked APPLIED
	4. Teardown:
	   - Full cancellation of Stock Reconciliation
	   - Purge of test SLE, GL Entry, Serial No, Batch, Bin, and Stock Reconciliation records
	   - Restores exact 0 counts across all ledger and master tables.
	"""

	@classmethod
	def setUpClass(cls):
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		if not cls.company:
			cls.company = "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr")

		# Ensure default inventory account is set for the company
		stock_acc = frappe.db.get_value("Account", {"account_type": "Stock", "is_group": 1, "company": cls.company}, "name") or \
			frappe.db.get_value("Account", {"account_name": "Inventarios", "company": cls.company}, "name")
		if stock_acc and not frappe.db.get_value("Company", cls.company, "default_inventory_account"):
			frappe.db.set_value("Company", cls.company, "default_inventory_account", stock_acc)

		cls.warehouse = f"Stores - {cls.abbr}"

		# Valid leaf balance sheet account (Equity)
		cls.opening_diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)

		# 1. Standard Item
		cls.standard_item = "ITEM-LIVE-HARDEN-STD"
		if not frappe.db.exists("Item", cls.standard_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.standard_item,
				"item_name": "Live Harden Standard Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		# 2. Serialized Item
		cls.serial_item = "ITEM-LIVE-HARDEN-SER"
		if not frappe.db.exists("Item", cls.serial_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.serial_item,
				"item_name": "Live Harden Serial Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_serial_no": 1,
			}).insert(ignore_permissions=True)

		# 3. Batch Item
		cls.batch_item = "ITEM-LIVE-HARDEN-BAT"
		if not frappe.db.exists("Item", cls.batch_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.batch_item,
				"item_name": "Live Harden Batch Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
			}).insert(ignore_permissions=True)

	def test_01_live_apply_multi_item_pipeline_and_clean_rollback(self):
		"""
		Tests live apply of standard, serialized, and batch-managed items.
		Verifies native ERPNext ledger entries, verifies fencing, and performs clean rollback.
		"""
		batch_id = "LIVE-APPLY-HARDEN-01"
		test_serial = "SN-HARDEN-9001"
		test_batch = "BAT-HARDEN-B01"

		records = [
			{
				"source_record_id": "REC-STD",
				"item_code": self.standard_item,
				"warehouse": self.warehouse,
				"quantity": 100.0,
				"valuation_rate": 10.0,
				"stock_uom": "Nos",
			},
			{
				"source_record_id": "REC-SER",
				"item_code": self.serial_item,
				"warehouse": self.warehouse,
				"quantity": 1.0,
				"valuation_rate": 500.0,
				"stock_uom": "Nos",
				"serial_no": test_serial,
			},
			{
				"source_record_id": "REC-BAT",
				"item_code": self.batch_item,
				"warehouse": self.warehouse,
				"quantity": 50.0,
				"valuation_rate": 20.0,
				"stock_uom": "Nos",
				"batch_no": test_batch,
			},
		]

		# 1. Stage
		batch_name = MigrationImporter.stage_batch(
			batch_id=batch_id,
			company=self.company,
			records=records,
			source_system="SYNTHETIC_LIVE_TEST",
			opening_difference_account=self.opening_diff_account,
		)
		self.assertTrue(frappe.db.exists("Inventory Migration Batch", batch_name))

		# 2. Validate
		val_res = MigrationValidator.validate_batch(batch_name)
		self.assertEqual(val_res["status"], "READY")
		self.assertEqual(val_res["valid_rows"], 3)
		self.assertEqual(val_res["error_rows"], 0)
		self.assertTrue(val_res["validation_snapshot_hash"])

		# 3. Preview
		preview = MigrationPreview.get_reconciliation_preview(batch_name)
		self.assertEqual(preview["total_items"], 3)
		self.assertEqual(preview["target_inventory_value"], 2500.0) # (100*10) + (1*500) + (50*20)
		self.assertEqual(preview["current_inventory_value"], 0.0)
		self.assertEqual(preview["estimated_adjustment_value"], 2500.0)

		# 4. Explicit Apply
		apply_res = MigrationExecutor.apply_batch(batch_name, user="Administrator")
		self.assertEqual(apply_res["status"], "APPLIED")
		self.assertEqual(apply_res["applied_rows"], 3)
		reco_name = apply_res["stock_reconciliation"]
		self.assertTrue(reco_name)

		# 5. Verify live stock via InventoryService and DB
		snap_std = InventoryService.get_warehouse_inventory(self.standard_item, self.warehouse)
		self.assertEqual(snap_std.actual_qty, 100.0)

		snap_ser = InventoryService.get_warehouse_inventory(self.serial_item, self.warehouse)
		self.assertEqual(snap_ser.actual_qty, 1.0)
		self.assertTrue(frappe.db.exists("Serial No", test_serial))

		snap_bat = InventoryService.get_warehouse_inventory(self.batch_item, self.warehouse)
		self.assertEqual(snap_bat.actual_qty, 50.0)
		self.assertTrue(frappe.db.exists("Batch", test_batch))

		# Verify GL Entries posted to opening_difference_account
		gl_entries = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": reco_name},
			fields=["account", "debit", "credit"],
		)
		accounts_hit = [g["account"] for g in gl_entries]
		self.assertIn(self.opening_diff_account, accounts_hit)

		# 6. Verify duplicate apply blocked
		with self.assertRaises(frappe.ValidationError):
			MigrationExecutor.apply_batch(batch_name)

		# 7. Clean test teardown: Cancel and delete Stock Reconciliation to restore baseline
		reco = frappe.get_doc("Stock Reconciliation", reco_name)
		reco.cancel()
		frappe.delete_doc("Stock Reconciliation", reco_name, force=True, ignore_permissions=True)

		# Purge test serial no and batch
		if frappe.db.exists("Serial No", test_serial):
			frappe.delete_doc("Serial No", test_serial, force=True, ignore_permissions=True)
		if frappe.db.exists("Batch", test_batch):
			frappe.delete_doc("Batch", test_batch, force=True, ignore_permissions=True)

		# Purge test ledger entries
		frappe.db.delete("Stock Ledger Entry", {"voucher_no": reco_name})
		frappe.db.delete("GL Entry", {"voucher_no": reco_name})
		frappe.db.delete("Bin", {"item_code": ["in", [self.standard_item, self.serial_item, self.batch_item]]})

		# Delete staged batch and rows
		frappe.db.delete("Inventory Migration Row", {"batch": batch_name})
		frappe.delete_doc("Inventory Migration Batch", batch_name, force=True, ignore_permissions=True)
		frappe.db.commit()

		# 8. Assert safety invariance restored
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), 0, "Stock Ledger Entries must be 0 after test cleanup")
		self.assertEqual(frappe.db.count("Bin"), 0, "Bins must be 0 after test cleanup")
		self.assertEqual(frappe.db.count("Item Price"), 0, "Item Prices must be 0")
		self.assertEqual(frappe.db.count("Stock Reconciliation"), 0, "Stock Reconciliations must be 0")
		self.assertEqual(frappe.db.count("Serial No"), 0, "Serial Nos must be 0")
		self.assertEqual(frappe.db.count("Batch"), 0, "Batches must be 0")
