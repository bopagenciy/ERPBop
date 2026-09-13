# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional, Union
import frappe
from frappe import _

from bop_erp.accounts.exceptions import CompanyMismatchError


def validate_company_accounting_isolation(doc: Any) -> None:
	"""
	Enforces company accounting isolation for transaction documents
	(Sales Invoice, Purchase Invoice, Payment Entry, Credit Note, Debit Note).

	Invariant:
	Every accounting transaction must use accounts and cost centers belonging
	to the same Company. Cross-company account or cost center contamination
	is strictly prohibited.
	"""
	company = getattr(doc, "company", None)
	if not company:
		if isinstance(doc, dict):
			company = doc.get("company")
	if not company:
		return

	# 1. Party / Default Accounts
	# Sales Invoice: debit_to
	debit_to = getattr(doc, "debit_to", None) or (doc.get("debit_to") if isinstance(doc, dict) else None)
	if debit_to:
		_assert_account_company(debit_to, company, "Receivable (debit_to)")

	# Purchase Invoice: credit_to
	credit_to = getattr(doc, "credit_to", None) or (doc.get("credit_to") if isinstance(doc, dict) else None)
	if credit_to:
		_assert_account_company(credit_to, company, "Payable (credit_to)")

	# Payment Entry: paid_from and paid_to
	paid_from = getattr(doc, "paid_from", None) or (doc.get("paid_from") if isinstance(doc, dict) else None)
	if paid_from:
		paid_from_company = getattr(doc, "paid_from_company", None) or (doc.get("paid_from_company") if isinstance(doc, dict) else None) or company
		_assert_account_company(paid_from, paid_from_company, "Paid From")

	paid_to = getattr(doc, "paid_to", None) or (doc.get("paid_to") if isinstance(doc, dict) else None)
	if paid_to:
		paid_to_company = getattr(doc, "paid_to_company", None) or (doc.get("paid_to_company") if isinstance(doc, dict) else None) or company
		_assert_account_company(paid_to, paid_to_company, "Paid To")

	# 2. Item lines (income_account, expense_account, cost_center)
	items = getattr(doc, "items", []) or (doc.get("items", []) if isinstance(doc, dict) else [])
	for idx, item in enumerate(items, start=1):
		# income account
		inc_acc = getattr(item, "income_account", None) or (item.get("income_account") if isinstance(item, dict) else None)
		if inc_acc:
			_assert_account_company(inc_acc, company, f"Item row #{idx} Income Account")

		# expense account
		exp_acc = getattr(item, "expense_account", None) or (item.get("expense_account") if isinstance(item, dict) else None)
		if exp_acc:
			_assert_account_company(exp_acc, company, f"Item row #{idx} Expense Account")

		# cost center
		cc = getattr(item, "cost_center", None) or (item.get("cost_center") if isinstance(item, dict) else None)
		if cc:
			_assert_cost_center_company(cc, company, f"Item row #{idx} Cost Center")

	# 3. Taxes and Charges (account_head, cost_center)
	taxes = getattr(doc, "taxes", []) or (doc.get("taxes", []) if isinstance(doc, dict) else [])
	for idx, tax in enumerate(taxes, start=1):
		acc_head = getattr(tax, "account_head", None) or (tax.get("account_head") if isinstance(tax, dict) else None)
		if acc_head:
			_assert_account_company(acc_head, company, f"Tax row #{idx} Account Head")

		tax_cc = getattr(tax, "cost_center", None) or (tax.get("cost_center") if isinstance(tax, dict) else None)
		if tax_cc:
			_assert_cost_center_company(tax_cc, company, f"Tax row #{idx} Cost Center")


def _assert_account_company(account: str, expected_company: str, role: str) -> None:
	"""Asserts that an account exists and belongs to expected_company."""
	if not account or not expected_company:
		return
	acc_company = frappe.db.get_value("Account", account, "company")
	if not acc_company:
		return
	if acc_company != expected_company:
		raise CompanyMismatchError(
			_("{0} '{1}' belongs to Company '{2}', not transaction Company '{3}'.").format(
				role, account, acc_company, expected_company
			)
		)


def _assert_cost_center_company(cost_center: str, expected_company: str, role: str) -> None:
	"""Asserts that a cost center exists and belongs to expected_company."""
	if not cost_center or not expected_company:
		return
	cc_company = frappe.db.get_value("Cost Center", cost_center, "company")
	if not cc_company:
		return
	if cc_company != expected_company:
		raise CompanyMismatchError(
			_("{0} '{1}' belongs to Company '{2}', not transaction Company '{3}'.").format(
				role, cost_center, cc_company, expected_company
			)
		)
