# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe import _
from bop_erp.constants import TransactionOrigin

def validate_sales_order_attribution(doc, method=None):
	if doc.transaction_origin and doc.transaction_origin not in TransactionOrigin.ALL:
		frappe.throw(
			_("Invalid Transaction Origin '{0}'. Allowed values: {1}").format(
				doc.transaction_origin, ", ".join(TransactionOrigin.ALL)
			)
		)

	if doc.sales_channel:
		validate_channel_company(doc.sales_channel, getattr(doc, "company", None))
		if doc.is_new():
			is_active = frappe.db.get_value("Sales Channel", doc.sales_channel, "active")
			if not is_active:
				frappe.throw(_("Selected Sales Channel '{0}' is inactive.").format(doc.sales_channel))

	validate_submitted_immutability(doc)

def validate_channel_company(sales_channel, company):
	if not sales_channel or not company:
		return
	channel_company = frappe.db.get_value("Sales Channel", sales_channel, "company")
	if channel_company and channel_company != company:
		frappe.throw(
			_("Sales Channel '{0}' belongs to Company '{1}', which does not match Document Company '{2}'.").format(
				sales_channel, channel_company, company
			)
		)

def validate_submitted_immutability(doc):
	if doc.is_new() or doc.docstatus != 1:
		return

	old_doc = doc.get_doc_before_save()
	if not old_doc:
		return

	if hasattr(doc, "sales_channel") and old_doc.get("sales_channel") != doc.get("sales_channel"):
		if frappe.session.user != "Administrator":
			frappe.throw(_("Sales Channel cannot be modified on a submitted document."))

	if hasattr(doc, "transaction_origin") and old_doc.get("transaction_origin") != doc.get("transaction_origin"):
		if frappe.session.user != "Administrator":
			frappe.throw(_("Transaction Origin cannot be modified on a submitted document."))

def propagate_attribution_to_pick_list(doc, method=None):
	if getattr(doc, "sales_channel", None):
		return

	sales_order_id = None
	if hasattr(doc, "locations"):
		for loc in doc.locations:
			if loc.sales_order:
				sales_order_id = loc.sales_order
				break

	if sales_order_id:
		so = frappe.db.get_value(
			"Sales Order",
			sales_order_id,
			["sales_channel", "transaction_origin", "external_order_id"],
			as_dict=True,
		)
		if so:
			if not doc.sales_channel and so.sales_channel:
				doc.sales_channel = so.sales_channel
			if not doc.transaction_origin and so.transaction_origin:
				doc.transaction_origin = so.transaction_origin
			if not doc.external_order_id and so.external_order_id:
				doc.external_order_id = so.external_order_id

def propagate_attribution_to_delivery_note(doc, method=None):
	if getattr(doc, "is_return", 0) and getattr(doc, "return_against", None):
		orig = frappe.db.get_value(
			"Delivery Note",
			doc.return_against,
			["sales_channel", "transaction_origin", "external_order_id"],
			as_dict=True,
		)
		if orig:
			doc.sales_channel = orig.sales_channel
			doc.transaction_origin = orig.transaction_origin
			doc.external_order_id = orig.external_order_id
		return

	if getattr(doc, "sales_channel", None):
		return

	sales_order_id = None
	if hasattr(doc, "items"):
		for item in doc.items:
			if getattr(item, "against_sales_order", None):
				sales_order_id = item.against_sales_order
				break

	if sales_order_id:
		so = frappe.db.get_value(
			"Sales Order",
			sales_order_id,
			["sales_channel", "transaction_origin", "external_order_id"],
			as_dict=True,
		)
		if so:
			if not doc.sales_channel and so.sales_channel:
				doc.sales_channel = so.sales_channel
			if not doc.transaction_origin and so.transaction_origin:
				doc.transaction_origin = so.transaction_origin
			if not doc.external_order_id and so.external_order_id:
				doc.external_order_id = so.external_order_id

