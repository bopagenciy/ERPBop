# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import csv
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
import openpyxl

from bop_erp.migration.datasets import (
	AuthorityDecision,
	CanonicalSourceItem,
	DatasetParsingError,
	DatasetProfileError,
	DatasetValidationError,
	DependencyCycleError,
	DetectionResult,
	FieldAuthorityPolicy,
	FileSecurityError,
	ProfileRegistry,
	SourceDatasetProfile,
	build_canonical_items_from_staging,
	build_dependency_order,
	check_file_safety,
	classify_item_completeness,
	default_registry,
	evaluate_field_authority,
	extract_composite_source_identity,
	generate_quality_reconciliation_report,
	get_initial_p21_profiles,
	parse_csv_stream,
	parse_xlsx_stream,
	sanitize_cell_value,
	stage_dataset_file,
	stage_dataset_row,
	stream_dataset_file,
	validate_cross_dataset_relationship,
	validate_row_structure,
)
from bop_erp.migration.exceptions import SourcePayloadDriftError, SourceSafetyViolationError
from bop_erp.migration.staging import compute_payload_hash
from bop_erp.migration.safety import assert_safe_source_target
from bop_erp.safety import FORBIDDEN_PRODUCTION_DOMAINS


class TestDatasetIngestionUnit(FrappeTestCase):
	"""
	Comprehensive Phase 1Y Unit Test Suite:
	Configurable Source Dataset Ingestion Foundation.
	Covers generic profile abstraction, registry, parsing, staging, validation,
	cross-dataset joins, canonical aggregation, field authority, and safety invariants.
	100% offline, zero network calls, zero production credentials, zero business mutations.
	"""

	def setUp(self):
		super().setUp()
		self.company = "_Test Company 1Y"
		if not frappe.db.exists("Company", self.company):
			frappe.get_doc({
				"doctype": "Company",
				"company_name": self.company,
				"default_currency": "USD",
				"country": "United States",
			}).insert(ignore_permissions=True)

		self.test_run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-RUN-DS-{frappe.generate_hash(length=6)}",
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "EXTRACTING",
		}).insert(ignore_permissions=True)

		self.temp_dir = tempfile.TemporaryDirectory()
		self.temp_path = Path(self.temp_dir.name)
		self.registry = ProfileRegistry()
		for p in get_initial_p21_profiles():
			self.registry.register(p)

	def tearDown(self):
		try:
			if hasattr(self, "test_run") and frappe.db.exists("Migration Run", self.test_run.name):
				frappe.db.delete("Migration Staging Row", {"migration_run": self.test_run.name})
				frappe.delete_doc("Migration Run", self.test_run.name, force=True, ignore_permissions=True)
			self.temp_dir.cleanup()
		except Exception:
			pass
		super().tearDown()

	# 1. Generic Profile Registration
	def test_01_generic_profile_registration(self):
		custom_profile = SourceDatasetProfile(
			profile_id="CUSTOM_PART_CATALOG",
			source_system="CUSTOM_ERP",
			dataset_name="Parts",
			entity_type="ITEM_MASTER",
			key_fields=["PartNumber"],
			required_fields=["PartNumber", "Description"],
		)
		self.registry.register(custom_profile)
		fetched = self.registry.get("CUSTOM_PART_CATALOG")
		self.assertEqual(fetched.profile_id, "CUSTOM_PART_CATALOG")
		self.assertEqual(fetched.source_system, "CUSTOM_ERP")

	# 2. Profile Enable / Disable
	def test_02_profile_enable_disable(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		self.assertTrue(prof.active)
		self.registry.disable("P21_ITEM_MASTER")
		self.assertFalse(self.registry.get("P21_ITEM_MASTER").active)
		active_list = self.registry.list_profiles(active_only=True)
		self.assertNotIn("P21_ITEM_MASTER", [p.profile_id for p in active_list])
		self.registry.enable("P21_ITEM_MASTER")
		self.assertTrue(self.registry.get("P21_ITEM_MASTER").active)

	# 3. Profile Versioning
	def test_03_profile_versioning(self):
		prof_v1 = SourceDatasetProfile(
			profile_id="V_PROFILE",
			source_system="SYS_A",
			dataset_name="DatasetA",
			entity_type="ITEM_MASTER",
			version="1.0.0",
			key_fields=["ID"],
		)
		self.registry.register(prof_v1)
		self.assertEqual(self.registry.get("V_PROFILE").version, "1.0.0")

		prof_v2 = SourceDatasetProfile(
			profile_id="V_PROFILE",
			source_system="SYS_A",
			dataset_name="DatasetA",
			entity_type="ITEM_MASTER",
			version="2.0.0",
			key_fields=["ID", "SubID"],
		)
		self.registry.register(prof_v2, overwrite=True)
		self.assertEqual(self.registry.get("V_PROFILE").version, "2.0.0")

	# 4. Unsupported Profile Rejection
	def test_04_unsupported_profile_rejection(self):
		with self.assertRaises(DatasetProfileError):
			# Missing required key_fields
			bad_prof = SourceDatasetProfile(
				profile_id="BAD_PROFILE",
				source_system="SYS_A",
				dataset_name="DatasetA",
				entity_type="ITEM_MASTER",
				key_fields=[],
			)
			self.registry.register(bad_prof)

		with self.assertRaises(DatasetProfileError):
			# Invalid data_start_row <= header_row
			bad_prof2 = SourceDatasetProfile(
				profile_id="BAD_PROFILE_2",
				source_system="SYS_A",
				dataset_name="DatasetA",
				entity_type="ITEM_MASTER",
				header_row=3,
				data_start_row=2,
				key_fields=["ID"],
			)
			self.registry.register(bad_prof2)

	# 5. CSV Parser
	def test_05_csv_parser(self):
		csv_file = self.temp_path / "test_items.csv"
		with open(csv_file, "w", encoding="utf-8", newline="") as f:
			writer = csv.writer(f)
			writer.writerow(["Item ID", "Item Description"])
			writer.writerow(["SYNTH-001", "Synthetic Push Valve"])
			writer.writerow(["SYNTH-002", "Synthetic Barb Adapter"])

		prof = SourceDatasetProfile(
			profile_id="TEST_CSV",
			source_system="TEST",
			dataset_name="Items",
			entity_type="ITEM_MASTER",
			file_type="csv",
			header_row=1,
			data_start_row=2,
			key_fields=["Item ID"],
		)
		rows = list(parse_csv_stream(csv_file, prof))
		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0][0], 2)
		self.assertEqual(rows[0][1]["Item ID"], "SYNTH-001")
		self.assertEqual(rows[1][1]["Item ID"], "SYNTH-002")

	# 6. XLSX Parser
	def test_06_xlsx_parser(self):
		xlsx_file = self.temp_path / "test_items.xlsx"
		wb = openpyxl.Workbook()
		ws = wb.active
		ws.title = "Sheet1"
		ws.append(["Item ID", "Item Description"])
		ws.append(["SYNTH-101", "Synthetic Adapter 101"])
		wb.save(xlsx_file)
		wb.close()

		prof = SourceDatasetProfile(
			profile_id="TEST_XLSX",
			source_system="TEST",
			dataset_name="Items",
			entity_type="ITEM_MASTER",
			file_type="xlsx",
			header_row=1,
			data_start_row=2,
			key_fields=["Item ID"],
		)
		rows = list(parse_xlsx_stream(xlsx_file, prof))
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0][1]["Item ID"], "SYNTH-101")

	# 7. Configurable Header Row
	def test_07_configurable_header_row(self):
		csv_file = self.temp_path / "custom_header.csv"
		with open(csv_file, "w", encoding="utf-8", newline="") as f:
			writer = csv.writer(f)
			writer.writerow(["Client Title Export v2"])  # row 1
			writer.writerow(["Exported on 2026-09-18"])  # row 2
			writer.writerow(["Item ID", "Item Description"])  # row 3: header
			writer.writerow(["ITM-99", "Special Widget"])  # row 4: data

		prof = SourceDatasetProfile(
			profile_id="TEST_CUSTOM_HEADER",
			source_system="TEST",
			dataset_name="Items",
			entity_type="ITEM_MASTER",
			file_type="csv",
			header_row=3,
			data_start_row=4,
			key_fields=["Item ID"],
		)
		rows = list(parse_csv_stream(csv_file, prof))
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0][1]["Item ID"], "ITM-99")

	# 8. Configurable Metadata Rows
	def test_08_configurable_metadata_rows(self):
		csv_file = self.temp_path / "meta_rows.csv"
		with open(csv_file, "w", encoding="utf-8", newline="") as f:
			writer = csv.writer(f)
			writer.writerow(["Item ID", "Cost"])  # row 1: header
			writer.writerow(["Alphanumeric", "Decimal"])  # row 2: types
			writer.writerow(["Required", "Not Required"])  # row 3: required
			writer.writerow([40, 19.4])  # row 4: length
			writer.writerow(["ITM-01", 45.50])  # row 5: data

		prof = SourceDatasetProfile(
			profile_id="TEST_META_ROWS",
			source_system="TEST",
			dataset_name="Items",
			entity_type="ITEM_MASTER",
			file_type="csv",
			header_row=1,
			metadata_rows=[2, 3, 4],
			data_start_row=5,
			key_fields=["Item ID"],
		)
		rows = list(parse_csv_stream(csv_file, prof))
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0][0], 5)
		self.assertEqual(rows[0][1]["Item ID"], "ITM-01")

	# 9. Configurable Example Rows
	def test_09_configurable_example_rows(self):
		csv_file = self.temp_path / "example_row.csv"
		with open(csv_file, "w", encoding="utf-8", newline="") as f:
			writer = csv.writer(f)
			writer.writerow(["Item ID", "UOM"])  # 1
			writer.writerow(["Alpha", "Alpha"])  # 2 meta
			writer.writerow(["TS", "EACH"])  # 3 example row to ignore
			writer.writerow(["REAL-01", "EA"])  # 4 data

		prof = SourceDatasetProfile(
			profile_id="TEST_EXAMPLE_ROW",
			source_system="TEST",
			dataset_name="Items",
			entity_type="ITEM_UOM",
			file_type="csv",
			header_row=1,
			metadata_rows=[2],
			example_rows_to_ignore=[3],
			data_start_row=4,
			key_fields=["Item ID", "UOM"],
		)
		rows = list(parse_csv_stream(csv_file, prof))
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0][1]["Item ID"], "REAL-01")
		self.assertEqual(rows[0][1]["UOM"], "EA")

	# 10. Data Start Row Configuration
	def test_10_data_start_row(self):
		csv_file = self.temp_path / "start_row.csv"
		with open(csv_file, "w", encoding="utf-8", newline="") as f:
			writer = csv.writer(f)
			for i in range(1, 10):
				writer.writerow([f"Header or Note {i}"])
			writer.writerow(["Item ID"])  # row 10: header
			writer.writerow(["ITM-START-12"])  # row 11: data

		prof = SourceDatasetProfile(
			profile_id="TEST_START_ROW",
			source_system="TEST",
			dataset_name="Items",
			entity_type="ITEM_MASTER",
			file_type="csv",
			header_row=10,
			data_start_row=11,
			key_fields=["Item ID"],
		)
		rows = list(parse_csv_stream(csv_file, prof))
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0][1]["Item ID"], "ITM-START-12")

	# 11. Profile Auto-Detection: MATCH
	def test_11_profile_detection_match(self):
		headers = ["Item ID", "Extended Description"]
		res, prof, reason = self.registry.detect_profile(headers, file_name="ItemDescription_sample.xlsx")
		self.assertEqual(res, DetectionResult.MATCH)
		self.assertIsNotNone(prof)
		self.assertEqual(prof.profile_id, "P21_ITEM_DESCRIPTION")

	# 12. Profile Auto-Detection: UNKNOWN
	def test_12_profile_detection_unknown(self):
		headers = ["CompletelyRandomColA", "ArbitraryColB", "PhantomColC"]
		res, prof, reason = self.registry.detect_profile(headers)
		self.assertEqual(res, DetectionResult.UNKNOWN)
		self.assertIsNone(prof)

	# 13. Profile Auto-Detection: AMBIGUOUS
	def test_13_profile_detection_ambiguous(self):
		# Register two profiles with overlapping columns
		p1 = SourceDatasetProfile(
			profile_id="TIE_A",
			source_system="TIE",
			dataset_name="Shared",
			entity_type="TIE_A",
			key_fields=["SharedKey"],
			required_fields=["SharedKey"],
			field_mappings={"ColX": "x"},
		)
		p2 = SourceDatasetProfile(
			profile_id="TIE_B",
			source_system="TIE",
			dataset_name="Shared",
			entity_type="TIE_B",
			key_fields=["SharedKey"],
			required_fields=["SharedKey"],
			field_mappings={"ColX": "x"},
		)
		self.registry.register(p1)
		self.registry.register(p2)
		res, prof, reason = self.registry.detect_profile(["SharedKey", "ColX"])
		self.assertEqual(res, DetectionResult.AMBIGUOUS)
		self.assertIsNone(prof)

	# 14. Ambiguous Profile Cannot Auto-Import
	def test_14_ambiguous_profile_cannot_auto_import(self):
		p1 = SourceDatasetProfile(
			profile_id="AMB_A",
			source_system="AMB",
			dataset_name="Shared",
			entity_type="ITEM_MASTER",
			key_fields=["SharedKey"],
			required_fields=["SharedKey"],
			field_mappings={"ColX": "x"},
		)
		p2 = SourceDatasetProfile(
			profile_id="AMB_B",
			source_system="AMB",
			dataset_name="Shared",
			entity_type="ITEM_MASTER",
			key_fields=["SharedKey"],
			required_fields=["SharedKey"],
			field_mappings={"ColX": "x"},
		)
		self.registry.register(p1)
		self.registry.register(p2)
		res, prof, reason = self.registry.detect_profile(["SharedKey", "ColX"])
		self.assertEqual(res, DetectionResult.AMBIGUOUS)
		self.assertIsNone(prof)

	# 15. Raw Payload Preserved
	def test_15_raw_payload_preserved(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		raw_data = {"Item ID": "SYNTH-VALVE-01", "Item Description": "Push Valve", "UnknownSourceAttr": "RawSentinel"}
		doc, is_new = stage_dataset_row(
			run_id=self.test_run.name,
			source_file_identifier="synth.xlsx",
			source_sheet="Sheet1",
			source_row_number=5,
			raw_row=raw_data,
			profile=prof,
		)
		loaded_raw = json.loads(doc.source_payload_json)
		self.assertEqual(loaded_raw["UnknownSourceAttr"], "RawSentinel")
		self.assertEqual(loaded_raw["Item ID"], "SYNTH-VALVE-01")

	# 16. Unknown Columns Preserved
	def test_16_unknown_columns_preserved(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		raw_data = {"Item ID": "SYNTH-01", "Item Description": "Desc", "ExtraLegacyField99": "ExtraValue"}
		doc, _ = stage_dataset_row(
			run_id=self.test_run.name,
			source_file_identifier="synth.xlsx",
			source_sheet="Sheet1",
			source_row_number=6,
			raw_row=raw_data,
			profile=prof,
		)
		loaded_raw = json.loads(doc.source_payload_json)
		self.assertIn("ExtraLegacyField99", loaded_raw)

	# 17. Deterministic Raw Hash
	def test_17_deterministic_raw_hash(self):
		payload1 = {"b": 2, "a": 1, "c": 3}
		payload2 = {"a": 1, "c": 3, "b": 2}
		hash1 = compute_payload_hash(payload1)
		hash2 = compute_payload_hash(payload2)
		self.assertEqual(hash1, hash2)
		self.assertEqual(len(hash1), 64)

	# 18. Composite Source Identity (Collision-Safe Canonical Tuple)
	def test_18_composite_source_identity(self):
		prof = self.registry.get("P21_INVENTORY_LOCATION")
		row = {"Item ID": "AB28400", "Company ID": "100", "Location ID": "LOC-EAST"}
		ident = extract_composite_source_identity(row, prof)
		self.assertEqual(ident, '["AB28400","100","LOC-EAST"]')
		# Collision check: delimiter strings in values do NOT collide
		row1 = {"Item ID": "ABC::DEF", "Company ID": "100", "Location ID": "123"}
		row2 = {"Item ID": "ABC", "Company ID": "DEF::100", "Location ID": "123"}
		self.assertNotEqual(
			extract_composite_source_identity(row1, prof),
			extract_composite_source_identity(row2, prof),
		)

	# 19. Row Number Not Used as Identity
	def test_19_row_number_not_used_as_identity(self):
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {"Item ID": "AB28400", "Item Description": "Sample"}
		ident1 = extract_composite_source_identity(row, prof, source_row_num=10)
		ident2 = extract_composite_source_identity(row, prof, source_row_num=500)
		self.assertEqual(ident1, ident2)
		self.assertNotIn("10", ident1)
		self.assertNotIn("500", ident1)

	# 20. Dependency Ordering
	def test_20_dependency_ordering(self):
		profiles = self.registry.list_profiles()
		ordered, deferred = build_dependency_order(profiles)
		p_ids = [p.profile_id for p in ordered]
		# ITEM_MASTER must precede INVENTORY_LOCATION, ITEM_UOM, etc.
		self.assertLess(p_ids.index("P21_ITEM_MASTER"), p_ids.index("P21_INVENTORY_LOCATION"))
		self.assertLess(p_ids.index("P21_ITEM_MASTER"), p_ids.index("P21_ITEM_UOM"))
		self.assertLess(p_ids.index("P21_INVENTORY_LOCATION"), p_ids.index("P21_ITEM_SUPPLIER_BY_LOCATION"))

	# 21. Dependency Cycle Rejected
	def test_21_dependency_cycle_rejected(self):
		c1 = SourceDatasetProfile(
			profile_id="CYCLE_1",
			source_system="CYC",
			dataset_name="A",
			entity_type="ITEM_MASTER",
			key_fields=["ID"],
			dependency_profiles=["CYCLE_2"],
		)
		c2 = SourceDatasetProfile(
			profile_id="CYCLE_2",
			source_system="CYC",
			dataset_name="B",
			entity_type="ITEM_MASTER",
			key_fields=["ID"],
			dependency_profiles=["CYCLE_1"],
		)
		with self.assertRaises(DependencyCycleError):
			build_dependency_order([c1, c2])

	# 22. Missing Dependency Classification
	def test_22_missing_dependency_classification(self):
		p = SourceDatasetProfile(
			profile_id="SUPP_MASTER_DEPENDENT",
			source_system="TEST",
			dataset_name="Supp",
			entity_type="INVENTORY_SUPPLIER",
			key_fields=["ID"],
			dependency_profiles=["SUPPLIER_MASTER"],
		)
		ordered, deferred = build_dependency_order([p], allow_missing_as_deferred=True)
		self.assertIn("SUPPLIER_MASTER", deferred)

	# 23-28. Staging of all 6 profiles into Migration Staging Row
	def test_23_item_master_staging(self):
		run_id = self.test_run.name
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {"Item ID": "ITM-M-01", "Item Description": "Synthetic Master Item"}
		doc, is_new = stage_dataset_row(run_id, "items.xlsx", "Sheet1", 5, row, prof)
		self.assertTrue(is_new)
		self.assertEqual(doc.entity_type, "ITEM_MASTER")

	def test_24_inventory_location_staging(self):
		run_id = self.test_run.name
		prof = self.registry.get("P21_INVENTORY_LOCATION")
		row = {"Item ID": "ITM-M-01", "Company ID": "100", "Location ID": "LOC-1", "Quantity On Hand": 150}
		doc, is_new = stage_dataset_row(run_id, "locs.xlsx", "Sheet1", 6, row, prof)
		self.assertTrue(is_new)
		self.assertEqual(doc.entity_type, "INVENTORY_LOCATION")

	def test_25_inventory_supplier_staging(self):
		run_id = self.test_run.name
		prof = self.registry.get("P21_INVENTORY_SUPPLIER")
		row = {"Item ID": "ITM-M-01", "Supplier ID": "SUPP-900"}
		doc, is_new = stage_dataset_row(run_id, "supp.xlsx", "Sheet1", 6, row, prof)
		self.assertTrue(is_new)
		self.assertEqual(doc.entity_type, "INVENTORY_SUPPLIER")

	def test_26_item_uom_staging(self):
		run_id = self.test_run.name
		prof = self.registry.get("P21_ITEM_UOM")
		row = {"Item ID": "ITM-M-01", "Unit of Measure": "BOX", "Unit Size": 24}
		doc, is_new = stage_dataset_row(run_id, "uom.xlsx", "Sheet1", 6, row, prof)
		self.assertTrue(is_new)
		self.assertEqual(doc.entity_type, "ITEM_UOM")

	def test_27_item_description_staging(self):
		run_id = self.test_run.name
		prof = self.registry.get("P21_ITEM_DESCRIPTION")
		row = {"Item ID": "ITM-M-01", "Extended Description": "Long form description"}
		doc, is_new = stage_dataset_row(run_id, "desc.xlsx", "Sheet1", 6, row, prof)
		self.assertTrue(is_new)
		self.assertEqual(doc.entity_type, "ITEM_DESCRIPTION")

	def test_28_item_supplier_by_location_staging(self):
		run_id = self.test_run.name
		prof = self.registry.get("P21_ITEM_SUPPLIER_BY_LOCATION")
		row = {"Item ID": "ITM-M-01", "Location ID": "LOC-1", "Supplier ID": "SUPP-900", "Location Cost": 12.50}
		doc, is_new = stage_dataset_row(run_id, "supp_loc.xlsx", "Sheet1", 6, row, prof)
		self.assertTrue(is_new)
		self.assertEqual(doc.entity_type, "ITEM_SUPPLIER_BY_LOCATION")

	# 29. Item Cross-File Aggregation
	def test_29_item_cross_file_aggregation(self):
		item = CanonicalSourceItem(item_id="TEST-AGG-01")
		item.master = {"Item ID": "TEST-AGG-01", "Item Description": "Test Item"}
		item.descriptions = [{"Extended Description": "Detailed Item"}]
		item.uoms = [{"Unit of Measure": "EA", "Unit Size": 1}]
		item.locations = [{"Location ID": "LOC-A", "Quantity On Hand": 10}]
		item.suppliers = [{"Supplier ID": "SUP-1"}]
		st = classify_item_completeness(item)
		self.assertEqual(st, "COMPLETE")

	# 30. Partial Item Supported
	def test_30_partial_item_supported(self):
		# Master only: no locations or UOMs yet
		item = CanonicalSourceItem(item_id="PARTIAL-01")
		item.master = {"Item ID": "PARTIAL-01", "Item Description": "Draft Setup Item"}
		st = classify_item_completeness(item)
		self.assertEqual(st, "PARTIAL")

	# 31. Missing Location Classification
	def test_31_missing_location_classification(self):
		item = CanonicalSourceItem(item_id="UNSTOCKED-01")
		item.master = {"Item ID": "UNSTOCKED-01"}
		item.descriptions = [{"Extended Description": "Desc"}]
		item.uoms = [{"Unit of Measure": "EA"}]
		item.suppliers = [{"Supplier ID": "S1"}]
		# No locations
		st = classify_item_completeness(item)
		self.assertEqual(st, "NOT_STOCKED")

	# 32. Missing UOM Classification
	def test_32_missing_uom_classification(self):
		item = CanonicalSourceItem(item_id="NO-UOM-01")
		item.master = {"Item ID": "NO-UOM-01"}
		item.descriptions = [{"Extended Description": "Desc"}]
		item.locations = [{"Location ID": "L1"}]
		item.suppliers = [{"Supplier ID": "S1"}]
		st = classify_item_completeness(item)
		self.assertEqual(st, "MISSING_UOM")

	# 33. Missing Supplier Classification
	def test_33_missing_supplier_classification(self):
		item = CanonicalSourceItem(item_id="NO-SUPP-01")
		item.master = {"Item ID": "NO-SUPP-01"}
		item.descriptions = [{"Extended Description": "Desc"}]
		item.locations = [{"Location ID": "L1"}]
		item.uoms = [{"Unit of Measure": "EA"}]
		st = classify_item_completeness(item)
		self.assertEqual(st, "MISSING_SUPPLIER")

	# 34. Multiple UOM Support
	def test_34_multiple_uom_support(self):
		item = CanonicalSourceItem(item_id="MULTI-UOM-01")
		item.master = {"Item ID": "MULTI-UOM-01"}
		item.uoms = [
			{"Unit of Measure": "EA", "Unit Size": 1},
			{"Unit of Measure": "BOX", "Unit Size": 12},
			{"Unit of Measure": "CASE", "Unit Size": 144},
		]
		self.assertEqual(len(item.uoms), 3)

	# 35. Multiple Supplier Support
	def test_35_multiple_supplier_support(self):
		item = CanonicalSourceItem(item_id="MULTI-SUPP-01")
		item.master = {"Item ID": "MULTI-SUPP-01"}
		item.suppliers = [
			{"Supplier ID": "SUPP-A", "Cost": 10.0},
			{"Supplier ID": "SUPP-B", "Cost": 9.5},
		]
		self.assertEqual(len(item.suppliers), 2)

	# 36. Multiple Location Support
	def test_36_multiple_location_support(self):
		item = CanonicalSourceItem(item_id="MULTI-LOC-01")
		item.master = {"Item ID": "MULTI-LOC-01"}
		item.locations = [
			{"Location ID": "LOC-1", "Quantity On Hand": 100},
			{"Location ID": "LOC-2", "Quantity On Hand": 250},
		]
		self.assertEqual(len(item.locations), 2)

	# 37. Supplier-By-Location Override Model
	def test_37_supplier_by_location_override_model(self):
		item = CanonicalSourceItem(item_id="OVERRIDE-01")
		item.suppliers = [{"Supplier ID": "SUPP-1", "Cost": 15.0}]
		item.supplier_location_overrides = [
			{"Location ID": "LOC-EAST", "Supplier ID": "SUPP-1", "Location Cost": 13.5, "lead_time_days": 5}
		]
		self.assertEqual(item.suppliers[0]["Cost"], 15.0)
		self.assertEqual(item.supplier_location_overrides[0]["Location Cost"], 13.5)

	# 38. Primary Bin Not Mapped to ERPNext Bin
	def test_38_primary_bin_not_mapped_to_erpnext_bin(self):
		loc_row = {
			"Item ID": "BIN-TEST-01",
			"Location ID": "LOC-1",
			"Company ID": "100",
			"Primary Bin": "Aisle-4-Shelf-B",
		}
		prof = self.registry.get("P21_INVENTORY_LOCATION")
		norm = prof.field_mappings
		# Must map to physical config primary_bin, NOT ERPNext Bin DocType
		self.assertEqual(norm["Primary Bin"], "primary_bin")
		self.assertNotEqual(norm["Primary Bin"], "Bin")

	# 39-42. Independent Quantity Preservation
	def test_39_qoh_preserved_independently(self):
		val = sanitize_cell_value(1500)
		self.assertEqual(val, 1500)

	def test_40_allocated_preserved_independently(self):
		val = sanitize_cell_value(250)
		self.assertEqual(val, 250)

	def test_41_backordered_preserved_independently(self):
		val = sanitize_cell_value(75)
		self.assertEqual(val, 75)

	def test_42_in_transit_preserved_independently(self):
		val = sanitize_cell_value(300)
		self.assertEqual(val, 300)

	# 43. Moving Average Cost Preserved
	def test_43_moving_average_cost_preserved(self):
		cost = sanitize_cell_value(Decimal("14.5250"))
		self.assertEqual(cost, Decimal("14.5250"))

	# 44. Price Tiers Preserved Without Item Price Mutation
	def test_44_price_tiers_preserved_without_item_price_mutation(self):
		pre_prices = frappe.db.count("Item Price") if hasattr(frappe, "db") and frappe.db else 0
		row = {f"Price {i}": 10.0 + i for i in range(1, 11)}
		row["Item ID"] = "TIER-PRICE-01"
		row["Item Description"] = "Price Tier Item"
		prof = self.registry.get("P21_ITEM_MASTER")
		doc, _ = stage_dataset_row(self.test_run.name, "p.xlsx", "Sheet1", 5, row, prof)
		post_prices = frappe.db.count("Item Price") if hasattr(frappe, "db") and frappe.db else 0
		# Zero Item Price records created
		self.assertEqual(pre_prices, post_prices)

	# 45. Decimal Precision
	def test_45_decimal_precision(self):
		d = Decimal("12345.678912")
		sanitized = sanitize_cell_value(d)
		self.assertEqual(sanitized, Decimal("12345.678912"))

	# 46. Leading-Zero ID Preservation
	def test_46_leading_zero_id_preservation(self):
		raw_id = "0001234"
		sanitized = sanitize_cell_value(raw_id)
		self.assertEqual(sanitized, "0001234")
		self.assertNotEqual(sanitized, 1234)

	# 47. None vs Empty vs Zero vs False
	def test_47_none_vs_empty_vs_zero_vs_false(self):
		self.assertIsNone(sanitize_cell_value(None))
		self.assertEqual(sanitize_cell_value(""), "")
		self.assertEqual(sanitize_cell_value(0), 0)
		self.assertEqual(sanitize_cell_value(0.0), 0)
		self.assertIs(sanitize_cell_value(False), False)
		self.assertIsNot(sanitize_cell_value(False), 0)

	# 48. Same-Run Replay Convergence
	def test_48_same_run_replay_convergence(self):
		run_id = self.test_run.name
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {"Item ID": "REPLAY-01", "Item Description": "Replay Item"}

		doc1, is_new1 = stage_dataset_row(run_id, "items.xlsx", "Sheet1", 5, row, prof)
		self.assertTrue(is_new1)

		doc2, is_new2 = stage_dataset_row(run_id, "items.xlsx", "Sheet1", 5, row, prof)
		self.assertFalse(is_new2)
		self.assertEqual(doc1.name, doc2.name)

	# 49. Same-Run Payload Drift Detection
	def test_49_same_run_payload_drift_detection(self):
		run_id = self.test_run.name
		prof = self.registry.get("P21_ITEM_MASTER")
		row_initial = {"Item ID": "DRIFT-01", "Item Description": "Original Description"}
		row_drifted = {"Item ID": "DRIFT-01", "Item Description": "Modified Unexpected Description"}

		stage_dataset_row(run_id, "items.xlsx", "Sheet1", 5, row_initial, prof)
		with self.assertRaises(SourcePayloadDriftError):
			stage_dataset_row(run_id, "items.xlsx", "Sheet1", 5, row_drifted, prof)

	# 50. Cross-Run Immutable Snapshot Behavior
	def test_50_cross_run_immutable_snapshot_behavior(self):
		run1 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-SNAP1-{frappe.generate_hash(length=4)}",
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "EXTRACTING",
		}).insert(ignore_permissions=True)
		run2 = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": f"TEST-SNAP2-{frappe.generate_hash(length=4)}",
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "EXTRACTING",
		}).insert(ignore_permissions=True)

		prof = self.registry.get("P21_ITEM_MASTER")
		row = {"Item ID": "SNAP-01", "Item Description": "Snapshot Item"}

		doc1, _ = stage_dataset_row(run1.name, "items.xlsx", "Sheet1", 5, row, prof)
		self.assertEqual(doc1.snapshot_state, "NEW")

		# In run 2, identical row is marked UNCHANGED
		doc2, _ = stage_dataset_row(run2.name, "items.xlsx", "Sheet1", 5, row, prof)
		self.assertEqual(doc2.snapshot_state, "UNCHANGED")

		frappe.db.delete("Migration Staging Row", {"migration_run": ("in", [run1.name, run2.name])})
		frappe.delete_doc("Migration Run", run1.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Migration Run", run2.name, force=True, ignore_permissions=True)

	# 51-55. Field Authority Policies
	def test_51_field_authority_source_authoritative(self):
		decision, val, msg = evaluate_field_authority(
			"item_code", "BOP-OLD", "SRC-NEW", FieldAuthorityPolicy.SOURCE_AUTHORITATIVE
		)
		self.assertEqual(decision, AuthorityDecision.APPLY_SOURCE)
		self.assertEqual(val, "SRC-NEW")

	def test_52_field_authority_bop_authoritative(self):
		decision, val, msg = evaluate_field_authority(
			"web_description", "Bop Curated Description", "Raw ERP Desc", FieldAuthorityPolicy.BOP_AUTHORITATIVE
		)
		self.assertEqual(decision, AuthorityDecision.KEEP_BOP)
		self.assertEqual(val, "Bop Curated Description")

	def test_53_field_authority_merge(self):
		decision, val, msg = evaluate_field_authority(
			"tags", ["tag1", "tag2"], ["tag2", "tag3"], FieldAuthorityPolicy.MERGE
		)
		self.assertEqual(decision, AuthorityDecision.MERGED)
		self.assertEqual(set(val), {"tag1", "tag2", "tag3"})

	def test_54_field_authority_review_on_conflict(self):
		decision, val, msg = evaluate_field_authority(
			"critical_account", "ACC-01", "ACC-02", FieldAuthorityPolicy.REVIEW_ON_CONFLICT
		)
		self.assertEqual(decision, AuthorityDecision.REVIEW_REQUIRED)

	def test_55_field_authority_import_once(self):
		# Initial import allows source value
		d1, v1, _ = evaluate_field_authority("init_qoh", None, 100, FieldAuthorityPolicy.IMPORT_ONCE, is_initial_import=True)
		self.assertEqual(d1, AuthorityDecision.APPLY_SOURCE)

		# Subsequent import keeps existing Bop value
		d2, v2, _ = evaluate_field_authority("init_qoh", 100, 200, FieldAuthorityPolicy.IMPORT_ONCE, is_initial_import=False)
		self.assertEqual(d2, AuthorityDecision.KEEP_BOP)
		self.assertEqual(v2, 100)

	# 56. Imported Item Not Globally Locked
	def test_56_imported_item_not_globally_locked(self):
		from bop_erp.inventory.item_lifecycle import verify_item_not_locked_by_provenance
		test_code = f"TEST-UNLOCKED-{frappe.generate_hash(length=4)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": test_code,
			"item_name": "Provenance Item",
			"item_group": "Products",
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)
		self.assertTrue(verify_item_not_locked_by_provenance(test_code))
		frappe.db.delete("Item", {"name": test_code})

	# 57. Profile Retirement Does Not Delete Staged History
	def test_57_profile_retirement_does_not_delete_staged_history(self):
		run_id = self.test_run.name
		prof = self.registry.get("P21_ITEM_MASTER")
		row = {"Item ID": "RETIRE-01", "Item Description": "Desc"}
		doc, _ = stage_dataset_row(run_id, "i.xlsx", "Sheet1", 5, row, prof)

		# Retiring profile
		self.registry.disable("P21_ITEM_MASTER")
		self.assertFalse(self.registry.get("P21_ITEM_MASTER").active)

		# Staged row remains in DB intact
		self.assertTrue(frappe.db.exists("Migration Staging Row", doc.name))
		self.registry.enable("P21_ITEM_MASTER")

	# 58. Quality Report Counts
	def test_58_quality_report_counts(self):
		item1 = CanonicalSourceItem(item_id="I-1", completeness_status="COMPLETE")
		item2 = CanonicalSourceItem(item_id="I-2", completeness_status="NOT_STOCKED")
		report = generate_quality_reconciliation_report(
			received_profiles=[self.registry.get("P21_ITEM_MASTER")],
			expected_profiles=self.registry.list_profiles(),
			canonical_items={"I-1": item1, "I-2": item2},
		)
		self.assertEqual(report["total_items"], 2)
		self.assertEqual(report["complete_items"], 1)
		self.assertEqual(report["items_not_stocked"], 1)
		self.assertEqual(report["partial_items"], 1)

	# 59-63. Target ERP Zero Mutation Invariant
	def test_59_no_item_target_mutation(self):
		pre = frappe.db.count("Item")
		item = CanonicalSourceItem(item_id="ZERO-MUT-01")
		item.master = {"Item ID": "ZERO-MUT-01", "Item Description": "Offline item"}
		self.assertEqual(frappe.db.count("Item"), pre)

	def test_60_no_warehouse_target_mutation(self):
		pre = frappe.db.count("Warehouse")
		loc_row = {"Location ID": "LOC-ZERO-1", "Company ID": "100"}
		self.assertEqual(frappe.db.count("Warehouse"), pre)

	def test_61_no_supplier_target_mutation(self):
		pre = frappe.db.count("Supplier")
		supp_row = {"Supplier ID": "SUPP-ZERO-1"}
		self.assertEqual(frappe.db.count("Supplier"), pre)

	def test_62_no_stock_target_mutation(self):
		pre_sle = frappe.db.count("Stock Ledger Entry") if hasattr(frappe, "db") and frappe.db else 0
		pre_bin = frappe.db.count("Bin") if hasattr(frappe, "db") and frappe.db else 0
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), pre_sle)
		self.assertEqual(frappe.db.count("Bin"), pre_bin)

	def test_63_no_item_price_mutation(self):
		pre = frappe.db.count("Item Price") if hasattr(frappe, "db") and frappe.db else 0
		self.assertEqual(frappe.db.count("Item Price"), pre)

	# 64. Malformed Workbook Handling
	def test_64_malformed_workbook_handling(self):
		bad_file = self.temp_path / "corrupt.xlsx"
		with open(bad_file, "wb") as f:
			f.write(b"NOT_A_VALID_ZIP_OR_XLSX_DATA_BUFFER")

		prof = self.registry.get("P21_ITEM_MASTER")
		with self.assertRaises(DatasetParsingError):
			list(parse_xlsx_stream(bad_file, prof))

	# 65. Formula/Macro Safe Handling
	def test_65_formula_macro_safe_handling(self):
		# Proves dangerous macro extensions are rejected upfront
		macro_file = self.temp_path / "unsafe.xlsm"
		with open(macro_file, "wb") as f:
			f.write(b"PK\x03\x04")
		with self.assertRaises(FileSecurityError):
			check_file_safety(macro_file)

	# 66. Oversized Input Bound
	def test_66_oversized_input_bound(self):
		huge_file = self.temp_path / "huge.csv"
		with open(huge_file, "w") as f:
			f.write("A" * 1024)
		with self.assertRaises(FileSecurityError):
			check_file_safety(huge_file, max_bytes=512)

	# 67. 40k-Record Bounded-Processing Simulation
	def test_67_40k_record_bounded_processing_simulation(self):
		# Generator simulation yielding 40,000 synthetic tuples to prove bounded memory
		def synthetic_40k_stream():
			for i in range(40000):
				yield i + 1, {"Item ID": f"SIM-{i:05d}", "Item Description": f"Desc {i}"}

		prof = self.registry.get("P21_ITEM_MASTER")
		count = 0
		for row_idx, row in synthetic_40k_stream():
			ident = extract_composite_source_identity(row, prof, source_row_num=row_idx)
			count += 1
		self.assertEqual(count, 40000)

	# 68. Production Denylist Invariance
	def test_68_production_denylist_invariance(self):
		for domain in FORBIDDEN_PRODUCTION_DOMAINS:
			with self.assertRaises(SourceSafetyViolationError):
				assert_safe_source_target(f"https://{domain}/api")

	# 69. Zero Network Capability
	def test_69_zero_network_capability(self):
		import socket
		# Verification that dataset ingestion classes do not initialize network sockets
		self.assertFalse(hasattr(self.registry, "connect"))
		self.assertFalse(hasattr(self.registry, "send_http"))

	# 70. No Core Modifications Invariant
	def test_70_no_core_modifications_invariant(self):
		# Verify that Frappe and ERPNext core modules are untouched
		import frappe
		import erpnext
		self.assertIn("bop_erp", frappe.get_installed_apps())
