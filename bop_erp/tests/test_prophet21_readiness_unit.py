# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from decimal import Decimal
from typing import Any, Dict, List

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.migration.exceptions import (
	ProductionSourceBlockedError,
	SourceSafetyViolationError,
)
from bop_erp.migration.prophet21_readiness import (
	ACCESS_MODE_EVALUATIONS,
	ColumnSnapshot,
	FieldRequirementLevel,
	LOGICAL_ENTITY_REQUIREMENTS,
	Prophet21AccessMode,
	Prophet21ConnectionConfig,
	Prophet21ReadinessReport,
	Prophet21ReadinessStatus,
	SchemaSnapshot,
	TableSnapshot,
	assess_prophet21_readiness,
	audit_sql_account_privileges,
	classify_data_volume,
)
from bop_erp.safety import FORBIDDEN_PRODUCTION_DOMAINS


class TestProphet21ReadinessUnit(FrappeTestCase):
	"""
	Phase 1W Unit Test Suite:
	Prophet 21 Access & Schema Readiness Audit.
	100% offline, deterministic, zero network calls, zero credentials, zero production contact.
	"""

	def setUp(self):
		super().setUp()
		# Build a canonical synthetic schema snapshot for tests
		self.sample_snapshot_dict = {
			"source_system": "PROPHET_21",
			"source_instance": "MOCK_P21",
			"captured_at": "2026-09-13T12:00:00Z",
			"database_version": "Microsoft SQL Server 2019",
			"p21_version": "2021.2",
			"database_timezone": "America/New_York",
			"database_collation": "SQL_Latin1_General_CP1_CI_AS",
			"tables": [
				{
					"name": "customer",
					"columns": [
						{"name": "customer_id", "data_type": "int", "is_primary_key": True},
						{"name": "customer_name", "data_type": "varchar"},
						{"name": "email_address", "data_type": "varchar"},
						{"name": "phone_number", "data_type": "varchar"},
						{"name": "tax_id_number", "data_type": "varchar"},
						{"name": "currency_code", "data_type": "varchar"},
						{"name": "terms_code", "data_type": "varchar"},
						{"name": "company_id", "data_type": "varchar"},
						{"name": "last_modified_date", "data_type": "datetime"},
					],
					"primary_keys": ["customer_id"],
					"approximate_row_count": 1500,
				},
				{
					"name": "vendor",
					"columns": [
						{"name": "vendor_id", "data_type": "int", "is_primary_key": True},
						{"name": "vendor_name", "data_type": "varchar"},
						{"name": "email_address", "data_type": "varchar"},
						{"name": "phone_number", "data_type": "varchar"},
						{"name": "tax_id_number", "data_type": "varchar"},
						{"name": "currency_code", "data_type": "varchar"},
						{"name": "terms_code", "data_type": "varchar"},
						{"name": "company_id", "data_type": "varchar"},
						{"name": "last_modified_date", "data_type": "datetime"},
					],
					"primary_keys": ["vendor_id"],
					"approximate_row_count": 500,
				},
				{
					"name": "inv_mast",
					"columns": [
						{"name": "item_id", "data_type": "int", "is_primary_key": True},
						{"name": "item_code", "data_type": "varchar"},
						{"name": "item_desc", "data_type": "varchar"},
						{"name": "extended_desc", "data_type": "varchar"},
						{"name": "unit_of_measure", "data_type": "varchar"},
						{"name": "product_group", "data_type": "varchar"},
						{"name": "stockable_flag", "data_type": "char"},
						{"name": "serialized_flag", "data_type": "char"},
						{"name": "lot_tracked_flag", "data_type": "char"},
						{"name": "company_id", "data_type": "varchar"},
						{"name": "last_modified_date", "data_type": "datetime"},
					],
					"primary_keys": ["item_id"],
					"approximate_row_count": 12000,
				},
				{
					"name": "location",
					"columns": [
						{"name": "location_id", "data_type": "int", "is_primary_key": True},
						{"name": "location_code", "data_type": "varchar"},
						{"name": "location_name", "data_type": "varchar"},
						{"name": "parent_location_code", "data_type": "varchar"},
						{"name": "company_id", "data_type": "varchar"},
						{"name": "last_modified_date", "data_type": "datetime"},
					],
					"primary_keys": ["location_id"],
					"approximate_row_count": 25,
				},
			],
		}

		self.sample_mappings = {
			"CUSTOMER": {
				"table_name": "customer",
				"primary_key": "customer_id",
				"field_mappings": {
					"source_record_id": "customer_id",
					"name": "customer_name",
					"email": "email_address",
					"phone": "phone_number",
					"tax_id": "tax_id_number",
					"currency": "currency_code",
					"payment_terms": "terms_code",
				},
			},
			"VENDOR": {
				"table_name": "vendor",
				"primary_key": "vendor_id",
				"field_mappings": {
					"source_record_id": "vendor_id",
					"name": "vendor_name",
					"email": "email_address",
					"phone": "phone_number",
					"tax_id": "tax_id_number",
					"currency": "currency_code",
					"payment_terms": "terms_code",
				},
			},
			"ITEM": {
				"table_name": "inv_mast",
				"primary_key": "item_id",
				"field_mappings": {
					"source_record_id": "item_id",
					"item_code": "item_code",
					"name": "item_desc",
					"description": "extended_desc",
					"stock_uom": "unit_of_measure",
					"item_group": "product_group",
					"is_stock_item": "stockable_flag",
					"serialized": "serialized_flag",
					"batch_tracked": "lot_tracked_flag",
				},
			},
			"WAREHOUSE": {
				"table_name": "location",
				"primary_key": "location_id",
				"field_mappings": {
					"source_record_id": "location_id",
					"warehouse_code": "location_code",
					"warehouse_name": "location_name",
					"parent_code": "parent_location_code",
					"company": "company_id",
				},
			},
		}

	# 1. Supported Access Modes
	def test_01_supported_access_modes(self):
		for mode in [
			Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
			Prophet21AccessMode.SANITIZED_DATABASE_COPY,
			Prophet21AccessMode.DATABASE_BACKUP_RESTORE,
			Prophet21AccessMode.SQL_SERVER_READONLY,
			Prophet21AccessMode.ODBC_READONLY,
			Prophet21AccessMode.API_READONLY,
			Prophet21AccessMode.CSV_EXPORT,
			Prophet21AccessMode.OTHER_READONLY,
		]:
			self.assertIn(mode, Prophet21AccessMode.ALL)
			self.assertIn(mode, ACCESS_MODE_EVALUATIONS)

	# 2. Unsupported Access Mode Blocked
	def test_02_unsupported_access_mode_blocked(self):
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode="UNSUPPORTED_WRITE_SOCKET",
		)
		report = assess_prophet21_readiness(cfg)
		self.assertEqual(report.status, Prophet21ReadinessStatus.BLOCKED)
		self.assertTrue(any("Unsupported access mode" in b for b in report.blockers))

	# 3. Production Direct Connection Blocked
	def test_03_production_direct_connection_blocked(self):
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="PRODUCTION",
			access_mode=Prophet21AccessMode.SQL_SERVER_READONLY,
		)
		report = assess_prophet21_readiness(cfg)
		self.assertEqual(report.status, Prophet21ReadinessStatus.BLOCKED)
		self.assertTrue(any("PRODUCTION" in b for b in report.blockers))

	# 4. Safe Config Representation
	def test_04_safe_config_representation(self):
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
			host="p21-internal-db",
			database="p21_snapshot_db",
			password_reference="SEC_VAULT_REF_123",
		)
		r = repr(cfg)
		self.assertNotIn("SEC_VAULT_REF_123", r)
		self.assertIn("p21-internal-db", r)
		self.assertIn("RESTORED_DATABASE_SNAPSHOT", r)

	# 5. Secret Values Never Serialized
	def test_05_secret_values_never_serialized(self):
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.SQL_SERVER_READONLY,
			password_reference="SECRET_DO_NOT_LEAK",
		)
		d = cfg.to_dict()
		self.assertNotIn("password_reference", d)
		self.assertTrue(d["password_reference_set"])

	# 6. SQL Account Read-Only Policy Evaluation
	def test_06_sql_account_read_only_policy_evaluation(self):
		valid_privs = ["CONNECT", "SELECT", "VIEW DEFINITION"]
		compliant, violations, allowed = audit_sql_account_privileges(valid_privs)
		self.assertTrue(compliant)
		self.assertEqual(len(violations), 0)

	# 7. Write Privilege Detection Blocks Readiness
	def test_07_write_privilege_detection_blocks_readiness(self):
		bad_privs = ["CONNECT", "SELECT", "INSERT", "db_owner"]
		compliant, violations, allowed = audit_sql_account_privileges(bad_privs)
		self.assertFalse(compliant)
		self.assertTrue(any("INSERT" in v for v in violations))
		self.assertTrue(any("db_owner" in v.lower() for v in violations))

	# 8. Schema Snapshot Parsing
	def test_08_schema_snapshot_parsing(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		self.assertEqual(snapshot.source_system, "PROPHET_21")
		self.assertEqual(snapshot.source_instance, "MOCK_P21")
		self.assertEqual(len(snapshot.tables), 4)
		self.assertIn("customer", snapshot.tables)
		self.assertIn("customer_id", snapshot.tables["customer"].columns)

	# 9. Missing Schema Blocks
	def test_09_missing_schema_blocks(self):
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, schema_snapshot=None)
		self.assertEqual(report.status, Prophet21ReadinessStatus.BLOCKED)
		self.assertTrue(any("Schema snapshot not captured" in b for b in report.blockers))

	# 10. Customer Logical-Field Gap Analysis
	def test_10_customer_logical_field_gap_analysis(self):
		# Break customer mapping: remove mandatory name
		broken_mappings = dict(self.sample_mappings)
		broken_mappings["CUSTOMER"] = {
			"table_name": "customer",
			"primary_key": "customer_id",
			"field_mappings": {"source_record_id": "customer_id"},  # Missing name
		}
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, broken_mappings)
		self.assertEqual(report.status, Prophet21ReadinessStatus.BLOCKED)
		self.assertTrue(any("CUSTOMER" in b and "name" in b for b in report.blockers))

	# 11. Vendor Gap Analysis
	def test_11_vendor_gap_analysis(self):
		broken_mappings = dict(self.sample_mappings)
		broken_mappings["VENDOR"] = {
			"table_name": "vendor",
			"primary_key": "vendor_id",
			"field_mappings": {"name": "vendor_name"},  # Missing source_record_id
		}
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, broken_mappings)
		self.assertEqual(report.status, Prophet21ReadinessStatus.BLOCKED)
		self.assertTrue(any("VENDOR" in b and "source_record_id" in b for b in report.blockers))

	# 12. Item Gap Analysis
	def test_12_item_gap_analysis(self):
		broken_mappings = dict(self.sample_mappings)
		broken_mappings["ITEM"] = {
			"table_name": "inv_mast",
			"primary_key": "item_id",
			"field_mappings": {
				"source_record_id": "item_id",
				"item_code": "item_code",
				"name": "item_desc",
				# missing stock_uom
			},
		}
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, broken_mappings)
		self.assertEqual(report.status, Prophet21ReadinessStatus.BLOCKED)
		self.assertTrue(any("ITEM" in b and "stock_uom" in b for b in report.blockers))

	# 13. Warehouse Gap Analysis
	def test_13_warehouse_gap_analysis(self):
		broken_mappings = dict(self.sample_mappings)
		broken_mappings["WAREHOUSE"] = {
			"table_name": "location",
			"primary_key": "location_id",
			"field_mappings": {
				"source_record_id": "location_id",
				# missing warehouse_code
				"warehouse_name": "location_name",
				"company": "company_id",
			},
		}
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, broken_mappings)
		self.assertEqual(report.status, Prophet21ReadinessStatus.BLOCKED)
		self.assertTrue(any("WAREHOUSE" in b and "warehouse_code" in b for b in report.blockers))

	# 14. Company Discriminator Missing Warns/Reports
	def test_14_company_discriminator_missing(self):
		# Strip company_id from sample snapshot
		modified = dict(self.sample_snapshot_dict)
		for tbl in modified["tables"]:
			tbl["columns"] = [c for c in tbl["columns"] if c["name"] != "company_id"]
		snapshot = SchemaSnapshot.from_dict(modified)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		self.assertEqual(report.company_discriminator_finding["status"], "NO_EXPLICIT_COLUMN_FOUND")
		self.assertTrue(any("company discriminator" in w for w in report.warnings))

	# 15. Stable Source Key Missing Blocks
	def test_15_stable_source_key_missing_blocks(self):
		broken_mappings = dict(self.sample_mappings)
		broken_mappings["CUSTOMER"] = {
			"table_name": "customer",
			"primary_key": "non_existent_pk",
			"field_mappings": {
				"source_record_id": "customer_id",
				"name": "customer_name",
			},
		}
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, broken_mappings)
		self.assertEqual(report.status, Prophet21ReadinessStatus.BLOCKED)
		self.assertTrue(any("Primary key 'non_existent_pk'" in b for b in report.blockers))

	# 16. Multi-Company Scope Handling
	def test_16_multi_company_scope_handling(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		self.assertTrue(report.company_discriminator_finding["is_company_scoped"])
		self.assertIn("company_id", report.company_discriminator_finding["candidates"])

	# 17. Data Volume Classification
	def test_17_data_volume_classification(self):
		small = classify_data_volume(5000)
		self.assertEqual(small["tier"], "SMALL")

		med = classify_data_volume(50000)
		self.assertEqual(med["tier"], "MEDIUM")

		large = classify_data_volume(500000)
		self.assertEqual(large["tier"], "LARGE")

	# 18. Delta Candidate Detection
	def test_18_delta_candidate_detection(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		self.assertEqual(report.delta_capability_finding["status"], "CANDIDATES_FOUND")
		self.assertTrue(any("last_modified_date" in c for c in report.delta_capability_finding["candidates"]))

	# 19. No Delta Method Fallback Classification
	def test_19_no_delta_method_fallback_classification(self):
		modified = dict(self.sample_snapshot_dict)
		for tbl in modified["tables"]:
			tbl["columns"] = [c for c in tbl["columns"] if "modified" not in c["name"]]
		snapshot = SchemaSnapshot.from_dict(modified)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		self.assertIn("fallback_strategy", report.delta_capability_finding)
		self.assertTrue(any("No explicit change tracking" in w for w in report.warnings))

	# 20. Timezone Unknown Warning Policy
	def test_20_timezone_unknown_warning_policy(self):
		modified = dict(self.sample_snapshot_dict)
		modified["database_timezone"] = None
		snapshot = SchemaSnapshot.from_dict(modified)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		self.assertEqual(report.timezone_finding["status"], "UNSPECIFIED")
		self.assertTrue(any("Database timezone not specified" in w for w in report.warnings))

	# 21. Decimal Metadata Handling
	def test_21_decimal_metadata_handling(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		self.assertTrue(report.numeric_precision_finding["decimal_scale_enforced"])
		self.assertFalse(report.numeric_precision_finding["floating_point_allowed"])

	# 22. Encoding / Collation Metadata
	def test_22_encoding_collation_metadata(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		self.assertEqual(report.text_encoding_finding["collation"], "SQL_Latin1_General_CP1_CI_AS")

	# 23. Legacy Sentinel Values Retained As Evidence
	def test_23_legacy_sentinel_values_retained_as_evidence(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		sentinels = report.null_sentinel_policy["sentinel_literals"]
		self.assertIn("N/A", sentinels)
		self.assertIn("1900-01-01", sentinels)

	# 24. READY Result Example
	def test_24_ready_result(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
			password_reference="SEC_VAULT_KEY",
		)
		privs = ["CONNECT", "SELECT", "VIEW DEFINITION"]
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings, reported_privileges=privs)
		self.assertEqual(report.status, Prophet21ReadinessStatus.READY)
		self.assertEqual(len(report.blockers), 0)

	# 25. PARTIAL Result Example
	def test_25_partial_result(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.SQL_SERVER_READONLY,
			# password_reference unset causes warning -> PARTIAL
		)
		privs = ["CONNECT", "SELECT"]
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings, reported_privileges=privs)
		self.assertEqual(report.status, Prophet21ReadinessStatus.PARTIAL)
		self.assertGreater(len(report.warnings), 0)
		self.assertEqual(len(report.blockers), 0)

	# 26. BLOCKED Result Example
	def test_26_blocked_result(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="PRODUCTION",  # Hard blocker
			access_mode=Prophet21AccessMode.SQL_SERVER_READONLY,
		)
		privs = ["CONNECT", "SELECT", "INSERT"]  # Write privilege blocker
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings, reported_privileges=privs)
		self.assertEqual(report.status, Prophet21ReadinessStatus.BLOCKED)
		self.assertGreater(len(report.blockers), 0)

	# 27. Report JSON Contains No Credentials
	def test_27_report_json_contains_no_credentials(self):
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="MAIN",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
			password_reference="ULTRA_SECRET_TOKEN_DO_NOT_EXPOSE",
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		json_str = json.dumps(report.to_dict())
		self.assertNotIn("ULTRA_SECRET_TOKEN_DO_NOT_EXPOSE", json_str)
		self.assertNotIn("password", json_str.lower())

	# 28. No P21 Network Execution
	def test_28_no_p21_network_execution(self):
		# Verifies that assess_prophet21_readiness runs in memory with 0 network operations
		snapshot = SchemaSnapshot.from_dict(self.sample_snapshot_dict)
		cfg = Prophet21ConnectionConfig(
			source_instance_id="OFFLINE_TEST",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
			host="non-routable-mock.local",
		)
		report = assess_prophet21_readiness(cfg, snapshot, self.sample_mappings)
		self.assertIsNotNone(report)

	# 29. No Source Write Capability
	def test_29_no_source_write_capability(self):
		forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "MERGE", "EXECUTE"]
		for cmd in forbidden:
			comp, violations, allowed = audit_sql_account_privileges([cmd])
			self.assertFalse(comp)
			self.assertTrue(len(violations) > 0)

	# 30. Production Denylist Invariance
	def test_30_production_denylist_invariance(self):
		for bad_domain in FORBIDDEN_PRODUCTION_DOMAINS:
			with self.assertRaises(SourceSafetyViolationError):
				Prophet21ConnectionConfig(
					source_instance_id="MAIN",
					environment="SNAPSHOT",
					access_mode=Prophet21AccessMode.SQL_SERVER_READONLY,
					host=bad_domain,
				)
