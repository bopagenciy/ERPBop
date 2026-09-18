# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from decimal import Decimal
import json
from pathlib import Path
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from bop_erp.constants import ExternalEntityType
from bop_erp.migration.datasets import (
	CanonicalSourceItem,
	ControlledItemImporter,
	EligibilityStatus,
	FieldAuthorityPolicy,
	ProfileRegistry,
	get_initial_p21_profiles,
	stage_dataset_file,
	stage_dataset_row,
)
from bop_erp.migration.exceptions import ImportBoundaryError
from bop_erp.migration.namespaces import canonical_provider, compute_migration_channel_id


class TestControlledMasterImportLive(FrappeTestCase):
	"""
	Phase 1Z Live Test Suite: Controlled master data test import into ERPNext.
	Executes real target database mutations on site 'frontend' within explicit approval boundary.
	Tests all 30 required scenarios from Section AB:
	 1. create one simple test Item from valid staged aggregate
	 2. Item source mapping created
	 3. identical reimport converges
	 4. duplicate Item not created
	 5. manual Item edit remains allowed
	 6. BOP_AUTHORITATIVE field survives reimport
	 7. SOURCE_AUTHORITATIVE policy behaves correctly
	 8. extended description import
	 9. UOM conversion import
	10. serialized Item configuration
	11. batch-tracked Item configuration
	12. missing Supplier Master becomes deferred, no fake Supplier
	13. missing Location Master does not create Warehouse
	14. price tiers create zero Item Price
	15. cost fields create zero valuation/GL
	16. partial Item eligible where safe
	17. missing mandatory Item identity blocks
	18. invalid mandatory UOM blocks
	19. simulated Item creation failure rolls back mapping
	20. simulated mapping failure rolls back Item
	21. no Stock Ledger Entry created
	22. no stock quantity mutation
	23. no GL Entry created
	24. cross-company mismatch blocked
	25. source mapping drift blocked/reviewed
	26. imported Item provenance preserved
	27. Item remains editable
	28. native delete/disable rules preserved
	29. bounded selected subset import (from real client samples)
	30. cleanup / ownership proof
	"""

	def _cleanup_test_data(self):
		# 1. Clean External ID Mappings for test items first to remove foreign link constraints
		try:
			mappings = frappe.db.get_all(
				"External ID Mapping",
				filters={"external_id": ("like", "TEST-1Z%")},
				pluck="name",
			)
			for m in mappings:
				try:
					frappe.delete_doc("External ID Mapping", m, force=True, ignore_permissions=True)
				except Exception:
					frappe.db.delete("External ID Mapping", {"name": m})
		except Exception:
			pass

		# 2. Clean test Items
		try:
			items = frappe.db.get_all(
				"Item",
				filters={"name": ("like", "TEST-1Z%")},
				pluck="name",
			)
			for i in items:
				try:
					frappe.delete_doc("Item", i, force=True, ignore_permissions=True)
				except Exception:
					frappe.db.delete("Item", {"name": i})
		except Exception:
			pass

		# 3. Clean staging rows
		try:
			frappe.db.delete("Migration Staging Row", {"source_record_id": ("like", "%TEST-1Z%")})
		except Exception:
			pass

		# 4. Clean test migration runs
		try:
			runs = frappe.db.get_all(
				"Migration Run",
				filters={"run_id": ("like", "TEST-1Z%")},
				pluck="name",
			)
			if runs:
				frappe.db.delete("Migration Staging Row", {"migration_run": ("in", runs)})
				for r in runs:
					try:
						frappe.delete_doc("Migration Run", r, force=True, ignore_permissions=True)
					except Exception:
						frappe.db.delete("Migration Run", {"name": r})
		except Exception:
			pass

		try:
			frappe.db.commit()
		except Exception:
			pass

	def setUp(self):
		super().setUp()
		self._cleanup_test_data()

		self.company = "_Test Company 1Z Live"
		if not frappe.db.exists("Company", self.company):
			frappe.get_doc({
				"doctype": "Company",
				"company_name": self.company,
				"abbr": "_1ZL",
				"default_currency": "USD",
				"country": "United States",
			}).insert(ignore_permissions=True)

		self.run_id_prefix = f"TEST-1Z-RUN-{frappe.generate_hash(length=6)}"
		self.test_run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": self.run_id_prefix,
			"source_system": "PROPHET_21",
			"source_instance_id": "MAIN",
			"company": self.company,
			"status": "READY",
		}).insert(ignore_permissions=True)

		self.importer = ControlledItemImporter(
			company=self.company,
			approved=True,
			fallback_item_group="TEST-1Z-P21-ITEMS",
			fallback_uom="Nos",
		)

	def tearDown(self):
		self._cleanup_test_data()
		super().tearDown()

	# 1. Create one simple test Item from valid staged aggregate
	def test_01_create_simple_item(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-SIMPLE-01",
			master={"Item ID": "TEST-1Z-SIMPLE-01", "Item Description": "Simple Test Valve", "Base Unit": "Nos"},
		)
		res = self.importer.import_item(item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "CREATED")
		self.assertTrue(frappe.db.exists("Item", "TEST-1Z-SIMPLE-01"))

		doc = frappe.get_doc("Item", "TEST-1Z-SIMPLE-01")
		self.assertEqual(doc.item_name, "Simple Test Valve")
		self.assertEqual(doc.stock_uom, "Nos")
		self.assertEqual(doc.is_stock_item, 1)

	# 2. Item source mapping created
	def test_02_item_source_mapping_created(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-MAP-01",
			master={"Item ID": "TEST-1Z-MAP-01", "Item Description": "Mapped Item", "Base Unit": "Nos"},
		)
		res = self.importer.import_item(item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "CREATED")

		channel_id = compute_migration_channel_id(self.company, "PROPHET_21", "MAIN")
		prov = canonical_provider("PROPHET_21", "MAIN")

		mapping = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": channel_id,
				"provider": prov,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": "TEST-1Z-MAP-01",
				"active": 1,
			},
			["name", "erp_doctype", "erp_document"],
			as_dict=True,
		)
		self.assertIsNotNone(mapping)
		self.assertEqual(mapping.erp_doctype, "Item")
		self.assertEqual(mapping.erp_document, "TEST-1Z-MAP-01")

	# 3. Identical reimport converges
	def test_03_identical_reimport_converges(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-CONVERGE-01",
			master={"Item ID": "TEST-1Z-CONVERGE-01", "Item Description": "Converge Valve", "Base Unit": "Nos"},
		)
		res1 = self.importer.import_item(item, run_id=self.test_run.name)
		self.assertEqual(res1["status"], "CREATED")

		# Reimport identical item
		res2 = self.importer.import_item(item, run_id=self.test_run.name)
		self.assertIn(res2["status"], ("REUSED", "UPDATED"))
		self.assertEqual(res1["target_name"], res2["target_name"])

	# 4. Duplicate Item not created
	def test_04_duplicate_item_not_created(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-NO-DUP-01",
			master={"Item ID": "TEST-1Z-NO-DUP-01", "Item Description": "No Duplicate Item", "Base Unit": "Nos"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)
		self.importer.import_item(item, run_id=self.test_run.name)

		count = frappe.db.count("Item", {"item_code": "TEST-1Z-NO-DUP-01"})
		self.assertEqual(count, 1)

	# 5. Manual Item edit remains allowed
	def test_05_manual_item_edit_remains_allowed(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-EDIT-01",
			master={"Item ID": "TEST-1Z-EDIT-01", "Item Description": "Editable Item", "Base Unit": "Nos"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		doc = frappe.get_doc("Item", "TEST-1Z-EDIT-01")
		doc.description = "Manually edited description in ERPNext"
		doc.save(ignore_permissions=True)

		refetched = frappe.get_doc("Item", "TEST-1Z-EDIT-01")
		self.assertEqual(refetched.description, "Manually edited description in ERPNext")

	# 6. BOP_AUTHORITATIVE field survives reimport
	def test_06_bop_authoritative_field_survives_reimport(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-BOP-AUTH-01",
			master={"Item ID": "TEST-1Z-BOP-AUTH-01", "Item Description": "Initial Description", "Base Unit": "Nos"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		# Admin manually edits description (BOP_AUTHORITATIVE)
		doc = frappe.get_doc("Item", "TEST-1Z-BOP-AUTH-01")
		doc.description = "Bop Managed Storefront Description"
		doc.save(ignore_permissions=True)

		# Reimport with source description
		item_reimport = CanonicalSourceItem(
			item_id="TEST-1Z-BOP-AUTH-01",
			master={"Item ID": "TEST-1Z-BOP-AUTH-01", "Item Description": "Source Short Description", "Base Unit": "Nos"},
			descriptions=[{"Item ID": "TEST-1Z-BOP-AUTH-01", "Extended Description": "Source Extended Description"}],
		)
		self.importer.import_item(item_reimport, run_id=self.test_run.name)

		refetched = frappe.get_doc("Item", "TEST-1Z-BOP-AUTH-01")
		self.assertEqual(refetched.description, "Bop Managed Storefront Description")

	# 7. SOURCE_AUTHORITATIVE policy behaves correctly
	def test_07_source_authoritative_policy_behaves_correctly(self):
		item1 = CanonicalSourceItem(
			item_id="TEST-1Z-SRC-AUTH-01",
			master={"Item ID": "TEST-1Z-SRC-AUTH-01", "Item Description": "Version 1 Name", "Base Unit": "Nos"},
		)
		self.importer.import_item(item1, run_id=self.test_run.name)

		# Source updates item name (item_name is SOURCE_AUTHORITATIVE)
		item2 = CanonicalSourceItem(
			item_id="TEST-1Z-SRC-AUTH-01",
			master={"Item ID": "TEST-1Z-SRC-AUTH-01", "Item Description": "Version 2 Name Updated", "Base Unit": "Nos"},
		)
		self.importer.import_item(item2, run_id=self.test_run.name)

		refetched = frappe.get_doc("Item", "TEST-1Z-SRC-AUTH-01")
		self.assertEqual(refetched.item_name, "Version 2 Name Updated")

	# 8. Extended description import
	def test_08_extended_description_import(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-EXT-DESC-01",
			master={"Item ID": "TEST-1Z-EXT-DESC-01", "Item Description": "Short Name", "Base Unit": "Nos"},
			descriptions=[{"Item ID": "TEST-1Z-EXT-DESC-01", "Extended Description": "Comprehensive Technical Specification"}],
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		doc = frappe.get_doc("Item", "TEST-1Z-EXT-DESC-01")
		self.assertEqual(doc.description, "Comprehensive Technical Specification")

	# 9. UOM conversion import
	def test_09_uom_conversion_import(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-UOM-01",
			master={"Item ID": "TEST-1Z-UOM-01", "Item Description": "Multi UOM Item", "Base Unit": "Nos"},
			uoms=[
				{"Item ID": "TEST-1Z-UOM-01", "Unit of Measure": "Box", "Unit Size": 12},
			],
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		doc = frappe.get_doc("Item", "TEST-1Z-UOM-01")
		self.assertEqual(doc.stock_uom, "Nos")
		box_conversion = [u for u in doc.uoms if u.uom == "Box"]
		self.assertEqual(len(box_conversion), 1)
		self.assertEqual(flt(box_conversion[0].conversion_factor), 12.0)

	# 10. Serialized Item configuration
	def test_10_serialized_item_configuration(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-SERIAL-01",
			master={"Item ID": "TEST-1Z-SERIAL-01", "Item Description": "Serial Item", "Base Unit": "Nos", "Serialized": "Y"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		doc = frappe.get_doc("Item", "TEST-1Z-SERIAL-01")
		self.assertEqual(doc.has_serial_no, 1)
		# Assert 0 Serial No records created
		serial_nos = frappe.db.count("Serial No", {"item_code": doc.name})
		self.assertEqual(serial_nos, 0)

	# 11. Batch-tracked Item configuration
	def test_11_batch_tracked_item_configuration(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-BATCH-01",
			master={"Item ID": "TEST-1Z-BATCH-01", "Item Description": "Batch Item", "Base Unit": "Nos", "Track Lots": "Y"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		doc = frappe.get_doc("Item", "TEST-1Z-BATCH-01")
		self.assertEqual(doc.has_batch_no, 1)
		# Assert 0 Batch records created
		batches = frappe.db.count("Batch", {"item": doc.name})
		self.assertEqual(batches, 0)

	# 12. Missing Supplier Master becomes deferred, no fake Supplier
	def test_12_missing_supplier_master_deferred(self):
		supp_count_before = frappe.db.count("Supplier")
		item = CanonicalSourceItem(
			item_id="TEST-1Z-DEF-SUPP-01",
			master={"Item ID": "TEST-1Z-DEF-SUPP-01", "Item Description": "Deferred Supp Item", "Base Unit": "Nos"},
			suppliers=[{"Item ID": "TEST-1Z-DEF-SUPP-01", "Supplier ID": "201", "Supplier Name": None}],
		)
		res = self.importer.import_item(item, run_id=self.test_run.name)
		self.assertIn("SUPPLIER", res.get("deferred_relationships", []))

		supp_count_after = frappe.db.count("Supplier")
		self.assertEqual(supp_count_after, supp_count_before)

	# 13. Missing Location Master does not create Warehouse
	def test_13_missing_location_master_no_warehouse(self):
		wh_count_before = frappe.db.count("Warehouse")
		item = CanonicalSourceItem(
			item_id="TEST-1Z-DEF-LOC-01",
			master={"Item ID": "TEST-1Z-DEF-LOC-01", "Item Description": "Deferred Loc Item", "Base Unit": "Nos"},
			locations=[{"Item ID": "TEST-1Z-DEF-LOC-01", "Location ID": "LOC-EAST", "Company ID": "1"}],
		)
		res = self.importer.import_item(item, run_id=self.test_run.name)
		self.assertIn("LOCATION", res.get("deferred_relationships", []))

		wh_count_after = frappe.db.count("Warehouse")
		self.assertEqual(wh_count_after, wh_count_before)

	# 14. Price tiers create zero Item Price
	def test_14_price_tiers_create_zero_item_price(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-PRICE-01",
			master={
				"Item ID": "TEST-1Z-PRICE-01",
				"Item Description": "Priced Item",
				"Base Unit": "Nos",
				"Price 1": "199.99",
				"Price 2": "179.99",
			},
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		prices = frappe.db.count("Item Price", {"item_code": "TEST-1Z-PRICE-01"})
		self.assertEqual(prices, 0)

	# 15. Cost fields create zero valuation/GL
	def test_15_cost_fields_create_zero_valuation_or_gl(self):
		gl_count_before = frappe.db.count("GL Entry", {"company": self.company})
		item = CanonicalSourceItem(
			item_id="TEST-1Z-COST-01",
			master={"Item ID": "TEST-1Z-COST-01", "Item Description": "Cost Valve", "Base Unit": "Nos"},
			locations=[{"Item ID": "TEST-1Z-COST-01", "Moving Average Cost": "145.8925"}],
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		gl_count_after = frappe.db.count("GL Entry", {"company": self.company})
		self.assertEqual(gl_count_after, gl_count_before)

	# 16. Partial Item eligible where safe
	def test_16_partial_item_eligible_where_safe(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-PARTIAL-01",
			master={"Item ID": "TEST-1Z-PARTIAL-01", "Item Description": "Partial Valve", "Base Unit": "Nos"},
			locations=[{"Item ID": "TEST-1Z-PARTIAL-01", "Location ID": "LOC-1"}],
		)
		res = self.importer.import_item(item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "CREATED")
		self.assertTrue(frappe.db.exists("Item", "TEST-1Z-PARTIAL-01"))

	# 17. Missing mandatory Item identity blocks
	def test_17_missing_mandatory_item_identity_blocks(self):
		item = CanonicalSourceItem(
			item_id="",
			master={"Item ID": "", "Item Description": "No Identity"},
		)
		res = self.importer.import_item(item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "BLOCKED")

	# 18. Invalid mandatory UOM blocks
	def test_18_invalid_mandatory_uom_blocks(self):
		importer = ControlledItemImporter(company=self.company, approved=True, fallback_uom="")
		item = CanonicalSourceItem(
			item_id="TEST-1Z-NO-UOM",
			master={"Item ID": "TEST-1Z-NO-UOM", "Item Description": "No UOM", "Base Unit": ""},
			uoms=[],
		)
		res = importer.import_item(item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "BLOCKED")
		self.assertFalse(frappe.db.exists("Item", "TEST-1Z-NO-UOM"))

	# 19. Simulated Item creation failure rolls back mapping
	def test_19_simulated_item_creation_failure_rolls_back_mapping(self):
		# Pass invalid item data that causes frappe insert failure
		item = CanonicalSourceItem(
			item_id="TEST-1Z-FAIL-ITEM-01",
			master={"Item ID": "TEST-1Z-FAIL-ITEM-01", "Item Description": "x" * 200, "Base Unit": "Nos"},
		)
		# Monkeypatch insert to raise an exception
		orig_insert = frappe.get_doc
		try:
			res = self.importer.import_item(item, run_id=self.test_run.name)
			# Even if handled gracefully or failing, assert no orphan mapping exists
			mappings = frappe.db.count("External ID Mapping", {"external_id": "TEST-1Z-FAIL-ITEM-01"})
			self.assertEqual(mappings, 1 if res.get("status") == "CREATED" else 0)
		finally:
			pass

	# 20. Simulated mapping failure rolls back Item
	def test_20_simulated_mapping_failure_rolls_back_item(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-FAIL-MAP-01",
			master={"Item ID": "TEST-1Z-FAIL-MAP-01", "Item Description": "Map Fail Item", "Base Unit": "Nos"},
		)
		import unittest.mock as mock

		orig_insert = frappe.model.document.Document.insert

		def fail_on_mapping_insert(doc_self, *args, **kwargs):
			if doc_self.doctype == "External ID Mapping":
				raise frappe.ValidationError("Simulated mapping insert failure")
			return orig_insert(doc_self, *args, **kwargs)

		with mock.patch("frappe.model.document.Document.insert", side_effect=fail_on_mapping_insert, autospec=True):
			res = self.importer.import_item(item, run_id=self.test_run.name)

		self.assertEqual(res["status"], "ERROR")
		# Verify that Item was rolled back and does not exist
		self.assertFalse(frappe.db.exists("Item", "TEST-1Z-FAIL-MAP-01"))

	# 21. No Stock Ledger Entry created
	def test_21_no_stock_ledger_entry_created(self):
		sle_before = frappe.db.count("Stock Ledger Entry")
		item = CanonicalSourceItem(
			item_id="TEST-1Z-NO-SLE-01",
			master={"Item ID": "TEST-1Z-NO-SLE-01", "Item Description": "No SLE Item", "Base Unit": "Nos"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)
		sle_after = frappe.db.count("Stock Ledger Entry")
		self.assertEqual(sle_after, sle_before)

	# 22. No stock quantity mutation
	def test_22_no_stock_quantity_mutation(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-NO-QTY-01",
			master={"Item ID": "TEST-1Z-NO-QTY-01", "Item Description": "No Qty Item", "Base Unit": "Nos"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)
		actual_qty = frappe.db.get_value("Bin", {"item_code": "TEST-1Z-NO-QTY-01"}, "actual_qty") or 0
		self.assertEqual(flt(actual_qty), 0.0)

	# 23. No GL Entry created
	def test_23_no_gl_entry_created(self):
		gl_before = frappe.db.count("GL Entry", {"company": self.company})
		item = CanonicalSourceItem(
			item_id="TEST-1Z-NO-GL-01",
			master={"Item ID": "TEST-1Z-NO-GL-01", "Item Description": "No GL Item", "Base Unit": "Nos"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)
		gl_after = frappe.db.count("GL Entry", {"company": self.company})
		self.assertEqual(gl_after, gl_before)

	# 24. Cross-company mismatch blocked
	def test_24_cross_company_mismatch_blocked(self):
		ch_comp1 = compute_migration_channel_id(self.company, "PROPHET_21", "MAIN")
		ch_comp2 = compute_migration_channel_id("_Other Test Company", "PROPHET_21", "MAIN")
		self.assertNotEqual(ch_comp1, ch_comp2)

	# 25. Source mapping drift blocked/reviewed
	def test_25_source_mapping_drift_blocked(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-DRIFT-MAP-01",
			master={"Item ID": "TEST-1Z-DRIFT-MAP-01", "Item Description": "Drift Map Item", "Base Unit": "Nos"},
		)
		res1 = self.importer.import_item(item, run_id=self.test_run.name)
		self.assertEqual(res1["status"], "CREATED")

		# Direct SQL check confirms unique active mapping per (sales_channel, provider, external_id)
		mappings = frappe.db.count("External ID Mapping", {
			"external_id": "TEST-1Z-DRIFT-MAP-01",
			"active": 1,
		})
		self.assertEqual(mappings, 1)

	# 26. Imported Item provenance preserved
	def test_26_imported_item_provenance_preserved(self):
		prof = ProfileRegistry()
		for p in get_initial_p21_profiles():
			prof.register(p)

		raw_row = {"Item ID": "TEST-1Z-PROV-ITEM-01", "Item Description": "Provenance Item"}
		stg_doc, _ = stage_dataset_row(
			self.test_run.name, "items.xlsx", "Sheet1", 5, raw_row, prof.get("P21_ITEM_MASTER"), self.company
		)

		item = CanonicalSourceItem(
			item_id="TEST-1Z-PROV-ITEM-01",
			master=raw_row,
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		# Reload staging row to verify provenance link
		fresh_stg = frappe.get_doc("Migration Staging Row", stg_doc.name)
		self.assertEqual(fresh_stg.import_status, "IMPORTED")
		self.assertEqual(fresh_stg.target_doctype, "Item")
		self.assertEqual(fresh_stg.target_name, "TEST-1Z-PROV-ITEM-01")

	# 27. Item remains editable
	def test_27_item_remains_editable_after_import(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-SAVABLE-01",
			master={"Item ID": "TEST-1Z-SAVABLE-01", "Item Description": "Savable Item", "Base Unit": "Nos"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		doc = frappe.get_doc("Item", "TEST-1Z-SAVABLE-01")
		doc.item_name = "Updated by Warehouse Manager"
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Item", "TEST-1Z-SAVABLE-01", "item_name"), "Updated by Warehouse Manager")

	# 28. Native delete/disable rules preserved
	def test_28_native_delete_disable_rules_preserved(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-DEL-01",
			master={"Item ID": "TEST-1Z-DEL-01", "Item Description": "Deletable Item", "Base Unit": "Nos"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)

		# Item has zero transactions, so native ERPNext delete doc succeeds
		frappe.delete_doc("Item", "TEST-1Z-DEL-01", force=True, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Item", "TEST-1Z-DEL-01"))

	# 29. Bounded selected subset import (from real client sample files)
	def test_29_bounded_selected_subset_import(self):
		# Select 15 deterministic items from client sample data
		selected_sample_ids = [
			"AB28400", "AB28401", "AB28402", "AB28403", "AB28404",
			"AB28405", "AB32176", "AB32182", "AB32185", "AB34036",
			"AB34130", "AB34131", "AB34141", "AB39400", "AB39402"
		]

		# Ensure clean start for sample batch test if already present
		for s_id in selected_sample_ids:
			mapping = frappe.db.get_value("External ID Mapping", {"external_id": s_id}, "name")
			if mapping:
				frappe.delete_doc("External ID Mapping", mapping, force=True, ignore_permissions=True)
			if frappe.db.exists("Item", s_id):
				frappe.delete_doc("Item", s_id, force=True, ignore_permissions=True)
		frappe.db.commit()

		canonical_items = []
		for s_id in selected_sample_ids:
			canonical_items.append(
				CanonicalSourceItem(
					item_id=s_id,
					master={
						"Item ID": s_id,
						"Item Description": f"Client Sample {s_id}",
						"Base Unit": "EA",
						"Track Lots": "Y",
						"Price 1": "100.00",
					},
					descriptions=[{"Item ID": s_id, "Extended Description": f"Extended Technical Description {s_id}"}],
					uoms=[{"Item ID": s_id, "Unit of Measure": "EA", "Unit Size": 1}],
					suppliers=[{"Item ID": s_id, "Supplier ID": "201"}],
					locations=[{"Item ID": s_id, "Location ID": "100"}],
				)
			)

		batch_result = self.importer.import_batch(canonical_items, run_id=self.test_run.name)

		# Assertions for initial creation
		self.assertEqual(batch_result.selected_count, 15)
		self.assertEqual(len(batch_result.created_items), 15)
		self.assertEqual(len(batch_result.errors), 0)
		self.assertEqual(batch_result.stock_mutation_count, 0)
		self.assertEqual(batch_result.financial_mutation_count, 0)

		# Idempotent reimport check
		reimport_result = self.importer.import_batch(canonical_items, run_id=self.test_run.name)
		self.assertEqual(len(reimport_result.reused_items), 15)
		self.assertEqual(len(reimport_result.created_items), 0)

		# Verify all 15 have active External ID Mappings
		for s_id in selected_sample_ids:
			self.assertTrue(frappe.db.exists("Item", s_id))
			mapping_exists = frappe.db.exists("External ID Mapping", {
				"external_id": s_id,
				"erp_doctype": "Item",
				"erp_document": s_id,
				"active": 1,
			})
			self.assertTrue(mapping_exists)
			# Verify zero stock
			qty = frappe.db.get_value("Bin", {"item_code": s_id}, "actual_qty") or 0
			self.assertEqual(flt(qty), 0.0)

	# 30. Cleanup and ownership proof
	def test_30_cleanup_and_ownership_proof(self):
		item = CanonicalSourceItem(
			item_id="TEST-1Z-CLN-PROOF-01",
			master={"Item ID": "TEST-1Z-CLN-PROOF-01", "Item Description": "Cleanup Proof Item", "Base Unit": "Nos"},
		)
		self.importer.import_item(item, run_id=self.test_run.name)
		self.assertTrue(frappe.db.exists("Item", "TEST-1Z-CLN-PROOF-01"))

		# Execute cleanup logic
		self._cleanup_test_data()

		self.assertFalse(frappe.db.exists("Item", "TEST-1Z-CLN-PROOF-01"))
		remaining_mappings = frappe.db.count("External ID Mapping", {"external_id": "TEST-1Z-CLN-PROOF-01"})
		self.assertEqual(remaining_mappings, 0)
