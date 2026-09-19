# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from unittest import TestCase
from unittest.mock import MagicMock, patch

from bop_erp.migration.datasets import (
	CanonicalSourceItem,
	ControlledItemEnricher,
	DescriptionAction,
	FieldAuthorityPolicy,
	ItemEnrichmentPreview,
	ItemEnrichmentResult,
	UOMAction,
	UOMMappingConfig,
	generate_enrichment_preview,
)


class TestMultiDatasetMasterEnrichmentUnit(TestCase):
	"""
	Unit tests for multi-dataset master data enrichment policies, UOM mapping,
	description authority, and preview logic. Runs offline without database dependencies.
	"""

	def setUp(self):
		self.uom_config = UOMMappingConfig(
			mappings={"EA": "EA", "EACH": "EA", "BOX": "Box", "CS": "Case"},
			allow_exact_match=False,
			allow_case_insensitive=False,
		)
		self.enricher = ControlledItemEnricher(
			company="Test Company",
			source_system="PROPHET_21",
			source_instance_id="TEST_INST",
			uom_config=self.uom_config,
			description_policy=FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
			uom_policy=FieldAuthorityPolicy.REVIEW_ON_CONFLICT,
		)

	# 1. UOM mapping configuration resolution
	def test_01_uom_mapping_config_resolution(self):
		self.assertEqual(self.uom_config.resolve_erp_uom("EA"), "EA")
		self.assertEqual(self.uom_config.resolve_erp_uom("EACH"), "EA")
		self.assertEqual(self.uom_config.resolve_erp_uom("BOX"), "Box")
		self.assertEqual(self.uom_config.resolve_erp_uom("CS"), "Case")
		self.assertIsNone(self.uom_config.resolve_erp_uom("UNKNOWN_UOM"))
		self.assertIsNone(self.uom_config.resolve_erp_uom(None))
		self.assertIsNone(self.uom_config.resolve_erp_uom(""))

	# 2. Target resolution via mapping
	@patch("frappe.db.get_value")
	@patch("frappe.db.exists")
	@patch("frappe.get_doc")
	def test_02_target_resolution_found(self, mock_get_doc, mock_exists, mock_get_value):
		mock_get_value.return_value = {"name": "MAP-001", "erp_document": "AB28400"}
		mock_exists.return_value = True
		mock_doc = MagicMock()
		mock_doc.name = "AB28400"
		mock_get_doc.return_value = mock_doc

		item_doc, mapping_name = self.enricher.resolve_target_item("AB28400")
		self.assertIsNotNone(item_doc)
		self.assertEqual(mapping_name, "MAP-001")
		self.assertEqual(item_doc.name, "AB28400")

	# 3. Target resolution missing blocks enrichment
	@patch("frappe.db.get_value")
	def test_03_target_resolution_missing_blocks(self, mock_get_value):
		mock_get_value.return_value = None

		item_doc, mapping_name = self.enricher.resolve_target_item("MISSING_ITEM")
		self.assertIsNone(item_doc)
		self.assertIsNone(mapping_name)

		canonical_item = CanonicalSourceItem(item_id="MISSING_ITEM")
		result = self.enricher.enrich_item(canonical_item)
		self.assertEqual(result["status"], "BLOCKED")
		self.assertIn("No active External ID Mapping", result["reason"])

	# 4. Description authority: SOURCE_AUTHORITATIVE updates
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_04_description_source_authoritative(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Old Short Description"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_item.sales_uom = None
		mock_item.purchase_uom = None
		mock_resolve.return_value = (mock_item, "MAP-001")

		self.enricher.description_policy = FieldAuthorityPolicy.SOURCE_AUTHORITATIVE
		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			descriptions=[{"Extended Description": "New 1/8BALL VALVE PUSH-FIT CONNECT"}],
		)
		res = self.enricher.enrich_item(canonical_item)
		self.assertEqual(res["status"], "UPDATED")
		self.assertEqual(res["description_action"], DescriptionAction.UPDATE.value)
		self.assertEqual(mock_item.description, "New 1/8BALL VALVE PUSH-FIT CONNECT")
		mock_item.save.assert_called_once()

	# 5. Description authority: BOP_AUTHORITATIVE preserves manual edit
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_05_description_bop_authoritative_preserves(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Manually Curated Marketing Description"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_item.sales_uom = None
		mock_item.purchase_uom = None
		mock_resolve.return_value = (mock_item, "MAP-001")

		self.enricher.description_policy = FieldAuthorityPolicy.BOP_AUTHORITATIVE
		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			descriptions=[{"Extended Description": "Raw Source Extended Description"}],
		)
		res = self.enricher.enrich_item(canonical_item)
		self.assertEqual(res["status"], "NOOP")
		self.assertEqual(res["description_action"], DescriptionAction.PRESERVE_BOP.value)
		self.assertEqual(mock_item.description, "Manually Curated Marketing Description")
		mock_item.save.assert_not_called()

	# 6. Description idempotency: identical description is NOOP
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_06_description_identical_noop(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Identical Description"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_item.sales_uom = None
		mock_item.purchase_uom = None
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			descriptions=[{"Extended Description": "Identical Description"}],
		)
		res = self.enricher.enrich_item(canonical_item)
		self.assertEqual(res["status"], "NOOP")
		self.assertEqual(res["description_action"], DescriptionAction.NOOP.value)
		mock_item.save.assert_not_called()

	# 7. UOM conversion factor validation (> 0)
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_07_uom_factor_validation(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Desc"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			uoms=[
				{"Unit of Measure": "BOX", "Unit Size": -5},
				{"Unit of Measure": "CS", "Unit Size": 0},
			],
		)
		res = self.enricher.enrich_item(canonical_item)
		for u_act in res["uom_actions"]:
			self.assertEqual(u_act["action"], UOMAction.INVALID.value)

	# 8. UOM addition and duplicate prevention
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_08_uom_addition_and_duplicate_prevention(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Desc"
		child_box = MagicMock()
		child_box.uom = "Box"
		child_box.conversion_factor = 10.0
		mock_item.uoms = [child_box]
		mock_item.stock_uom = "EA"
		mock_resolve.return_value = (mock_item, "MAP-001")

		# Incoming Box with same factor 10.0 should be NOOP
		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			uoms=[{"Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		res = self.enricher.enrich_item(canonical_item)
		self.assertEqual(res["status"], "NOOP")
		self.assertEqual(res["uom_actions"][0]["action"], UOMAction.NOOP.value)
		mock_item.save.assert_not_called()

	# 9. UOM factor conflict detection
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_09_uom_factor_conflict_detection(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Desc"
		child_box = MagicMock()
		child_box.uom = "Box"
		child_box.conversion_factor = 10.0
		mock_item.uoms = [child_box]
		mock_item.stock_uom = "EA"
		mock_resolve.return_value = (mock_item, "MAP-001")

		# Incoming Box with factor 12.0 when policy is REVIEW_ON_CONFLICT
		self.enricher.uom_policy = FieldAuthorityPolicy.REVIEW_ON_CONFLICT
		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			uoms=[{"Unit of Measure": "BOX", "Unit Size": 12.0}],
		)
		res = self.enricher.enrich_item(canonical_item)
		self.assertEqual(res["uom_actions"][0]["action"], UOMAction.CONFLICT.value)
		# Item not modified
		self.assertEqual(child_box.conversion_factor, 10.0)

	# 10. Manual Bop UOM preserved
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_10_manual_bop_uom_preserved(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Desc"
		manual_pack = MagicMock()
		manual_pack.uom = "Pack"
		manual_pack.conversion_factor = 5.0
		mock_item.uoms = [manual_pack]
		mock_item.stock_uom = "EA"
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			uoms=[{"Unit of Measure": "CS", "Unit Size": 24.0}],
		)
		res = self.enricher.enrich_item(canonical_item)
		# Ensure manual row is still in uoms
		self.assertIn(manual_pack, mock_item.uoms)

	# 11. Selling Unit / Purchasing Unit flags
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_11_selling_purchasing_unit_flags(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Desc"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_item.sales_uom = None
		mock_item.purchase_uom = None
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			uoms=[{
				"Unit of Measure": "EA",
				"Unit Size": 1.0,
				"Selling Unit": "Y",
				"Purchasing Unit": "Y",
			}],
		)
		res = self.enricher.enrich_item(canonical_item)
		self.assertEqual(mock_item.sales_uom, "EA")
		self.assertEqual(mock_item.purchase_uom, "EA")

	# 12. Stock UOM factor mismatch detected
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_12_stock_uom_factor_mismatch(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Desc"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			uoms=[{"Unit of Measure": "EA", "Unit Size": 2.0}],
		)
		res = self.enricher.enrich_item(canonical_item)
		self.assertEqual(res["uom_actions"][0]["action"], UOMAction.CONFLICT.value)

	# 13. Missing description tolerated (partial item)
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_13_missing_description_tolerated(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Existing Description"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(item_id="AB28400", descriptions=[])
		res = self.enricher.enrich_item(canonical_item)
		self.assertEqual(res["description_action"], DescriptionAction.MISSING.value)
		self.assertEqual(res["status"], "NOOP")

	# 14. Missing UOM tolerated
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_14_missing_uom_tolerated(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Desc"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(item_id="AB28400", uoms=[])
		res = self.enricher.enrich_item(canonical_item)
		self.assertEqual(res["uom_actions"], [])
		self.assertEqual(res["status"], "NOOP")

	# 15. Deferred relationships registered
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_15_deferred_relationships_registered(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Desc"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			suppliers=[{"Supplier ID": "SUP-01"}],
			locations=[{"Location ID": "LOC-01"}],
			supplier_location_overrides=[{"Supplier ID": "SUP-01", "Location ID": "LOC-01"}],
		)
		res = self.enricher.enrich_item(canonical_item)
		self.assertIn("INVENTORY_SUPPLIER", res["deferred_relationships"])
		self.assertIn("INVENTORY_LOCATION", res["deferred_relationships"])
		self.assertIn("ITEM_SUPPLIER_BY_LOCATION", res["deferred_relationships"])

	# 16. Preview produces zero mutations
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	def test_16_preview_produces_zero_mutations(self, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Old Desc"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			descriptions=[{"Extended Description": "New Desc"}],
			uoms=[{"Unit of Measure": "BOX", "Unit Size": 10.0}],
		)
		preview = self.enricher.preview([canonical_item])
		self.assertEqual(preview.summary["to_update"], 1)
		mock_item.save.assert_not_called()

	# 17. Batch enrichment result accounting
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	def test_17_batch_result_accounting(self, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Old Desc"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		mock_item.sales_uom = None
		mock_item.purchase_uom = None
		mock_resolve.return_value = (mock_item, "MAP-001")

		items = [
			CanonicalSourceItem(
				item_id="AB28400",
				descriptions=[{"Extended Description": "Updated Desc"}],
				uoms=[{"Unit of Measure": "BOX", "Unit Size": 10.0}],
			)
		]
		batch_res = self.enricher.enrich_batch(items)
		self.assertEqual(batch_res.items_processed, 1)
		self.assertEqual(len(batch_res.items_updated), 1)
		self.assertEqual(batch_res.descriptions_updated, 1)
		self.assertEqual(batch_res.uom_rows_added, 1)

	# 18. Simulated mid-enrichment failure rolls back savepoint
	@patch.object(ControlledItemEnricher, "resolve_target_item")
	@patch("frappe.db.savepoint")
	@patch("frappe.db.rollback")
	def test_18_atomic_savepoint_rollback_on_failure(self, mock_rollback, mock_sp, mock_resolve):
		mock_item = MagicMock()
		mock_item.name = "AB28400"
		mock_item.description = "Old Desc"
		mock_item.uoms = []
		mock_item.stock_uom = "EA"
		# Simulate save failure
		mock_item.save.side_effect = RuntimeError("Database constraint failure")
		mock_resolve.return_value = (mock_item, "MAP-001")

		canonical_item = CanonicalSourceItem(
			item_id="AB28400",
			descriptions=[{"Extended Description": "Updated Desc"}],
		)
		with self.assertRaises(RuntimeError):
			self.enricher.enrich_item(canonical_item)

		mock_rollback.assert_called_once()

	# 19. Completeness reconciliation: complete join
	def test_19_reconcile_dataset_completeness_complete(self):
		from bop_erp.migration.datasets import (
			CompletenessGatePolicy,
			reconcile_dataset_completeness,
		)
		roots = ["AB28400", "AB28401", "AB28402"]
		secondary = ["AB28400", "AB28401", "AB28402"]
		report = reconcile_dataset_completeness(roots, "ItemDescription", secondary)
		self.assertEqual(report.status, "COMPLETE")
		self.assertEqual(report.matched_ids, ["AB28400", "AB28401", "AB28402"])
		self.assertEqual(report.missing_ids, [])
		self.assertEqual(report.unexpected_secondary_only_ids, [])

	# 20. Completeness reconciliation: expected missing
	def test_20_reconcile_dataset_completeness_expected_missing(self):
		from bop_erp.migration.datasets import (
			CompletenessGatePolicy,
			reconcile_dataset_completeness,
		)
		roots = ["AB28400", "AB28401", "AB28406"]
		secondary = ["AB28400", "AB28401"]
		report = reconcile_dataset_completeness(
			roots,
			"ItemDescription",
			secondary,
			policy=CompletenessGatePolicy.STRICT,
			expected_missing_ids={"AB28406"},
		)
		self.assertEqual(report.status, "PARTIAL_EXPECTED")
		self.assertEqual(report.matched_ids, ["AB28400", "AB28401"])
		self.assertEqual(report.missing_ids, ["AB28406"])

	# 21. Completeness reconciliation: unexpected missing under STRICT policy blocks
	def test_21_reconcile_dataset_completeness_strict_blocking(self):
		from bop_erp.migration.datasets import (
			CompletenessGatePolicy,
			reconcile_dataset_completeness,
		)
		roots = ["AB28400", "AB28401", "AB28406"]
		secondary = ["AB28400", "AB28401"]
		# AB28406 not declared in expected_missing_ids
		report = reconcile_dataset_completeness(
			roots,
			"ItemDescription",
			secondary,
			policy=CompletenessGatePolicy.STRICT,
		)
		self.assertEqual(report.status, "BLOCKED")
		self.assertIn("Unexpected missing IDs in ItemDescription: ['AB28406']", report.warnings[0])

	# 22. Completeness reconciliation: secondary-only IDs tracked
	def test_22_reconcile_dataset_completeness_secondary_only_tracking(self):
		from bop_erp.migration.datasets import reconcile_dataset_completeness
		roots = ["AB28400"]
		secondary = ["AB28400", "AB32176", "AB32182"]
		report = reconcile_dataset_completeness(roots, "ItemUnitofMeasure", secondary)
		self.assertEqual(report.matched_ids, ["AB28400"])
		self.assertEqual(report.unexpected_secondary_only_ids, ["AB32176", "AB32182"])

	# 23. Root Item identity extraction across all 15 test IDs
	def test_23_root_item_identity_extraction_exact(self):
		from bop_erp.migration.datasets.staging import (
			extract_item_id_from_record_id,
			serialize_canonical_key,
		)
		selected_15 = [
			"AB28400", "AB28401", "AB28402", "AB28403", "AB28404",
			"AB28405", "AB28406", "AB28407", "AB28408", "AB28409",
			"AB28410", "AB28411", "AB28412", "AB28413", "AB39402"
		]
		for s_id in selected_15:
			# Test canonical JSON representation and composite forms
			rec_id_1 = serialize_canonical_key([s_id])
			rec_id_2 = serialize_canonical_key([s_id, "EA"])
			rec_id_3 = f"{s_id}::EA"
			rec_id_4 = s_id
			self.assertEqual(extract_item_id_from_record_id(rec_id_1), s_id)
			self.assertEqual(extract_item_id_from_record_id(rec_id_2), s_id)
			self.assertEqual(extract_item_id_from_record_id(rec_id_3), s_id)
			self.assertEqual(extract_item_id_from_record_id(rec_id_4), s_id)

	# 24. Parser row boundary and generator non-truncation proof
	def test_24_parser_row_boundary_integrity(self):
		from pathlib import Path
		# Prove generator yields all items without premature stopping at 7
		from bop_erp.migration.datasets.parser import parse_csv_stream
		from bop_erp.migration.datasets.profiles import SourceDatasetProfile
		import io

		# Generate a CSV stream with 30 items
		csv_lines = ["Item ID,Item Description\n"]
		for i in range(1, 31):
			csv_lines.append(f"ITEM-{i:03d},Description {i}\n")
		csv_content = "".join(csv_lines)

		profile = SourceDatasetProfile(
			profile_id="TEST_PROFILE",
			source_system="TEST",
			dataset_name="Test",
			entity_type="ITEM_MASTER",
			file_type="csv",
			sheet_name="",
			header_row=1,
			metadata_rows=[],
			data_start_row=2,
			example_rows_to_ignore=[],
			key_fields=["Item ID"],
			required_fields=["Item ID"],
			field_mappings={"Item ID": "item_code"},
		)

		with patch("bop_erp.migration.datasets.parser.check_file_safety") as mock_safety:
			mock_safety.return_value = Path("dummy.csv")
			with patch("builtins.open", return_value=io.StringIO(csv_content)):
				rows = list(parse_csv_stream("dummy.csv", profile))
				self.assertEqual(len(rows), 30)
				self.assertEqual(rows[0][1]["Item ID"], "ITEM-001")
				self.assertEqual(rows[29][1]["Item ID"], "ITEM-030")
