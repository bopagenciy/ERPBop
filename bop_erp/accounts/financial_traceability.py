# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional, Set
import frappe
from frappe import _
from frappe.utils import flt

from bop_erp.accounts.exceptions import FinancialTraceabilityError


def _doc_summary(doc: Any) -> Dict[str, Any]:
	"""Formats a standard document metadata dictionary without duplicating GL data."""
	docstatus = cint_val(getattr(doc, "docstatus", 0))
	status_label = {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(docstatus, "Unknown")
	
	posting_date = getattr(doc, "posting_date", None) or getattr(doc, "transaction_date", None)
	
	doctype = getattr(doc, "doctype", "")
	currency = getattr(doc, "currency", None)
	if not currency and doctype == "Payment Entry":
		currency = getattr(doc, "paid_from_account_currency", None) or getattr(doc, "paid_to_account_currency", "")

	grand_total = flt(getattr(doc, "grand_total", 0.0))
	if not grand_total and doctype == "Payment Entry":
		grand_total = flt(getattr(doc, "paid_amount", 0.0) or getattr(doc, "received_amount", 0.0))

	return {
		"doctype": doctype,
		"name": getattr(doc, "name", ""),
		"docstatus": docstatus,
		"docstatus_label": status_label,
		"company": getattr(doc, "company", ""),
		"currency": currency or "",
		"grand_total": grand_total,
		"outstanding_amount": flt(getattr(doc, "outstanding_amount", 0.0)),
		"posting_date": str(posting_date) if posting_date else None,
		"linked_accounting_docs": [],
	}


def cint_val(val: Any) -> int:
	try:
		return int(val)
	except (ValueError, TypeError):
		return 0


def get_sales_financial_traceability(
	sales_order: Optional[str] = None,
	delivery_note: Optional[str] = None,
	sales_invoice: Optional[str] = None,
	payment_entry: Optional[str] = None,
) -> Dict[str, Any]:
	"""
	Provider-neutral financial traceability view for sales flows:
	Sales Order -> Delivery Note -> Sales Invoice -> Payment Entry -> Credit Note / Refund

	Returns:
	- document names, docstatus, company, currency, grand_total, outstanding, posting_date
	- linked accounting documents
	- 0 shadow tables, 0 duplicate GL entries
	"""
	so_names: Set[str] = set()
	dn_names: Set[str] = set()
	si_names: Set[str] = set()
	pe_names: Set[str] = set()
	cn_names: Set[str] = set()

	# Seed discovery from provided document
	if sales_order:
		so_names.add(sales_order)
	if delivery_note:
		dn_names.add(delivery_note)
	if sales_invoice:
		si_names.add(sales_invoice)
	if payment_entry:
		pe_names.add(payment_entry)

	# 1. Expand from Payment Entry
	for pe in list(pe_names):
		refs = frappe.get_all(
			"Payment Entry Reference",
			filters={"parent": pe, "reference_doctype": "Sales Invoice"},
			pluck="reference_name",
		)
		si_names.update(refs)

	# 2. Expand from Sales Invoice
	for si in list(si_names):
		if frappe.db.exists("Sales Invoice", si):
			si_doc = frappe.get_cached_doc("Sales Invoice", si)
			if si_doc.is_return and si_doc.return_against:
				cn_names.add(si)
				si_names.add(si_doc.return_against)
			for item in si_doc.items:
				if item.sales_order:
					so_names.add(item.sales_order)
				if item.delivery_note:
					dn_names.add(item.delivery_note)

	# 3. Expand from Delivery Note
	for dn in list(dn_names):
		if frappe.db.exists("Delivery Note", dn):
			dn_doc = frappe.get_cached_doc("Delivery Note", dn)
			for item in dn_doc.items:
				if item.against_sales_order:
					so_names.add(item.against_sales_order)
			# Find linked SIs
			linked_sis = frappe.get_all(
				"Sales Invoice Item",
				filters={"delivery_note": dn},
				pluck="parent",
			)
			si_names.update(linked_sis)

	# 4. Expand from Sales Order
	for so in list(so_names):
		linked_dns = frappe.get_all(
			"Delivery Note Item",
			filters={"against_sales_order": so},
			pluck="parent",
		)
		dn_names.update(linked_dns)
		linked_sis = frappe.get_all(
			"Sales Invoice Item",
			filters={"sales_order": so},
			pluck="parent",
		)
		si_names.update(linked_sis)

	# 5. For all SIs, find linked Payment Entries and Credit Notes
	for si in list(si_names):
		# PEs
		linked_pes = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Sales Invoice", "reference_name": si},
			pluck="parent",
		)
		pe_names.update(linked_pes)

		# Credit Notes (Sales Invoice with is_return=1 and return_against=si)
		linked_cns = frappe.get_all(
			"Sales Invoice",
			filters={"is_return": 1, "return_against": si},
			pluck="name",
		)
		cn_names.update(linked_cns)

	# Build structured summaries
	so_records = [_doc_summary(frappe.get_doc("Sales Order", n)) for n in sorted(so_names) if frappe.db.exists("Sales Order", n)]
	dn_records = [_doc_summary(frappe.get_doc("Delivery Note", n)) for n in sorted(dn_names) if frappe.db.exists("Delivery Note", n)]
	si_records = [_doc_summary(frappe.get_doc("Sales Invoice", n)) for n in sorted(si_names) if frappe.db.exists("Sales Invoice", n) and n not in cn_names]
	cn_records = [_doc_summary(frappe.get_doc("Sales Invoice", n)) for n in sorted(cn_names) if frappe.db.exists("Sales Invoice", n)]
	pe_records = [_doc_summary(frappe.get_doc("Payment Entry", n)) for n in sorted(pe_names) if frappe.db.exists("Payment Entry", n)]

	# Annotate linked accounting docs on each SI
	for si_rec in si_records:
		si_name = si_rec["name"]
		linked = []
		for pe_rec in pe_records:
			pe_doc = frappe.get_doc("Payment Entry", pe_rec["name"])
			if any(r.reference_name == si_name for r in getattr(pe_doc, "references", [])):
				linked.append(f"Payment Entry: {pe_rec['name']}")
		for cn_rec in cn_records:
			cn_doc = frappe.get_doc("Sales Invoice", cn_rec["name"])
			if getattr(cn_doc, "return_against", None) == si_name:
				linked.append(f"Credit Note: {cn_rec['name']}")
		si_rec["linked_accounting_docs"] = linked

	primary_company = (
		(si_records and si_records[0]["company"])
		or (so_records and so_records[0]["company"])
		or (dn_records and dn_records[0]["company"])
		or ""
	)
	primary_currency = (
		(si_records and si_records[0]["currency"])
		or (so_records and so_records[0]["currency"])
		or ""
	)

	return {
		"flow": "SALE",
		"company": primary_company,
		"currency": primary_currency,
		"sales_orders": so_records,
		"delivery_notes": dn_records,
		"sales_invoices": si_records,
		"payment_entries": pe_records,
		"credit_notes": cn_records,
		"total_billed": sum(r["grand_total"] for r in si_records if r["docstatus"] == 1),
		"total_paid": sum(r["grand_total"] for r in pe_records if r["docstatus"] == 1),
		"total_refunded": sum(r["grand_total"] for r in cn_records if r["docstatus"] == 1),
		"total_outstanding": sum(r["outstanding_amount"] for r in si_records if r["docstatus"] == 1),
	}


