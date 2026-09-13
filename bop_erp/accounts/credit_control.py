# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, Optional
import frappe
from frappe import _
from frappe.utils import flt

from bop_erp.accounts.exceptions import CreditLimitExceededError


def get_effective_credit_limit(customer: str, company: str) -> float:
	"""
	Resolves the effective credit limit for a customer in a specific company,
	following native ERPNext hierarchy:
	1. Customer-specific credit limit for company
	2. Customer Group credit limit for company
	3. Company-level default credit limit
	"""
	from erpnext.selling.doctype.customer.customer import get_credit_limit
	return flt(get_credit_limit(customer, company))


def get_customer_current_exposure(
	customer: str,
	company: str,
	ignore_outstanding_sales_orders: bool = False,
) -> float:
	"""
	Resolves the current financial exposure (outstanding AR + unbilled SOs)
	using ERPNext native accounting calculators.
	"""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding
	return flt(get_customer_outstanding(customer, company, ignore_outstanding_sales_orders))


def validate_customer_credit_control(
	customer: str,
	company: str,
	extra_amount: float = 0.0,
	allow_review: bool = False,
	ignore_outstanding_sales_orders: bool = False,
) -> Dict[str, Any]:
	"""
	Bop credit control validator.

	Enforces:
	- If customer has no credit limit configured (or limit <= 0): credit check passes.
	- If customer has credit limit > 0:
	  total_exposure = current_outstanding + extra_amount
	  If total_exposure > credit_limit:
	    - If allow_review is True: returns status 'REVIEW_REQUIRED', allowed=False
	    - Otherwise: raises CreditLimitExceededError
	- If within limit: returns status 'APPROVED', allowed=True
	"""
	if not customer or not company:
		return {
			"allowed": True,
			"status": "APPROVED",
			"credit_limit": 0.0,
			"current_outstanding": 0.0,
			"total_exposure": flt(extra_amount),
			"bypass": True,
		}

	credit_limit = get_effective_credit_limit(customer, company)
	if credit_limit <= 0:
		return {
			"allowed": True,
			"status": "APPROVED",
			"credit_limit": 0.0,
			"current_outstanding": 0.0,
			"total_exposure": flt(extra_amount),
			"unlimited": True,
		}

	current_outstanding = get_customer_current_exposure(
		customer, company, ignore_outstanding_sales_orders=ignore_outstanding_sales_orders
	)
	total_exposure = current_outstanding + flt(extra_amount)

	if total_exposure > (credit_limit + 1e-6):
		message = _(
			"Credit limit exceeded for Customer '{0}' in Company '{1}': "
			"Total exposure {2:.2f} exceeds credit limit {3:.2f}."
		).format(customer, company, total_exposure, credit_limit)

		if allow_review:
			return {
				"allowed": False,
				"status": "REVIEW_REQUIRED",
				"credit_limit": credit_limit,
				"current_outstanding": current_outstanding,
				"total_exposure": total_exposure,
				"reason": message,
			}
		raise CreditLimitExceededError(message)

	return {
		"allowed": True,
		"status": "APPROVED",
		"credit_limit": credit_limit,
		"current_outstanding": current_outstanding,
		"total_exposure": total_exposure,
	}
