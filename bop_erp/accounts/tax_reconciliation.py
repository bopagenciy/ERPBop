# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, Optional, Union
import frappe
from frappe import _
from frappe.utils import flt

from bop_erp.accounts.exceptions import MaterialTaxMismatchError

# Currencies that traditionally operate without decimal sub-units (0 decimals)
ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW", "CLP", "HUF", "VND", "PYG", "ISK"}

# Currencies that operate with 3 decimal sub-units
THREE_DECIMAL_CURRENCIES = {"BHD", "KWD", "OMR", "TND", "JOD"}


def get_currency_tax_tolerance(currency: Optional[str] = None) -> float:
	"""
	Determines the permissible tax reconciliation tolerance based on the
	currency precision and standard decimal sub-units.

	- Zero-decimal currencies (JPY, KRW, etc.): tolerance = 1.0
	- Three-decimal currencies (BHD, KWD, etc.): tolerance = 0.005
	- Standard two-decimal currencies (USD, COP, EUR, GBP, etc.): tolerance = 0.02
	"""
	curr = (currency or "").upper().strip()
	if not curr:
		return 0.02

	if curr in ZERO_DECIMAL_CURRENCIES:
		return 1.0

	if curr in THREE_DECIMAL_CURRENCIES:
		return 0.005

	# Standard 2 decimal currencies
	return 0.02


def reconcile_external_taxes(
	doc: Any,
	external_tax_amount: Union[float, int, str],
	currency: Optional[str] = None,
	tolerance: Optional[float] = None,
) -> Dict[str, Any]:
	"""
	Reconciles native ERPNext calculated taxes against external channel order tax totals.

	Policy (Section C):
	External tax information is evidence to RECONCILE, not authority to manually write GL.
	Native Sales Invoice must compute accounting through ERPNext.
	Material mismatch halts or routes to REVIEW_REQUIRED without silent mutation.

	Tolerance is currency-aware.
	"""
	doc_currency = currency or getattr(doc, "currency", None) or (doc.get("currency") if isinstance(doc, dict) else None) or "USD"
	tol = tolerance if tolerance is not None else get_currency_tax_tolerance(doc_currency)

	taxes = getattr(doc, "taxes", []) or (doc.get("taxes", []) if isinstance(doc, dict) else [])
	native_tax_amount = sum(flt(t.tax_amount if not isinstance(t, dict) else t.get("tax_amount")) for t in taxes)

	ext_tax = flt(external_tax_amount)
	tax_delta = abs(flt(native_tax_amount) - ext_tax)

	# Currency-aware tolerance comparison
	if tax_delta > (tol + 1e-6):
		doc_name = getattr(doc, "name", None) or (doc.get("name") if isinstance(doc, dict) else "Draft")
		raise MaterialTaxMismatchError(
			_(
				"Material Tax Mismatch on document '{0}': Native tax ({1:.2f}) differs from "
				"external tax evidence ({2:.2f}) by {3:.2f}, exceeding currency '{4}' tolerance ({5}). "
				"Review required — silent tax mutation is prohibited."
			).format(doc_name, native_tax_amount, ext_tax, tax_delta, doc_currency, tol)
		)

	return {
		"status": "RECONCILED",
		"currency": doc_currency,
		"native_tax_amount": native_tax_amount,
		"external_tax_amount": ext_tax,
		"tax_delta": round(tax_delta, 4),
		"tolerance": tol,
		"reconciled": True,
	}
