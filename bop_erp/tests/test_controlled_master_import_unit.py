# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest

from bop_erp.migration.datasets.aggregation import CanonicalSourceItem
from bop_erp.migration.datasets.authority import (
	AuthorityDecision,
	FieldAuthorityPolicy,
	evaluate_field_authority,
)
from bop_erp.migration.datasets.importers import (
	ControlledItemImporter,
	EligibilityStatus,
	ImportEligibilityResult,
	ItemImportPreview,
	ItemImportResult,
	build_target_item_code,
	evaluate_item_eligibility,
	generate_import_preview,
)
from bop_erp.migration.exceptions import ImportBoundaryError


class TestControlledMasterImportUnit(unittest.TestCase):
	"""
	Phase 1Z Unit Test Suite: Pure policy and mapping behavior for controlled master import.
	Tests eligibility evaluation, field authority, preview generation, identity preservation,
	and zero stock invariants without requiring database round-trips.
	"""

	def test_01_eligibility_simple_item(self):
		item = CanonicalSourceItem(
			item_id="AB28400",
			master={"Item ID": "AB28400", "Item Description": "1/8 Ball Valve", "Base Unit": "EA"},
			descriptions=[{"Item ID": "AB28400", "Extended Description": "Full 1/8 Ball Valve Push-Fit"}],
			uoms=[{"Item ID": "AB28400", "Unit of Measure": "EA", "Unit Size": 1}],
		)
		elig = evaluate_item_eligibility(item)
		self.assertEqual(elig.status, EligibilityStatus.ELIGIBLE)
		self.assertEqual(elig.resolved_stock_uom, "EA")
		self.assertEqual(len(elig.reasons), 0)

	def test_02_eligibility_missing_item_id(self):
		item = CanonicalSourceItem(
			item_id="",
			master={"Item ID": "", "Item Description": "No ID Item", "Base Unit": "EA"},
		)
		elig = evaluate_item_eligibility(item)
		self.assertEqual(elig.status, EligibilityStatus.BLOCKED)
		self.assertIn("Missing mandatory Item ID.", elig.reasons)

	def test_03_eligibility_missing_master(self):
		item = CanonicalSourceItem(item_id="NO-MASTER-01", master={})
		elig = evaluate_item_eligibility(item)
		self.assertEqual(elig.status, EligibilityStatus.BLOCKED)
		self.assertIn("Missing master record.", elig.reasons)

	def test_04_eligibility_missing_uom(self):
		item = CanonicalSourceItem(
			item_id="NO-UOM-01",
			master={"Item ID": "NO-UOM-01", "Item Description": "No UOM Item", "Base Unit": ""},
			uoms=[],
		)
		elig = evaluate_item_eligibility(item, fallback_uom="")
		self.assertEqual(elig.status, EligibilityStatus.BLOCKED)
		self.assertIn("Missing mandatory Stock UOM.", elig.reasons)

	def test_05_eligibility_partial_item_missing_description(self):
		item = CanonicalSourceItem(
			item_id="PARTIAL-01",
			master={"Item ID": "PARTIAL-01", "Item Description": "Partial Item", "Base Unit": "EA"},
			descriptions=[],
		)
		elig = evaluate_item_eligibility(item)
		self.assertEqual(elig.status, EligibilityStatus.ELIGIBLE_PARTIAL)
		self.assertTrue(any("description missing" in w.lower() for w in elig.warnings))

	def test_06_eligibility_deferred_supplier(self):
		item = CanonicalSourceItem(
			item_id="SUPP-01",
			master={"Item ID": "SUPP-01", "Item Description": "Supplier Item", "Base Unit": "EA"},
			suppliers=[{"Item ID": "SUPP-01", "Supplier ID": "201", "Supplier Name": None}],
		)
		elig = evaluate_item_eligibility(item)
		self.assertEqual(elig.status, EligibilityStatus.ELIGIBLE_PARTIAL)
		self.assertIn("SUPPLIER", elig.deferred_relationships)

	def test_07_eligibility_deferred_location(self):
		item = CanonicalSourceItem(
			item_id="LOC-01",
			master={"Item ID": "LOC-01", "Item Description": "Location Item", "Base Unit": "EA"},
			locations=[{"Item ID": "LOC-01", "Location ID": "100", "Company ID": "1"}],
		)
		elig = evaluate_item_eligibility(item)
		self.assertEqual(elig.status, EligibilityStatus.ELIGIBLE_PARTIAL)
		self.assertIn("LOCATION", elig.deferred_relationships)

	def test_08_eligibility_deferred_supplier_location_override(self):
		item = CanonicalSourceItem(
			item_id="OVR-01",
			master={"Item ID": "OVR-01", "Item Description": "Override Item", "Base Unit": "EA"},
			supplier_location_overrides=[{"Item ID": "OVR-01", "Location ID": "100", "Supplier ID": "201"}],
		)
		elig = evaluate_item_eligibility(item)
		self.assertIn("SUPPLIER_LOCATION_OVERRIDE", elig.deferred_relationships)

	def test_09_target_item_code_exact_preservation(self):
		self.assertEqual(build_target_item_code("00123"), "00123")
		self.assertEqual(build_target_item_code("  00123  "), "00123")
		self.assertEqual(build_target_item_code("AB28400"), "AB28400")

	def test_10_target_item_code_no_destructive_coercion(self):
		self.assertEqual(build_target_item_code("VALVE-1/8-MIP"), "VALVE-1/8-MIP")
		self.assertEqual(build_target_item_code("PART.01_A#2"), "PART.01_A#2")

	def test_11_preview_structure_and_counts(self):
		items = [
			CanonicalSourceItem(
				item_id="ITM-01",
				master={"Item ID": "ITM-01", "Item Description": "Item 1", "Base Unit": "EA"},
				descriptions=[{"Item ID": "ITM-01", "Extended Description": "Extended 1"}],
			),
			CanonicalSourceItem(
				item_id="ITM-02",
				master={"Item ID": "ITM-02", "Item Description": "Item 2", "Base Unit": "EA"},
				suppliers=[{"Item ID": "ITM-02", "Supplier ID": "999"}],
			),
			CanonicalSourceItem(item_id="ITM-03", master={}),
		]
		preview = generate_import_preview(items, company="TestCo")
		self.assertEqual(preview.selected_items, ["ITM-01", "ITM-02", "ITM-03"])
		self.assertEqual(preview.eligibility_summary["ELIGIBLE"], 1)
		self.assertEqual(preview.eligibility_summary["ELIGIBLE_PARTIAL"], 1)
		self.assertEqual(preview.eligibility_summary["BLOCKED"], 1)
		self.assertIn("Quantity On Hand (Inventory Cutover)", preview.fields_omitted["ITM-01"])

	def test_12_field_authority_initial_import(self):
		dec, val, _ = evaluate_field_authority(
			"stock_uom",
			current_bop_val=None,
			incoming_source_val="EA",
			policy=FieldAuthorityPolicy.IMPORT_ONCE,
			is_initial_import=True,
		)
		self.assertEqual(dec, AuthorityDecision.APPLY_SOURCE)
		self.assertEqual(val, "EA")

	def test_13_field_authority_reimport_bop_preservation(self):
		# Manual edit in BOP ERP must not be overwritten
		dec, val, _ = evaluate_field_authority(
			"description",
			current_bop_val="Custom Bop Storefront Description",
			incoming_source_val="Original P21 Short Description",
			policy=FieldAuthorityPolicy.BOP_AUTHORITATIVE,
			is_initial_import=False,
		)
		self.assertEqual(dec, AuthorityDecision.KEEP_BOP)
		self.assertEqual(val, "Custom Bop Storefront Description")

	def test_14_field_authority_reimport_source_authoritative(self):
		dec, val, _ = evaluate_field_authority(
			"item_name",
			current_bop_val="Old Name",
			incoming_source_val="New Supplier Catalog Name",
			policy=FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
			is_initial_import=False,
		)
		self.assertEqual(dec, AuthorityDecision.APPLY_SOURCE)
		self.assertEqual(val, "New Supplier Catalog Name")

	def test_15_price_tiers_omitted_from_target_creation(self):
		item = CanonicalSourceItem(
			item_id="PRICE-01",
			master={
				"Item ID": "PRICE-01",
				"Item Description": "Priced Item",
				"Base Unit": "EA",
				"Price 1": "10.50",
				"Price 2": "9.50",
			},
		)
		elig = evaluate_item_eligibility(item)
		self.assertTrue(any("price tier" in w.lower() for w in elig.warnings))

	def test_16_cost_fields_omitted_from_valuation(self):
		item = CanonicalSourceItem(
			item_id="COST-01",
			master={"Item ID": "COST-01", "Item Description": "Cost Item", "Base Unit": "EA"},
			locations=[{"Item ID": "COST-01", "Moving Average Cost": "145.89"}],
		)
		preview = generate_import_preview([item], company="TestCo")
		self.assertIn("Moving Average Cost (Valuation Cutover)", preview.fields_omitted["COST-01"])

	def test_17_unapproved_importer_blocks_mutation(self):
		importer = ControlledItemImporter(company="TestCo", approved=False)
		item = CanonicalSourceItem(
			item_id="AB28400",
			master={"Item ID": "AB28400", "Item Description": "Valve", "Base Unit": "EA"},
		)
		with self.assertRaises(ImportBoundaryError) as ctx:
			importer.import_item(item)
		self.assertIn("must be explicitly approved", str(ctx.exception))

	def test_18_result_summary_zero_stock_mutations(self):
		res = ItemImportResult(run_id="TEST-RUN", selected_count=5)
		self.assertEqual(res.stock_mutation_count, 0)
		self.assertEqual(res.financial_mutation_count, 0)


if __name__ == "__main__":
	unittest.main()
