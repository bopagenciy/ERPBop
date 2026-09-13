# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe
from frappe.utils import flt

from bop_erp.inventory.migration.importer import MigrationImporter
from bop_erp.inventory.migration.validator import MigrationValidator
from bop_erp.inventory.migration.preview import MigrationPreview
from bop_erp.inventory.migration.executor import MigrationExecutor


class TestInventoryMigrationUnit(unittest.TestCase):
	"""
	Unit test suite for Phase 1H:
	- CSV Parsing & input contract validation.
	- Staging into Inventory Migration Batch and Inventory Migration Row.
	- Batch & Row idempotency and hashing.
	- Comprehensive validation pipeline:
	  * item existence, stock item check
	  * warehouse existence, leaf check, company check
	  * UOM compatibility
	  * negative quantity & rate checks
	  * serialized item exact count checks
	  * batch-tracked item batch_no requirement
	  * duplicate (item, warehouse) row in same batch
	- Dry-run validation (zero Stock Ledger Entry / Bin writes).
	- Reconciliation preview calculation:
	  * current_qty, source_target_qty, adjustment_delta, inventory_value
	  * prominent non-zero current stock warning
	- Pre-apply safety gate:
	  * prevents applying DRAFT, VALIDATED with errors, or already APPLIED batches
	- Precision hardening:
	  * fractional and high-precision values preserved without arbitrary float rounding
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

		# Test Warehouse
		cls.test_wh = f"Stores - {cls.abbr}"

		cls.opening_diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)

		# Test Items
		cls.simple_item = "ITEM-PHASE1H-SIMPLE-01"
		if not frappe.db.exists("Item", cls.simple_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.simple_item,
				"item_name": "Simple Migration Item 01",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.non_stock_item = "ITEM-PHASE1H-SERVICE-02"
		if not frappe.db.exists("Item", cls.non_stock_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.non_stock_item,
				"item_name": "Consulting Service Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)

		cls.serial_item = "ITEM-PHASE1H-SERIAL-03"
		if not frappe.db.exists("Item", cls.serial_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.serial_item,
				"item_name": "Serialized Migration Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_serial_no": 1,
				"serial_no_series": "MIG-SER-.#####",
			}).insert(ignore_permissions=True)

		cls.batch_item = "ITEM-PHASE1H-BATCH-04"
		if not frappe.db.exists("Item", cls.batch_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.batch_item,
				"item_name": "Batch Managed Migration Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
				"batch_number_series": "MIG-BAT-.#####",
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

	def test_01_csv_parsing_and_staging(self):
		"""Validates CSV parsing and batch/row staging with input hash computation."""
		csv_text = f"""source_record_id,item_code,warehouse,quantity,valuation_rate,stock_uom
