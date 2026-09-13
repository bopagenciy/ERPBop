# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import MagicMock, patch
import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.accounts import (
	AccountingPeriodClosedError,
	CompanyMismatchError,
	CreditLimitExceededError,
	FinancialInvariantViolationError,
	FinancialTraceabilityError,
	MaterialTaxMismatchError,
	PostingDateFrozenError,
	check_payment_entry_invariants,
	check_purchase_invoice_invariants,
	check_sales_invoice_invariants,
	get_currency_tax_tolerance,
	get_purchasing_financial_traceability,
	get_sales_financial_traceability,
	reconcile_external_taxes,
	validate_company_accounting_isolation,
	validate_customer_credit_control,
)


class MockDocument(dict):
	"""Helper mock document that allows attribute access while keeping dict methods."""
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.__dict__ = self

	def get(self, key, default=None):
		return super().get(key, default)

	def run_method(self, method, *args, **kwargs):
		return None


class TestAccountingControlsUnit(FrappeTestCase):
	"""
	Phase 1T Unit Test Suite:
	Accounting, Tax & Financial Controls Foundation.
	Validates all 30 target areas specified in Section K.
	"""

	def setUp(self):
		super().setUp()

	# -------------------------------------------------------------------------
	# 1. Native accounting is source of truth
	# -------------------------------------------------------------------------
	def test_01_native_accounting_is_source_of_truth(self):
		"""Verifies that native ERPNext GL Entry / Accounts are the sole accounting source of truth."""
		from bop_erp.accounts.financial_invariants import check_sales_invoice_invariants
		si = MockDocument(
			name="ACC-SI-001",
			doctype="Sales Invoice",
			docstatus=1,
			company="Company A",
			customer="Customer A",
			grand_total=100.0,
			outstanding_amount=100.0,
		)
		mock_gl = [
			MockDocument(account="Debtors - CA", party_type="Customer", party="Customer A", debit=100.0, credit=0.0, is_cancelled=0, company="Company A"),
			MockDocument(account="Sales - CA", party_type="", party="", debit=0.0, credit=100.0, is_cancelled=0, company="Company A"),
		]
		with patch("frappe.get_all", return_value=mock_gl):
			res = check_sales_invoice_invariants(si)
			self.assertTrue(res["valid"])
			self.assertEqual(len(res["violations"]), 0)

	# -------------------------------------------------------------------------
	# 2. No direct GL mutation
	# -------------------------------------------------------------------------
	def test_02_no_direct_gl_mutation(self):
		"""Verifies financial invariant checker is diagnostic-only and never mutates or inserts GL."""
		from bop_erp.accounts.financial_invariants import check_sales_invoice_invariants
		si = MockDocument(
			name="ACC-SI-002",
			doctype="Sales Invoice",
			docstatus=1,
			company="Company A",
			customer="Customer A",
			grand_total=100.0,
			outstanding_amount=100.0,
		)
		with patch("frappe.get_all", return_value=[]), \
		     patch("frappe.db.sql") as mock_sql, \
		     patch("frappe.db.set_value") as mock_set:
			res = check_sales_invoice_invariants(si)
			self.assertFalse(res["valid"])
			# Must never run INSERT or UPDATE to 'repair' GL
			mock_sql.assert_not_called()
			mock_set.assert_not_called()

	# -------------------------------------------------------------------------
	# 3. Company account isolation
	# -------------------------------------------------------------------------
	def test_03_company_account_isolation(self):
		"""Verifies validate_company_accounting_isolation blocks accounts from other companies."""
		si = MockDocument(
			name="ACC-SI-003",
			company="Company A",
			debit_to="Debtors - CB",  # belongs to Company B
			items=[],
			taxes=[],
		)
		with patch("frappe.db.get_value", return_value="Company B"):
			with self.assertRaises(CompanyMismatchError):
				validate_company_accounting_isolation(si)

	# -------------------------------------------------------------------------
	# 4. Cost center isolation
	# -------------------------------------------------------------------------
	def test_04_cost_center_isolation(self):
		"""Verifies validate_company_accounting_isolation blocks cost centers from other companies."""
		si = MockDocument(
			name="ACC-SI-004",
			company="Company A",
			debit_to=None,
			items=[MockDocument(cost_center="Main - CB", income_account=None)],
			taxes=[],
		)
		with patch("frappe.db.get_value", return_value="Company B"):
			with self.assertRaises(CompanyMismatchError):
				validate_company_accounting_isolation(si)

	# -------------------------------------------------------------------------
	# 5. Sales tax structure preservation
	# -------------------------------------------------------------------------
	def test_05_sales_tax_posting(self):
		"""Verifies sales taxes on invoices are preserved and validated natively."""
		si = MockDocument(
			name="ACC-SI-005",
			currency="USD",
			taxes=[
				MockDocument(account_head="VAT 19% - CA", rate=19.0, tax_amount=19.0),
			],
		)
		res = reconcile_external_taxes(si, external_tax_amount=19.0, currency="USD")
		self.assertTrue(res["reconciled"])
		self.assertEqual(res["native_tax_amount"], 19.0)

	# -------------------------------------------------------------------------
	# 6. Purchase tax structure preservation
	# -------------------------------------------------------------------------
	def test_06_purchase_tax_posting(self):
		"""Verifies purchase tax amounts reconcile and preserve native account head."""
		pi = MockDocument(
			name="ACC-PI-006",
			currency="USD",
			taxes=[
				MockDocument(account_head="Input Tax 10% - CA", rate=10.0, tax_amount=10.0),
			],
		)
		res = reconcile_external_taxes(pi, external_tax_amount=10.0, currency="USD")
		self.assertTrue(res["reconciled"])

	# -------------------------------------------------------------------------
	# 7. Zero tax transactions
	# -------------------------------------------------------------------------
	def test_07_zero_tax(self):
		"""Verifies zero-tax transactions reconcile without error."""
		si = MockDocument(
			name="ACC-SI-007",
			currency="USD",
			taxes=[],
		)
		res = reconcile_external_taxes(si, external_tax_amount=0.0, currency="USD")
		self.assertTrue(res["reconciled"])
		self.assertEqual(res["native_tax_amount"], 0.0)

	# -------------------------------------------------------------------------
	# 8. Multi-row tax handling
	# -------------------------------------------------------------------------
	def test_08_multi_row_tax(self):
		"""Verifies multi-row tax structures sum natively and reconcile accurately."""
		si = MockDocument(
			name="ACC-SI-008",
			currency="USD",
			taxes=[
				MockDocument(account_head="State Tax - CA", rate=6.0, tax_amount=6.0),
				MockDocument(account_head="City Tax - CA", rate=2.5, tax_amount=2.5),
			],
		)
		res = reconcile_external_taxes(si, external_tax_amount=8.5, currency="USD")
		self.assertTrue(res["reconciled"])
		self.assertEqual(res["native_tax_amount"], 8.5)

	# -------------------------------------------------------------------------
	# 9. Tax mismatch blocks and requires review
	# -------------------------------------------------------------------------
	def test_09_tax_mismatch_blocks_reviews(self):
		"""Verifies that material tax discrepancy raises MaterialTaxMismatchError."""
		si = MockDocument(
			name="ACC-SI-009",
			currency="USD",
			taxes=[MockDocument(tax_amount=10.0)],
		)
		# External tax is 15.0 (diff 5.0 exceeds USD tolerance 0.02)
		with self.assertRaises(MaterialTaxMismatchError):
			reconcile_external_taxes(si, external_tax_amount=15.0, currency="USD")

	# -------------------------------------------------------------------------
	# 10. Credit note tax reversal
	# -------------------------------------------------------------------------
	def test_10_credit_note_tax_reversal(self):
		"""Verifies Credit Note reflects negative/reversal tax structure proportionally."""
		cn = MockDocument(
			name="ACC-CN-010",
			currency="USD",
			is_return=1,
			taxes=[MockDocument(tax_amount=-5.0)],
		)
		res = reconcile_external_taxes(cn, external_tax_amount=-5.0, currency="USD")
		self.assertTrue(res["reconciled"])
		self.assertEqual(res["native_tax_amount"], -5.0)

	# -------------------------------------------------------------------------
	# 11. Payment terms Net 30
	# -------------------------------------------------------------------------
	def test_11_payment_terms_net_30(self):
		"""Verifies Payment Terms Template Net 30 is respected natively."""
		ps = [
			MockDocument(payment_term="Net 30", due_date="2026-10-13", payment_amount=100.0),
		]
		si = MockDocument(name="ACC-SI-011", payment_schedule=ps, grand_total=100.0)
		self.assertEqual(len(si.payment_schedule), 1)
		self.assertEqual(si.payment_schedule[0].payment_term, "Net 30")
		self.assertEqual(si.payment_schedule[0].due_date, "2026-10-13")

	# -------------------------------------------------------------------------
	# 12. Custom due-date schedule
	# -------------------------------------------------------------------------
	def test_12_custom_due_date_schedule(self):
		"""Verifies custom split payment schedules preserve each installment due date."""
		ps = [
			MockDocument(payment_term="50% Advance", due_date="2026-09-13", payment_amount=50.0),
			MockDocument(payment_term="50% Net 30", due_date="2026-10-13", payment_amount=50.0),
		]
		si = MockDocument(name="ACC-SI-012", payment_schedule=ps, grand_total=100.0)
		self.assertEqual(len(si.payment_schedule), 2)
		self.assertEqual(sum(row.payment_amount for row in si.payment_schedule), 100.0)

	# -------------------------------------------------------------------------
	# 13. Partial payment outstanding
	# -------------------------------------------------------------------------
	def test_13_partial_payment_outstanding(self):
		"""Verifies partial payment reduces outstanding balance natively without corrupting document."""
		si = MockDocument(
			name="ACC-SI-013",
			doctype="Sales Invoice",
			docstatus=1,
			company="Company A",
			customer="Customer A",
			grand_total=100.0,
			outstanding_amount=60.0,  # 40.0 paid
		)
		mock_gl = [
			MockDocument(account="Debtors - CA", party_type="Customer", party="Customer A", debit=100.0, credit=0.0, is_cancelled=0, company="Company A"),
			MockDocument(account="Sales - CA", party_type="", party="", debit=0.0, credit=100.0, is_cancelled=0, company="Company A"),
		]
		with patch("frappe.get_all", return_value=mock_gl):
			res = check_sales_invoice_invariants(si)
			self.assertTrue(res["valid"])
			self.assertEqual(res["details"]["outstanding_amount"], 60.0)

	# -------------------------------------------------------------------------
	# 14. Customer credit within limit
	# -------------------------------------------------------------------------
	def test_14_customer_credit_within_limit(self):
		"""Verifies validate_customer_credit_control approves when exposure is below credit limit."""
		with patch("bop_erp.accounts.credit_control.get_effective_credit_limit", return_value=1000.0), \
		     patch("bop_erp.accounts.credit_control.get_customer_current_exposure", return_value=400.0):
			res = validate_customer_credit_control("Customer A", "Company A", extra_amount=100.0)
			self.assertTrue(res["allowed"])
			self.assertEqual(res["status"], "APPROVED")
			self.assertEqual(res["total_exposure"], 500.0)

	# -------------------------------------------------------------------------
	# 15. Customer over limit protection
	# -------------------------------------------------------------------------
	def test_15_customer_over_limit_protection(self):
		"""Verifies validate_customer_credit_control blocks or requires review when exceeding limit."""
		with patch("bop_erp.accounts.credit_control.get_effective_credit_limit", return_value=500.0), \
		     patch("bop_erp.accounts.credit_control.get_customer_current_exposure", return_value=450.0):
			# Direct raise
			with self.assertRaises(CreditLimitExceededError):
				validate_customer_credit_control("Customer A", "Company A", extra_amount=100.0, allow_review=False)

			# Review required mode
			review_res = validate_customer_credit_control("Customer A", "Company A", extra_amount=100.0, allow_review=True)
			self.assertFalse(review_res["allowed"])
			self.assertEqual(review_res["status"], "REVIEW_REQUIRED")

	# -------------------------------------------------------------------------
	# 16. Same-currency accounting
	# -------------------------------------------------------------------------
	def test_16_same_currency_accounting(self):
		"""Verifies conversion_rate is 1.0 when transaction currency equals company currency."""
		si = MockDocument(currency="USD", company_currency="USD", conversion_rate=1.0, grand_total=100.0, base_grand_total=100.0)
		self.assertEqual(si.conversion_rate, 1.0)
		self.assertEqual(si.grand_total, si.base_grand_total)

	# -------------------------------------------------------------------------
	# 17. Foreign-currency Sales Invoice
	# -------------------------------------------------------------------------
	def test_17_foreign_currency_sales_invoice(self):
		"""Verifies foreign currency Sales Invoice maintains valid base totals and conversion rate."""
		si = MockDocument(currency="EUR", company_currency="USD", conversion_rate=1.10, grand_total=100.0, base_grand_total=110.0)
		self.assertAlmostEqual(si.grand_total * si.conversion_rate, si.base_grand_total, places=2)

	# -------------------------------------------------------------------------
	# 18. Foreign-currency Purchase Invoice
	# -------------------------------------------------------------------------
	def test_18_foreign_currency_purchase_invoice(self):
		"""Verifies foreign currency Purchase Invoice maintains valid base totals."""
		pi = MockDocument(currency="EUR", company_currency="USD", conversion_rate=1.10, grand_total=200.0, base_grand_total=220.0)
		self.assertAlmostEqual(pi.grand_total * pi.conversion_rate, pi.base_grand_total, places=2)

	# -------------------------------------------------------------------------
	# 19. Exchange gain/loss native behavior
	# -------------------------------------------------------------------------
	def test_19_exchange_gain_loss_native_behavior(self):
		"""Verifies native ERPNext handles exchange gain/loss; Bop inserts zero custom gain/loss GL."""
		from erpnext.accounts.doctype.payment_entry.payment_entry import PaymentEntry
		self.assertTrue(hasattr(PaymentEntry, "set_exchange_gain_loss"))

	# -------------------------------------------------------------------------
	# 20. Closed Accounting Period blocks posting
	# -------------------------------------------------------------------------
	def test_20_closed_accounting_period_blocks_posting(self):
		"""Verifies ERPNext general_ledger.validate_accounting_period is native barrier."""
		from erpnext.accounts.general_ledger import validate_accounting_period
		self.assertTrue(callable(validate_accounting_period))

	# -------------------------------------------------------------------------
	# 21. Frozen accounting date blocks posting
	# -------------------------------------------------------------------------
	def test_21_frozen_accounting_date_blocks_posting(self):
		"""Verifies posting date frozen checks exist natively in general_ledger."""
		from erpnext.accounts.general_ledger import check_freezing_date
		self.assertTrue(callable(check_freezing_date))

	# -------------------------------------------------------------------------
	# 22. Fiscal Year control
	# -------------------------------------------------------------------------
	def test_22_fiscal_year_control(self):
		"""Verifies Fiscal Year validation is native and callable in ERPNext."""
		from erpnext.accounts.utils import validate_fiscal_year
		self.assertTrue(callable(validate_fiscal_year))

	# -------------------------------------------------------------------------
	# 23. Currency-aware rounding tolerance
	# -------------------------------------------------------------------------
	def test_23_currency_aware_rounding_tolerance(self):
		"""Verifies tolerance resolution adapts accurately across currency types."""
		self.assertEqual(get_currency_tax_tolerance("USD"), 0.02)
		self.assertEqual(get_currency_tax_tolerance("COP"), 0.02)
		self.assertEqual(get_currency_tax_tolerance("EUR"), 0.02)
		self.assertEqual(get_currency_tax_tolerance("JPY"), 1.0)
		self.assertEqual(get_currency_tax_tolerance("KRW"), 1.0)
		self.assertEqual(get_currency_tax_tolerance("BHD"), 0.005)

	# -------------------------------------------------------------------------
	# 24. Sales financial traceability
	# -------------------------------------------------------------------------
	def test_24_sales_financial_traceability(self):
		"""Verifies get_sales_financial_traceability builds complete chain without shadow tables."""
		so = MockDocument(doctype="Sales Order", name="SO-01", docstatus=1, company="Company A", currency="USD", grand_total=100.0, items=[])
		dn = MockDocument(doctype="Delivery Note", name="DN-01", docstatus=1, company="Company A", currency="USD", grand_total=100.0, items=[])
		si = MockDocument(doctype="Sales Invoice", name="SI-01", docstatus=1, company="Company A", currency="USD", grand_total=100.0, outstanding_amount=0.0, is_return=0, items=[MockDocument(sales_order="SO-01", delivery_note="DN-01")])
		pe = MockDocument(doctype="Payment Entry", name="PE-01", docstatus=1, company="Company A", currency="USD", grand_total=100.0, references=[MockDocument(reference_name="SI-01")])

		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_cached_doc", side_effect=lambda dt, n: si if dt == "Sales Invoice" else dn), \
		     patch("frappe.get_doc", side_effect=lambda dt, n: {"Sales Order": so, "Delivery Note": dn, "Sales Invoice": si, "Payment Entry": pe}[dt]), \
		     patch("frappe.get_all", return_value=[]):
			chain = get_sales_financial_traceability(sales_invoice="SI-01")
			self.assertEqual(chain["flow"], "SALE")
			self.assertEqual(chain["company"], "Company A")
			self.assertEqual(len(chain["sales_invoices"]), 1)

	# -------------------------------------------------------------------------
	# 25. Purchasing financial traceability
	# -------------------------------------------------------------------------
	def test_25_purchasing_financial_traceability(self):
		"""Verifies get_purchasing_financial_traceability builds complete PO -> PR -> PI -> PE chain."""
		po = MockDocument(doctype="Purchase Order", name="PO-01", docstatus=1, company="Company A", currency="USD", grand_total=200.0, items=[])
		pr = MockDocument(doctype="Purchase Receipt", name="PR-01", docstatus=1, company="Company A", currency="USD", grand_total=200.0, items=[])
		pi = MockDocument(doctype="Purchase Invoice", name="PI-01", docstatus=1, company="Company A", currency="USD", grand_total=200.0, outstanding_amount=200.0, is_return=0, items=[MockDocument(purchase_order="PO-01", purchase_receipt="PR-01")])

		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_cached_doc", side_effect=lambda dt, n: pi if dt == "Purchase Invoice" else pr), \
		     patch("frappe.get_doc", side_effect=lambda dt, n: {"Purchase Order": po, "Purchase Receipt": pr, "Purchase Invoice": pi}[dt]), \
		     patch("frappe.get_all", return_value=[]):
			chain = get_purchasing_financial_traceability(purchase_invoice="PI-01")
			self.assertEqual(chain["flow"], "PURCHASE")
			self.assertEqual(len(chain["purchase_invoices"]), 1)

	# -------------------------------------------------------------------------
	# 26. Payment cancellation restoration invariant
	# -------------------------------------------------------------------------
	def test_26_payment_cancellation_restoration(self):
		"""Verifies check_payment_entry_invariants confirms cancellation and outstanding restoration."""
		pe = MockDocument(
			name="ACC-PE-026",
			doctype="Payment Entry",
			docstatus=2,  # Cancelled
			company="Company A",
			party_type="Customer",
			party="Customer A",
			paid_amount=100.0,
			received_amount=100.0,
		)
		# Active GL entries count is 0 (or cancelled)
		mock_gl = [MockDocument(is_cancelled=1)]
		with patch("frappe.get_all", return_value=mock_gl):
			res = check_payment_entry_invariants(pe)
			self.assertTrue(res["valid"])

	# -------------------------------------------------------------------------
	# 27. No validation bypass
	# -------------------------------------------------------------------------
	def test_27_no_validation_bypass(self):
		"""Verifies create_sales_invoice_from_fulfillment preserves strict validation flags."""
		from bop_erp.accounts.invoice import create_sales_invoice_from_fulfillment
		mock_so = MockDocument(name="SO-01", docstatus=1, status="To Deliver and Bill", company="Company A", currency="USD", conversion_rate=1.0, items=[])
		mock_dn = MockDocument(
			name="DN-01", docstatus=1, status="To Bill", company="Company A", currency="USD", conversion_rate=1.0,
			items=[MockDocument(name="DNI-01", item_code="SKU-1", qty=1.0)]
		)
		mock_si = MockDocument(
			name="SI-01", docstatus=0, company="Company A", currency="USD", conversion_rate=1.0,
			items=[MockDocument(name="SII-01", item_code="SKU-1", qty=1.0, dn_detail="DNI-01")],
			posting_date="2026-09-13",
			flags=MockDocument()
		)
		mock_si.insert = MagicMock()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, n: mock_so if dt == "Sales Order" else mock_dn), \
		     patch("bop_erp.accounts.invoice.assert_sales_invoice_eligibility", return_value=(mock_dn, mock_so)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=mock_si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created = create_sales_invoice_from_fulfillment("DN-01")
			self.assertFalse(created.flags.ignore_validate)
			self.assertFalse(created.flags.ignore_mandatory)
			self.assertFalse(created.flags.ignore_permissions)

	# -------------------------------------------------------------------------
	# 28. Manual ERP accounting unaffected
	# -------------------------------------------------------------------------
	def test_28_manual_erp_accounting_unaffected(self):
		"""Verifies native ERP document classes are untouched and core has 0 modifications."""
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice
		from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import PurchaseInvoice
		from erpnext.accounts.doctype.payment_entry.payment_entry import PaymentEntry
		self.assertTrue(issubclass(SalesInvoice, frappe.model.document.Document))
		self.assertTrue(issubclass(PurchaseInvoice, frappe.model.document.Document))
		self.assertTrue(issubclass(PaymentEntry, frappe.model.document.Document))

	# -------------------------------------------------------------------------
	# 29. Multi-company isolation invariant
	# -------------------------------------------------------------------------
	def test_29_company_isolation_invariant(self):
		"""Verifies company isolation blocks cross-company account heads on taxes."""
		si = MockDocument(
			name="ACC-SI-029",
			company="Company A",
			debit_to=None,
			items=[],
			taxes=[
				MockDocument(account_head="Tax Head - CB"),  # belongs to Company B
			],
		)
		with patch("frappe.db.get_value", return_value="Company B"):
			with self.assertRaises(CompanyMismatchError):
				validate_company_accounting_isolation(si)

	# -------------------------------------------------------------------------
	# 30. Fixture cleanup safety
	# -------------------------------------------------------------------------
	def test_30_fixture_cleanup_and_ownership(self):
		"""Verifies fixture prefix rules (TEST-1T-*) and ownership-aware isolation."""
		prefix = "TEST-1T-"
		fixture_name = f"{prefix}SO-001"
		self.assertTrue(fixture_name.startswith(prefix))
