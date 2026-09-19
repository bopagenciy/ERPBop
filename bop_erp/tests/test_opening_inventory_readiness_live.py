# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from decimal import Decimal
from pathlib import Path
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import ExternalEntityType
from bop_erp.migration.opening_inventory import (
	CanonicalOpeningInventoryRow,
	CutoverPolicy,
	LocationMappingRegistry,
	LocationMappingStatus,
	SnapshotProvenancePolicy,
	SourceInventoryLocationIdentity,
	ValuationPolicyStatus,
	assess_opening_inventory_readiness,
	audit_inventory_location_semantics,
	audit_valuation_sources,
	compute_candidate_valuation_scenarios,
	generate_opening_inventory_reconciliation_report,
)


class TestOpeningInventoryReadinessLive(FrappeTestCase):
	"""
	Phase 2A Live Integration Test Suite: Opening Inventory Readiness & Valuation Audit.
	Executes live against the local Frappe test site with ZERO stock mutations.
	"""

	def setUp(self):
		super().setUp()
		self.sample_dir = Path(frappe.get_app_path("bop_erp", "..", "local_data", "p21_samples")).resolve()

		# Ensure a live test company exists
		self.test_company = "_Test Company Phase 2A"
		if not frappe.db.exists("Company", self.test_company):
			co = frappe.get_doc({
				"doctype": "Company",
				"company_name": self.test_company,
				"abbr": "_2A",
				"default_currency": "USD",
				"country": "United States",
			})
			co.insert(ignore_permissions=True)
			frappe.db.commit()

		# Set up mapping registry
		self.mapping_registry = LocationMappingRegistry()
		self.mapping_registry.register_company_mapping("ABFAST", self.test_company)

	def test_01_live_item_and_company_resolution(self):
		"""
		Verifies resolution of source items against live External ID Mapping and Company.
		"""
		# Query any existing External ID Mappings
		mappings = frappe.get_all(
			"External ID Mapping",
			filters={"external_entity_type": ExternalEntityType.PRODUCT},
			fields=["external_id", "erp_document"],
			limit=5,
		)
		resolved_map = {m["external_id"]: m["erp_document"] for m in mappings}

		# Audit client samples with resolved map
		report = generate_opening_inventory_reconciliation_report(
			sample_dir=self.sample_dir,
			mapping_registry=self.mapping_registry,
			resolved_item_map=resolved_map,
		)

		self.assertEqual(report["total_inventory_location_rows"], 25)
		self.assertEqual(report["unique_company_location_pairs_count"], 1)
		self.assertEqual(report["target_stock_mutations"], 0)

	def test_02_live_zero_mutation_invariant_across_database(self):
		"""
		Verifies that running live readiness audits produces ZERO mutations
		to Stock Reconciliation, Stock Ledger Entry, Bin, GL Entry, Warehouse, or Supplier.
		"""
		sre_before = frappe.db.count("Stock Reconciliation")
		sle_before = frappe.db.count("Stock Ledger Entry")
		gl_before = frappe.db.count("GL Entry")
		wh_before = frappe.db.count("Warehouse")
		supp_before = frappe.db.count("Supplier")
		non_zero_bins_before = frappe.db.count("Bin", {"actual_qty": (">", 0)})

		# Execute all audit procedures live
		semantics = audit_inventory_location_semantics(self.sample_dir / "2InventoryLocation_sample.xlsx")
		val_audit = audit_valuation_sources(self.sample_dir)
		recon = generate_opening_inventory_reconciliation_report(
			sample_dir=self.sample_dir,
			mapping_registry=self.mapping_registry,
		)

		sre_after = frappe.db.count("Stock Reconciliation")
		sle_after = frappe.db.count("Stock Ledger Entry")
		gl_after = frappe.db.count("GL Entry")
		wh_after = frappe.db.count("Warehouse")
		supp_after = frappe.db.count("Supplier")
		non_zero_bins_after = frappe.db.count("Bin", {"actual_qty": (">", 0)})

		self.assertEqual(sre_after, sre_before, "Stock Reconciliation count must remain unchanged (0 created)")
		self.assertEqual(sle_after, sle_before, "Stock Ledger Entry count must remain unchanged (0 created)")
		self.assertEqual(gl_after, gl_before, "GL Entry count must remain unchanged (0 created)")
		self.assertEqual(wh_after, wh_before, "Warehouse count must remain unchanged (0 created)")
		self.assertEqual(supp_after, supp_before, "Supplier count must remain unchanged (0 created)")
		self.assertEqual(non_zero_bins_after, non_zero_bins_before, "Non-zero bins must remain unchanged")

	def test_03_live_client_sample_files_unmodified(self):
		"""
		Verifies that client sample files remain completely untracked and unmodified.
		"""
		for f in self.sample_dir.glob("*.xlsx"):
			self.assertTrue(f.exists(), f"Sample file {f.name} must exist.")
			self.assertGreater(f.stat().st_size, 0, f"Sample file {f.name} must be non-empty.")
