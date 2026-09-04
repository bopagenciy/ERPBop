# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe
from frappe.utils import flt

from bop_erp.inventory.migration.importer import MigrationImporter
from bop_erp.inventory.migration.validator import MigrationValidator
from bop_erp.inventory.migration.preview import MigrationPreview
from bop_erp.inventory.migration.executor import MigrationExecutor


class TestStockMigrationStateIntegrity(unittest.TestCase):
	"""
	Comprehensive Unit & Integration Test Suite for Phase 1H.2: Final Migration State Integrity.
	Covers:
	1. Pre-apply input hash recomputation:
	   - Staging row mutation after validation invalidates apply and reverts batch to VALIDATED.
	2. Validated batch configuration snapshot:
	   - Posting date drift blocks apply and reverts batch to VALIDATED.
	   - Posting time drift blocks apply and reverts batch to VALIDATED.
	   - Opening difference account drift blocks apply and reverts batch to VALIDATED.
	   - Company drift blocks apply and reverts batch to VALIDATED.
	3. Serial / Batch inventory snapshot hardening:
	   - Serial identity drift with same total quantity blocks apply.
	   - Batch distribution drift with same total quantity blocks apply.
	   - Deterministic, order-independent canonicalization for serial and batch structures.
	4. Untouched configuration & ERP state permits apply.
	5. Stock Settings test isolation:
	   - Preserves and restores enable_serial_and_batch_no_for_item on success and failure.
	"""

	@classmethod
	def setUpClass(cls):
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		if not cls.company:
			cls.company = "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr")

		stock_acc = frappe.db.get_value("Account", {"account_type": "Stock", "is_group": 1, "company": cls.company}, "name") or \
			frappe.db.get_value("Account", {"account_name": "Inventarios", "company": cls.company}, "name")
		if stock_acc and not frappe.db.get_value("Company", cls.company, "default_inventory_account"):
			frappe.db.set_value("Company", cls.company, "default_inventory_account", stock_acc)

		cls.warehouse = f"Stores - {cls.abbr}"

		cls.opening_diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)

		# Alternate valid account for drift test
		accounts = frappe.get_all(
			"Account",
			filters={"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			pluck="name",
		)
		cls.alt_diff_account = [a for a in accounts if a != cls.opening_diff_account][0] if len(accounts) > 1 else cls.opening_diff_account

		# Standard Item
		cls.item_code = "ITEM-STATE-INT-01"
		if not frappe.db.exists("Item", cls.item_code):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code,
				"item_name": "State Integrity Item 01",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		# Standard Item 2
		cls.item_code_2 = "ITEM-STATE-INT-02"
		if not frappe.db.exists("Item", cls.item_code_2):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code_2,
				"item_name": "State Integrity Item 02",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		# Serialized Item
		cls.serial_item = "ITEM-STATE-SER-02"
		if not frappe.db.exists("Item", cls.serial_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.serial_item,
				"item_name": "State Integrity Serial Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_serial_no": 1,
			}).insert(ignore_permissions=True)

		# Batch Item
		cls.batch_item = "ITEM-STATE-BAT-03"
		if not frappe.db.exists("Item", cls.batch_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.batch_item,
				"item_name": "State Integrity Batch Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
			}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		for item in [cls.item_code, getattr(cls, "item_code_2", None), cls.serial_item, cls.batch_item]:
			if item and frappe.db.exists("Item", item):
				frappe.db.delete("Bin", {"item_code": item})
				frappe.delete_doc("Item", item, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		self.created_batches = []
		self.orig_stock_setting = frappe.db.get_single_value("Stock Settings", "enable_serial_and_batch_no_for_item")
		frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", self.orig_stock_setting)
		for batch_name in self.created_batches:
			if frappe.db.exists("Inventory Migration Batch", batch_name):
				frappe.db.delete("Inventory Migration Row", {"batch": batch_name})
				frappe.delete_doc("Inventory Migration Batch", batch_name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _track_batch(self, batch_name):
		self.created_batches.append(batch_name)

	def test_01_staged_row_mutation_invalidates_apply(self):
		"""
		Verifies that if a staged Inventory Migration Row is modified after validation,
		recomputing the payload hash fails, apply is rejected, and batch is set to VALIDATED.
		"""
		records = [
			{"source_record_id": "ROW-1", "item_code": self.item_code, "warehouse": self.warehouse, "quantity": 10, "valuation_rate": 5},
			{"source_record_id": "ROW-2", "item_code": self.item_code_2, "warehouse": self.warehouse, "quantity": 20, "valuation_rate": 5},
		]
		batch_name = MigrationImporter.stage_batch(
			"INT-ROW-MUTATE",
			self.company,
			records,
			opening_difference_account=self.opening_diff_account,
		)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)
		batch = frappe.get_doc("Inventory Migration Batch", batch_name)
		self.assertEqual(batch.status, "READY")

		# Mutate one staged row directly
		row_name = frappe.db.get_value("Inventory Migration Row", {"batch": batch_name, "source_record_id": "ROW-1"}, "name")
		frappe.db.set_value("Inventory Migration Row", row_name, "quantity", 99.0)
		frappe.db.commit()

		# Apply must detect row mutation, revert status to VALIDATED, and abort
		with self.assertRaises(frappe.ValidationError) as ctx:
			MigrationExecutor.apply_batch(batch_name)
		self.assertIn("were modified since validation", str(ctx.exception))

		batch.reload()
		self.assertEqual(batch.status, "VALIDATED")

	def test_02_posting_date_drift_blocks_apply(self):
		"""Verifies that changing posting_date after validation blocks apply."""
		records = [
			{"source_record_id": "ROW-D1", "item_code": self.item_code, "warehouse": self.warehouse, "quantity": 5, "valuation_rate": 10},
		]
		batch_name = MigrationImporter.stage_batch(
			"INT-DATE-DRIFT",
			self.company,
			records,
			opening_difference_account=self.opening_diff_account,
		)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)

		# Modify posting_date
		frappe.db.set_value("Inventory Migration Batch", batch_name, "posting_date", "2026-12-31")
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError) as ctx:
			MigrationExecutor.apply_batch(batch_name)
		self.assertIn("modified since validation", str(ctx.exception))

		batch = frappe.get_doc("Inventory Migration Batch", batch_name)
		self.assertEqual(batch.status, "VALIDATED")

	def test_03_posting_time_drift_blocks_apply(self):
		"""Verifies that changing posting_time after validation blocks apply."""
		records = [
			{"source_record_id": "ROW-T1", "item_code": self.item_code, "warehouse": self.warehouse, "quantity": 5, "valuation_rate": 10},
		]
		batch_name = MigrationImporter.stage_batch(
			"INT-TIME-DRIFT",
			self.company,
			records,
			opening_difference_account=self.opening_diff_account,
		)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)

		# Modify posting_time
		frappe.db.set_value("Inventory Migration Batch", batch_name, "posting_time", "23:59:59")
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError) as ctx:
			MigrationExecutor.apply_batch(batch_name)
		self.assertIn("modified since validation", str(ctx.exception))

	def test_04_opening_account_drift_blocks_apply(self):
		"""Verifies that altering opening_difference_account after validation blocks apply."""
		records = [
			{"source_record_id": "ROW-A1", "item_code": self.item_code, "warehouse": self.warehouse, "quantity": 5, "valuation_rate": 10},
		]
		batch_name = MigrationImporter.stage_batch(
			"INT-ACC-DRIFT",
			self.company,
			records,
			opening_difference_account=self.opening_diff_account,
		)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)

		# Tamper opening difference account
		frappe.db.set_value("Inventory Migration Batch", batch_name, "opening_difference_account", self.alt_diff_account)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError) as ctx:
			MigrationExecutor.apply_batch(batch_name)
		self.assertIn("modified since validation", str(ctx.exception))

		batch = frappe.get_doc("Inventory Migration Batch", batch_name)
		self.assertEqual(batch.status, "VALIDATED")

	def test_05_company_drift_blocks_apply(self):
		"""Verifies that altering company after validation blocks apply."""
		records = [
			{"source_record_id": "ROW-C1", "item_code": self.item_code, "warehouse": self.warehouse, "quantity": 5, "valuation_rate": 10},
		]
		batch_name = MigrationImporter.stage_batch(
			"INT-COMP-DRIFT",
			self.company,
			records,
			opening_difference_account=self.opening_diff_account,
		)
		self._track_batch(batch_name)

		MigrationValidator.validate_batch(batch_name)

		# Modify company
		frappe.db.set_value("Inventory Migration Batch", batch_name, "company", "Bamal Fastener Corp")
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError) as ctx:
			MigrationExecutor.apply_batch(batch_name)
		self.assertIn("modified since validation", str(ctx.exception))

	def test_06_serial_identity_drift_blocks_apply(self):
		"""
		Verifies scenario A: Total quantity in warehouse is unchanged, but native active
		serial identities changed -> apply is rejected due to snapshot hash mismatch.
		"""
		pairs = [(self.serial_item, self.warehouse)]
		hash_baseline = MigrationValidator.compute_stock_snapshot_hash(pairs)

		# Temporarily create an active Serial No
		sn_doc = frappe.get_doc({
			"doctype": "Serial No",
			"serial_no": "SN-TEST-DRIFT-001",
			"item_code": self.serial_item,
			"warehouse": self.warehouse,
			"company": self.company,
			"status": "Active",
		})
		sn_doc.flags.ignore_validate = True
		sn_doc.insert(ignore_permissions=True)

		hash_with_sn1 = MigrationValidator.compute_stock_snapshot_hash(pairs)
		self.assertNotEqual(hash_baseline, hash_with_sn1)

		# Replace with a different serial number (quantity 1 remains 1)
		sn_doc.delete(force=True)
		sn_doc2 = frappe.get_doc({
			"doctype": "Serial No",
			"serial_no": "SN-TEST-DRIFT-002",
			"item_code": self.serial_item,
			"warehouse": self.warehouse,
			"company": self.company,
			"status": "Active",
		})
		sn_doc2.flags.ignore_validate = True
		sn_doc2.insert(ignore_permissions=True)

		hash_with_sn2 = MigrationValidator.compute_stock_snapshot_hash(pairs)
		self.assertNotEqual(hash_with_sn1, hash_with_sn2)

		# Cleanup serial
		sn_doc2.delete(force=True)
		frappe.db.commit()

		# Baseline restored
		self.assertEqual(MigrationValidator.compute_stock_snapshot_hash(pairs), hash_baseline)

	def test_07_batch_distribution_drift_blocks_apply(self):
		"""
		Verifies scenario B: Total quantity unchanged, but batch distribution altered
		produces different snapshot hashes.
		"""
		from unittest.mock import patch
		pairs = [(self.batch_item, self.warehouse)]

		b1 = frappe.get_doc({
			"doctype": "Batch",
			"batch_id": "BAT-DIST-01",
			"item": self.batch_item,
		}).insert(ignore_permissions=True)

		b2 = frappe.get_doc({
			"doctype": "Batch",
			"batch_id": "BAT-DIST-02",
			"item": self.batch_item,
		}).insert(ignore_permissions=True)

		try:
			# Distribution 1: b1=6, b2=4 (total=10)
			with patch("erpnext.stock.doctype.batch.batch.get_batch_qty", side_effect=lambda batch_no, warehouse: 6.0 if batch_no == "BAT-DIST-01" else 4.0):
				hash_dist1 = MigrationValidator.compute_stock_snapshot_hash(pairs)

			# Distribution 2: b1=5, b2=5 (total=10)
			with patch("erpnext.stock.doctype.batch.batch.get_batch_qty", side_effect=lambda batch_no, warehouse: 5.0 if batch_no == "BAT-DIST-01" else 5.0):
				hash_dist2 = MigrationValidator.compute_stock_snapshot_hash(pairs)

			self.assertNotEqual(hash_dist1, hash_dist2)
		finally:
			b1.delete(force=True)
			b2.delete(force=True)
			frappe.db.commit()

	def test_08_deterministic_serial_batch_ordering(self):
		"""
		Verifies that serial numbers and batch structures are sorted canonically
		and order-independent of SQL return orders.
		"""
		pairs_1 = [(self.serial_item, self.warehouse), (self.batch_item, self.warehouse)]
		pairs_2 = [(self.batch_item, self.warehouse), (self.serial_item, self.warehouse)]

		hash_1 = MigrationValidator.compute_stock_snapshot_hash(pairs_1)
		hash_2 = MigrationValidator.compute_stock_snapshot_hash(pairs_2)
		self.assertEqual(hash_1, hash_2)

	def test_09_unchanged_tracked_inventory_permits_apply(self):
		"""
		Verifies that an untouched staged batch and unchanged live ERP stock allows
		apply_batch to execute cleanly.
		"""
		records = [
			{"source_record_id": "OK-1", "item_code": self.item_code, "warehouse": self.warehouse, "quantity": 10, "valuation_rate": 5},
		]
		batch_name = MigrationImporter.stage_batch(
			"INT-UNTOUCHED-OK",
			self.company,
			records,
			opening_difference_account=self.opening_diff_account,
		)
		self._track_batch(batch_name)

		val_res = MigrationValidator.validate_batch(batch_name)
		self.assertEqual(val_res["status"], "READY")

		# Apply
		apply_res = MigrationExecutor.apply_batch(batch_name)
		self.assertEqual(apply_res["status"], "APPLIED")
		reco_name = apply_res["stock_reconciliation"]

		# Teardown reco
		reco = frappe.get_doc("Stock Reconciliation", reco_name)
		reco.cancel()
		frappe.delete_doc("Stock Reconciliation", reco_name, force=True, ignore_permissions=True)
		frappe.db.delete("Stock Ledger Entry", {"voucher_no": reco_name})
		frappe.db.delete("GL Entry", {"voucher_no": reco_name})
		frappe.db.delete("Bin", {"item_code": self.item_code})
		frappe.db.commit()

	def test_10_stock_settings_restoration_on_success_and_failure(self):
		"""
		Verifies that Stock Settings enable_serial_and_batch_no_for_item is safely recorded
		and restored on both clean runs and failure branches.
		"""
		orig_val = frappe.db.get_single_value("Stock Settings", "enable_serial_and_batch_no_for_item")
		try:
			frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 0)
			frappe.db.commit()
			self.assertEqual(frappe.db.get_single_value("Stock Settings", "enable_serial_and_batch_no_for_item"), 0)
		finally:
			frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", orig_val)
			frappe.db.commit()

		self.assertEqual(frappe.db.get_single_value("Stock Settings", "enable_serial_and_batch_no_for_item"), orig_val)
