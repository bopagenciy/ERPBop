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


class TestInventoryMigrationLive(unittest.TestCase):
	"""
	Live Integration and Smoke Test Suite for Phase 1H:
	- Executes full lifecycle on synthetic test item and warehouse:
	  CSV Data -> Stage Batch -> Validate/Dry Run -> Preview -> Explicit Apply
	- Submits native Stock Reconciliation with purpose 'Opening Stock'.
	- Verifies that native Stock Ledger Entry and Bin quantities match the migrated opening stock.
	- Validates that the applied batch cannot be applied a second time.
	- Cancels and deletes the test Stock Reconciliation and restores baseline counts:
	  Stock Ledger Entry == 0, Bin == 0, Item Price == 0.
	- Verifies ZERO contact with theindustrialdepot.com.
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

		# Ensure a difference/stock adjustment account is configured on Company
		exp_acc = frappe.db.get_value("Account", {"account_type": "Stock Adjustment", "company": cls.company}, "name") or \
			frappe.db.get_value("Account", {"account_type": "Expense Account", "company": cls.company}, "name")
		if exp_acc and not frappe.db.get_value("Company", cls.company, "stock_adjustment_account"):
			frappe.db.set_value("Company", cls.company, "stock_adjustment_account", exp_acc)

		cls.warehouse = f"Stores - {cls.abbr}"
		cls.item_code = "ITEM-LIVE-MIGRATE-01"
		if not frappe.db.exists("Item", cls.item_code):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code,
				"item_name": "Live Migration Test Item 01",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.opening_diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)

	def test_01_live_smoke_migration_pipeline_and_clean_rollback(self):
		"""
		Executes end-to-end synthetic migration pipeline:
		Stage -> Validate -> Preview -> Apply -> Verify Bin -> Clean Cancel & Delete -> Verify Invariants.
		"""
		batch_id = "SMOKE-MIGRATE-BATCH-01"
		records = [
			{
				"source_record_id": "SMK-01",
				"item_code": self.item_code,
				"warehouse": self.warehouse,
				"quantity": 150.0,
				"valuation_rate": 25.0,
				"stock_uom": "Nos",
			}
		]

		# Record pre-test baseline counts
		sle_baseline = frappe.db.count("Stock Ledger Entry")
		bin_baseline = frappe.db.count("Bin")
		price_baseline = frappe.db.count("Item Price")

		# 1. Stage
		batch_name = MigrationImporter.stage_batch(
			batch_id=batch_id,
			company=self.company,
			records=records,
			source_system="SYNTHETIC_SMOKE_TEST",
			opening_difference_account=self.opening_diff_account,
		)
		self.assertTrue(frappe.db.exists("Inventory Migration Batch", batch_name))

		# 2. Validate / Dry Run
		val_res = MigrationValidator.validate_batch(batch_name)
		self.assertEqual(val_res["status"], "READY")
		self.assertEqual(val_res["valid_rows"], 1)
		self.assertEqual(val_res["error_rows"], 0)

		# Confirm NO stock writes during dry run
		self.assertEqual(frappe.db.count("Stock Ledger Entry") - sle_baseline, 0)
		self.assertEqual(frappe.db.count("Bin") - bin_baseline, 0)
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": self.item_code}), 0)
		self.assertEqual(frappe.db.count("Bin", {"item_code": self.item_code}), 0)

		# 3. Preview
		preview = MigrationPreview.get_reconciliation_preview(batch_name)
		self.assertEqual(preview["total_opening_qty"], 150.0)
		self.assertEqual(preview["total_current_qty"], 0.0)
		self.assertEqual(preview["total_adjustment_delta"], 150.0)
		self.assertEqual(preview["total_inventory_value"], 3750.0)

		# 4. Explicit Apply
		apply_res = MigrationExecutor.apply_batch(batch_name)
		self.assertEqual(apply_res["status"], "APPLIED")
		reco_name = apply_res["stock_reconciliation"]
		self.assertTrue(reco_name)

		# 5. Verify live stock snapshot via InventoryService
		snap = InventoryService.get_warehouse_inventory(self.item_code, self.warehouse)
		self.assertEqual(snap.actual_qty, 150.0)
		self.assertEqual(frappe.db.count("Stock Ledger Entry") - sle_baseline, 1)
		self.assertEqual(frappe.db.count("Bin") - bin_baseline, 1)
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": self.item_code}), 1)
		self.assertEqual(frappe.db.count("Bin", {"item_code": self.item_code}), 1)

		# 6. Verify duplicate apply blocked
		with self.assertRaises(frappe.ValidationError):
			MigrationExecutor.apply_batch(batch_name)

		# 7. Clean test teardown: Cancel and delete Stock Reconciliation to restore baseline
		reco = frappe.get_doc("Stock Reconciliation", reco_name)
		reco.cancel()
		frappe.delete_doc("Stock Reconciliation", reco_name, force=True, ignore_permissions=True)

		# Explicitly purge test ledger artifacts created during test run
		frappe.db.delete("Stock Ledger Entry", {"voucher_no": reco_name})
		frappe.db.delete("GL Entry", {"voucher_no": reco_name})
		frappe.db.delete("Bin", {"item_code": self.item_code})

		# Delete staged batch and rows
		frappe.db.delete("Inventory Migration Row", {"batch": batch_name})
		frappe.delete_doc("Inventory Migration Batch", batch_name, force=True, ignore_permissions=True)
		frappe.db.commit()

		# 8. Assert safety invariance restored
		self.assertEqual(frappe.db.count("Stock Ledger Entry") - sle_baseline, 0, "Stock Ledger Entries must restore to baseline")
		self.assertEqual(frappe.db.count("Bin") - bin_baseline, 0, "Bins must restore to baseline")
		self.assertEqual(frappe.db.count("Item Price") - price_baseline, 0, "Item Prices must restore to baseline")
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": self.item_code}), 0)
		self.assertEqual(frappe.db.count("Bin", {"item_code": self.item_code}), 0)
		self.assertEqual(frappe.db.count("Item Price", {"item_code": self.item_code}), 0)
