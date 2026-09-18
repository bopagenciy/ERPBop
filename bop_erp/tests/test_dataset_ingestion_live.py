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


class TestDatasetIngestionLive(FrappeTestCase):
	"""
	Phase 1Y.1 Live Staging Hardening & Persistent Identity Test Suite.
	Tests real database persistence against MariaDB on site 'frontend'.
	Uses strictly synthetic TEST-1Y prefixed fixtures.
	Zero real P21 connections, zero production credentials, zero ERP business mutations.
	"""

	def setUp(self):
		super().setUp()
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
		# Scenario N: Complete cleanup of all TEST-1Y fixtures
		try:
			runs = frappe.db.sql_list(
				"SELECT name FROM `tabMigration Run` WHERE run_id LIKE 'TEST-1Y%'"
			)
			if runs:
				frappe.db.delete("Migration Staging Row", {"migration_run": ("in", runs)})
				for r in runs:
					frappe.delete_doc("Migration Run", r, force=True, ignore_permissions=True)
			frappe.db.commit()
		except Exception:
			pass
		super().tearDown()

	# Scenario A: Migration Run can persist new manifest/profile metadata fields
	def test_scenario_a_migration_run_manifest_persistence(self):
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

	# Scenario B & C: Migration Staging Row persists provenance & read-after-write round trip
	def test_scenario_b_c_staging_row_provenance_and_round_trip(self):
		prof = self.registry.get("P21_INVENTORY_LOCATION")
		row = {
			"Item ID": "TEST-1Y-ITEM-01",
			"Company ID": "100",
			"Location ID": "LOC-1",
			"Quantity On Hand": 500,
			"Primary Bin": "BIN-A1-04",
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
			       source_sheet, source_row_number, source_record_id, staging_identity_key,
			       source_payload_hash, normalized_payload_hash, source_payload_json,
			       normalized_payload_json, validation_status
			FROM `tabMigration Staging Row`
			WHERE name = %s
			""",
			(doc.name,),
			as_dict=True,
		)[0]

		# Verify exact fields
		self.assertEqual(db_row.source_profile, "P21_INVENTORY_LOCATION")
		self.assertEqual(db_row.profile_version, "1.0.0-client-sample")
		self.assertEqual(db_row.source_file_identifier, "2InventoryLocation_sample.xlsx")
		self.assertEqual(db_row.source_sheet, "Sheet1")
		self.assertEqual(db_row.source_row_number, 14)
		self.assertEqual(db_row.validation_status, "VALID")

		# Canonical tuple representation check
		self.assertEqual(db_row.source_record_id, '["TEST-1Y-ITEM-01","100","LOC-1"]')
		self.assertEqual(len(db_row.staging_identity_key), 64)
		self.assertEqual(len(db_row.source_payload_hash), 64)

		# Read-after-write payload round trip
		loaded_raw = json.loads(db_row.source_payload_json)
		self.assertEqual(loaded_raw["Item ID"], "TEST-1Y-ITEM-01")
		self.assertEqual(loaded_raw["Primary Bin"], "BIN-A1-04")
		self.assertEqual(loaded_raw["Quantity On Hand"], 500)

	# Scenario D: Composite identity collision counterexample
	def test_scenario_d_composite_identity_collision_counterexample(self):
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

	# Scenario E: Leading-zero identity persists
	def test_scenario_e_leading_zero_identity_persists(self):
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

	# Scenario F: Same-run identical replay converges in database
	def test_scenario_f_same_run_replay_convergence(self):
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

	# Scenario G: Same-run changed payload triggers drift without mutating original raw payload
	def test_scenario_g_same_run_drift_detection(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row_orig = {"Item ID": "TEST-1Y-DRIFT", "Item Description": "Original Description"}
		row_drift = {"Item ID": "TEST-1Y-DRIFT", "Item Description": "Mutated Conflict"}

		doc1, _ = stage_dataset_row(
			self.test_run.name, "items.xlsx", "Sheet1", 5, row_orig, prof
		)

		with self.assertRaises(SourcePayloadDriftError):
			stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 5, row_drift, prof)

		# Verify original row remains completely unchanged in MariaDB
		doc_fresh = frappe.get_doc("Migration Staging Row", doc1.name)
		self.assertEqual(
			json.loads(doc_fresh.source_payload_json)["Item Description"],
			"Original Description",
		)

	# Scenario H: Cross-run same identity creates a separate immutable snapshot
	def test_scenario_h_cross_run_immutable_snapshot(self):
		run2 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-1Y-RUN2-{frappe.generate_hash(length=4)}",
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "EXTRACTING",
		}).insert(ignore_permissions=True)

		prof = self.registry.get("P21_ITEM_MASTER")
		row = {"Item ID": "TEST-1Y-SNAP", "Item Description": "Snapshot Item"}

		doc1, _ = stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 5, row, prof)
		self.assertEqual(doc1.snapshot_state, "NEW")

		# In second run, creates distinct row marked UNCHANGED
		doc2, _ = stage_dataset_row(run2.name, "items.xlsx", "Sheet1", 5, row, prof)
		self.assertNotEqual(doc1.name, doc2.name)
		self.assertEqual(doc2.snapshot_state, "UNCHANGED")

	# Scenario I: Profile retirement/disable does not delete historical staging rows
	def test_scenario_i_profile_retirement_preserves_history(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {"Item ID": "TEST-1Y-RETIRE", "Item Description": "Retire Test Item"}

		doc, _ = stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 5, row, prof)
		row_name = doc.name

		# Disable/retire profile
		self.registry.disable("P21_ITEM_MASTER")
		self.assertFalse(self.registry.get("P21_ITEM_MASTER").active)

		# Staged row in database remains 100% intact
		self.assertTrue(frappe.db.exists("Migration Staging Row", row_name))
		self.registry.enable("P21_ITEM_MASTER")

	# Scenario J: Unknown extra source columns survive raw payload persistence
	def test_scenario_j_unknown_extra_columns_survive(self):
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

	# Scenario K: None / empty string / zero / False survive round trip
	def test_scenario_k_none_empty_zero_false_round_trip(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {
			"Item ID": "TEST-1Y-TYPES",
			"Item Description": "Types test",
			"NoneVal": None,
			"EmptyStrVal": "",
			"ZeroVal": 0,
			"FalseVal": False,
		}

		doc, _ = stage_dataset_row(self.test_run.name, "items.xlsx", "Sheet1", 5, row, prof)
		reloaded = json.loads(frappe.db.get_value("Migration Staging Row", doc.name, "source_payload_json"))

		self.assertIsNone(reloaded["NoneVal"])
		self.assertEqual(reloaded["EmptyStrVal"], "")
		self.assertEqual(reloaded["ZeroVal"], 0)
		self.assertIs(reloaded["FalseVal"], False)
		self.assertIsNot(reloaded["FalseVal"], 0)

	# Scenario L: Decimal-safe source values survive canonical staging
	def test_scenario_l_decimal_safe_source_values(self):
		prof = self.registry.get("P21_INVENTORY_LOCATION")
		row = {
			"Item ID": "TEST-1Y-DECIMAL",
			"Company ID": "100",
			"Location ID": "LOC-1",
			"Moving Average Cost": "145.8925",
			"Quantity On Hand": "1000.50",
		}

		doc, _ = stage_dataset_row(self.test_run.name, "loc.xlsx", "Sheet1", 6, row, prof)
		reloaded = json.loads(frappe.db.get_value("Migration Staging Row", doc.name, "source_payload_json"))
		self.assertEqual(Decimal(str(reloaded["Moving Average Cost"])), Decimal("145.8925"))

	# Scenario M: Manifest row counts & dependency metadata persist and reload correctly
	def test_scenario_m_manifest_row_counts_and_dependencies(self):
		manifest = {
			"run_id": self.test_run.run_id,
			"ordered_profiles": [p.profile_id for p in self.registry.list_profiles()],
			"row_counts": {
				"P21_ITEM_MASTER": 26,
				"P21_INVENTORY_LOCATION": 25,
				"P21_INVENTORY_SUPPLIER": 25,
				"P21_ITEM_UOM": 25,
				"P21_ITEM_DESCRIPTION": 25,
				"P21_ITEM_SUPPLIER_BY_LOCATION": 26,
			},
			"deferred_dependencies": [],
			"partial_items_count": 1,
			"complete_items_count": 25,
		}
		self.test_run.manifest_json = json.dumps(manifest)
		self.test_run.save(ignore_permissions=True)

		refetched = frappe.get_doc("Migration Run", self.test_run.name)
		data = json.loads(refetched.manifest_json)
		self.assertEqual(data["row_counts"]["P21_ITEM_MASTER"], 26)
		self.assertEqual(data["partial_items_count"], 1)
		self.assertEqual(data["complete_items_count"], 25)
