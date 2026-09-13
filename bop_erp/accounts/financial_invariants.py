# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional, Union
import frappe
from frappe import _
from frappe.utils import flt


def check_sales_invoice_invariants(sales_invoice: Union[str, Any]) -> Dict[str, Any]:
	"""
	Diagnostic-only accounting integrity checker for Sales Invoice.
	Strictly read-only; NEVER alters or repairs accounting records.
	"""
	if isinstance(sales_invoice, str):
		if not frappe.db.exists("Sales Invoice", sales_invoice):
			return {"valid": False, "violations": [f"Sales Invoice '{sales_invoice}' does not exist."], "details": {}}
		si_doc = frappe.get_doc("Sales Invoice", sales_invoice)
	else:
		si_doc = sales_invoice

	violations: List[str] = []
	details: Dict[str, Any] = {
		"invoice": si_doc.name,
		"docstatus": si_doc.docstatus,
		"company": si_doc.company,
		"customer": si_doc.customer,
		"grand_total": flt(si_doc.grand_total),
		"outstanding_amount": flt(si_doc.outstanding_amount),
	}

	gl_entries = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Sales Invoice", "voucher_no": si_doc.name},
		fields=["name", "account", "party_type", "party", "debit", "credit", "is_cancelled", "company"],
	)
	details["gl_entry_count"] = len(gl_entries)

	if si_doc.docstatus == 0:
		# Draft: Should have zero GL entries
		if gl_entries:
			violations.append(f"Draft Sales Invoice has {len(gl_entries)} premature GL entries.")

	elif si_doc.docstatus == 1:
		# Submitted: Must have balancing GL entries and valid AR liability/receivable
		if not gl_entries:
			violations.append("Submitted Sales Invoice has 0 GL entries.")
		else:
			active_gl = [g for g in gl_entries if not g.is_cancelled]
			total_debit = sum(flt(g.debit) for g in active_gl)
			total_credit = sum(flt(g.credit) for g in active_gl)
			details["total_debit"] = total_debit
			details["total_credit"] = total_credit

			if abs(total_debit - total_credit) > 0.01:
				violations.append(f"GL Entry imbalance: Total Debit ({total_debit}) != Total Credit ({total_credit}).")

			# Company isolation in GL
			for g in active_gl:
				if g.company != si_doc.company:
					violations.append(f"GL Entry '{g.name}' company '{g.company}' != invoice company '{si_doc.company}'.")

			# Receivable account verification
			receivable_gl = [
				g for g in active_gl
				if g.party_type == "Customer" and g.party == si_doc.customer
			]
			if not receivable_gl:
				violations.append(f"No active Receivable GL Entry found for Customer '{si_doc.customer}'.")

		# Outstanding invariant
		if flt(si_doc.outstanding_amount) < -0.01:
			violations.append(f"Negative outstanding amount ({si_doc.outstanding_amount}) on Sales Invoice.")
		if flt(si_doc.outstanding_amount) > flt(si_doc.grand_total) + 0.01 and not si_doc.is_return:
			violations.append(f"Outstanding amount ({si_doc.outstanding_amount}) exceeds grand total ({si_doc.grand_total}).")

	elif si_doc.docstatus == 2:
		# Cancelled: All GL entries must be cancelled or net zero
		active_gl = [g for g in gl_entries if not g.is_cancelled]
		if active_gl:
			net_sum = sum(flt(g.debit) - flt(g.credit) for g in active_gl)
			if abs(net_sum) > 0.01:
				violations.append(f"Cancelled Sales Invoice has uncancelled active GL entries with net balance {net_sum}.")

	return {
		"valid": len(violations) == 0,
		"violations": violations,
		"details": details,
	}


