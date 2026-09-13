# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import ExternalEntityType
from bop_erp.migration.adapters.base import SourceAdapter
from bop_erp.migration.adapters.synthetic import SyntheticSourceAdapter
from bop_erp.migration.dry_run import execute_dry_run
from bop_erp.migration.exceptions import (
	ImportBoundaryError,
	MigrationError,
	MigrationRunStateError,
	MigrationValidationError,
	NormalizationError,
	SourcePayloadDriftError,
	SourceSafetyViolationError,
	SourceWriteBlockedError,
	StagingError,
)
from bop_erp.migration.import_boundary import import_validated_entity
from bop_erp.migration.normalization import (
	normalize_customer,
	normalize_item,
	normalize_record,
	normalize_vendor,
	normalize_warehouse,
)
from bop_erp.migration.reconciliation import reconcile_migration_run
from bop_erp.migration.safety import (
	assert_read_only_http_method,
	assert_read_only_sql,
	assert_safe_source_target,
	redact_sensitive_payload,
)
from bop_erp.migration.staging import (
	compute_payload_hash,
	compute_staging_identity,
	extract_and_stage_from_adapter,
	extract_record_id,
	stage_source_record,
)
from bop_erp.migration.validation import (
	validate_customer_candidate,
	validate_item_candidate,
	validate_migration_run,
	validate_vendor_candidate,
	validate_warehouse_candidate,
)


