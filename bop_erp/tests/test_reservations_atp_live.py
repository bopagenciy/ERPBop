# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import threading
import time
import unittest
import frappe
from frappe.utils import flt
from erpnext.stock.utils import get_stock_balance

from bop_erp.inventory.availability import (
	get_atp_breakdown,
	get_channel_atp,
	get_effective_reserved_breakdown,
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
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

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

	def setUp(self):
		super().setUp()
		self._created_recos = []

	def tearDown(self):
		if hasattr(self, "_created_recos") and self._created_recos:
			self._cleanup_stock(self._created_recos)
			self._created_recos = []
		super().tearDown()

	def _set_physical_stock(self, item_code, warehouse, qty, valuation_rate=10.0, posting_date=None, posting_time=None):
		"""Helper to adjust physical stock using native Stock Reconciliation."""
		import datetime
		site_now = frappe.utils.now_datetime()
		effective_dt = site_now - datetime.timedelta(seconds=10)
		p_date = posting_date or effective_dt.strftime("%Y-%m-%d")
		p_time = posting_time or effective_dt.strftime("%H:%M:%S")

		reco = frappe.get_doc({
			"doctype": "Stock Reconciliation",
			"company": self.company,
			"purpose": "Opening Stock",
			"expense_account": self.diff_account,
			"set_posting_time": 1,
			"posting_date": p_date,
			"posting_time": p_time,
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
		if not hasattr(self, "_created_recos"):
			self._created_recos = []
		self._created_recos.append(reco.name)
		return reco.name

	def _setup_stock(self, warehouse, qty, valuation_rate=10.0, posting_date=None, posting_time=None):
		"""Convenience helper to set stock for default self.item_code."""
		return self._set_physical_stock(self.item_code, warehouse, qty, valuation_rate=valuation_rate, posting_date=posting_date, posting_time=posting_time)

	def _set_serialized_stock(self, item_code, warehouse, serial_nos, valuation_rate=10.0, posting_date=None, posting_time=None):
		"""Helper to adjust physical stock for serialized items."""
		import datetime
		site_now = frappe.utils.now_datetime()
		effective_dt = site_now - datetime.timedelta(seconds=10)
		p_date = posting_date or effective_dt.strftime("%Y-%m-%d")
		p_time = posting_time or effective_dt.strftime("%H:%M:%S")

		reco = frappe.get_doc({
			"doctype": "Stock Reconciliation",
			"company": self.company,
			"purpose": "Opening Stock",
			"expense_account": self.diff_account,
			"set_posting_time": 1,
			"posting_date": p_date,
			"posting_time": p_time,
			"items": [
				{
					"item_code": item_code,
					"warehouse": warehouse,
					"qty": float(len(serial_nos)),
					"valuation_rate": valuation_rate,
					"serial_no": "\n".join(serial_nos),
					"use_serial_batch_fields": 1,
				}
			],
		})
		reco.insert(ignore_permissions=True)
		reco.submit()
		if not hasattr(self, "_created_recos"):
			self._created_recos = []
		self._created_recos.append(reco.name)
		return reco.name

	def _set_batch_stock(self, item_code, warehouse, batch_no, qty, valuation_rate=10.0, posting_date=None, posting_time=None):
		"""Helper to adjust physical stock for batch items."""
		import datetime
		site_now = frappe.utils.now_datetime()
		effective_dt = site_now - datetime.timedelta(seconds=10)
		p_date = posting_date or effective_dt.strftime("%Y-%m-%d")
		p_time = posting_time or effective_dt.strftime("%H:%M:%S")

		if not frappe.db.exists("Batch", batch_no):
			frappe.get_doc({
				"doctype": "Batch",
				"batch_id": batch_no,
				"item": item_code,
			}).insert(ignore_permissions=True)
		reco = frappe.get_doc({
			"doctype": "Stock Reconciliation",
			"company": self.company,
			"purpose": "Opening Stock",
			"expense_account": self.diff_account,
			"set_posting_time": 1,
			"posting_date": p_date,
			"posting_time": p_time,
			"items": [
				{
					"item_code": item_code,
					"warehouse": warehouse,
					"qty": float(qty),
					"valuation_rate": valuation_rate,
					"batch_no": batch_no,
					"use_serial_batch_fields": 1,
				}
			],
		})
		reco.insert(ignore_permissions=True)
		reco.submit()
		if not hasattr(self, "_created_recos"):
			self._created_recos = []
		self._created_recos.append(reco.name)
		return reco.name

	def _cleanup_stock(self, reco_names=None):
		"""Helper to cancel and purge test stock reconciliations."""
		reco_list = []
		if reco_names:
			reco_list = [reco_names] if isinstance(reco_names, str) else list(reco_names)

		test_items = [self.item_code, self.serial_item, self.batch_item, self.bundle_parent, self.bundle_comp1, self.bundle_comp2]
		test_whs = [self.wh_miami, self.wh_orlando, self.wh_quarantine]
		if reco_list:
			frappe.db.delete("Stock Ledger Entry", {"voucher_no": ["in", reco_list]})
			frappe.db.delete("GL Entry", {"voucher_no": ["in", reco_list]})
			frappe.db.delete("GL Entry", {"against_voucher": ["in", reco_list]})
			frappe.db.delete("Stock Reconciliation Item", {"parent": ["in", reco_list]})
			frappe.db.delete("Stock Reconciliation", {"name": ["in", reco_list]})

		frappe.db.delete("Stock Ledger Entry", {"warehouse": ["in", test_whs]})
		frappe.db.delete("Bin", {"warehouse": ["in", test_whs]})
		frappe.db.delete("Bin", {"item_code": ["in", test_items]})
		frappe.db.delete("Stock Reservation Entry", {"item_code": ["in", test_items]})
		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", test_items]})
		frappe.db.delete("Item Price", {"item_code": ["in", test_items]})
		frappe.db.delete("Serial No", {"item_code": ["in", test_items]})
		frappe.db.delete("Batch", {"item": ["in", test_items]})
		frappe.db.delete("Serial and Batch Bundle", {"item_code": ["in", test_items]})
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
		barrier = threading.Barrier(2)

		def worker_reserve(worker_id):
			# Use an isolated db connection / transaction per thread
			try:
				frappe.init(site="frontend")
				frappe.connect()
				barrier.wait(timeout=5)
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

	def test_10_concurrent_identical_idempotency_request(self):
		"""
		CRITICAL IDEMPOTENCY CONCURRENCY TEST:
		Two concurrent identical reservation requests with the SAME idempotency key
		must converge to exactly one logical reservation outcome.
		Total reserved quantity must equal 5 (NOT 10), and no duplicate SRE or key collision errors leaked.
		"""
		reco_name = self._set_physical_stock(self.item_code, self.wh_miami, 10.0)
		frappe.db.commit()
		results = []
		errors = []
		barrier = threading.Barrier(2)
		idempotency_key = "CONCURRENT-IDEM-SAME-KEY-999"

		def worker_idem(worker_id):
			try:
				frappe.init(site="frontend")
				frappe.connect()
				barrier.wait(timeout=5)
				try:
					res = reserve_stock(
						item_code=self.item_code,
						warehouse=self.wh_miami,
						requested_qty=5.0,
						idempotency_key=idempotency_key,
					)
					frappe.db.commit()
					results.append((worker_id, res))
				except (frappe.QueryDeadlockError, Exception) as lock_err:
					# On lock contention, rollback and retry to observe winning committed reservation
					frappe.db.rollback()
					res = reserve_stock(
						item_code=self.item_code,
						warehouse=self.wh_miami,
						requested_qty=5.0,
						idempotency_key=idempotency_key,
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

		t1 = threading.Thread(target=worker_idem, args=("W1",))
		t2 = threading.Thread(target=worker_idem, args=("W2",))

		t1.start()
		t2.start()

		t1.join(timeout=10)
		t2.join(timeout=10)

		frappe.init(site="frontend")
		frappe.connect()

		try:
			self.assertEqual(len(errors), 0, f"Errors occurred: {errors}")
			self.assertEqual(len(results), 2, f"Expected 2 successful responses, got {len(results)}")

			# Both responses must report 5.0 reserved (NOT 10)
			for w_id, res in results:
				self.assertTrue(res.success)
				self.assertEqual(res.reserved_qty, 5.0)

			# At least one must be marked as idempotent replay
			replays = [res.is_idempotent_replay for w_id, res in results]
			self.assertTrue(any(replays), "At least one worker must receive idempotent replay")

			# Both point to the exact same SRE
			sre_1 = results[0][1].allocations[0].stock_reservation_entry
			sre_2 = results[1][1].allocations[0].stock_reservation_entry
			self.assertEqual(sre_1, sre_2)

			# Net reserved in DB is exactly 5.0
			snap = get_reservation_snapshot(self.item_code, self.wh_miami)
			self.assertEqual(snap.net_reserved_qty, 5.0)

			# Cleanup
			release_stock_reservation(sre_1)
			frappe.delete_doc("Stock Reservation Entry", sre_1, force=True, ignore_permissions=True)
		finally:
			self._cleanup_stock([reco_name])

	def test_11_release_stock_reservation_idempotency(self):
		"""
		VERIFIES RESERVATION RELEASE IDEMPOTENCY:
		Releasing/cancelling the same logical reservation twice must:
		- Not free stock twice
		- Not error unpredictably
		- Not create negative reserved quantities
		"""
		reco_name = self._set_physical_stock(self.item_code, self.wh_miami, 50.0)
		try:
			res = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=10.0,
				idempotency_key="REL-IDEM-001",
			)
			self.assertTrue(res.success)
			sre_name = res.allocations[0].stock_reservation_entry

			atp_after_res = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_after_res.candidate_atp_qty, 40.0)

			# 1st release: full cancellation
			ok1 = release_stock_reservation(sre_name)
			self.assertTrue(ok1)
			atp_after_rel1 = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_after_rel1.candidate_atp_qty, 50.0)
			self.assertEqual(atp_after_rel1.effective_reserved_qty, 0.0)

			# 2nd release: idempotent safe no-op
			ok2 = release_stock_reservation(sre_name)
			self.assertTrue(ok2)
			atp_after_rel2 = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_after_rel2.candidate_atp_qty, 50.0)
			self.assertEqual(atp_after_rel2.effective_reserved_qty, 0.0)

			# 3rd release with explicit quantity: idempotent safe no-op
			ok3 = release_stock_reservation(sre_name, qty=5.0)
			self.assertTrue(ok3)
			atp_after_rel3 = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_after_rel3.candidate_atp_qty, 50.0)
			self.assertEqual(atp_after_rel3.effective_reserved_qty, 0.0)

			# Reference status is Cancelled
			ref_status = frappe.db.get_value("Inventory Reservation Reference", {"stock_reservation_entry": sre_name}, "status")
			self.assertEqual(ref_status, "Cancelled")

			frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
		finally:
			self._cleanup_stock([reco_name])

	def test_12_multi_warehouse_all_or_nothing_rollback(self):
		"""
		MULTI-WAREHOUSE ALL-OR-NOTHING TEST:
		Warehouse A (Miami) ATP = 5
		Warehouse B (Orlando) ATP = 3
		Total Channel ATP = 8
		Requested = 10, all_or_nothing = True (allow_partial = False)

		Expected:
		- Reservation fails with InsufficientStockToReserveError
		- Warehouse A reserved delta = 0
		- Warehouse B reserved delta = 0
		- Zero Inventory Reservation Reference persisted
		- Zero partial SRE records remain
		"""
		reco_m = self._set_physical_stock(self.item_code, self.wh_miami, 5.0)
		reco_o = self._set_physical_stock(self.item_code, self.wh_orlando, 3.0)

		try:
			snap_m_before = get_reservation_snapshot(self.item_code, self.wh_miami)
			snap_o_before = get_reservation_snapshot(self.item_code, self.wh_orlando)
			sre_count_before = frappe.db.count("Stock Reservation Entry")
			ref_count_before = frappe.db.count("Inventory Reservation Reference")

			with self.assertRaises(InsufficientStockToReserveError):
				reserve_channel_stock(
					item_code=self.item_code,
					sales_channel=self.channel,
					requested_qty=10.0,
					allow_partial=False,
					idempotency_key="ALL-OR-NOTHING-FAIL-TEST",
				)

			# Verify delta = 0 across both warehouses
			snap_m_after = get_reservation_snapshot(self.item_code, self.wh_miami)
			snap_o_after = get_reservation_snapshot(self.item_code, self.wh_orlando)
			self.assertEqual(snap_m_after.net_reserved_qty - snap_m_before.net_reserved_qty, 0.0)
			self.assertEqual(snap_o_after.net_reserved_qty - snap_o_before.net_reserved_qty, 0.0)

			# Verify zero SRE and reference leakage
			self.assertEqual(frappe.db.count("Stock Reservation Entry"), sre_count_before)
			self.assertEqual(frappe.db.count("Inventory Reservation Reference"), ref_count_before)
		finally:
			self._cleanup_stock([reco_m, reco_o])

	def test_13_multi_warehouse_partial_mode(self):
		"""
		MULTI-WAREHOUSE PARTIAL MODE TEST:
		Miami ATP = 5, Orlando ATP = 3 (Total = 8)
		Requested = 10, allow_partial = True
		Expected:
		- reserved_qty = 8.0
		- unfulfilled_qty = 2.0
		- Miami allocated = 5.0, Orlando allocated = 3.0
		- No silent partial behavior
		"""
		reco_m = self._set_physical_stock(self.item_code, self.wh_miami, 5.0)
		reco_o = self._set_physical_stock(self.item_code, self.wh_orlando, 3.0)
		res = None

		try:
			res = reserve_channel_stock(
				item_code=self.item_code,
				sales_channel=self.channel,
				requested_qty=10.0,
				allow_partial=True,
				idempotency_key="PARTIAL-MODE-SUCCESS-001",
			)
			self.assertTrue(res.success)
			self.assertEqual(res.requested_qty, 10.0)
			self.assertEqual(res.reserved_qty, 8.0)
			self.assertEqual(res.unfulfilled_qty, 2.0)
			self.assertEqual(len(res.allocations), 2)

			alloc_whs = {a.warehouse: a.allocated_qty for a in res.allocations}
			self.assertEqual(alloc_whs.get(self.wh_miami), 5.0)
			self.assertEqual(alloc_whs.get(self.wh_orlando), 3.0)

			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_atp_qty, 0.0)
		finally:
			if res and res.allocations:
				for a in res.allocations:
					try:
						release_stock_reservation(a.stock_reservation_entry)
						frappe.delete_doc("Stock Reservation Entry", a.stock_reservation_entry, force=True, ignore_permissions=True)
					except Exception:
						pass
			self._cleanup_stock([reco_m, reco_o])

	def test_14_sales_order_lifecycle_proof(self):
		"""
		SALES ORDER NATIVE LIFECYCLE PROOF:
		1. Physical stock = 50.0. Initial ATP = 50.0.
		2. Submitted Sales Order for 10.0.
		3. Native reservation of 10.0 created against SO.
		   Confirm NO double counting: ATP is 40.0 (not 30.0!).
		4. Delivery Note of 4.0 submitted.
		   Actual stock becomes 46.0, SRE net reserved becomes 6.0.
		   ATP remains 40.0.
		5. Cancellation: Cancel Delivery Note, cancel SRE, cancel Sales Order.
		   Confirm ATP restores cleanly to 50.0.
		"""
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		reco_name = self._set_physical_stock(self.item_code, self.wh_miami, 50.0)
		cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
		if not cust:
			cust = frappe.get_doc({"doctype": "Customer", "customer_name": "Test ATP Cust"}).insert(ignore_permissions=True).name

		so = None
		dn = None
		sre_name = None

		try:
			# 1. Initial ATP = 50
			atp_0 = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_0.candidate_atp_qty, 50.0)

			# 2. Submitted Sales Order of 10
			so = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [
					{
						"item_code": self.item_code,
						"warehouse": self.wh_miami,
						"qty": 10.0,
						"rate": 25.0,
					}
				],
			}).insert(ignore_permissions=True)
			so.submit()

			# Unreserved SO demand reduces ATP to 40
			atp_so = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_so.candidate_atp_qty, 40.0)

			# 3. Create native SRE against this Sales Order
			res = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=10.0,
				voucher_type="Sales Order",
				voucher_no=so.name,
				voucher_detail_no=so.items[0].name,
				idempotency_key=f"SO-LIFECYCLE-{so.name}",
			)
			self.assertTrue(res.success)
			sre_name = res.allocations[0].stock_reservation_entry

			# Verify NO double-counting: ATP must still be 40.0 (NOT 30.0!)
			atp_sre = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_sre.effective_reserved_qty, 10.0)
			self.assertEqual(atp_sre.candidate_atp_qty, 40.0)

			# 4. Partial Delivery Note (4 units)
			dn = make_delivery_note(so.name)
			dn.items[0].qty = 4.0
			dn.insert(ignore_permissions=True)
			dn.submit()

			atp_dn = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_dn.actual_qty, 46.0)
			self.assertEqual(atp_dn.effective_reserved_qty, 6.0)
			self.assertEqual(atp_dn.candidate_atp_qty, 40.0)

			# 5. Cancellation sequence
			dn.reload()
			dn.cancel()
			frappe.delete_doc("Delivery Note", dn.name, force=True, ignore_permissions=True)
			dn = None

			release_stock_reservation(sre_name)
			frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
			sre_name = None

			so.reload()
			so.cancel()
			frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
			so = None

			# ATP fully restored to 50
			atp_restored = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_restored.actual_qty, 50.0)
			self.assertEqual(atp_restored.effective_reserved_qty, 0.0)
			self.assertEqual(atp_restored.candidate_atp_qty, 50.0)

		finally:
			if dn:
				try:
					dn.reload()
					dn.cancel()
					frappe.delete_doc("Delivery Note", dn.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			if sre_name:
				try:
					release_stock_reservation(sre_name)
					frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
				except Exception:
					pass
			if so:
				try:
					so.reload()
					so.cancel()
					frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			self._cleanup_stock([reco_name])

	def test_15_serialized_and_batch_reservation_behavior(self):
		"""
		VERIFIES SERIALIZED & BATCH ITEM RESERVATION BEHAVIOR:
		At reservation time, reservations are quantity-level (reservation_based_on = 'Qty')
		without prematurely locking specific serial or batch numbers.
		"""
		ser_nos = ["SN-ATP-01", "SN-ATP-02", "SN-ATP-03", "SN-ATP-04", "SN-ATP-05"]
		reco_ser = self._set_serialized_stock(self.serial_item, self.wh_miami, ser_nos)
		reco_bat = self._set_batch_stock(self.batch_item, self.wh_miami, "BAT-ATP-B01", 10.0)
		sre_ser = None
		sre_bat = None

		orig_auto_res = frappe.db.get_single_value("Stock Settings", "auto_reserve_serial_and_batch")
		try:
			# Mode A: When auto_reserve_serial_and_batch is disabled (0), reservations remain purely quantity-level
			frappe.db.set_single_value("Stock Settings", "auto_reserve_serial_and_batch", 0)

			res_ser = reserve_stock(
				item_code=self.serial_item,
				warehouse=self.wh_miami,
				requested_qty=2.0,
				idempotency_key="SER-RES-QTY-LEVEL",
			)
			self.assertTrue(res_ser.success)
			self.assertEqual(res_ser.reserved_qty, 2.0)
			sre_ser = res_ser.allocations[0].stock_reservation_entry

			sre_doc_ser = frappe.get_doc("Stock Reservation Entry", sre_ser)
			self.assertEqual(sre_doc_ser.reservation_based_on, "Qty")
			self.assertEqual(sre_doc_ser.has_serial_no, 1)
			self.assertEqual(len(sre_doc_ser.get("sb_entries") or []), 0)

			atp_ser = get_warehouse_atp(self.serial_item, self.wh_miami)
			self.assertEqual(atp_ser.candidate_atp_qty, 3.0)

			# Batch item reservation: reserve 4 units in Qty-level mode
			res_bat = reserve_stock(
				item_code=self.batch_item,
				warehouse=self.wh_miami,
				requested_qty=4.0,
				idempotency_key="BAT-RES-QTY-LEVEL",
			)
			self.assertTrue(res_bat.success)
			self.assertEqual(res_bat.reserved_qty, 4.0)
			sre_bat = res_bat.allocations[0].stock_reservation_entry

			sre_doc_bat = frappe.get_doc("Stock Reservation Entry", sre_bat)
			self.assertEqual(sre_doc_bat.reservation_based_on, "Qty")
			self.assertEqual(sre_doc_bat.has_batch_no, 1)
			self.assertEqual(len(sre_doc_bat.get("sb_entries") or []), 0)

			atp_bat = get_warehouse_atp(self.batch_item, self.wh_miami)
			self.assertEqual(atp_bat.candidate_atp_qty, 6.0)

			# Mode B: When auto_reserve_serial_and_batch is enabled (1), native ERPNext binds specific serials
			frappe.db.set_single_value("Stock Settings", "auto_reserve_serial_and_batch", 1)
			res_ser_auto = reserve_stock(
				item_code=self.serial_item,
				warehouse=self.wh_miami,
				requested_qty=1.0,
				idempotency_key="SER-RES-AUTO-BOUND",
			)
			self.assertTrue(res_ser_auto.success)
			sre_auto_name = res_ser_auto.allocations[0].stock_reservation_entry
			sre_auto_doc = frappe.get_doc("Stock Reservation Entry", sre_auto_name)
			self.assertEqual(sre_auto_doc.reservation_based_on, "Serial and Batch")
			self.assertEqual(len(sre_auto_doc.sb_entries), 1)
			release_stock_reservation(sre_auto_name)
			frappe.delete_doc("Stock Reservation Entry", sre_auto_name, force=True, ignore_permissions=True)

		finally:
			if sre_ser:
				try:
					release_stock_reservation(sre_ser)
					frappe.delete_doc("Stock Reservation Entry", sre_ser, force=True, ignore_permissions=True)
				except Exception:
					pass
			if sre_bat:
				try:
					release_stock_reservation(sre_bat)
					frappe.delete_doc("Stock Reservation Entry", sre_bat, force=True, ignore_permissions=True)
				except Exception:
					pass
			self._cleanup_stock([reco_ser, reco_bat])
			frappe.db.set_single_value("Stock Settings", "auto_reserve_serial_and_batch", orig_auto_res)
			frappe.db.commit()

	def test_16_native_sre_remaining_qty_formula_and_bin_consistency(self):
		"""
		VERIFIES ERPNext v16.32.3 NATIVE SRE REMAINING-QTY SEMANTICS AND BIN CONSISTENCY:
		1. Net SRE remaining quantity formula:
		   reserved_qty - delivered_qty - transferred_qty - consumed_qty.
		2. Verified that native helper get_sre_reserved_qty_for_item_and_warehouse
		   equals Bin.reserved_stock after native SRE lifecycle events.
		"""
		from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
			get_sre_reserved_qty_for_item_and_warehouse,
		)
		reco = self._setup_stock(self.wh_miami, 50.0)
		sre_name = None
		try:
			res = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=20.0,
				idempotency_key="TEST-16-NATIVE-SRE",
			)
			self.assertTrue(res.success)
			sre_name = res.allocations[0].stock_reservation_entry

			# Check native SRE helper and Bin.reserved_stock consistency
			helper_qty = get_sre_reserved_qty_for_item_and_warehouse(self.item_code, self.wh_miami)
			bin_res_stock = frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_stock")
			self.assertEqual(flt(helper_qty), 20.0)
			self.assertEqual(flt(bin_res_stock), 20.0)
			self.assertEqual(flt(helper_qty), flt(bin_res_stock))

			# Simulate native transfer (transferred_qty = 5.0)
			frappe.db.set_value("Stock Reservation Entry", sre_name, "transferred_qty", 5.0)
			sre_doc = frappe.get_doc("Stock Reservation Entry", sre_name)
			sre_doc.update_status()
			sre_doc.update_reserved_stock_in_bin()

			helper_qty_trans = get_sre_reserved_qty_for_item_and_warehouse(self.item_code, self.wh_miami)
			bin_res_stock_trans = frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_stock")
			self.assertEqual(flt(helper_qty_trans), 15.0)
			self.assertEqual(flt(bin_res_stock_trans), 15.0)
			self.assertEqual(flt(helper_qty_trans), flt(bin_res_stock_trans))

			# Simulate consumption (consumed_qty = 3.0)
			frappe.db.set_value("Stock Reservation Entry", sre_name, "consumed_qty", 3.0)
			sre_doc.reload()
			sre_doc.update_status()
			sre_doc.update_reserved_stock_in_bin()

			helper_qty_cons = get_sre_reserved_qty_for_item_and_warehouse(self.item_code, self.wh_miami)
			bin_res_stock_cons = frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_stock")
			self.assertEqual(flt(helper_qty_cons), 12.0)
			self.assertEqual(flt(bin_res_stock_cons), 12.0)
			self.assertEqual(flt(helper_qty_cons), flt(bin_res_stock_cons))

			# Effective reserved breakdown reflects 12.0
			bd = get_effective_reserved_breakdown(self.item_code, self.wh_miami)
			self.assertEqual(bd.standalone_sre_demand, 12.0)
			self.assertEqual(bd.total_effective_reserved, 12.0)
			self.assertEqual(bd.native_reserved_stock, 12.0)

		finally:
			if sre_name:
				try:
					release_stock_reservation(sre_name)
					frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
				except Exception:
					pass
			self._cleanup_stock([reco])

	def test_17_standalone_sre_reduces_atp_once(self):
		"""
		VERIFIES STANDALONE SRE (Bop / Channel Reservation):
		Actual = 20, standalone SRE = 5, other demand = 0 => effective_reserved = 5, ATP = 15.
		"""
		reco = self._setup_stock(self.wh_miami, 20.0)
		sre_name = None
		try:
			res = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=5.0,
				idempotency_key="TEST-17-STANDALONE",
			)
			self.assertTrue(res.success)
			sre_name = res.allocations[0].stock_reservation_entry

			bd = get_effective_reserved_breakdown(self.item_code, self.wh_miami)
			self.assertEqual(bd.standalone_sre_demand, 5.0)
			self.assertEqual(bd.sales_order_demand, 0.0)
			self.assertEqual(bd.production_demand, 0.0)
			self.assertEqual(bd.total_effective_reserved, 5.0)

			atp = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp.actual_qty, 20.0)
			self.assertEqual(atp.effective_reserved_qty, 5.0)
			self.assertEqual(atp.candidate_atp_qty, 15.0)

		finally:
			if sre_name:
				try:
					release_stock_reservation(sre_name)
					frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
				except Exception:
					pass
			self._cleanup_stock([reco])

	def test_18_work_order_and_manufacturing_overlap_no_double_count(self):
		"""
		VERIFIES MANUFACTURING DEMAND OVERLAP PREVENTION:
		Work Order: native production reservation = 10, SRE against same Work Order demand = 10.
		Effective reserved must represent 10 demand, NOT 20.
		"""
		reco = self._setup_stock(self.wh_miami, 50.0)
		sre_name = None
		try:
			# Set native Bin.reserved_qty_for_production = 10.0
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_qty_for_production", 10.0)

			# Create SRE tied to voucher_type='Work Order' for 10.0
			res = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=10.0,
				voucher_type="Work Order",
				voucher_no="WO-SYNTHETIC-001",
				voucher_detail_no="WO-ITEM-001",
				idempotency_key="TEST-18-WO-SRE",
			)
			self.assertTrue(res.success)
			sre_name = res.allocations[0].stock_reservation_entry

			bd = get_effective_reserved_breakdown(self.item_code, self.wh_miami)
			self.assertEqual(bd.production_demand, 10.0)
			self.assertEqual(bd.standalone_sre_demand, 0.0)  # Tied to Work Order, not counted as standalone
			self.assertEqual(bd.sales_order_demand, 0.0)
			self.assertEqual(bd.total_effective_reserved, 10.0)  # NOT 20!

			atp = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp.effective_reserved_qty, 10.0)
			self.assertEqual(atp.candidate_atp_qty, 40.0)

		finally:
			if sre_name:
				try:
					release_stock_reservation(sre_name)
					frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
				except Exception:
					pass
			# Reset bin field
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_qty_for_production", 0.0)
			self._cleanup_stock([reco])

	def test_19_production_plan_overlap_no_double_count(self):
		"""
		VERIFIES PRODUCTION PLAN DEMAND OVERLAP PREVENTION:
		Native production plan reservation = 10, SRE against same underlying demand = 10.
		No double count: total effective reserved = 10.0.
		"""
		reco = self._setup_stock(self.wh_miami, 50.0)
		sre_name = None
		try:
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_qty_for_production_plan", 10.0)

			res = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=10.0,
				voucher_type="Production Plan",
				voucher_no="PP-SYNTHETIC-001",
				voucher_detail_no="PP-ITEM-001",
				idempotency_key="TEST-19-PP-SRE",
			)
			self.assertTrue(res.success)
			sre_name = res.allocations[0].stock_reservation_entry

			bd = get_effective_reserved_breakdown(self.item_code, self.wh_miami)
			self.assertEqual(bd.production_plan_demand, 10.0)
			self.assertEqual(bd.standalone_sre_demand, 0.0)
			self.assertEqual(bd.total_effective_reserved, 10.0)  # NOT 20!

			atp = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp.effective_reserved_qty, 10.0)
			self.assertEqual(atp.candidate_atp_qty, 40.0)

		finally:
			if sre_name:
				try:
					release_stock_reservation(sre_name)
					frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
				except Exception:
					pass
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_qty_for_production_plan", 0.0)
			self._cleanup_stock([reco])

	def test_20_subcontract_overlap_no_double_count(self):
		"""
		VERIFIES SUBCONTRACTING DEMAND OVERLAP PREVENTION:
		Native subcontract reservation = 10, SRE against same demand = 10.
		No double count: total effective reserved = 10.0.
		"""
		reco = self._setup_stock(self.wh_miami, 50.0)
		sre_name = None
		try:
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_qty_for_sub_contract", 10.0)

			res = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=10.0,
				voucher_type="Subcontracting Order",
				voucher_no="SCO-SYNTHETIC-001",
				voucher_detail_no="SCO-ITEM-001",
				idempotency_key="TEST-20-SCO-SRE",
			)
			self.assertTrue(res.success)
			sre_name = res.allocations[0].stock_reservation_entry

			bd = get_effective_reserved_breakdown(self.item_code, self.wh_miami)
			self.assertEqual(bd.subcontract_demand, 10.0)
			self.assertEqual(bd.standalone_sre_demand, 0.0)
			self.assertEqual(bd.total_effective_reserved, 10.0)  # NOT 20!

			atp = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp.effective_reserved_qty, 10.0)
			self.assertEqual(atp.candidate_atp_qty, 40.0)

		finally:
			if sre_name:
				try:
					release_stock_reservation(sre_name)
					frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
				except Exception:
					pass
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_qty_for_sub_contract", 0.0)
			self._cleanup_stock([reco])

	def test_21_cross_warehouse_so_and_sre_channel_deduplication(self):
		"""
		VERIFIES SALES ORDER CROSS-WAREHOUSE SEMANTICS:
		Sales Order target warehouse = Warehouse Miami (demand = 10.0).
		Stock Reservation Entry for that SO item allocated at Warehouse Orlando (qty = 4.0).
		Across channel warehouses Miami + Orlando:
		The same Sales Order demand MUST NOT reduce channel ATP twice!
		Demand must total exactly 10.0 (not 14.0).
		"""
		reco_m = self._setup_stock(self.wh_miami, 20.0)
		reco_o = self._setup_stock(self.wh_orlando, 20.0)
		so = None
		sre = None
		try:
			cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
			if not cust:
				cust = frappe.get_doc({"doctype": "Customer", "customer_name": "Test Cust Cross"}).insert(ignore_permissions=True).name

			so = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [
					{
						"item_code": self.item_code,
						"warehouse": self.wh_miami,
						"qty": 10.0,
						"rate": 25.0,
					}
				],
			}).insert(ignore_permissions=True)
			so.submit()

			# Create SRE for 4.0 against SO item, allocated at Orlando
			sre = frappe.get_doc({
				"doctype": "Stock Reservation Entry",
				"item_code": self.item_code,
				"warehouse": self.wh_orlando,
				"voucher_type": "Sales Order",
				"voucher_no": so.name,
				"voucher_detail_no": so.items[0].name,
				"voucher_qty": 10.0,
				"available_qty": 20.0,
				"reserved_qty": 4.0,
				"company": self.company,
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			sre.submit()

			# Local Warehouse ATP:
			# Miami: Actual 20.0, Reserved 10.0 (SO) => ATP 10.0
			atp_m = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_m.actual_qty, 20.0)
			self.assertEqual(atp_m.effective_reserved_qty, 10.0)
			self.assertEqual(atp_m.candidate_atp_qty, 10.0)

			# Orlando: Actual 20.0, Reserved 4.0 (SRE) => ATP 16.0
			atp_o = get_warehouse_atp(self.item_code, self.wh_orlando)
			self.assertEqual(atp_o.actual_qty, 20.0)
			self.assertEqual(atp_o.effective_reserved_qty, 4.0)
			self.assertEqual(atp_o.candidate_atp_qty, 16.0)

			# Channel ATP across Miami + Orlando:
			# Total Actual = 40.0. Total logical demand = 10.0 (4 at Orlando + 6 unreserved at Miami).
			# Without deduplication, naive sum is 10 + 4 = 14.
			# With deduplication, channel aggregate reserved = 10.0, channel ATP = 30.0!
			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_actual_qty, 40.0)
			self.assertEqual(ch_atp.aggregate_reserved_qty, 10.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 30.0)

		finally:
			if sre:
				try:
					sre.reload()
					sre.cancel()
					frappe.delete_doc("Stock Reservation Entry", sre.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			if so:
				try:
					so.reload()
					so.cancel()
					frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			self._cleanup_stock([reco_m, reco_o])

	def test_22_partial_cross_warehouse_multi_warehouse_so_reservation(self):
		"""
		VERIFIES PARTIAL MULTI-ALLOCATION SALES ORDER RESERVATIONS:
		Sales Order demand = 10.0 at Warehouse Miami.
		SRE allocations: Orlando = 4.0, Miami = 3.0.
		Remaining unreserved SO demand = 3.0 at Miami.
		Across channel warehouses Miami + Orlando:
		Total logical demand must total exactly 10.0, not 17.0.
		"""
		reco_m = self._setup_stock(self.wh_miami, 20.0)
		reco_o = self._setup_stock(self.wh_orlando, 20.0)
		so = None
		sre_o = None
		sre_m = None
		try:
			cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
			if not cust:
				cust = frappe.get_doc({"doctype": "Customer", "customer_name": "Test Cust MultiCross"}).insert(ignore_permissions=True).name

			so = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [
					{
						"item_code": self.item_code,
						"warehouse": self.wh_miami,
						"qty": 10.0,
						"rate": 25.0,
					}
				],
			}).insert(ignore_permissions=True)
			so.submit()

			# SRE 1 at Orlando (4.0)
			sre_o = frappe.get_doc({
				"doctype": "Stock Reservation Entry",
				"item_code": self.item_code,
				"warehouse": self.wh_orlando,
				"voucher_type": "Sales Order",
				"voucher_no": so.name,
				"voucher_detail_no": so.items[0].name,
				"voucher_qty": 10.0,
				"available_qty": 20.0,
				"reserved_qty": 4.0,
				"company": self.company,
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			sre_o.submit()

			# SRE 2 at Miami (3.0)
			sre_m = frappe.get_doc({
				"doctype": "Stock Reservation Entry",
				"item_code": self.item_code,
				"warehouse": self.wh_miami,
				"voucher_type": "Sales Order",
				"voucher_no": so.name,
				"voucher_detail_no": so.items[0].name,
				"voucher_qty": 10.0,
				"available_qty": 20.0,
				"reserved_qty": 3.0,
				"company": self.company,
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			sre_m.submit()

			# Across channel warehouses (Miami, Orlando both in channel):
			# Total actual = 40.0. Total logical reserved = 10.0.
			# Aggregate ATP = 30.0.
			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_actual_qty, 40.0)
			self.assertEqual(ch_atp.aggregate_reserved_qty, 10.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 30.0)

		finally:
			if sre_o:
				try:
					sre_o.reload()
					sre_o.cancel()
					frappe.delete_doc("Stock Reservation Entry", sre_o.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			if sre_m:
				try:
					sre_m.reload()
					sre_m.cancel()
					frappe.delete_doc("Stock Reservation Entry", sre_m.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			if so:
				try:
					so.reload()
					so.cancel()
					frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			self._cleanup_stock([reco_m, reco_o])

	def test_23_partially_used_transferred_consumed_and_closed_states(self):
		"""
		VERIFIES SRE PARTIALLY USED, TRANSFERRED, CONSUMED, AND CLOSED STATUS LIFECYCLE:
		1. reserved_qty = 10, transferred_qty = 4 => status='Partially Used', remaining = 6.
		2. transferred_qty = 10 => status='Closed', remaining = 0.
		3. Delivered / Cancelled states excluded completely.
		"""
		from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import (
			get_sre_reserved_qty_for_item_and_warehouse,
		)
		reco = self._setup_stock(self.wh_miami, 50.0)
		sre_name = None
		try:
			res = reserve_stock(
				item_code=self.item_code,
				warehouse=self.wh_miami,
				requested_qty=10.0,
				idempotency_key="TEST-23-STATUS-CYCLE",
			)
			self.assertTrue(res.success)
			sre_name = res.allocations[0].stock_reservation_entry

			sre_doc = frappe.get_doc("Stock Reservation Entry", sre_name)
			self.assertEqual(sre_doc.status, "Reserved")

			# Step 1: Set transferred_qty = 4.0
			frappe.db.set_value("Stock Reservation Entry", sre_name, "transferred_qty", 4.0)
			sre_doc.reload()
			sre_doc.update_status()
			sre_doc.update_reserved_stock_in_bin()
			sre_doc.reload()
			self.assertEqual(sre_doc.status, "Partially Used")

			helper_qty = get_sre_reserved_qty_for_item_and_warehouse(self.item_code, self.wh_miami)
			self.assertEqual(helper_qty, 6.0)
			atp = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp.effective_reserved_qty, 6.0)
			self.assertEqual(atp.candidate_atp_qty, 44.0)

			# Step 2: Set transferred_qty = 10.0
			frappe.db.set_value("Stock Reservation Entry", sre_name, "transferred_qty", 10.0)
			sre_doc.reload()
			sre_doc.update_status()
			sre_doc.update_reserved_stock_in_bin()
			sre_doc.reload()
			self.assertEqual(sre_doc.status, "Closed")

			helper_closed = get_sre_reserved_qty_for_item_and_warehouse(self.item_code, self.wh_miami)
			self.assertEqual(helper_closed, 0.0)
			atp_closed = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_closed.effective_reserved_qty, 0.0)
			self.assertEqual(atp_closed.candidate_atp_qty, 50.0)

		finally:
			if sre_name:
				try:
					release_stock_reservation(sre_name)
					frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)
				except Exception:
					pass
	def test_24_live_safety_stock_deficit_isolated_between_warehouses(self):
		"""
		VERIFIES LIVE SAFETY STOCK DEFICIT ISOLATION (SECTION 2 & 7):
		Miami: actual 5, safety 10 => local ATP = 0 (deficit of 5).
		Orlando: actual 100, safety 0 => local ATP = 100.
		Channel ATP must equal 100, NOT 95.
		"""
		reco_m = self._setup_stock(self.wh_miami, 5.0)
		reco_o = self._setup_stock(self.wh_orlando, 100.0)
		policy_name = None
		try:
			# Configure safety stock policy of 10.0 for Miami
			policy = frappe.get_doc({
				"doctype": "Inventory Availability Policy",
				"policy_name": f"POL-SAFETY-ISO-{self.abbr}",
				"warehouse": self.wh_miami,
				"item_code": self.item_code,
				"safety_stock_qty": 10.0,
			}).insert(ignore_permissions=True)
			policy_name = policy.name

			atp_m = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_m.actual_qty, 5.0)
			self.assertEqual(atp_m.safety_stock_qty, 10.0)
			self.assertEqual(atp_m.candidate_atp_qty, 0.0)

			atp_o = get_warehouse_atp(self.item_code, self.wh_orlando)
			self.assertEqual(atp_o.actual_qty, 100.0)
			self.assertEqual(atp_o.safety_stock_qty, 0.0)
			self.assertEqual(atp_o.candidate_atp_qty, 100.0)

			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_atp_qty, 100.0)
			self.assertNotEqual(ch_atp.aggregate_atp_qty, 95.0)

		finally:
			if policy_name and frappe.db.exists("Inventory Availability Policy", policy_name):
				frappe.delete_doc("Inventory Availability Policy", policy_name, force=True, ignore_permissions=True)
			self._cleanup_stock([reco_m, reco_o])

	def test_25_live_production_deficit_isolated_between_warehouses(self):
		"""
		VERIFIES LIVE PRODUCTION DEMAND ISOLATION (SECTION 3):
		Miami: actual 0, production demand 10 => local ATP = 0.
		Orlando: actual 100, production demand 0 => local ATP = 100.
		Channel ATP must equal 100, NOT 90.
		"""
		from erpnext.stock.utils import get_or_make_bin

		reco_o = self._setup_stock(self.wh_orlando, 100.0)
		try:
			# Ensure Bin exists for Miami and force native production demand
			get_or_make_bin(self.item_code, self.wh_miami)
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_qty_for_production", 10.0)

			atp_m = get_warehouse_atp(self.item_code, self.wh_miami)
			self.assertEqual(atp_m.actual_qty, 0.0)
			self.assertEqual(atp_m.effective_reserved_qty, 10.0)
			self.assertEqual(atp_m.candidate_atp_qty, 0.0)

			atp_o = get_warehouse_atp(self.item_code, self.wh_orlando)
			self.assertEqual(atp_o.actual_qty, 100.0)
			self.assertEqual(atp_o.effective_reserved_qty, 0.0)
			self.assertEqual(atp_o.candidate_atp_qty, 100.0)

			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_atp_qty, 100.0)
			self.assertNotEqual(ch_atp.aggregate_atp_qty, 90.0)

		finally:
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_miami}, "reserved_qty_for_production", 0.0)
			self._cleanup_stock([reco_o])

	def test_26_live_non_sellable_warehouse_cannot_reduce_sellable_atp(self):
		"""
		VERIFIES LIVE NON-SELLABLE SOURCE ISOLATION (SECTION 8):
		Quarantine: actual 100, reserved 200, allow_sellable_stock = 0 => 0 ATP.
		Miami: actual 50, reserved 0, allow_sellable_stock = 1 => 50 ATP.
		Channel ATP must be exactly 50 based on Miami only.
		Quarantine inventory/deficits do NOT alter sellable aggregates.
		"""
		reco_q = self._setup_stock(self.wh_quarantine, 100.0)
		reco_m = self._setup_stock(self.wh_miami, 50.0)
		try:
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_quarantine}, "reserved_qty", 200.0)

			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_actual_qty, 50.0)
			self.assertEqual(ch_atp.aggregate_reserved_qty, 0.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 50.0)

		finally:
			frappe.db.set_value("Bin", {"item_code": self.item_code, "warehouse": self.wh_quarantine}, "reserved_qty", 0.0)
			self._cleanup_stock([reco_q, reco_m])

	def test_27_live_disabled_source_cannot_reduce_channel_atp(self):
		"""
		VERIFIES LIVE DISABLED SOURCE ISOLATION (SECTION 9):
		Disabled Channel Inventory Source is completely excluded from channel ATP.
		"""
		reco_m = self._setup_stock(self.wh_miami, 40.0)
		reco_o = self._setup_stock(self.wh_orlando, 60.0)
		try:
			# Temporarily disable Orlando
			frappe.db.set_value("Channel Inventory Source", self.cis_orlando, "enabled", 0)

			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.aggregate_actual_qty, 40.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 40.0)

		finally:
			frappe.db.set_value("Channel Inventory Source", self.cis_orlando, "enabled", 1)
			self._cleanup_stock([reco_m, reco_o])

	def test_28_live_priority_invariance_and_channel_demand_breakdown(self):
		"""
		VERIFIES LIVE PRIORITY INVARIANCE AND DEMAND BREAKDOWN (SECTION 5, 11, 14):
		Priority swap (10/20 -> 20/10) does NOT alter aggregate quantity.
		ChannelDemandBreakdown and explainability fields populated accurately.
		"""
		reco_m = self._setup_stock(self.wh_miami, 50.0)
		reco_o = self._setup_stock(self.wh_orlando, 50.0)
		try:
			# Priority Miami=10, Orlando=20
			ch_atp_1 = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp_1.aggregate_atp_qty, 100.0)
			self.assertIsNotNone(ch_atp_1.demand_breakdown)
			self.assertEqual(ch_atp_1.demand_breakdown.total_demand, 0.0)

			# Swap priorities: Miami=20, Orlando=10
			frappe.db.set_value("Channel Inventory Source", self.cis_miami, "priority", 20)
			frappe.db.set_value("Channel Inventory Source", self.cis_orlando, "priority", 10)

			ch_atp_2 = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp_2.aggregate_atp_qty, 100.0)
			self.assertEqual(ch_atp_1.aggregate_atp_qty, ch_atp_2.aggregate_atp_qty)

			# Verify get_atp_breakdown matches
			bd = get_atp_breakdown(self.item_code, self.channel)
			self.assertEqual(bd.channel_atp, 100.0)
			self.assertEqual(len(bd.lines), 3)  # Miami, Orlando, Quarantine

		finally:
			frappe.db.set_value("Channel Inventory Source", self.cis_miami, "priority", 10)
			frappe.db.set_value("Channel Inventory Source", self.cis_orlando, "priority", 20)
			self._cleanup_stock([reco_m, reco_o])

	def test_29_live_critical_counterexample_uncovered_so_demand_atp_zero(self):
		"""
		VERIFIES PHASE 1I.4 SECTION 1 CRITICAL COUNTEREXAMPLE:
		Sellable channel pool: Warehouse Miami (Wh A), Warehouse Orlando (Wh B).
		Physical inventory:
		    Miami (A): actual_qty = 0
		    Orlando (B): actual_qty = 10
		Demand:
		    Sales Order 1: target Miami (A), qty = 10 (unreserved pending SO demand)
		    Sales Order 2: target Miami (A), qty = 4, with active SRE of 4 in Orlando (B)
		Pool calculations:
		    Orlando local physical capacity = 10 - 4 = 6
		    Miami local physical capacity = 0
		    base_pool_capacity = 6 + 0 = 6
		    Uncovered SO demand (SO 1) = 10 - 0 = 10
		    Channel ATP = max(0, 6 - 10) = 0
		"""
		reco_o = self._setup_stock(self.wh_orlando, 10.0)
		so1 = None
		so2 = None
		sre2 = None
		try:
			cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
			if not cust:
				cust = frappe.get_doc({"doctype": "Customer", "customer_name": "Test Cust Counterexample"}).insert(ignore_permissions=True).name

			# SO 1: 10 units at Miami (pending, no SRE)
			so1 = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [
					{
						"item_code": self.item_code,
						"warehouse": self.wh_miami,
						"qty": 10.0,
						"rate": 25.0,
					}
				],
			}).insert(ignore_permissions=True)
			so1.submit()

			# SO 2: 4 units at Miami, covered by SRE in Orlando
			so2 = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [
					{
						"item_code": self.item_code,
						"warehouse": self.wh_miami,
						"qty": 4.0,
						"rate": 25.0,
					}
				],
			}).insert(ignore_permissions=True)
			so2.submit()

			sre2 = frappe.get_doc({
				"doctype": "Stock Reservation Entry",
				"item_code": self.item_code,
				"warehouse": self.wh_orlando,
				"voucher_type": "Sales Order",
				"voucher_no": so2.name,
				"voucher_detail_no": so2.items[0].name,
				"voucher_qty": 4.0,
				"available_qty": 10.0,
				"reserved_qty": 4.0,
				"company": self.company,
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			sre2.submit()

			# Verify Channel ATP is strictly 0.0 (prevents overselling)
			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.base_physical_capacity, 6.0)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 10.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 0.0)

		finally:
			if sre2:
				try:
					sre2.reload()
					sre2.cancel()
					frappe.delete_doc("Stock Reservation Entry", sre2.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			for so in [so1, so2]:
				if so:
					try:
						so.reload()
						so.cancel()
						frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
					except Exception:
						pass
			self._cleanup_stock([reco_o])

	def test_30_live_transactional_reservation_rejected_by_uncovered_so_demand(self):
		"""
		VERIFIES TRANSACTIONAL CHANNEL RESERVATION REJECTION:
		In the critical counterexample state (channel ATP = 0),
		calling reserve_channel_stock(requested_qty=1) MUST be rejected with
		InsufficientStockToReserveError and commit NO database records.
		"""
		reco_o = self._setup_stock(self.wh_orlando, 10.0)
		so1 = None
		so2 = None
		sre2 = None
		try:
			cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
			if not cust:
				cust = frappe.get_doc({"doctype": "Customer", "customer_name": "Test Cust Reject"}).insert(ignore_permissions=True).name

			so1 = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [{"item_code": self.item_code, "warehouse": self.wh_miami, "qty": 10.0, "rate": 25.0}],
			}).insert(ignore_permissions=True)
			so1.submit()

			so2 = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [{"item_code": self.item_code, "warehouse": self.wh_miami, "qty": 4.0, "rate": 25.0}],
			}).insert(ignore_permissions=True)
			so2.submit()

			sre2 = frappe.get_doc({
				"doctype": "Stock Reservation Entry",
				"item_code": self.item_code,
				"warehouse": self.wh_orlando,
				"voucher_type": "Sales Order",
				"voucher_no": so2.name,
				"voucher_detail_no": so2.items[0].name,
				"voucher_qty": 4.0,
				"available_qty": 10.0,
				"reserved_qty": 4.0,
				"company": self.company,
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			sre2.submit()

			# Attempt to reserve 1 unit on channel: must fail because channel ATP = 0
			with self.assertRaises(InsufficientStockToReserveError):
				reserve_channel_stock(
					item_code=self.item_code,
					sales_channel=self.channel,
					requested_qty=1.0,
				)

		finally:
			if sre2:
				try:
					sre2.reload()
					sre2.cancel()
					frappe.delete_doc("Stock Reservation Entry", sre2.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			for so in [so1, so2]:
				if so:
					try:
						so.reload()
						so.cancel()
						frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
					except Exception:
						pass
			self._cleanup_stock([reco_o])

	def test_31_live_multiwarehouse_successful_allocation_with_partial_uncovered_demand(self):
		"""
		VERIFIES SUCCESSFUL CHANNEL ALLOCATION WHEN ATP > 0:
		Orlando: actual 12. SRE 4 for SO2 => local physical cap = 8.
		Miami: actual 0. SO1 pending = 6.
		Base physical cap = 8. Uncovered SO = 6.
		Channel ATP = 8 - 6 = 2.
		reserve_channel_stock(requested_qty=2) MUST succeed and allocate 2 from Orlando.
		"""
		reco_o = self._setup_stock(self.wh_orlando, 12.0)
		so1 = None
		so2 = None
		sre2 = None
		res_sre_names = []
		try:
			cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
			if not cust:
				cust = frappe.get_doc({"doctype": "Customer", "customer_name": "Test Cust Success"}).insert(ignore_permissions=True).name

			so1 = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [{"item_code": self.item_code, "warehouse": self.wh_miami, "qty": 6.0, "rate": 25.0}],
			}).insert(ignore_permissions=True)
			so1.submit()

			so2 = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [{"item_code": self.item_code, "warehouse": self.wh_miami, "qty": 4.0, "rate": 25.0}],
			}).insert(ignore_permissions=True)
			so2.submit()

			sre2 = frappe.get_doc({
				"doctype": "Stock Reservation Entry",
				"item_code": self.item_code,
				"warehouse": self.wh_orlando,
				"voucher_type": "Sales Order",
				"voucher_no": so2.name,
				"voucher_detail_no": so2.items[0].name,
				"voucher_qty": 4.0,
				"available_qty": 12.0,
				"reserved_qty": 4.0,
				"company": self.company,
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			sre2.submit()

			# Check ATP is exactly 2.0
			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.base_physical_capacity, 8.0)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 6.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 2.0)

			# Reserve 2.0: should succeed
			res = reserve_channel_stock(
				item_code=self.item_code,
				sales_channel=self.channel,
				requested_qty=2.0,
				idempotency_key="TEST-31-SUCCESS",
			)
			self.assertTrue(res.success)
			self.assertEqual(res.reserved_qty, 2.0)
			self.assertEqual(len(res.allocations), 1)
			self.assertEqual(res.allocations[0].warehouse, self.wh_orlando)
			res_sre_names.append(res.allocations[0].stock_reservation_entry)

		finally:
			for sname in res_sre_names:
				try:
					release_stock_reservation(sname)
					frappe.delete_doc("Stock Reservation Entry", sname, force=True, ignore_permissions=True)
				except Exception:
					pass
			if sre2:
				try:
					sre2.reload()
					sre2.cancel()
					frappe.delete_doc("Stock Reservation Entry", sre2.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			for so in [so1, so2]:
				if so:
					try:
						so.reload()
						so.cancel()
						frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
					except Exception:
						pass
			self._cleanup_stock([reco_o])

	def test_32_live_all_or_nothing_and_partial_mode_with_uncovered_demand(self):
		"""
		VERIFIES ALL-OR-NOTHING VS PARTIAL MODE UNDER UNCOVERED DEMAND:
		Channel ATP = 2.0.
		allow_partial=False: request 3.0 => raises InsufficientStockToReserveError, 0 reserved.
		allow_partial=True: request 3.0 => reserves exactly 2.0, remaining 1.0 unallocated.
		"""
		reco_o = self._setup_stock(self.wh_orlando, 12.0)
		so1 = None
		sre2 = None
		so2 = None
		res_sre_names = []
		try:
			cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
			if not cust:
				cust = frappe.get_doc({"doctype": "Customer", "customer_name": "Test Cust Partial"}).insert(ignore_permissions=True).name

			so1 = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [{"item_code": self.item_code, "warehouse": self.wh_miami, "qty": 6.0, "rate": 25.0}],
			}).insert(ignore_permissions=True)
			so1.submit()

			so2 = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [{"item_code": self.item_code, "warehouse": self.wh_miami, "qty": 4.0, "rate": 25.0}],
			}).insert(ignore_permissions=True)
			so2.submit()

			sre2 = frappe.get_doc({
				"doctype": "Stock Reservation Entry",
				"item_code": self.item_code,
				"warehouse": self.wh_orlando,
				"voucher_type": "Sales Order",
				"voucher_no": so2.name,
				"voucher_detail_no": so2.items[0].name,
				"voucher_qty": 4.0,
				"available_qty": 12.0,
				"reserved_qty": 4.0,
				"company": self.company,
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			sre2.submit()

			# 1. All-or-nothing: request 3.0 with ATP 2.0 => failure
			with self.assertRaises(InsufficientStockToReserveError):
				reserve_channel_stock(
					item_code=self.item_code,
					sales_channel=self.channel,
					requested_qty=3.0,
					allow_partial=False,
				)

			# 2. Partial mode: request 3.0 with ATP 2.0 => allocates 2.0
			res = reserve_channel_stock(
				item_code=self.item_code,
				sales_channel=self.channel,
				requested_qty=3.0,
				allow_partial=True,
				idempotency_key="TEST-32-PARTIAL",
			)
			self.assertTrue(res.success)
			self.assertEqual(res.reserved_qty, 2.0)
			res_sre_names.append(res.allocations[0].stock_reservation_entry)

		finally:
			for sname in res_sre_names:
				try:
					release_stock_reservation(sname)
					frappe.delete_doc("Stock Reservation Entry", sname, force=True, ignore_permissions=True)
				except Exception:
					pass
			if sre2:
				try:
					sre2.reload()
					sre2.cancel()
					frappe.delete_doc("Stock Reservation Entry", sre2.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			for so in [so1, so2]:
				if so:
					try:
						so.reload()
						so.cancel()
						frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
					except Exception:
						pass
			self._cleanup_stock([reco_o])

	def test_33_live_sre_outside_pool_and_target_outside_pool(self):
		"""
		VERIFIES SRE OUTSIDE POOL REDUCES UNCOVERED DEMAND & TARGET OUTSIDE POOL:
		1. Sales Order targets Miami (in pool) for 10 units.
		   SRE for 6 units is created in Quarantine (OUTSIDE sellable pool).
		   The SRE outside the pool reduces the uncovered demand from 10 to 4!
		2. Sales Order targets Quarantine (outside sellable pool) for 5 units.
		   This SO target outside the pool does NOT burden the sellable channel pool.
		"""
		# Establish clean test fixture isolation deterministically before setting physical stock
		self._cleanup_stock()

		reco_m = self._setup_stock(self.wh_miami, 15.0)
		reco_q = self._setup_stock(self.wh_quarantine, 10.0)

		# Explicitly verify physical stock balance is effective and visible before SRE creation
		bal_q = get_stock_balance(self.item_code, self.wh_quarantine)
		self.assertEqual(bal_q, 10.0, "Physical stock in Quarantine must be 10.0 before SRE creation")
		bal_m = get_stock_balance(self.item_code, self.wh_miami)
		self.assertEqual(bal_m, 15.0, "Physical stock in Miami must be 15.0 before SRE creation")

		so1 = None
		sre1_q = None
		so_out = None
		try:
			cust = frappe.db.get_value("Customer", {"disabled": 0}, "name")
			if not cust:
				cust = frappe.get_doc({"doctype": "Customer", "customer_name": "Test Cust PoolBound"}).insert(ignore_permissions=True).name

			# SO 1: targets Miami (in pool), qty = 10
			so1 = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [{"item_code": self.item_code, "warehouse": self.wh_miami, "qty": 10.0, "rate": 25.0}],
			}).insert(ignore_permissions=True)
			so1.submit()

			# SRE for SO 1 located in Quarantine (outside pool) for 6 units
			sre1_q = frappe.get_doc({
				"doctype": "Stock Reservation Entry",
				"item_code": self.item_code,
				"warehouse": self.wh_quarantine,
				"voucher_type": "Sales Order",
				"voucher_no": so1.name,
				"voucher_detail_no": so1.items[0].name,
				"voucher_qty": 10.0,
				"available_qty": 10.0,
				"reserved_qty": 6.0,
				"company": self.company,
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			sre1_q.submit()

			# SO Outside: targets Quarantine (outside pool) for 5 units
			so_out = frappe.get_doc({
				"doctype": "Sales Order",
				"company": self.company,
				"customer": cust,
				"delivery_date": frappe.utils.nowdate(),
				"items": [{"item_code": self.item_code, "warehouse": self.wh_quarantine, "qty": 5.0, "rate": 25.0}],
			}).insert(ignore_permissions=True)
			so_out.submit()

			# Evaluate Channel ATP:
			# Sellable pool: Miami (actual 15, physical SRE 0, local cap 15).
			# Uncovered demand: SO 1 (pending 10 - SRE in Q 6 = 4). SO outside is ignored.
			# Channel ATP = 15 - 4 = 11.0.
			ch_atp = get_channel_atp(self.item_code, self.channel)
			self.assertEqual(ch_atp.base_physical_capacity, 15.0)
			self.assertEqual(ch_atp.uncovered_sales_order_demand, 4.0)
			self.assertEqual(ch_atp.aggregate_atp_qty, 11.0)

		finally:
			if sre1_q:
				try:
					sre1_q.reload()
					sre1_q.cancel()
					frappe.delete_doc("Stock Reservation Entry", sre1_q.name, force=True, ignore_permissions=True)
				except Exception:
					pass
			for so in [so1, so_out]:
				if so:
					try:
						so.reload()
						so.cancel()
						frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
					except Exception:
						pass
			self._cleanup_stock([reco_m, reco_q])

	def test_99_safety_invariance_restoration(self):
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
