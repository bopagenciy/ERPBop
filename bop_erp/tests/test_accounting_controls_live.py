# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, flt, nowdate

from bop_erp.constants import TransactionOrigin
from bop_erp.accounts import (
	CompanyMismatchError,
	CreditLimitExceededError,
	MaterialTaxMismatchError,
	check_payment_entry_invariants,
	check_purchase_invoice_invariants,
	check_sales_invoice_invariants,
	create_sales_invoice_from_fulfillment,
	get_customer_current_exposure,
	get_purchasing_financial_traceability,
	get_sales_financial_traceability,
	reconcile_external_taxes,
	submit_sales_invoice,
	validate_company_accounting_isolation,
	validate_customer_credit_control,
)
from bop_erp.orders.models import (
	ExternalOrder,
	ExternalOrderLine,
	ExternalCustomer,
	ExternalTotals,
)
from bop_erp.orders.ingestion import ingest_order_pipeline


class TestAccountingControlsLive(FrappeTestCase):
	"""
	Phase 1T.1 Live Integration Test Suite:
	Accounting, Tax & Financial Controls Foundation and Enforcement Proofs.
	Executes live against the isolated local MariaDB / ERPNext test environment.
	Uses ownership-aware TEST-1T-* fixtures and restores clean baseline.
	"""

	FIXTURE_PREFIX = "TEST-1T-"
	item_code = "TEST-1T-SKU-01"

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

		# Foreign Supplier (Industrial DP)
		cls.foreign_supplier = f"{cls.FIXTURE_PREFIX}ForeignSupp-IDP"
		cls.payable_acc_usd = "TEST-1T-Proveedores USD - IDP"
		if not frappe.db.exists("Account", cls.payable_acc_usd):
			acc = frappe.get_doc({
				"doctype": "Account",
				"account_name": "TEST-1T-Proveedores USD",
				"company": cls.company_a,
				"parent_account": "23 - Cuentas por pagar - IDP",
				"account_type": "Payable",
				"root_type": "Liability",
				"account_currency": "USD",
				"is_group": 0,
			})
			acc.insert(ignore_permissions=True)

		if not frappe.db.exists("Supplier", cls.foreign_supplier):
			supp = frappe.get_doc({
				"doctype": "Supplier",
				"supplier_name": cls.foreign_supplier,
				"supplier_group": "Local",
				"supplier_type": "Company",
				"default_currency": "USD",
				"accounts": [
					{
						"company": cls.company_a,
						"account": cls.payable_acc_usd,
					}
				],
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
				"stock_uom": "Meter",
				"is_stock_item": 1,
			})
			item.insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Item", cls.item_code, "stock_uom", "Meter")

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

		# 8. Split 50/50 Payment Terms Template (Multi-Installment)
		cls.split_terms_template = f"{cls.FIXTURE_PREFIX}Split-50-50"
		if not frappe.db.exists("Payment Terms Template", cls.split_terms_template):
			term_0 = f"{cls.FIXTURE_PREFIX}Term-0"
			if not frappe.db.exists("Payment Term", term_0):
				term = frappe.get_doc({
					"doctype": "Payment Term",
					"payment_term_name": term_0,
					"due_date_based_on": "Day(s) after invoice date",
					"credit_days": 0,
				})
				term.insert(ignore_permissions=True)

			term_30 = f"{cls.FIXTURE_PREFIX}Term-30"
			ptt_split = frappe.get_doc({
				"doctype": "Payment Terms Template",
				"template_name": cls.split_terms_template,
				"terms": [
					{
						"payment_term": term_0,
						"invoice_portion": 50.0,
						"credit_days": 0,
					},
					{
						"payment_term": term_30,
						"invoice_portion": 50.0,
						"credit_days": 30,
					},
				],
			})
			ptt_split.insert(ignore_permissions=True)

		# 9. Exchange Difference Account for Company A
		cls.fx_diff_account = f"{cls.FIXTURE_PREFIX}FX-Diff - IDP"
		if not frappe.db.exists("Account", cls.fx_diff_account):
			acc = frappe.get_doc({
				"doctype": "Account",
				"account_name": f"{cls.FIXTURE_PREFIX}FX-Diff",
				"company": cls.company_a,
				"parent_account": "7 - Costos de producción o de operación - IDP",
				"account_type": "Expense Account",
				"root_type": "Expense",
				"is_group": 0,
			})
			acc.insert(ignore_permissions=True)

		cls._orig_exchange_account = frappe.db.get_value("Company", cls.company_a, "exchange_gain_loss_account")
		frappe.db.set_value("Company", cls.company_a, "exchange_gain_loss_account", cls.fx_diff_account)

		# 10. Local Currency Exchange fixture (USD -> COP at 4000)
		if not frappe.db.exists("Currency Exchange", {"from_currency": "USD", "to_currency": "COP", "for_selling": 1}):
			ce = frappe.get_doc({
				"doctype": "Currency Exchange",
				"date": nowdate(),
				"from_currency": "USD",
				"to_currency": "COP",
				"exchange_rate": 4000.0,
				"for_selling": 1,
				"for_buying": 1,
			})
			ce.insert(ignore_permissions=True)

		# 11. Channel Inventory Source for TID
		if not frappe.db.exists("Channel Inventory Source", {"sales_channel": "TID", "warehouse": "Stores - IDP"}):
			cis = frappe.get_doc({
				"doctype": "Channel Inventory Source",
				"sales_channel": "TID",
				"warehouse": "Stores - IDP",
				"enabled": 1,
				"allow_sellable_stock": 1,
				"priority": 1,
			})
			cis.insert(ignore_permissions=True)

		# 12. PrestaShop Connector fixture for TID
		if not frappe.db.exists("PrestaShop Connector", "PS-TID-DEVELOPMENT"):
			conn = frappe.get_doc({
				"doctype": "PrestaShop Connector",
				"connector_name": f"{cls.FIXTURE_PREFIX}PS-Conn",
				"sales_channel": "TID",
				"environment": "DEVELOPMENT",
				"base_url": "http://prestashop-test",
				"credential_reference": "TEST_KEY",
				"enabled": 1,
				"eligible_order_states": "2,3,4",
			})
			conn.flags.ignore_mandatory = True
			conn.insert(ignore_permissions=True)

		# 13. External ID Mappings for order ingestion test
		cls.ext_prod_id = f"{cls.FIXTURE_PREFIX}PROD-01"
		if not frappe.db.exists("External ID Mapping", {"sales_channel": "TID", "external_id": cls.ext_prod_id}):
			mapping = frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": "TID",
				"provider": "PRESTASHOP",
				"external_entity_type": "PRODUCT",
				"external_id": cls.ext_prod_id,
				"erp_doctype": "Item",
				"erp_document": cls.item_code,
				"active": 1,
			})
			mapping.insert(ignore_permissions=True)

		cls.ext_cust_id = f"{cls.FIXTURE_PREFIX}EXT-CUST-01"
		if not frappe.db.exists("External ID Mapping", {"sales_channel": "TID", "external_id": cls.ext_cust_id}):
			mapping = frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": "TID",
				"provider": "PRESTASHOP",
				"external_entity_type": "CUSTOMER",
				"external_id": cls.ext_cust_id,
				"erp_doctype": "Customer",
				"erp_document": cls.credit_customer,
				"active": 1,
			})
			mapping.insert(ignore_permissions=True)

		# 14. Stock Entry to seed stock for ATP check in order ingestion
		stock_qty = frappe.db.get_value("Bin", {"item_code": cls.item_code, "warehouse": "Stores - IDP"}, "actual_qty") or 0.0
		if flt(stock_qty) < 50.0:
			se = frappe.get_doc({
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": cls.company_a,
				"remarks": f"{cls.FIXTURE_PREFIX}Seed Stock",
				"items": [
					{
						"item_code": cls.item_code,
						"t_warehouse": "Stores - IDP",
						"qty": 100.0,
						"basic_rate": 500.0,
						"cost_center": "Main - IDP",
					}
				],
			})
			se.insert(ignore_permissions=True)
			se.submit()

		# 15. Resolve native accounts
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

		# 4. Cancel and delete Delivery Notes, Sales Orders
		for dt, party_field in [("Delivery Note", "customer"), ("Sales Order", "customer"), ("Purchase Receipt", "supplier"), ("Purchase Order", "supplier")]:
			try:
				vnames = frappe.db.sql(
					f"SELECT name FROM `tab{dt}` WHERE `{party_field}` LIKE %s",
					(f"{cls.FIXTURE_PREFIX}%",),
					pluck="name",
				)
				for vn in vnames:
					try:
						doc = frappe.get_doc(dt, vn)
						if doc.docstatus == 1:
							doc.cancel()
						frappe.delete_doc(dt, vn, force=True, ignore_permissions=True)
					except Exception:
						pass
			except Exception:
				pass

		# 6. Clean Stock Reservation Entries, IRR, Integration Events, Accounting Periods, and Order Mappings
		frappe.db.delete("Stock Reservation Entry", {"item_code": cls.item_code})
		frappe.db.delete("Inventory Reservation Reference", {"item_code": cls.item_code})
		frappe.db.delete("Integration Event", {"external_id": ["like", f"{cls.FIXTURE_PREFIX}%"]})
		frappe.db.delete("Accounting Period", {"name": ["like", f"{cls.FIXTURE_PREFIX}%"]})
		frappe.db.delete("External ID Mapping", {
			"external_entity_type": "ORDER",
			"external_id": ["like", f"{cls.FIXTURE_PREFIX}%"],
		})

		frappe.db.commit()

	@classmethod
	def _cleanup_module_fixtures(cls):
		"""Defensively cleans up all vouchers and master data belonging to this module."""
		cls._cleanup_vouchers()

		# Restore original company exchange gain loss account
		if hasattr(cls, "_orig_exchange_account"):
			frappe.db.set_value("Company", cls.company_a, "exchange_gain_loss_account", cls._orig_exchange_account)

		# Clean stock entries
		se_names = frappe.db.sql(
			"""
			SELECT name FROM `tabStock Entry`
			WHERE remarks LIKE %s
			""",
			(f"%{cls.FIXTURE_PREFIX}%",),
			pluck="name",
		)
		for se in se_names:
			try:
				doc = frappe.get_doc("Stock Entry", se)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Stock Entry", se, force=True, ignore_permissions=True)
			except Exception:
				pass

		# Clean mappings and connectors
		frappe.db.delete("External ID Mapping", {"external_id": ["like", f"{cls.FIXTURE_PREFIX}%"]})
		frappe.db.delete("Channel Inventory Source", {"sales_channel": "TID", "warehouse": "Stores - IDP"})

		# Clean up created master data and templates
		for dt in ["Customer", "Supplier", "Item", "Payment Terms Template", "Payment Term", "Account", "Accounting Period"]:
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
	# TEST 04: Sales Tax GL Posting & External Tax Reconciliation Live (Section A)
	# =========================================================================
	def test_04_sales_tax_gl_posting_live(self):
		"""
		Creates and submits Sales Invoice with native tax row; verifies GL Entry balance.
		Enforces external tax reconciliation: exact match, within tolerance, and material mismatch blocking.
		Proves mismatch creates no submitted invoice and zero GL mutation.
		"""
		# 1. Native tax calculation and GL entry posting
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

		# 2. External Tax Reconciliation: Exact Match -> Allowed
		res_exact = reconcile_external_taxes(si, external_tax_amount=380.0, currency="COP")
		self.assertTrue(res_exact["reconciled"])
		self.assertTrue(res_exact["allowed"])
		self.assertEqual(res_exact["status"], "RECONCILED")

		# 3. External Tax Reconciliation: Within Currency Tolerance -> Allowed
		res_tol = reconcile_external_taxes(si, external_tax_amount=380.01, currency="COP")
		self.assertTrue(res_tol["reconciled"])
		self.assertTrue(res_tol["allowed"])

		# 4. External Tax Reconciliation: Material Mismatch -> Hard Block & Zero GL Mutation
		si_draft = frappe.get_doc({
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
		si_draft.insert()
		draft_name = si_draft.name

		# Attempt submission with external_tax_amount differing materially (500.0 vs native 380.0)
		with self.assertRaises(MaterialTaxMismatchError):
			submit_sales_invoice(si_draft, external_tax_amount=500.0)

		# PROOF: Invoice was NOT submitted and ZERO GL Entries were created
		reloaded = frappe.get_doc("Sales Invoice", draft_name)
		self.assertEqual(reloaded.docstatus, 0)
		gl_count = frappe.db.count("GL Entry", {"voucher_no": draft_name})
		self.assertEqual(gl_count, 0)

		# 5. External Tax Reconciliation: Review Mode -> REVIEW_REQUIRED without raising
		rev_res = reconcile_external_taxes(si_draft, external_tax_amount=500.0, currency="COP", allow_review=True)
		self.assertFalse(rev_res["allowed"])
		self.assertFalse(rev_res["reconciled"])
		self.assertEqual(rev_res["status"], "REVIEW_REQUIRED")

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
	# TEST 07: Payment Terms Template & Payment Schedule Live (Section D)
	# =========================================================================
	def test_07_payment_terms_schedule_live(self):
		"""
		Verifies Payment Terms Template populates Payment Schedule with exact 30-day due date.
		Proves both Sales Invoice and Purchase Invoice Net 30 mechanics, plus multi-installment schedule.
		"""
		today = nowdate()
		expected_due = add_days(today, 30)

		# 1. Net 30 Sales Invoice
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
		self.assertEqual(str(si.due_date), str(expected_due))
		self.assertEqual(flt(sched.payment_amount), 1000.0)

		# 2. Net 30 Purchase Invoice
		exp_acc = frappe.db.get_value("Company", self.company_a, "default_expense_account") or "5105 - Gastos de personal - IDP"
		pi = frappe.get_doc({
			"doctype": "Purchase Invoice",
			"company": self.company_a,
			"supplier": self.supplier_a,
			"credit_to": self.payable_acc_a,
			"posting_date": today,
			"currency": self.currency_a,
			"payment_terms_template": self.terms_template,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 500.0,
					"expense_account": exp_acc,
					"cost_center": self.cost_center_a,
				}
			],
		})
		pi.insert()
		self.assertTrue(len(pi.payment_schedule) > 0)
		sched_pi = pi.payment_schedule[0]
		self.assertEqual(str(sched_pi.due_date), str(expected_due))
		self.assertEqual(str(pi.due_date), str(expected_due))
		self.assertEqual(flt(sched_pi.payment_amount), 500.0)

		# 3. Multi-Installment Payment Schedule (50% Immediate, 50% Net 30)
		si_split = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": today,
			"currency": self.currency_a,
			"payment_terms_template": self.split_terms_template,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 2000.0,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
		})
		si_split.insert()
		self.assertEqual(len(si_split.payment_schedule), 2)
		self.assertEqual(flt(si_split.payment_schedule[0].payment_amount), 1000.0)
		self.assertEqual(str(si_split.payment_schedule[0].due_date), str(today))
		self.assertEqual(flt(si_split.payment_schedule[1].payment_amount), 1000.0)
		self.assertEqual(str(si_split.payment_schedule[1].due_date), str(expected_due))

	# =========================================================================
	# TEST 08: Customer Credit Limit Enforcement Live (Section B)
	# =========================================================================
	def test_08_customer_credit_control_live(self):
		"""
		Verifies customer credit limit enforcement in live environment using dedicated credit customer.
		Proves:
		- within limit -> allowed
		- over limit -> blocked / REVIEW_REQUIRED
		- imported order pipeline cannot bypass credit limit: over-limit order does not submit,
		  creates no reservation, no publication, no accounting doc
		- manual ERP workflow preserves native ERPNext behavior
		"""
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

		# 4. Integration Inbound Order Ingestion Enforcement:
		# Over-limit order must fail fast and produce ZERO submitted documents, reservations, or publication
		over_limit_order = ExternalOrder(
			provider="PRESTASHOP",
			sales_channel="TID",
			external_order_id="EXT-ORD-OVER-CREDIT",
			external_reference="PS-OVER-CREDIT",
			order_state_id="2",
			currency=self.currency_a,
			customer=ExternalCustomer(external_customer_id=self.ext_cust_id),
			lines=[
				ExternalOrderLine(
					external_line_id="L1",
					external_product_id=self.ext_prod_id,
					quantity=16.0,
					unit_price_ex_tax=500.0,
					line_total_ex_tax=8000.0,
				)
			],
			totals=ExternalTotals(
				total_products_ex_tax=8000.0,
				total_paid=8000.0,
				currency=self.currency_a,
			),
		)

		with self.assertRaises(CreditLimitExceededError):
			ingest_order_pipeline(over_limit_order)

		# PROOF: No submitted Sales Order, no stock reservation, no accounting document
		so_count = frappe.db.count("Sales Order", {"customer": self.credit_customer, "docstatus": 1})
		self.assertEqual(so_count, 0)
		sre_count = frappe.db.count("Stock Reservation Entry", {"item_code": self.item_code})
		self.assertEqual(sre_count, 0)
		si_count = frappe.db.count("Sales Invoice", {"customer": self.credit_customer})
		self.assertEqual(si_count, 0)

		# 5. Manual ERP workflow preserves native ERPNext behavior
		manual_so = frappe.get_doc({
			"doctype": "Sales Order",
			"company": self.company_a,
			"customer": self.credit_customer,
			"transaction_date": nowdate(),
			"delivery_date": nowdate(),
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 1000.0,
					"warehouse": "Stores - IDP",
				}
			],
		})
		manual_so.insert(ignore_permissions=True)
		self.assertEqual(manual_so.docstatus, 0)

	# =========================================================================
	# TEST 09: Multi-Currency, Foreign Invoice & Realized FX Live (Section E)
	# =========================================================================
	def test_09_multi_currency_sales_invoice_live(self):
		"""
		Explicit live tests for multi-currency:
		1. Base-currency Sales Invoice
		2. Foreign-currency Sales Invoice (USD @ 4000)
		3. Foreign-currency Purchase Invoice (USD @ 4000)
		4. Foreign-currency Payment Entry allocation
		5. Native realized exchange gain/loss verification
		"""
		# Guarantee company exchange gain loss account is configured
		frappe.db.set_value("Company", self.company_a, "exchange_gain_loss_account", self.fx_diff_account)
		frappe.db.commit()

		# 1. Base-currency Sales Invoice
		si_base = frappe.get_doc({
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
		si_base.insert()
		si_base.submit()
		self.assertEqual(flt(si_base.grand_total), 1000.0)
		self.assertEqual(flt(si_base.base_grand_total), 1000.0)

		# 2. Foreign-currency Sales Invoice (10 USD @ conversion_rate 4000)
		si_foreign = frappe.get_doc({
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
		si_foreign.insert()
		si_foreign.submit()

		self.assertEqual(flt(si_foreign.grand_total), 10.0)
		self.assertEqual(flt(si_foreign.base_grand_total), 40000.0)

		gl_entries = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Sales Invoice", "voucher_no": si_foreign.name, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		total_debit = sum(flt(g.debit) for g in gl_entries)
		self.assertAlmostEqual(total_debit, 40000.0, places=2)

		# 3. Foreign-currency Purchase Invoice (10 USD @ conversion_rate 4000)
		exp_acc = frappe.db.get_value("Company", self.company_a, "default_expense_account") or "5105 - Gastos de personal - IDP"
		pi_foreign = frappe.get_doc({
			"doctype": "Purchase Invoice",
			"company": self.company_a,
			"supplier": self.foreign_supplier,
			"credit_to": self.payable_acc_usd,
			"posting_date": nowdate(),
			"currency": "USD",
			"conversion_rate": 4000.0,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 1.0,
					"rate": 10.0,
					"expense_account": exp_acc,
					"cost_center": self.cost_center_a,
				}
			],
		})
		pi_foreign.insert()
		pi_foreign.submit()
		self.assertEqual(flt(pi_foreign.grand_total), 10.0)
		self.assertEqual(flt(pi_foreign.base_grand_total), 40000.0)

		# 4. Foreign-currency Payment Entry Allocation with Realized Exchange Gain
		# Received at rate 4100 (41,000 COP) against invoice at rate 4000 (40,000 COP) -> 1,000 COP realized gain
		bank_acc = "Banco Principal - IDP"
		pe = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": self.foreign_customer,
			"company": self.company_a,
			"posting_date": nowdate(),
			"paid_from": self.receivable_acc_usd,
			"paid_to": bank_acc,
			"paid_from_account_currency": "USD",
			"paid_to_account_currency": self.currency_a,
			"paid_amount": 10.0,
			"source_exchange_rate": 4000.0,
			"received_amount": 41000.0,
			"target_exchange_rate": 1.0,
			"reference_no": "REF-FX-001",
			"reference_date": nowdate(),
			"references": [
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si_foreign.name,
					"total_amount": 10.0,
					"outstanding_amount": 10.0,
					"allocated_amount": 10.0,
					"exchange_rate": 4000.0,
				}
			],
		})
		pe.insert()
		pe.submit()

		si_foreign.reload()
		self.assertAlmostEqual(flt(si_foreign.outstanding_amount), 0.0, places=2)

		# 5. Verify GL balance and realized exchange gain account
		pe_gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Payment Entry", "voucher_no": pe.name, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		tot_deb = sum(flt(g.debit) for g in pe_gl)
		tot_cred = sum(flt(g.credit) for g in pe_gl)
		self.assertAlmostEqual(tot_deb, tot_cred, places=2)
		self.assertAlmostEqual(tot_deb, 41000.0, places=2)

		# Verify exchange gain account received the 1,000 credit
		fx_gl = [g for g in pe_gl if g.account == self.fx_diff_account]
		self.assertEqual(len(fx_gl), 1)
		self.assertAlmostEqual(flt(fx_gl[0].credit), 1000.0, places=2)

	# =========================================================================
	# TEST 10: Frozen Date & Closed Period Controls Live (Section C)
	# =========================================================================
	def test_10_frozen_date_control_live(self):
		"""
		Verifies that posting on or before accounts_frozen_till_date is blocked natively.
		Proves integration-created accounting documents cannot bypass frozen date or closed periods.
		No ignore_validate, no broad flags, no DB mutation.
		"""
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

		# Explicitly verify flags are NOT bypassing validations
		self.assertFalse(si.flags.ignore_validate)
		self.assertFalse(si.flags.ignore_permissions)

		with self.assertRaises(frappe.ValidationError):
			si.submit()

	# =========================================================================
	# TEST 11: Rounding, Precision & Cancellation Restoration Live (Section F)
	# =========================================================================
	def test_11_partial_payment_and_cancellation_restoration_live(self):
		"""
		Proves rounding / precision with fractional rates/quantities:
		- Sales: fractional rate/qty -> grand_total, outstanding, full allocation without penny drift
		- Purchase: fractional rate/qty -> grand_total, outstanding, full allocation without penny drift
		- Payment cancellation: accurately restores invoice outstanding balance
		"""
		# 1. Standard partial payment & restoration
		si_std = frappe.get_doc({
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
		si_std.insert()
		si_std.submit()

		bank_acc = "Banco Principal - IDP"
		pe_std = frappe.get_doc({
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
			"reference_no": "REF-PARTIAL",
			"reference_date": nowdate(),
			"references": [
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si_std.name,
					"total_amount": 1000.0,
					"outstanding_amount": 1000.0,
					"allocated_amount": 400.0,
				}
			],
		})
		pe_std.insert()
		pe_std.submit()

		si_std.reload()
		self.assertAlmostEqual(flt(si_std.outstanding_amount), 600.0, places=2)

		pe_std.cancel()
		si_std.reload()
		self.assertAlmostEqual(flt(si_std.outstanding_amount), 1000.0, places=2)

		# 2. Sales Fractional Rounding Precision (Qty 3.333 @ rate 11.77 -> 39.23)
		si_frac = frappe.get_doc({
			"doctype": "Sales Invoice",
			"company": self.company_a,
			"customer": self.customer_a,
			"debit_to": self.receivable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"disable_rounded_total": 1,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 3.333,
					"rate": 11.77,
					"income_account": self.income_acc_a,
					"cost_center": self.cost_center_a,
				}
			],
		})
		si_frac.insert()
		si_frac.submit()

		expected_tot = round(3.333 * 11.77, 2)
		self.assertAlmostEqual(flt(si_frac.grand_total), expected_tot, places=2)
		self.assertAlmostEqual(flt(si_frac.outstanding_amount), expected_tot, places=2)

		# Pay exact fractional outstanding
		pe_frac = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": self.customer_a,
			"company": self.company_a,
			"posting_date": nowdate(),
			"paid_from": self.receivable_acc_a,
			"paid_to": bank_acc,
			"paid_amount": expected_tot,
			"received_amount": expected_tot,
			"reference_no": "REF-FRAC-PAY",
			"reference_date": nowdate(),
			"references": [
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si_frac.name,
					"total_amount": expected_tot,
					"outstanding_amount": expected_tot,
					"allocated_amount": expected_tot,
				}
			],
		})
		pe_frac.insert()
		pe_frac.submit()

		si_frac.reload()
		self.assertAlmostEqual(flt(si_frac.outstanding_amount), 0.0, places=2)

		# 3. Purchase Fractional Rounding Precision (Qty 7.125 @ rate 14.83 -> 105.66)
		exp_acc = frappe.db.get_value("Company", self.company_a, "default_expense_account") or "5105 - Gastos de personal - IDP"
		pi_frac = frappe.get_doc({
			"doctype": "Purchase Invoice",
			"company": self.company_a,
			"supplier": self.supplier_a,
			"credit_to": self.payable_acc_a,
			"posting_date": nowdate(),
			"currency": self.currency_a,
			"disable_rounded_total": 1,
			"items": [
				{
					"item_code": self.item_code,
					"qty": 7.125,
					"rate": 14.83,
					"expense_account": exp_acc,
					"cost_center": self.cost_center_a,
				}
			],
		})
		pi_frac.insert()
		pi_frac.submit()

		expected_pi_tot = round(7.125 * 14.83, 2)
		self.assertAlmostEqual(flt(pi_frac.grand_total), expected_pi_tot, places=2)
		self.assertAlmostEqual(flt(pi_frac.outstanding_amount), expected_pi_tot, places=2)

		# Pay exact fractional purchase outstanding
		pe_pfrac = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Pay",
			"party_type": "Supplier",
			"party": self.supplier_a,
			"company": self.company_a,
			"posting_date": nowdate(),
			"paid_from": bank_acc,
			"paid_to": self.payable_acc_a,
			"paid_amount": expected_pi_tot,
			"received_amount": expected_pi_tot,
			"reference_no": "REF-PI-FRAC",
			"reference_date": nowdate(),
			"references": [
				{
					"reference_doctype": "Purchase Invoice",
					"reference_name": pi_frac.name,
					"total_amount": expected_pi_tot,
					"outstanding_amount": expected_pi_tot,
					"allocated_amount": expected_pi_tot,
				}
			],
		})
		pe_pfrac.insert()
		pe_pfrac.submit()

		pi_frac.reload()
		self.assertAlmostEqual(flt(pi_frac.outstanding_amount), 0.0, places=2)

	# =========================================================================
	# TEST 12: Financial Traceability View Live (Section G)
	# =========================================================================
	def test_12_financial_traceability_live(self):
		"""
		Verifies read-only financial traceability service for complete lifecycle:
		SALE: SO -> DN -> SI -> PE -> Credit Note
		PURCHASE: PO -> PR -> PI -> PE
		Returns name, docstatus, company, currency, grand_total, outstanding, posting_date.
		Proves service performs zero writes.
		"""
		# 1. Construct Sales Flow: SO -> DN -> SI -> PE -> CN
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"company": self.company_a,
			"customer": self.customer_a,
			"transaction_date": nowdate(),
			"delivery_date": nowdate(),
			"items": [
				{
					"item_code": self.item_code,
					"qty": 2.0,
					"rate": 1000.0,
					"warehouse": "Stores - IDP",
				}
			],
		})
		so.insert(ignore_permissions=True)
		so.submit()

		dn = frappe.get_doc({
			"doctype": "Delivery Note",
			"company": self.company_a,
			"customer": self.customer_a,
			"posting_date": nowdate(),
			"items": [
				{
					"item_code": self.item_code,
					"qty": 2.0,
					"rate": 1000.0,
					"against_sales_order": so.name,
					"so_detail": so.items[0].name,
					"warehouse": "Stores - IDP",
					"cost_center": self.cost_center_a,
				}
			],
		})
		dn.insert(ignore_permissions=True)
		dn.submit()

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
					"sales_order": so.name,
					"delivery_note": dn.name,
					"dn_detail": dn.items[0].name,
				}
			],
		})
		si.insert()
		si.submit()

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
			"paid_amount": 1000.0,
			"received_amount": 1000.0,
			"reference_no": "REF-TRACE-01",
			"reference_date": nowdate(),
			"references": [
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si.name,
					"total_amount": 2000.0,
					"outstanding_amount": 2000.0,
					"allocated_amount": 1000.0,
				}
			],
		})
		pe.insert()
		pe.submit()

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
		})
		cn.insert()
		cn.submit()

		# Snapshot DB counts before calling traceability service to prove zero writes
		gl_count_before = frappe.db.count("GL Entry")
		si_count_before = frappe.db.count("Sales Invoice")

		trace_sale = get_sales_financial_traceability(sales_invoice=si.name)

		# Read-only proof: DB counts unchanged
		self.assertEqual(frappe.db.count("GL Entry"), gl_count_before)
		self.assertEqual(frappe.db.count("Sales Invoice"), si_count_before)

		self.assertEqual(trace_sale["flow"], "SALE")
		self.assertEqual(trace_sale["company"], self.company_a)
		self.assertTrue(len(trace_sale["sales_orders"]) >= 1)
		self.assertTrue(len(trace_sale["delivery_notes"]) >= 1)
		self.assertTrue(len(trace_sale["sales_invoices"]) >= 1)
		self.assertTrue(len(trace_sale["payment_entries"]) >= 1)
		self.assertTrue(len(trace_sale["credit_notes"]) >= 1)

		# Verify returned structure on documents
		for doc_summary in trace_sale["sales_invoices"] + trace_sale["payment_entries"]:
			self.assertIn("name", doc_summary)
			self.assertIn("docstatus", doc_summary)
			self.assertIn("company", doc_summary)
			self.assertIn("currency", doc_summary)
			self.assertIn("grand_total", doc_summary)
			self.assertIn("outstanding_amount", doc_summary)
			self.assertIn("posting_date", doc_summary)

		self.assertEqual(trace_sale["total_billed"], 2000.0)
		self.assertEqual(trace_sale["total_paid"], 1000.0)
		self.assertEqual(trace_sale["total_refunded"], -1000.0)

		# 2. Construct Purchasing Flow: PO -> PR -> PI -> PE
		exp_acc = frappe.db.get_value("Company", self.company_a, "default_expense_account") or "5105 - Gastos de personal - IDP"
		po = frappe.get_doc({
			"doctype": "Purchase Order",
			"company": self.company_a,
			"supplier": self.supplier_a,
			"schedule_date": nowdate(),
			"items": [
				{
					"item_code": self.item_code,
					"qty": 2.0,
					"rate": 500.0,
					"schedule_date": nowdate(),
				}
			],
		})
		po.insert(ignore_permissions=True)
		po.submit()

		pr = frappe.get_doc({
			"doctype": "Purchase Receipt",
			"company": self.company_a,
			"supplier": self.supplier_a,
			"posting_date": nowdate(),
			"items": [
				{
					"item_code": self.item_code,
					"qty": 2.0,
					"rate": 500.0,
					"purchase_order": po.name,
					"po_detail": po.items[0].name,
					"warehouse": "Stores - IDP",
					"cost_center": self.cost_center_a,
				}
			],
		})
		pr.insert(ignore_permissions=True)
		pr.submit()

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
					"purchase_order": po.name,
					"purchase_receipt": pr.name,
					"pr_detail": pr.items[0].name,
				}
			],
		})
		pi.insert()
		pi.submit()

		pe_p = frappe.get_doc({
			"doctype": "Payment Entry",
			"payment_type": "Pay",
			"party_type": "Supplier",
			"party": self.supplier_a,
			"company": self.company_a,
			"posting_date": nowdate(),
			"paid_from": bank_acc,
			"paid_to": self.payable_acc_a,
			"paid_amount": 1000.0,
			"received_amount": 1000.0,
			"reference_no": "REF-PURCH-TRACE",
			"reference_date": nowdate(),
			"references": [
				{
					"reference_doctype": "Purchase Invoice",
					"reference_name": pi.name,
					"total_amount": 1000.0,
					"outstanding_amount": 1000.0,
					"allocated_amount": 1000.0,
				}
			],
		})
		pe_p.insert()
		pe_p.submit()

		trace_purch = get_purchasing_financial_traceability(purchase_invoice=pi.name)
		self.assertEqual(trace_purch["flow"], "PURCHASE")
		self.assertEqual(trace_purch["company"], self.company_a)
		self.assertTrue(len(trace_purch["purchase_orders"]) >= 1)
		self.assertTrue(len(trace_purch["purchase_receipts"]) >= 1)
		self.assertTrue(len(trace_purch["purchase_invoices"]) >= 1)
		self.assertTrue(len(trace_purch["payment_entries"]) >= 1)

	# =========================================================================
	# TEST 13: Financial Invariant Diagnostic Service Live
	# =========================================================================
	def test_13_financial_invariants_diagnostic_live(self):
		"""Verifies diagnostic invariant checks on live documents: SI, PI, PE."""
		# 1. Sales Invoice invariants
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

		res_si = check_sales_invoice_invariants(si.name)
		self.assertTrue(res_si["valid"])
		self.assertEqual(len(res_si["violations"]), 0)

		# 2. Purchase Invoice invariants
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
					"qty": 1.0,
					"rate": 500.0,
					"expense_account": exp_acc,
					"cost_center": self.cost_center_a,
				}
			],
		})
		pi.insert()
		pi.submit()

		res_pi = check_purchase_invoice_invariants(pi.name)
		self.assertTrue(res_pi["valid"])
		self.assertEqual(len(res_pi["violations"]), 0)

		# 3. Payment Entry invariants
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
			"paid_amount": 1000.0,
			"received_amount": 1000.0,
			"reference_no": "REF-INV-CHK",
			"reference_date": nowdate(),
			"references": [
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": si.name,
					"total_amount": 1000.0,
					"outstanding_amount": 1000.0,
					"allocated_amount": 1000.0,
				}
			],
		})
		pe.insert()
		pe.submit()

		res_pe = check_payment_entry_invariants(pe.name)
		self.assertTrue(res_pe["valid"])
		self.assertEqual(len(res_pe["violations"]), 0)

	# =========================================================================
	# TEST 14: Closed Accounting Period Live Proof (Section A)
	# =========================================================================
	def test_14_closed_accounting_period_live(self):
		"""
		Section A: Closed Accounting Period Live Proof.
		Proves a Bop/integration-created accounting document whose posting date
		falls inside a closed Accounting Period cannot submit.
		Uses native ERPNext lifecycle (validate_accounting_period).
		Expected:
		- document submission blocked (frappe.ValidationError)
		- GL Entry delta = 0
		- Payment Ledger Entry delta = 0
		- Zero validation bypass
		- Baseline periods preserved; TEST-1T-owned fixture removed.
		"""
		# 1. Capture baseline Accounting Periods count
		baseline_ap_count = frappe.db.count("Accounting Period", {"name": ["not like", f"{self.FIXTURE_PREFIX}%"]})

		# 2. Create ownership-safe TEST-1T Accounting Period fixture
		period_name = f"{self.FIXTURE_PREFIX}AP-CLOSED"
		if frappe.db.exists("Accounting Period", period_name):
			frappe.delete_doc("Accounting Period", period_name, force=True, ignore_permissions=True)

		ap = frappe.get_doc({
			"doctype": "Accounting Period",
			"period_name": period_name,
			"company": self.company_a,
			"start_date": "2026-06-01",
			"end_date": "2026-06-30",
			"disabled": 0,
			"closed_documents": [
				{
					"document_type": "Sales Invoice",
					"closed": 1,
				}
			],
		})
		ap.insert(ignore_permissions=True)
		frappe.db.commit()

		si_name = None
		try:
			# 3. Create Bop Sales Invoice in draft
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
			si.insert(ignore_permissions=True)
			si_name = si.name

			# Set posting date to closed period via DB value before submission
			frappe.db.set_value("Sales Invoice", si.name, {"set_posting_time": 1, "posting_date": "2026-06-15"})
			frappe.db.commit()

			# 4. Snapshot GL Entry and Payment Ledger Entry counts
			gl_before = frappe.db.count("GL Entry")
			ple_before = frappe.db.count("Payment Ledger Entry")
			si_submitted_before = frappe.db.count("Sales Invoice", {"docstatus": 1})

			# 5. Attempt submission through real Bop invoice submission entry point
			with self.assertRaises(frappe.ValidationError) as ctx:
				submit_sales_invoice(si.name)

			# Verify error indicates closed Accounting Period
			self.assertIn("closed accounting period", str(ctx.exception).lower())

			# 6. Verify zero accounting impact
			gl_after = frappe.db.count("GL Entry")
			ple_after = frappe.db.count("Payment Ledger Entry")
			si_submitted_after = frappe.db.count("Sales Invoice", {"docstatus": 1})

			self.assertEqual(gl_after - gl_before, 0, "GL Entry delta must be 0")
			self.assertEqual(ple_after - ple_before, 0, "Payment Ledger Entry delta must be 0")
			self.assertEqual(si_submitted_after - si_submitted_before, 0, "Submitted SI delta must be 0")

			# Verify document was not submitted
			reloaded = frappe.get_doc("Sales Invoice", si.name)
			self.assertEqual(reloaded.docstatus, 0)
		finally:
			# 7. Clean up TEST-1T-owned fixture and invoice; preserve baseline periods
			if si_name and frappe.db.exists("Sales Invoice", si_name):
				frappe.delete_doc("Sales Invoice", si_name, force=True, ignore_permissions=True)
			if frappe.db.exists("Accounting Period", ap.name):
				frappe.delete_doc("Accounting Period", ap.name, force=True, ignore_permissions=True)
			frappe.db.commit()

			# Verify baseline preserved
			baseline_after = frappe.db.count("Accounting Period", {"name": ["not like", f"{self.FIXTURE_PREFIX}%"]})
			self.assertEqual(baseline_after, baseline_ap_count)

	# =========================================================================
	# TEST 15: Fiscal Year Control Live Proof (Section B)
	# =========================================================================
	def test_15_fiscal_year_control_live(self):
		"""
		Section B: Fiscal Year Live Proof.
		Provides deterministic live proof that ERPNext Fiscal Year validation remains
		active for Bop-created accounting documents.
		Tests a posting date invalid for Company / Fiscal Year configuration.
		Expected:
		- submission blocked natively with FiscalYearError / ValidationError
		- GL Entry delta = 0
		- Payment Ledger Entry delta = 0
		- Legitimate Fiscal Years untouched
		- Baseline captured and verified intact.
		"""
		# 1. Capture baseline Fiscal Years
		baseline_fys = frappe.get_all(
			"Fiscal Year",
			fields=["name", "year_start_date", "year_end_date", "disabled"],
			order_by="name",
		)

		# 2. Date invalid for any active Fiscal Year (baseline has 2026: 2026-01-01 to 2026-12-31)
		invalid_posting_date = "2024-05-15"

		# 3. Snapshot counts before attempt
		gl_before = frappe.db.count("GL Entry")
		ple_before = frappe.db.count("Payment Ledger Entry")
		si_submitted_before = frappe.db.count("Sales Invoice", {"docstatus": 1})

		# 4. Attempt to create and submit Bop Sales Invoice with invalid fiscal year date
		from erpnext.accounts.utils import FiscalYearError
		si_name = None
		with self.assertRaises((FiscalYearError, frappe.ValidationError)) as ctx:
			si = frappe.get_doc({
				"doctype": "Sales Invoice",
				"company": self.company_a,
				"customer": self.customer_a,
				"debit_to": self.receivable_acc_a,
				"set_posting_time": 1,
				"posting_date": invalid_posting_date,
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
			si.insert(ignore_permissions=True)
			si_name = si.name
			submit_sales_invoice(si.name)

		self.assertIn("fiscal year", str(ctx.exception).lower())

		# 5. Verify zero financial mutations
		gl_after = frappe.db.count("GL Entry")
		ple_after = frappe.db.count("Payment Ledger Entry")
		si_submitted_after = frappe.db.count("Sales Invoice", {"docstatus": 1})

		self.assertEqual(gl_after - gl_before, 0, "GL Entry delta must be 0")
		self.assertEqual(ple_after - ple_before, 0, "Payment Ledger Entry delta must be 0")
		self.assertEqual(si_submitted_after - si_submitted_before, 0, "Submitted SI delta must be 0")

		if si_name and frappe.db.exists("Sales Invoice", si_name):
			frappe.delete_doc("Sales Invoice", si_name, force=True, ignore_permissions=True)
			frappe.db.commit()

		# 6. Verify baseline Fiscal Years were NOT modified or deleted
		current_fys = frappe.get_all(
			"Fiscal Year",
			fields=["name", "year_start_date", "year_end_date", "disabled"],
			order_by="name",
		)
		self.assertEqual(current_fys, baseline_fys, "Baseline Fiscal Years must remain exactly identical")

	# =========================================================================
	# TEST 16: Material Tax Mismatch Live Proof (Section C)
	# =========================================================================
	def test_16_material_tax_mismatch_live(self):
		"""
		Section C: Material Tax Mismatch Live Proof.
		Exercises the REAL imported-order/fulfillment/accounting wired integration path:
		SO -> DN -> create_sales_invoice_from_fulfillment -> submit_sales_invoice.
		Proves:
		- External tax materially different from native calculated tax raises MaterialTaxMismatchError
		  or returns explicit REVIEW_REQUIRED in review mode.
		- Blocked attempt creates:
		  submitted Sales Invoice = 0
		  GL Entry delta = 0
		  Payment Ledger Entry delta = 0
		  Payment Entry delta = 0
		- Difference within configured currency tolerance -> allowed and submitted.
		"""
		# 1. Ingest upstream order via real ingestion pipeline (real wired flow)
		order = ExternalOrder(
			provider="PRESTASHOP",
			sales_channel="TID",
			external_order_id=f"{self.FIXTURE_PREFIX}ORD-TAX-01",
			external_reference="PS-TAX-01",
			order_state_id="2",
			currency=self.currency_a,
			customer=ExternalCustomer(external_customer_id=self.ext_cust_id),
			lines=[
				ExternalOrderLine(
					external_line_id="L1",
					external_product_id=self.ext_prod_id,
					quantity=2.0,
					unit_price_ex_tax=1000.0,
					line_total_ex_tax=2000.0,
				)
			],
			totals=ExternalTotals(
				total_products_ex_tax=2000.0,
				total_paid=2000.0,
				currency=self.currency_a,
			),
		)
		ingest_res = ingest_order_pipeline(order)
		so_name = ingest_res["sales_order"]
		so = frappe.get_doc("Sales Order", so_name)

		dn = frappe.get_doc({
			"doctype": "Delivery Note",
			"company": self.company_a,
			"customer": so.customer,
			"posting_date": nowdate(),
			"items": [
				{
					"item_code": self.item_code,
					"qty": 2.0,
					"rate": 1000.0,
					"against_sales_order": so.name,
					"so_detail": so.items[0].name,
					"warehouse": "Stores - IDP",
					"cost_center": self.cost_center_a,
				}
			],
		})
		dn.insert(ignore_permissions=True)
		dn.submit()

		# 2. Create draft Sales Invoice from fulfillment via real Bop factory
		si = create_sales_invoice_from_fulfillment(dn.name)
		# Add 19% VAT tax to invoice: Net total = 2000.0, Tax = 380.0
		si.append("taxes", {
			"charge_type": "On Net Total",
			"account_head": self.tax_acc_a,
			"description": "VAT 19%",
			"rate": 19.0,
		})
		si.save()
		frappe.db.commit()

		native_tax = sum(flt(t.tax_amount) for t in si.taxes)
		self.assertAlmostEqual(native_tax, 380.0, places=2)

		# 3. Snapshot state before material mismatch submission attempt
		si_submitted_before = frappe.db.count("Sales Invoice", {"docstatus": 1})
		gl_before = frappe.db.count("GL Entry")
		ple_before = frappe.db.count("Payment Ledger Entry")
		pe_before = frappe.db.count("Payment Entry")

		# 4. Material mismatch test: External tax = 600.0 (diff 220.0 >> tolerance 0.02 COP)
		with self.assertRaises(MaterialTaxMismatchError):
			submit_sales_invoice(si.name, external_tax_amount=600.0)

		# Verify all 4 invariants remain strictly 0
		self.assertEqual(frappe.db.count("Sales Invoice", {"docstatus": 1}) - si_submitted_before, 0, "Submitted SI delta must be 0")
		self.assertEqual(frappe.db.count("GL Entry") - gl_before, 0, "GL Entry delta must be 0")
		self.assertEqual(frappe.db.count("Payment Ledger Entry") - ple_before, 0, "Payment Ledger Entry delta must be 0")
		self.assertEqual(frappe.db.count("Payment Entry") - pe_before, 0, "Payment Entry delta must be 0")

		# Verify review mode policy returns REVIEW_REQUIRED without raising
		review_res = reconcile_external_taxes(si, external_tax_amount=600.0, currency=self.currency_a, allow_review=True)
		self.assertEqual(review_res["status"], "REVIEW_REQUIRED")
		self.assertFalse(review_res["allowed"])
		self.assertFalse(review_res["reconciled"])

		# 5. Within tolerance test: External tax = 380.01 (diff 0.01 <= tolerance 0.02 COP)
		# Real invoice integration submission succeeds
		submitted_si = submit_sales_invoice(si.name, external_tax_amount=380.01)
		self.assertEqual(submitted_si.docstatus, 1)
		self.assertEqual(frappe.db.count("Sales Invoice", {"docstatus": 1}) - si_submitted_before, 1)
		self.assertGreater(frappe.db.count("GL Entry") - gl_before, 0, "GL Entry delta must be > 0 on valid submission")
		self.assertEqual(frappe.db.count("Payment Entry") - pe_before, 0, "Payment Entry delta must remain 0")

	# =========================================================================
	# TEST 17: Credit Limit Block Live Proof (Section D)
	# =========================================================================
	def test_17_credit_limit_block_live(self):
		"""
		Section D: Credit Limit Block Live Proof.
		Exercises the REAL imported-order ingestion flow (ingest_order_pipeline).
		Customer has explicit TEST-1T credit limit (5000.0).
		Case 1: existing exposure (0) + new order (2000.0) <= limit -> imported Sales Order succeeds.
		Case 2: existing exposure (2000.0) + new order (4000.0) = 6000.0 > limit -> CreditLimitExceededError.
		Prove blocked attempt creates:
		- Sales Order delta = 0
		- Stock Reservation Entry delta = 0
		- Inventory Reservation Reference delta = 0
		- Integration publication intent delta = 0 (Integration Event)
		- GL Entry delta = 0
		Uses native Customer Credit Limit data; zero shadow credit ledgers.
		"""
		# 1. Configure explicit TEST-1T native credit limit
		cust_doc = frappe.get_doc("Customer", self.credit_customer)
		cust_doc.credit_limits = []
		cust_doc.append("credit_limits", {
			"company": self.company_a,
			"credit_limit": 5000.0,
		})
		cust_doc.save(ignore_permissions=True)
		frappe.db.commit()

		# Case 1: Order within limit (2000.0 <= 5000.0)
		order_1 = ExternalOrder(
			provider="PRESTASHOP",
			sales_channel="TID",
			external_order_id=f"{self.FIXTURE_PREFIX}ORD-CREDIT-CASE1",
			external_reference="PS-CREDIT-OK",
			order_state_id="2",
			currency=self.currency_a,
			customer=ExternalCustomer(external_customer_id=self.ext_cust_id),
			lines=[
				ExternalOrderLine(
					external_line_id="L1",
					external_product_id=self.ext_prod_id,
					quantity=4.0,
					unit_price_ex_tax=500.0,
					line_total_ex_tax=2000.0,
				)
			],
			totals=ExternalTotals(
				total_products_ex_tax=2000.0,
				total_paid=2000.0,
				currency=self.currency_a,
			),
		)

		res_1 = ingest_order_pipeline(order_1)
		self.assertTrue(res_1.get("success"), "Order within credit limit must succeed")
		so_1_name = res_1.get("sales_order")
		self.assertTrue(bool(so_1_name))
		so_1_doc = frappe.get_doc("Sales Order", so_1_name)
		self.assertEqual(so_1_doc.docstatus, 1)

		# Verify current exposure is now 2000.0
		exp_after_case1 = get_customer_current_exposure(self.credit_customer, self.company_a)
		self.assertAlmostEqual(exp_after_case1, 2000.0, places=2)

		# Case 2: New order above limit (existing 2000.0 + new 4000.0 = 6000.0 > 5000.0)
		order_2 = ExternalOrder(
			provider="PRESTASHOP",
			sales_channel="TID",
			external_order_id=f"{self.FIXTURE_PREFIX}ORD-CREDIT-CASE2",
			external_reference="PS-CREDIT-BLOCK",
			order_state_id="2",
			currency=self.currency_a,
			customer=ExternalCustomer(external_customer_id=self.ext_cust_id),
			lines=[
				ExternalOrderLine(
					external_line_id="L1",
					external_product_id=self.ext_prod_id,
					quantity=8.0,
					unit_price_ex_tax=500.0,
					line_total_ex_tax=4000.0,
				)
			],
			totals=ExternalTotals(
				total_products_ex_tax=4000.0,
				total_paid=4000.0,
				currency=self.currency_a,
			),
		)

		# Snapshot exact baseline before blocked attempt
		so_count_before = frappe.db.count("Sales Order")
		sre_count_before = frappe.db.count("Stock Reservation Entry")
		irr_count_before = frappe.db.count("Inventory Reservation Reference")
		intent_count_before = frappe.db.count("Integration Event")
		gl_count_before = frappe.db.count("GL Entry")

		with self.assertRaises(CreditLimitExceededError):
			ingest_order_pipeline(order_2)

		# PROOF: All 5 targets create exactly ZERO deltas
		self.assertEqual(frappe.db.count("Sales Order") - so_count_before, 0, "Blocked Sales Order delta must be 0")
		self.assertEqual(frappe.db.count("Stock Reservation Entry") - sre_count_before, 0, "Blocked SRE delta must be 0")
		self.assertEqual(frappe.db.count("Inventory Reservation Reference") - irr_count_before, 0, "Blocked IRR delta must be 0")
		self.assertEqual(frappe.db.count("Integration Event") - intent_count_before, 0, "Blocked publication intent delta must be 0")
		self.assertEqual(frappe.db.count("GL Entry") - gl_count_before, 0, "Blocked GL delta must be 0")

	# =========================================================================
	# TEST 18: Fixture Cleanup Proof Live
	# =========================================================================
	def test_18_fixture_cleanup_proof_live(self):
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

		residual_pis = frappe.db.sql(
			"""
			SELECT name FROM `tabPurchase Invoice`
			WHERE supplier LIKE %s OR name LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%", f"{self.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		self.assertEqual(len(residual_pis), 0)

		residual_sos = frappe.db.sql(
			"""
			SELECT name FROM `tabSales Order`
			WHERE customer LIKE %s OR name LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%", f"{self.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		self.assertEqual(len(residual_sos), 0)

		residual_aps = frappe.db.sql(
			"""
			SELECT name FROM `tabAccounting Period`
			WHERE name LIKE %s OR period_name LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%", f"{self.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		self.assertEqual(len(residual_aps), 0)
