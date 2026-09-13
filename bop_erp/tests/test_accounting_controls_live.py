# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from bop_erp.accounts import (
	CompanyMismatchError,
	CreditLimitExceededError,
	check_payment_entry_invariants,
	check_purchase_invoice_invariants,
	check_sales_invoice_invariants,
	get_purchasing_financial_traceability,
	get_sales_financial_traceability,
	reconcile_external_taxes,
	validate_company_accounting_isolation,
	validate_customer_credit_control,
)


class TestAccountingControlsLive(FrappeTestCase):
	"""
	Phase 1T Live Integration Test Suite:
	Accounting, Tax & Financial Controls Foundation.
	Executes live against the isolated local MariaDB / ERPNext test environment.
	Uses ownership-aware TEST-1T-* fixtures and restores clean baseline.
	"""

	FIXTURE_PREFIX = "TEST-1T-"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.company_a = "Industrial DP"
		cls.abbr_a = "IDP"
		cls.currency_a = "COP"

		cls.company_b = "Bamal Fastener Corp"
		cls.abbr_b = "BFC"
		cls.currency_b = "USD"

		# Ensure default bank account points to valid bank account
		frappe.db.set_value("Company", cls.company_a, "default_bank_account", "Banco Principal - IDP")
		frappe.db.commit()

		# Defensively clean any interrupted fixtures first
		cls._cleanup_module_fixtures()

		# Ensure default test accounts and parties
		cls._ensure_base_fixtures()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_module_fixtures()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		# Snapshot company frozen date for baseline safety
		self._orig_frozen_date = frappe.db.get_value("Company", self.company_a, "accounts_frozen_till_date")

	def tearDown(self):
		# Restore original frozen date
		frappe.db.set_value("Company", self.company_a, "accounts_frozen_till_date", self._orig_frozen_date)
		frappe.db.commit()
		self._cleanup_vouchers()
		super().tearDown()

	@classmethod
	def _ensure_base_fixtures(cls):
		"""Sets up required TEST-1T-* items, customers, suppliers, and templates."""
		# 1. Customer A (Industrial DP)
		cls.customer_a = f"{cls.FIXTURE_PREFIX}Cust-IDP"
		if not frappe.db.exists("Customer", cls.customer_a):
			cust = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": cls.customer_a,
				"customer_group": "Commercial",
				"customer_type": "Company",
			})
			cust.insert(ignore_permissions=True)

		# 2. Customer B (Bamal Fastener Corp)
		cls.customer_b = f"{cls.FIXTURE_PREFIX}Cust-BFC"
		if not frappe.db.exists("Customer", cls.customer_b):
			cust = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": cls.customer_b,
				"customer_group": "Commercial",
				"customer_type": "Company",
			})
			cust.insert(ignore_permissions=True)

		# 3. Credit Control Dedicated Customer (Industrial DP)
		cls.credit_customer = f"{cls.FIXTURE_PREFIX}CreditCust-IDP"
		if not frappe.db.exists("Customer", cls.credit_customer):
			cust = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": cls.credit_customer,
				"customer_group": "Commercial",
				"customer_type": "Company",
			})
			cust.insert(ignore_permissions=True)

		# 4. Foreign Currency Dedicated Customer (Industrial DP)
		cls.foreign_customer = f"{cls.FIXTURE_PREFIX}ForeignCust-IDP"
		cls.receivable_acc_usd = "TEST-1T-Clientes USD - IDP"
		if not frappe.db.exists("Account", cls.receivable_acc_usd):
			acc = frappe.get_doc({
				"doctype": "Account",
				"account_name": "TEST-1T-Clientes USD",
				"company": cls.company_a,
				"parent_account": "1305 - Clientes - IDP",
				"account_type": "Receivable",
				"root_type": "Asset",
				"account_currency": "USD",
				"is_group": 0,
			})
			acc.insert(ignore_permissions=True)

		if not frappe.db.exists("Customer", cls.foreign_customer):
			cust = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": cls.foreign_customer,
				"customer_group": "Commercial",
				"customer_type": "Company",
				"default_currency": "USD",
				"accounts": [
					{
						"company": cls.company_a,
						"account": cls.receivable_acc_usd,
					}
				],
			})
			cust.insert(ignore_permissions=True)

		# 5. Supplier A (Industrial DP)
		cls.supplier_a = f"{cls.FIXTURE_PREFIX}Supp-IDP"
		if not frappe.db.exists("Supplier", cls.supplier_a):
			supp = frappe.get_doc({
				"doctype": "Supplier",
				"supplier_name": cls.supplier_a,
				"supplier_group": "Local",
				"supplier_type": "Company",
			})
			supp.insert(ignore_permissions=True)

		# 6. Item
		cls.item_code = f"{cls.FIXTURE_PREFIX}SKU-01"
		if not frappe.db.exists("Item", cls.item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": cls.item_code,
				"item_name": cls.item_code,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 1,
			})
			item.insert(ignore_permissions=True)

		# 7. Payment Terms Template Net 30
		cls.terms_template = f"{cls.FIXTURE_PREFIX}Net-30"
		if not frappe.db.exists("Payment Terms Template", cls.terms_template):
			term_name = f"{cls.FIXTURE_PREFIX}Term-30"
			if not frappe.db.exists("Payment Term", term_name):
				term = frappe.get_doc({
					"doctype": "Payment Term",
					"payment_term_name": term_name,
					"due_date_based_on": "Day(s) after invoice date",
					"credit_days": 30,
				})
				term.insert(ignore_permissions=True)

			ptt = frappe.get_doc({
				"doctype": "Payment Terms Template",
				"template_name": cls.terms_template,
				"terms": [
					{
						"payment_term": term_name,
						"invoice_portion": 100.0,
						"credit_days": 30,
					}
				],
			})
			ptt.insert(ignore_permissions=True)

		# 8. Resolve native accounts
		cls.income_acc_a = "Ventas de mercancías - IDP"
		cls.receivable_acc_a = frappe.db.get_value("Company", cls.company_a, "default_receivable_account") or "1390 - Deudas de difícil cobro - IDP"
		cls.payable_acc_a = frappe.db.get_value("Company", cls.company_a, "default_payable_account") or "2375 - Cuotas por devolver - IDP"
		cls.tax_acc_a = "VAT - IDP"
		cls.cost_center_a = "Main - IDP"

		cls.income_acc_b = "Sales - BFC"
		cls.receivable_acc_b = "Debtors - BFC"
		cls.cost_center_b = "Main - BFC"
		cls.tax_acc_b = "VAT - BFC"

		frappe.db.commit()

	@classmethod
	def _cleanup_vouchers(cls):
		"""Cleans all test vouchers created during live tests."""
		# 1. Cancel and delete Payment Entries
		pe_names = frappe.db.sql(
			"""
			SELECT name FROM `tabPayment Entry`
			WHERE party LIKE %s OR remarks LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%", f"%{cls.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		for pe in pe_names:
			try:
				doc = frappe.get_doc("Payment Entry", pe)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.db.delete("Payment Ledger Entry", {"voucher_no": pe})
				frappe.delete_doc("Payment Entry", pe, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": pe})
			except Exception:
				pass

		# 2. Cancel and delete Sales Invoices
		si_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabSales Invoice Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabSales Invoice`
			WHERE customer LIKE %s OR remarks LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%", f"{cls.FIXTURE_PREFIX}%", f"%{cls.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		cns = [n for n in si_names if frappe.db.get_value("Sales Invoice", n, "is_return")]
		regular_sis = [n for n in si_names if n not in cns]
		for si in cns + regular_sis:
			try:
				doc = frappe.get_doc("Sales Invoice", si)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.db.delete("Payment Ledger Entry", {"against_voucher_no": si})
				frappe.delete_doc("Sales Invoice", si, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": si})
			except Exception:
				pass

		# 3. Cancel and delete Purchase Invoices
		pi_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabPurchase Invoice Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabPurchase Invoice`
			WHERE supplier LIKE %s OR remarks LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%", f"{cls.FIXTURE_PREFIX}%", f"%{cls.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		for pi in pi_names:
			try:
				doc = frappe.get_doc("Purchase Invoice", pi)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.db.delete("Payment Ledger Entry", {"against_voucher_no": pi})
				frappe.delete_doc("Purchase Invoice", pi, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": pi})
			except Exception:
				pass

		frappe.db.commit()

	@classmethod
	def _cleanup_module_fixtures(cls):
		"""Defensively cleans up all vouchers and master data belonging to this module."""
		cls._cleanup_vouchers()

		# Clean up created master data and templates (Customer, Supplier, Item, Terms, Account)
		for dt in ["Customer", "Supplier", "Item", "Payment Terms Template", "Payment Term", "Account"]:
			names = frappe.get_all(dt, filters={"name": ["like", f"{cls.FIXTURE_PREFIX}%"]}, pluck="name")
			for n in names:
				try:
					frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
				except Exception:
					pass

		frappe.db.commit()

	# =========================================================================
	# TEST 01: Company Account Isolation Live
	# =========================================================================
	def test_01_company_account_isolation_live(self):
		"""Proves Company A cannot use Company B receivable account."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_b,  # Belongs to Company B
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
		})
		with self.assertRaises(CompanyMismatchError):
			validate_company_accounting_isolation(si)
		with self.assertRaises(frappe.ValidationError):
			si.insert()

	# =========================================================================
	# TEST 02: Cost Center Company Isolation Live
	# =========================================================================
	def test_02_cost_center_isolation_live(self):
		"""Proves Company A cannot use Company B Cost Center."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_b,  # Belongs to Company B
				}
			],
		})
		with self.assertRaises(CompanyMismatchError):
			validate_company_accounting_isolation(si)
		with self.assertRaises(frappe.ValidationError):
			si.insert()

	# =========================================================================
	# TEST 03: Tax Account Isolation Live
	# =========================================================================
	def test_03_tax_account_isolation_live(self):
		"""Proves Company A cannot use Company B tax account."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
			"taxes": [
				{
					"charge_type": "On Net Total",
					"account_head": self.tax_acc_b,  # Belongs to Company B
					"description": "Tax Row",
					"rate": 19.0,
				}
			],
		})
		with self.assertRaises(CompanyMismatchError):
			validate_company_accounting_isolation(si)
		with self.assertRaises(frappe.ValidationError):
			si.insert()

	# =========================================================================
	# TEST 04: Sales Tax GL Posting Live
	# =========================================================================
	def test_04_sales_tax_gl_posting_live(self):
		"""Creates and submits Sales Invoice with native tax row; verifies GL Entry balance."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 2.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
			"taxes": [
				{
					"charge_type": "On Net Total",
					"account_head": self.tax_acc_a,
					"description": "VAT 19%",
					"rate": 19.0,
				}
			],
		})
		si.insert()
		si.submit()

		self.assertEqual(flt(si.grand_total), 2380.0)
		self.assertEqual(flt(si.outstanding_amount), 2380.0)

		gl_entries = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Sales Invoice", "voucher_no": si.name, "is_cancelled": 0},
			fields=["account", "debit", "credit", "company"],
		)
		self.assertTrue(len(gl_entries) >= 3)
		total_debit = sum(flt(g.debit) for g in gl_entries)
		total_credit = sum(flt(g.credit) for g in gl_entries)
		self.assertAlmostEqual(total_debit, total_credit, places=2)
		self.assertAlmostEqual(total_debit, 2380.0, places=2)

		for g in gl_entries:
			self.assertEqual(g.company, self.company_a)

		inv_res = check_sales_invoice_invariants(si)
		self.assertTrue(inv_res["valid"])

	# =========================================================================
	# TEST 05: Credit Note Tax Reversal Live
	# =========================================================================
	def test_05_credit_note_tax_reversal_live(self):
		"""Creates Credit Note against submitted Sales Invoice; verifies GL reversal."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 2.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
			"taxes": [
				{
					"charge_type": "On Net Total",
					"account_head": self.tax_acc_a,
					"description": "VAT 19%",
					"rate": 19.0,
				}
			],
		})
		si.insert()
		si.submit()

		cn = frappe.get_doc({
			"doctype": "Sales Invoice",
			"is_return": 1,
			"return_against": si.name,
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": -1.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
			"taxes": [
				{
					"charge_type": "On Net Total",
					"account_head": self.tax_acc_a,
					"description": "VAT 19%",
					"rate": 19.0,
				}
			],
		})
		cn.insert()
		cn.submit()

		self.assertEqual(flt(cn.grand_total), -1190.0)

		cn_gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Sales Invoice", "voucher_no": cn.name, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		self.assertTrue(len(cn_gl) >= 3)
		tot_deb = sum(flt(g.debit) for g in cn_gl)
		tot_cred = sum(flt(g.credit) for g in cn_gl)
		self.assertAlmostEqual(tot_deb, tot_cred, places=2)

	# =========================================================================
	# TEST 06: Purchase Invoice Tax Posting Live
	# =========================================================================
	def test_06_purchase_invoice_tax_gl_posting_live(self):
		"""Creates and submits Purchase Invoice with tax row; verifies AP liability GL."""
		exp_acc = frappe.db.get_value("Company", self.company_a, "default_expense_account") or "5105 - Gastos de personal - IDP"

		pi = frappe.get_doc({
			"doctype": "Purchase Invoice",
			"company": self.company_a,
			"supplier": self.supplier_a,
			"credit_to": self.payable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 2.0,
					"rate": 500.0,
					"expense_account": exp_acc,
					"cost_center": self.cost_center_a,
				}
			],
			"taxes": [
				{
					"charge_type": "On Net Total",
					"account_head": self.tax_acc_a,
					"description": "VAT 19%",
					"rate": 19.0,
				}
			],
		})
		pi.insert()
		pi.submit()

		self.assertEqual(flt(pi.grand_total), 1190.0)
		self.assertEqual(flt(pi.outstanding_amount), 1190.0)

		inv_res = check_purchase_invoice_invariants(pi)
		self.assertTrue(inv_res["valid"])

	# =========================================================================
	# TEST 07: Payment Terms Template & Payment Schedule Live
	# =========================================================================
	def test_07_payment_terms_schedule_live(self):
		"""Verifies Payment Terms Template populates Payment Schedule with 30 day due date."""
		today = nowdate()
		expected_due = add_days(today, 30)

		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": today,
			"currency": self.currency_a,
			"payment_terms_template": self.terms_template,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
		})
		si.insert()

		self.assertTrue(len(si.payment_schedule) > 0)
		sched = si.payment_schedule[0]
		self.assertEqual(str(sched.due_date), str(expected_due))
		self.assertEqual(flt(sched.payment_amount), 1000.0)

	# =========================================================================
	# TEST 08: Customer Credit Limit Enforcement Live
	# =========================================================================
	def test_08_customer_credit_control_live(self):
		"""Verifies customer credit limit enforcement in live environment using dedicated credit customer."""
		cust_doc = frappe.get_doc("Customer", self.credit_customer)
		cust_doc.credit_limits = []
		cust_doc.append("credit_limits", {
			"company": self.company_a,
			"credit_limit": 5000.0,
		})
		cust_doc.save(ignore_permissions=True)
		frappe.db.commit()

		# 1. Within limit -> approved
		res_ok = validate_customer_credit_control(self.credit_customer, self.company_a, extra_amount=2000.0)
		self.assertTrue(res_ok["allowed"])
		self.assertEqual(res_ok["status"], "APPROVED")

		# 2. Exceeding limit -> blocked
		with self.assertRaises(CreditLimitExceededError):
			validate_customer_credit_control(self.credit_customer, self.company_a, extra_amount=8000.0, allow_review=False)

		# 3. Exceeding limit in review mode -> REVIEW_REQUIRED
		res_rev = validate_customer_credit_control(self.credit_customer, self.company_a, extra_amount=8000.0, allow_review=True)
		self.assertFalse(res_rev["allowed"])
		self.assertEqual(res_rev["status"], "REVIEW_REQUIRED")

	# =========================================================================
	# TEST 09: Multi-Currency Sales Invoice Live
	# =========================================================================
	def test_09_multi_currency_sales_invoice_live(self):
		"""Creates foreign-currency Sales Invoice and verifies base amount conversion."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.foreign_customer,
			"debit_to": self.receivable_acc_usd,
			"posting_date": nowdate(),
			"currency": "USD",
			"conversion_rate": 4000.0,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 10.0,  # 10 USD
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
		})
		si.insert()
		si.submit()

		self.assertEqual(flt(si.grand_total), 10.0)
		self.assertEqual(flt(si.base_grand_total), 40000.0)

		gl_entries = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Sales Invoice", "voucher_no": si.name, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		total_debit = sum(flt(g.debit) for g in gl_entries)
		self.assertAlmostEqual(total_debit, 40000.0, places=2)

	# =========================================================================
	# TEST 10: Frozen Posting Date Protection Live
	# =========================================================================
	def test_10_frozen_date_control_live(self):
		"""Verifies that posting on or before accounts_frozen_till_date is blocked natively."""
		frappe.db.set_value("Company", self.company_a, "accounts_frozen_till_date", "2026-09-10")
		frappe.db.commit()

		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": "2026-09-05",  # Before frozen date
			"set_posting_time": 1,
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
		})
		si.insert()

		with self.assertRaises(frappe.ValidationError):
			si.submit()

	# =========================================================================
	# TEST 11: Partial Payment & Cancellation Restoration Live
	# =========================================================================
	def test_11_partial_payment_and_cancellation_restoration_live(self):
		"""Creates Sales Invoice, submits partial Payment Entry, verifies outstanding reduction & restoration on cancel."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
		})
		si.insert()
		si.submit()
		self.assertEqual(flt(si.outstanding_amount), 1000.0)

		bank_acc = frappe.db.get_value("Account", {"company": self.company_a, "account_type": "Bank", "is_group": 0}, "name")
		if not bank_acc:
			bank_acc = "Banco Principal - IDP"

		pe = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": self.customer_a,
			"company": self.company_a,
			"posting_date": nowdate(),
			"paid_from": self.receivable_acc_a,
			"paid_to": bank_acc,
			"paid_amount": 400.0,
			"received_amount": 400.0,
			"reference_no": "REF-001",
			"reference_date": nowdate(),
			"references": [
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si.name,
					"total_amount": 1000.0,
					"outstanding_amount": 1000.0,
					"allocated_amount": 400.0,
				}
			],
		})
		pe.insert()
		pe.submit()

		si.reload()
		self.assertAlmostEqual(flt(si.outstanding_amount), 600.0, places=2)

		pe.cancel()
		si.reload()
		self.assertAlmostEqual(flt(si.outstanding_amount), 1000.0, places=2)

	# =========================================================================
	# TEST 12: Financial Traceability View Live
	# =========================================================================
	def test_12_financial_traceability_live(self):
		"""Verifies get_sales_financial_traceability returns structured chain without shadow tables."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
		})
		si.insert()
		si.submit()

		trace = get_sales_financial_traceability(sales_invoice=si.name)
		self.assertEqual(trace["flow"], "SALE")
		self.assertEqual(trace["company"], self.company_a)
		self.assertEqual(len(trace["sales_invoices"]), 1)
		self.assertEqual(trace["sales_invoices"][0]["name"], si.name)
		self.assertEqual(trace["total_billed"], 1000.0)

	# =========================================================================
	# TEST 13: Financial Invariant Diagnostic Service Live
	# =========================================================================
	def test_13_financial_invariants_diagnostic_live(self):
		"""Verifies check_sales_invoice_invariants validates live submitted document."""
		si = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 1000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
		})
		si.insert()
		si.submit()

		res = check_sales_invoice_invariants(si.name)
		self.assertTrue(res["valid"])
		self.assertEqual(len(res["violations"]), 0)

	# =========================================================================
	# TEST 14: Fixture Cleanup Proof Live
	# =========================================================================
	def test_14_fixture_cleanup_proof_live(self):
		"""Cleans all test vouchers and confirms zero lingering TEST-1T-* records."""
		self._cleanup_module_fixtures()

		residual_sis = frappe.db.sql(
			"""
			SELECT name FROM `tabSales Invoice`
			WHERE customer LIKE %s OR name LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%", f"{self.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		self.assertEqual(len(residual_sis), 0)

		residual_pes = frappe.db.sql(
			"""
			SELECT name FROM `tabPayment Entry`
			WHERE party LIKE %s OR name LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%", f"{self.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		self.assertEqual(len(residual_pes), 0)
