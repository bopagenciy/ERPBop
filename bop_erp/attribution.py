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
	if doc.docstatus != 1 or getattr(doc, "__islocal", False):
		return

	persisted = None
	if getattr(doc, "name", None):
		persisted = frappe.db.get_value(
			doc.doctype, doc.name, ["sales_channel", "transaction_origin"], as_dict=True
		)

	if not persisted and hasattr(doc, "get_doc_before_save"):
		before = doc.get_doc_before_save()
		if before:
			persisted = frappe._dict({
				"sales_channel": getattr(before, "sales_channel", None) if not isinstance(before, dict) else before.get("sales_channel"),
				"transaction_origin": getattr(before, "transaction_origin", None) if not isinstance(before, dict) else before.get("transaction_origin"),
			})

	if not persisted:
		return

	if hasattr(doc, "sales_channel") and doc.get("sales_channel") != persisted.sales_channel:
		frappe.throw(_("Sales Channel cannot be modified on a submitted {0}.").format(doc.doctype))

	if hasattr(doc, "transaction_origin") and doc.get("transaction_origin") != persisted.transaction_origin:
		frappe.throw(_("Transaction Origin cannot be modified on a submitted {0}.").format(doc.doctype))

def propagate_attribution_to_pick_list(doc, method=None):
	channels = set()
	origins = set()
	ext_ids = set()

	locations = doc.get("locations") or []
	for loc in locations:
		so_id = getattr(loc, "sales_order", None) if not isinstance(loc, dict) else loc.get("sales_order")
		if so_id:
			so = frappe.db.get_value(
				"Sales Order", so_id, ["sales_channel", "transaction_origin", "external_order_id"], as_dict=True
			)
			if so:
				if so.sales_channel:
					channels.add(so.sales_channel)
				if so.transaction_origin:
					origins.add(so.transaction_origin)
				if so.external_order_id:
					ext_ids.add(so.external_order_id)

	# Independent resolution for operational aggregation
	if len(channels) == 1:
		doc.sales_channel = next(iter(channels))
	elif len(channels) > 1:
		doc.sales_channel = None

	if len(origins) == 1:
		doc.transaction_origin = next(iter(origins))
	elif len(origins) > 1:
		doc.transaction_origin = None

	if len(ext_ids) == 1:
		doc.external_order_id = next(iter(ext_ids))
	elif len(ext_ids) > 1:
		doc.external_order_id = None

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

	channels = set()
	origins = set()
	ext_ids = set()

	items = doc.get("items") or []
	for item in items:
		so_id = getattr(item, "against_sales_order", None) if not isinstance(item, dict) else item.get("against_sales_order")
		if so_id:
			so = frappe.db.get_value(
				"Sales Order",
				so_id,
				["sales_channel", "transaction_origin", "external_order_id"],
				as_dict=True,
			)
			if so:
				if so.sales_channel:
					channels.add(so.sales_channel)
				if so.transaction_origin:
					origins.add(so.transaction_origin)
				if so.external_order_id:
					ext_ids.add(so.external_order_id)

	# Enforce Commercial Document Homogeneity on Channel
	if len(channels) > 1:
		frappe.throw(
			_(
				"Commercial Homogeneity Violation: Delivery Note combines items from multiple Sales Channels ({0}). "
				"A Delivery Note must belong to a single homogeneous Sales Channel."
			).format(", ".join(sorted(channels)))
		)

	if len(channels) == 1:
		doc.sales_channel = next(iter(channels))

	# Origin resolved independently: do not reject same-channel consolidation with differing origins
	if len(origins) == 1:
		doc.transaction_origin = next(iter(origins))
	elif len(origins) > 1:
		doc.transaction_origin = None

	if len(ext_ids) == 1:
		doc.external_order_id = next(iter(ext_ids))
	elif len(ext_ids) > 1:
		doc.external_order_id = None

def propagate_attribution_to_shipment(doc, method=None):
	channels = set()
	origins = set()
	ext_ids = set()

	delivery_notes = doc.get("delivery_notes") or []
	for dn_row in delivery_notes:
		dn_id = getattr(dn_row, "delivery_note", None) if not isinstance(dn_row, dict) else dn_row.get("delivery_note")
		if dn_id:
			dn = frappe.db.get_value(
				"Delivery Note",
				dn_id,
				["sales_channel", "transaction_origin", "external_order_id"],
				as_dict=True,
			)
			if dn:
				if dn.sales_channel:
					channels.add(dn.sales_channel)
				if dn.transaction_origin:
					origins.add(dn.transaction_origin)
				if dn.external_order_id:
					ext_ids.add(dn.external_order_id)

	if len(channels) > 1:
		frappe.throw(
			_(
				"Commercial Homogeneity Violation: Shipment combines deliveries from multiple Sales Channels ({0}). "
				"A Shipment must belong to a single homogeneous Sales Channel."
			).format(", ".join(sorted(channels)))
		)

	if len(channels) == 1:
		doc.sales_channel = next(iter(channels))

	if len(origins) == 1:
		doc.transaction_origin = next(iter(origins))
	elif len(origins) > 1:
		doc.transaction_origin = None

	if len(ext_ids) == 1:
		doc.external_order_id = next(iter(ext_ids))
	elif len(ext_ids) > 1:
		doc.external_order_id = None

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

	channels = set()
	origins = set()
	ext_ids = set()

	items = doc.get("items") or []
	for item in items:
		so_id = getattr(item, "sales_order", None) if not isinstance(item, dict) else item.get("sales_order")
		dn_id = getattr(item, "delivery_note", None) if not isinstance(item, dict) else item.get("delivery_note")
		if so_id:
			so = frappe.db.get_value(
				"Sales Order",
				so_id,
				["sales_channel", "transaction_origin", "external_order_id"],
				as_dict=True,
			)
			if so:
				if so.sales_channel:
					channels.add(so.sales_channel)
				if so.transaction_origin:
					origins.add(so.transaction_origin)
				if so.external_order_id:
					ext_ids.add(so.external_order_id)
		elif dn_id:
			dn = frappe.db.get_value(
				"Delivery Note",
				dn_id,
				["sales_channel", "transaction_origin", "external_order_id"],
				as_dict=True,
			)
			if dn:
				if dn.sales_channel:
					channels.add(dn.sales_channel)
				if dn.transaction_origin:
					origins.add(dn.transaction_origin)
				if dn.external_order_id:
					ext_ids.add(dn.external_order_id)

	# Enforce Commercial Document Homogeneity on Channel
	if len(channels) > 1:
		frappe.throw(
			_(
				"Commercial Homogeneity Violation: Sales Invoice combines items from multiple Sales Channels ({0}). "
				"A Sales Invoice must belong to a single homogeneous Sales Channel."
			).format(", ".join(sorted(channels)))
		)

	if len(channels) == 1:
		doc.sales_channel = next(iter(channels))

	if len(origins) == 1:
		doc.transaction_origin = next(iter(origins))
	elif len(origins) > 1:
		doc.transaction_origin = None

	if len(ext_ids) == 1:
		doc.external_order_id = next(iter(ext_ids))
	elif len(ext_ids) > 1:
		doc.external_order_id = None