def propagate_attribution_to_shipment(doc, method=None):
	if getattr(doc, "sales_channel", None):
		return

	delivery_note_id = None
	if hasattr(doc, "delivery_notes"):
		for dn in doc.delivery_notes:
			if dn.delivery_note:
				delivery_note_id = dn.delivery_note
				break

	if delivery_note_id:
		dn = frappe.db.get_value(
			"Delivery Note",
			delivery_note_id,
			["sales_channel", "transaction_origin", "external_order_id"],
			as_dict=True,
		)
		if dn:
			if not doc.sales_channel and dn.sales_channel:
				doc.sales_channel = dn.sales_channel
			if not doc.transaction_origin and dn.transaction_origin:
				doc.transaction_origin = dn.transaction_origin
			if not doc.external_order_id and dn.external_order_id:
				doc.external_order_id = dn.external_order_id

def propagate_attribution_to_sales_invoice(doc, method=None):
	if getattr(doc, "is_return", 0) and getattr(doc, "return_against", None):
		orig = frappe.db.get_value(
			"Sales Invoice",
			doc.return_against,
			["sales_channel", "transaction_origin", "external_order_id"],
			as_dict=True,
		)
		if orig:
			doc.sales_channel = orig.sales_channel
			doc.transaction_origin = orig.transaction_origin
			doc.external_order_id = orig.external_order_id
		return

	if getattr(doc, "sales_channel", None):
		return

	sales_order_id = None
	delivery_note_id = None
	if hasattr(doc, "items"):
		for item in doc.items:
			if getattr(item, "sales_order", None):
				sales_order_id = item.sales_order
				break
			if getattr(item, "delivery_note", None):
				delivery_note_id = item.delivery_note
				break

	if sales_order_id:
		so = frappe.db.get_value(
			"Sales Order",
			sales_order_id,
			["sales_channel", "transaction_origin", "external_order_id"],
			as_dict=True,
		)
		if so:
			if not doc.sales_channel and so.sales_channel:
				doc.sales_channel = so.sales_channel
			if not doc.transaction_origin and so.transaction_origin:
				doc.transaction_origin = so.transaction_origin
			if not doc.external_order_id and so.external_order_id:
				doc.external_order_id = so.external_order_id
	elif delivery_note_id:
		dn = frappe.db.get_value(
			"Delivery Note",
			delivery_note_id,
			["sales_channel", "transaction_origin", "external_order_id"],
			as_dict=True,
		)
		if dn:
			if not doc.sales_channel and dn.sales_channel:
				doc.sales_channel = dn.sales_channel
			if not doc.transaction_origin and dn.transaction_origin:
				doc.transaction_origin = dn.transaction_origin
			if not doc.external_order_id and dn.external_order_id:
				doc.external_order_id = dn.external_order_id

def propagate_attribution_to_payment_entry(doc, method=None):
	if getattr(doc, "sales_channel", None):
		return

	if hasattr(doc, "references"):
		for ref in doc.references:
			if ref.reference_doctype in ("Sales Invoice", "Sales Order") and ref.reference_name:
				ref_channel = frappe.db.get_value(ref.reference_doctype, ref.reference_name, "sales_channel")
				ref_origin = frappe.db.get_value(ref.reference_doctype, ref.reference_name, "transaction_origin")
				if ref_channel:
					doc.sales_channel = ref_channel
					doc.transaction_origin = ref_origin
					break

def validate_transaction_attribution(doc, method=None):
	validate_submitted_immutability(doc)

	if getattr(doc, "is_return", 0) and getattr(doc, "return_against", None):
		ref_doctype = doc.doctype
		orig_channel = frappe.db.get_value(ref_doctype, doc.return_against, "sales_channel")
		if orig_channel and doc.get("sales_channel") and doc.sales_channel != orig_channel:
			frappe.throw(
				_("Return / Credit Note channel '{0}' must match the original {1} channel '{2}'.").format(
					doc.sales_channel, ref_doctype, orig_channel
				)
			)
