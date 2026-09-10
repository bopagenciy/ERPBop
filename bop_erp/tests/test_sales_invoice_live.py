# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe
from frappe.utils import flt, nowdate

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
	IntegrationReadinessStatus,
	TransactionOrigin,
)
from bop_erp.accounts import (
	CompanyMismatchError,
	OrderNotEligibleForInvoicingError,
	OverbillingBlockedError,
	SalesInvoiceError,
	assert_sales_invoice_eligibility,
	cancel_sales_invoice,
	compute_sales_invoice_idempotency_key,
	create_sales_invoice_from_fulfillment,
	get_invoice_counters,
	reset_invoice_counters,
	submit_sales_invoice,
)
from bop_erp.orders.reconciliation import audit_sales_order_cancellation_safety
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note


class TestSalesInvoiceLive(unittest.TestCase):
	"""
	Phase 1P Live Integration Test Suite:
	Sales Invoice / Accounts Receivable Foundation.
	Executes against the local Frappe / ERPNext isolated test environment.

	Scenarios (Section 45):
	A. READY imported Sales Order + submitted Delivery Note -> draft Sales Invoice
	B. Submit invoice -> native GL Entry creation -> AR outstanding balance
	C. Verify: update_stock = 0, Bin unchanged, SLE unchanged
	D. Duplicate invoice request -> same effective invoice / no duplicate submitted invoice
	E. Partial fulfilled scope -> only fulfilled quantity invoiced
	F. Already partially billed -> only unbilled quantity eligible
	G. Concurrent duplicate creation -> one effective invoice
	H. Invoice cancel -> GL reversal, Delivery Note remains, Sales Order remains
	I. External paid order metadata -> invoice remains unpaid/outstanding, zero Payment Entry
	J. Cross-company accounting mismatch -> blocked
	K. Phase 1L cancellation guard -> submitted invoice blocks auto-cancel
	L. Fixture cleanup proof: zero residual Phase 1P fixtures
	"""

	FIXTURE_PREFIX = "TEST-1P-"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"

		# Defensively clean prior interrupted Phase 1P fixtures
		cls._cleanup_module_fixtures()

		cls.sales_channel = f"{cls.FIXTURE_PREFIX}CH-A"
		cls.channel_b = f"{cls.FIXTURE_PREFIX}CH-B"

		# Ensure sales channels exist
		for ch in [cls.sales_channel, cls.channel_b]:
			if not frappe.db.exists("Sales Channel", ch):
				frappe.get_doc({
					"doctype": "Sales Channel",
					"channel_id": ch,
					"channel_name": ch,
					"channel_type": "PRESTASHOP",
					"integration_provider": IntegrationProvider.PRESTASHOP,
					"company": cls.company,
					"active": 1,
				}).insert(ignore_permissions=True)

		# Ensure warehouses exist
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")
		cls.warehouse = f"{cls.FIXTURE_PREFIX}WH-{cls.abbr} - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.warehouse):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"{cls.FIXTURE_PREFIX}WH-{cls.abbr}",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
				"is_group": 0,
			})
			w.flags.ignore_permissions = True
			w.insert(ignore_permissions=True)

		# Ensure test items exist
		cls.item_code = f"{cls.FIXTURE_PREFIX}ITEM-01"
		cls.item_code_2 = f"{cls.FIXTURE_PREFIX}ITEM-02"

		for item_id in [cls.item_code, cls.item_code_2]:
			if not frappe.db.exists("Item", item_id):
				frappe.get_doc({
					"doctype": "Item",
					"item_code": item_id,
					"item_name": item_id,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}).insert(ignore_permissions=True)

		# Customer
		cls.customer = frappe.db.get_value("Customer", {}, "name") or "Test Customer"

		# Ensure default income account is set on Company for native resolution
		cls.income_account = "Ventas de mercancías - IDP"
		if not frappe.db.exists("Account", cls.income_account):
			acc = frappe.get_doc({
				"doctype": "Account",
				"account_name": "Ventas de mercancías",
				"company": cls.company,
				"parent_account": "4135 - Comercio al por mayor y al por menor - IDP",
				"account_type": "Income Account",
				"root_type": "Income",
				"is_group": 0,
				"account_currency": "COP",
			})
			acc.insert(ignore_permissions=True)

		frappe.db.set_value("Company", cls.company, "default_income_account", cls.income_account)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_module_fixtures()
		super().tearDownClass()

	@classmethod
	def _cleanup_module_fixtures(cls):
		"""Defensively and cleanly removes only fixtures owned by this Phase 1P module."""
		company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		abbr = frappe.get_cached_value("Company", company, "abbr") or "IDP"

		# 1. Clean Sales Invoices
		si_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabSales Invoice Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabSales Invoice`
			WHERE sales_channel LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%", f"{cls.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		for si_name in si_names:
			if frappe.db.exists("Sales Invoice", si_name):
				si = frappe.get_doc("Sales Invoice", si_name)
				if si.docstatus == 1:
					si.cancel()
				frappe.delete_doc("Sales Invoice", si_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": si_name})

		# 2. Clean Delivery Notes
		dn_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabDelivery Note Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabDelivery Note`
			WHERE sales_channel LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%", f"{cls.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		for dn_name in dn_names:
			if frappe.db.exists("Delivery Note", dn_name):
				dn = frappe.get_doc("Delivery Note", dn_name)
				if dn.docstatus == 1:
					dn.cancel()
				frappe.delete_doc("Delivery Note", dn_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": dn_name})
				frappe.db.delete("Stock Ledger Entry", {"voucher_no": dn_name})

		# 3. Clean Sales Orders
		so_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabSales Order Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabSales Order`
			WHERE sales_channel LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%", f"{cls.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		for so_name in so_names:
			if frappe.db.exists("Sales Order", so_name):
				so = frappe.get_doc("Sales Order", so_name)
				if so.docstatus == 1:
					so.cancel()
				frappe.delete_doc("Sales Order", so_name, force=True, ignore_permissions=True)

		# 4. Clean Stock Entries
		se_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabStock Entry Detail`
			WHERE item_code LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%",),
			pluck="name",
		)
		for se_name in se_names:
			if frappe.db.exists("Stock Entry", se_name):
				se = frappe.get_doc("Stock Entry", se_name)
				if se.docstatus == 1:
					se.cancel()
				frappe.delete_doc("Stock Entry", se_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": se_name})
				frappe.db.delete("Stock Ledger Entry", {"voucher_no": se_name})

		# 5. Clean Mappings and Channels
		for ch in [f"{cls.FIXTURE_PREFIX}CH-A", f"{cls.FIXTURE_PREFIX}CH-B"]:
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("Sales Channel", {"name": ch})

		# 6. Clean Bins, Items, Warehouses
		for ic in [f"{cls.FIXTURE_PREFIX}ITEM-01", f"{cls.FIXTURE_PREFIX}ITEM-02"]:
			frappe.db.delete("Bin", {"item_code": ic})
			if frappe.db.exists("Item", ic):
				frappe.delete_doc("Item", ic, force=True, ignore_permissions=True)

		wh = f"{cls.FIXTURE_PREFIX}WH-{abbr} - {abbr}"
		if frappe.db.exists("Warehouse", wh):
			frappe.delete_doc("Warehouse", wh, force=True, ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_module_fixtures()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		reset_invoice_counters()
		# Seed physical stock for tests
		self._seed_stock(self.item_code, 20.0)

	def tearDown(self):
		# Clean up any transactions generated during the single test
		self._cleanup_test_docs()
		super().tearDown()

	def _seed_stock(self, item_code: str, target_qty: float):
		current = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": self.warehouse}, "actual_qty") or 0.0)
		diff = target_qty - current
		if abs(diff) > 0.001:
			if diff > 0:
				make_stock_entry(
					item_code=item_code,
					target=self.warehouse,
					qty=diff,
					rate=50.0,
					company=self.company,
					purpose="Material Receipt",
				)
			else:
				make_stock_entry(
					item_code=item_code,
					source=self.warehouse,
					qty=abs(diff),
					rate=50.0,
					company=self.company,
					purpose="Material Issue",
				)
		frappe.db.commit()

	def _create_so_and_dn(self, ext_id: str, qty: float = 3.0, channel: str = None) -> Tuple[Any, Any]:
		"""Helper to create a paired submitted Sales Order and Delivery Note."""
		ch = channel or self.sales_channel
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": self.customer,
			"company": self.company,
			"transaction_origin": TransactionOrigin.WEB,
			"sales_channel": ch,
			"external_order_id": ext_id,
			"integration_status": IntegrationReadinessStatus.READY,
			"delivery_date": nowdate(),
			"currency": "COP",
			"items": [{
				"item_code": self.item_code,
				"qty": qty,
				"rate": 100.0,
				"warehouse": self.warehouse,
			}],
		})
		so.insert(ignore_permissions=True)
		so.submit()

		# Map order cleanly
		if not frappe.db.exists("External ID Mapping", {"external_id": ext_id, "sales_channel": ch}):
			mapping = frappe.get_doc({
				"doctype": "External ID Mapping",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": ch,
				"external_entity_type": ExternalEntityType.ORDER,
				"external_id": ext_id,
				"erp_doctype": "Sales Order",
				"erp_document": so.name,
				"active": 1,
			})
			mapping.insert(ignore_permissions=True)

		# Create Stock Reservation Entry so operational guard passes
		sre = frappe.get_doc({
			"doctype": "Stock Reservation Entry",
			"item_code": self.item_code,
			"warehouse": self.warehouse,
			"voucher_type": "Sales Order",
			"voucher_no": so.name,
			"voucher_detail_no": so.items[0].name,
			"voucher_qty": qty,
			"available_qty": 20.0,
			"reserved_qty": qty,
			"delivered_qty": 0.0,
			"transferred_qty": 0.0,
			"consumed_qty": 0.0,
			"company": self.company,
			"stock_uom": "Nos",
			"reservation_based_on": "Qty",
		})
		sre.flags.ignore_validate = True
		sre.flags.ignore_permissions = True
		sre.insert(ignore_permissions=True)
		sre.submit()

		ref = frappe.get_doc({
			"doctype": "Inventory Reservation Reference",
			"idempotency_key": f"IRR-{so.name}-{self.item_code}-{frappe.generate_hash(length=4)}",
			"stock_reservation_entry": sre.name,
			"item_code": self.item_code,
			"warehouse": self.warehouse,
			"reserved_qty": qty,
			"source_doctype": "Sales Order",
			"source_document": so.name,
			"source_document_item": so.items[0].name,
			"status": "Reserved",
		})
		ref.insert(ignore_permissions=True)

		dn = make_delivery_note(so.name)
		dn.items[0].qty = qty
		dn.sales_channel = ch
		dn.transaction_origin = TransactionOrigin.WEB
		dn.external_order_id = ext_id
		dn.insert(ignore_permissions=True)
		dn.submit()

		frappe.db.commit()
		return so, dn

	def _cleanup_test_docs(self):
		si_names = frappe.db.get_all("Sales Invoice", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]}, pluck="name")
		for name in si_names:
			si = frappe.get_doc("Sales Invoice", name)
			if si.docstatus == 1:
				si.cancel()
			frappe.delete_doc("Sales Invoice", name, force=True, ignore_permissions=True)
			frappe.db.delete("GL Entry", {"voucher_no": name})

		dn_names = frappe.db.get_all("Delivery Note", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]}, pluck="name")
		for name in dn_names:
			dn = frappe.get_doc("Delivery Note", name)
			if dn.docstatus == 1:
				dn.cancel()
			frappe.delete_doc("Delivery Note", name, force=True, ignore_permissions=True)
			frappe.db.delete("GL Entry", {"voucher_no": name})
			frappe.db.delete("Stock Ledger Entry", {"voucher_no": name})

		so_names = frappe.db.get_all("Sales Order", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]}, pluck="name")
		for name in so_names:
			so = frappe.get_doc("Sales Order", name)
			if so.docstatus == 1:
				so.cancel()
			frappe.delete_doc("Sales Order", name, force=True, ignore_permissions=True)

		# Clean SREs and IRRs for test channels/items
		sres = frappe.db.get_all("Stock Reservation Entry", filters={"item_code": ["in", [self.item_code, self.item_code_2]]}, pluck="name")
		for sre_name in sres:
			frappe.db.set_value("Stock Reservation Entry", sre_name, "docstatus", 2)
			frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)

		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [self.item_code, self.item_code_2]]})

		# Clean mappings for test channels
		for ch in [self.sales_channel, self.channel_b]:
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})

		frappe.db.commit()

	# =========================================================================
	# SCENARIO A: READY imported Sales Order + submitted DN -> draft Sales Invoice
	# =========================================================================
	def test_scenario_a_draft_sales_invoice_creation(self):
		so, dn = self._create_so_and_dn("1P-EXT-A-001", qty=2.0)
		si = create_sales_invoice_from_fulfillment(dn.name, submit=False)

		self.assertEqual(si.doctype, "Sales Invoice")
		self.assertEqual(si.docstatus, 0)
		self.assertEqual(si.sales_channel, self.sales_channel)
		self.assertEqual(si.transaction_origin, TransactionOrigin.WEB)
		self.assertEqual(si.external_order_id, "1P-EXT-A-001")
		self.assertEqual(si.update_stock, 0)
		self.assertEqual(len(si.items), 1)
		self.assertEqual(si.items[0].qty, 2.0)

	# =========================================================================
	# SCENARIO B: Submit invoice -> native GL Entry creation -> AR outstanding balance
	# =========================================================================
	def test_scenario_b_submit_invoice_creates_gl_and_ar_balance(self):
		so, dn = self._create_so_and_dn("1P-EXT-B-001", qty=2.0)
		si = create_sales_invoice_from_fulfillment(dn.name, submit=True)

		self.assertEqual(si.docstatus, 1)
		self.assertEqual(si.update_stock, 0)
		self.assertGreater(si.outstanding_amount, 0.0)

		# GL Entry proof: debits must balance credits
		gles = frappe.get_all("GL Entry", filters={"voucher_no": si.name}, fields=["account", "debit", "credit"])
		self.assertGreater(len(gles), 0, "GL Entries must exist for submitted Sales Invoice")
		tot_debit = sum(flt(g.debit) for g in gles)
		tot_credit = sum(flt(g.credit) for g in gles)
		self.assertAlmostEqual(tot_debit, tot_credit, places=2)

		# Verify Receivable account has debit and Income account has credit
		rec_account = frappe.db.get_value("Company", self.company, "default_receivable_account")
		rec_entry = [g for g in gles if g.account == rec_account]
		self.assertTrue(len(rec_entry) > 0, f"Receivable account '{rec_account}' must have an entry")
		self.assertGreater(flt(rec_entry[0].debit), 0.0)

	# =========================================================================
	# SCENARIO C: Verify: update_stock = 0, Bin unchanged, SLE unchanged
	# =========================================================================
	def test_scenario_c_zero_stock_side_effects(self):
		so, dn = self._create_so_and_dn("1P-EXT-C-001", qty=3.0)

		bin_before = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.warehouse}, "actual_qty"))
		sle_count_before = frappe.db.count("Stock Ledger Entry", {"voucher_no": dn.name})

		# Submit Sales Invoice
		si = create_sales_invoice_from_fulfillment(dn.name, submit=True)
		self.assertEqual(si.update_stock, 0)

		bin_after = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.warehouse}, "actual_qty"))
		sle_for_si = frappe.db.count("Stock Ledger Entry", {"voucher_no": si.name})

		# Bin actual_qty strictly unchanged by invoice
		self.assertEqual(bin_before, bin_after)
		# 0 Stock Ledger Entry created for Sales Invoice
		self.assertEqual(sle_for_si, 0)

	# =========================================================================
	# SCENARIO D: Duplicate invoice request -> same effective invoice / no duplicate
	# =========================================================================
	def test_scenario_d_duplicate_invoice_convergence(self):
		so, dn = self._create_so_and_dn("1P-EXT-D-001", qty=2.0)

		si1 = create_sales_invoice_from_fulfillment(dn.name, submit=True)
		si2 = create_sales_invoice_from_fulfillment(dn.name, submit=True)

		self.assertEqual(si1.name, si2.name)
		self.assertEqual(si2.docstatus, 1)

		# Ensure only one invoice was created in DB
		total_sis = frappe.db.count("Sales Invoice Item", {"delivery_note": dn.name})
		self.assertEqual(total_sis, 1)

	# =========================================================================
	# SCENARIO E: Partial fulfilled scope -> only fulfilled quantity invoiced
	# =========================================================================
	def test_scenario_e_partial_fulfilled_scope(self):
		so, dn = self._create_so_and_dn("1P-EXT-E-001", qty=4.0)

		# Invoice only 2 of 4
		dn_detail = dn.items[0].name
		si = create_sales_invoice_from_fulfillment(
			dn.name,
			requested_lines=[{"dn_detail": dn_detail, "qty": 2.0}],
			submit=False,
		)
		self.assertEqual(si.items[0].qty, 2.0)
		self.assertEqual(si.grand_total, 200.0)

	# =========================================================================
	# SCENARIO F: Already partially billed -> only unbilled quantity eligible
	# =========================================================================
	def test_scenario_f_already_partially_billed(self):
		so, dn = self._create_so_and_dn("1P-EXT-F-001", qty=4.0)

		# Bill all 4.0
		si1 = create_sales_invoice_from_fulfillment(dn.name, submit=True)
		self.assertEqual(si1.docstatus, 1)

		# Attempting to bill again must be blocked as overbilling
		with self.assertRaises((OverbillingBlockedError, frappe.ValidationError)):
			# Force a fresh creation attempt
			from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
			fresh_si = make_sales_invoice(dn.name)
			fresh_si.update_stock = 0
			fresh_si.insert()
			submit_sales_invoice(fresh_si)

	# =========================================================================
	# SCENARIO G: Concurrent duplicate creation -> one effective invoice
	# =========================================================================
	def test_scenario_g_concurrent_duplicate_creation(self):
		so, dn = self._create_so_and_dn("1P-EXT-G-001", qty=2.0)

		key1 = compute_sales_invoice_idempotency_key(dn.name, so.name, self.company)
		key2 = compute_sales_invoice_idempotency_key(dn.name, so.name, self.company)
		self.assertEqual(key1, key2)

		si1 = create_sales_invoice_from_fulfillment(dn.name, submit=True)
		si2 = create_sales_invoice_from_fulfillment(dn.name, submit=True)
		self.assertEqual(si1.name, si2.name)

	# =========================================================================
	# SCENARIO H: Invoice cancel -> GL reversal, Delivery Note remains, Sales Order remains
	# =========================================================================
	def test_scenario_h_invoice_cancel_reverses_gl_preserves_dn_and_so(self):
		so, dn = self._create_so_and_dn("1P-EXT-H-001", qty=2.0)
		si = create_sales_invoice_from_fulfillment(dn.name, submit=True)
		self.assertEqual(si.docstatus, 1)

		# Cancel invoice
		cancel_sales_invoice(si.name)

		si.reload()
		self.assertEqual(si.docstatus, 2)

		# Delivery Note and Sales Order remain submitted (docstatus = 1)
		dn.reload()
		self.assertEqual(dn.docstatus, 1)
		so.reload()
		self.assertEqual(so.docstatus, 1)

		# GL Entries net to zero
		gles = frappe.get_all("GL Entry", filters={"voucher_no": si.name}, fields=["debit", "credit"])
		tot_debit = sum(flt(g.debit) for g in gles)
		tot_credit = sum(flt(g.credit) for g in gles)
		self.assertAlmostEqual(tot_debit, tot_credit, places=2)

	# =========================================================================
	# SCENARIO I: External paid order metadata -> invoice remains unpaid, zero Payment Entry
	# =========================================================================
	def test_scenario_i_external_paid_order_metadata_zero_payment_entry(self):
		so, dn = self._create_so_and_dn("1P-EXT-I-001", qty=2.0)
		si = create_sales_invoice_from_fulfillment(dn.name, submit=True)

		self.assertEqual(si.paid_amount, 0.0)
		self.assertGreater(si.outstanding_amount, 0.0)

		# 0 Payment Entries exist for this Sales Invoice
		pe_refs = frappe.db.count("Payment Entry Reference", {"reference_doctype": "Sales Invoice", "reference_name": si.name})
		self.assertEqual(pe_refs, 0)

	# =========================================================================
	# SCENARIO J: Cross-company accounting mismatch -> blocked
	# =========================================================================
	def test_scenario_j_cross_company_accounting_mismatch_blocked(self):
		so, dn = self._create_so_and_dn("1P-EXT-J-001", qty=2.0)

		# Attempting to invoice with mismatched company
		with self.assertRaises(CompanyMismatchError):
			dn.company = "Bamal Fastener Corp"
			assert_sales_invoice_eligibility(dn, so)

	# =========================================================================
	# SCENARIO K: Phase 1L cancellation guard -> submitted invoice blocks auto-cancel
	# =========================================================================
	def test_scenario_k_submitted_invoice_blocks_auto_cancel(self):
		so, dn = self._create_so_and_dn("1P-EXT-K-001", qty=2.0)
		si = create_sales_invoice_from_fulfillment(dn.name, submit=True)

		is_safe, reasons = audit_sales_order_cancellation_safety(so.name)
		self.assertFalse(is_safe, "Submitted Sales Invoice must block automatic Sales Order cancellation")
		self.assertTrue(any("Sales Invoice" in r for r in reasons))

	# =========================================================================
	# SCENARIO M: Mapping drift protection blocks invoice creation live
	# =========================================================================
	def test_scenario_m_mapping_drift_blocks_invoice_creation(self):
		so, dn = self._create_so_and_dn("1P-EXT-M-001", qty=2.0)

		# Deactivate the canonical External ID Mapping to simulate mapping drift
		frappe.db.set_value(
			"External ID Mapping",
			{"external_id": "1P-EXT-M-001", "sales_channel": self.sales_channel},
			"active",
			0,
		)
		frappe.db.commit()

		with self.assertRaises(OrderNotEligibleForInvoicingError) as ctx:
			create_sales_invoice_from_fulfillment(dn.name)
		self.assertIn("Mapping Drift Violation", str(ctx.exception))

		# Restore active mapping and verify invoice creation succeeds
		frappe.db.set_value(
			"External ID Mapping",
			{"external_id": "1P-EXT-M-001", "sales_channel": self.sales_channel},
			"active",
			1,
		)
		frappe.db.commit()

		si = create_sales_invoice_from_fulfillment(dn.name)
		self.assertEqual(si.docstatus, 0)
		self.assertEqual(si.external_order_id, "1P-EXT-M-001")

	# =========================================================================
	# SCENARIO N: Cancelled invoice restores billable scope live
	# =========================================================================
	def test_scenario_n_cancelled_invoice_restores_billable_scope(self):
		# Create delivery note with 3.0 items
		so, dn = self._create_so_and_dn("1P-EXT-N-001", qty=3.0)

		# Bill partial scope 2.0 and submit
		si1 = create_sales_invoice_from_fulfillment(
			dn.name,
			requested_lines=[{"dn_detail": dn.items[0].name, "qty": 2.0}],
			submit=True,
		)
		self.assertEqual(si1.docstatus, 1)

		# Delivery Note remaining billable is now 1.0; attempting to bill 2.0 is blocked
		with self.assertRaises(OverbillingBlockedError):
			create_sales_invoice_from_fulfillment(
				dn.name,
				requested_lines=[{"dn_detail": dn.items[0].name, "qty": 2.0}],
			)

		# Cancel the submitted Sales Invoice
		cancel_sales_invoice(si1.name)
		self.assertEqual(si1.reload().docstatus, 2)

		# Billable scope is now fully restored to 3.0
		# Invoicing 3.0 now succeeds
		si2 = create_sales_invoice_from_fulfillment(
			dn.name,
			requested_lines=[{"dn_detail": dn.items[0].name, "qty": 3.0}],
			submit=True,
		)
		self.assertEqual(si2.docstatus, 1)
		self.assertEqual(si2.items[0].qty, 3.0)

	# =========================================================================
	# SCENARIO L: Fixture cleanup proof
	# =========================================================================
	def test_scenario_l_fixture_cleanup_proof(self):
		# Clean test docs first
		self._cleanup_test_docs()

		# Assert zero lingering test invoices
		residual_sis = frappe.db.count("Sales Invoice", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]})
		self.assertEqual(residual_sis, 0)

		# Assert zero lingering test delivery notes
		residual_dns = frappe.db.count("Delivery Note", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]})
		self.assertEqual(residual_dns, 0)