def get_purchasing_financial_traceability(
	purchase_order: Optional[str] = None,
	purchase_receipt: Optional[str] = None,
	purchase_invoice: Optional[str] = None,
	payment_entry: Optional[str] = None,
) -> Dict[str, Any]:
	"""
	Provider-neutral financial traceability view for purchasing flows:
	Purchase Order -> Purchase Receipt -> Purchase Invoice -> Payment Entry

	Returns:
	- document names, docstatus, company, currency, grand_total, outstanding, posting_date
	- linked accounting documents
	- 0 shadow tables, 0 duplicate GL entries
	"""
	po_names: Set[str] = set()
	pr_names: Set[str] = set()
	pi_names: Set[str] = set()
	pe_names: Set[str] = set()
	dn_names: Set[str] = set()  # Debit Notes (PI return)

	if purchase_order:
		po_names.add(purchase_order)
	if purchase_receipt:
		pr_names.add(purchase_receipt)
	if purchase_invoice:
		pi_names.add(purchase_invoice)
	if payment_entry:
		pe_names.add(payment_entry)

	# 1. Expand from Payment Entry
	for pe in list(pe_names):
		refs = frappe.get_all(
			"Payment Entry Reference",
			filters={"parent": pe, "reference_doctype": "Purchase Invoice"},
			pluck="reference_name",
		)
		pi_names.update(refs)

	# 2. Expand from Purchase Invoice
	for pi in list(pi_names):
		if frappe.db.exists("Purchase Invoice", pi):
			pi_doc = frappe.get_cached_doc("Purchase Invoice", pi)
			if pi_doc.is_return and pi_doc.return_against:
				dn_names.add(pi)
				pi_names.add(pi_doc.return_against)
			for item in pi_doc.items:
				if item.purchase_order:
					po_names.add(item.purchase_order)
				if item.purchase_receipt:
					pr_names.add(item.purchase_receipt)

	# 3. Expand from Purchase Receipt
	for pr in list(pr_names):
		if frappe.db.exists("Purchase Receipt", pr):
			pr_doc = frappe.get_cached_doc("Purchase Receipt", pr)
			for item in pr_doc.items:
				if item.purchase_order:
					po_names.add(item.purchase_order)
			linked_pis = frappe.get_all(
				"Purchase Invoice Item",
				filters={"purchase_receipt": pr},
				pluck="parent",
			)
			pi_names.update(linked_pis)

	# 4. Expand from Purchase Order
	for po in list(po_names):
		linked_prs = frappe.get_all(
			"Purchase Receipt Item",
			filters={"purchase_order": po},
			pluck="parent",
		)
		pr_names.update(linked_prs)
		linked_pis = frappe.get_all(
			"Purchase Invoice Item",
			filters={"purchase_order": po},
			pluck="parent",
		)
		pi_names.update(linked_pis)

	# 5. Find linked PEs and Debit Notes for PIs
	for pi in list(pi_names):
		linked_pes = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Purchase Invoice", "reference_name": pi},
			pluck="parent",
		)
		pe_names.update(linked_pes)
		linked_dns = frappe.get_all(
			"Purchase Invoice",
			filters={"is_return": 1, "return_against": pi},
			pluck="name",
		)
		dn_names.update(linked_dns)

	po_records = [_doc_summary(frappe.get_doc("Purchase Order", n)) for n in sorted(po_names) if frappe.db.exists("Purchase Order", n)]
	pr_records = [_doc_summary(frappe.get_doc("Purchase Receipt", n)) for n in sorted(pr_names) if frappe.db.exists("Purchase Receipt", n)]
	pi_records = [_doc_summary(frappe.get_doc("Purchase Invoice", n)) for n in sorted(pi_names) if frappe.db.exists("Purchase Invoice", n) and n not in dn_names]
	dn_records_list = [_doc_summary(frappe.get_doc("Purchase Invoice", n)) for n in sorted(dn_names) if frappe.db.exists("Purchase Invoice", n)]
	pe_records = [_doc_summary(frappe.get_doc("Payment Entry", n)) for n in sorted(pe_names) if frappe.db.exists("Payment Entry", n)]

	# Annotate linked accounting docs on each PI
	for pi_rec in pi_records:
		pi_name = pi_rec["name"]
		linked = []
		for pe_rec in pe_records:
			pe_doc = frappe.get_doc("Payment Entry", pe_rec["name"])
			if any(r.reference_name == pi_name for r in getattr(pe_doc, "references", [])):
				linked.append(f"Payment Entry: {pe_rec['name']}")
		for dn_rec in dn_records_list:
			dn_doc = frappe.get_doc("Purchase Invoice", dn_rec["name"])
			if getattr(dn_doc, "return_against", None) == pi_name:
				linked.append(f"Debit Note: {dn_rec['name']}")
		pi_rec["linked_accounting_docs"] = linked

	primary_company = (
		(pi_records and pi_records[0]["company"])
		or (po_records and po_records[0]["company"])
		or (pr_records and pr_records[0]["company"])
		or ""
	)
	primary_currency = (
		(pi_records and pi_records[0]["currency"])
		or (po_records and po_records[0]["currency"])
		or ""
	)

	return {
		"flow": "PURCHASE",
		"company": primary_company,
		"currency": primary_currency,
		"purchase_orders": po_records,
		"purchase_receipts": pr_records,
		"purchase_invoices": pi_records,
		"payment_entries": pe_records,
		"debit_notes": dn_records_list,
		"total_billed": sum(r["grand_total"] for r in pi_records if r["docstatus"] == 1),
		"total_paid": sum(r["grand_total"] for r in pe_records if r["docstatus"] == 1),
		"total_returned": sum(r["grand_total"] for r in dn_records_list if r["docstatus"] == 1),
		"total_outstanding": sum(r["outstanding_amount"] for r in pi_records if r["docstatus"] == 1),
	}
