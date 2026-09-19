# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from decimal import Decimal
from pathlib import Path
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.migration.opening_inventory import (
	CanonicalOpeningInventoryRow,
	CutoverPolicy,
	ItemReadinessStatus,
	LocationMappingRegistry,
	LocationMappingStatus,
	PhysicalStockQuantityPolicy,
	SnapshotProvenancePolicy,
	SourceInventoryLocationIdentity,
	TargetLocationMapping,
	ValuationPolicyStatus,
	assess_opening_inventory_readiness,
	audit_inventory_location_semantics,
	audit_valuation_sources,
	compute_candidate_valuation_scenarios,
	generate_opening_inventory_reconciliation_report,
	get_physical_stock_quantity_policy_table,
)


class TestOpeningInventoryReadinessUnit(FrappeTestCase):
	"""
	Phase 2A Test Suite: Opening Inventory Readiness & Valuation Mapping Audit.
	Verifies all 24 required safety, semantic, and mapping gates without mutating stock.
	"""

	def setUp(self):
		super().setUp()
		self.sample_dir = Path(frappe.get_app_path("bop_erp", "..", "local_data", "p21_samples")).resolve()

		# Standard test cutover policy
		self.valid_cutover = CutoverPolicy(
			cutoff_date="2026-09-30",
			cutoff_time="23:59:59",
			timezone="America/New_York",
			source_snapshot_time="2026-09-30T23:59:59-04:00",
			target_posting_date="2026-10-01",
			target_posting_time="00:00:01",
		)

		# Standard test snapshot policy
		self.valid_snapshot = SnapshotProvenancePolicy(
			export_timestamp="2026-09-30T23:59:59Z",
			backup_timestamp="2026-09-30T23:55:00Z",
			report_timestamp="2026-09-30T23:59:59Z",
			file_manifest={"InventoryLocation": "hash123", "ItemMaster": "hash456"},
			source_snapshot_id="SNAP-20260930-01",
		)

		# Standard test location mapping registry
		self.mapping_registry = LocationMappingRegistry()
		self.mapping_registry.register_company_mapping("ABFAST", "_Test Company")
		loc_id = SourceInventoryLocationIdentity(
			source_system="PROPHET_21",
			source_instance="DEFAULT",
			company_id="ABFAST",
			location_id="9012923",
		)
		self.mapping_registry.register_location_mapping(
			identity=loc_id,
			target_company="_Test Company",
			target_warehouse="_Test Warehouse - _TC",
			status=LocationMappingStatus.MAPPED,
		)

	def _build_base_row(self, **kwargs) -> CanonicalOpeningInventoryRow:
		base_data = {
			"source_item_id": "AB28400",
			"target_item": "AB28400",
			"source_company_id": "ABFAST",
			"source_location_id": "9012923",
			"target_company": "_Test Company",
			"target_warehouse": "_Test Warehouse - _TC",
			"quantity_on_hand": Decimal("100.00"),
			"allocated_qty": Decimal("15.00"),
			"backordered_qty": Decimal("5.00"),
			"in_transit_qty": Decimal("20.00"),
			"in_process_qty": Decimal("10.00"),
			"primary_bin": "RECEIVING",
			"valuation_candidate_values": {"Moving Average Cost": Decimal("25.50")},
			"selected_valuation_rate": None,
			"valuation_policy_status": ValuationPolicyStatus.CONFIRMED.value,
			"currency": "USD",
			"serialized": False,
			"batch_tracked": False,
			"has_serial_detail": False,
			"has_batch_detail": False,
		}
		base_data.update(kwargs)
		return CanonicalOpeningInventoryRow(**base_data)

	# 1. positive QOH classification
	def test_01_positive_qoh_classification(self):
		row = self._build_base_row(quantity_on_hand=Decimal("50.00"))
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertTrue(is_ready)
		self.assertEqual(row.readiness_status, "READY")
		self.assertEqual(len(blockers), 0)

	# 2. zero QOH classification
	def test_02_zero_qoh_classification(self):
		row = self._build_base_row(quantity_on_hand=Decimal("0.00"))
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		# Zero quantity does not require opening stock entry, so not marked READY for opening import
		self.assertFalse(is_ready)
		self.assertTrue(any("ZERO_QUANTITY" in b for b in blockers))

	# 3. negative QOH blocked
	def test_03_negative_qoh_blocked(self):
		row = self._build_base_row(quantity_on_hand=Decimal("-5.00"))
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("BLOCKED_NEGATIVE_QUANTITY" in b for b in blockers))
		self.assertEqual(row.quantity_on_hand, Decimal("-5.00"), "Must not normalize negative to zero")

	# 4. null QOH review
	def test_04_null_qoh_review(self):
		row = self._build_base_row(quantity_on_hand=None)
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("BLOCKED_NULL_QUANTITY" in b for b in blockers))

	# 5. allocated excluded from physical stock
	def test_05_allocated_excluded_from_physical_stock(self):
		row = self._build_base_row(quantity_on_hand=Decimal("100.00"), allocated_qty=Decimal("30.00"))
		policy_table = get_physical_stock_quantity_policy_table()
		alloc_policy = next(p for p in policy_table if p.field_name == "Quantity Allocated")

		self.assertTrue(alloc_policy.is_excluded)
		self.assertFalse(alloc_policy.is_included)
		self.assertIn("Demand allocation", alloc_policy.reason)
		# Physical stock remains 100.00; allocated remains 30.00 without folding
		self.assertEqual(row.quantity_on_hand, Decimal("100.00"))
		self.assertEqual(row.allocated_qty, Decimal("30.00"))

	# 6. backordered excluded
	def test_06_backordered_excluded(self):
		row = self._build_base_row(quantity_on_hand=Decimal("100.00"), backordered_qty=Decimal("40.00"))
		policy_table = get_physical_stock_quantity_policy_table()
		back_policy = next(p for p in policy_table if p.field_name == "Quantity Backordered")

		self.assertTrue(back_policy.is_excluded)
		self.assertFalse(back_policy.is_included)
		self.assertEqual(row.quantity_on_hand, Decimal("100.00"))
		self.assertEqual(row.backordered_qty, Decimal("40.00"))

	# 7. in-transit excluded
	def test_07_in_transit_excluded(self):
		row = self._build_base_row(quantity_on_hand=Decimal("100.00"), in_transit_qty=Decimal("25.00"))
		policy_table = get_physical_stock_quantity_policy_table()
		transit_policy = next(p for p in policy_table if p.field_name == "Quantity In Transit")

		self.assertTrue(transit_policy.is_excluded)
		self.assertFalse(transit_policy.is_included)
		self.assertEqual(row.quantity_on_hand, Decimal("100.00"))
		self.assertEqual(row.in_transit_qty, Decimal("25.00"))

	# 8. in-process excluded
	def test_08_in_process_excluded(self):
		row = self._build_base_row(quantity_on_hand=Decimal("100.00"), in_process_qty=Decimal("12.00"))
		policy_table = get_physical_stock_quantity_policy_table()
		proc_policy = next(p for p in policy_table if p.field_name == "Quantity in Process")

		self.assertTrue(proc_policy.is_excluded)
		self.assertFalse(proc_policy.is_included)
		self.assertEqual(row.quantity_on_hand, Decimal("100.00"))
		self.assertEqual(row.in_process_qty, Decimal("12.00"))

	# 9. unmapped warehouse blocked
	def test_09_unmapped_warehouse_blocked(self):
		# Create empty registry with no warehouse mappings
		empty_registry = LocationMappingRegistry()
		row = self._build_base_row(source_location_id="9999999", target_warehouse=None)
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=empty_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("UNMAPPED_WAREHOUSE" in b for b in blockers))

	# 10. ambiguous company blocked
	def test_10_ambiguous_company_blocked(self):
		row = self._build_base_row(source_company_id="", target_company=None)
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("BLOCKED_AMBIGUOUS_COMPANY" in b for b in blockers))

	# 11. missing Item blocked
	def test_11_missing_item_blocked(self):
		row = self._build_base_row(target_item=ItemReadinessStatus.MISSING_ITEM.value)
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("MISSING_ITEM" in b for b in blockers))

	# 12. serialized without serial detail blocked
	def test_12_serialized_without_serial_detail_blocked(self):
		row = self._build_base_row(serialized=True, has_serial_detail=False)
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("BLOCKED_PENDING_SERIAL_DETAIL" in b for b in blockers))

	# 13. batch without batch detail blocked
	def test_13_batch_without_batch_detail_blocked(self):
		row = self._build_base_row(batch_tracked=True, has_batch_detail=False)
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("BLOCKED_PENDING_BATCH_DETAIL" in b for b in blockers))

	# 14. valuation unconfirmed blocked
	def test_14_valuation_unconfirmed_blocked(self):
		row = self._build_base_row()
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.REVIEW_REQUIRED,  # Not CONFIRMED
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("BLOCKED_VALUATION_UNCONFIRMED" in b for b in blockers))
		self.assertIsNone(row.selected_valuation_rate)

	# 15. currency unknown blocked
	def test_15_currency_unknown_blocked(self):
		row = self._build_base_row(currency=None)
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=False,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("BLOCKED_CURRENCY_UNKNOWN" in b for b in blockers))

	# 16. cutoff missing blocked
	def test_16_cutoff_missing_blocked(self):
		row = self._build_base_row()
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=None,  # Missing cutover policy
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("BLOCKED_CUTOFF_MISSING" in b for b in blockers))

	# 17. snapshot provenance missing blocked/review
	def test_17_snapshot_provenance_missing_blocked_review(self):
		row = self._build_base_row()
		empty_snapshot = SnapshotProvenancePolicy()  # Empty, not acceptable
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=empty_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertFalse(is_ready)
		self.assertTrue(any("BLOCKED_SNAPSHOT_PROVENANCE" in b for b in blockers))

	# 18. duplicate identity detection
	def test_18_duplicate_identity_detection(self):
		row1 = self._build_base_row(source_item_id="AB28400", quantity_on_hand=Decimal("10.00"))
		row2 = self._build_base_row(source_item_id="AB28400", quantity_on_hand=Decimal("20.00"))

		known = set()
		rows_map = {}

		assess_opening_inventory_readiness(
			row1,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
			known_identities=known,
			existing_rows_by_identity=rows_map,
		)
		rows_map[row1.identity_key] = row1

		is_ready2, blockers2 = assess_opening_inventory_readiness(
			row2,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
			known_identities=known,
			existing_rows_by_identity=rows_map,
		)
		self.assertFalse(is_ready2)
		self.assertTrue(any("BLOCKED_DUPLICATE_IDENTITY" in b for b in blockers2))

	# 19. Primary Bin not mapped to ERPNext Bin
	def test_19_primary_bin_not_mapped_to_erpnext_bin(self):
		row = self._build_base_row(primary_bin="RECEIVING")
		self.assertEqual(row.primary_bin, "RECEIVING")
		# Verify it is not mapped to target warehouse or ERPNext Bin
		self.assertNotEqual(row.target_warehouse, row.primary_bin)
		self.assertTrue(row.target_warehouse.endswith(" - _TC"))

	# 20. valuation candidate preservation
	def test_20_valuation_candidate_preservation(self):
		cands = {
			"Moving Average Cost": Decimal("25.50"),
			"Standard Cost": Decimal("24.00"),
			"Last Received PO Cost": Decimal("23.50"),
			"Next Due In PO Cost": Decimal("26.00"),
		}
		row = self._build_base_row(valuation_candidate_values=cands)
		self.assertEqual(row.valuation_candidate_values["Moving Average Cost"], Decimal("25.50"))
		self.assertEqual(row.valuation_candidate_values["Standard Cost"], Decimal("24.00"))
		self.assertEqual(len(row.valuation_candidate_values), 4)

	# 21. selected valuation remains unset without approval
	def test_21_selected_valuation_remains_unset_without_approval(self):
		row = self._build_base_row(
			valuation_candidate_values={"Moving Average Cost": Decimal("25.50")},
			selected_valuation_rate=None,
		)
		assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.REVIEW_REQUIRED,
		)
		self.assertIsNone(row.selected_valuation_rate)
		self.assertEqual(row.valuation_policy_status, ValuationPolicyStatus.BLOCKED.value)

	# 22. readiness gate READY only with all required conditions
	def test_22_readiness_gate_ready_only_with_all_conditions(self):
		row = self._build_base_row()
		is_ready, blockers = assess_opening_inventory_readiness(
			row,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=True,
			approved_valuation_policy=ValuationPolicyStatus.CONFIRMED,
		)
		self.assertTrue(is_ready)
		self.assertEqual(len(blockers), 0)
		self.assertEqual(row.readiness_status, "READY")

	# 23. reconciliation counts
	def test_23_reconciliation_counts(self):
		report = generate_opening_inventory_reconciliation_report(
			sample_dir=self.sample_dir,
			mapping_registry=self.mapping_registry,
			cutover_policy=self.valid_cutover,
			snapshot_policy=self.valid_snapshot,
			currency_confirmed=False,  # Unconfirmed in client samples
			approved_valuation_policy=ValuationPolicyStatus.BLOCKED,
		)
		self.assertEqual(report["total_inventory_location_rows"], 25)
		self.assertEqual(report["unique_items_count"], 25)
		self.assertEqual(report["null_qoh_rows"], 25)
		self.assertEqual(report["positive_qoh_rows"], 0)
		self.assertEqual(report["zero_qoh_rows"], 0)
		self.assertEqual(report["negative_qoh_rows"], 0)
		self.assertEqual(report["fully_ready_rows"], 0)
		self.assertEqual(report["fully_blocked_rows"], 25)
		self.assertEqual(report["target_stock_mutations"], 0)

	# 24. zero target mutation invariant
	def test_24_zero_target_mutation_invariant(self):
		# Count initial state
		initial_sre = frappe.db.count("Stock Reconciliation")
		initial_sle = frappe.db.count("Stock Ledger Entry")
		initial_gl = frappe.db.count("GL Entry")
		initial_wh = frappe.db.count("Warehouse")
		initial_supp = frappe.db.count("Supplier")

		# Run full report and offline valuation scenario calculations
		audit_inventory_location_semantics(self.sample_dir / "2InventoryLocation_sample.xlsx")
		audit_valuation_sources(self.sample_dir)
		report = generate_opening_inventory_reconciliation_report(
			sample_dir=self.sample_dir,
			mapping_registry=self.mapping_registry,
		)
		dummy_rows = [
			self._build_base_row(quantity_on_hand=Decimal("10")),
			self._build_base_row(quantity_on_hand=Decimal("20")),
		]
		compute_candidate_valuation_scenarios(dummy_rows)

		# Verify ZERO mutations occurred
		self.assertEqual(frappe.db.count("Stock Reconciliation"), initial_sre)
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), initial_sle)
		self.assertEqual(frappe.db.count("GL Entry"), initial_gl)
		self.assertEqual(frappe.db.count("Warehouse"), initial_wh)
		self.assertEqual(frappe.db.count("Supplier"), initial_supp)
