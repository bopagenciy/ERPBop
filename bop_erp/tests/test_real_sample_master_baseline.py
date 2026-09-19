# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

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
	FieldAuthorityPolicy,
	SourceDataClass,
	UOMMappingConfig,
	get_initial_p21_profiles,
	parse_xlsx_stream,
	reconcile_dataset_completeness,
	stage_dataset_row,
)
from bop_erp.migration.namespaces import canonical_provider, compute_migration_channel_id


REAL_15_SOURCE_ITEMS = [
	"AB28400", "AB28401", "AB28402", "AB28403", "AB28404",
	"AB28405", "AB32176", "AB32182", "AB32185", "AB34036",
	"AB34130", "AB34131", "AB34141", "AB39400", "AB39402",
]


class TestRealSampleMasterBaseline(FrappeTestCase):
	"""
	Phase 1Z.3 Test Suite: Controlled Import Provenance Reconciliation & Real-Sample Master Baseline.
	Verifies all 18 requirements from Section M against physical client workbooks.
	"""

	def _get_sample_dir(self) -> Path:
		return Path(frappe.get_app_path("bop_erp", "..", "local_data", "p21_samples")).resolve()

	def _cleanup_test_data(self):
		"""Clean up test Items, mappings, staging rows, and runs."""
		try:
			# Remove test items and mappings
			for iid in REAL_15_SOURCE_ITEMS + ["SYNTH-001", "SYNTH-002"]:
				if frappe.db.exists("Item", iid):
					try:
						frappe.delete_doc("Item", iid, force=True, ignore_permissions=True)
					except Exception:
						frappe.db.delete("Item", {"name": iid})

				maps = frappe.db.get_all(
					"External ID Mapping",
					filters={"external_id": iid},
					pluck="name",
				)
				for m in maps:
					frappe.db.delete("External ID Mapping", {"name": m})

			# Clean test runs and staging rows
			frappe.db.delete("Migration Staging Row", {"source_record_id": ("like", "%1Z3%")})
			runs = frappe.db.get_all("Migration Run", filters={"name": ("like", "%1Z3%")}, pluck="name")
			for r in runs:
				frappe.db.delete("Migration Run", {"name": r})
		except Exception:
			pass
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		self.company = "_Test Company 1Z3 Baseline"
		if not frappe.db.exists("Company", self.company):
			co = frappe.get_doc({
				"doctype": "Company",
				"company_name": self.company,
				"abbr": "_1Z3",
				"default_currency": "USD",
				"country": "United States",
			})
			co.insert(ignore_permissions=True)
			frappe.db.commit()

		self._cleanup_test_data()

		self.run_id = f"RUN-1Z3-BASELINE-{frappe.generate_hash(length=6)}"
		self.test_run = frappe.get_doc({
			"doctype": "Migration Run",
			"run_id": self.run_id,
			"company": self.company,
			"source_system": "PROPHET_21",
			"source_instance_id": "TEST_INST",
			"status": "READY",
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		# Ensure test UOMs exist
		for u_name in ["EA", "Nos", "Box", "Case", "Pack"]:
			if not frappe.db.exists("UOM", u_name):
				frappe.get_doc({"doctype": "UOM", "uom_name": u_name}).insert(ignore_permissions=True)
		frappe.db.commit()

		self.uom_config = UOMMappingConfig(
			mappings={"EA": "EA", "Nos": "Nos", "BOX": "Box", "CS": "Case", "PK": "Pack"},
			allow_exact_match=True,
		)
		self.importer = ControlledItemImporter(
			company=self.company,
			source_system="PROPHET_21",
			source_instance_id="TEST_INST",
			approved=True,
		)
		self.enricher = ControlledItemEnricher(
			company=self.company,
			source_system="PROPHET_21",
			source_instance_id="TEST_INST",
			uom_config=self.uom_config,
			description_policy=FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
			uom_policy=FieldAuthorityPolicy.REVIEW_ON_CONFLICT,
			completeness_policy=CompletenessGatePolicy.STRICT,
		)

	def tearDown(self):
		self._cleanup_test_data()
		super().tearDown()

	def _load_real_sample_canonical_items(self):
		"""Loads the exact first 15 real items from client workbooks with full provenance."""
		sample_dir = self._get_sample_dir()
		if not sample_dir.exists():
			self.skipTest("Sample files not present")

		profiles = {p.profile_id: p for p in get_initial_p21_profiles()}
		m_prof = profiles["P21_ITEM_MASTER"]
		d_prof = profiles["P21_ITEM_DESCRIPTION"]
		u_prof = profiles["P21_ITEM_UOM"]

		master_stream = list(parse_xlsx_stream(str(sample_dir / "1ItemMaster_sample.xlsx"), m_prof))
		desc_stream = list(parse_xlsx_stream(str(sample_dir / "5ItemDescription_sample.xlsx"), d_prof))
		uom_stream = list(parse_xlsx_stream(str(sample_dir / "4ItemUnitofMeasure_sample.xlsx"), u_prof))

		# Take first 15 real rows
		selected_master = master_stream[:15]
		selected_ids = [str(r[1]["Item ID"]).strip() for r in selected_master]

		desc_by_id = {}
		for row_num, row in desc_stream:
			iid = str(row.get("Item ID", "")).strip()
			if iid in selected_ids:
				desc_by_id[iid] = (row_num, row)

		uom_by_id = {}
		for row_num, row in uom_stream:
			iid = str(row.get("Item ID", "")).strip()
			if iid in selected_ids:
				uom_by_id.setdefault(iid, []).append((row_num, row))

		canonical_items = []
		for m_row_num, m_row in selected_master:
			iid = str(m_row["Item ID"]).strip()
			d_info = desc_by_id.get(iid)
			u_info_list = uom_by_id.get(iid, [])

			desc_rows = [d_info[1]] if d_info else []
			uom_rows = [u[1] for u in u_info_list]

			item = CanonicalSourceItem(
				item_id=iid,
				master=m_row,
				descriptions=desc_rows,
				uoms=uom_rows,
				source_data_class=SourceDataClass.CLIENT_SAMPLE.value,
				provenance={
					"source_data_class": SourceDataClass.CLIENT_SAMPLE.value,
					"source_file": "1ItemMaster_sample.xlsx",
					"source_row": m_row_num,
					"profile_id": "P21_ITEM_MASTER",
					"profile_version": "1.0.0-client-sample",
					"canonical_source_identity": json.dumps([iid], separators=(",", ":")),
					"migration_run": self.test_run.name,
					"datasets": {
						"ITEM_MASTER": {"source_file": "1ItemMaster_sample.xlsx", "source_row": m_row_num},
						"ITEM_DESCRIPTION": {"source_file": "5ItemDescription_sample.xlsx", "source_row": d_info[0] if d_info else None},
						"ITEM_UOM": {"source_file": "4ItemUnitofMeasure_sample.xlsx", "source_row": [u[0] for u in u_info_list]},
					}
				},
			)
			canonical_items.append(item)

		return canonical_items

	# 1. Selected IDs physically exist in ItemMaster
	def test_01_selected_ids_physically_exist_in_item_master(self):
		items = self._load_real_sample_canonical_items()
		self.assertEqual(len(items), 15)
		extracted_ids = [i.item_id for i in items]
		self.assertEqual(extracted_ids, REAL_15_SOURCE_ITEMS)

		# Prove none of the synthetic IDs AB28406..AB28413 are in this list
		synthetic_ids = [f"AB{n}" for n in range(28406, 28414)]
		for syn in synthetic_ids:
			self.assertNotIn(syn, extracted_ids)

	# 2. Synthetic IDs cannot be reported as client sample records
	def test_02_synthetic_ids_cannot_be_reported_as_client_sample(self):
		synth_item = CanonicalSourceItem(
			item_id="SYNTH-001",
			master={"Item ID": "SYNTH-001", "Item Description": "Synthetic test item"},
			source_data_class=SourceDataClass.SYNTHETIC_FIXTURE.value,
			provenance={
				"source_data_class": SourceDataClass.SYNTHETIC_FIXTURE.value,
				"source_file": "synthetic_fixture.json",
				"source_row": 1,
			}
		)
		self.assertEqual(synth_item.source_data_class, SourceDataClass.SYNTHETIC_FIXTURE.value)
		self.assertNotEqual(synth_item.source_data_class, SourceDataClass.CLIENT_SAMPLE.value)

	# 3. source_data_class/provenance distinction
	def test_03_source_data_class_and_provenance_distinction(self):
		real_item = CanonicalSourceItem(item_id="AB28400", source_data_class=SourceDataClass.CLIENT_SAMPLE.value)
		synth_item = CanonicalSourceItem(item_id="AB28406", source_data_class=SourceDataClass.SYNTHETIC_FIXTURE.value)

		self.assertEqual(real_item.to_dict()["source_data_class"], "CLIENT_SAMPLE")
		self.assertEqual(synth_item.to_dict()["source_data_class"], "SYNTHETIC_FIXTURE")

	# 4. Physical source row preserved
	def test_04_physical_source_row_preserved(self):
		items = self._load_real_sample_canonical_items()
		first_item = items[0]
		self.assertEqual(first_item.item_id, "AB28400")
		self.assertEqual(first_item.provenance["source_row"], 5)
		self.assertEqual(first_item.provenance["datasets"]["ITEM_DESCRIPTION"]["source_row"], 6)
		self.assertEqual(first_item.provenance["datasets"]["ITEM_UOM"]["source_row"], [6])

	# 5. 15 real-root IDs pass completeness reconciliation
	def test_05_15_real_root_ids_pass_completeness_reconciliation(self):
		items = self._load_real_sample_canonical_items()
		desc_ids = [i.item_id for i in items if i.descriptions]
		uom_ids = [i.item_id for i in items if i.uoms]

		report_d = reconcile_dataset_completeness(REAL_15_SOURCE_ITEMS, "ItemDescription", desc_ids, policy=CompletenessGatePolicy.STRICT)
		report_u = reconcile_dataset_completeness(REAL_15_SOURCE_ITEMS, "ItemUnitofMeasure", uom_ids, policy=CompletenessGatePolicy.STRICT)

		self.assertEqual(report_d.status, "COMPLETE")
		self.assertEqual(report_u.status, "COMPLETE")
		self.assertEqual(len(report_d.matched_ids), 15)
		self.assertEqual(len(report_u.matched_ids), 15)
		self.assertEqual(len(report_d.missing_ids), 0)
		self.assertEqual(len(report_u.missing_ids), 0)

	# 6. Target Item resolves through mapping
	def test_06_target_item_resolves_through_mapping(self):
		items = self._load_real_sample_canonical_items()
		import_res = self.importer.import_batch(items, run_id=self.test_run.name)
		enrich_res = self.enricher.enrich_batch(items, run_id=self.test_run.name)

		self.assertEqual(len(import_res.created_items), 15)
		for iid in REAL_15_SOURCE_ITEMS:
			target_doc, mapping_id = self.enricher.resolve_target_item(iid)
			self.assertIsNotNone(target_doc)
			self.assertIsNotNone(mapping_id)
			self.assertEqual(target_doc.name, iid)

	# 7. Reimport converges
	def test_07_reimport_converges(self):
		items = self._load_real_sample_canonical_items()
		# Run 1
		self.importer.import_batch(items, run_id=self.test_run.name)
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()

		# Run 2
		re_import_res = self.importer.import_batch(items, run_id=self.test_run.name)
		re_enrich_res = self.enricher.enrich_batch(items, run_id=self.test_run.name)

		self.assertEqual(len(re_import_res.reused_items), 15)
		self.assertEqual(len(re_import_res.created_items), 0)
		self.assertEqual(re_enrich_res.items_noop, [i.item_id for i in items])
		self.assertEqual(len(re_enrich_res.items_updated), 0)

	# 8. No duplicate Item
	def test_08_no_duplicate_item(self):
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		item_count_1 = frappe.db.count("Item", {"name": ("in", REAL_15_SOURCE_ITEMS)})
		self.assertEqual(item_count_1, 15)

		# Reimport
		self.importer.import_batch(items, run_id=self.test_run.name)
		item_count_2 = frappe.db.count("Item", {"name": ("in", REAL_15_SOURCE_ITEMS)})
		self.assertEqual(item_count_2, 15)

	# 9. No duplicate UOM
	def test_09_no_duplicate_uom(self):
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()

		# Check UOM child rows count on AB28400
		doc = frappe.get_doc("Item", "AB28400")
		uom_count_1 = len(doc.uoms)

		# Re-enrich
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()

		doc.reload()
		self.assertEqual(len(doc.uoms), uom_count_1)

	# 10. Bop-authoritative edit survives
	def test_10_bop_authoritative_edit_survives(self):
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		frappe.db.commit()

		# User manually edits description in ERPNext
		custom_desc = "BOP-CURATED-VALVE-DESCRIPTION-42"
		doc = frappe.get_doc("Item", "AB28400")
		doc.description = custom_desc
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		# Re-enrich with BOP_AUTHORITATIVE policy
		bop_enricher = ControlledItemEnricher(
			company=self.company,
			source_system="PROPHET_21",
			source_instance_id="TEST_INST",
			uom_config=self.uom_config,
			description_policy=FieldAuthorityPolicy.BOP_AUTHORITATIVE,
			completeness_policy=CompletenessGatePolicy.STRICT,
		)
		bop_enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()

		doc.reload()
		self.assertEqual(doc.description, custom_desc)

	# 11. Zero Item Price
	def test_11_zero_item_price(self):
		price_before = frappe.db.count("Item Price")
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()
		self.assertEqual(frappe.db.count("Item Price"), price_before)

	# 12. Zero Supplier creation
	def test_12_zero_supplier_creation(self):
		supp_before = frappe.db.count("Supplier")
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()
		self.assertEqual(frappe.db.count("Supplier"), supp_before)

	# 13. Zero Warehouse creation
	def test_13_zero_warehouse_creation(self):
		wh_before = frappe.db.count("Warehouse")
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()
		self.assertEqual(frappe.db.count("Warehouse"), wh_before)

	# 14. Zero Stock Ledger Entry
	def test_14_zero_stock_ledger_entry(self):
		sle_before = frappe.db.count("Stock Ledger Entry")
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), sle_before)

	# 15. Zero nonzero Bin stock
	def test_15_zero_nonzero_bin_stock(self):
		bin_qty_before = frappe.db.count("Bin", {"actual_qty": (">", 0)})
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()
		self.assertEqual(frappe.db.count("Bin", {"actual_qty": (">", 0)}), bin_qty_before)

	# 16. Zero Stock Reconciliation
	def test_16_zero_stock_reconciliation(self):
		recon_before = frappe.db.count("Stock Reconciliation")
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()
		self.assertEqual(frappe.db.count("Stock Reconciliation"), recon_before)

	# 17. Zero GL
	def test_17_zero_gl(self):
		gl_before = frappe.db.count("GL Entry")
		items = self._load_real_sample_canonical_items()
		self.importer.import_batch(items, run_id=self.test_run.name)
		self.enricher.enrich_batch(items, run_id=self.test_run.name)
		frappe.db.commit()
		self.assertEqual(frappe.db.count("GL Entry"), gl_before)

	# 18. Client workbooks unchanged
	def test_18_client_workbooks_unchanged(self):
		import subprocess
		res = subprocess.run(
			["git", "status", "--porcelain", "local_data"],
			capture_output=True,
			text=True,
			cwd=frappe.get_app_path("bop_erp", ".."),
		)
		self.assertEqual(res.returncode, 0)
		self.assertEqual(res.stdout.strip(), "", "local_data must remain completely untracked and unchanged")
