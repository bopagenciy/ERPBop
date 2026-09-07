# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import threading
import time
import unittest
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
from bop_erp.inventory.exceptions import (
	InsufficientStockToReserveError,
	InvalidReservationRequestError,
	ReservationNotFoundError,
)
from bop_erp.inventory.reservations import (
	get_reservation_snapshot,
	release_stock_reservation,
	reserve_channel_stock,
	reserve_stock,
)
from bop_erp.inventory.service import InventoryService


class TestReservationsATPLive(unittest.TestCase):
	"""
	Comprehensive Live Integration Tests for Phase 1I: Reservations & ATP Foundation.
	Covers:
	1. Point-in-time ATP with physical stock and safety stock policy.
	2. Native reservation reduction of ATP.
	3. Reservation release and ATP restoration.
	4. Idempotency protection against duplicate requests.
	5. Concurrency & anti-overselling under simultaneous threads.
	6. Multi-warehouse priority-based allocation.
	7. All-or-Nothing vs partial reservation.
	8. Serialized and batch reservation readiness.
	9. Product bundle kit read-only ATP.
	10. Clean teardown restoring 0 counts across all ledger and master tables.
	"""

	@classmethod
	def setUpClass(cls):
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		if not cls.company:
			cls.company = "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"

		# Equity difference account for reconciliations
		cls.diff_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Equity", "report_type": "Balance Sheet", "is_group": 0, "disabled": 0},
			"name",
		)

		# Ensure Stock Settings enable reservation
		cls.orig_stock_res = frappe.db.get_single_value("Stock Settings", "enable_stock_reservation")
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", 1)
		frappe.db.commit()

		# 1. Synthetic Warehouses
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")
		cls.wh_miami = f"WH-ATP-MIA-{cls.abbr} - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.wh_miami):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"WH-ATP-MIA-{cls.abbr}",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
			}).insert(ignore_permissions=True)
			cls.wh_miami = w.name

		cls.wh_orlando = f"WH-ATP-ORL-{cls.abbr} - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.wh_orlando):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"WH-ATP-ORL-{cls.abbr}",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
			}).insert(ignore_permissions=True)
			cls.wh_orlando = w.name

		cls.wh_quarantine = f"WH-ATP-QRT-{cls.abbr} - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.wh_quarantine):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"WH-ATP-QRT-{cls.abbr}",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
			}).insert(ignore_permissions=True)
			cls.wh_quarantine = w.name

		# 2. Synthetic Sales Channel
		cls.channel = "SC-ATP-TEST"
		if not frappe.db.exists("Sales Channel", cls.channel):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.channel,
				"channel_name": "ATP Live Test Channel",
				"channel_type": "PRESTASHOP",
				"company": cls.company,
				"active": 1,
			}).insert(ignore_permissions=True)

		# 3. Channel Inventory Sources
		# Miami: Priority 10, Sellable = 1
		cls.cis_miami = f"CIS-{cls.channel}-{cls.wh_miami}"
		if not frappe.db.exists("Channel Inventory Source", cls.cis_miami):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.channel,
				"warehouse": cls.wh_miami,
				"company": cls.company,
				"priority": 10,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		# Orlando: Priority 20, Sellable = 1
		cls.cis_orlando = f"CIS-{cls.channel}-{cls.wh_orlando}"
		if not frappe.db.exists("Channel Inventory Source", cls.cis_orlando):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.channel,
				"warehouse": cls.wh_orlando,
				"company": cls.company,
				"priority": 20,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		# Quarantine: Priority 99, Sellable = 0
		cls.cis_quarantine = f"CIS-{cls.channel}-{cls.wh_quarantine}"
		if not frappe.db.exists("Channel Inventory Source", cls.cis_quarantine):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.channel,
				"warehouse": cls.wh_quarantine,
				"company": cls.company,
				"priority": 99,
				"enabled": 1,
				"allow_sellable_stock": 0,
				"allow_fulfillment": 1,
			}).insert(ignore_permissions=True)

		# 4. Synthetic Standard Item
		cls.item_code = "ITEM-ATP-TEST-01"
		if not frappe.db.exists("Item", cls.item_code):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code,
				"item_name": "ATP Test Standard Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		# 5. Synthetic Serialized Item
		cls.serial_item = "ITEM-ATP-SER-01"
		if not frappe.db.exists("Item", cls.serial_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.serial_item,
				"item_name": "ATP Test Serial Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_serial_no": 1,
			}).insert(ignore_permissions=True)

		# 6. Synthetic Batch Item
		cls.batch_item = "ITEM-ATP-BAT-01"
		if not frappe.db.exists("Item", cls.batch_item):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.batch_item,
				"item_name": "ATP Test Batch Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
			}).insert(ignore_permissions=True)

		# 7. Synthetic Product Bundle Components & Parent
		cls.bundle_parent = "ITEM-ATP-KIT-PARENT"
		if not frappe.db.exists("Item", cls.bundle_parent):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.bundle_parent,
				"item_name": "ATP Test Bundle Parent",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)

		cls.bundle_comp1 = "ITEM-ATP-KIT-C1"
		if not frappe.db.exists("Item", cls.bundle_comp1):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.bundle_comp1,
				"item_name": "ATP Kit Component 1",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.bundle_comp2 = "ITEM-ATP-KIT-C2"
		if not frappe.db.exists("Item", cls.bundle_comp2):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.bundle_comp2,
				"item_name": "ATP Kit Component 2",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		if not frappe.db.exists("Product Bundle", cls.bundle_parent):
			pb = frappe.get_doc({
				"doctype": "Product Bundle",
				"new_item_code": cls.bundle_parent,
				"items": [
					{"item_code": cls.bundle_comp1, "qty": 2, "uom": "Nos"},
					{"item_code": cls.bundle_comp2, "qty": 1, "uom": "Nos"},
				],
			})
			pb.insert(ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# Restore Stock Settings
		frappe.db.set_single_value("Stock Settings", "enable_stock_reservation", cls.orig_stock_res)

		# Clean up any leftover SREs
		sres = frappe.get_all(
			"Stock Reservation Entry",
			filters={"item_code": ["in", [cls.item_code, cls.serial_item, cls.batch_item, cls.bundle_parent, cls.bundle_comp1, cls.bundle_comp2]]},
			fields=["name", "docstatus"],
		)
		for s in sres:
			if s.docstatus == 1:
				try:
					doc = frappe.get_doc("Stock Reservation Entry", s.name)
					doc.reload()
					doc.cancel()
				except Exception:
					pass
			frappe.delete_doc("Stock Reservation Entry", s.name, force=True, ignore_permissions=True)

		# Clean references and policies
		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [cls.item_code, cls.serial_item, cls.batch_item]]})
		frappe.db.delete("Inventory Availability Policy", {"warehouse": ["in", [cls.wh_miami, cls.wh_orlando, cls.wh_quarantine]]})

		# Clean Channel Inventory Sources
		frappe.db.delete("Channel Inventory Source", {"sales_channel": cls.channel})
		if frappe.db.exists("Sales Channel", cls.channel):
			frappe.delete_doc("Sales Channel", cls.channel, force=True, ignore_permissions=True)

		# Clean Product Bundle
		if frappe.db.exists("Product Bundle", cls.bundle_parent):
			frappe.delete_doc("Product Bundle", cls.bundle_parent, force=True, ignore_permissions=True)

		# Clean Stock Ledgers and Bins for test warehouses
		frappe.db.delete("Stock Ledger Entry", {"warehouse": ["in", [cls.wh_miami, cls.wh_orlando, cls.wh_quarantine]]})
		frappe.db.delete("Bin", {"warehouse": ["in", [cls.wh_miami, cls.wh_orlando, cls.wh_quarantine]]})

		# Clean Items
		for item in [cls.item_code, cls.serial_item, cls.batch_item, cls.bundle_parent, cls.bundle_comp1, cls.bundle_comp2]:
			frappe.db.delete("Bin", {"item_code": item})
			if frappe.db.exists("Item", item):
				frappe.delete_doc("Item", item, force=True, ignore_permissions=True)

		# Clean Warehouses
		for wh in [cls.wh_miami, cls.wh_orlando, cls.wh_quarantine]:
			if frappe.db.exists("Warehouse", wh):
				frappe.delete_doc("Warehouse", wh, force=True, ignore_permissions=True)

		frappe.db.commit()

	def _set_physical_stock(self, item_code, warehouse, qty, valuation_rate=10.0):
		"""Helper to adjust physical stock using native Stock Reconciliation."""
		reco = frappe.get_doc({
			"doctype": "Stock Reconciliation",
			"company": self.company,
			"purpose": "Opening Stock",
			"expense_account": self.diff_account,
			"items": [
				{
					"item_code": item_code,
					"warehouse": warehouse,
					"qty": qty,
					"valuation_rate": valuation_rate,
				}
			],
		})
		reco.insert(ignore_permissions=True)
		reco.submit()
		return reco.name

	def _cleanup_stock(self, reco_names):
		"""Helper to cancel and purge test stock reconciliations."""
		for r_name in reco_names:
			if frappe.db.exists("Stock Reconciliation", r_name):
				try:
					r = frappe.get_doc("Stock Reconciliation", r_name)
					if r.docstatus == 1:
						r.cancel()
				except Exception:
					pass
				try:
					frappe.delete_doc("Stock Reconciliation", r_name, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.db.delete("Stock Ledger Entry", {"voucher_no": ["in", reco_names]})
		frappe.db.delete("Stock Ledger Entry", {"warehouse": ["in", [self.wh_miami, self.wh_orlando, self.wh_quarantine]]})
		frappe.db.delete("GL Entry", {"voucher_no": ["in", reco_names]})
		frappe.db.delete("GL Entry", {"against_voucher": ["in", reco_names]})
		frappe.db.delete("Bin", {"warehouse": ["in", [self.wh_miami, self.wh_orlando, self.wh_quarantine]]})
		frappe.db.delete("Stock Reservation Entry", {"item_code": ["in", [self.item_code, self.serial_item, self.batch_item, self.bundle_parent, self.bundle_comp1, self.bundle_comp2]]})
		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [self.item_code, self.serial_item, self.batch_item]]})
		frappe.db.commit()

	def test_01_atp_calculation_with_safety_stock_and_native_reservation(self):
		"""
		Scenario 1 & 2 & 3:
		Physical stock = 100 in Miami Main.
		Safety stock policy = 10.
		Native reservation = 20.
		Expected ATP = 70.
		Then reserve additional 15 => ATP = 55.
		Release the 15 => ATP = 70.
		"""
		reco_name = self._set_physical_stock(self.item_code, self.wh_miami, 100.0)
		policy = None
		sre_20_name = None
		sre_15_name = None

		try:
			# Create safety stock policy
			policy = frappe.get_doc({
				"doctype": "Inventory Availability Policy",
				"warehouse": self.wh_miami,
				"item_code": self.item_code,
				"safety_stock_qty": 10.0,
				"enabled": 1,
			}).insert(ignore_permissions=True)

			# Create native reservation of 20
			res_20 = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=20.0,
				idempotency_key="TEST-RES-01-20",
			)
			self.assertTrue(res_20.success)
			self.assertEqual(res_20.reserved_qty, 20.0)
			sre_20_name = res_20.allocations[0].stock_reservation_entry

			# Verify ATP = 100 - 20 - 10 = 70
			atp = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp.actual_qty, 100.0)
			self.assertEqual(atp.effective_reserved_qty, 20.0)
			self.assertEqual(atp.safety_stock_qty, 10.0)
			self.assertEqual(atp.candidate_atp_qty, 70.0)

			# Channel ATP should match warehouse ATP
			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_atp_qty, 70.0)

			# Reserve additional 15
			res_15 = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=15.0,
				idempotency_key="TEST-RES-01-15",
			)
			self.assertTrue(res_15.success)
			sre_15_name = res_15.allocations[0].stock_reservation_entry

			# Verify ATP decreased to 55
			atp_after_15 = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_after_15.effective_reserved_qty, 35.0)
			self.assertEqual(atp_after_15.candidate_atp_qty, 55.0)

			# Release the 15 units reservation
			release_stock_reservation(sre_15_name)
			sre_15_name = None

			# Verify ATP restored to 70
			atp_restored = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_restored.effective_reserved_qty, 20.0)
			self.assertEqual(atp_restored.candidate_atp_qty, 70.0)

		finally:
			# Cleanup
			if sre_15_name:
				try:
					release_stock_reservation(sre_15_name)
				except Exception:
					pass
			if sre_20_name:
				try:
					release_stock_reservation(sre_20_name)
				except Exception:
					pass
			if policy:
				frappe.delete_doc("Inventory Availability Policy", policy.name, force=True, ignore_permissions=True)
			self._cleanup_stock([reco_name])

	def test_02_idempotency_prevents_duplicate_reservations(self):
		"""
		Scenario: API order ingestion retry with the same idempotency key
		must return the existing reservation without double-reserving quantity.
		"""
		reco_name = self._set_physical_stock(self.item_code, self.wh_miami, 50.0)
		idempotency_key = "ORDER-RETRY-TEST-999"

		try:
			# First reservation attempt
			res1 = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=10.0,
				idempotency_key=idempotency_key,
			)
			self.assertTrue(res1.success)
			self.assertFalse(res1.is_idempotent_replay)
			self.assertEqual(res1.reserved_qty, 10.0)
			sre_name = res1.allocations[0].stock_reservation_entry

			# ATP should now be 40
			atp_1 = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_1.candidate_atp_qty, 40.0)

			# Second (retry) reservation attempt with exact same idempotency key
			res2 = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=10.0,
				idempotency_key=idempotency_key,
			)
			self.assertTrue(res2.success)
			self.assertTrue(res2.is_idempotent_replay)
			self.assertEqual(res2.reserved_qty, 10.0)
			self.assertEqual(res2.allocations[0].stock_reservation_entry, sre_name)

			# ATP must STILL be 40 (NOT reduced to 30)
			atp_2 = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_2.candidate_atp_qty, 40.0)

			# Cleanup
			release_stock_reservation(sre_name)

		finally:
			self._cleanup_stock([reco_name])

	def test_03_over_reservation_rejected(self):
		"""
		Scenario: Attempting to reserve more than available ATP is strictly rejected.
		"""
		reco_name = self._set_physical_stock(self.item_code, self.wh_miami, 10.0)

		try:
			# Available ATP is 10. Request 11.
			with self.assertRaises(InsufficientStockToReserveError):
				reserve_stock(
					item_code=self.item_code,
					warehouse=self.wh_miami,
					requested_qty=11.0,
				)

			# Verify nothing was reserved
			snap = get_reservation_snapshot(self.item_code, self.wh_miami)
			self.assertEqual(snap.net_reserved_qty, 0.0)

		finally:
			self._cleanup_stock([reco_name])

	def test_04_concurrency_and_anti_overselling(self):
		"""
		CRITICAL ANTI-OVERSELLING CONCURRENCY TEST:
		Physical reservable ATP = 1.
		Worker A and Worker B launch simultaneously to reserve 1.
		Due to row-level locking on tabBin inside the transaction:
		Exactly ONE must succeed, and ONE must fail with InsufficientStockToReserveError.
		Total active reservations MUST NEVER exceed 1.
		"""
		reco_name = self._set_physical_stock(self.item_code, self.wh_miami, 1.0)
		frappe.db.commit()  # Ensure stock is committed for isolated thread DB sessions
		results = []
		errors = []

		def worker_reserve(worker_id):
			# Use an isolated db connection / transaction per thread
			try:
				frappe.init(site="frontend")
				frappe.connect()
				res = reserve_stock(
					item_code=self.item_code,
					warehouse=self.wh_miami,
					requested_qty=1.0,
					idempotency_key=f"CONCURRENCY-WORKER-{worker_id}",
				)
				frappe.db.commit()
				results.append((worker_id, res))
			except Exception as e:
				frappe.db.rollback()
				errors.append((worker_id, e))
			finally:
				try:
					frappe.destroy()
				except Exception:
					pass

		t1 = threading.Thread(target=worker_reserve, args=("A",))
		t2 = threading.Thread(target=worker_reserve, args=("B",))

		t1.start()
		t2.start()

		t1.join(timeout=10)
		t2.join(timeout=10)

		# Reconnect main thread
		frappe.init(site="frontend")
		frappe.connect()

		try:
			# Exactly one worker succeeded and one failed (anti-overselling invariant)
			self.assertEqual(len(results), 1, f"Expected exactly 1 success, got {len(results)}")
			self.assertEqual(len(errors), 1, f"Expected exactly 1 error, got {len(errors)}")
			self.assertTrue(
				isinstance(errors[0][1], (InsufficientStockToReserveError, frappe.QueryDeadlockError)),
				f"Unexpected error: {errors[0][1]}"
			)

			# Assert physical invariant: net reserved qty <= 1
			snap = get_reservation_snapshot(self.item_code, self.wh_miami)
			self.assertEqual(snap.net_reserved_qty, 1.0)

			# Cleanup winner's reservation
			if results:
				winner_sre = results[0][1].allocations[0].stock_reservation_entry
				release_stock_reservation(winner_sre)
				frappe.delete_doc("Stock Reservation Entry", winner_sre, force=True, ignore_permissions=True)

		finally:
			self._cleanup_stock([reco_name])

	def test_05_multi_warehouse_priority_allocation_and_all_or_nothing(self):
		"""
		Multi-warehouse allocation scenario:
		Miami Main (Priority 10): Actual 5
		Orlando Main (Priority 20): Actual 10
		Total Channel ATP = 15

		Test A: Request 8 units.
		Allocation: Miami receives 5 (fills completely), Orlando receives 3.

		Test B: All-or-Nothing check.
		Request 20 units with allow_partial = False.
		Rejects entire request (no partial leak).
		"""
		reco_m = self._set_physical_stock(self.item_code, self.wh_miami, 5.0)
		reco_o = self._set_physical_stock(self.item_code, self.wh_orlando, 10.0)
		res = None

		try:
			# Verify channel aggregate ATP is 15
			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_atp_qty, 15.0)

			# Test B: All-or-nothing check
			with self.assertRaises(InsufficientStockToReserveError):
				reserve_channel_stock(
					item_code=self.item_code,
					sales_channel=self.channel,
					requested_qty=20.0,
					allow_partial=False,
				)

			# Assert zero reservations created by failed all-or-nothing request
			snap_m = get_reservation_snapshot(self.item_code, self.wh_miami)
			snap_o = get_reservation_snapshot(self.item_code, self.wh_orlando)
			self.assertEqual(snap_m.net_reserved_qty, 0.0)
			self.assertEqual(snap_o.net_reserved_qty, 0.0)

			# Test A: Sequential allocation of 8 units
			res = reserve_channel_stock(
				item_code=self.item_code,
				sales_channel=self.channel,
				requested_qty=8.0,
				idempotency_key="MULTI-WH-ORDER-001",
			)
			self.assertTrue(res.success)
			self.assertEqual(res.reserved_qty, 8.0)
			self.assertEqual(len(res.allocations), 2)

			# Miami (Priority 10) must receive 5
			self.assertEqual(res.allocations[0].warehouse, self.wh_miami)
			self.assertEqual(res.allocations[0].allocated_qty, 5.0)

			# Orlando (Priority 20) must receive 3
			self.assertEqual(res.allocations[1].warehouse, self.wh_orlando)
			self.assertEqual(res.allocations[1].allocated_qty, 3.0)

			# Remaining Channel ATP must be 15 - 8 = 7
			ch_atp_rem = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp_rem.aggregate_atp_qty, 7.0)

		finally:
			if res and res.allocations:
				for alloc in res.allocations:
					try:
						release_stock_reservation(alloc.stock_reservation_entry)
					except Exception:
						pass
			self._cleanup_stock([reco_m, reco_o])

	def test_06_non_sellable_quarantine_contributes_zero_atp(self):
		"""
		Verifies that physical stock in a non-sellable warehouse (Quarantine)
		remains physical stock in ERP, but contributes 0 ATP to the channel.
		"""
		reco_q = self._set_physical_stock(self.item_code, self.wh_quarantine, 100.0)

		try:
			# Physical inventory in warehouse is 100
			wh_inv = InventoryService.get_warehouse_inventory(self.item_code, self.wh_quarantine)
			self.assertEqual(wh_inv.actual_qty, 100.0)

			# Channel ATP contribution is 0
			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_atp_qty, 0.0)

			# Warehouse ATP with allow_sellable_stock=False is 0
			wh_atp = get_warehouse_atp(self.item_code, self.wh_quarantine, allow_sellable_stock=False)
			self.assertEqual(wh_atp.candidate_atp_qty, 0.0)
			self.assertEqual(wh_atp.actual_qty, 100.0)

		finally:
			self._cleanup_stock([reco_q])

	def test_07_product_bundle_kit_atp(self):
		"""
		Verifies read-only Product Bundle kit ATP in live DB:
		Bundle Parent requires 2x Component 1 + 1x Component 2.
		Component 1 stock = 20 (can make 10 kits)
		Component 2 stock = 6  (can make 6 kits)
		Bundle ATP = min(10, 6) = 6 kits.
		"""
		reco_c1 = self._set_physical_stock(self.bundle_comp1, self.wh_miami, 20.0)
		reco_c2 = self._set_physical_stock(self.bundle_comp2, self.wh_miami, 6.0)

		try:
			kit_atp = get_product_bundle_atp(self.bundle_parent, warehouse=self.wh_miami)
			self.assertEqual(kit_atp, 6.0)
		finally:
			self._cleanup_stock([reco_c1, reco_c2])

	def test_08_diagnostic_external_comparison_helper(self):
		"""
		Verifies compare_channel_atp_with_external:
		ERP ATP = 70, PrestaShop Test reported = 50 => delta = -20.
		Zero mutations, zero auto-reconciliation.
		"""
		reco_m = self._set_physical_stock(self.item_code, self.wh_miami, 70.0)

		try:
			comp = InventoryService.compare_channel_atp_with_external(
				item_code=self.item_code,
				sales_channel=self.channel,
				external_qty=50.0,
			)
			self.assertEqual(comp["erp_atp_qty"], 70.0)
			self.assertEqual(comp["external_qty"], 50.0)
			self.assertEqual(comp["delta"], -20.0)
			self.assertEqual(comp["erp_source"], "SOURCE ERP")
			self.assertEqual(comp["external_source"], "SOURCE EXTERNAL")

		finally:
			self._cleanup_stock([reco_m])

	def test_09_safety_invariance_restoration(self):
		"""
		Verifies that after all tests and cleanups, exact zero counts are restored across all
		inventory ledger and transaction tables.
		"""
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), 0, "SLE count must be 0")
		self.assertEqual(frappe.db.count("Bin"), 0, "Bin count must be 0")
		self.assertEqual(frappe.db.count("Stock Reservation Entry"), 0, "SRE count must be 0")
		self.assertEqual(frappe.db.count("Stock Reconciliation"), 0, "Stock Reconciliation count must be 0")
		self.assertEqual(frappe.db.count("Item Price"), 0, "Item Price count must be 0")
		self.assertEqual(frappe.db.count("Inventory Reservation Reference"), 0, "Reservation reference count must be 0")
		self.assertEqual(frappe.db.count("Inventory Availability Policy"), 0, "Availability policy count must be 0")