def check_purchase_invoice_invariants(purchase_invoice: Union[str, Any]) -> Dict[str, Any]:
	"""
	Diagnostic-only accounting integrity checker for Purchase Invoice.
	Strictly read-only; NEVER alters or repairs accounting records.
	"""
	if isinstance(purchase_invoice, str):
		if not frappe.db.exists("Purchase Invoice", purchase_invoice):
			return {"valid": False, "violations": [f"Purchase Invoice '{purchase_invoice}' does not exist."], "details": {}}
		pi_doc = frappe.get_doc("Purchase Invoice", purchase_invoice)
	else:
		pi_doc = purchase_invoice

	violations: List[str] = []
	details: Dict[str, Any] = {
		"invoice": pi_doc.name,
		"docstatus": pi_doc.docstatus,
		"company": pi_doc.company,
		"supplier": pi_doc.supplier,
		"grand_total": flt(pi_doc.grand_total),
		"outstanding_amount": flt(pi_doc.outstanding_amount),
	}

	gl_entries = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Purchase Invoice", "voucher_no": pi_doc.name},
		fields=["name", "account", "party_type", "party", "debit", "credit", "is_cancelled", "company"],
	)
	details["gl_entry_count"] = len(gl_entries)

	if pi_doc.docstatus == 0:
		if gl_entries:
			violations.append(f"Draft Purchase Invoice has {len(gl_entries)} premature GL entries.")

	elif pi_doc.docstatus == 1:
		if not gl_entries:
			violations.append("Submitted Purchase Invoice has 0 GL entries.")
		else:
			active_gl = [g for g in gl_entries if not g.is_cancelled]
			total_debit = sum(flt(g.debit) for g in active_gl)
			total_credit = sum(flt(g.credit) for g in active_gl)
			details["total_debit"] = total_debit
			details["total_credit"] = total_credit

			if abs(total_debit - total_credit) > 0.01:
				violations.append(f"GL Entry imbalance: Total Debit ({total_debit}) != Total Credit ({total_credit}).")

			for g in active_gl:
				if g.company != pi_doc.company:
					violations.append(f"GL Entry '{g.name}' company '{g.company}' != invoice company '{pi_doc.company}'.")

			# Payable account verification
			payable_gl = [
				g for g in active_gl
				if g.party_type == "Supplier" and g.party == pi_doc.supplier
			]
			if not payable_gl:
				violations.append(f"No active Payable GL Entry found for Supplier '{pi_doc.supplier}'.")

		if flt(pi_doc.outstanding_amount) < -0.01:
			violations.append(f"Negative outstanding amount ({pi_doc.outstanding_amount}) on Purchase Invoice.")
		if flt(pi_doc.outstanding_amount) > flt(pi_doc.grand_total) + 0.01 and not pi_doc.is_return:
			violations.append(f"Outstanding amount ({pi_doc.outstanding_amount}) exceeds grand total ({pi_doc.grand_total}).")

	elif pi_doc.docstatus == 2:
		active_gl = [g for g in gl_entries if not g.is_cancelled]
		if active_gl:
			net_sum = sum(flt(g.debit) - flt(g.credit) for g in active_gl)
			if abs(net_sum) > 0.01:
				violations.append(f"Cancelled Purchase Invoice has uncancelled active GL entries with net balance {net_sum}.")

	return {
		"valid": len(violations) == 0,
		"violations": violations,
		"details": details,
	}


def check_payment_entry_invariants(payment_entry: Union[str, Any]) -> Dict[str, Any]:
	"""
	Diagnostic-only accounting integrity checker for Payment Entry.
	Strictly read-only; NEVER alters or repairs accounting records.
	"""
	if isinstance(payment_entry, str):
		if not frappe.db.exists("Payment Entry", payment_entry):
			return {"valid": False, "violations": [f"Payment Entry '{payment_entry}' does not exist."], "details": {}}
		pe_doc = frappe.get_doc("Payment Entry", payment_entry)
	else:
		pe_doc = payment_entry

	violations: List[str] = []
	details: Dict[str, Any] = {
		"payment_entry": getattr(pe_doc, "name", "PE"),
		"payment_type": getattr(pe_doc, "payment_type", ""),
		"docstatus": getattr(pe_doc, "docstatus", 0),
		"company": getattr(pe_doc, "company", ""),
		"party_type": getattr(pe_doc, "party_type", ""),
		"party": getattr(pe_doc, "party", ""),
		"paid_amount": flt(getattr(pe_doc, "paid_amount", 0.0)),
		"received_amount": flt(getattr(pe_doc, "received_amount", 0.0)),
	}

	gl_entries = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Payment Entry", "voucher_no": pe_doc.name},
		fields=["name", "account", "party_type", "party", "debit", "credit", "is_cancelled", "company"],
	)
	details["gl_entry_count"] = len(gl_entries)

	if pe_doc.docstatus == 0:
		if gl_entries:
			violations.append(f"Draft Payment Entry has {len(gl_entries)} premature GL entries.")

	elif pe_doc.docstatus == 1:
		if not gl_entries:
			violations.append("Submitted Payment Entry has 0 GL entries.")
		else:
			active_gl = [g for g in gl_entries if not g.is_cancelled]
			total_debit = sum(flt(g.debit) for g in active_gl)
			total_credit = sum(flt(g.credit) for g in active_gl)
			details["total_debit"] = total_debit
			details["total_credit"] = total_credit

			if abs(total_debit - total_credit) > 0.01:
				violations.append(f"GL Entry imbalance on Payment Entry: Debit ({total_debit}) != Credit ({total_credit}).")

			for g in active_gl:
				if g.company != pe_doc.company:
					violations.append(f"GL Entry '{g.name}' company '{g.company}' != Payment Entry company '{pe_doc.company}'.")

		# References check: verify linked vouchers outstanding reduced
		for ref in getattr(pe_doc, "references", []):
			ref_dt = ref.reference_doctype
			ref_dn = ref.reference_name
			if frappe.db.exists(ref_dt, ref_dn):
				ref_doc = frappe.get_doc(ref_dt, ref_dn)
				# Outstanding must not exceed grand total
				if flt(ref_doc.outstanding_amount) > flt(ref_doc.grand_total) + 0.01:
					violations.append(
						f"Linked {ref_dt} '{ref_dn}' outstanding ({ref_doc.outstanding_amount}) exceeds grand total ({ref_doc.grand_total})."
					)

	elif pe_doc.docstatus == 2:
		active_gl = [g for g in gl_entries if not g.is_cancelled]
		if active_gl:
			net_sum = sum(flt(g.debit) - flt(g.credit) for g in active_gl)
			if abs(net_sum) > 0.01:
				violations.append(f"Cancelled Payment Entry has uncancelled active GL entries with net balance {net_sum}.")

	return {
		"valid": len(violations) == 0,
		"violations": violations,
		"details": details,
	}