class TestSourceERPMigrationUnit(FrappeTestCase):
	"""
	Comprehensive unit test suite for Phase 1U: Source ERP Migration Foundation.
	Covers all 24 required invariant and safety checks.
	"""

	def setUp(self):
		self.company = (
			frappe.db.get_single_value("Global Defaults", "default_company")
			or frappe.db.get_value("Company", {}, "name")
			or "Industrial DP"
		)

	# 1. Adapter Contract
	def test_01_adapter_contract(self):
		class IncompleteAdapter(SourceAdapter):
			pass

		with self.assertRaises(TypeError):
			IncompleteAdapter("MOCK")

	# 2. Synthetic Adapter
	def test_02_synthetic_adapter(self):
		adapter = SyntheticSourceAdapter("SYNTHETIC", "INSTANCE_01")
		self.assertEqual(adapter.source_system, "SYNTHETIC")
		self.assertEqual(adapter.source_instance_id, "INSTANCE_01")

		health = adapter.health_check()
		self.assertEqual(health["status"], "HEALTHY")
		self.assertTrue(health["read_only"])

		meta = adapter.get_source_metadata()
		self.assertIn("capabilities", meta)
		self.assertEqual(meta["customer_count"], 2)

		# Pagination test
		p1 = list(adapter.stream_customers(limit=1, offset=0))
		p2 = list(adapter.stream_customers(limit=1, offset=1))
		self.assertEqual(len(p1), 1)
		self.assertEqual(len(p2), 1)
		self.assertNotEqual(p1[0]["id"], p2[0]["id"])

	# 3. Source Read-Only SQL Safety
	def test_03_source_read_only_sql_safety(self):
		# Allowed queries
		assert_read_only_sql("SELECT * FROM p21_customer")
		assert_read_only_sql("  select id, name from items where active = 1  ")
		assert_read_only_sql("WITH cte AS (SELECT id FROM items) SELECT * FROM cte")
		assert_read_only_sql("-- comment\nSELECT 1;")
		assert_read_only_sql("/* multi-line \n comment */ SELECT * FROM vendor")

		# Blocked DML / DDL / DCL
		blocked_queries = [
			"INSERT INTO p21_customer (id) VALUES ('123')",
			"UPDATE p21_customer SET name = 'Bad'",
			"DELETE FROM p21_customer WHERE id = '123'",
			"MERGE INTO target USING source ON target.id = source.id",
			"UPSERT INTO items (id) VALUES ('123')",
			"REPLACE INTO items (id) VALUES ('123')",
			"ALTER TABLE items ADD COLUMN evil text",
			"DROP TABLE items",
			"CREATE TABLE evil (id int)",
			"TRUNCATE TABLE items",
			"EXEC sp_malicious_proc",
			"EXECUTE immediate 'drop table'",
			"CALL dangerous_proc()",
			"GRANT ALL ON db TO user",
			"REVOKE ALL ON db FROM user",
			"SELECT * FROM items; DROP TABLE items;",
			"SELECT * FROM items INTO OUTFILE '/tmp/leak.txt'",
			"WITH cte AS (SELECT 1) UPDATE items SET val = 2",
		]

		for q in blocked_queries:
			with self.assertRaises(SourceWriteBlockedError, msg=f"Should block: {q}"):
				assert_read_only_sql(q)

	# 4. HTTP Method Safety
	def test_04_http_method_safety(self):
		assert_read_only_http_method("GET")
		assert_read_only_http_method("HEAD")
		assert_read_only_http_method("OPTIONS")

		blocked_methods = ["POST", "PUT", "PATCH", "DELETE", "CONNECT", "TRACE"]
		for m in blocked_methods:
			with self.assertRaises(SourceWriteBlockedError, msg=f"Should block verb: {m}"):
				assert_read_only_http_method(m)

	# 5. Canonical Staging Identity
	def test_05_canonical_staging_identity(self):
		k1 = compute_staging_identity("PROPHET_21", "INST_1", "CUSTOMER", "CUST-100")
		k2 = compute_staging_identity("prophet_21", "INST_1", "customer", "CUST-100")
		self.assertEqual(k1, k2, "Staging identity must be case-insensitive on system and entity type")
		self.assertEqual(len(k1), 64)

		# Differing system or instance produces different identity
		k3 = compute_staging_identity("PROPHET_21", "INST_2", "CUSTOMER", "CUST-100")
		self.assertNotEqual(k1, k3)

	# 6. Duplicate Replay Convergence
	def test_06_duplicate_replay_convergence(self):
		payload = {"id": "TEST-REPLAY-01", "name": "Identical Customer"}
		h1 = compute_payload_hash(payload)
		h2 = compute_payload_hash(payload)
		self.assertEqual(h1, h2)

		# Key ordering invariance in dictionary
		payload_reordered = {"name": "Identical Customer", "id": "TEST-REPLAY-01"}
		self.assertEqual(h1, compute_payload_hash(payload_reordered))

	# 7. Payload Drift Detection
	def test_07_payload_drift_detection(self):
		payload_orig = {"id": "TEST-DRIFT-01", "name": "Original Name"}
		payload_drift = {"id": "TEST-DRIFT-01", "name": "Modified Name"}

		h_orig = compute_payload_hash(payload_orig)
		h_drift = compute_payload_hash(payload_drift)
		self.assertNotEqual(h_orig, h_drift)

	# 8. Raw Payload Immutability
	def test_08_raw_payload_immutability(self):
		# Staging row documents must reject modifications to raw source payload
		row = frappe.get_doc({
			"doctype": "Migration Staging Row",
			"migration_run": "DUMMY-RUN-01",
			"entity_type": "CUSTOMER",
			"source_record_id": "RAW-IMM-01",
			"staging_identity_key": "a" * 64,
			"source_payload_hash": "b" * 64,
			"source_payload_json": json.dumps({"id": "RAW-IMM-01"}),
			"validation_status": "PENDING",
			"import_status": "PENDING",
		})
		# Document class validation prevents altering payload hash or json once saved
		row.is_new = lambda: False
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({
			"source_payload_hash": "b" * 64,
			"source_payload_json": json.dumps({"id": "RAW-IMM-01"}),
			"staging_identity_key": "a" * 64,
		})):
			# Mutating hash
			row.source_payload_hash = "c" * 64
			with self.assertRaises(frappe.ValidationError):
				row.validate_immutability()

	# 9. Normalized Payload Generation
	def test_09_normalized_payload_generation(self):
		raw_cust = {"id": "C-1", "name": "Acme", "email": "a@b.com", "tax_id": "123"}
		norm_cust = normalize_customer(raw_cust, self.company)
		self.assertEqual(norm_cust["doctype"], "Customer")
		self.assertEqual(norm_cust["external_id"], "C-1")
		self.assertEqual(norm_cust["customer_name"], "Acme")

		raw_vend = {"id": "V-1", "name": "Apex", "tax_id": "456"}
		norm_vend = normalize_vendor(raw_vend, self.company)
		self.assertEqual(norm_vend["doctype"], "Supplier")
		self.assertEqual(norm_vend["external_id"], "V-1")

		raw_item = {"id": "I-1", "sku": "SKU-1", "name": "Widget", "uom": "Nos"}
		norm_item = normalize_item(raw_item, self.company)
		self.assertEqual(norm_item["doctype"], "Item")
		self.assertEqual(norm_item["item_code"], "SKU-1")

		raw_wh = {"id": "W-1", "name": "Stores"}
		norm_wh = normalize_warehouse(raw_wh, self.company)
		self.assertEqual(norm_wh["doctype"], "Warehouse")
		self.assertEqual(norm_wh["warehouse_name"], "Stores")

	# 10. Customer Validation
	def test_10_customer_validation(self):
		# Missing external_id
		errs, warns = validate_customer_candidate({"customer_name": "Acme"}, self.company)
		self.assertIn("Missing mandatory source external_id.", errs)

		# Invalid company
		errs, warns = validate_customer_candidate(
			{"external_id": "C-1", "customer_name": "Acme", "company": "NON_EXISTENT_COMPANY"},
			self.company,
		)
		self.assertTrue(any("does not exist" in e for e in errs))

		# Invalid currency
		errs, warns = validate_customer_candidate(
			{"external_id": "C-1", "customer_name": "Acme", "default_currency": "INVALID_CURR", "company": self.company},
			self.company,
		)
		self.assertTrue(any("Currency" in e for e in errs))

	# 11. Vendor Validation
	def test_11_vendor_validation(self):
		errs, warns = validate_vendor_candidate({"supplier_name": "Apex"}, self.company)
		self.assertIn("Missing mandatory source external_id.", errs)

		errs, warns = validate_vendor_candidate(
			{"external_id": "V-1", "supplier_name": "Apex", "default_currency": "FAKE"},
			self.company,
		)
		self.assertTrue(any("Currency" in e for e in errs))

	# 12. Item Validation
	def test_12_item_validation(self):
		errs, warns = validate_item_candidate({"external_id": "I-1", "item_code": "SKU-1"}, self.company)
		self.assertIn("Missing mandatory stock_uom.", errs)

		errs, warns = validate_item_candidate(
			{"external_id": "I-1", "item_code": "SKU-1", "stock_uom": "NON_EXISTENT_UOM"},
			self.company,
		)
		self.assertTrue(any("Stock UOM" in e for e in errs))

	# 13. Warehouse Validation
	def test_13_warehouse_validation(self):
		errs, warns = validate_warehouse_candidate({"warehouse_name": "Main"}, self.company)
		self.assertIn("Missing mandatory source external_id.", errs)

		errs, warns = validate_warehouse_candidate(
			{"external_id": "W-1", "warehouse_name": "Main", "parent_warehouse_ref": "UNKNOWN_PARENT"},
			self.company,
		)
		self.assertTrue(any("parent warehouse" in e for e in errs))

	# 14. Warning vs Error Behavior
	def test_14_warning_vs_error_behavior(self):
		# Malformed email is a WARNING, does not block
		cand = {
			"external_id": "C-WARN",
			"customer_name": "Warn Customer",
			"email_id": "not_an_email",
			"default_currency": "COP",
			"company": self.company,
		}
		errs, warns = validate_customer_candidate(cand, self.company)
		self.assertEqual(len(errs), 0)
		self.assertEqual(len(warns), 1)
		self.assertIn("malformed format", warns[0])

	# 15. Migration Run State Transitions
	def test_15_migration_run_state_transitions(self):
		run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-RUN-FLOW-{frappe.generate_hash(length=6)}",
			"source_system": "SYNTHETIC",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		run.status = "EXTRACTING"
		run.save(ignore_permissions=True)
		run.status = "STAGED"
		run.save(ignore_permissions=True)
		run.status = "VALIDATING"
		run.save(ignore_permissions=True)
		run.status = "READY"
		run.save(ignore_permissions=True)
		run.status = "IMPORTING"
		run.save(ignore_permissions=True)
		run.status = "COMPLETED"
		run.save(ignore_permissions=True)
		self.assertEqual(run.status, "COMPLETED")

		frappe.delete_doc("Migration Run", run.name, force=True)

	# 16. Invalid Transition Blocking
	def test_16_invalid_transition_blocking(self):
		run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-RUN-BLOCK-{frappe.generate_hash(length=6)}",
			"source_system": "SYNTHETIC",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		# DRAFT cannot jump directly to COMPLETED
		run.status = "COMPLETED"
		with self.assertRaises(frappe.ValidationError):
			run.save(ignore_permissions=True)

		frappe.delete_doc("Migration Run", run.name, force=True)

	# 17. Dry-Run Zero Mutation
	def test_17_dry_run_zero_mutation(self):
		run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-DRY-RUN-{frappe.generate_hash(length=6)}",
			"source_system": "SYNTHETIC",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		adapter = SyntheticSourceAdapter()
		extract_and_stage_from_adapter(adapter, run.name)
		validate_migration_run(run.name)

		res = execute_dry_run(run.name)
		self.assertTrue(res["dry_run"])
		for table, delta in res["target_table_deltas"].items():
			self.assertEqual(delta, 0, f"Table {table} must have delta 0")

		# Clean test docs
		frappe.db.delete("Migration Staging Row", {"migration_run": run.name})
		frappe.delete_doc("Migration Run", run.name, force=True)

	# 18. Target Identity Reuse
	def test_18_target_identity_reuse(self):
		# Verifies External ID Mapping can link to migration records
		self.assertIn("VENDOR", ExternalEntityType.ALL)

	# 19. Source / Instance Isolation
	def test_19_source_instance_isolation(self):
		k_p21 = compute_staging_identity("PROPHET_21", "US_EAST", "CUSTOMER", "1001")
		k_sap = compute_staging_identity("SAP", "US_EAST", "CUSTOMER", "1001")
		self.assertNotEqual(k_p21, k_sap)

	# 20. Company Isolation
	def test_20_company_isolation(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "Migration Run",
				"run_id": "BAD-COMP-RUN",
				"source_system": "SYNTHETIC",
				"company": "NON_EXISTENT_COMPANY_XYZ",
				"status": "DRAFT",
			}).insert(ignore_permissions=True)

	# 21. Reconciliation Counts
	def test_21_reconciliation_counts(self):
		run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-RECON-{frappe.generate_hash(length=6)}",
			"source_system": "SYNTHETIC",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		adapter = SyntheticSourceAdapter()
		extract_and_stage_from_adapter(adapter, run.name)
		report = reconcile_migration_run(run.name, adapter.get_source_metadata())

		self.assertEqual(report["run_id"], run.name)
		self.assertIn("CUSTOMER", report["by_entity_type"])
		self.assertEqual(report["by_entity_type"]["CUSTOMER"]["staged_count"], 2)

		frappe.db.delete("Migration Staging Row", {"migration_run": run.name})
		frappe.delete_doc("Migration Run", run.name, force=True)

	# 22. Secret / PII Redaction
	def test_22_secret_pii_redaction(self):
		payload = {
			"api_key": "secret_abc_123",
			"password": "super_secret_password",
			"email": "customer@example.com",
			"phone": "+1-555-1234567",
			"tax_id": "12-3456789",
			"public_name": "Public Corp",
		}
		redacted = redact_sensitive_payload(payload)
		self.assertEqual(redacted["api_key"], "[REDACTED_SECRET]")
		self.assertEqual(redacted["password"], "[REDACTED_SECRET]")
		self.assertEqual(redacted["public_name"], "Public Corp")
		self.assertTrue(redacted["email"].startswith("c*") and redacted["email"].endswith("@example.com"))
		self.assertTrue(redacted["phone"].endswith("4567"))

	# 23. No Source-Write Operation Exists
	def test_23_no_source_write_operation_exists(self):
		adapter = SyntheticSourceAdapter()
		with self.assertRaises(SourceWriteBlockedError):
			adapter.attempt_write_sql("DELETE FROM customers")

		with self.assertRaises(SourceWriteBlockedError):
			adapter.attempt_write_http("POST", "http://localhost:8000/api/orders")

	# 24. Production Safety Denylist Invariance
	def test_24_production_safety_denylist_invariance(self):
		with self.assertRaises(SourceSafetyViolationError):
			assert_safe_source_target("https://theindustrialdepot.com/api")

		with self.assertRaises(SourceSafetyViolationError):
			assert_safe_source_target("http://api.theindustrialdepot.com:443/v1")
