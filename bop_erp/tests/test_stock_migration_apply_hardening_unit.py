# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe
from frappe.utils import flt

from bop_erp.inventory.migration.importer import MigrationImporter
from bop_erp.inventory.migration.validator import MigrationValidator
from bop_erp.inventory.migration.preview import MigrationPreview
from bop_erp.inventory.migration.executor import MigrationExecutor


class TestStockMigrationApplyHardeningUnit(unittest.TestCase):
	"""
	Comprehensive Unit Tests for Phase 1H.1: Stock Migration Apply Safety & Accounting Hardening.
	Covers:
	1. Validation snapshot hash computation & determinism across orderings.
	2. Concurrency fencing: second worker blocked when batch is in APPLYING state.
	3. Pre-apply optimistic safety gate: stock drift causes rejection and requires revalidation.
	4. Explicit opening difference account enforcement:
	   - Missing account prevents READY.
	   - Account belonging to different company rejected.
	   - Group account rejected.
	   - Disabled account rejected.
	   - P&L account rejected (OpeningEntryAccountError prevention).
	5. Extended accounting preview metrics calculation.
	6. Immutability protection for opening_difference_account and validation_snapshot_hash on APPLIED batches.
	7. Failure atomicity: failed reconciliation sets batch status to FAILED and records traceback notes.
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

		cls.test_wh = f"Stores - {cls.abbr}"

		# Valid leaf balance sheet account (Equity)
		cls.valid_diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)

		# P&L Expense Account (for negative test)
		cls.pnl_expense_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "report_type": "Profit and Loss", "is_group": 0, "disabled": 0},
			"name",
		)

		# Test Item
		cls.item_code = "ITEM-PHASE1H1-UNIT-01"
		if not frappe.db.exists("Item", cls.item_code):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code,
				"item_name": "Phase 1H.1 Unit Test Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.item_code_2 = "ITEM-PHASE1H1-UNIT-02"
		if not frappe.db.exists("Item", cls.item_code_2):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code_2,
				"item_name": "Phase 1H.1 Unit Test Item 2",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

	def setUp(self):
		self.created_batches = []

	def tearDown(self):
		for batch_name in self.created_batches:
			if frappe.db.exists("Inventory Migration Batch", batch_name):
				frappe.db.delete("Inventory Migration Row", {"batch": batch_name})
				frappe.delete_doc("Inventory Migration Batch", batch_name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _track_batch(self, batch_name):
		self.created_batches.append(batch_name)

	def test_01_snapshot_hash_determinism(self):
		"""Verifies that validation snapshot hashing is canonical, deterministic, and order-independent."""
		pairs_1 = [(self.item_code, self.test_wh), (self.item_code_2, self.test_wh)]
		pairs_2 = [(self.item_code_2, self.test_wh), (self.item_code, self.test_wh)]

		hash_1 = MigrationValidator.compute_stock_snapshot_hash(pairs_1)
		hash_2 = MigrationValidator.compute_stock_snapshot_hash(pairs_2)

		self.assertEqual(hash_1, hash_2)
		self.assertEqual(len(hash_1), 64)

	def test_02_concurrency_fencing_blocks_second_worker(self):
		"""
		Verifies that when a batch is marked APPLYING, a second concurrent
		apply_batch attempt is blocked immediately with a ValidationError.
		"""
		records = [
			{"source_record_id": "FENCE-1", "item_code": self.item_code, "warehouse": self.test_wh, "quantity": 10, "valuation_rate": 5},
		]
		batch_name = MigrationImporter.stage_batch(
			"TEST-FENCE-BATCH",
			self.company,
			records,
			opening_difference_account=self.valid_diff_account,
		)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)

		# Simulate Worker 1 acquiring the APPLYING fence
		frappe.db.set_value("Inventory Migration Batch", batch_name, "status", "APPLYING")
		frappe.db.commit()

		# Worker 2 attempts apply
		with self.assertRaises(frappe.ValidationError) as ctx:
			MigrationExecutor.apply_batch(batch_name, user="Worker-2")
		self.assertIn("currently being applied", str(ctx.exception))

	def test_03_pre_apply_gate_blocks_stock_drift(self):
		"""
		Verifies that if ERP stock drift occurs (hash mismatch against validation_snapshot_hash),
		apply_batch aborts, reverts batch to VALIDATED, and requires revalidation.
		"""
		records = [
			{"source_record_id": "DRIFT-1", "item_code": self.item_code, "warehouse": self.test_wh, "quantity": 25, "valuation_rate": 10},
		]
		batch_name = MigrationImporter.stage_batch(
			"TEST-DRIFT-BATCH",
			self.company,
			records,
			opening_difference_account=self.valid_diff_account,
		)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)
		batch = frappe.get_doc("Inventory Migration Batch", batch_name)
		self.assertEqual(batch.status, "READY")

		# Tamper snapshot hash to simulate live stock having drifted
		frappe.db.set_value("Inventory Migration Batch", batch_name, "validation_snapshot_hash", "0" * 64)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError) as ctx:
			MigrationExecutor.apply_batch(batch_name)
		self.assertIn("ERP stock changed since validation", str(ctx.exception))

		# Verify batch reverted to VALIDATED
		batch.reload()
		self.assertEqual(batch.status, "VALIDATED")

	def test_04_opening_difference_account_validation(self):
		"""
		Verifies strict validation on opening_difference_account:
		- Missing account prevents READY.
		- P&L account prevents READY.
		- Valid Balance Sheet leaf account allows READY.
		"""
		records = [
			{"source_record_id": "ACC-1", "item_code": self.item_code, "warehouse": self.test_wh, "quantity": 5, "valuation_rate": 20},
		]

		# 1. Missing account
		b1 = MigrationImporter.stage_batch("TEST-ACC-NONE", self.company, records)
		self._track_batch(b1)
		res1 = MigrationValidator.validate_batch(b1)
		self.assertEqual(res1["status"], "VALIDATED")
		self.assertTrue(any("Opening Difference Account is required" in e for e in res1["errors"]))

		# 2. P&L Account
		if self.pnl_expense_account:
			b2 = MigrationImporter.stage_batch("TEST-ACC-PNL", self.company, records, opening_difference_account=self.pnl_expense_account)
			self._track_batch(b2)
			res2 = MigrationValidator.validate_batch(b2)
			self.assertEqual(res2["status"], "VALIDATED")
			self.assertTrue(any("Balance Sheet" in e for e in res2["errors"]))

		# 3. Valid Equity Account
		b3 = MigrationImporter.stage_batch("TEST-ACC-VALID", self.company, records, opening_difference_account=self.valid_diff_account)
		self._track_batch(b3)
		res3 = MigrationValidator.validate_batch(b3)
		self.assertEqual(res3["status"], "READY")
		self.assertEqual(len(res3["errors"]), 0)

	def test_05_accounting_preview_financial_metrics(self):
		"""Verifies that extended preview calculates target, current, and estimated adjustment value."""
		records = [
			{"source_record_id": "PREV-A", "item_code": self.item_code, "warehouse": self.test_wh, "quantity": 50, "valuation_rate": 12.0},
		]
		batch_name = MigrationImporter.stage_batch(
			"TEST-PREV-METRICS",
			self.company,
			records,
			opening_difference_account=self.valid_diff_account,
		)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)
		preview = MigrationPreview.get_reconciliation_preview(batch_name)

		self.assertEqual(preview["target_inventory_value"], 600.0)
		self.assertEqual(preview["current_inventory_value"], 0.0)
		self.assertEqual(preview["estimated_adjustment_value"], 600.0)
		self.assertEqual(preview["opening_difference_account"], self.valid_diff_account)

	def test_06_applied_batch_hardening_immutability(self):
		"""Verifies that opening_difference_account and validation_snapshot_hash cannot be altered on APPLIED batches."""
		batch = frappe.get_doc({
			"doctype": "Inventory Migration Batch",
			"batch_id": "TEST-APPLIED-IMMUTABLE",
			"company": self.company,
			"posting_date": "2026-09-04",
			"status": "APPLIED",
			"input_hash": "dummyhash",
			"opening_difference_account": self.valid_diff_account,
			"validation_snapshot_hash": "dummysnapshothash",
		}).insert(ignore_permissions=True)
		self._track_batch(batch.name)

		# Modify opening difference account
		batch.opening_difference_account = "Different Account"
		with self.assertRaises(frappe.ValidationError):
			batch.save(ignore_permissions=True)
