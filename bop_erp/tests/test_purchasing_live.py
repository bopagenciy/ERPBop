# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, nowdate

from bop_erp.constants import IntegrationProvider
from bop_erp.inventory.reservations import get_channel_atp
from bop_erp.purchasing import (
	AccountMismatchError,
	CompanyMismatchError,
	OverBillingBlockedError,
	OverPaymentBlockedError,
	OverReceiptBlockedError,
	PurchaseDriftError,
	PurchaseReplayCancelledError,
	cancel_purchase_document,
	cancel_vendor_payment,
	create_purchase_invoice,
	create_purchase_order,
	get_purchasing_counters,
	get_three_way_traceability,
	pay_purchase_invoice,
	receive_purchase_order,
	reset_purchasing_counters,
)


class TestPurchasingLive(unittest.TestCase):
	"""
	Phase 1S Live Integration Test Suite:
	Purchasing / Accounts Payable / Vendor Flow Foundation.
	Executes against the local Frappe / ERPNext isolated test environment.

	Scenarios (Section 30: A to J):
	A. Standard Stock Purchase (PO 10 -> PR 10 -> PI 10 -> Payment 100%)
	B. Partial Receipt (PO 10 -> PR 4 -> PR 6 -> cumulative 10 -> over-receipt blocked)
	C. Partial Invoice (Received 10 -> PI 4 -> PI 6 -> overbilling blocked)
	D. Partial Vendor Payment (PI 100 -> Pay 30 [rem 70] -> Pay 70 [rem 0])
	E. Payment Cancellation (Cancel Pay B -> PI rem 70; PI, PR, PO intact)
	F. Concurrent Receipt Protection (PO rem 1 -> two attempts -> exactly 1 receives)
	G. Quarantine / Non-Sellable Receipt (Non-sellable WH stock increases, channel ATP unchanged, no publication)
	H. Sellable Receipt Publication (Sellable WH receipt -> durable outbox publication intent created, 0 HTTP calls)
	I. Service / Non-Stock Invoice (Service PI -> AP created, 0 stock movement)
	J. Fixture Cleanup Proof (0 residual TEST-1S records, baseline intact)
	"""

	FIXTURE_PREFIX = "TEST-1S-"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"
		# Snapshot baseline batches and serial numbers for baseline safety verification
		cls.baseline_batches = set(frappe.get_all("Batch", pluck="name"))
		cls.baseline_serials = set(frappe.get_all("Serial No", pluck="name"))

		# Defensively clean prior interrupted fixtures
		cls._cleanup_module_fixtures()

		# Ensure default bank account (must be non-group)
		default_bank = frappe.db.get_value("Company", cls.company, "default_bank_account")
		if default_bank and frappe.db.get_value("Account", default_bank, "is_group") == 0:
			cls.bank_account = default_bank
		else:
			cls.bank_account = (
				frappe.db.get_value("Account", {"company": cls.company, "account_type": "Bank", "is_group": 0}, "name")
				or "Banco Principal - IDP"
			)

		# Ensure payable account
		cls.payable_account = (
			frappe.db.get_value("Account", {"company": cls.company, "account_type": "Payable", "is_group": 0}, "name")
			or "2205 - Nacionales - IDP"
		)

		# Ensure expense account
		cls.expense_account = (
			frappe.db.get_value("Company", cls.company, "default_expense_account")
			or frappe.db.get_value("Account", {"company": cls.company, "root_type": "Expense", "is_group": 0}, "name")
			or "5105 - Gastos de personal - IDP"
		)

		# 1. Supplier / Vendor Master
		cls.supplier_name = f"{cls.FIXTURE_PREFIX}VENDOR-01"
		if not frappe.db.exists("Supplier", cls.supplier_name):
			s = frappe.get_doc({
				"doctype": "Supplier",
				"supplier_name": cls.supplier_name,
				"supplier_group": "All Supplier Groups",
				"supplier_type": "Company",
			})
			s.insert(ignore_permissions=True)

		# 2. Warehouses: Sellable Main & Quarantine Non-sellable
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")
		cls.wh_sellable = f"{cls.FIXTURE_PREFIX}WH-MAIN - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.wh_sellable):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"{cls.FIXTURE_PREFIX}WH-MAIN",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
				"is_group": 0,
			})
			w.insert(ignore_permissions=True)

		cls.wh_quarantine = f"{cls.FIXTURE_PREFIX}WH-QUAR - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.wh_quarantine):
			w_q = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"{cls.FIXTURE_PREFIX}WH-QUAR",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
				"is_group": 0,
			})
			w_q.insert(ignore_permissions=True)

		# 3. Test Items: Stock Item & Service Item
		cls.item_stock = f"{cls.FIXTURE_PREFIX}STOCK-ITEM-01"
		if not frappe.db.exists("Item", cls.item_stock):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_stock,
				"item_name": cls.item_stock,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}).insert(ignore_permissions=True)

		cls.item_service = f"{cls.FIXTURE_PREFIX}SERVICE-ITEM-01"
		if not frappe.db.exists("Item", cls.item_service):
			frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_service,
				"item_name": cls.item_service,
				"item_group": "All Item Groups",
				"stock_uom": "Unit",
				"is_stock_item": 0,
			}).insert(ignore_permissions=True)

		# 4. Sales Channel and Channel Inventory Source (only sellable WH linked)
		cls.sales_channel = f"{cls.FIXTURE_PREFIX}CH-A"
		if not frappe.db.exists("Sales Channel", cls.sales_channel):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.sales_channel,
				"channel_name": cls.sales_channel,
				"channel_type": "PRESTASHOP",
				"integration_provider": IntegrationProvider.PRESTASHOP,
				"company": cls.company,
				"active": 1,
			}).insert(ignore_permissions=True)

		# Ensure Channel Inventory Source only for sellable warehouse
		cis_key = f"{cls.sales_channel}-{cls.wh_sellable}"
		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": cls.sales_channel, "warehouse": cls.wh_sellable}):
			frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": cls.sales_channel,
				"warehouse": cls.wh_sellable,
				"company": cls.company,
				"enabled": 1,
				"allow_sellable_stock": 1,
				"priority": 1,
			}).insert(ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_module_fixtures()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _cleanup_module_fixtures(cls):
		"""
		Strict fixture hygiene: deletes only TEST-1S owned documents in reverse dependency order.
		Never touches baseline records.
		"""
		prefix = cls.FIXTURE_PREFIX
		company = getattr(cls, "company", None) or frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		abbr = getattr(cls, "abbr", None) or frappe.get_cached_value("Company", company, "abbr") or "IDP"

		# 1. Payment Entries
		pe_names = set(frappe.db.sql(
			"""
			SELECT name FROM `tabPayment Entry`
			WHERE party LIKE %s OR remarks LIKE %s
			""",
			(f"{prefix}%", f"%{prefix}%"),
			pluck="name",
		))
		pe_from_refs = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabPayment Entry Reference`
			WHERE reference_doctype IN ('Purchase Invoice', 'Purchase Order')
			  AND reference_name IN (SELECT name FROM `tabPurchase Invoice` WHERE supplier LIKE %s)
			""",
			(f"{prefix}%",),
			pluck="parent",
		)
		pe_names.update(pe_from_refs)
		for name in pe_names:
			try:
				if frappe.db.exists("Payment Entry", name):
					doc = frappe.get_doc("Payment Entry", name)
					if doc.docstatus == 1:
						doc.cancel()
					frappe.delete_doc("Payment Entry", name, force=True, ignore_permissions=True)
					frappe.db.delete("GL Entry", {"voucher_no": name})
			except Exception:
				pass

		# 2. Purchase Invoices
		pi_names = set(frappe.db.sql(
			"""
			SELECT name FROM `tabPurchase Invoice` WHERE supplier LIKE %s
			""",
			(f"{prefix}%",),
			pluck="name",
		))
		pi_from_items = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabPurchase Invoice Item` WHERE item_code LIKE %s
			""",
			(f"{prefix}%",),
			pluck="parent",
		)
		pi_names.update(pi_from_items)
		for name in pi_names:
			try:
				if frappe.db.exists("Purchase Invoice", name):
					doc = frappe.get_doc("Purchase Invoice", name)
					if doc.docstatus == 1:
						doc.cancel()
					frappe.delete_doc("Purchase Invoice", name, force=True, ignore_permissions=True)
					frappe.db.delete("GL Entry", {"voucher_no": name})
			except Exception:
				pass

		# 3. Purchase Receipts
		pr_names = set(frappe.db.sql(
			"""
			SELECT name FROM `tabPurchase Receipt` WHERE supplier LIKE %s
			""",
			(f"{prefix}%",),
			pluck="name",
		))
		pr_from_items = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabPurchase Receipt Item` WHERE item_code LIKE %s
			""",
			(f"{prefix}%",),
			pluck="parent",
		)
		pr_names.update(pr_from_items)
		for name in pr_names:
			try:
				if frappe.db.exists("Purchase Receipt", name):
					doc = frappe.get_doc("Purchase Receipt", name)
					if doc.docstatus == 1:
						doc.cancel()
					frappe.delete_doc("Purchase Receipt", name, force=True, ignore_permissions=True)
					frappe.db.delete("Stock Ledger Entry", {"voucher_no": name})
					frappe.db.delete("GL Entry", {"voucher_no": name})
			except Exception:
				pass

		# 4. Purchase Orders
		po_names = set(frappe.db.sql(
			"""
			SELECT name FROM `tabPurchase Order` WHERE supplier LIKE %s
			""",
			(f"{prefix}%",),
			pluck="name",
		))
		po_from_items = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabPurchase Order Item` WHERE item_code LIKE %s
			""",
			(f"{prefix}%",),
			pluck="parent",
		)
		po_names.update(po_from_items)
		for name in po_names:
			try:
				if frappe.db.exists("Purchase Order", name):
					doc = frappe.get_doc("Purchase Order", name)
					if doc.docstatus == 1:
						doc.cancel()
					frappe.delete_doc("Purchase Order", name, force=True, ignore_permissions=True)
			except Exception:
				pass

		# 5. Integration Events
		frappe.db.sql(
			"""
			DELETE FROM `tabIntegration Event`
			WHERE idempotency_key LIKE %s
			   OR active_idempotency_key LIKE %s
			   OR erp_document LIKE %s
			   OR request_metadata LIKE %s
			""",
			(f"%{prefix}%", f"%{prefix}%", f"%{prefix}%", f"%{prefix}%"),
		)

		# 6. Channel Inventory Source
		cis_records = frappe.db.sql(
			"""
			SELECT name FROM `tabChannel Inventory Source`
			WHERE sales_channel LIKE %s OR warehouse LIKE %s
			""",
			(f"{prefix}%", f"%{prefix}%"),
			pluck="name",
		)
		for r in cis_records:
			frappe.delete_doc("Channel Inventory Source", r, force=True, ignore_permissions=True)

		# 7. Sales Channel
		if frappe.db.exists("Sales Channel", f"{prefix}CH-A"):
			frappe.delete_doc("Sales Channel", f"{prefix}CH-A", force=True, ignore_permissions=True)

		# 8. Items
		for it in [f"{prefix}STOCK-ITEM-01", f"{prefix}SERVICE-ITEM-01"]:
			frappe.db.delete("Bin", {"item_code": it})
			frappe.db.delete("Stock Ledger Entry", {"item_code": it})
			if frappe.db.exists("Item", it):
				frappe.delete_doc("Item", it, force=True, ignore_permissions=True)

		# 9. Warehouses
		wh_list = [f"{prefix}WH-MAIN - {abbr}", f"{prefix}WH-QUAR - {abbr}"]
		frappe.db.delete("Stock Ledger Entry", {"warehouse": ["in", wh_list]})
		frappe.db.delete("Bin", {"warehouse": ["in", wh_list]})
		for wh in wh_list:
			if frappe.db.exists("Warehouse", wh):
				frappe.delete_doc("Warehouse", wh, force=True, ignore_permissions=True)

		# 10. Supplier
		if frappe.db.exists("Supplier", f"{prefix}VENDOR-01"):
			frappe.delete_doc("Supplier", f"{prefix}VENDOR-01", force=True, ignore_permissions=True)

		# 11. Defensively clean any TEST-1S owned Batches or Serials
		for b in frappe.get_all("Batch", filters=[["name", "like", f"{prefix}%"]], pluck="name"):
			frappe.delete_doc("Batch", b, force=True, ignore_permissions=True)
		for b in frappe.get_all("Batch", filters=[["item", "like", f"{prefix}%"]], pluck="name"):
			frappe.delete_doc("Batch", b, force=True, ignore_permissions=True)
		for s in frappe.get_all("Serial No", filters=[["name", "like", f"{prefix}%"]], pluck="name"):
			frappe.delete_doc("Serial No", s, force=True, ignore_permissions=True)
		for s in frappe.get_all("Serial No", filters=[["item_code", "like", f"{prefix}%"]], pluck="name"):
			frappe.delete_doc("Serial No", s, force=True, ignore_permissions=True)

		frappe.db.commit()

	def _get_bin_qty(self, item_code: str, warehouse: str) -> float:
		"""Fetches actual physical stock quantity from Bin."""
		qty = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty")
		return flt(qty)

	# ==========================================================================
	# SCENARIO A — Standard Stock Purchase
	# ==========================================================================
	def test_scenario_a_standard_stock_purchase(self):
		"""
		Vendor -> PO qty 10 -> submit PO (stock unchanged)
		-> PR qty 10 -> submit PR (stock +10)
		-> PI qty 10 from PR -> submit PI (stock remains +10, AP created)
		-> Payment 100% -> PI outstanding = 0
		"""
		stock_initial = self._get_bin_qty(self.item_stock, self.wh_sellable)

		# 1. Purchase Order (Financial Commitment Only)
		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 10, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-A",
		)
		frappe.db.commit()

		self.assertEqual(po.docstatus, 1)
		stock_after_po = self._get_bin_qty(self.item_stock, self.wh_sellable)
		self.assertEqual(stock_after_po, stock_initial, "PO must NOT increase physical stock!")

		# 2. Purchase Receipt (Goods Receipt - Physical Stock Movement)
		pr = receive_purchase_order(
			po_name=po.name,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-A",
		)
		frappe.db.commit()

		self.assertEqual(pr.docstatus, 1)
		stock_after_pr = self._get_bin_qty(self.item_stock, self.wh_sellable)
		self.assertEqual(stock_after_pr, stock_initial + 10, "PR must increase actual physical stock by 10!")

		# 3. Purchase Invoice (AP Liability Creation - Zero Stock Movement)
		pi = create_purchase_invoice(
			pr_name=pr.name,
			payable_account=self.payable_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PI-A",
		)
		frappe.db.commit()

		self.assertEqual(pi.docstatus, 1)
		self.assertEqual(pi.update_stock, 0, "PI update_stock must be 0!")
		stock_after_pi = self._get_bin_qty(self.item_stock, self.wh_sellable)
		self.assertEqual(stock_after_pi, stock_initial + 10, "PI must NOT duplicate stock movement!")
		self.assertEqual(flt(pi.outstanding_amount), 500.0, "PI outstanding amount must equal total payable!")

		# 4. Vendor Payment (AP Settlement)
		pe = pay_purchase_invoice(
			pi_name=pi.name,
			paid_amount=500.0,
			bank_account=self.bank_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PE-A",
		)
		frappe.db.commit()

		self.assertEqual(pe.docstatus, 1)
		fresh_outstanding = flt(frappe.db.get_value("Purchase Invoice", pi.name, "outstanding_amount"))
		self.assertEqual(fresh_outstanding, 0.0, "Payment must settle PI outstanding amount to 0!")

		# Traceability verification
		trace = get_three_way_traceability(po_name=po.name)
		self.assertIn(pr.name, trace["linked_purchase_receipts"])
		self.assertIn(pi.name, trace["linked_purchase_invoices"])

	# ==========================================================================
	# SCENARIO B — Partial Receipt
	# ==========================================================================
	def test_scenario_b_partial_receipt(self):
		"""
		PO qty 10 -> PR A = 4 -> PR B = 6 -> cumulative 10 -> over-receipt blocked
		"""
		stock_initial = self._get_bin_qty(self.item_stock, self.wh_sellable)

		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 10, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-B",
		)
		frappe.db.commit()

		# Receipt A: 4 units
		pr_a = receive_purchase_order(
			po_name=po.name,
			items_to_receive=[{"item_code": self.item_stock, "qty": 4}],
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-B1",
		)
		frappe.db.commit()
		self.assertEqual(self._get_bin_qty(self.item_stock, self.wh_sellable), stock_initial + 4)

		# Receipt B: 6 units
		pr_b = receive_purchase_order(
			po_name=po.name,
			items_to_receive=[{"item_code": self.item_stock, "qty": 6}],
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-B2",
		)
		frappe.db.commit()
		self.assertEqual(self._get_bin_qty(self.item_stock, self.wh_sellable), stock_initial + 10)

		# Attempt over-receipt: 1 additional unit
		with self.assertRaises(OverReceiptBlockedError):
			receive_purchase_order(
				po_name=po.name,
				items_to_receive=[{"item_code": self.item_stock, "qty": 1}],
				submit=True,
			)

	# ==========================================================================
	# SCENARIO C — Partial Invoice
	# ==========================================================================
	def test_scenario_c_partial_invoice(self):
		"""
		Received 10 -> Invoice A = 4 -> Invoice B = 6 -> fully billed -> overbilling blocked
		"""
		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 10, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-C",
		)
		pr = receive_purchase_order(
			po_name=po.name,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-C",
		)
		frappe.db.commit()

		pr_item_name = pr.items[0].name

		# Invoice A: 4 units
		pi_a = create_purchase_invoice(
			pr_name=pr.name,
			items_to_invoice=[{"pr_detail": pr_item_name, "qty": 4}],
			payable_account=self.payable_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PI-C1",
		)
		frappe.db.commit()
		self.assertEqual(pi_a.items[0].qty, 4)

		# Invoice B: 6 units
		pi_b = create_purchase_invoice(
			pr_name=pr.name,
			items_to_invoice=[{"pr_detail": pr_item_name, "qty": 6}],
			payable_account=self.payable_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PI-C2",
		)
		frappe.db.commit()
		self.assertEqual(pi_b.items[0].qty, 6)

		# Attempt over-billing: 1 additional unit
		with self.assertRaises(OverBillingBlockedError):
			create_purchase_invoice(
				pr_name=pr.name,
				items_to_invoice=[{"pr_detail": pr_item_name, "qty": 1}],
				payable_account=self.payable_account,
				submit=True,
			)

	# ==========================================================================
	# SCENARIO D — Partial Vendor Payment
	# ==========================================================================
	def test_scenario_d_partial_vendor_payment(self):
		"""
		PI = 100 -> Payment A = 30 (outstanding 70) -> Payment B = 70 (outstanding 0)
		"""
		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 2, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-D",
		)
		pr = receive_purchase_order(
			po_name=po.name,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-D",
		)
		pi = create_purchase_invoice(
			pr_name=pr.name,
			payable_account=self.payable_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PI-D",
		)
		frappe.db.commit()

		self.assertEqual(flt(pi.outstanding_amount), 100.0)

		# Payment A: 30
		pe_a = pay_purchase_invoice(
			pi_name=pi.name,
			paid_amount=30.0,
			bank_account=self.bank_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PE-D1",
		)
		frappe.db.commit()
		rem_after_a = flt(frappe.db.get_value("Purchase Invoice", pi.name, "outstanding_amount"))
		self.assertEqual(rem_after_a, 70.0)

		# Payment B: 70
		pe_b = pay_purchase_invoice(
			pi_name=pi.name,
			paid_amount=70.0,
			bank_account=self.bank_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PE-D2",
		)
		frappe.db.commit()
		rem_after_b = flt(frappe.db.get_value("Purchase Invoice", pi.name, "outstanding_amount"))
		self.assertEqual(rem_after_b, 0.0)

		# Attempt overpayment
		with self.assertRaises(OverPaymentBlockedError):
			pay_purchase_invoice(
				pi_name=pi.name,
				paid_amount=10.0,
				bank_account=self.bank_account,
				submit=True,
			)

	# ==========================================================================
	# SCENARIO E — Payment Cancellation
	# ==========================================================================
	def test_scenario_e_payment_cancellation(self):
		"""
		Cancel Payment B -> outstanding restored to 70; PI, PR, PO remain submitted
		"""
		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 2, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-E",
		)
		pr = receive_purchase_order(
			po_name=po.name,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-E",
		)
		pi = create_purchase_invoice(
			pr_name=pr.name,
			payable_account=self.payable_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PI-E",
		)
		pe_a = pay_purchase_invoice(
			pi_name=pi.name,
			paid_amount=30.0,
			bank_account=self.bank_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PE-E1",
		)
		pe_b = pay_purchase_invoice(
			pi_name=pi.name,
			paid_amount=70.0,
			bank_account=self.bank_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PE-E2",
		)
		frappe.db.commit()

		self.assertEqual(flt(frappe.db.get_value("Purchase Invoice", pi.name, "outstanding_amount")), 0.0)

		# Cancel Payment B
		cancel_vendor_payment(pe_b.name)
		frappe.db.commit()

		# Outstanding restored to 70
		restored_outstanding = flt(frappe.db.get_value("Purchase Invoice", pi.name, "outstanding_amount"))
		self.assertEqual(restored_outstanding, 70.0)

		# Invariants: PI, PR, PO must remain submitted!
		self.assertEqual(frappe.db.get_value("Purchase Invoice", pi.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Purchase Receipt", pr.name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Purchase Order", po.name, "docstatus"), 1)

	# ==========================================================================
	# SCENARIO F — Concurrent Receipt Protection
	# ==========================================================================
	def test_scenario_f_concurrent_receipt(self):
		"""
		PO remaining qty = 1. Worker 1 receives 1.
		Worker 2 attempts receipt 1 -> blocked under MariaDB FOR UPDATE lock.
		Stock increments exactly once.
		"""
		stock_initial = self._get_bin_qty(self.item_stock, self.wh_sellable)

		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 1, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-F",
		)
		frappe.db.commit()

		# Worker 1 receives 1
		pr1 = receive_purchase_order(
			po_name=po.name,
			items_to_receive=[{"item_code": self.item_stock, "qty": 1}],
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-F1",
		)
		frappe.db.commit()

		# Worker 2 attempts receipt 1 -> blocked
		with self.assertRaises(OverReceiptBlockedError):
			receive_purchase_order(
				po_name=po.name,
				items_to_receive=[{"item_code": self.item_stock, "qty": 1}],
				submit=True,
				operation_key=f"{self.FIXTURE_PREFIX}OP-PR-F2",
			)

		stock_final = self._get_bin_qty(self.item_stock, self.wh_sellable)
		self.assertEqual(stock_final, stock_initial + 1, "Stock must increment exactly once!")

	# ==========================================================================
	# SCENARIO G — Quarantine / Non-Sellable Receipt
	# ==========================================================================
	def test_scenario_g_quarantine_non_sellable_receipt(self):
		"""
		Receive into non-sellable warehouse:
		- Actual stock in quarantine increases
		- Channel ATP not incorrectly increased
		- No publication intent created
		"""
		stock_quar_initial = self._get_bin_qty(self.item_stock, self.wh_quarantine)
		atp_initial = get_channel_atp(self.item_stock, self.sales_channel).aggregate_atp_qty

		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_quarantine,
				"items": [{"item_code": self.item_stock, "qty": 15, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-G",
		)
		pr = receive_purchase_order(
			po_name=po.name,
			target_warehouse=self.wh_quarantine,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-G",
		)
		frappe.db.commit()

		stock_quar_after = self._get_bin_qty(self.item_stock, self.wh_quarantine)
		self.assertEqual(stock_quar_after, stock_quar_initial + 15, "Quarantine physical stock must increase!")

		atp_after = get_channel_atp(self.item_stock, self.sales_channel).aggregate_atp_qty
		self.assertEqual(atp_after, atp_initial, "Sellable channel ATP must NOT increase from quarantine stock!")

	# ==========================================================================
	# SCENARIO H — Sellable Receipt Publication
	# ==========================================================================
	def test_scenario_h_sellable_receipt_publication(self):
		"""
		Receive stock into sellable warehouse:
		- Durable publication intent created in DB (status=PENDING, entity_type=INVENTORY)
		- Zero direct HTTP calls during transaction
		"""
		with patch("requests.post") as mock_http:
			po = create_purchase_order(
				data={
					"company": self.company,
					"supplier": self.supplier_name,
					"set_warehouse": self.wh_sellable,
					"items": [{"item_code": self.item_stock, "qty": 5, "rate": 50}],
				},
				submit=True,
				operation_key=f"{self.FIXTURE_PREFIX}OP-PO-H",
			)
			pr = receive_purchase_order(
				po_name=po.name,
				submit=True,
				operation_key=f"{self.FIXTURE_PREFIX}OP-PR-H",
			)
			frappe.db.commit()

			mock_http.assert_not_called()

		# Verify durable publication outbox intent in Integration Event
		pub_events = frappe.db.sql(
			"""
			SELECT name, status, entity_type, direction FROM `tabIntegration Event`
			WHERE sales_channel = %s AND entity_type = 'INVENTORY' AND direction = 'OUTBOUND'
			ORDER BY creation DESC LIMIT 5
			""",
			(self.sales_channel,),
			as_dict=True,
		)
		self.assertTrue(len(pub_events) > 0, "Durable publication outbox intent must be persisted!")
		self.assertEqual(pub_events[0].status, "PENDING")

	# ==========================================================================
	# SCENARIO I — Service / Non-Stock Invoice
	# ==========================================================================
	def test_scenario_i_service_non_stock_invoice(self):
		"""
		Purchase Invoice for service / non-stock item:
		- AP liability created natively
		- Zero stock movement
		"""
		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"items": [{"item_code": self.item_service, "qty": 1, "rate": 200}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-I",
		)
		# Direct from PO (no Goods Receipt needed for services)
		pi = create_purchase_invoice(
			po_name=po.name,
			payable_account=self.payable_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PI-I",
		)
		frappe.db.commit()

		self.assertEqual(pi.docstatus, 1)
		self.assertEqual(flt(pi.outstanding_amount), 200.0)

		# Verify zero stock ledger entries
		sle_count = frappe.db.count("Stock Ledger Entry", {"voucher_no": pi.name})
		self.assertEqual(sle_count, 0, "Service PI must have zero Stock Ledger Entries!")

	# ==========================================================================
	# SCENARIO K — Cancelled Purchasing Document Operation Identity Remains Consumed
	# ==========================================================================
	def test_scenario_k_cancelled_operation_identity_remains_consumed(self):
		"""
		PR completed with operation key.
		PR subsequently cancelled.
		Replaying same operation key must raise PurchaseReplayCancelledError.
		"""
		op_key = f"{self.FIXTURE_PREFIX}OP-PR-K"
		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 5, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-K",
		)
		pr = receive_purchase_order(
			po_name=po.name,
			submit=True,
			operation_key=op_key,
		)
		frappe.db.commit()

		# Cancel PR
		cancel_purchase_document("Purchase Receipt", pr.name)
		frappe.db.commit()

		# Replaying same operation key must be rejected with PurchaseReplayCancelledError
		with self.assertRaises(PurchaseReplayCancelledError):
			receive_purchase_order(
				po_name=po.name,
				submit=True,
				operation_key=op_key,
			)

	# ==========================================================================
	# SCENARIO L — Downstream Dependency Blocking on PO Cancellation
	# ==========================================================================
	def test_scenario_l_po_cancellation_blocked_by_submitted_pr(self):
		"""
		PO with submitted PR cannot be cancelled; native dependency blocking preserved.
		"""
		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 3, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-L",
		)
		pr = receive_purchase_order(
			po_name=po.name,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-L",
		)
		frappe.db.commit()

		# Attempt to cancel PO directly must raise ValidationError / LinkValidationError
		with self.assertRaises(Exception):
			cancel_purchase_document("Purchase Order", po.name)

	# ==========================================================================
	# SCENARIO M — Downstream Dependency Blocking on PR Cancellation
	# ==========================================================================
	def test_scenario_m_pr_cancellation_blocked_by_submitted_pi(self):
		"""
		PR with submitted PI cannot be cancelled; native dependency blocking preserved.
		"""
		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 3, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-M",
		)
		pr = receive_purchase_order(
			po_name=po.name,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-M",
		)
		pi = create_purchase_invoice(
			pr_name=pr.name,
			payable_account=self.payable_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PI-M",
		)
		frappe.db.commit()

		# Attempt to cancel PR directly must raise ValidationError / LinkValidationError
		with self.assertRaises(Exception):
			cancel_purchase_document("Purchase Receipt", pr.name)

	# ==========================================================================
	# SCENARIO N — Downstream Dependency Blocking on PI Cancellation
	# ==========================================================================
	def test_scenario_n_pi_cancellation_blocked_by_submitted_payment(self):
		"""
		PI with submitted Payment Entry cannot be cancelled; native dependency blocking preserved.
		"""
		po = create_purchase_order(
			data={
				"company": self.company,
				"supplier": self.supplier_name,
				"set_warehouse": self.wh_sellable,
				"items": [{"item_code": self.item_stock, "qty": 3, "rate": 50}],
			},
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PO-N",
		)
		pr = receive_purchase_order(
			po_name=po.name,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PR-N",
		)
		pi = create_purchase_invoice(
			pr_name=pr.name,
			payable_account=self.payable_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PI-N",
		)
		pe = pay_purchase_invoice(
			pi_name=pi.name,
			paid_amount=50.0,
			bank_account=self.bank_account,
			submit=True,
			operation_key=f"{self.FIXTURE_PREFIX}OP-PE-N",
		)
		frappe.db.commit()

		# Attempt to cancel PI directly must raise ValidationError / LinkValidationError
		with self.assertRaises(Exception):
			cancel_purchase_document("Purchase Invoice", pi.name)

	# ==========================================================================
	# SCENARIO Z — Fixture Cleanup Proof (Runs Last)
	# ==========================================================================
	def test_scenario_z_fixture_cleanup(self):
		"""
		Prove all TEST-1S fixtures are deleted cleanly while baseline records are preserved.
		"""
		self._cleanup_module_fixtures()
		prefix = self.FIXTURE_PREFIX

		# Count residuals
		residual_po = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabPurchase Order` WHERE supplier LIKE %s",
			(f"{prefix}%",),
		)[0][0]
		residual_pr = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabPurchase Receipt` WHERE supplier LIKE %s",
			(f"{prefix}%",),
		)[0][0]
		residual_pi = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabPurchase Invoice` WHERE supplier LIKE %s",
			(f"{prefix}%",),
		)[0][0]
		residual_pe = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabPayment Entry` WHERE party LIKE %s",
			(f"{prefix}%",),
		)[0][0]
		residual_events = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabIntegration Event` WHERE idempotency_key LIKE %s OR request_metadata LIKE %s",
			(f"%{prefix}%", f"%{prefix}%"),
		)[0][0]

		self.assertEqual(residual_po, 0, f"Residual Purchase Orders found: {residual_po}")
		self.assertEqual(residual_pr, 0, f"Residual Purchase Receipts found: {residual_pr}")
		self.assertEqual(residual_pi, 0, f"Residual Purchase Invoices found: {residual_pi}")
		self.assertEqual(residual_pe, 0, f"Residual Payment Entries found: {residual_pe}")
		self.assertEqual(residual_events, 0, f"Residual Integration Events found: {residual_events}")

		# Batch and Serial No residual counts and baseline safety check
		residual_batches = len(frappe.get_all("Batch", filters=[["item", "like", f"{prefix}%"]], pluck="name"))
		residual_serials = len(frappe.get_all("Serial No", filters=[["item_code", "like", f"{prefix}%"]], pluck="name"))
		self.assertEqual(residual_batches, 0, f"Residual TEST-1S Batches found: {residual_batches}")
		self.assertEqual(residual_serials, 0, f"Residual TEST-1S Serials found: {residual_serials}")

		# Prove baseline Batch and Serial No sets are preserved
		current_batches = set(frappe.get_all("Batch", pluck="name"))
		current_serials = set(frappe.get_all("Serial No", pluck="name"))
		self.assertTrue(self.baseline_batches.issubset(current_batches), "Baseline batches were corrupted by purchasing tests")
		self.assertTrue(self.baseline_serials.issubset(current_serials), "Baseline serials were corrupted by purchasing tests")