def propagate_attribution_to_payment_entry(doc, method=None):
	channels = set()
	origins = set()

	refs = doc.get("references") or []
	for ref in refs:
		ref_doctype = getattr(ref, "reference_doctype", None) if not isinstance(ref, dict) else ref.get("reference_doctype")
		ref_name = getattr(ref, "reference_name", None) if not isinstance(ref, dict) else ref.get("reference_name")
		if ref_doctype in ("Sales Invoice", "Sales Order") and ref_name:
			ref_channel = frappe.db.get_value(ref_doctype, ref_name, "sales_channel")
			ref_origin = frappe.db.get_value(ref_doctype, ref_name, "transaction_origin")
			if ref_channel:
				channels.add(ref_channel)
			if ref_origin:
				origins.add(ref_origin)

	# Independent attribution resolution:
	if len(channels) == 1:
		doc.sales_channel = next(iter(channels))
	elif len(channels) > 1:
		doc.sales_channel = None

	if len(origins) == 1:
		doc.transaction_origin = next(iter(origins))
	elif len(origins) > 1:
		doc.transaction_origin = None

def get_payment_channel_breakdown(doc):
	"""
	Derive channel allocation breakdown from Payment Entry child references.
	Returns a dictionary mapping sales_channel to allocated amount:
	e.g. {'TID': 100.0, 'BAMAL': 50.0}
	Note: This represents cash collections / payments, not revenue (authority is Sales Invoice).
	"""
	if isinstance(doc, str):
		doc = frappe.get_doc("Payment Entry", doc)
	breakdown = {}
	refs = doc.get("references") or []
	for ref in refs:
		ref_doctype = getattr(ref, "reference_doctype", None) if not isinstance(ref, dict) else ref.get("reference_doctype")
		ref_name = getattr(ref, "reference_name", None) if not isinstance(ref, dict) else ref.get("reference_name")
		allocated = getattr(ref, "allocated_amount", 0.0) if not isinstance(ref, dict) else ref.get("allocated_amount", 0.0)
		if ref_doctype in ("Sales Invoice", "Sales Order") and ref_name:
			channel = frappe.db.get_value(ref_doctype, ref_name, "sales_channel")
			allocated = float(allocated or 0.0)
			breakdown[channel] = breakdown.get(channel, 0.0) + allocated
	return breakdown

def get_payment_origin_breakdown(doc):
	"""
	Derive transaction origin allocation breakdown from Payment Entry child references.
	Returns a dictionary mapping transaction_origin to allocated amount:
	e.g. {'WEB': 100.0, 'PHONE': 50.0}
	Note: This represents cash collections / payments, not revenue (authority is Sales Invoice).
	"""
	if isinstance(doc, str):
		doc = frappe.get_doc("Payment Entry", doc)
	breakdown = {}
	refs = doc.get("references") or []
	for ref in refs:
		ref_doctype = getattr(ref, "reference_doctype", None) if not isinstance(ref, dict) else ref.get("reference_doctype")
		ref_name = getattr(ref, "reference_name", None) if not isinstance(ref, dict) else ref.get("reference_name")
		allocated = getattr(ref, "allocated_amount", 0.0) if not isinstance(ref, dict) else ref.get("allocated_amount", 0.0)
		if ref_doctype in ("Sales Invoice", "Sales Order") and ref_name:
			origin = frappe.db.get_value(ref_doctype, ref_name, "transaction_origin")
			allocated = float(allocated or 0.0)
			breakdown[origin] = breakdown.get(origin, 0.0) + allocated
	return breakdown

def validate_transaction_attribution(doc, method=None):
	validate_submitted_immutability(doc)

	if getattr(doc, "is_return", 0) and getattr(doc, "return_against", None):
		ref_doctype = doc.doctype
		orig_channel = frappe.db.get_value(ref_doctype, doc.return_against, "sales_channel")
		if orig_channel:
			if not doc.get("sales_channel"):
				doc.sales_channel = orig_channel
			elif doc.sales_channel != orig_channel:
				frappe.throw(
					_("Return / Credit Note channel '{0}' must match the original {1} channel '{2}'.").format(
						doc.sales_channel, ref_doctype, orig_channel
					)
				)
