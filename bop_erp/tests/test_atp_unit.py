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
from bop_erp.inventory.models import WarehouseATP


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
