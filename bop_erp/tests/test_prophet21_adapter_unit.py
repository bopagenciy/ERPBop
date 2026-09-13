# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.migration.adapters.base import SourceAdapter
from bop_erp.migration.adapters.prophet21 import (
	Prophet21SourceAdapter,
	SourceColumnMetadata,
	SourceSchemaMetadata,
	SourceTableMetadata,
	extract_entity_to_staging,
)
from bop_erp.migration.adapters.prophet21_queries import (
	DEFAULT_PAGE_SIZE,
	MAX_PAGE_SIZE,
	LogicalEntityMapping,
	build_bounded_select_query,
)
from bop_erp.migration.adapters.sql_executor import (
	ReadOnlySqlExecutor,
	SyntheticSqlExecutor,
	coerce_source_record,
	coerce_source_value,
)
from bop_erp.migration.adapters.synthetic_p21_data import get_synthetic_p21_dataset
from bop_erp.migration.exceptions import (
	ProductionSourceBlockedError,
	SourceMappingError,
	SourceReadError,
	SourceSafetyViolationError,
	SourceSchemaError,
	SourceWriteBlockedError,
	StagingError,
)
from bop_erp.migration.namespaces import (
	canonical_provider,
	canonical_source_instance_id,
	canonical_source_namespace,
	canonical_source_system,
)
from bop_erp.migration.safety import (
	assert_read_only_sql,
	assert_safe_source_target,
	redact_sensitive_payload,
)
from bop_erp.migration.staging import compute_staging_identity, stage_source_record


