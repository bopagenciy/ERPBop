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
from bop_erp.migration.import_boundary import (
	get_or_create_migration_channel,
	import_validated_entity,
)
from bop_erp.migration.namespaces import (
	canonical_company_tag,
	canonical_provider,
	canonical_source_instance_id,
	canonical_source_namespace,
	canonical_source_system,
	compute_migration_channel_id,
)
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
		self._test_runs = []
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

	# Scenario K: Source Instance isolation in External ID Mapping
	def test_k_source_instance_mapping_isolation(self):
		run_a = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"{self.FIXTURE_PREFIX}INST-A-{frappe.generate_hash(length=6)}",
			"source_system": "PROPHET_21",
			"source_instance_id": "client-A",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)
		self._test_runs.append(run_a.name)
		frappe.db.set_value("Migration Run", run_a.name, "status", "READY")
		run_a.reload()

		run_b = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"{self.FIXTURE_PREFIX}INST-B-{frappe.generate_hash(length=6)}",
			"source_system": "PROPHET_21",
			"source_instance_id": "client-B",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)
		self._test_runs.append(run_b.name)
		frappe.db.set_value("Migration Run", run_b.name, "status", "READY")
		run_b.reload()

		cust_payload = {"id": "123", "name": f"{self.FIXTURE_PREFIX}Cust-123"}
		row_a, _ = stage_source_record(
			run_id=run_a.name,
			source_system=run_a.source_system,
			source_instance_id=run_a.source_instance_id,
			entity_type="CUSTOMER",
			source_record=cust_payload,
		)
		row_a.normalized_payload_json = json.dumps({
			"customer_name": f"{self.FIXTURE_PREFIX}Client A Customer",
			"company": self.company,
		})
		row_a.validation_status = "VALID"
		row_a.save(ignore_permissions=True)

		row_b, _ = stage_source_record(
			run_id=run_b.name,
			source_system=run_b.source_system,
			source_instance_id=run_b.source_instance_id,
			entity_type="CUSTOMER",
			source_record=cust_payload,
		)
		row_b.normalized_payload_json = json.dumps({
			"customer_name": f"{self.FIXTURE_PREFIX}Client B Customer",
			"company": self.company,
		})
		row_b.validation_status = "VALID"
		row_b.save(ignore_permissions=True)

		# Import both records
		res_a = import_validated_entity(row_a.name)
		res_b = import_validated_entity(row_b.name)

		self.assertEqual(res_a["status"], "IMPORTED")
		self.assertEqual(res_b["status"], "IMPORTED")

		# Check External ID Mapping: both exist without collision
		mappings = frappe.get_all(
			"External ID Mapping",
			filters={"external_id": "123", "external_entity_type": ExternalEntityType.CUSTOMER},
			fields=["name", "sales_channel", "provider", "erp_document"],
		)
		test_mappings = [m for m in mappings if "client-a" in (m.provider or "").lower() or "client-b" in (m.provider or "").lower()]
		self.assertEqual(len(test_mappings), 2)
		providers = {m.provider for m in test_mappings}
		self.assertEqual(providers, {"PROPHET_21:CLIENT-A", "PROPHET_21:CLIENT-B"})

		# Verify company-scoped sales channels
		co_tag = canonical_company_tag(self.company)
		expected_ch_a = f"MIG-{co_tag}-PROPHET_21-CLIENT-A"
		expected_ch_b = f"MIG-{co_tag}-PROPHET_21-CLIENT-B"
		channels = {m.sales_channel for m in test_mappings}
		self.assertEqual(channels, {expected_ch_a, expected_ch_b})

		# Clean created target customers and mappings
		for m in test_mappings:
			frappe.db.delete("External ID Mapping", {"name": m.name})
			if frappe.db.exists("Customer", m.erp_document):
				frappe.delete_doc("Customer", m.erp_document, force=True, ignore_permissions=True)

	# Scenario L: Import boundary blocks WARNING and ERROR
	def test_l_import_boundary_blocks_warning_and_error(self):
		run = self._create_test_run("WARN-ERR-L")
		frappe.db.set_value("Migration Run", run.name, "status", "READY")
		run.reload()

		# Row with WARNING
		row_w = frappe.get_doc({
			"doctype": "Migration Staging Row",
			"migration_run": run.name,
			"entity_type": "CUSTOMER",
			"source_record_id": "WARN-1",
			"staging_identity_key": "w" * 64,
			"source_payload_hash": "w" * 64,
			"source_payload_json": json.dumps({"id": "WARN-1"}),
			"normalized_payload_json": json.dumps({"customer_name": "Warn Cust", "company": self.company}),
			"validation_status": "WARNING",
			"validation_warnings_json": json.dumps(["Warning reason"]),
			"import_status": "PENDING",
		}).insert(ignore_permissions=True)

		with self.assertRaises(ImportBoundaryError):
			import_validated_entity(row_w.name)

		# Row with ERROR
		row_e = frappe.get_doc({
			"doctype": "Migration Staging Row",
			"migration_run": run.name,
			"entity_type": "CUSTOMER",
			"source_record_id": "ERR-1",
			"staging_identity_key": "e" * 64,
			"source_payload_hash": "e" * 64,
			"source_payload_json": json.dumps({"id": "ERR-1"}),
			"normalized_payload_json": json.dumps({"customer_name": "Err Cust", "company": self.company}),
			"validation_status": "ERROR",
			"validation_errors_json": json.dumps(["Error reason"]),
			"import_status": "PENDING",
		}).insert(ignore_permissions=True)

		with self.assertRaises(ImportBoundaryError):
			import_validated_entity(row_e.name)

	# Scenario M: Cross-run immutable snapshot semantics and drift classification
	def test_m_cross_run_snapshot_immutability_and_drift(self):
		run1 = self._create_test_run("RUN1-M")
		run2 = self._create_test_run("RUN2-M")
		run3 = self._create_test_run("RUN3-M")

		raw_v1 = {"id": "ITEM-SNAP-99", "name": "Item Original", "sku": "SKU-99"}
		raw_v2 = {"id": "ITEM-SNAP-99", "name": "Item Modified", "sku": "SKU-99"}

		# Run 1: stage v1
		row1, is_new1 = stage_source_record(
			run_id=run1.name,
			source_system=run1.source_system,
			source_instance_id=run1.source_instance_id,
			entity_type="ITEM",
			source_record=raw_v1,
		)
		self.assertTrue(is_new1)
		self.assertEqual(row1.snapshot_state, "NEW")

		# Run 2: stage same v1
		row2, is_new2 = stage_source_record(
			run_id=run2.name,
			source_system=run2.source_system,
			source_instance_id=run2.source_instance_id,
			entity_type="ITEM",
			source_record=raw_v1,
		)
		self.assertTrue(is_new2)
		self.assertEqual(row2.snapshot_state, "UNCHANGED")
		# Verify Run 1 row was NOT mutated
		row1_fresh = frappe.get_doc("Migration Staging Row", row1.name)
		self.assertEqual(row1_fresh.source_payload_hash, row1.source_payload_hash)
		self.assertEqual(row1_fresh.snapshot_state, "NEW")

		# Run 3: stage changed v2
		row3, is_new3 = stage_source_record(
			run_id=run3.name,
			source_system=run3.source_system,
			source_instance_id=run3.source_instance_id,
			entity_type="ITEM",
			source_record=raw_v2,
		)
		self.assertTrue(is_new3)
		self.assertEqual(row3.snapshot_state, "CHANGED")
		# Verify Run 1 and Run 2 rows were NOT mutated
		row2_fresh = frappe.get_doc("Migration Staging Row", row2.name)
		self.assertEqual(row2_fresh.source_payload_hash, row2.source_payload_hash)
		self.assertEqual(row2_fresh.snapshot_state, "UNCHANGED")

		# Reconcile Run 3
		rep = reconcile_migration_run(run3.name)
		self.assertEqual(rep["by_entity_type"]["ITEM"]["changed_count"], 1)
		self.assertEqual(rep["discrepancies"]["changed_entities"], 1)

	# Scenario N: Casing convergence & company migration channel isolation
	def test_n_casing_convergence_and_company_channel_isolation(self):
		# Prove casing & whitespace variations converge to same canonical namespace and channel
		ch_1 = get_or_create_migration_channel(self.company, "prophet_21", "client-a")
		ch_2 = get_or_create_migration_channel(self.company, " PROPHET_21 ", " CLIENT-A ")
		self.assertEqual(ch_1, ch_2, "Casing/whitespace variations must converge to identical migration channel.")

		# Prove company isolation: different company produces different migration channel
		co_tag = canonical_company_tag(self.company)
		expected_ch = f"MIG-{co_tag}-PROPHET_21-CLIENT-A"
		self.assertEqual(ch_1, expected_ch)

		other_ch = compute_migration_channel_id("OTHER_COMPANY_XYZ", "PROPHET_21", "CLIENT-A")
		self.assertNotEqual(ch_1, other_ch, "Different company must produce isolated migration channel.")
