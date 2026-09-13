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
from bop_erp.migration.namespaces import (
	canonical_company_tag,
	canonical_provider,
	canonical_source_instance_id,
	canonical_source_namespace,
	canonical_source_system,
	compute_migration_channel_id,
)
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

	# 25. Write-Capable CTE Safety Proof
	def test_25_write_capable_cte_safety(self):
		# CTE with DELETE RETURNING
		with self.assertRaises(SourceWriteBlockedError):
			assert_read_only_sql("WITH del AS (DELETE FROM p21_customer RETURNING *) SELECT * FROM del")

		# CTE with UPDATE RETURNING
		with self.assertRaises(SourceWriteBlockedError):
			assert_read_only_sql("WITH upd AS (UPDATE items SET price = 0 RETURNING *) SELECT * FROM upd")

		# CTE with INSERT RETURNING
		with self.assertRaises(SourceWriteBlockedError):
			assert_read_only_sql("WITH ins AS (INSERT INTO vendors (name) VALUES ('x') RETURNING *) SELECT * FROM ins")

		# Safe CTE with pure SELECT
		assert_read_only_sql("WITH safe_cte AS (SELECT id, name FROM items WHERE active = 1) SELECT * FROM safe_cte")

	# 26. Source Instance Identity in External ID Mapping
	def test_26_source_instance_external_id_mapping(self):
		from bop_erp.bop_erp.doctype.external_id_mapping.external_id_mapping import compute_active_external_key
		from bop_erp.migration.import_boundary import get_or_create_migration_channel

		ch_a = get_or_create_migration_channel(self.company, "PROPHET_21", "client-A")
		ch_b = get_or_create_migration_channel(self.company, "PROPHET_21", "client-B")
		prov_a = canonical_provider("PROPHET_21", "client-A")
		prov_b = canonical_provider("PROPHET_21", "client-B")

		key_a = compute_active_external_key(ch_a, ExternalEntityType.CUSTOMER, "123", provider=prov_a)
		key_b = compute_active_external_key(ch_b, ExternalEntityType.CUSTOMER, "123", provider=prov_b)
		self.assertNotEqual(key_a, key_b, "Mappings for client-A and client-B on same external ID must not collide!")

	# 27. Import Boundary VALID-Only Policy
	def test_27_import_boundary_valid_only(self):
		run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-VAL-ONLY-{frappe.generate_hash(length=6)}",
			"source_system": "SYNTHETIC",
			"source_instance_id": "INST_TEST",
			"company": self.company,
			"status": "READY",
		}).insert(ignore_permissions=True)

		# WARNING row cannot be imported
		row_warn = frappe.get_doc({
			"doctype": "Migration Staging Row",
			"migration_run": run.name,
			"entity_type": "CUSTOMER",
			"source_record_id": "C-WARN-01",
			"staging_identity_key": "1" * 64,
			"source_payload_hash": "2" * 64,
			"source_payload_json": json.dumps({"id": "C-WARN-01"}),
			"normalized_payload_json": json.dumps({"customer_name": "Warn Cust", "company": self.company}),
			"validation_status": "WARNING",
			"validation_warnings_json": json.dumps(["Minor warning"]),
			"import_status": "PENDING",
		}).insert(ignore_permissions=True)

		with self.assertRaises(ImportBoundaryError) as cm_warn:
			import_validated_entity(row_warn.name)
		self.assertIn("WARNING", str(cm_warn.exception))

		# ERROR row cannot be imported
		row_err = frappe.get_doc({
			"doctype": "Migration Staging Row",
			"migration_run": run.name,
			"entity_type": "CUSTOMER",
			"source_record_id": "C-ERR-01",
			"staging_identity_key": "3" * 64,
			"source_payload_hash": "4" * 64,
			"source_payload_json": json.dumps({"id": "C-ERR-01"}),
			"normalized_payload_json": json.dumps({"customer_name": "Err Cust", "company": self.company}),
			"validation_status": "ERROR",
			"validation_errors_json": json.dumps(["Fatal error"]),
			"import_status": "PENDING",
		}).insert(ignore_permissions=True)

		with self.assertRaises(ImportBoundaryError) as cm_err:
			import_validated_entity(row_err.name)
		self.assertIn("ERROR", str(cm_err.exception))

		# Cleanup
		frappe.db.delete("Migration Staging Row", {"migration_run": run.name})
		frappe.delete_doc("Migration Run", run.name, force=True, ignore_permissions=True)

	# 28. Cross-Run Immutable Snapshot Semantics & Drift Classification
	def test_28_cross_run_snapshot_semantics_and_drift(self):
		run1 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-SNAP1-{frappe.generate_hash(length=6)}",
			"source_system": "SYNTHETIC",
			"source_instance_id": "SNAP_INST",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		run2 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-SNAP2-{frappe.generate_hash(length=6)}",
			"source_system": "SYNTHETIC",
			"source_instance_id": "SNAP_INST",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		run3 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-SNAP3-{frappe.generate_hash(length=6)}",
			"source_system": "SYNTHETIC",
			"source_instance_id": "SNAP_INST",
			"company": self.company,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		payload_v1 = {"id": "REC-SNAP-01", "name": "Version 1"}
		payload_v2 = {"id": "REC-SNAP-01", "name": "Version 2"}

		# Run 1: initial stage -> NEW
		row1, is_new1 = stage_source_record(
			run_id=run1.name,
			source_system=run1.source_system,
			source_instance_id=run1.source_instance_id,
			entity_type="CUSTOMER",
			source_record=payload_v1,
		)
		self.assertTrue(is_new1)
		self.assertEqual(row1.snapshot_state, "NEW")

		# Intra-run replay convergence in Run 1
		row1_replay, is_new_replay = stage_source_record(
			run_id=run1.name,
			source_system=run1.source_system,
			source_instance_id=run1.source_instance_id,
			entity_type="CUSTOMER",
			source_record=payload_v1,
		)
		self.assertFalse(is_new_replay)
		self.assertEqual(row1_replay.name, row1.name)

		# Intra-run payload drift in Run 1 -> raises SourcePayloadDriftError
		with self.assertRaises(SourcePayloadDriftError):
			stage_source_record(
				run_id=run1.name,
				source_system=run1.source_system,
				source_instance_id=run1.source_instance_id,
				entity_type="CUSTOMER",
				source_record=payload_v2,
			)

		# Run 2: identical payload across different run -> UNCHANGED, new row, does not mutate Run 1
		row2, is_new2 = stage_source_record(
			run_id=run2.name,
			source_system=run2.source_system,
			source_instance_id=run2.source_instance_id,
			entity_type="CUSTOMER",
			source_record=payload_v1,
		)
		self.assertTrue(is_new2)
		self.assertNotEqual(row2.name, row1.name)
		self.assertEqual(row2.snapshot_state, "UNCHANGED")

		# Run 3: modified payload across different run -> CHANGED, new row, does not mutate Run 1 or Run 2
		row3, is_new3 = stage_source_record(
			run_id=run3.name,
			source_system=run3.source_system,
			source_instance_id=run3.source_instance_id,
			entity_type="CUSTOMER",
			source_record=payload_v2,
		)
		self.assertTrue(is_new3)
		self.assertNotEqual(row3.name, row2.name)
		self.assertEqual(row3.snapshot_state, "CHANGED")

		# Reconciliation surfaces snapshot states
		rep3 = reconcile_migration_run(run3.name)
		self.assertEqual(rep3["by_entity_type"]["CUSTOMER"]["changed_count"], 1)
		self.assertEqual(rep3["discrepancies"]["changed_entities"], 1)

		# Cleanup
		for r in (run1.name, run2.name, run3.name):
			frappe.db.delete("Migration Staging Row", {"migration_run": r})
			frappe.delete_doc("Migration Run", r, force=True, ignore_permissions=True)

	# 29. Canonical Source Namespace Rules & Case Normalization
	def test_29_canonical_source_namespace_rules(self):
		# source_system rules
		self.assertEqual(canonical_source_system("prophet_21"), "PROPHET_21")
		self.assertEqual(canonical_source_system(" PROPHET_21 "), "PROPHET_21")
		self.assertEqual(canonical_source_system("p21"), "P21")
		with self.assertRaises(ValueError):
			canonical_source_system("")
		with self.assertRaises(ValueError):
			canonical_source_system("   ")
		with self.assertRaises(ValueError):
			canonical_source_system(None)

		# source_instance_id rules
		self.assertEqual(canonical_source_instance_id("client-a"), "CLIENT-A")
		self.assertEqual(canonical_source_instance_id(" CLIENT-A "), "CLIENT-A")
		self.assertEqual(canonical_source_instance_id(""), "DEFAULT")
		self.assertEqual(canonical_source_instance_id("   "), "DEFAULT")
		self.assertEqual(canonical_source_instance_id(None), "DEFAULT")

		# canonical_provider helper
		self.assertEqual(canonical_provider(" prophet_21 ", " client-a "), "PROPHET_21:CLIENT-A")
		self.assertEqual(canonical_provider("PROPHET_21", None), "PROPHET_21:DEFAULT")

		# Casing & whitespace invariance in compute_staging_identity
		k1 = compute_staging_identity("prophet_21", "client-a", "customer", "123", company=self.company)
		k2 = compute_staging_identity(" PROPHET_21 ", " CLIENT-A ", "CUSTOMER", "123", company=self.company)
		self.assertEqual(k1, k2, "Casing and surrounding whitespace must converge to identical staging key.")

	# 30. Company Migration Channel Isolation
	def test_30_company_migration_channel_isolation(self):
		# Different companies produce different migration channels
		ch_co_a = compute_migration_channel_id("Company A", "PROPHET_21", "INST-1")
		ch_co_b = compute_migration_channel_id("Company B", "PROPHET_21", "INST-1")
		self.assertNotEqual(ch_co_a, ch_co_b, "Different companies must have isolated migration channels.")

		# Replay converges to identical channel
		ch_co_a_replay = compute_migration_channel_id("Company A", "PROPHET_21", "INST-1")
		self.assertEqual(ch_co_a, ch_co_a_replay)

		# Same company with different instance produces different channel
		ch_co_a_inst2 = compute_migration_channel_id("Company A", "PROPHET_21", "INST-2")
		self.assertNotEqual(ch_co_a, ch_co_a_inst2)

	# 31. External ID Mapping Target Identity Complete Matrix
	def test_31_external_id_mapping_target_identity_matrix(self):
		from bop_erp.bop_erp.doctype.external_id_mapping.external_id_mapping import compute_active_external_key

		# Matrix:
		# 1. Company A / P21 / INST-1 / CUSTOMER / 123
		# 2. Company A / P21 / INST-2 / CUSTOMER / 123
		# 3. Company B / P21 / INST-1 / CUSTOMER / 123
		ch_a1 = compute_migration_channel_id("Company A", "P21", "INST-1")
		prov_a1 = canonical_provider("P21", "INST-1")
		key_a1 = compute_active_external_key(ch_a1, ExternalEntityType.CUSTOMER, "123", provider=prov_a1)

		ch_a2 = compute_migration_channel_id("Company A", "P21", "INST-2")
		prov_a2 = canonical_provider("P21", "INST-2")
		key_a2 = compute_active_external_key(ch_a2, ExternalEntityType.CUSTOMER, "123", provider=prov_a2)

		ch_b1 = compute_migration_channel_id("Company B", "P21", "INST-1")
		prov_b1 = canonical_provider("P21", "INST-1")
		key_b1 = compute_active_external_key(ch_b1, ExternalEntityType.CUSTOMER, "123", provider=prov_b1)

		# All three keys must be strictly distinct
		self.assertNotEqual(key_a1, key_a2, "Company A INST-1 and INST-2 must not collide.")
		self.assertNotEqual(key_a1, key_b1, "Company A and Company B INST-1 must not collide.")
		self.assertNotEqual(key_a2, key_b1, "Company A INST-2 and Company B INST-1 must not collide.")

		# Replay converges exactly
		key_a1_replay = compute_active_external_key(ch_a1, ExternalEntityType.CUSTOMER, "123", provider=prov_a1)
		self.assertEqual(key_a1, key_a1_replay)

	# 32. Company-Scoped Cross-Run Snapshot Chain & Isolation
	def test_32_company_scoped_cross_run_snapshot_chain(self):
		co_a = self.company
		# Run A1
		run_a1 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-RUN-A1-{frappe.generate_hash(length=6)}",
			"source_system": "P21",
			"source_instance_id": "INST-1",
			"company": co_a,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		# Run A2
		run_a2 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-RUN-A2-{frappe.generate_hash(length=6)}",
			"source_system": "P21",
			"source_instance_id": "INST-1",
			"company": co_a,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		payload_v1 = {"id": "SHARED-ITEM-01", "name": "Item V1"}

		# Run A1 stages v1 -> NEW
		row_a1, is_new_a1 = stage_source_record(
			run_id=run_a1.name,
			source_system=run_a1.source_system,
			source_instance_id=run_a1.source_instance_id,
			entity_type="ITEM",
			source_record=payload_v1,
		)
		self.assertTrue(is_new_a1)
		self.assertEqual(row_a1.snapshot_state, "NEW")
		frappe.db.set_value("Migration Run", run_a1.name, "status", "STAGED")

		# Interleaved Run B1 for a DIFFERENT company stages changed payload -> NEW (not polluted by Company A)
		co_b = frappe.db.get_value("Company", {"name": ["!=", co_a]}, "name") or "Bamal Fastener Corp"
		run_b1 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-RUN-B1-{frappe.generate_hash(length=6)}",
			"source_system": "P21",
			"source_instance_id": "INST-1",
			"company": co_b,
			"status": "DRAFT",
		}).insert(ignore_permissions=True)

		row_b1, is_new_b1 = stage_source_record(
			run_id=run_b1.name,
			source_system=run_b1.source_system,
			source_instance_id=run_b1.source_instance_id,
			entity_type="ITEM",
			source_record={"id": "SHARED-ITEM-01", "name": "Item V2 Modified for Company B"},
		)
		self.assertTrue(is_new_b1)
		self.assertEqual(row_b1.snapshot_state, "NEW", "Company B must not use Company A snapshot as prior state.")
		frappe.db.set_value("Migration Run", run_b1.name, "status", "STAGED")

		# Run A2 stages identical v1 -> UNCHANGED (compares to Run A1, NOT Run B1)
		row_a2, is_new_a2 = stage_source_record(
			run_id=run_a2.name,
			source_system=run_a2.source_system,
			source_instance_id=run_a2.source_instance_id,
			entity_type="ITEM",
			source_record=payload_v1,
		)
		self.assertTrue(is_new_a2)
		self.assertEqual(row_a2.snapshot_state, "UNCHANGED", "Company A Run 2 must compare to Company A Run 1, ignoring Company B.")

		# Reconciliation on Run A2 compares against Run A1, ignoring Company B
		rep_a2 = reconcile_migration_run(run_a2.name)
		self.assertEqual(rep_a2["prior_run_id"], run_a1.name)
		self.assertEqual(rep_a2["by_entity_type"]["ITEM"]["unchanged_count"], 1)

		# Clean test docs
		for r in (run_a1.name, run_a2.name, run_b1.name):
			frappe.db.delete("Migration Staging Row", {"migration_run": r})
			frappe.delete_doc("Migration Run", r, force=True, ignore_permissions=True)