class TestProphet21AdapterUnit(FrappeTestCase):
	"""
	Comprehensive unit test suite for Phase 1V: Prophet 21 Read-Only Adapter Foundation.
	Covers all 35 required checks and invariants.
	"""

	def setUp(self):
		self.company = (
			frappe.db.get_single_value("Global Defaults", "default_company")
			or frappe.db.get_value("Company", {}, "name")
			or "Industrial DP"
		)
		self.dataset = get_synthetic_p21_dataset(company_id="COMP_1", alt_company_id="COMP_2")
		self.executor = SyntheticSqlExecutor(self.dataset)
		self.adapter = Prophet21SourceAdapter(
			source_instance_id="MAIN",
			source_environment="SYNTHETIC",
			executor=self.executor,
			source_company_id="COMP_1",
		)

	# 1. P21 adapter implements SourceAdapter
	def test_01_p21_adapter_implements_source_adapter(self):
		self.assertTrue(issubclass(Prophet21SourceAdapter, SourceAdapter))
		self.assertIsInstance(self.adapter, SourceAdapter)

	# 2. source_system canonical PROPHET_21
	def test_02_source_system_canonical_prophet_21(self):
		self.assertEqual(self.adapter.source_system, "PROPHET_21")
		self.assertEqual(canonical_source_system("prophet_21"), "PROPHET_21")
		self.assertEqual(self.adapter.source_instance_id, "MAIN")

	# 3. Production environment blocked
	def test_03_production_environment_blocked(self):
		with self.assertRaises(ProductionSourceBlockedError):
			Prophet21SourceAdapter(
				source_environment="PRODUCTION",
				source_instance_id="PROD_1",
			)

	# 4. No connection created when production blocked
	def test_04_no_connection_created_when_production_blocked(self):
		mock_conn_ctor = MagicMock()
		with self.assertRaises(ProductionSourceBlockedError):
			with patch("sqlite3.connect", mock_conn_ctor):
				Prophet21SourceAdapter(
					source_environment="PRODUCTION",
					source_instance_id="PROD_TEST",
				)
		mock_conn_ctor.assert_not_called()

	# 5. SQL always passes read-only gate
	def test_05_sql_always_passes_read_only_gate(self):
		with self.assertRaises(SourceWriteBlockedError):
			self.executor.execute_select("INSERT INTO synthetic_customer VALUES (1, 'Bad')")

		with self.assertRaises(SourceWriteBlockedError):
			self.executor.execute_select("UPDATE synthetic_customer SET customer_name = 'Hacked'")

		with self.assertRaises(SourceWriteBlockedError):
			self.executor.execute_select("DELETE FROM synthetic_customer WHERE 1=1")

		with self.assertRaises(SourceWriteBlockedError):
			self.executor.execute_select("DROP TABLE synthetic_customer")

	# 6. Arbitrary SQL cannot be submitted
	def test_06_arbitrary_sql_cannot_be_submitted(self):
		with self.assertRaises(SourceWriteBlockedError):
			self.executor.execute_select("EXEC sp_who2")

		with self.assertRaises(SourceWriteBlockedError):
			self.executor.execute_select("WITH cte AS (DELETE FROM synthetic_customer RETURNING *) SELECT * FROM cte")

	# 7. Query catalog SELECT-only
	def test_07_query_catalog_select_only(self):
		mapping = self.adapter.entity_mappings["CUSTOMER"]
		sql, params = build_bounded_select_query(mapping, source_company_id="COMP_1", limit=10)
		self.assertTrue(sql.strip().upper().startswith("SELECT"))
		assert_read_only_sql(sql)

	# 8. Query catalog explicit columns
	def test_08_query_catalog_explicit_columns(self):
		mapping = self.adapter.entity_mappings["CUSTOMER"]
		sql, _ = build_bounded_select_query(mapping, limit=10)
		self.assertNotIn("SELECT *", sql.upper())
		for phys_col in mapping.field_mappings.values():
			self.assertIn(phys_col, sql)

	# 9. Parameter binding
	def test_09_parameter_binding(self):
		mapping = self.adapter.entity_mappings["CUSTOMER"]
		sql, params = build_bounded_select_query(mapping, source_company_id="COMP_1", cursor_val="P21-001", limit=50)
		self.assertIn("?", sql)
		self.assertIn("COMP_1", params)
		self.assertIn("P21-001", params)

	# 10. Deterministic ordering required
	def test_10_deterministic_ordering_required(self):
		mapping = self.adapter.entity_mappings["CUSTOMER"]
		sql, _ = build_bounded_select_query(mapping, limit=10)
		self.assertIn(f"ORDER BY {mapping.primary_key} ASC", sql)

	# 11. Default page bound
	def test_11_default_page_bound(self):
		mapping = self.adapter.entity_mappings["CUSTOMER"]
		sql, params = build_bounded_select_query(mapping, limit=None)
		self.assertIn(DEFAULT_PAGE_SIZE, params)

	# 12. Maximum page bound
	def test_12_maximum_page_bound(self):
		mapping = self.adapter.entity_mappings["CUSTOMER"]
		sql, params = build_bounded_select_query(mapping, limit=100000)
		self.assertIn(MAX_PAGE_SIZE, params)

	# 13. Stable source identity required
	def test_13_stable_source_identity_required(self):
		# If source_record_id is missing or unmapped, mapping validation fails
		bad_mapping = LogicalEntityMapping(
			entity_type="CUSTOMER",
			table_name="synthetic_customer",
			primary_key="customer_id",
			field_mappings={"name": "customer_name"},  # lacks source_record_id
		)
		with self.assertRaises(SourceMappingError):
			bad_mapping.validate()

	# 14. Customer extraction
	def test_14_customer_extraction(self):
		custs = list(self.adapter.stream_customers(limit=2))
		self.assertEqual(len(custs), 2)
		self.assertIn("source_record_id", custs[0])
		self.assertIn("name", custs[0])

	# 15. Vendor extraction
	def test_15_vendor_extraction(self):
		vends = list(self.adapter.stream_vendors(limit=2))
		self.assertEqual(len(vends), 2)
		self.assertIn("source_record_id", vends[0])
		self.assertIn("name", vends[0])

	# 16. Item extraction
	def test_16_item_extraction(self):
		items = list(self.adapter.stream_items(limit=2))
		self.assertEqual(len(items), 2)
		self.assertIn("item_code", items[0])
		self.assertIn("source_record_id", items[0])

	# 17. Warehouse extraction
	def test_17_warehouse_extraction(self):
		whs = list(self.adapter.stream_warehouses(limit=2))
		self.assertEqual(len(whs), 2)
		self.assertIn("warehouse_code", whs[0])
		self.assertIn("source_record_id", whs[0])

	# 18. Source company scoping
	def test_18_source_company_scoping(self):
		# Adapter configured for COMP_1 must exclude COMP_2 records
		custs = list(self.adapter.stream_customers())
		ids = [c["source_record_id"] for c in custs]
		self.assertIn("P21-CUST-001", ids)
		self.assertNotIn("P21-CUST-ALT-999", ids)

	# 19. Schema discovery normalization
	def test_19_schema_discovery_normalization(self):
		schema = self.adapter.discover_schema()
		self.assertIsInstance(schema, SourceSchemaMetadata)
		self.assertTrue(schema.has_table("synthetic_customer"))
		self.assertTrue(schema.has_table("synthetic_item"))
		can_resolve, missing = schema.can_resolve_mapping(self.adapter.entity_mappings["CUSTOMER"])
		self.assertTrue(can_resolve)
		self.assertEqual(missing, [])

	# 20. Missing required table mapping
	def test_20_missing_required_table_mapping(self):
		schema = self.adapter.discover_schema()
		missing_mapping = LogicalEntityMapping(
			entity_type="CUSTOMER",
			table_name="non_existent_table_xyz",
			primary_key="id",
			field_mappings={"source_record_id": "id", "name": "name"},
		)
		can_resolve, missing = schema.can_resolve_mapping(missing_mapping)
		self.assertFalse(can_resolve)
		self.assertTrue(any("does not exist" in m for m in missing))

	# 21. Missing required column mapping
	def test_21_missing_required_column_mapping(self):
		schema = self.adapter.discover_schema()
		bad_col_mapping = LogicalEntityMapping(
			entity_type="CUSTOMER",
			table_name="synthetic_customer",
			primary_key="customer_id",
			field_mappings={"source_record_id": "customer_id", "name": "phantom_col_123"},
		)
		can_resolve, missing = schema.can_resolve_mapping(bad_col_mapping)
		self.assertFalse(can_resolve)
		self.assertTrue(any("phantom_col_123" in m for m in missing))

	# 22. Decimal preservation
	def test_22_decimal_preservation(self):
		d = Decimal("9999.99")
		coerced = coerce_source_value(d)
		self.assertEqual(coerced, "9999.99")
		self.assertIsInstance(coerced, str)

	# 23. Datetime serialization
	def test_23_datetime_serialization(self):
		now = datetime(2026, 9, 13, 12, 0, 0)
		self.assertEqual(coerce_source_value(now), "2026-09-13T12:00:00")

	# 24. NULL preservation
	def test_24_null_preservation(self):
		self.assertIsNone(coerce_source_value(None))

	# 25. Empty string preservation
	def test_25_empty_string_preservation(self):
		self.assertEqual(coerce_source_value(""), "")
		self.assertIsNotNone(coerce_source_value(""))

	# 26. Zero preservation
	def test_26_zero_preservation(self):
		self.assertEqual(coerce_source_value(0), 0)
		self.assertEqual(coerce_source_value(0.0), 0.0)
		self.assertNotEqual(coerce_source_value(0), None)
		self.assertNotEqual(coerce_source_value(0), "")

	# 27. False preservation
	def test_27_false_preservation(self):
		self.assertIs(coerce_source_value(False), False)
		self.assertIsNot(coerce_source_value(False), 0)

	# 28. Secret redaction
	def test_28_secret_redaction(self):
		raw_data = {
			"p21_password": "P21_PASSWORD_SENTINEL",
			"api_token": "P21_API_KEY_SENTINEL",
			"customer_name": "Safe Customer",
		}
		redacted = redact_sensitive_payload(raw_data)
		self.assertEqual(redacted["p21_password"], "[REDACTED_SECRET]")
		self.assertEqual(redacted["api_token"], "[REDACTED_SECRET]")
		self.assertEqual(redacted["customer_name"], "Safe Customer")

	# 29. Source metadata contains no secrets
	def test_29_source_metadata_contains_no_secrets(self):
		meta = self.adapter.get_source_metadata()
		meta_str = json.dumps(meta)
		self.assertNotIn("password", meta_str.lower())
		self.assertNotIn("secret", meta_str.lower())
		self.assertNotIn("token", meta_str.lower())

	# 30. Failure mid-extraction state behavior
	def test_30_failure_mid_extraction_state_behavior(self):
		run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-P21-FAIL-{frappe.generate_hash(length=6)}",
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		with self.assertRaises(SourceReadError):
			extract_entity_to_staging(
				adapter=self.adapter,
				run_id=run.name,
				entity_type="CUSTOMER",
				fail_after_records=1,
			)

		run.reload()
		# Run must be FAILED, not COMPLETED
		self.assertEqual(run.status, "FAILED")
		# Staged evidence is retained, not destroyed
		staged_rows = frappe.db.count("Migration Staging Row", {"migration_run": run.name})
		self.assertEqual(staged_rows, 1)

		# Cleanup
		frappe.db.delete("Migration Staging Row", {"migration_run": run.name})
		frappe.delete_doc("Migration Run", run.name, force=True, ignore_permissions=True)

	# 31. Resume / replay staging convergence
	def test_31_resume_replay_staging_convergence(self):
		run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-P21-RESUME-{frappe.generate_hash(length=6)}",
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		# Pass 1: extract 1 customer
		extract_entity_to_staging(run_id=run.name, adapter=self.adapter, entity_type="CUSTOMER", limit=1)
		count1 = frappe.db.count("Migration Staging Row", {"migration_run": run.name})
		self.assertEqual(count1, 1)

		# Pass 2: replay customer 1 plus customer 2
		# Reset status to EXTRACTING to simulate resume
		frappe.db.set_value("Migration Run", run.name, "status", "EXTRACTING")
		extract_entity_to_staging(run_id=run.name, adapter=self.adapter, entity_type="CUSTOMER", limit=2)
		count2 = frappe.db.count("Migration Staging Row", {"migration_run": run.name})
		# Identical replay converges: total staged rows equals 2 (not 3)
		self.assertEqual(count2, 2)

		# Cleanup
		frappe.db.delete("Migration Staging Row", {"migration_run": run.name})
		frappe.delete_doc("Migration Run", run.name, force=True, ignore_permissions=True)

	# 32. No target ERP direct mutation
	def test_32_no_target_erp_direct_mutation(self):
		pre_cust = frappe.db.count("Customer")
		pre_supp = frappe.db.count("Supplier")
		pre_item = frappe.db.count("Item")
		pre_wh = frappe.db.count("Warehouse")

		# Streaming from adapter does NOT insert into ERPNext target tables
		list(self.adapter.stream_customers())
		list(self.adapter.stream_vendors())
		list(self.adapter.stream_items())
		list(self.adapter.stream_warehouses())

		self.assertEqual(frappe.db.count("Customer"), pre_cust)
		self.assertEqual(frappe.db.count("Supplier"), pre_supp)
		self.assertEqual(frappe.db.count("Item"), pre_item)
		self.assertEqual(frappe.db.count("Warehouse"), pre_wh)

	# 33. No P21 write operation exposed
	def test_33_no_p21_write_operation_exposed(self):
		self.assertFalse(hasattr(self.adapter, "execute_write"))
		self.assertFalse(hasattr(self.adapter, "commit_source"))
		self.assertFalse(hasattr(self.adapter, "rollback_source"))
		self.assertFalse(hasattr(self.executor, "execute_write"))
		self.assertFalse(hasattr(self.executor, "commit_source"))

	# 34. Source namespace reuse from Phase 1U
	def test_34_source_namespace_reuse_from_phase_1u(self):
		sys, inst = canonical_source_namespace(self.adapter.source_system, self.adapter.source_instance_id)
		self.assertEqual(sys, "PROPHET_21")
		self.assertEqual(inst, "MAIN")
		self.assertEqual(canonical_provider(sys, inst), "PROPHET_21:MAIN")

	# 35. Production website denylist remains intact
	def test_35_production_website_denylist_remains_intact(self):
		with self.assertRaises(SourceSafetyViolationError):
			assert_safe_source_target("https://theindustrialdepot.com")
		with self.assertRaises(SourceSafetyViolationError):
			assert_safe_source_target("http://api.theindustrialdepot.com/p21")
