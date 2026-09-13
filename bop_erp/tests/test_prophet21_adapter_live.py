# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.migration.adapters.prophet21 import (
	DEFAULT_SYNTHETIC_MAPPINGS,
	Prophet21SourceAdapter,
	extract_entity_to_staging,
)
from bop_erp.migration.adapters.prophet21_queries import (
	LogicalEntityMapping,
)
from bop_erp.migration.adapters.sql_executor import (
	ReadOnlySqlExecutor,
	SyntheticSqlExecutor,
)
from bop_erp.migration.adapters.synthetic_p21_data import get_synthetic_p21_dataset
from bop_erp.migration.dry_run import execute_dry_run
from bop_erp.migration.exceptions import (
	ProductionSourceBlockedError,
	SourceReadError,
	SourceSafetyViolationError,
	SourceWriteBlockedError,
	StagingError,
)
from bop_erp.migration.namespaces import (
	canonical_source_instance_id,
	canonical_source_namespace,
	canonical_source_system,
)
from bop_erp.migration.normalization import normalize_migration_run
from bop_erp.migration.staging import stage_source_record
from bop_erp.migration.validation import validate_migration_run


class TestProphet21AdapterLive(FrappeTestCase):
	"""
	Phase 1V Live Integration Suite:
	Prophet 21 Read-Only Adapter Foundation against synthetic P21 data.
	Operates strictly in-memory / local container. Zero production contact.
	Zero writes to P21 source tables. Zero ERPNext target mutations during dry-run.
	"""

	FIXTURE_PREFIX = "TEST-1V-"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = (
			frappe.db.get_single_value("Global Defaults", "default_company")
			or frappe.db.get_value("Company", {}, "name")
			or "Industrial DP"
		)
		# Ensure test Item Group exists if needed
		if not frappe.db.exists("Item Group", "Fasteners"):
			frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": "Fasteners",
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}).insert(ignore_permissions=True)
		cls._cleanup_all_fixtures()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_all_fixtures()
		super().tearDownClass()

	@classmethod
	def _cleanup_all_fixtures(cls):
		runs = frappe.get_all(
			"Migration Run",
			filters={"run_id": ["like", f"{cls.FIXTURE_PREFIX}%"]},
			pluck="name",
		)
		for r in runs:
			frappe.db.delete("Migration Staging Row", {"migration_run": r})
			frappe.delete_doc("Migration Run", r, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self._test_runs: List[str] = []
		# Populate synthetic dataset with company COMP_1 and alt COMP_2
		self.synthetic_data = get_synthetic_p21_dataset(company_id="COMP_1", alt_company_id="COMP_2")
		self.executor = SyntheticSqlExecutor(initial_data=self.synthetic_data)
		self.adapter = Prophet21SourceAdapter(
			source_instance_id="TEST_MAIN",
			source_environment="SYNTHETIC",
			source_company_id="COMP_1",
			executor=self.executor,
		)

	def tearDown(self):
		for run_name in getattr(self, "_test_runs", []):
			frappe.db.delete("Migration Staging Row", {"migration_run": run_name})
			if frappe.db.exists("Migration Run", run_name):
				frappe.delete_doc("Migration Run", run_name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _create_test_run(self, suffix: str) -> Any:
		run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"{self.FIXTURE_PREFIX}{suffix}",
			"source_system": self.adapter.source_system,
			"source_instance_id": self.adapter.source_instance_id,
			"company": self.company,
			"status": "DRAFT",
			"dry_run": 1,
		}).insert(ignore_permissions=True)
		self._test_runs.append(run.name)
		return run

	# Scenario A: Schema discovery against synthetic P21 source
	def test_a_schema_discovery_against_synthetic_p21(self):
		schema = self.adapter.discover_schema()
		self.assertIsNotNone(schema)
		self.assertTrue(schema.has_table("synthetic_customer"))
		self.assertTrue(schema.has_table("synthetic_vendor"))
		self.assertTrue(schema.has_table("synthetic_item"))
		self.assertTrue(schema.has_table("synthetic_warehouse"))

		cust_tbl = schema.get_table("synthetic_customer")
		self.assertIsNotNone(cust_tbl)
		self.assertIn("customer_id", cust_tbl.columns)
		self.assertIn("customer_name", cust_tbl.columns)
		self.assertIn("email_address", cust_tbl.columns)
		self.assertIn("tax_id_number", cust_tbl.columns)

		meta = self.adapter.get_source_metadata()
		self.assertEqual(meta["source_system"], "PROPHET_21")
		self.assertEqual(meta["source_environment"], "SYNTHETIC")
		self.assertTrue(meta["read_only"])
		self.assertEqual(meta["customer_count"], 3)
		self.assertEqual(meta["vendor_count"], 2)

	# Scenario B: CUSTOMER extraction: source -> staging -> normalization -> validation
	def test_b_customer_extraction_lifecycle(self):
		run = self._create_test_run("CUST-LIFECYCLE")
		count = extract_entity_to_staging(
			run_id=run.name,
			adapter=self.adapter,
			entity_type="CUSTOMER",
		)
		self.assertEqual(count, 3)

		run.reload()
		self.assertEqual(run.status, "STAGED")
		self.assertEqual(run.total_rows, 3)

		# Validate staged rows exist and have immutable keys
		staged_rows = frappe.get_all(
			"Migration Staging Row",
			filters={"migration_run": run.name},
			fields=["name", "source_record_id", "staging_identity_key", "source_payload_hash"],
		)
		self.assertEqual(len(staged_rows), 3)
		for r in staged_rows:
			self.assertEqual(len(r.staging_identity_key), 64)
			self.assertEqual(len(r.source_payload_hash), 64)

		# Normalization & Validation
		val_res = validate_migration_run(run.name)
		self.assertEqual(val_res["status"], "READY")
		self.assertEqual(val_res["valid_rows"], 3)
		self.assertEqual(val_res["error_rows"], 0)

	# Scenario C: VENDOR extraction
	def test_c_vendor_extraction_lifecycle(self):
		run = self._create_test_run("VEND-LIFECYCLE")
		count = extract_entity_to_staging(
			run_id=run.name,
			adapter=self.adapter,
			entity_type="VENDOR",
		)
		self.assertEqual(count, 2)

		val_res = validate_migration_run(run.name)
		self.assertEqual(val_res["status"], "READY")
		self.assertEqual(val_res["valid_rows"], 2)
		self.assertEqual(val_res["error_rows"], 0)

	# Scenario D: ITEM extraction
	def test_d_item_extraction_lifecycle(self):
		run = self._create_test_run("ITEM-LIFECYCLE")
		count = extract_entity_to_staging(
			run_id=run.name,
			adapter=self.adapter,
			entity_type="ITEM",
		)
		self.assertEqual(count, 3)

		val_res = validate_migration_run(run.name)
		self.assertIn(val_res["status"], ("READY", "REVIEW_REQUIRED"))
		# 3 items staged under COMP_1
		self.assertEqual(run.reload().total_rows, 3)

	# Scenario E: WAREHOUSE extraction
	def test_e_warehouse_extraction_lifecycle(self):
		run = self._create_test_run("WH-LIFECYCLE")
		count = extract_entity_to_staging(
			run_id=run.name,
			adapter=self.adapter,
			entity_type="WAREHOUSE",
		)
		self.assertEqual(count, 2)

		val_res = validate_migration_run(run.name)
		self.assertEqual(val_res["status"], "READY")
		self.assertEqual(val_res["valid_rows"], 2)
		self.assertEqual(val_res["error_rows"], 0)

	# Scenario F: Pagination across multiple pages
	def test_f_pagination_across_multiple_pages(self):
		# Stream customers with page_size=1
		stream = self.adapter.stream_customers(page_size=1)
		extracted = list(stream)
		self.assertEqual(len(extracted), 3)
		self.assertEqual(self.adapter.pages_extracted, 3)

		# Primary key ordering invariant
		ids = [r["source_record_id"] for r in extracted]
		self.assertEqual(ids, sorted(ids))

	# Scenario G: Resume after simulated extraction failure
	def test_g_resume_after_simulated_failure(self):
		run = self._create_test_run("FAIL-RESUME")

		# Fail after 1 record
		with self.assertRaises(SourceReadError):
			extract_entity_to_staging(
				run_id=run.name,
				adapter=self.adapter,
				entity_type="CUSTOMER",
				fail_after_records=1,
			)

		run.reload()
		self.assertEqual(run.status, "FAILED")
		# 1 row was staged and preserved
		staged_before_resume = frappe.db.count("Migration Staging Row", {"migration_run": run.name})
		self.assertEqual(staged_before_resume, 1)

		# Resume by resetting status to EXTRACTING and extracting full batch
		frappe.db.set_value("Migration Run", run.name, "status", "EXTRACTING")
		frappe.db.commit()

		count = extract_entity_to_staging(
			run_id=run.name,
			adapter=self.adapter,
			entity_type="CUSTOMER",
		)
		self.assertEqual(count, 3)

		run.reload()
		self.assertEqual(run.status, "STAGED")
		# Replay converges, exactly 3 distinct rows present
		staged_after_resume = frappe.db.count("Migration Staging Row", {"migration_run": run.name})
		self.assertEqual(staged_after_resume, 3)

	# Scenario H: Same source replay converges without duplicate staging in same run
	def test_h_same_source_replay_converges(self):
		run = self._create_test_run("CONVERGE")
		count1 = extract_entity_to_staging(
			run_id=run.name,
			adapter=self.adapter,
			entity_type="CUSTOMER",
		)
		self.assertEqual(count1, 3)
		cnt_pass1 = frappe.db.count("Migration Staging Row", {"migration_run": run.name})

		# Re-run extraction into same run
		frappe.db.set_value("Migration Run", run.name, "status", "EXTRACTING")
		frappe.db.commit()

		count2 = extract_entity_to_staging(
			run_id=run.name,
			adapter=self.adapter,
			entity_type="CUSTOMER",
		)
		self.assertEqual(count2, 3)
		cnt_pass2 = frappe.db.count("Migration Staging Row", {"migration_run": run.name})

		self.assertEqual(cnt_pass1, cnt_pass2, "Replay into same run must not duplicate staging rows")

	# Scenario I: Cross-run extraction creates new immutable snapshot rows
	def test_i_cross_run_extraction_creates_immutable_snapshot_rows(self):
		run1 = self._create_test_run("SNAP-1")
		run2 = self._create_test_run("SNAP-2")

		extract_entity_to_staging(run_id=run1.name, adapter=self.adapter, entity_type="CUSTOMER")
		extract_entity_to_staging(run_id=run2.name, adapter=self.adapter, entity_type="CUSTOMER")

		rows1 = frappe.get_all(
			"Migration Staging Row",
			filters={"migration_run": run1.name},
			fields=["name", "source_record_id", "staging_identity_key"],
		)
		rows2 = frappe.get_all(
			"Migration Staging Row",
			filters={"migration_run": run2.name},
			fields=["name", "source_record_id", "staging_identity_key"],
		)

		self.assertEqual(len(rows1), 3)
		self.assertEqual(len(rows2), 3)

		names1 = {r.name for r in rows1}
		names2 = {r.name for r in rows2}
		# Different row documents in different runs
		self.assertEqual(len(names1.intersection(names2)), 0)

		# But identical staging identity key for matching source record
		keys1 = {r.source_record_id: r.staging_identity_key for r in rows1}
		keys2 = {r.source_record_id: r.staging_identity_key for r in rows2}
		for rec_id in keys1:
			self.assertEqual(keys1[rec_id], keys2[rec_id])

	# Scenario J: Source company scope excludes another synthetic company
	def test_j_source_company_scoping_excludes_other_companies(self):
		# Adapter configured with COMP_1 must NOT extract COMP_2 records
		customers = list(self.adapter.stream_customers())
		vendors = list(self.adapter.stream_vendors())
		items = list(self.adapter.stream_items())
		warehouses = list(self.adapter.stream_warehouses())

		self.assertEqual(len(customers), 3)
		self.assertEqual(len(vendors), 2)
		self.assertEqual(len(items), 3)
		self.assertEqual(len(warehouses), 2)

		# Excluded records from COMP_2
		cust_ids = [c["source_record_id"] for c in customers]
		self.assertNotIn("P21-CUST-ALT-999", cust_ids)

		vend_ids = [v["source_record_id"] for v in vendors]
		self.assertNotIn("P21-VEND-ALT-888", vend_ids)

		item_ids = [i["source_record_id"] for i in items]
		self.assertNotIn("P21-ITEM-ALT-777", item_ids)

		wh_ids = [w["source_record_id"] for w in warehouses]
		self.assertNotIn("P21-LOC-ALT-666", wh_ids)

	# Scenario K: Production source configuration blocks before connection creation
	def test_k_production_source_configuration_blocked(self):
		with self.assertRaises(ProductionSourceBlockedError):
			Prophet21SourceAdapter(
				source_instance_id="PROD-LIVE",
				source_environment="PRODUCTION",
				executor=self.executor,
			)

		with self.assertRaises(ProductionSourceBlockedError):
			Prophet21SourceAdapter(
				source_instance_id="STAGE-LIVE",
				source_environment="STAGING",
				executor=self.executor,
			)

	# Scenario L: Write-capable SQL probe blocks before executor
	def test_l_write_capable_sql_probe_blocked(self):
		dangerous_queries = [
			"UPDATE synthetic_customer SET customer_name = 'Hacked'",
			"DELETE FROM synthetic_customer WHERE customer_id = 'P21-CUST-001'",
			"DROP TABLE synthetic_customer",
			"INSERT INTO synthetic_customer (customer_id) VALUES ('NEW')",
			"TRUNCATE TABLE synthetic_customer",
			"EXEC sp_some_proc",
		]
		for q in dangerous_queries:
			with self.assertRaises(SourceWriteBlockedError):
				self.executor.execute_select(q)

	# Scenario M: Dry run after P21 extraction produces zero target mutation
	def test_m_dry_run_after_p21_extraction_zero_target_mutation(self):
		run = self._create_test_run("DRY-RUN-ZERO")
		extract_entity_to_staging(run_id=run.name, adapter=self.adapter, entity_type="CUSTOMER")
		extract_entity_to_staging(run_id=run.name, adapter=self.adapter, entity_type="VENDOR")

		validate_migration_run(run.name)

		# Target table counts before dry run
		pre_cust = frappe.db.count("Customer")
		pre_supp = frappe.db.count("Supplier")
		pre_wh = frappe.db.count("Warehouse")
		pre_item = frappe.db.count("Item")
		pre_gl = frappe.db.count("GL Entry")
		pre_sle = frappe.db.count("Stock Ledger Entry")

		dry_run_res = execute_dry_run(run.name)

		# Target table counts after dry run
		self.assertEqual(frappe.db.count("Customer"), pre_cust)
		self.assertEqual(frappe.db.count("Supplier"), pre_supp)
		self.assertEqual(frappe.db.count("Warehouse"), pre_wh)
		self.assertEqual(frappe.db.count("Item"), pre_item)
		self.assertEqual(frappe.db.count("GL Entry"), pre_gl)
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), pre_sle)

		self.assertEqual(dry_run_res["target_table_deltas"]["Customer"], 0)
		self.assertEqual(dry_run_res["target_table_deltas"]["Supplier"], 0)
		self.assertEqual(dry_run_res["target_table_deltas"]["Stock Ledger Entry"], 0)
		self.assertEqual(dry_run_res["target_table_deltas"]["GL Entry"], 0)

	# Scenario N: Fixture cleanup / re-entry
	def test_n_fixture_cleanup_and_reentry(self):
		run = self._create_test_run("CLEANUP-N")
		extract_entity_to_staging(run_id=run.name, adapter=self.adapter, entity_type="CUSTOMER")
		self.assertGreater(frappe.db.count("Migration Staging Row", {"migration_run": run.name}), 0)

		# Explicit cleanup
		frappe.db.delete("Migration Staging Row", {"migration_run": run.name})
		frappe.delete_doc("Migration Run", run.name, force=True, ignore_permissions=True)
		frappe.db.commit()

		self.assertEqual(frappe.db.count("Migration Staging Row", {"migration_run": run.name}), 0)
		self.assertFalse(frappe.db.exists("Migration Run", run.name))
