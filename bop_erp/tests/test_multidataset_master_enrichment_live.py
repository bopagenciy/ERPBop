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
	CompletenessGatePolicy,
	ControlledItemEnricher,
	ControlledItemImporter,
	DescriptionAction,
	FieldAuthorityPolicy,
	ItemEnrichmentPreview,
	ItemEnrichmentResult,
	UOMAction,
	UOMMappingConfig,
	generate_enrichment_preview,
	get_initial_p21_profiles,
	parse_xlsx_stream,
	reconcile_dataset_completeness,
)
from bop_erp.migration.namespaces import canonical_provider, compute_migration_channel_id


class TestMultiDatasetMasterEnrichmentLive(FrappeTestCase):
	"""
	Phase 1Z.1 Live Test Suite: Controlled Multi-Dataset Master Data Enrichment.
	Executes real target database mutations on site 'frontend' within explicit approval boundary.
	Tests all 30 required scenarios from Section V:
	 1. description dataset resolves existing Item
	 2. UOM dataset resolves same existing Item
	 3. no duplicate Item created
	 4. existing External ID Mapping reused
	 5. source-owned description populated
	 6. identical description reimport no-op
	 7. manually edited Bop description survives reimport
	 8. changed source description routes according to authority
	 9. valid UOM conversion added
	10. identical UOM conversion reimport converges
	11. duplicate UOM child not created
	12. conflicting UOM factor blocked/reviewed
	13. manual Bop-only UOM row preserved
	14. missing description tolerated
	15. missing UOM tolerated when Item stock UOM valid
	16. source selling-unit flag handled safely
	17. source purchasing-unit flag handled safely
	18. stock UOM mismatch routes review
	19. no Supplier created
	20. no Warehouse created
	21. no Item Price created
	22. no SLE created
	23. no stock quantity mutation
	24. no GL Entry created
	25. description + UOM atomic rollback on simulated failure
	26. preview produces zero target mutation
	27. second full enrichment run converges
	28. provenance retained
	29. bounded 15-Item scope enforced
	30. cleanup/synthetic fixture ownership proof
	"""

	def _cleanup_test_data(self):
		# 1. Clean External ID Mappings for test items first
		try:
			sample_15 = [
				"AB28400", "AB28401", "AB28402", "AB28403", "AB28404",
				"AB28405", "AB28406", "AB28407", "AB28408", "AB28409",
				"AB28410", "AB28411", "AB28412", "AB28413", "AB39402"
			]
			mappings = frappe.db.get_all(
				"External ID Mapping",
				filters={"external_id": ("like", "TEST-1Z1%")},
				pluck="name",
			)
			for s_id in sample_15:
				mappings.extend(frappe.db.get_all("External ID Mapping", filters={"external_id": s_id}, pluck="name"))

			for m in set(mappings):
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
				filters={"name": ("like", "TEST-1Z1%")},
				pluck="name",
			)
			for s_id in sample_15:
				if frappe.db.exists("Item", s_id):
					items.append(s_id)

			for i in set(items):
				try:
					frappe.delete_doc("Item", i, force=True, ignore_permissions=True)
				except Exception:
					frappe.db.delete("Item", {"name": i})
		except Exception:
			pass

		# 3. Clean test Migration Runs and Staging Rows
		try:
			frappe.db.delete("Migration Staging Row", {"source_record_id": ("like", "%TEST-1Z1%")})
			runs = frappe.db.get_all("Migration Run", filters={"name": ("like", "%1Z1%")}, pluck="name")
			for r in runs:
				frappe.db.delete("Migration Run", {"name": r})
		except Exception:
			pass
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self.company = "_Test Company 1Z1 Live"
		if not frappe.db.exists("Company", self.company):
			co = frappe.get_doc({
				"doctype": "Company",
				"company_name": self.company,
				"abbr": "_1Z1",
				"default_currency": "USD",
				"country": "United States",
			})
			co.insert(ignore_permissions=True)
			frappe.db.commit()

		self._cleanup_test_data()

		# Ensure test migration run
		self.run_id_prefix = f"TEST-1Z1-RUN-{frappe.generate_hash(length=6)}"
		self.test_run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": self.run_id_prefix,
			"company": self.company,
			"source_system": "PROPHET_21",
			"source_instance_id": "TEST_INST",
			"status": "READY",
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Ensure test UOMs exist
		for u_name in ["Nos", "EA", "Box", "Case", "Pack"]:
			if not frappe.db.exists("UOM", u_name):
				u_doc = frappe.get_doc({"doctype": "UOM", "uom_name": u_name})
				u_doc.insert(ignore_permissions=True)
		frappe.db.commit()

		# Base importer & enricher
		self.importer = ControlledItemImporter(
			company=self.company,
			source_system="PROPHET_21",
			source_instance_id="TEST_INST",
			approved=True,
		)
		self.uom_config = UOMMappingConfig(
			mappings={"EA": "EA", "Nos": "Nos", "BOX": "Box", "CS": "Case", "PK": "Pack"},
			allow_exact_match=True,
		)
		self.enricher = ControlledItemEnricher(
			company=self.company,
			source_system="PROPHET_21",
			source_instance_id="TEST_INST",
			uom_config=self.uom_config,
			description_policy=FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
			uom_policy=FieldAuthorityPolicy.REVIEW_ON_CONFLICT,
		)

	def tearDown(self):
		self._cleanup_test_data()
		super().tearDown()

	def _create_base_item(self, item_id: str, desc: str = "Base Item", uom: str = "EA") -> str:
		"""Helper to create an initial item and mapping via ControlledItemImporter."""
		base_item = CanonicalSourceItem(
			item_id=item_id,
			master={"Item ID": item_id, "Item Description": desc, "Base Unit": uom},
		)
		res = self.importer.import_item(base_item, run_id=self.test_run.name)
		frappe.db.commit()
		return res["item_code"]

	# 1. Description dataset resolves existing Item
	def test_01_description_dataset_resolves_existing_item(self):
		item_id = "TEST-1Z1-RES-01"
		target_code = self._create_base_item(item_id, desc="Initial Short")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Enriched Extended Description 01"}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "UPDATED")
		self.assertEqual(res["description_action"], DescriptionAction.UPDATE.value)

		doc = frappe.get_doc("Item", target_code)
		self.assertEqual(doc.description, "Enriched Extended Description 01")

	# 2. UOM dataset resolves same existing Item
	def test_02_uom_dataset_resolves_same_existing_item(self):
		item_id = "TEST-1Z1-RES-02"
		target_code = self._create_base_item(item_id, desc="Base Item")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "UPDATED")

		doc = frappe.get_doc("Item", target_code)
		box_rows = [r for r in doc.uoms if r.uom == "Box"]
		self.assertEqual(len(box_rows), 1)
		self.assertEqual(flt(box_rows[0].conversion_factor), 10.0)

	# 3. No duplicate Item created
	def test_03_no_duplicate_item_created(self):
		item_id = "TEST-1Z1-NODUP-03"
		target_code = self._create_base_item(item_id, desc="Base Item")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Desc"}],
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)

		count = frappe.db.count("Item", {"name": ("like", f"{item_id}%")})
		self.assertEqual(count, 1)

	# 4. Existing External ID Mapping reused
	def test_04_existing_external_id_mapping_reused(self):
		item_id = "TEST-1Z1-MAP-04"
		target_code = self._create_base_item(item_id, desc="Base Item")

		initial_map_count = frappe.db.count("External ID Mapping", {"external_id": item_id})
		self.assertEqual(initial_map_count, 1)

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Desc"}],
		)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)

		post_map_count = frappe.db.count("External ID Mapping", {"external_id": item_id})
		self.assertEqual(post_map_count, 1)

	# 5. Source-owned description populated
	def test_05_source_owned_description_populated(self):
		item_id = "TEST-1Z1-DESC-05"
		target_code = self._create_base_item(item_id, desc="Raw Short")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "1/4 BALL VALVE PUSH-FIT"}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "UPDATED")
		self.assertEqual(frappe.db.get_value("Item", target_code, "description"), "1/4 BALL VALVE PUSH-FIT")

	# 6. Identical description reimport no-op
	def test_06_identical_description_reimport_noop(self):
		item_id = "TEST-1Z1-NOOP-06"
		target_code = self._create_base_item(item_id, desc="Raw Short")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Steady Description"}],
		)
		# First import
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		doc_before = frappe.get_doc("Item", target_code)

		# Second identical reimport
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "NOOP")
		self.assertEqual(res["description_action"], DescriptionAction.NOOP.value)

	# 7. Manually edited Bop description survives reimport
	def test_07_manually_edited_bop_description_survives_reimport(self):
		item_id = "TEST-1Z1-BOPAUTH-07"
		target_code = self._create_base_item(item_id, desc="Initial Desc")

		# Manually edit in ERPNext
		doc = frappe.get_doc("Item", target_code)
		doc.description = "Manual High-Quality Marketing Copy by Merchandising"
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		# Configure enricher with BOP_AUTHORITATIVE
		enricher = ControlledItemEnricher(
			company=self.company,
			source_system="PROPHET_21",
			source_instance_id="TEST_INST",
			description_policy=FieldAuthorityPolicy.BOP_AUTHORITATIVE,
		)
		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Incoming Raw ERP Description"}],
		)
		res = enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "NOOP")
		self.assertEqual(res["description_action"], DescriptionAction.PRESERVE_BOP.value)

		# Value preserved
		doc_after = frappe.get_doc("Item", target_code)
		self.assertEqual(doc_after.description, "Manual High-Quality Marketing Copy by Merchandising")

	# 8. Changed source description routes according to authority
	def test_08_changed_source_description_routes_according_to_authority(self):
		item_id = "TEST-1Z1-AUTHROUT-08"
		target_code = self._create_base_item(item_id, desc="Initial Desc")

		# Review on conflict policy
		enricher = ControlledItemEnricher(
			company=self.company,
			source_system="PROPHET_21",
			source_instance_id="TEST_INST",
			description_policy=FieldAuthorityPolicy.REVIEW_ON_CONFLICT,
		)
		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Changed Description"}],
		)
		res = enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["description_action"], DescriptionAction.CONFLICT.value)
		# Value unchanged
		self.assertEqual(frappe.db.get_value("Item", target_code, "description"), "Initial Desc")

	# 9. Valid UOM conversion added
	def test_09_valid_uom_conversion_added(self):
		item_id = "TEST-1Z1-UOMADD-09"
		target_code = self._create_base_item(item_id, uom="EA")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "CS", "Unit Size": 24.0}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "UPDATED")

		doc = frappe.get_doc("Item", target_code)
		cs_rows = [r for r in doc.uoms if r.uom == "Case"]
		self.assertEqual(len(cs_rows), 1)
		self.assertEqual(flt(cs_rows[0].conversion_factor), 24.0)

	# 10. Identical UOM conversion reimport converges
	def test_10_identical_uom_conversion_reimport_converges(self):
		item_id = "TEST-1Z1-UOMCONV-10"
		target_code = self._create_base_item(item_id, uom="EA")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)

		# Second reimport
		res2 = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res2["status"], "NOOP")
		self.assertEqual(res2["uom_actions"][0]["action"], UOMAction.NOOP.value)

	# 11. Duplicate UOM child not created
	def test_11_duplicate_uom_child_not_created(self):
		item_id = "TEST-1Z1-UOMNODUP-11"
		target_code = self._create_base_item(item_id, uom="EA")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)

		doc = frappe.get_doc("Item", target_code)
		box_rows = [r for r in doc.uoms if r.uom == "Box"]
		self.assertEqual(len(box_rows), 1)

	# 12. Conflicting UOM factor blocked/reviewed
	def test_12_conflicting_uom_factor_blocked_or_reviewed(self):
		item_id = "TEST-1Z1-UOMCONF-12"
		target_code = self._create_base_item(item_id, uom="EA")

		enrich_item_1 = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		self.enricher.enrich_item(enrich_item_1, run_id=self.test_run.name)

		# Reimport with conflicting factor 12.0
		enrich_item_2 = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 12.0}],
		)
		res = self.enricher.enrich_item(enrich_item_2, run_id=self.test_run.name)
		self.assertEqual(res["uom_actions"][0]["action"], UOMAction.CONFLICT.value)

		# Factor remains 10.0
		doc = frappe.get_doc("Item", target_code)
		box_row = [r for r in doc.uoms if r.uom == "Box"][0]
		self.assertEqual(flt(box_row.conversion_factor), 10.0)

	# 13. Manual Bop-only UOM row preserved
	def test_13_manual_bop_only_uom_row_preserved(self):
		item_id = "TEST-1Z1-MANUOM-13"
		target_code = self._create_base_item(item_id, uom="EA")

		# Manually add Pack row
		doc = frappe.get_doc("Item", target_code)
		doc.append("uoms", {"uom": "Pack", "conversion_factor": 5.0})
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		# Reimport with Box UOM
		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)

		doc_after = frappe.get_doc("Item", target_code)
		uoms = {r.uom: flt(r.conversion_factor) for r in doc_after.uoms}
		self.assertIn("Pack", uoms)
		self.assertEqual(uoms["Pack"], 5.0)
		self.assertIn("Box", uoms)
		self.assertEqual(uoms["Box"], 10.0)

	# 14. Missing description tolerated
	def test_14_missing_description_tolerated(self):
		item_id = "TEST-1Z1-MISSDESC-14"
		target_code = self._create_base_item(item_id, desc="Initial Desc")

		enrich_item = CanonicalSourceItem(item_id=item_id, descriptions=[])
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["description_action"], DescriptionAction.MISSING.value)
		self.assertEqual(res["status"], "NOOP")
		self.assertEqual(frappe.db.get_value("Item", target_code, "description"), "Initial Desc")

	# 15. Missing UOM tolerated when Item stock UOM valid
	def test_15_missing_uom_tolerated_when_stock_uom_valid(self):
		item_id = "TEST-1Z1-MISSUOM-15"
		target_code = self._create_base_item(item_id, uom="EA")

		enrich_item = CanonicalSourceItem(item_id=item_id, uoms=[])
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["uom_actions"], [])
		self.assertEqual(res["status"], "NOOP")
		self.assertEqual(frappe.db.get_value("Item", target_code, "stock_uom"), "EA")

	# 16. Source selling-unit flag handled safely
	def test_16_source_selling_unit_flag_handled_safely(self):
		item_id = "TEST-1Z1-SELLUOM-16"
		target_code = self._create_base_item(item_id, uom="EA")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0, "Selling Unit": "Y"}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "UPDATED")
		doc = frappe.get_doc("Item", target_code)
		self.assertEqual(doc.sales_uom, "Box")

	# 17. Source purchasing-unit flag handled safely
	def test_17_source_purchasing_unit_flag_handled_safely(self):
		item_id = "TEST-1Z1-PURCHUOM-17"
		target_code = self._create_base_item(item_id, uom="EA")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "CS", "Unit Size": 24.0, "Purchasing Unit": "Y"}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["status"], "UPDATED")
		doc = frappe.get_doc("Item", target_code)
		self.assertEqual(doc.purchase_uom, "Case")

	# 18. Stock UOM mismatch routes review
	def test_18_stock_uom_mismatch_routes_review(self):
		item_id = "TEST-1Z1-STOCKMIS-18"
		target_code = self._create_base_item(item_id, uom="EA")

		# Incoming factor 2.0 for stock UOM EA
		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			uoms=[{"Item ID": item_id, "Unit of Measure": "EA", "Unit Size": 2.0}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["uom_actions"][0]["action"], UOMAction.CONFLICT.value)
		self.assertEqual(res["status"], "NOOP")

	# 19. No Supplier created
	def test_19_no_supplier_created(self):
		item_id = "TEST-1Z1-NOSUPP-19"
		target_code = self._create_base_item(item_id)
		supp_count_before = frappe.db.count("Supplier")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			suppliers=[{"Supplier ID": "SUPP-999"}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertIn("INVENTORY_SUPPLIER", res["deferred_relationships"])
		self.assertEqual(frappe.db.count("Supplier"), supp_count_before)

	# 20. No Warehouse created
	def test_20_no_warehouse_created(self):
		item_id = "TEST-1Z1-NOWH-20"
		target_code = self._create_base_item(item_id)
		wh_count_before = frappe.db.count("Warehouse")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			locations=[{"Location ID": "LOC-999"}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertIn("INVENTORY_LOCATION", res["deferred_relationships"])
		self.assertEqual(frappe.db.count("Warehouse"), wh_count_before)

	# 21. No Item Price created
	def test_21_no_item_price_created(self):
		item_id = "TEST-1Z1-NOPRICE-21"
		target_code = self._create_base_item(item_id)
		price_count_before = frappe.db.count("Item Price")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Desc"}],
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(frappe.db.count("Item Price"), price_count_before)

	# 22. No SLE created
	def test_22_no_sle_created(self):
		item_id = "TEST-1Z1-NOSLE-22"
		target_code = self._create_base_item(item_id)
		sle_count_before = frappe.db.count("Stock Ledger Entry")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Desc"}],
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), sle_count_before)

	# 23. No stock quantity mutation
	def test_23_no_stock_quantity_mutation(self):
		item_id = "TEST-1Z1-NOBIN-23"
		target_code = self._create_base_item(item_id)
		non_zero_bins_before = frappe.db.count("Bin", {"actual_qty": (">", 0)})

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Desc"}],
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(frappe.db.count("Bin", {"actual_qty": (">", 0)}), non_zero_bins_before)

	# 24. No GL Entry created
	def test_24_no_gl_entry_created(self):
		item_id = "TEST-1Z1-NOGL-24"
		target_code = self._create_base_item(item_id)
		gl_count_before = frappe.db.count("GL Entry")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Desc"}],
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(frappe.db.count("GL Entry"), gl_count_before)

	# 25. Description + UOM atomic rollback on simulated failure
	def test_25_description_and_uom_atomic_rollback_on_failure(self):
		item_id = "TEST-1Z1-ROLLBACK-25"
		target_code = self._create_base_item(item_id, desc="Original Stable Description")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Volatile Description"}],
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)

		from unittest.mock import patch
		# Simulate a failure during save
		with patch("frappe.model.document.Document.save", side_effect=RuntimeError("Simulated DB Crash")):
			with self.assertRaises(RuntimeError):
				self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)

		# Rollback verified: original description intact, no Box UOM row
		doc = frappe.get_doc("Item", target_code)
		self.assertEqual(doc.description, "Original Stable Description")
		box_rows = [r for r in doc.uoms if r.uom == "Box"]
		self.assertEqual(len(box_rows), 0)

	# 26. Preview produces zero target mutation
	def test_26_preview_produces_zero_target_mutation(self):
		item_id = "TEST-1Z1-PREVIEW-26"
		target_code = self._create_base_item(item_id, desc="Old Short")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Brand New Extended"}],
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)

		preview = self.enricher.preview([enrich_item])
		self.assertEqual(preview.summary["to_update"], 1)

		# Verify DB state has ZERO mutations
		doc = frappe.get_doc("Item", target_code)
		self.assertEqual(doc.description, "Old Short")
		box_rows = [r for r in doc.uoms if r.uom == "Box"]
		self.assertEqual(len(box_rows), 0)

	# 27. Second full enrichment run converges
	def test_27_second_full_enrichment_run_converges(self):
		item_id = "TEST-1Z1-CONVERGE-27"
		target_code = self._create_base_item(item_id, desc="Initial Short")

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Final Enriched Description"}],
			uoms=[{"Item ID": item_id, "Unit of Measure": "BOX", "Unit Size": 10.0}],
		)

		# Run 1: Mutates
		batch_res_1 = self.enricher.enrich_batch([enrich_item], run_id=self.test_run.name)
		self.assertEqual(len(batch_res_1.items_updated), 1)

		# Run 2: Completely converges (0 updates, all noop)
		batch_res_2 = self.enricher.enrich_batch([enrich_item], run_id=self.test_run.name)
		self.assertEqual(len(batch_res_2.items_updated), 0)
		self.assertEqual(len(batch_res_2.items_noop), 1)
		self.assertEqual(batch_res_2.uom_rows_added, 0)
		self.assertEqual(batch_res_2.descriptions_updated, 0)

	# 28. Provenance retained
	def test_28_provenance_retained(self):
		item_id = "TEST-1Z1-PROV-28"
		target_code = self._create_base_item(item_id)

		enrich_item = CanonicalSourceItem(
			item_id=item_id,
			descriptions=[{"Item ID": item_id, "Extended Description": "Desc"}],
		)
		res = self.enricher.enrich_item(enrich_item, run_id=self.test_run.name)
		self.assertEqual(res["item_id"], item_id)
		self.assertEqual(res["target_code"], target_code)
		self.assertIn("description", res["modified_fields"])

	# 29. Bounded 15-Item scope enforced (using real client sample files)
	def test_29_bounded_15_item_scope_enforced(self):
		sample_dir = Path(frappe.get_app_path("bop_erp", "..", "local_data", "p21_samples")).resolve()
		profiles = {p.profile_id: p for p in get_initial_p21_profiles()}

		selected_15_ids = [
			"AB28400", "AB28401", "AB28402", "AB28403", "AB28404",
			"AB28405", "AB28406", "AB28407", "AB28408", "AB28409",
			"AB28410", "AB28411", "AB28412", "AB28413", "AB39402"
		]

		# 1. Parse sample files for the 15 items
		master_data = {}
		for _, row in parse_xlsx_stream(sample_dir / "1ItemMaster_sample.xlsx", profiles["P21_ITEM_MASTER"]):
			iid = str(row.get("Item ID", "")).strip()
			if iid in selected_15_ids:
				master_data[iid] = row

		desc_data = {}
		for _, row in parse_xlsx_stream(sample_dir / "5ItemDescription_sample.xlsx", profiles["P21_ITEM_DESCRIPTION"]):
			iid = str(row.get("Item ID", "")).strip()
			if iid in selected_15_ids:
				desc_data[iid] = row

		uom_data = {}
		for _, row in parse_xlsx_stream(sample_dir / "4ItemUnitofMeasure_sample.xlsx", profiles["P21_ITEM_UOM"]):
			iid = str(row.get("Item ID", "")).strip()
			if iid in selected_15_ids:
				uom_data.setdefault(iid, []).append(row)

		# Verify exact matched vs missing counts
		self.assertEqual(len(desc_data), 7)
		self.assertEqual(len(uom_data), 7)
		missing_desc_ids = set(selected_15_ids) - set(desc_data.keys())
		missing_uom_ids = set(selected_15_ids) - set(uom_data.keys())
		self.assertEqual(len(missing_desc_ids), 8)
		self.assertEqual(len(missing_uom_ids), 8)

		# 2. Ensure all 15 base items exist via ControlledItemImporter
		for s_id in selected_15_ids:
			m_row = master_data.get(s_id, {"Item ID": s_id, "Item Description": f"Item {s_id}", "Base Unit": "EA"})
			canonical_base = CanonicalSourceItem(item_id=s_id, master=m_row)
			self.importer.import_item(canonical_base, run_id=self.test_run.name)
		frappe.db.commit()

		# Capture baseline counts before enrichment
		item_count_before = frappe.db.count("Item")
		map_count_before = frappe.db.count("External ID Mapping")
		price_count_before = frappe.db.count("Item Price")
		supp_count_before = frappe.db.count("Supplier")
		wh_count_before = frappe.db.count("Warehouse")
		sle_count_before = frappe.db.count("Stock Ledger Entry")
		bin_qty_before = frappe.db.count("Bin", {"actual_qty": (">", 0)})
		recon_count_before = frappe.db.count("Stock Reconciliation")
		gl_count_before = frappe.db.count("GL Entry")

		# 3. Build CanonicalSourceItem aggregates with description and UOM data
		canonical_items = []
		for s_id in selected_15_ids:
			m_row = master_data.get(s_id, {"Item ID": s_id, "Item Description": f"Item {s_id}", "Base Unit": "EA"})
			d_rows = [desc_data[s_id]] if s_id in desc_data else []
			u_rows = uom_data.get(s_id, [])
			canonical_items.append(
				CanonicalSourceItem(
					item_id=s_id,
					master=m_row,
					descriptions=d_rows,
					uoms=u_rows,
					suppliers=[{"Supplier ID": "201"}],
					locations=[{"Location ID": "100"}],
				)
			)

		# 4. Preview before mutation
		preview = self.enricher.preview(canonical_items)
		self.assertEqual(len(preview.selected_items), 15)
		self.assertEqual(preview.summary["missing_description"], 8)
		self.assertEqual(preview.summary["missing_uom"], 8)

		# 5. Apply enrichment
		batch_res = self.enricher.enrich_batch(canonical_items, run_id=self.test_run.name)
		frappe.db.commit()

		# 6. Verify zero forbidden mutations
		self.assertEqual(frappe.db.count("Item"), item_count_before)
		self.assertEqual(frappe.db.count("External ID Mapping"), map_count_before)
		self.assertEqual(frappe.db.count("Item Price"), price_count_before)
		self.assertEqual(frappe.db.count("Supplier"), supp_count_before)
		self.assertEqual(frappe.db.count("Warehouse"), wh_count_before)
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), sle_count_before)
		self.assertEqual(frappe.db.count("Bin", {"actual_qty": (">", 0)}), bin_qty_before)
		self.assertEqual(frappe.db.count("Stock Reconciliation"), recon_count_before)
		self.assertEqual(frappe.db.count("GL Entry"), gl_count_before)

		# 7. Check that matched items got enriched descriptions
		doc_ab28400 = frappe.get_doc("Item", "AB28400")
		self.assertEqual(doc_ab28400.description, "1/8BALL VALVE PUSH-FIT CONNECT")

		# 8. Check that missing items remained valid
		doc_ab28406 = frappe.get_doc("Item", "AB28406")
		self.assertTrue(doc_ab28406.name, "AB28406")
		self.assertEqual(doc_ab28406.stock_uom, "EA")

	# 30. Cleanup and synthetic fixture ownership proof
	def test_30_cleanup_and_synthetic_fixture_ownership(self):
		item_id = "TEST-1Z1-CLEAN-30"
		target_code = self._create_base_item(item_id)
		self.assertTrue(frappe.db.exists("Item", target_code))

		self._cleanup_test_data()
		self.assertFalse(frappe.db.exists("Item", target_code))
		self.assertFalse(frappe.db.exists("External ID Mapping", {"external_id": item_id}))

	# 31. Physical sample completeness reconciliation exact
	def test_31_physical_sample_completeness_reconciliation_exact(self):
		sample_dir = Path(frappe.get_app_path("bop_erp", "..", "local_data", "p21_samples")).resolve()
		if not sample_dir.exists():
			self.skipTest("p21_samples directory not found")

		profiles = {p.profile_id: p for p in get_initial_p21_profiles()}
		m_prof = profiles["P21_ITEM_MASTER"]
		d_prof = profiles["P21_ITEM_DESCRIPTION"]
		u_prof = profiles["P21_ITEM_UOM"]

		m_file = sample_dir / "1ItemMaster_sample.xlsx"
		d_file = sample_dir / "5ItemDescription_sample.xlsx"
		u_file = sample_dir / "4ItemUnitofMeasure_sample.xlsx"

		if not (m_file.exists() and d_file.exists() and u_file.exists()):
			self.skipTest("Sample files not present")

		m_items = {str(row["Item ID"]).strip() for _, row in parse_xlsx_stream(str(m_file), m_prof)}
		d_items = {str(row["Item ID"]).strip() for _, row in parse_xlsx_stream(str(d_file), d_prof)}
		u_items = {str(row["Item ID"]).strip() for _, row in parse_xlsx_stream(str(u_file), u_prof)}

		# Master has 26 rows, Desc has 25, UOM has 25
		self.assertEqual(len(m_items), 26)
		self.assertEqual(len(d_items), 25)
		self.assertEqual(len(u_items), 25)

		selected_15 = [
			"AB28400", "AB28401", "AB28402", "AB28403", "AB28404",
			"AB28405", "AB28406", "AB28407", "AB28408", "AB28409",
			"AB28410", "AB28411", "AB28412", "AB28413", "AB39402"
		]

		report_d = reconcile_dataset_completeness(selected_15, "ItemDescription", d_items)
		report_u = reconcile_dataset_completeness(selected_15, "ItemUnitofMeasure", u_items)

		# Exactly 7 matched, exactly 8 missing
		expected_matched = ["AB28400", "AB28401", "AB28402", "AB28403", "AB28404", "AB28405", "AB39402"]
		expected_missing = ["AB28406", "AB28407", "AB28408", "AB28409", "AB28410", "AB28411", "AB28412", "AB28413"]

		self.assertEqual(report_d.matched_ids, expected_matched)
		self.assertEqual(report_d.missing_ids, expected_missing)
		self.assertEqual(report_u.matched_ids, expected_matched)
		self.assertEqual(report_u.missing_ids, expected_missing)

	# 32. Completeness gate policy live enforcement
	def test_32_completeness_gate_policy_live_enforcement(self):
		strict_enricher = ControlledItemEnricher(
			company=self.company,
			completeness_gate_policy=CompletenessGatePolicy.STRICT,
		)
		partial_enricher = ControlledItemEnricher(
			company=self.company,
			completeness_gate_policy=CompletenessGatePolicy.ALLOW_PARTIAL,
		)

		items = [
			CanonicalSourceItem(
				item_id="TEST-G1",
				master={"Item ID": "TEST-G1"},
				descriptions=[{"Item ID": "TEST-G1"}],
				uoms=[{"Item ID": "TEST-G1", "Unit of Measure": "EA"}],
			),
			CanonicalSourceItem(
				item_id="TEST-G2",
				master={"Item ID": "TEST-G2"},
				descriptions=[],
				uoms=[{"Item ID": "TEST-G2", "Unit of Measure": "EA"}],
			),
		]

		# STRICT policy marks preview blocked
		preview_strict = strict_enricher.preview(items)
		self.assertTrue(preview_strict.is_blocked)
		self.assertEqual(preview_strict.completeness_reports["ItemDescription"]["status"], "BLOCKED")

		# STRICT enrich_batch halts on completeness gate violation without mutating
		res_strict = strict_enricher.enrich_batch(items, run_id=self.test_run.name)
		self.assertTrue(any("Completeness gate violation" in err for err in res_strict.errors))
		self.assertEqual(len(res_strict.items_updated), 0)

		# STRICT policy with expected_missing passes
		strict_enricher_with_expected = ControlledItemEnricher(
			company=self.company,
			completeness_gate_policy=CompletenessGatePolicy.STRICT,
			expected_missing_secondary_ids={"ItemDescription": {"TEST-G2"}},
		)
		preview_with_expected = strict_enricher_with_expected.preview(items)
		self.assertEqual(preview_with_expected.completeness_reports["ItemDescription"]["status"], "PARTIAL_EXPECTED")

		# ALLOW_PARTIAL allows preview without block
		preview_partial = partial_enricher.preview(items)
		self.assertEqual(preview_partial.completeness_reports["ItemDescription"]["status"], "PARTIAL_EXPECTED")
