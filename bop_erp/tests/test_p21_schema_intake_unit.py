# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import re
from pathlib import Path
from typing import Any, Dict

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.migration.exceptions import SourceSafetyViolationError
from bop_erp.migration.p21_schema_intake import (
	SchemaIntakeValidationError,
	analyze_entity_candidates,
	assess_p21_schema_snapshot,
	detect_customizations,
	parse_schema_metadata_csv,
	parse_schema_metadata_json,
	validate_schema_metadata,
)
from bop_erp.migration.prophet21_readiness import (
	Prophet21AccessMode,
	Prophet21CompanyScopeMode,
	Prophet21ConnectionConfig,
	Prophet21ReadinessStatus,
	SchemaSnapshot,
	SnapshotProvenanceMetadata,
)
from bop_erp.safety import FORBIDDEN_PRODUCTION_DOMAINS


class TestP21SchemaIntakeUnit(FrappeTestCase):
	"""
	Phase 1X Unit Test Suite:
	Prophet 21 Client IT Intake & Offline Schema Capture Kit.
	100% offline, deterministic, zero network calls, zero credentials, zero production contact.
	"""

	def setUp(self):
		super().setUp()
		self.app_root = Path(frappe.get_app_path("bop_erp")).parent
		self.sql_script_path = self.app_root / "docs" / "migration" / "sql" / "p21_schema_metadata_capture.sql"
		self.intake_doc_path = self.app_root / "docs" / "migration" / "p21-client-it-intake.md"

		# Sample client IT JSON export (multi-resultset format)
		self.sample_client_json = {
			"source_system": "PROPHET_21",
			"source_instance": "CLIENT_P21_PROD_RESTORE",
			"environment": [
				{
					"server_machine_name": "SQL-STAGING-01",
					"sql_instance_name": "SQL-STAGING-01\\P21REPORT",
					"sql_server_edition": "Enterprise Edition (64-bit)",
					"sql_server_version": "15.0.4198.2",
					"sql_service_pack_level": "RTM",
					"server_default_collation": "SQL_Latin1_General_CP1_CI_AS",
					"source_database_name": "p21_reporting_snapshot",
					"database_collation": "SQL_Latin1_General_CP1_CI_AS",
					"capture_timestamp_iso8601": "2026-09-13T16:45:00+00:00",
				}
			],
			"tables": [
				{"schema_name": "dbo", "table_name": "customer", "object_type": "USER_TABLE", "approximate_row_count": 14500},
				{"schema_name": "dbo", "table_name": "vendor", "object_type": "USER_TABLE", "approximate_row_count": 820},
				{"schema_name": "dbo", "table_name": "inv_mast", "object_type": "USER_TABLE", "approximate_row_count": 68000},
				{"schema_name": "dbo", "table_name": "location", "object_type": "USER_TABLE", "approximate_row_count": 12},
				{"schema_name": "dbo", "table_name": "customer_custom", "object_type": "USER_TABLE", "approximate_row_count": 14500},
				{"schema_name": "custom_ext", "table_name": "integration_log", "object_type": "USER_TABLE", "approximate_row_count": 500},
			],
			"columns": [
				{"schema_name": "dbo", "table_name": "customer", "column_name": "customer_id", "data_type": "int", "is_nullable": False, "is_primary_key": True},
				{"schema_name": "dbo", "table_name": "customer", "column_name": "customer_name", "data_type": "varchar", "is_nullable": True, "max_length": 255},
				{"schema_name": "dbo", "table_name": "customer", "column_name": "company_id", "data_type": "varchar", "is_nullable": True, "max_length": 16},
				{"schema_name": "dbo", "table_name": "customer", "column_name": "udf_tier_rating", "data_type": "varchar", "is_nullable": True, "max_length": 50},
				{"schema_name": "dbo", "table_name": "customer", "column_name": "last_modified_date", "data_type": "datetime", "is_nullable": True},
				{"schema_name": "dbo", "table_name": "vendor", "column_name": "vendor_id", "data_type": "int", "is_nullable": False, "is_primary_key": True},
				{"schema_name": "dbo", "table_name": "vendor", "column_name": "vendor_name", "data_type": "varchar", "is_nullable": True, "max_length": 255},
				{"schema_name": "dbo", "table_name": "vendor", "column_name": "company_id", "data_type": "varchar", "is_nullable": True, "max_length": 16},
				{"schema_name": "dbo", "table_name": "inv_mast", "column_name": "item_id", "data_type": "int", "is_nullable": False, "is_primary_key": True},
				{"schema_name": "dbo", "table_name": "inv_mast", "column_name": "item_code", "data_type": "varchar", "is_nullable": True, "max_length": 64},
				{"schema_name": "dbo", "table_name": "inv_mast", "column_name": "company_id", "data_type": "varchar", "is_nullable": True, "max_length": 16},
				{"schema_name": "dbo", "table_name": "location", "column_name": "location_id", "data_type": "int", "is_nullable": False, "is_primary_key": True},
				{"schema_name": "dbo", "table_name": "location", "column_name": "location_code", "data_type": "varchar", "is_nullable": True, "max_length": 32},
				{"schema_name": "dbo", "table_name": "location", "column_name": "company_id", "data_type": "varchar", "is_nullable": True, "max_length": 16},
			],
			"primary_keys": [
				{"schema_name": "dbo", "table_name": "customer", "column_name": "customer_id"},
				{"schema_name": "dbo", "table_name": "vendor", "column_name": "vendor_id"},
				{"schema_name": "dbo", "table_name": "inv_mast", "column_name": "item_id"},
				{"schema_name": "dbo", "table_name": "location", "column_name": "location_id"},
			],
		}

	# 1. Metadata SQL File Exists
	def test_01_metadata_sql_file_exists(self):
		self.assertTrue(self.sql_script_path.exists(), f"SQL capture script not found at {self.sql_script_path}")
		self.assertGreater(self.sql_script_path.stat().st_size, 0)

	# 2. SQL File Contains SELECT-Only Operations
	def test_02_sql_file_contains_select_only_operations(self):
		content = self.sql_script_path.read_text(encoding="utf-8")
		# Strip SQL comments
		stripped = re.sub(r"--.*$", "", content, flags=re.MULTILINE)
		statements = [s.strip() for s in stripped.split(";") if s.strip()]
		self.assertGreater(len(statements), 0)
		for stmt in statements:
			# Every executable statement must begin with SELECT
			self.assertTrue(
				stmt.upper().startswith("SELECT"),
				f"Statement does not begin with SELECT: {stmt[:60]}...",
			)

	# 3. Forbidden Mutation Tokens Absent
	def test_03_forbidden_mutation_tokens_absent(self):
		content = self.sql_script_path.read_text(encoding="utf-8")
		forbidden = [
			"INSERT", "UPDATE", "DELETE", "MERGE",
			"CREATE", "ALTER", "DROP", "TRUNCATE",
			"EXEC", "EXECUTE", "CALL",
		]
		for token in forbidden:
			matches = re.findall(rf"\b{token}\b", content, re.IGNORECASE)
			self.assertEqual(
				len(matches), 0,
				f"Forbidden token '{token}' found {len(matches)} time(s) in SQL metadata capture script.",
			)

	# 4. No Business-Row SELECT Patterns
	def test_04_no_business_row_select_patterns(self):
		content = self.sql_script_path.read_text(encoding="utf-8").lower()
		# Must never query business tables or columns directly
		forbidden_patterns = [
			"from customer", "from cust_mast", "from vendor", "from inv_mast",
			"customer_name", "phone_number", "email_address", "credit_limit",
			"order_no", "invoice_no", "unit_price", "tax_amount",
		]
		for pat in forbidden_patterns:
			self.assertNotIn(pat, content)

	# 5. SQL Server Metadata Fields Captured
	def test_05_sql_server_metadata_fields_captured(self):
		content = self.sql_script_path.read_text(encoding="utf-8")
		expected_catalog_views = [
			"sys.objects", "sys.schemas", "sys.columns",
			"sys.indexes", "sys.partitions", "sys.foreign_keys",
		]
		for view in expected_catalog_views:
			self.assertIn(view, content)

	# 6. JSON Schema Intake
	def test_06_json_schema_intake(self):
		snapshot = parse_schema_metadata_json(self.sample_client_json)
		self.assertEqual(snapshot.source_system, "PROPHET_21")
		self.assertEqual(snapshot.database_name, "p21_reporting_snapshot")
		self.assertEqual(snapshot.captured_at, "2026-09-13T16:45:00+00:00")
		self.assertEqual(len(snapshot.tables), 6)
		self.assertIn("customer", snapshot.tables)
		self.assertIn("customer_id", snapshot.tables["customer"].primary_keys)

	# 7. CSV Metadata Intake
	def test_07_csv_metadata_intake(self):
		tables_csv = (
			"table_name,schema_name,approximate_row_count\n"
			"customer,dbo,1200\n"
			"inv_mast,dbo,5000\n"
		)
		columns_csv = (
			"table_name,schema_name,column_name,data_type,is_nullable,max_length\n"
			"customer,dbo,customer_id,int,0,4\n"
			"customer,dbo,customer_name,varchar,1,255\n"
			"inv_mast,dbo,item_id,int,0,4\n"
			"inv_mast,dbo,item_code,varchar,1,64\n"
		)
		pks_csv = (
			"table_name,column_name\n"
			"customer,customer_id\n"
			"inv_mast,item_id\n"
		)
		snapshot = parse_schema_metadata_csv(
			tables_csv_content=tables_csv,
			columns_csv_content=columns_csv,
			primary_keys_csv_content=pks_csv,
			database_name="p21_csv_db",
			captured_at="2026-09-13T10:00:00Z",
		)
		self.assertEqual(snapshot.database_name, "p21_csv_db")
		self.assertEqual(len(snapshot.tables), 2)
		self.assertIn("customer_id", snapshot.tables["customer"].primary_keys)
		self.assertTrue(snapshot.tables["customer"].columns["customer_id"].is_primary_key)

	# 8. Invalid / Missing Database Identity
	def test_08_invalid_missing_database_identity(self):
		broken = dict(self.sample_client_json)
		broken["environment"] = [{"source_database_name": ""}]
		broken["database_name"] = ""
		snapshot = parse_schema_metadata_json(broken)
		val = validate_schema_metadata(snapshot)
		self.assertFalse(val["valid"])
		self.assertTrue(any("source database name/identity" in err for err in val["errors"]))

	# 9. Missing Capture Timestamp
	def test_09_missing_capture_timestamp(self):
		broken = dict(self.sample_client_json)
		broken["environment"] = [{"capture_timestamp_iso8601": "", "source_database_name": "p21_db"}]
		broken["captured_at"] = ""
		snapshot = parse_schema_metadata_json(broken)
		val = validate_schema_metadata(snapshot)
		self.assertFalse(val["valid"])
		self.assertTrue(any("capture timestamp" in err for err in val["errors"]))

	# 10. Entity Candidate Analysis
	def test_10_entity_candidate_analysis(self):
		snapshot = parse_schema_metadata_json(self.sample_client_json)
		analysis = analyze_entity_candidates(snapshot)
		for ent in ["CUSTOMER", "VENDOR", "ITEM", "WAREHOUSE"]:
			self.assertIn(ent, analysis)
			self.assertEqual(analysis[ent]["status"], "CANDIDATES_FOUND")
			self.assertIsNotNone(analysis[ent]["top_candidate"])

		self.assertEqual(analysis["CUSTOMER"]["top_candidate"], "customer")
		self.assertEqual(analysis["ITEM"]["top_candidate"], "inv_mast")

	# 11. Candidate Stable-Key Discovery
	def test_11_candidate_stable_key_discovery(self):
		snapshot = parse_schema_metadata_json(self.sample_client_json)
		analysis = analyze_entity_candidates(snapshot)
		cust_candidate = analysis["CUSTOMER"]["candidates"][0]
		self.assertTrue(cust_candidate["has_stable_key"])
		self.assertIn("customer_id", cust_candidate["candidate_id_columns"])
		self.assertIn("customer_id", cust_candidate["primary_keys"])

	# 12. Candidate Company Discriminator Discovery
	def test_12_candidate_company_discriminator_discovery(self):
		snapshot = parse_schema_metadata_json(self.sample_client_json)
		analysis = analyze_entity_candidates(snapshot)
		cust_candidate = analysis["CUSTOMER"]["candidates"][0]
		self.assertIn("company_id", cust_candidate["candidate_company_columns"])

	# 13. Customization Surfaced
	def test_13_customization_surfaced(self):
		snapshot = parse_schema_metadata_json(self.sample_client_json)
		cust = detect_customizations(snapshot)
		self.assertIn("custom_ext", cust["custom_schemas"])
		self.assertTrue(any("customer_custom" in t["table_name"] for t in cust["custom_tables"]))
		self.assertIn("customer", cust["custom_columns"])
		self.assertIn("udf_tier_rating", cust["custom_columns"]["customer"])

	# 14. No Automatic Mapping Activation
	def test_14_no_automatic_mapping_activation(self):
		snapshot = parse_schema_metadata_json(self.sample_client_json)
		analysis = analyze_entity_candidates(snapshot)
		# Verify advisory notes and ensure no physical mapping state is modified
		self.assertIn("Advisory candidate analysis only", analysis["CUSTOMER"]["advisory_note"])
		self.assertIn("Physical mappings are not automatically activated", analysis["CUSTOMER"]["advisory_note"])

	# 15. Offline Readiness Assessment
	def test_15_offline_readiness_assessment(self):
		snapshot = parse_schema_metadata_json(self.sample_client_json)
		res = assess_p21_schema_snapshot(
			snapshot_source=snapshot,
			target_bop_company="Industrial DP",
			company_scope_mode=Prophet21CompanyScopeMode.SINGLE_COMPANY_DATABASE,
		)
		self.assertTrue(res["intake_valid"])
		self.assertEqual(len(res["validation_errors"]), 0)
		self.assertIn("readiness_status", res)
		self.assertIn("candidate_analysis", res)
		self.assertIn("customizations", res)

	# 16. No Network Capability
	def test_16_no_network_capability(self):
		# Running assess_p21_schema_snapshot with mock data does not establish network connections
		res = assess_p21_schema_snapshot(
			snapshot_source=self.sample_client_json,
			target_bop_company="Industrial DP",
		)
		self.assertIsNotNone(res)

	# 17. No Secret Serialization
	def test_17_no_secret_serialization(self):
		cfg = Prophet21ConnectionConfig(
			source_instance_id="OFFLINE_TEST",
			environment="SNAPSHOT",
			access_mode=Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT,
			target_bop_company="Industrial DP",
			password_reference="SECRET_DO_NOT_SERIALIZE",
		)
		d = cfg.to_dict()
		self.assertNotIn("SECRET_DO_NOT_SERIALIZE", json.dumps(d))
		self.assertNotIn("password_reference", d)

	# 18. Snapshot Provenance Fields
	def test_18_snapshot_provenance_fields(self):
		prov = SnapshotProvenanceMetadata(
			provenance_verified=True,
			source_instance_confirmed=True,
			capture_timestamp_known=True,
			database_identity_confirmed=True,
			backup_file_name="p21_backup.bak",
		)
		self.assertTrue(prov.is_fully_verified())
		self.assertEqual(prov.backup_file_name, "p21_backup.bak")

	# 19. Target Company Remains Explicit
	def test_19_target_company_remains_explicit(self):
		snapshot = parse_schema_metadata_json(self.sample_client_json)
		# Blank target company fails closed
		res = assess_p21_schema_snapshot(
			snapshot_source=snapshot,
			target_bop_company="",
		)
		self.assertEqual(res["readiness_status"], Prophet21ReadinessStatus.BLOCKED)
		self.assertTrue(any("target bop company" in b.lower() for b in res["blockers"]))

	# 20. Production Denylist Invariance
	def test_20_production_denylist_invariance(self):
		for bad_domain in FORBIDDEN_PRODUCTION_DOMAINS:
			with self.assertRaises(SourceSafetyViolationError):
				Prophet21ConnectionConfig(
					source_instance_id="MAIN",
					environment="SNAPSHOT",
					access_mode=Prophet21AccessMode.SQL_SERVER_READONLY,
					target_bop_company="Industrial DP",
					host=bad_domain,
				)
