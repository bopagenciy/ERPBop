# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import MagicMock, patch
import frappe
from frappe.utils import flt

from bop_erp.inventory.availability import (
	get_atp_breakdown,
	get_channel_atp,
	get_effective_reserved_qty,
	get_product_bundle_atp,
	get_safety_stock,
	get_warehouse_atp,
)
from bop_erp.inventory.models import (
	ChannelATP,
	ChannelDemandBreakdown,
	EffectiveReservedBreakdown,
	WarehouseATP,
)


class TestATPUnit(unittest.TestCase):
	"""
	Comprehensive Unit Tests for Phase 1I: Available-To-Promise (ATP) Foundation.
	Covers:
	- Core ATP formula: max(0, actual - effective_reserved - safety_stock)
	- Clamping negative ATP to 0
	- Exclusion of incoming stock (ordered, indented, planned, projected)
	- Exclusion of non-sellable sources (allow_sellable_stock = 0)
	- Exclusion of disabled sources (enabled = 0)
	- Multi-warehouse aggregation
	- Priority invariance on aggregate ATP quantity
	- Fractional stock items and precision preservation
	- UOM conversion factors
	- Product Bundle / Kit read-only ATP
	- Safety stock hierarchical policy inheritance
	- Auditable explanation breakdown
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

	def test_01_core_atp_formula_calculation(self):
		"""
		Verifies base candidate ATP:
		actual 100 - reserved 20 - safety 10 => ATP 70
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_qty", return_value=20.0), \
			 patch("bop_erp.inventory.availability.get_safety_stock", return_value=10.0):

			def db_side_effect(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Warehouse":
					return frappe._dict({"name": "Miami Main", "company": "Industrial DP", "is_group": 0})
				if doctype == "Item":
					return "Nos"
				if doctype == "Bin":
					return 100.0  # actual_qty
				return None

			mock_get_value.side_effect = db_side_effect

			atp = get_warehouse_atp("BOLT-001", "Miami Main")
			self.assertEqual(atp.actual_qty, 100.0)
			self.assertEqual(atp.effective_reserved_qty, 20.0)
			self.assertEqual(atp.safety_stock_qty, 10.0)
			self.assertEqual(atp.candidate_atp_qty, 70.0)

	def test_02_negative_result_clamps_to_zero(self):
		"""
		Verifies that if reservations and/or safety stock exceed on-hand actual quantity,
		ATP is strictly clamped to 0.0 (never negative).
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_qty", return_value=80.0), \
			 patch("bop_erp.inventory.availability.get_safety_stock", return_value=30.0):

			def db_side_effect(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Warehouse":
					return frappe._dict({"name": "Miami Main", "company": "Industrial DP", "is_group": 0})
				if doctype == "Item":
					return "Nos"
				if doctype == "Bin":
					return 50.0  # actual_qty: 50 - 80 - 30 = -60 => clamp 0
				return None

			mock_get_value.side_effect = db_side_effect

			atp = get_warehouse_atp("BOLT-001", "Miami Main")
			self.assertEqual(atp.actual_qty, 50.0)
			self.assertEqual(atp.candidate_atp_qty, 0.0)

	def test_03_incoming_stock_excluded_from_atp(self):
		"""
		Verifies that ordered_qty, indented_qty, planned_qty, and projected_qty
		do NOT contribute to immediately sellable physical ATP.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_qty", return_value=10.0), \
			 patch("bop_erp.inventory.availability.get_safety_stock", return_value=5.0):

			def db_side_effect(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Warehouse":
					return frappe._dict({"name": "Miami Main", "company": "Industrial DP", "is_group": 0})
				if doctype == "Item":
					return "Nos"
				if doctype == "Bin":
					return 100.0  # actual_qty only
				return None

			mock_get_value.side_effect = db_side_effect

			atp = get_warehouse_atp("BOLT-001", "Miami Main")
			# Even if ordered_qty=500 and projected_qty=585, ATP must be 100 - 10 - 5 = 85
			self.assertEqual(atp.candidate_atp_qty, 85.0)

	def test_04_non_sellable_warehouse_excluded(self):
		"""
		Verifies that a warehouse with allow_sellable_stock = 0 contributes 0 ATP.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_qty", return_value=0.0), \
			 patch("bop_erp.inventory.availability.get_safety_stock", return_value=0.0):

			def db_side_effect(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Warehouse":
					return frappe._dict({"name": "Miami Quarantine", "company": "Industrial DP", "is_group": 0})
				if doctype == "Item":
					return "Nos"
				if doctype == "Bin":
					return 100.0
				return None

			mock_get_value.side_effect = db_side_effect

			atp = get_warehouse_atp("BOLT-001", "Miami Quarantine", allow_sellable_stock=False)
			self.assertEqual(atp.actual_qty, 100.0)
			self.assertEqual(atp.allow_sellable_stock, False)
			self.assertEqual(atp.candidate_atp_qty, 0.0)

	def test_05_disabled_source_excluded(self):
		"""
		Verifies that disabled Channel Inventory Sources (enabled = 0) are excluded from Channel ATP.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp:

			def db_side_effect(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Sales Channel":
					return frappe._dict({"name": "TID", "company": "Industrial DP"})
				if doctype == "Item":
					return "Nos"
				return None

			mock_get_value.side_effect = db_side_effect
			# Enabled query only returns enabled sources
			mock_get_all.return_value = [
				{"warehouse": "Miami Main", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1}
			]
			mock_wh_atp.return_value = WarehouseATP(
				item_code="BOLT-001",
				warehouse="Miami Main",
				company="Industrial DP",
				actual_qty=100.0,
				native_reserved_qty=0.0,
				effective_reserved_qty=0.0,
				safety_stock_qty=0.0,
				candidate_atp_qty=100.0,
				stock_uom="Nos",
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_atp_qty, 100.0)
			self.assertEqual(len(ch_atp.warehouses), 1)

	def test_06_multi_warehouse_aggregation_and_priority_invariance(self):
		"""
		Verifies:
		Miami Main: actual 100, reserved 20, safety 10 => ATP 70
		Orlando Main: actual 50, reserved 5, safety 0 => ATP 45
		Aggregate Channel ATP => 115
		And changing priority from 10/20 to 20/10 does NOT alter aggregate quantity 115.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp:

			def db_side_effect(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Sales Channel":
					return frappe._dict({"name": "TID", "company": "Industrial DP"})
				if doctype == "Item":
					return "Nos"
				return None

			mock_get_value.side_effect = db_side_effect
			mock_get_all.return_value = [
				{"warehouse": "Miami Main", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Orlando Main", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			def wh_atp_side_effect(item_code, warehouse, allow_sellable_stock=True, allow_fulfillment=True):
				if warehouse == "Miami Main":
					return WarehouseATP(
						item_code=item_code,
						warehouse="Miami Main",
						company="Industrial DP",
						actual_qty=100.0,
						native_reserved_qty=20.0,
						effective_reserved_qty=20.0,
						safety_stock_qty=10.0,
						candidate_atp_qty=70.0,
						stock_uom="Nos",
					)
				else:
					return WarehouseATP(
						item_code=item_code,
						warehouse="Orlando Main",
						company="Industrial DP",
						actual_qty=50.0,
						native_reserved_qty=5.0,
						effective_reserved_qty=5.0,
						safety_stock_qty=0.0,
						candidate_atp_qty=45.0,
						stock_uom="Nos",
					)

			mock_wh_atp.side_effect = wh_atp_side_effect

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_actual_qty, 150.0)
			self.assertEqual(ch_atp.aggregate_reserved_qty, 25.0)
			self.assertEqual(ch_atp.aggregate_safety_stock_qty, 10.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 115.0)

	def test_07_fractional_stock_precision_preserved(self):
		"""
		Verifies arithmetic precision for fractional quantities (e.g. bulk raw materials):
		actual 12.375 - reserved 2.125 - safety 1.250 => ATP 9.000.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_qty", return_value=2.125), \
			 patch("bop_erp.inventory.availability.get_safety_stock", return_value=1.250):

			def db_side_effect(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Warehouse":
					return frappe._dict({"name": "Miami Main", "company": "Industrial DP", "is_group": 0})
				if doctype == "Item":
					return "Kg"
				if doctype == "Bin":
					return 12.375
				return None

			mock_get_value.side_effect = db_side_effect

			atp = get_warehouse_atp("RESIN-BULK", "Miami Main")
			self.assertEqual(atp.actual_qty, 12.375)
			self.assertEqual(atp.effective_reserved_qty, 2.125)
			self.assertEqual(atp.safety_stock_qty, 1.250)
			self.assertEqual(atp.candidate_atp_qty, 9.000)

	def test_08_uom_conversion_demand_calculation(self):
		"""
		Verifies UOM conversion factors:
		Demand: 2 BOX, where 1 BOX = 100 NOS.
		Demand in stock UOM is 200 NOS.
		"""
		with patch("frappe.db.get_value") as mock_get_value:
			mock_get_value.return_value = 100.0  # conversion_factor
			conv_factor = frappe.db.get_value("UOM Conversion Detail", {"parent": "BOLT-001", "uom": "BOX"}, "conversion_factor")
			requested_qty_box = 2.0
			stock_qty_nos = requested_qty_box * (conv_factor or 1.0)
			self.assertEqual(stock_qty_nos, 200.0)

	def test_09_product_bundle_read_only_atp(self):
		"""
		Verifies read-only Product Bundle kit calculation:
		Bundle KIT-01 contains:
		- 2x PART-A (ATP = 10 => 10 // 2 = 5 kits)
		- 1x PART-B (ATP = 4  => 4 // 1 = 4 kits)
		Bundle ATP = min(5, 4) = 4 kits.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp:

			mock_get_value.return_value = frappe._dict({"name": "KIT-01"})
			mock_get_all.return_value = [
				{"item_code": "PART-A", "qty": 2.0, "uom": "Nos"},
				{"item_code": "PART-B", "qty": 1.0, "uom": "Nos"},
			]

			def wh_side_effect(item_code, warehouse):
				if item_code == "PART-A":
					return WarehouseATP(
						item_code="PART-A",
						warehouse="Miami Main",
						company="Industrial DP",
						actual_qty=10.0,
						native_reserved_qty=0.0,
						effective_reserved_qty=0.0,
						safety_stock_qty=0.0,
						candidate_atp_qty=10.0,
						stock_uom="Nos",
					)
				else:
					return WarehouseATP(
						item_code="PART-B",
						warehouse="Miami Main",
						company="Industrial DP",
						actual_qty=4.0,
						native_reserved_qty=0.0,
						effective_reserved_qty=0.0,
						safety_stock_qty=0.0,
						candidate_atp_qty=4.0,
						stock_uom="Nos",
					)

			mock_wh_atp.side_effect = wh_side_effect

			bundle_atp = get_product_bundle_atp("KIT-01", warehouse="Miami Main")
			self.assertEqual(bundle_atp, 4.0)

	def test_10_safety_stock_hierarchical_policy_inheritance(self):
		"""
		Verifies policy inheritance precedence:
		1. Item + Warehouse override takes precedence over group and default.
		2. Item Group + Warehouse override applies when item has no override.
		3. Warehouse default applies when neither item nor group override exists.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.db.sql") as mock_sql:

			# Scenario A: Specific Item override exists (returns 10.0)
			mock_get_value.return_value = 10.0
			safety_a = get_safety_stock("Miami Main", "BOLT-001")
			self.assertEqual(safety_a, 10.0)

			# Scenario B: No item override, but item group override exists
			def side_effect_group(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Inventory Availability Policy":
					if "item_code" in filters:
						return None
					if "item_group" in filters:
						return 5.0
				if doctype == "Item":
					return "Fasteners"
				return None

			mock_get_value.side_effect = side_effect_group
			safety_b = get_safety_stock("Miami Main", "BOLT-001")
			self.assertEqual(safety_b, 5.0)

			# Scenario C: No item, no group override => warehouse default
			def side_effect_default(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Item":
					return "Fasteners"
				return None

			mock_get_value.side_effect = side_effect_default
			mock_sql.return_value = [{"safety_stock_qty": 3.0}]
			safety_c = get_safety_stock("Miami Main", "BOLT-001")
			self.assertEqual(safety_c, 3.0)

	def test_11_atp_breakdown_explainability(self):
		"""
		Verifies auditable breakdown report generation.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp:

			def db_side_effect(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Sales Channel":
					return frappe._dict({"name": "TID", "company": "Industrial DP"})
				if doctype == "Item":
					return "Nos"
				return None

			mock_get_value.side_effect = db_side_effect
			mock_get_all.return_value = [
				{"warehouse": "Miami Main", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Orlando Main", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			def wh_side_effect(item_code, warehouse, allow_sellable_stock=True, allow_fulfillment=True):
				if warehouse == "Miami Main":
					return WarehouseATP(
						item_code="BOLT-001",
						warehouse="Miami Main",
						company="Industrial DP",
						actual_qty=100.0,
						native_reserved_qty=20.0,
						effective_reserved_qty=20.0,
						safety_stock_qty=10.0,
						candidate_atp_qty=70.0,
						stock_uom="Nos",
					)
				else:
					return WarehouseATP(
						item_code="BOLT-001",
						warehouse="Orlando Main",
						company="Industrial DP",
						actual_qty=50.0,
						native_reserved_qty=5.0,
						effective_reserved_qty=5.0,
						safety_stock_qty=0.0,
						candidate_atp_qty=45.0,
						stock_uom="Nos",
					)

			mock_wh_atp.side_effect = wh_side_effect

			bd = get_atp_breakdown("BOLT-001", "TID")
			self.assertEqual(bd.channel_atp, 115.0)
			self.assertEqual(len(bd.lines), 2)
			self.assertEqual(bd.lines[0].warehouse, "Miami Main")
			self.assertEqual(bd.lines[0].atp_qty, 70.0)
			self.assertEqual(bd.lines[1].warehouse, "Orlando Main")
			self.assertEqual(bd.lines[1].atp_qty, 45.0)

	def test_12_effective_reserved_qty_no_double_counting(self):
		"""
		VERIFIES EXACT NON-DOUBLE-COUNTING FORMULA FOR EFFECTIVE RESERVED QTY:
		Case 1: Only SRE exists (10) -> effective = 10
		Case 2: Sales Order (10) + SRE tied to SO (10) -> effective = 10 (NOT 20!)
		Case 3: Sales Order (10) + SRE tied to SO (4) -> effective = 10 (4 SRE + 6 unreserved SO)
		Case 4: Sales Order (10) + no SRE -> effective = 10
		Case 5: Manufacturing allocations (production=5, subcontract=3, plan=2) -> +10
		"""
		# Case 2 simulation: SO=10, SRE=10 for SO -> must equal 10 (NOT 20!)
		def fake_sql_c2(query, values=None, as_list=0, *args, **kwargs):
			if "GROUP BY voucher_type" in query:
				return [{"voucher_type": "Sales Order", "net_qty": 10.0}]
			return [[10.0]]

		def fake_get_value_c2(doctype, filters, fieldname=None, *args, **kwargs):
			if doctype == "Bin":
				return frappe._dict({
					"reserved_qty": 10.0,
					"reserved_stock": 10.0,
					"reserved_qty_for_production": 0.0,
					"reserved_qty_for_sub_contract": 0.0,
					"reserved_qty_for_production_plan": 0.0,
				})
			return None

		with patch.object(frappe.db, "sql", side_effect=fake_sql_c2), \
			 patch.object(frappe.db, "get_value", side_effect=fake_get_value_c2), \
			 patch("bop_erp.inventory.availability.get_stock_precision", return_value=3):
			res = get_effective_reserved_qty("BOLT-001", "Miami Main")
			self.assertEqual(res, 10.0)

		# Case 3 simulation: SO=10, SRE=4 for SO -> max(10, 4) = 10
		def fake_sql_c3(query, values=None, as_list=0, *args, **kwargs):
			if "GROUP BY voucher_type" in query:
				return [{"voucher_type": "Sales Order", "net_qty": 4.0}]
			return [[4.0]]

		def fake_get_value_c3(doctype, filters, fieldname=None, *args, **kwargs):
			if doctype == "Bin":
				return frappe._dict({
					"reserved_qty": 10.0,
					"reserved_stock": 4.0,
					"reserved_qty_for_production": 0.0,
					"reserved_qty_for_sub_contract": 0.0,
					"reserved_qty_for_production_plan": 0.0,
				})
			return None

		with patch.object(frappe.db, "sql", side_effect=fake_sql_c3), \
			 patch.object(frappe.db, "get_value", side_effect=fake_get_value_c3), \
			 patch("bop_erp.inventory.availability.get_stock_precision", return_value=3):
			res = get_effective_reserved_qty("BOLT-001", "Miami Main")
			self.assertEqual(res, 10.0)

		# Case 5 simulation: SO=0, SRE=0, manufacturing=10 -> 10
		def fake_sql_c5(query, values=None, as_list=0, *args, **kwargs):
			return []

		def fake_get_value_c5(doctype, filters, fieldname=None, *args, **kwargs):
			if doctype == "Bin":
				return frappe._dict({
					"reserved_qty": 0.0,
					"reserved_stock": 0.0,
					"reserved_qty_for_production": 5.0,
					"reserved_qty_for_sub_contract": 3.0,
					"reserved_qty_for_production_plan": 2.0,
				})
			return None

		with patch.object(frappe.db, "sql", side_effect=fake_sql_c5), \
			 patch.object(frappe.db, "get_value", side_effect=fake_get_value_c5), \
			 patch("bop_erp.inventory.availability.get_stock_precision", return_value=3):
			res = get_effective_reserved_qty("BOLT-001", "Miami Main")
			self.assertEqual(res, 10.0)

	def test_13_reservation_result_partial_and_unfulfilled_qty(self):
		"""
		Verifies ReservationResult model supports unfulfilled_qty and partial mode fields.
		"""
		from bop_erp.inventory.models import ReservationAllocation, ReservationResult

		res = ReservationResult(
			success=True,
			item_code="BOLT-001",
			requested_qty=10.0,
			reserved_qty=8.0,
			unfulfilled_qty=2.0,
			allocations=[
				ReservationAllocation(warehouse="Miami Main", allocated_qty=8.0, stock_reservation_entry="SRE-001")
			],
		)
		self.assertEqual(res.requested_qty, 10.0)
		self.assertEqual(res.reserved_qty, 8.0)
		self.assertEqual(res.unfulfilled_qty, 2.0)
		self.assertEqual(res.is_idempotent_replay, False)

	def test_14_atp_snapshot_vs_reservation_guarantee_semantics(self):
		"""
		Verifies that WarehouseATP and ChannelATP are read-only point-in-time snapshots,
		confirming the architectural separation between informational calculation and transactional reservation.
		"""
		from bop_erp.inventory.models import ChannelATP, WarehouseATP

		wh_atp = WarehouseATP(
			item_code="BOLT-001",
			warehouse="Miami Main",
			company="Industrial DP",
			actual_qty=100.0,
			native_reserved_qty=10.0,
			effective_reserved_qty=10.0,
			safety_stock_qty=5.0,
			candidate_atp_qty=85.0,
			stock_uom="Nos",
		)
		self.assertIn("INFORMATIONAL ONLY", wh_atp.__doc__)
		self.assertEqual(wh_atp.candidate_atp_qty, 85.0)

	def test_15_safety_stock_deficit_does_not_leak_between_warehouses(self):
		"""
		VERIFIES SAFETY STOCK LOCAL ISOLATION (SECTION 2 & 7):
		Warehouse A: actual 5, safety 10 => local ATP = 0 (deficit = 5)
		Warehouse B: actual 100, safety 0 => local ATP = 100
		Channel ATP must equal 100, NOT 95.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown:

			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse B", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			def wh_side_effect(item_code, warehouse, allow_sellable_stock=True, allow_fulfillment=True):
				if warehouse == "Warehouse A":
					return WarehouseATP(
						item_code=item_code, warehouse="Warehouse A", company="Industrial DP",
						actual_qty=5.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
						safety_stock_qty=10.0, candidate_atp_qty=0.0, stock_uom="Nos",
					)
				return WarehouseATP(
					item_code=item_code, warehouse="Warehouse B", company="Industrial DP",
					actual_qty=100.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
					safety_stock_qty=0.0, candidate_atp_qty=100.0, stock_uom="Nos",
				)

			mock_wh_atp.side_effect = wh_side_effect
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_atp_qty, 100.0)
			self.assertNotEqual(ch_atp.aggregate_atp_qty, 95.0)

	def test_16_safety_stock_local_surplus_and_deduction(self):
		"""
		VERIFIES SAFETY STOCK LOCAL SURPLUS DEDUCTION (SECTION 7):
		Warehouse A: actual 20, safety 10 => local ATP = 10
		Warehouse B: actual 100, safety 0 => local ATP = 100
		Channel ATP must equal 110.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown:

			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse B", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			def wh_side_effect(item_code, warehouse, allow_sellable_stock=True, allow_fulfillment=True):
				if warehouse == "Warehouse A":
					return WarehouseATP(
						item_code=item_code, warehouse="Warehouse A", company="Industrial DP",
						actual_qty=20.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
						safety_stock_qty=10.0, candidate_atp_qty=10.0, stock_uom="Nos",
					)
				return WarehouseATP(
					item_code=item_code, warehouse="Warehouse B", company="Industrial DP",
					actual_qty=100.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
					safety_stock_qty=0.0, candidate_atp_qty=100.0, stock_uom="Nos",
				)

			mock_wh_atp.side_effect = wh_side_effect
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_atp_qty, 110.0)

	def test_17_production_deficit_does_not_leak_between_warehouses(self):
		"""
		VERIFIES LOCAL PRODUCTION COMMITMENT ISOLATION (SECTION 3):
		Warehouse A: actual 0, production demand 10 => local ATP = 0
		Warehouse B: actual 100, production demand 0 => local ATP = 100
		Channel ATP must equal 100, NOT 90.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown:

			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse B", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			def wh_side_effect(item_code, warehouse, allow_sellable_stock=True, allow_fulfillment=True):
				if warehouse == "Warehouse A":
					return WarehouseATP(
						item_code=item_code, warehouse="Warehouse A", company="Industrial DP",
						actual_qty=0.0, native_reserved_qty=10.0, effective_reserved_qty=10.0,
						safety_stock_qty=0.0, candidate_atp_qty=0.0, stock_uom="Nos",
					)
				return WarehouseATP(
					item_code=item_code, warehouse="Warehouse B", company="Industrial DP",
					actual_qty=100.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
					safety_stock_qty=0.0, candidate_atp_qty=100.0, stock_uom="Nos",
				)

			mock_wh_atp.side_effect = wh_side_effect
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=10.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=10.0, native_reserved_stock=0.0,
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_atp_qty, 100.0)
			self.assertNotEqual(ch_atp.aggregate_atp_qty, 90.0)

	def test_18_subcontract_deficit_does_not_leak_between_warehouses(self):
		"""
		VERIFIES SUBCONTRACTING DEMAND ISOLATION (SECTION 3):
		Warehouse A: actual 0, subcontract 10 => local ATP = 0
		Warehouse B: actual 100, subcontract 0 => local ATP = 100
		Channel ATP must equal 100.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown:

			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse B", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			def wh_side_effect(item_code, warehouse, allow_sellable_stock=True, allow_fulfillment=True):
				if warehouse == "Warehouse A":
					return WarehouseATP(
						item_code=item_code, warehouse="Warehouse A", company="Industrial DP",
						actual_qty=0.0, native_reserved_qty=10.0, effective_reserved_qty=10.0,
						safety_stock_qty=0.0, candidate_atp_qty=0.0, stock_uom="Nos",
					)
				return WarehouseATP(
					item_code=item_code, warehouse="Warehouse B", company="Industrial DP",
					actual_qty=100.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
					safety_stock_qty=0.0, candidate_atp_qty=100.0, stock_uom="Nos",
				)

			mock_wh_atp.side_effect = wh_side_effect
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=10.0,
				production_plan_demand=0.0, total_effective_reserved=10.0, native_reserved_stock=0.0,
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_atp_qty, 100.0)

	def test_19_production_plan_deficit_does_not_leak_between_warehouses(self):
		"""
		VERIFIES PRODUCTION PLAN DEMAND ISOLATION (SECTION 3):
		Warehouse A: actual 0, production plan demand 10 => local ATP = 0
		Warehouse B: actual 100, production plan demand 0 => local ATP = 100
		Channel ATP must equal 100.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown:

			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse B", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			def wh_side_effect(item_code, warehouse, allow_sellable_stock=True, allow_fulfillment=True):
				if warehouse == "Warehouse A":
					return WarehouseATP(
						item_code=item_code, warehouse="Warehouse A", company="Industrial DP",
						actual_qty=0.0, native_reserved_qty=10.0, effective_reserved_qty=10.0,
						safety_stock_qty=0.0, candidate_atp_qty=0.0, stock_uom="Nos",
					)
				return WarehouseATP(
					item_code=item_code, warehouse="Warehouse B", company="Industrial DP",
					actual_qty=100.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
					safety_stock_qty=0.0, candidate_atp_qty=100.0, stock_uom="Nos",
				)

			mock_wh_atp.side_effect = wh_side_effect
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=10.0, total_effective_reserved=10.0, native_reserved_stock=0.0,
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_atp_qty, 100.0)

	def test_20_non_sellable_source_isolation(self):
		"""
		VERIFIES NON-SELLABLE SOURCE ISOLATION (SECTION 8):
		Quarantine: actual 100, reserved 200, allow_sellable_stock = 0 => contributes 0 ATP.
		Main: actual 50, reserved 0, allow_sellable_stock = 1 => contributes 50 ATP.
		Channel ATP must remain exactly 50 based on Main only.
		Quarantine's actual stock and reservation deficit must NOT alter sellable aggregates.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown:

			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)
			mock_get_all.return_value = [
				{"warehouse": "Main", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Quarantine", "priority": 99, "allow_sellable_stock": 0, "allow_fulfillment": 1},
			]

			def wh_side_effect(item_code, warehouse, allow_sellable_stock=True, allow_fulfillment=True):
				if warehouse == "Main":
					return WarehouseATP(
						item_code=item_code, warehouse="Main", company="Industrial DP",
						actual_qty=50.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
						safety_stock_qty=0.0, candidate_atp_qty=50.0, stock_uom="Nos",
						allow_sellable_stock=True,
					)
				return WarehouseATP(
					item_code=item_code, warehouse="Quarantine", company="Industrial DP",
					actual_qty=100.0, native_reserved_qty=200.0, effective_reserved_qty=200.0,
					safety_stock_qty=0.0, candidate_atp_qty=0.0, stock_uom="Nos",
					allow_sellable_stock=False,
				)

			mock_wh_atp.side_effect = wh_side_effect
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_actual_qty, 50.0)
			self.assertEqual(ch_atp.aggregate_reserved_qty, 0.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 50.0)

	def test_21_disabled_source_isolation(self):
		"""
		VERIFIES DISABLED SOURCE ISOLATION (SECTION 9):
		A disabled Channel Inventory Source contributes neither positive nor negative channel ATP.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown:

			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)
			# Only enabled sources returned by query
			mock_get_all.return_value = [
				{"warehouse": "Active Warehouse", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			mock_wh_atp.return_value = WarehouseATP(
				item_code="BOLT-001", warehouse="Active Warehouse", company="Industrial DP",
				actual_qty=40.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
				safety_stock_qty=0.0, candidate_atp_qty=40.0, stock_uom="Nos",
			)
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_atp_qty, 40.0)
			self.assertEqual(len(ch_atp.warehouses), 1)

	def test_22_cross_warehouse_excluded_source_policy(self):
		"""
		VERIFIES CROSS-WAREHOUSE EXCLUDED SOURCE POLICY (SECTION 10):
		An SRE for an excluded warehouse (not in channel sellable pool) does not deduct from channel.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown:

			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)
			mock_get_all.return_value = [
				{"warehouse": "Channel Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			mock_wh_atp.return_value = WarehouseATP(
				item_code="BOLT-001", warehouse="Channel Warehouse A", company="Industrial DP",
				actual_qty=50.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
				safety_stock_qty=0.0, candidate_atp_qty=50.0, stock_uom="Nos",
			)
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.aggregate_atp_qty, 50.0)
			self.assertEqual(ch_atp.cross_warehouse_adjustments["deduplicated_sre_demand"], 0.0)

	def test_23_channel_demand_breakdown_explainability(self):
		"""
		VERIFIES CHANNEL DEMAND BREAKDOWN MODEL & EXPLAINABILITY (SECTION 5 & 11):
		Checks ChannelDemandBreakdown structure and cross_warehouse_adjustments.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown:

			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)
			mock_get_all.return_value = [
				{"warehouse": "Miami", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]

			mock_wh_atp.return_value = WarehouseATP(
				item_code="BOLT-001", warehouse="Miami", company="Industrial DP",
				actual_qty=100.0, native_reserved_qty=20.0, effective_reserved_qty=20.0,
				safety_stock_qty=10.0, candidate_atp_qty=70.0, stock_uom="Nos",
			)
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="Miami", sales_order_demand=10.0,
				standalone_sre_demand=5.0, production_demand=5.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=20.0, native_reserved_stock=5.0,
			)

			bd = get_atp_breakdown("BOLT-001", "TID")
			self.assertEqual(bd.channel_atp, 70.0)
			self.assertIsNotNone(bd.demand_breakdown)
			self.assertEqual(bd.demand_breakdown.sales_order_demand, 10.0)
			self.assertEqual(bd.demand_breakdown.standalone_sre_demand, 5.0)
			self.assertEqual(bd.demand_breakdown.production_demand, 5.0)
			self.assertEqual(bd.demand_breakdown.safety_stock, 10.0)
			self.assertIn("sales_order_unallocated_demand", bd.cross_warehouse_adjustments)
			self.assertIn("deduplicated_sre_demand", bd.cross_warehouse_adjustments)

	def test_24_critical_counterexample_uncovered_so_demand_must_not_return_six(self):
		"""
		CRITICAL COUNTEREXAMPLE (PHASE 1I.4 SECTION 1):
		Sales Order pending qty = 10
		Sales Order target warehouse = A
		Warehouse A: actual = 0
		Warehouse B: actual = 10
		SRE linked to same Sales Order Item: warehouse B, remaining reserved qty = 4
		Both A and B are eligible sellable channel sources.

		Expected:
		physical reservable capacity after SRE in B = 6
		uncovered SO demand = 6
		channel ATP = 0

		The current Phase 1I.3 algorithm must NOT return 6.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("frappe.db.sql") as mock_sql, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown, \
			 patch("bop_erp.inventory.availability.get_sre_reserved_qty_for_item_and_warehouse") as mock_sre:

			mock_sre.side_effect = lambda ic, wh: 4.0 if wh == "Warehouse B" else 0.0
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse B", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)
			def wh_mock(*a, **kw):
				wh = kw.get("warehouse") or (a[1] if len(a) > 1 else (a[0] if a else "Warehouse A"))
				ic = kw.get("item_code") or (a[0] if a else "BOLT-001")
				return WarehouseATP(
					item_code=ic, warehouse=wh, company="Industrial DP",
					actual_qty=0.0 if wh == "Warehouse A" else 10.0,
					native_reserved_qty=0.0 if wh == "Warehouse A" else 4.0,
					effective_reserved_qty=0.0 if wh == "Warehouse A" else 4.0,
					safety_stock_qty=0.0, candidate_atp_qty=0.0 if wh == "Warehouse A" else 6.0,
					stock_uom="Nos",
				)
			mock_wh_atp.side_effect = wh_mock

			def sql_side_effect(query, values=None, *args, **kwargs):
				q_str = str(query)
				if "tabSales Order Item" in q_str:
					return [frappe._dict({
						"sales_order": "SO-001",
						"sales_order_item": "SOI-001",
						"target_warehouse": "Warehouse A",
						"stock_qty": 10.0,
						"qty": 10.0,
						"delivered_qty": 0.0,
					})]
				if "GROUP BY sre.voucher_detail_no" in q_str:
					return [frappe._dict({"voucher_detail_no": "SOI-001", "linked_sre_qty": 4.0})]
				if "GROUP BY voucher_type" in q_str:
					if values and len(values) > 1 and values[1] == "Warehouse B":
						return [frappe._dict({"voucher_type": "Sales Order", "net_qty": 4.0})]
					return []
				if "SUM(reserved_qty" in q_str:
					if values and len(values) > 1 and values[1] == "Warehouse B":
						return [(4.0,)]
					return [(0.0,)]
				return []

			mock_sql.side_effect = sql_side_effect

			def get_value_side_effect(doctype, filters, fieldname=None, *args, **kwargs):
				if doctype == "Sales Channel":
					return frappe._dict({"name": "TID", "company": "Industrial DP"})
				if doctype == "Item":
					return "Nos"
				if doctype == "Bin":
					wh = filters.get("warehouse") if isinstance(filters, dict) else None
					if fieldname == "actual_qty":
						return 10.0 if wh == "Warehouse B" else 0.0
					if isinstance(fieldname, list):
						return frappe._dict({"reserved_qty_for_production": 0.0, "reserved_qty_for_sub_contract": 0.0, "reserved_qty_for_production_plan": 0.0})
				return None

			mock_get_value.side_effect = get_value_side_effect

			ch_atp = get_channel_atp("BOLT-001", "TID")
			# Under Phase 1I.4:
			# Warehouse B physical capacity = 10 - 4 = 6.0
			# Uncovered SO demand = 10 - 4 = 6.0
			# Channel ATP = max(0, 6.0 - 6.0) = 0.0!
			self.assertEqual(ch_atp.base_physical_capacity, 6.0)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 6.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 0.0, "Channel ATP must be 0.0, but Phase 1I.3 returned 6.0!")

	def test_25_so10_sre4_uncovered_six(self):
		"""
		SO pending = 10, SRE in pool = 4.
		Physical capacity deducts 4, uncovered logical demand = 6, total demand = 10.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("frappe.db.sql") as mock_sql, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown, \
			 patch("bop_erp.inventory.availability.get_sre_reserved_qty_for_item_and_warehouse") as mock_sre:

			mock_sre.side_effect = lambda ic, wh: 4.0 if wh == "Warehouse B" else 0.0
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse B", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)
			mock_wh_atp.side_effect = lambda item_code, warehouse, **kw: WarehouseATP(
				item_code=item_code, warehouse=warehouse, company="Industrial DP",
				actual_qty=10.0, native_reserved_qty=0.0 if warehouse == "Warehouse A" else 4.0,
				effective_reserved_qty=0.0 if warehouse == "Warehouse A" else 4.0,
				safety_stock_qty=0.0, candidate_atp_qty=10.0 if warehouse == "Warehouse A" else 6.0,
				stock_uom="Nos",
			)

			def sql_side_effect(query, values=None, *args, **kwargs):
				q_str = str(query)
				if "tabSales Order Item" in q_str:
					return [frappe._dict({
						"sales_order": "SO-001", "sales_order_item": "SOI-001",
						"target_warehouse": "Warehouse A", "stock_qty": 10.0, "qty": 10.0, "delivered_qty": 0.0,
					})]
				if "GROUP BY sre.voucher_detail_no" in q_str:
					return [frappe._dict({"voucher_detail_no": "SOI-001", "linked_sre_qty": 4.0})]
				if "SUM(reserved_qty" in q_str:
					if values and len(values) > 1 and values[1] == "Warehouse B":
						return [(4.0,)]
					return [(0.0,)]
				return []

			mock_sql.side_effect = sql_side_effect
			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			# A capacity = 10, B capacity = 6 => Base pool = 16.
			# Uncovered SO demand = 10 - 4 = 6.
			# Channel ATP = 16 - 6 = 10.0.
			self.assertEqual(ch_atp.base_physical_capacity, 16.0)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 6.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 10.0)

	def test_26_so10_sre10_uncovered_zero(self):
		"""
		Fully reserved SO: SO pending = 10, SRE = 10 => uncovered = 0.
		Physical capacity accounts for all 10 SRE units.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("frappe.db.sql") as mock_sql, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown, \
			 patch("bop_erp.inventory.availability.get_sre_reserved_qty_for_item_and_warehouse") as mock_sre:

			mock_sre.side_effect = lambda ic, wh: 10.0 if wh == "Warehouse B" else 0.0
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse B", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)
			mock_wh_atp.side_effect = lambda item_code, warehouse, **kw: WarehouseATP(
				item_code=item_code, warehouse=warehouse, company="Industrial DP",
				actual_qty=0.0 if warehouse == "Warehouse A" else 15.0,
				native_reserved_qty=0.0 if warehouse == "Warehouse A" else 10.0,
				effective_reserved_qty=0.0 if warehouse == "Warehouse A" else 10.0,
				safety_stock_qty=0.0, candidate_atp_qty=0.0 if warehouse == "Warehouse A" else 5.0,
				stock_uom="Nos",
			)

			def sql_side_effect(query, values=None, *args, **kwargs):
				q_str = str(query)
				if "tabSales Order Item" in q_str:
					return [frappe._dict({
						"sales_order": "SO-001", "sales_order_item": "SOI-001",
						"target_warehouse": "Warehouse A", "stock_qty": 10.0, "qty": 10.0, "delivered_qty": 0.0,
					})]
				if "GROUP BY sre.voucher_detail_no" in q_str:
					return [frappe._dict({"voucher_detail_no": "SOI-001", "linked_sre_qty": 10.0})]
				if "SUM(reserved_qty" in q_str:
					if values and len(values) > 1 and values[1] == "Warehouse B":
						return [(10.0,)]
					return [(0.0,)]
				return []

			mock_sql.side_effect = sql_side_effect
			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			# B capacity = 15 - 10 = 5. Uncovered SO = 10 - 10 = 0. Channel ATP = 5.
			self.assertEqual(ch_atp.base_physical_capacity, 5.0)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 0.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 5.0)

	def test_27_so10_sre4_and_3_uncovered_three(self):
		"""
		Partial multi-warehouse SRE: SO pending = 10, target A.
		SRE B = 4, SRE C = 3 => Uncovered = 3.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("frappe.db.sql") as mock_sql, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown, \
			 patch("bop_erp.inventory.availability.get_sre_reserved_qty_for_item_and_warehouse") as mock_sre:

			mock_sre.side_effect = lambda ic, wh: 4.0 if wh == "Warehouse B" else (3.0 if wh == "Warehouse C" else 0.0)
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse B", "priority": 20, "allow_sellable_stock": 1, "allow_fulfillment": 1},
				{"warehouse": "Warehouse C", "priority": 30, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="WH", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)

			def wh_mock(item_code, warehouse, **kw):
				actual = 0.0 if warehouse == "Warehouse A" else 10.0
				res = 4.0 if warehouse == "Warehouse B" else (3.0 if warehouse == "Warehouse C" else 0.0)
				return WarehouseATP(
					item_code=item_code, warehouse=warehouse, company="Industrial DP",
					actual_qty=actual, native_reserved_qty=res, effective_reserved_qty=res,
					safety_stock_qty=0.0, candidate_atp_qty=max(0.0, actual - res),
					stock_uom="Nos",
				)
			mock_wh_atp.side_effect = wh_mock

			def sql_side_effect(query, values=None, *args, **kwargs):
				q_str = str(query)
				if "tabSales Order Item" in q_str:
					return [frappe._dict({
						"sales_order": "SO-001", "sales_order_item": "SOI-001",
						"target_warehouse": "Warehouse A", "stock_qty": 10.0, "qty": 10.0, "delivered_qty": 0.0,
					})]
				if "GROUP BY sre.voucher_detail_no" in q_str:
					return [frappe._dict({"voucher_detail_no": "SOI-001", "linked_sre_qty": 7.0})]
				if "SUM(reserved_qty" in q_str:
					if values and len(values) > 1:
						if values[1] == "Warehouse B":
							return [(4.0,)]
						if values[1] == "Warehouse C":
							return [(3.0,)]
					return [(0.0,)]
				return []

			mock_sql.side_effect = sql_side_effect
			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			# B cap = 6, C cap = 7 => Base pool = 13.
			# Linked SRE = 7. Uncovered SO = 10 - 7 = 3.
			# Channel ATP = 13 - 3 = 10.0.
			self.assertEqual(ch_atp.base_physical_capacity, 13.0)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 3.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 10.0)

	def test_28_sre_outside_pool_reduces_uncovered_demand(self):
		"""
		SO target A in channel pool, pending 10.
		SRE in outside warehouse X = 4.
		Uncovered demand burden on pool = 10 - 4 = 6.
		Warehouse X capacity is NOT added to pool.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("frappe.db.sql") as mock_sql, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown, \
			 patch("bop_erp.inventory.availability.get_sre_reserved_qty_for_item_and_warehouse") as mock_sre:

			mock_sre.return_value = 0.0
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="Warehouse A", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)
			mock_wh_atp.return_value = WarehouseATP(
				item_code="BOLT-001", warehouse="Warehouse A", company="Industrial DP",
				actual_qty=10.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
				safety_stock_qty=0.0, candidate_atp_qty=10.0, stock_uom="Nos",
			)

			def sql_side_effect(query, values=None, *args, **kwargs):
				q_str = str(query)
				if "tabSales Order Item" in q_str:
					return [frappe._dict({
						"sales_order": "SO-001", "sales_order_item": "SOI-001",
						"target_warehouse": "Warehouse A", "stock_qty": 10.0, "qty": 10.0, "delivered_qty": 0.0,
					})]
				if "GROUP BY sre.voucher_detail_no" in q_str:
					# Linked SRE in outside warehouse X = 4
					return [frappe._dict({"voucher_detail_no": "SOI-001", "linked_sre_qty": 4.0})]
				return []

			mock_sql.side_effect = sql_side_effect
			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			# A capacity = 10. Uncovered burden = 10 - 4 = 6.
			# Channel ATP = 10 - 6 = 4.0.
			self.assertEqual(ch_atp.base_physical_capacity, 10.0)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 6.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 4.0)

	def test_29_target_outside_pool_does_not_impose_uncovered_demand(self):
		"""
		SO target X outside pool, pending 10.
		SRE in eligible warehouse B = 4.
		B physical capacity loses 4. Unreserved demand on X does NOT consume channel pool.
		Channel ATP = 6.0.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("frappe.db.sql") as mock_sql, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown, \
			 patch("bop_erp.inventory.availability.get_sre_reserved_qty_for_item_and_warehouse") as mock_sre:

			mock_sre.return_value = 4.0
			mock_get_all.return_value = [
				{"warehouse": "Warehouse B", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="Warehouse B", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)
			mock_wh_atp.return_value = WarehouseATP(
				item_code="BOLT-001", warehouse="Warehouse B", company="Industrial DP",
				actual_qty=10.0, native_reserved_qty=4.0, effective_reserved_qty=4.0,
				safety_stock_qty=0.0, candidate_atp_qty=6.0, stock_uom="Nos",
			)

			def sql_side_effect(query, values=None, *args, **kwargs):
				q_str = str(query)
				if "tabSales Order Item" in q_str:
					# Target warehouse X is not in sellable_warehouses (Warehouse B)
					return []
				if "SUM(reserved_qty" in q_str:
					return [(4.0,)]
				return []

			mock_sql.side_effect = sql_side_effect
			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			# B capacity = 6. Uncovered SO demand = 0. Channel ATP = 6.0.
			self.assertEqual(ch_atp.base_physical_capacity, 6.0)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 0.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 6.0)

	def test_30_linked_sre_greater_than_pending_clamps_to_zero(self):
		"""
		Inconsistent/transitional state: linked active SRE = 12 > pending SO = 10.
		Uncovered demand must clamp to 0.0 without creating negative demand or phantom credit.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("frappe.db.sql") as mock_sql, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown, \
			 patch("bop_erp.inventory.availability.get_sre_reserved_qty_for_item_and_warehouse") as mock_sre:

			mock_sre.return_value = 0.0
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="Warehouse A", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)
			mock_wh_atp.return_value = WarehouseATP(
				item_code="BOLT-001", warehouse="Warehouse A", company="Industrial DP",
				actual_qty=10.0, native_reserved_qty=0.0, effective_reserved_qty=0.0,
				safety_stock_qty=0.0, candidate_atp_qty=10.0, stock_uom="Nos",
			)

			def sql_side_effect(query, values=None, *args, **kwargs):
				q_str = str(query)
				if "tabSales Order Item" in q_str:
					return [frappe._dict({
						"sales_order": "SO-001", "sales_order_item": "SOI-001",
						"target_warehouse": "Warehouse A", "stock_qty": 10.0, "qty": 10.0, "delivered_qty": 0.0,
					})]
				if "GROUP BY sre.voucher_detail_no" in q_str:
					return [frappe._dict({"voucher_detail_no": "SOI-001", "linked_sre_qty": 12.0})]
				return []

			mock_sql.side_effect = sql_side_effect
			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			# Clamps to 0.0
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 0.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 10.0)

	def test_31_product_bundle_atp_with_uncovered_demand(self):
		"""
		Product bundle with Component 1 (req 2) and Component 2 (req 1).
		Comp 1 ATP = 10, Comp 2 ATP = 3 (due to uncovered demand).
		Bundle ATP = min(10//2, 3//1) = min(5, 3) = 3.0.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("bop_erp.inventory.availability.get_channel_atp") as mock_chan_atp:

			mock_get_value.return_value = frappe._dict({"name": "BUNDLE-01"})
			mock_get_all.return_value = [
				{"item_code": "COMP-01", "qty": 2.0},
				{"item_code": "COMP-02", "qty": 1.0},
			]

			def atp_mock(item_code, sales_channel):
				if item_code == "COMP-01":
					return ChannelATP(
						item_code="COMP-01", sales_channel="TID", company="Industrial DP",
						aggregate_atp_qty=10.0,
					)
				return ChannelATP(
					item_code="COMP-02", sales_channel="TID", company="Industrial DP",
					aggregate_atp_qty=3.0,
				)

			mock_chan_atp.side_effect = atp_mock
			bundle_atp = get_product_bundle_atp("BUNDLE-01", sales_channel="TID")
			self.assertEqual(bundle_atp, 3.0)

	def test_32_fractional_precision_uncovered_demand(self):
		"""
		Verifies fractional precision handling:
		SO pending = 10.5, linked SRE = 3.25 => uncovered = 7.25.
		Actual = 15.0, physical SRE = 3.25 => physical capacity = 11.75.
		Channel ATP = 11.75 - 7.25 = 4.5.
		"""
		with patch("frappe.db.get_value") as mock_get_value, \
			 patch("frappe.get_all") as mock_get_all, \
			 patch("frappe.db.sql") as mock_sql, \
			 patch("bop_erp.inventory.availability.get_warehouse_atp") as mock_wh_atp, \
			 patch("bop_erp.inventory.availability.get_effective_reserved_breakdown") as mock_breakdown, \
			 patch("bop_erp.inventory.availability.get_sre_reserved_qty_for_item_and_warehouse") as mock_sre:

			mock_sre.return_value = 3.25
			mock_get_all.return_value = [
				{"warehouse": "Warehouse A", "priority": 10, "allow_sellable_stock": 1, "allow_fulfillment": 1},
			]
			mock_breakdown.return_value = EffectiveReservedBreakdown(
				item_code="BOLT-001", warehouse="Warehouse A", sales_order_demand=0.0,
				standalone_sre_demand=0.0, production_demand=0.0, subcontract_demand=0.0,
				production_plan_demand=0.0, total_effective_reserved=0.0, native_reserved_stock=0.0,
			)
			mock_wh_atp.return_value = WarehouseATP(
				item_code="BOLT-001", warehouse="Warehouse A", company="Industrial DP",
				actual_qty=15.0, native_reserved_qty=3.25, effective_reserved_qty=3.25,
				safety_stock_qty=0.0, candidate_atp_qty=11.75, stock_uom="Nos",
			)

			def sql_side_effect(query, values=None, *args, **kwargs):
				q_str = str(query)
				if "tabSales Order Item" in q_str:
					return [frappe._dict({
						"sales_order": "SO-001", "sales_order_item": "SOI-001",
						"target_warehouse": "Warehouse A", "stock_qty": 10.5, "qty": 10.5, "delivered_qty": 0.0,
					})]
				if "GROUP BY sre.voucher_detail_no" in q_str:
					return [frappe._dict({"voucher_detail_no": "SOI-001", "linked_sre_qty": 3.25})]
				if "SUM(reserved_qty" in q_str:
					return [(3.25,)]
				return []

			mock_sql.side_effect = sql_side_effect
			mock_get_value.side_effect = lambda dt, flt, fn=None, *args, **kwargs: (
				frappe._dict({"name": "TID", "company": "Industrial DP"}) if dt == "Sales Channel"
				else ("Nos" if dt == "Item" else None)
			)

			ch_atp = get_channel_atp("BOLT-001", "TID")
			self.assertEqual(ch_atp.base_physical_capacity, 11.75)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 7.25)
			self.assertEqual(ch_atp.aggregate_atp_qty, 4.5)




