# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import ExternalEntityType
from bop_erp.migration.adapters.synthetic import SyntheticSourceAdapter
from bop_erp.migration.dry_run import execute_dry_run
from bop_erp.migration.exceptions import (
	ImportBoundaryError,
	SourcePayloadDriftError,
	SourceWriteBlockedError,
)
from bop_erp.migration.import_boundary import import_validated_entity
from bop_erp.migration.normalization import normalize_migration_run
from bop_erp.migration.reconciliation import reconcile_migration_run
from bop_erp.migration.staging import (
	compute_staging_identity,
	extract_and_stage_from_adapter,
	stage_source_record,
)
from bop_erp.migration.validation import validate_migration_run


class TestSourceERPMigrationLive(FrappeTestCase):
	"""
	Phase 1U Live Integration Test Suite:
	Source ERP Migration Foundation (Read-Only Adapter, Staging, Normalization, Dry Run).
	Executes strictly against SyntheticSourceAdapter and local container environment.
	Zero contact with external or production systems.
	"""

	FIXTURE_PREFIX = "TEST-1U-"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = (
			frappe.db.get_single_value("Global Defaults", "default_company")
			or frappe.db.get_value("Company", {}, "name")
			or "Industrial DP"
		)
		cls.created_runs = []

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_all_fixtures()
		super().tearDownClass()

	@classmethod
	def _cleanup_all_fixtures(cls):
		# Clean staging rows and migration runs
		runs = frappe.get_all(
			"Migration Run",
			filters={"run_id": ["like", f"{cls.FIXTURE_PREFIX}%"]},
			pluck="name",
		)
		for r in runs:
			frappe.db.delete("Migration Staging Row", {"migration_run": r})
			frappe.delete_doc("Migration Run", r, force=True, ignore_permissions=True)

		# Clean any test mappings
		frappe.db.delete(
			"External ID Mapping",
			{"sales_channel": ["like", f"MIG-{cls.FIXTURE_PREFIX}%"]},
		)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self.adapter = SyntheticSourceAdapter(
			source_system=f"{self.FIXTURE_PREFIX}SYN",
			source_instance_id="MAIN",
		)

	def tearDown(self):
		# Clean test runs created in test
		for run_name in getattr(self, "_test_runs", []):
			frappe.db.delete("Migration Staging Row", {"migration_run": run_name})
			if frappe.db.exists("Migration Run", run_name):
				frappe.delete_doc("Migration Run", run_name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _create_test_run(self, run_id_suffix: str) -> Any:
		run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"{self.FIXTURE_PREFIX}{run_id_suffix}",
			"source_system": self.adapter.source_system,
			"source_instance_id": self.adapter.source_instance_id,
			"company": self.company,
			"status": "DRAFT",
			"dry_run": 1,
		}).insert(ignore_permissions=True)
		if not hasattr(self, "_test_runs"):
			self._test_runs = []
		self._test_runs.append(run.name)
		return run

	# Scenario A: Synthetic CUSTOMER extraction -> staging -> normalization -> validation -> dry run
	def test_a_synthetic_customer_lifecycle(self):
		run = self._create_test_run("CUST-A")
		counts = extract_and_stage_from_adapter(self.adapter, run.name, entity_types=["CUSTOMER"])
		self.assertEqual(counts["CUSTOMER"], 2)

		val_res = validate_migration_run(run.name)
		self.assertEqual(val_res["status"], "READY")
		self.assertEqual(val_res["valid_rows"], 2)
		self.assertEqual(val_res["error_rows"], 0)

		dry_res = execute_dry_run(run.name)
		self.assertEqual(dry_res["by_entity_type"]["CUSTOMER"]["would_error"], 0)
		self.assertEqual(dry_res["summary"]["would_create"], 2)
		self.assertEqual(dry_res["target_table_deltas"]["Customer"], 0)

	# Scenario B: Synthetic VENDOR
	def test_b_synthetic_vendor_lifecycle(self):
		run = self._create_test_run("VEND-B")
		counts = extract_and_stage_from_adapter(self.adapter, run.name, entity_types=["VENDOR"])
		self.assertEqual(counts["VENDOR"], 2)

		val_res = validate_migration_run(run.name)
		self.assertEqual(val_res["status"], "READY")
		self.assertEqual(val_res["valid_rows"], 2)
		self.assertEqual(val_res["error_rows"], 0)

		dry_res = execute_dry_run(run.name)
		self.assertEqual(dry_res["by_entity_type"]["VENDOR"]["would_error"], 0)
		self.assertEqual(dry_res["target_table_deltas"]["Supplier"], 0)

	# Scenario C: Synthetic ITEM
	def test_c_synthetic_item_lifecycle(self):
		run = self._create_test_run("ITEM-C")
		counts = extract_and_stage_from_adapter(self.adapter, run.name, entity_types=["ITEM"])
		self.assertEqual(counts["ITEM"], 2)

		val_res = validate_migration_run(run.name)
		self.assertEqual(val_res["status"], "READY")
		self.assertEqual(val_res["valid_rows"], 2)

		dry_res = execute_dry_run(run.name)
		self.assertEqual(dry_res["by_entity_type"]["ITEM"]["would_error"], 0)
		self.assertEqual(dry_res["target_table_deltas"]["Item"], 0)

	# Scenario D: Synthetic WAREHOUSE
	def test_d_synthetic_warehouse_lifecycle(self):
		run = self._create_test_run("WH-D")
		counts = extract_and_stage_from_adapter(self.adapter, run.name, entity_types=["WAREHOUSE"])
		self.assertEqual(counts["WAREHOUSE"], 2)

		val_res = validate_migration_run(run.name)
		self.assertEqual(val_res["status"], "READY")
		self.assertEqual(val_res["valid_rows"], 2)

		dry_res = execute_dry_run(run.name)
		self.assertEqual(dry_res["by_entity_type"]["WAREHOUSE"]["would_error"], 0)
		self.assertEqual(dry_res["target_table_deltas"]["Warehouse"], 0)

	# Scenario E: Duplicate run / replay convergence
	def test_e_duplicate_run_replay_convergence(self):
		run = self._create_test_run("REPLAY-E")

		# First extraction pass
		counts1 = extract_and_stage_from_adapter(self.adapter, run.name, entity_types=["CUSTOMER"])
		staged_rows_pass1 = frappe.db.count("Migration Staging Row", {"migration_run": run.name})
		self.assertEqual(staged_rows_pass1, 2)

		# Second identical staging pass into the same run
		for raw in self.adapter.stream_customers():
			row_doc, is_new = stage_source_record(
				run_id=run.name,
				source_system=self.adapter.source_system,
				source_instance_id=self.adapter.source_instance_id,
				entity_type="CUSTOMER",
				source_record=raw,
			)
			self.assertFalse(is_new, "Identical replay must converge to existing record")

		staged_rows_pass2 = frappe.db.count("Migration Staging Row", {"migration_run": run.name})
		self.assertEqual(staged_rows_pass1, staged_rows_pass2, "Replay must produce zero duplicate rows")

	# Scenario F: Source payload drift -> review / error classification
	def test_f_source_payload_drift_classification(self):
		run = self._create_test_run("DRIFT-F")

		record_initial = {"id": "DRIFT-CUST-01", "name": "Initial Name", "currency": "COP"}
		row1, is_new = stage_source_record(
			run_id=run.name,
			source_system=self.adapter.source_system,
			source_instance_id=self.adapter.source_instance_id,
			entity_type="CUSTOMER",
			source_record=record_initial,
		)
		self.assertTrue(is_new)

		# Replaying with modified payload raises SourcePayloadDriftError
		record_modified = {"id": "DRIFT-CUST-01", "name": "Altered Name", "currency": "USD"}
		with self.assertRaises(SourcePayloadDriftError):
			stage_source_record(
				run_id=run.name,
				source_system=self.adapter.source_system,
				source_instance_id=self.adapter.source_instance_id,
				entity_type="CUSTOMER",
				source_record=record_modified,
			)

	# Scenario G: Cross-source same external id remains isolated
	def test_g_cross_source_same_external_id_isolated(self):
		run_p21 = self._create_test_run("ISO-P21")
		run_sap = self._create_test_run("ISO-SAP")

		rec_p21 = {"id": "SHARED-ID-100", "name": "P21 Customer", "currency": "COP"}
		rec_sap = {"id": "SHARED-ID-100", "name": "SAP Customer", "currency": "COP"}

		row_p21, _ = stage_source_record(
			run_id=run_p21.name,
			source_system=f"{self.FIXTURE_PREFIX}P21",
			source_instance_id="EAST",
			entity_type="CUSTOMER",
			source_record=rec_p21,
		)
		row_sap, _ = stage_source_record(
			run_id=run_sap.name,
			source_system=f"{self.FIXTURE_PREFIX}SAP",
			source_instance_id="WEST",
			entity_type="CUSTOMER",
			source_record=rec_sap,
		)

		self.assertNotEqual(row_p21.staging_identity_key, row_sap.staging_identity_key)
		self.assertNotEqual(row_p21.name, row_sap.name)

	# Scenario H: Dry run proves zero target mutations
	def test_h_dry_run_zero_target_mutations(self):
		run = self._create_test_run("DRY-H")

		# Record pre-test table counts
		pre_cust = frappe.db.count("Customer")
		pre_supp = frappe.db.count("Supplier")
		pre_item = frappe.db.count("Item")
		pre_wh = frappe.db.count("Warehouse")
		pre_sle = frappe.db.count("Stock Ledger Entry")
		pre_gle = frappe.db.count("GL Entry")
		pre_map = frappe.db.count("External ID Mapping")

		extract_and_stage_from_adapter(self.adapter, run.name)
		validate_migration_run(run.name)
		res = execute_dry_run(run.name)

		self.assertEqual(res["dry_run"], True)
		self.assertEqual(frappe.db.count("Customer"), pre_cust)
		self.assertEqual(frappe.db.count("Supplier"), pre_supp)
		self.assertEqual(frappe.db.count("Item"), pre_item)
		self.assertEqual(frappe.db.count("Warehouse"), pre_wh)
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), pre_sle)
		self.assertEqual(frappe.db.count("GL Entry"), pre_gle)
		self.assertEqual(frappe.db.count("External ID Mapping"), pre_map)

	# Scenario I: Read-only write attempt blocked before execution
	def test_i_write_attempt_blocked_before_execution(self):
		# SQL write blocked
		with self.assertRaises(SourceWriteBlockedError):
			self.adapter.attempt_write_sql("UPDATE p21_customer SET active = 0")

		# HTTP write blocked
		with self.assertRaises(SourceWriteBlockedError):
			self.adapter.attempt_write_http("PUT", "https://mock-source.internal/customers/1")

	# Scenario J: Fixture cleanup / re-entry proof
	def test_j_fixture_cleanup_and_reentry_proof(self):
		# Run a complete cycle
		run = self._create_test_run("CLEANUP-J")
		extract_and_stage_from_adapter(self.adapter, run.name)
		validate_migration_run(run.name)

		# Explicitly clean test docs
		frappe.db.delete("Migration Staging Row", {"migration_run": run.name})
		frappe.delete_doc("Migration Run", run.name, force=True, ignore_permissions=True)
		frappe.db.commit()

		residual_rows = frappe.db.count("Migration Staging Row", {"migration_run": run.name})
		residual_run = frappe.db.count("Migration Run", {"name": run.name})
		self.assertEqual(residual_rows, 0, "Residual staging rows leaked!")
		self.assertEqual(residual_run, 0, "Residual migration run leaked!")
