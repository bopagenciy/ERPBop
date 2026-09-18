# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.migration.datasets import (
	ProfileRegistry,
	SourceDatasetProfile,
	compute_source_record_key_hash,
	extract_composite_source_identity,
	extract_source_key_components,
	get_initial_p21_profiles,
	parse_source_record_id,
	serialize_canonical_key,
	stage_dataset_row,
)
from bop_erp.migration.exceptions import SourcePayloadDriftError
from bop_erp.migration.staging import compute_payload_hash


class TestDatasetIngestionLive(FrappeTestCase):
	"""
	Phase 1Y.1 Live Staging Hardening & Persistent Identity Test Suite.
	Tests real database persistence against MariaDB on site 'frontend'.
	Uses strictly synthetic TEST-1Y prefixed fixtures.
	Zero real P21 connections, zero production credentials, zero ERP business mutations.
	"""

	def _cleanup_test_data(self):
		try:
			runs = frappe.db.sql_list(
				"SELECT name FROM `tabMigration Run` WHERE run_id LIKE 'TEST-1Y%'"
			)
			if runs:
				frappe.db.delete("Migration Staging Row", {"migration_run": ("in", runs)})
				for r in runs:
					frappe.delete_doc("Migration Run", r, force=True, ignore_permissions=True)
			frappe.db.delete("Migration Staging Row", {"source_record_id": ("like", "%TEST-1Y%")})
			frappe.db.commit()
		except Exception:
			pass

	def setUp(self):
		super().setUp()
		self._cleanup_test_data()

		self.company = "_Test Company 1Y Live"
		if not frappe.db.exists("Company", self.company):
			frappe.get_doc({
				"doctype": "Company",
				"company_name": self.company,
				"default_currency": "USD",
				"country": "United States",
			}).insert(ignore_permissions=True)

		self.run_id_prefix = f"TEST-1Y-RUN-{frappe.generate_hash(length=6)}"
		self.test_run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": self.run_id_prefix,
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "EXTRACTING",
		}).insert(ignore_permissions=True)

		self.registry = ProfileRegistry()
		for p in get_initial_p21_profiles():
			self.registry.register(p)

	def tearDown(self):
		self._cleanup_test_data()
		super().tearDown()

	# 1. Manifest persistence
	def test_01_manifest_persistence(self):
		manifest = {
			"source_system": "PROPHET_21",
			"profiles_executed": ["P21_ITEM_MASTER", "P21_INVENTORY_LOCATION"],
			"dependency_order": ["P21_ITEM_MASTER", "P21_INVENTORY_LOCATION"],
			"total_rows": 50,
			"deferred_dependencies": ["SUPPLIER_MASTER"],
		}
		self.test_run.manifest_json = json.dumps(manifest)
		self.test_run.save(ignore_permissions=True)

		# Reload fresh from DB
		fresh_run = frappe.get_doc("Migration Run", self.test_run.name)
		self.assertIsNotNone(fresh_run.manifest_json)
		loaded_manifest = json.loads(fresh_run.manifest_json)
		self.assertEqual(loaded_manifest["total_rows"], 50)
		self.assertEqual(loaded_manifest["profiles_executed"], ["P21_ITEM_MASTER", "P21_INVENTORY_LOCATION"])

	# 2. Staging provenance persistence
	def test_02_staging_provenance_persistence(self):
		prof = self.registry.get("P21_INVENTORY_LOCATION")
		row = {
			"Item ID": "TEST-1Y-PROV-01",
			"Company ID": "100",
			"Location ID": "LOC-1",
			"Quantity On Hand": 250,
			"Primary Bin": "BIN-PROV-01",
		}

		doc, is_new = stage_dataset_row(
			run_id=self.test_run.name,
			source_file_identifier="2InventoryLocation_sample.xlsx",
			source_sheet="Sheet1",
			source_row_number=14,
			raw_row=row,
			profile=prof,
			company=self.company,
		)
		self.assertTrue(is_new)

		# Direct SQL fetch to verify real MariaDB column persistence
		db_row = frappe.db.sql(
			"""
			SELECT name, source_profile, profile_version, source_file_identifier,
			       source_sheet, source_row_number, source_record_id, staging_identity_key
			FROM `tabMigration Staging Row`
			WHERE name = %s
			""",
			(doc.name,),
			as_dict=True,
		)[0]

		self.assertEqual(db_row.source_profile, "P21_INVENTORY_LOCATION")
		self.assertEqual(db_row.profile_version, "1.0.0-client-sample")
		self.assertEqual(db_row.source_file_identifier, "2InventoryLocation_sample.xlsx")
		self.assertEqual(db_row.source_sheet, "Sheet1")
		self.assertEqual(db_row.source_row_number, 14)
		self.assertEqual(db_row.source_record_id, '["TEST-1Y-PROV-01","100","LOC-1"]')
		self.assertEqual(len(db_row.staging_identity_key), 64)

	# 3. Read-after-write fidelity
	def test_03_read_after_write_fidelity(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {
			"Item ID": "TEST-1Y-FIDELITY-01",
			"Item Description": "Read-After-Write Precision Item",
			"Extended Info": "Exact string preservation",
			"Sample Numeric": 12345,
		}

		doc, _ = stage_dataset_row(
			run_id=self.test_run.name,
			source_file_identifier="1ItemMaster_sample.xlsx",
			source_sheet="Sheet1",
			source_row_number=2,
			raw_row=row,
			profile=prof,
			company=self.company,
		)

		fresh_doc = frappe.get_doc("Migration Staging Row", doc.name)
		self.assertEqual(fresh_doc.source_payload_hash, compute_payload_hash(row))
		parsed_raw = json.loads(fresh_doc.source_payload_json)
		self.assertEqual(parsed_raw["Item ID"], "TEST-1Y-FIDELITY-01")
		self.assertEqual(parsed_raw["Item Description"], "Read-After-Write Precision Item")
		self.assertEqual(parsed_raw["Sample Numeric"], 12345)
		self.assertEqual(fresh_doc.validation_status, "VALID")

	# 4. Delimiter collision resistance
	def test_04_delimiter_collision_resistance(self):
		prof = SourceDatasetProfile(
			profile_id="COLLISION_CHECK_PROFILE",
			source_system="COLLISION_SYS",
			dataset_name="TestCollisions",
			entity_type="ITEM_MASTER",
			key_fields=["PartA", "PartB"],
		)

		case1_components = ["ABC::DEF", "123"]
		case2_components = ["ABC", "DEF::123"]

		key1 = serialize_canonical_key(case1_components)
		key2 = serialize_canonical_key(case2_components)

		# Canonical representations must differ
		self.assertNotEqual(key1, key2)
		self.assertEqual(key1, '["ABC::DEF","123"]')
		self.assertEqual(key2, '["ABC","DEF::123"]')

		# SHA-256 hashes must differ
		hash1 = compute_source_record_key_hash(case1_components)
		hash2 = compute_source_record_key_hash(case2_components)
		self.assertNotEqual(hash1, hash2)

		# Check parsed extraction
		self.assertEqual(parse_source_record_id(key1), ["ABC::DEF", "123"])
		self.assertEqual(parse_source_record_id(key2), ["ABC", "DEF::123"])

		# Database stage verification: both stage independently without collision
		doc1, is_new1 = stage_dataset_row(
			self.test_run.name, "col.xlsx", "Sheet1", 1,
			{"PartA": "ABC::DEF", "PartB": "123"}, prof
		)
		doc2, is_new2 = stage_dataset_row(
			self.test_run.name, "col.xlsx", "Sheet1", 2,
			{"PartA": "ABC", "PartB": "DEF::123"}, prof
		)
		self.assertTrue(is_new1)
		self.assertTrue(is_new2)
		self.assertNotEqual(doc1.name, doc2.name)
		self.assertNotEqual(doc1.staging_identity_key, doc2.staging_identity_key)

	# 5. Leading-zero identity
	def test_05_leading_zero_identity(self):
		prof = SourceDatasetProfile(
			profile_id="LEADING_ZERO_PROFILE",
			source_system="SYS",
			dataset_name="Data",
			entity_type="ITEM_MASTER",
			key_fields=["Code"],
		)
		key1 = extract_composite_source_identity({"Code": "00123"}, prof)
		key2 = extract_composite_source_identity({"Code": "123"}, prof)

		self.assertEqual(key1, '["00123"]')
		self.assertEqual(key2, '["123"]')
		self.assertNotEqual(key1, key2)

		doc1, is_new1 = stage_dataset_row(self.test_run.name, "f.xlsx", "S1", 1, {"Code": "00123"}, prof)
		doc2, is_new2 = stage_dataset_row(self.test_run.name, "f.xlsx", "S1", 2, {"Code": "123"}, prof)
		self.assertTrue(is_new1)
		self.assertTrue(is_new2)
		self.assertNotEqual(doc1.name, doc2.name)
		self.assertNotEqual(doc1.staging_identity_key, doc2.staging_identity_key)

	# 6. Same-run identical replay convergence
	def test_06_same_run_replay_convergence(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {"Item ID": "TEST-1Y-CONVERGE", "Item Description": "Convergence Test Item"}

		doc1, is_new1 = stage_dataset_row(
			self.test_run.name, "items.xlsx", "Sheet1", 5, row, prof
		)
		self.assertTrue(is_new1)

		# Replaying identical row in same run
		doc2, is_new2 = stage_dataset_row(
			self.test_run.name, "items.xlsx", "Sheet1", 5, row, prof
		)
		self.assertFalse(is_new2)
		self.assertEqual(doc1.name, doc2.name)

		count = frappe.db.count("Migration Staging Row", {
			"migration_run": self.test_run.name,
			"source_record_id": '["TEST-1Y-CONVERGE"]',
		})
		self.assertEqual(count, 1)

	# 7. Same-run changed payload raises SourcePayloadDriftError
	def test_07_same_run_changed_payload_raises_drift_error(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row_orig = {"Item ID": "TEST-1Y-DRIFT-ERR", "Item Description": "Original Description"}
		row_drift = {"Item ID": "TEST-1Y-DRIFT-ERR", "Item Description": "Mutated Conflict"}

		stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 5, row_orig, prof)

		with self.assertRaises(SourcePayloadDriftError) as ctx:
			stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 5, row_drift, prof)

		self.assertIn("Source payload drift detected", str(ctx.exception))

	# 8. Original row remains unchanged after drift attempt
	def test_08_original_row_immutable_after_drift_attempt(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row_orig = {"Item ID": "TEST-1Y-IMMUTABLE", "Item Description": "Immutable Original"}
		row_drift = {"Item ID": "TEST-1Y-IMMUTABLE", "Item Description": "Attempted Mutation"}

		doc1, is_new = stage_dataset_row(
			self.test_run.name, "items.xlsx", "Sheet1", 10, row_orig, prof
		)
		self.assertTrue(is_new)

		# Capture exact initial state from DB
		initial_state = frappe.db.sql(
			"""
			SELECT name, source_payload_json, source_payload_hash,
			       normalized_payload_json, normalized_payload_hash,
			       source_profile, profile_version, source_file_identifier,
			       source_sheet, source_row_number, modified
			FROM `tabMigration Staging Row`
			WHERE name = %s
			""",
			(doc1.name,),
			as_dict=True,
		)[0]

		# Trigger drift error
		with self.assertRaises(SourcePayloadDriftError):
			stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 99, row_drift, prof)

		# Refetch fresh from DB and assert absolute immutability
		after_state = frappe.db.sql(
			"""
			SELECT name, source_payload_json, source_payload_hash,
			       normalized_payload_json, normalized_payload_hash,
			       source_profile, profile_version, source_file_identifier,
			       source_sheet, source_row_number, modified
			FROM `tabMigration Staging Row`
			WHERE name = %s
			""",
			(doc1.name,),
			as_dict=True,
		)[0]

		self.assertEqual(after_state.name, initial_state.name)
		self.assertEqual(after_state.source_payload_json, initial_state.source_payload_json)
		self.assertEqual(after_state.source_payload_hash, initial_state.source_payload_hash)
		self.assertEqual(after_state.normalized_payload_json, initial_state.normalized_payload_json)
		self.assertEqual(after_state.normalized_payload_hash, initial_state.normalized_payload_hash)
		self.assertEqual(after_state.source_profile, initial_state.source_profile)
		self.assertEqual(after_state.profile_version, initial_state.profile_version)
		self.assertEqual(after_state.source_file_identifier, initial_state.source_file_identifier)
		self.assertEqual(after_state.source_sheet, initial_state.source_sheet)
		self.assertEqual(after_state.source_row_number, initial_state.source_row_number)
		self.assertEqual(after_state.modified, initial_state.modified)

		# Row count must remain exactly 1
		count = frappe.db.count("Migration Staging Row", {
			"migration_run": self.test_run.name,
			"source_record_id": '["TEST-1Y-IMMUTABLE"]',
		})
		self.assertEqual(count, 1)

	# 9. Cross-run UNCHANGED
	def test_09_cross_run_unchanged_snapshot(self):
		uid = frappe.generate_hash(length=6)
		run2 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-1Y-RUN2-{uid}",
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "EXTRACTING",
		}).insert(ignore_permissions=True)

		prof = self.registry.get("P21_ITEM_MASTER")
		row = {"Item ID": f"TEST-1Y-SNAP-UNCHANGED-{uid}", "Item Description": "Snapshot Item"}

		doc1, is_new1 = stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 5, row, prof)
		self.assertTrue(is_new1)
		self.assertEqual(doc1.snapshot_state, "NEW")

		# In second run with identical payload, creates distinct row marked UNCHANGED
		doc2, is_new2 = stage_dataset_row(run2.name, "items.xlsx", "Sheet1", 5, row, prof)
		self.assertTrue(is_new2)
		self.assertNotEqual(doc1.name, doc2.name)
		self.assertEqual(doc2.snapshot_state, "UNCHANGED")

	# 10. Cross-run CHANGED
	def test_10_cross_run_changed_snapshot(self):
		uid = frappe.generate_hash(length=6)
		run2 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-1Y-RUN2-CHG-{uid}",
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "EXTRACTING",
		}).insert(ignore_permissions=True)

		prof = self.registry.get("P21_ITEM_MASTER")
		row_v1 = {"Item ID": f"TEST-1Y-SNAP-CHANGED-{uid}", "Item Description": "Version 1 Description"}
		row_v2 = {"Item ID": f"TEST-1Y-SNAP-CHANGED-{uid}", "Item Description": "Version 2 Description Updated"}

		doc1, is_new1 = stage_dataset_row(self.test_run.name, "items_v1.xlsx", "Sheet1", 5, row_v1, prof)
		self.assertTrue(is_new1)
		self.assertEqual(doc1.snapshot_state, "NEW")

		# In second run with changed payload, creates distinct row marked CHANGED
		doc2, is_new2 = stage_dataset_row(run2.name, "items_v2.xlsx", "Sheet1", 5, row_v2, prof)
		self.assertTrue(is_new2)
		self.assertNotEqual(doc1.name, doc2.name)
		self.assertEqual(doc2.snapshot_state, "CHANGED")

		# Ensure original row from Run 1 remains untouched
		doc1_fresh = frappe.get_doc("Migration Staging Row", doc1.name)
		self.assertEqual(json.loads(doc1_fresh.source_payload_json)["Item Description"], "Version 1 Description")
		self.assertEqual(doc1_fresh.snapshot_state, "NEW")

	# 11. Profile-version provenance does not change business identity
	def test_11_profile_version_does_not_change_business_identity(self):
		uid = frappe.generate_hash(length=6)
		run2 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-1Y-RUN2-VER-{uid}",
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "EXTRACTING",
		}).insert(ignore_permissions=True)

		prof_v1 = self.registry.get("P21_ITEM_MASTER")
		# Create an upgraded version of the profile
		prof_v2 = SourceDatasetProfile(
			profile_id=prof_v1.profile_id,
			source_system=prof_v1.source_system,
			dataset_name=prof_v1.dataset_name,
			entity_type=prof_v1.entity_type,
			version="2.0.0-upgraded",
			key_fields=prof_v1.key_fields,
			field_mappings=prof_v1.field_mappings,
		)

		row = {"Item ID": f"TEST-1Y-VER-DECOUPLED-{uid}", "Item Description": "Version Decoupled Item"}

		doc1, _ = stage_dataset_row(self.test_run.name, "file_v1.xlsx", "Sheet1", 5, row, prof_v1)
		doc2, _ = stage_dataset_row(run2.name, "file_v2.xlsx", "Sheet1", 5, row, prof_v2)

		# Staging identity keys must match identically
		self.assertEqual(doc1.staging_identity_key, doc2.staging_identity_key)
		# Snapshot in run 2 correctly detects UNCHANGED business payload
		self.assertEqual(doc2.snapshot_state, "UNCHANGED")
		# Provenance fields reflect respective profile versions
		self.assertEqual(doc1.profile_version, "1.0.0-client-sample")
		self.assertEqual(doc2.profile_version, "2.0.0-upgraded")

	# 12. Unknown extra columns persist
	def test_12_unknown_extra_columns_persist(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {
			"Item ID": "TEST-1Y-EXTRA",
			"Item Description": "Item with extra fields",
			"ClientCustomAttr1": "CustomValueAlpha",
			"LegacyP21Flag99": 42,
		}

		doc, _ = stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 5, row, prof)
		reloaded = frappe.db.get_value("Migration Staging Row", doc.name, "source_payload_json")
		parsed = json.loads(reloaded)

		self.assertEqual(parsed["ClientCustomAttr1"], "CustomValueAlpha")
		self.assertEqual(parsed["LegacyP21Flag99"], 42)

	# 13. None / empty string / zero / False payload fidelity
	def test_13_none_empty_zero_false_payload_fidelity(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {
			"Item ID": "TEST-1Y-TYPES",
			"Item Description": "Types test",
			"NoneVal": None,
			"EmptyStrVal": "",
			"ZeroVal": 0,
			"FalseVal": False,
			"Cost": "145.8925",
		}

		doc, _ = stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 5, row, prof)
		reloaded = json.loads(frappe.db.get_value("Migration Staging Row", doc.name, "source_payload_json"))

		self.assertIsNone(reloaded["NoneVal"])
		self.assertEqual(reloaded["EmptyStrVal"], "")
		self.assertEqual(reloaded["ZeroVal"], 0)
		self.assertIs(reloaded["FalseVal"], False)
		self.assertIsNot(reloaded["FalseVal"], 0)
		self.assertEqual(Decimal(str(reloaded["Cost"])), Decimal("145.8925"))

	# 14. Cleanup leaves zero TEST-1Y fixtures
	def test_14_cleanup_leaves_zero_test_fixtures(self):
		cleanup_run_id = f"TEST-1Y-CLEANUP-{frappe.generate_hash(length=4)}"
		cleanup_run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": cleanup_run_id,
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "EXTRACTING",
		}).insert(ignore_permissions=True)

		prof = self.registry.get("P21_ITEM_MASTER")
		stage_dataset_row(cleanup_run.name, "cln.xlsx", "Sheet1", 1, {"Item ID": "TEST-1Y-CLN-01"}, prof)
		stage_dataset_row(cleanup_run.name, "cln.xlsx", "Sheet1", 2, {"Item ID": "TEST-1Y-CLN-02"}, prof)

		# Execute cleanup query specifically targeting this run
		frappe.db.delete("Migration Staging Row", {"migration_run": cleanup_run.name})
		frappe.delete_doc("Migration Run", cleanup_run.name, force=True, ignore_permissions=True)
		frappe.db.commit()

		remaining_runs = frappe.db.count("Migration Run", {"name": cleanup_run.name})
		remaining_rows = frappe.db.count("Migration Staging Row", {"migration_run": cleanup_run.name})
		self.assertEqual(remaining_runs, 0)
		self.assertEqual(remaining_rows, 0)