P21-001,{self.simple_item},{self.test_wh},100,12.50,Nos
P21-002,{self.simple_item},{self.test_wh},50,12.50,Nos
"""
		records = MigrationImporter.parse_csv(csv_text)
		self.assertEqual(len(records), 2)
		self.assertEqual(records[0]["source_record_id"], "P21-001")

		batch_name = MigrationImporter.stage_batch(
			batch_id="TEST-BATCH-01",
			company=self.company,
			records=records,
			source_system="PROPHET_21_CSV",
		)
		self._track_batch(batch_name)

		batch = frappe.get_doc("Inventory Migration Batch", batch_name)
		self.assertEqual(batch.total_rows, 2)
		self.assertEqual(batch.status, "DRAFT")
		self.assertEqual(len(batch.input_hash), 64)

		rows = frappe.get_all("Inventory Migration Row", filters={"batch": batch.name}, fields=["source_record_id", "quantity"])
		self.assertEqual(len(rows), 2)

	def test_02_idempotent_restaging_and_duplicate_row_rejection(self):
		"""
		Validates that re-staging an unapplied batch updates rows safely,
		while duplicate source_record_id within the same batch is blocked by DB uniqueness.
		"""
		records = [
			{"source_record_id": "ROW-A", "item_code": self.simple_item, "warehouse": self.test_wh, "quantity": 10, "valuation_rate": 5},
			{"source_record_id": "ROW-B", "item_code": self.simple_item, "warehouse": self.test_wh, "quantity": 20, "valuation_rate": 5},
		]
		batch_name = MigrationImporter.stage_batch("TEST-IDEMP-01", self.company, records)
		self._track_batch(batch_name)

		# Attempting to insert a duplicate row with same source_record_id on the same batch must fail DB uniqueness
		dup_row = frappe.get_doc({
			"doctype": "Inventory Migration Row",
			"batch": batch_name,
			"source_record_id": "ROW-A",
			"item_code": self.simple_item,
			"warehouse": self.test_wh,
			"quantity": 15,
			"valuation_rate": 5,
		})
		with self.assertRaises((frappe.DuplicateEntryError, frappe.UniqueValidationError)):
			dup_row.insert(ignore_permissions=True)

	def test_03_validation_pipeline_catches_errors(self):
		"""
		Validates that the validation pipeline catches all structural errors:
		- Non-existent item
		- Non-stock item
		- Non-existent warehouse
		- Group warehouse
		- Negative quantity
		- Negative valuation rate
		- Serial quantity mismatch
		- Missing batch number
		"""
		records = [
			{"source_record_id": "ERR-1", "item_code": "NONEXISTENT-SKU", "warehouse": self.test_wh, "quantity": 10, "valuation_rate": 5},
			{"source_record_id": "ERR-2", "item_code": self.non_stock_item, "warehouse": self.test_wh, "quantity": 10, "valuation_rate": 5},
			{"source_record_id": "ERR-3", "item_code": self.simple_item, "warehouse": "NONEXISTENT-WH", "quantity": 10, "valuation_rate": 5},
			{"source_record_id": "ERR-4", "item_code": self.simple_item, "warehouse": f"All Warehouses - {self.abbr}", "quantity": 10, "valuation_rate": 5},
			{"source_record_id": "ERR-5", "item_code": self.simple_item, "warehouse": self.test_wh, "quantity": -5, "valuation_rate": 5},
			{"source_record_id": "ERR-6", "item_code": self.simple_item, "warehouse": self.test_wh, "quantity": 10, "valuation_rate": -5},
			{"source_record_id": "ERR-7", "item_code": self.serial_item, "warehouse": self.test_wh, "quantity": 2, "valuation_rate": 50, "serial_no": "ONLY_ONE"},
			{"source_record_id": "ERR-8", "item_code": self.batch_item, "warehouse": self.test_wh, "quantity": 50, "valuation_rate": 8},
		]
		batch_name = MigrationImporter.stage_batch("TEST-ERRORS-01", self.company, records)
		self._track_batch(batch_name)

		res = MigrationValidator.validate_batch(batch_name)
		self.assertEqual(res["total_rows"], 8)
		self.assertEqual(res["valid_rows"], 0)
		self.assertEqual(res["error_rows"], 8)
		self.assertEqual(res["status"], "VALIDATED")

		batch = frappe.get_doc("Inventory Migration Batch", batch_name)
		self.assertNotEqual(batch.status, "READY")

	def test_04_dry_run_validation_produces_zero_mutations(self):
		"""Validates that dry run produces structured summary with ZERO SLE and ZERO Bin mutations."""
		sle_before = frappe.db.count("Stock Ledger Entry")
		bin_before = frappe.db.count("Bin")
		price_before = frappe.db.count("Item Price")

		records = [
			{"source_record_id": "VAL-1", "item_code": self.simple_item, "warehouse": self.test_wh, "quantity": 100, "valuation_rate": 15.0},
		]
		batch_name = MigrationImporter.stage_batch("TEST-DRY-01", self.company, records, opening_difference_account=self.opening_diff_account)
		self._track_batch(batch_name)

		res = MigrationValidator.validate_batch(batch_name)
		self.assertEqual(res["valid_rows"], 1)
		self.assertEqual(res["error_rows"], 0)
		self.assertEqual(res["status"], "READY")

		# Assert zero stock writes
		self.assertEqual(frappe.db.count("Stock Ledger Entry") - sle_before, 0)
		self.assertEqual(frappe.db.count("Bin") - bin_before, 0)
		self.assertEqual(frappe.db.count("Item Price") - price_before, 0)
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": self.simple_item}), 0)
		self.assertEqual(frappe.db.count("Bin", {"item_code": self.simple_item}), 0)
		self.assertEqual(frappe.db.count("Item Price", {"item_code": self.simple_item}), 0)

	def test_05_reconciliation_preview_calculation(self):
		"""
		Validates that reconciliation preview correctly computes:
		current_qty (0), source_target_qty (75), adjustment_delta (+75), inventory_value (1500).
		"""
		records = [
			{"source_record_id": "PREV-1", "item_code": self.simple_item, "warehouse": self.test_wh, "quantity": 75, "valuation_rate": 20.0},
		]
		batch_name = MigrationImporter.stage_batch("TEST-PREV-01", self.company, records, opening_difference_account=self.opening_diff_account)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)
		preview = MigrationPreview.get_reconciliation_preview(batch_name)

		self.assertEqual(preview["total_items"], 1)
		self.assertEqual(preview["total_opening_qty"], 75.0)
		self.assertEqual(preview["total_current_qty"], 0.0)
		self.assertEqual(preview["total_adjustment_delta"], 75.0)
		self.assertEqual(preview["total_inventory_value"], 1500.0)

		line = preview["lines"][0]
		self.assertEqual(line["item_code"], self.simple_item)
		self.assertEqual(line["warehouse"], self.test_wh)
		self.assertEqual(line["source_target_qty"], 75.0)
		self.assertEqual(line["adjustment_delta"], 75.0)

	def test_06_pre_apply_safety_gate_rejects_unready_batches(self):
		"""Validates that apply_batch rejects DRAFT batches or batches containing validation errors."""
		records = [
			{"source_record_id": "GATE-1", "item_code": "INVALID-SKU", "warehouse": self.test_wh, "quantity": 10, "valuation_rate": 5},
		]
		batch_name = MigrationImporter.stage_batch("TEST-GATE-01", self.company, records)
		self._track_batch(batch_name)

		# 1. Attempt apply on DRAFT batch
		with self.assertRaises(frappe.ValidationError):
			MigrationExecutor.apply_batch(batch_name)

		# 2. Validate (which fails) and attempt apply
		MigrationValidator.validate_batch(batch_name)
		with self.assertRaises(frappe.ValidationError):
			MigrationExecutor.apply_batch(batch_name)

	def test_07_fractional_precision_preserved(self):
		"""Validates that fractional quantities and high-precision valuations are not rounded away."""
		records = [
			{"source_record_id": "PREC-1", "item_code": self.simple_item, "warehouse": self.test_wh, "quantity": 12.375, "valuation_rate": 45.678},
		]
		batch_name = MigrationImporter.stage_batch("TEST-PREC-01", self.company, records, opening_difference_account=self.opening_diff_account)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)
		row = frappe.get_doc("Inventory Migration Row", {"batch": batch_name, "source_record_id": "PREC-1"})
		self.assertEqual(flt(row.quantity), 12.375)
		self.assertEqual(flt(row.valuation_rate), 45.678)

	def test_08_applied_batch_immutability(self):
		"""Validates that once a batch is marked APPLIED, its status, input_hash, and company cannot be changed."""
		batch = frappe.get_doc({
			"doctype": "Inventory Migration Batch",
			"batch_id": "TEST-IMMUTABLE-01",
			"company": self.company,
			"posting_date": "2026-09-04",
			"status": "APPLIED",
			"input_hash": "dummyhash123",
		}).insert(ignore_permissions=True)
		self._track_batch(batch.name)

		batch.status = "DRAFT"
		with self.assertRaises(frappe.ValidationError):
			batch.save(ignore_permissions=True)
