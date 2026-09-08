# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Optional
import frappe
from frappe import _

from bop_erp.constants import IntegrationReadinessStatus, TransactionOrigin


class OperationalGuardError(frappe.ValidationError):
	"""Raised when operational fulfillment is attempted on an incomplete imported order."""
	pass


def assert_sales_order_ready_for_fulfillment(so_name: Optional[str]) -> None:
	"""
	Audits whether a Sales Order is operationally eligible for downstream fulfillment
	(Pick List, Delivery Note, Shipment, or other Bop fulfillment processes).

	Rules:
	1. Native / Manual ERP Sales Orders (no external_order_id, no integration_status,
	   not WEB origin) are completely unaffected and pass without restriction.
	2. Imported Bop Sales Orders must strictly have integration_status == READY.
	3. Imported Bop Sales Orders must satisfy complete native Stock Reservation Entries.
	"""
	if not so_name:
		return

	so_data = frappe.db.get_value(
		"Sales Order",
		so_name,
		["name", "sales_channel", "external_order_id", "integration_status", "transaction_origin", "docstatus"],
		as_dict=True,
	)
	if not so_data:
		return

	is_imported = bool(
		so_data.get("external_order_id")
		or so_data.get("integration_provider")
		or (
			so_data.get("sales_channel")
			and so_data.get("transaction_origin") in (TransactionOrigin.WEB, TransactionOrigin.MARKETPLACE)
		)
	)
	if not is_imported:
		# Native manual Sales Order: unaffected
		return

	status = so_data.get("integration_status")
	if status and status != IntegrationReadinessStatus.READY:
		frappe.throw(
			_(
				"Operational Guard Violation: Sales Order '{0}' is an imported order in '{1}' status "
				"and is not READY for operational fulfillment (Pick List / Delivery Note / Shipment)."
			).format(so_name, status),
			exc=OperationalGuardError,
			title=_("Operational Guard Blocked"),
		)

	if status == IntegrationReadinessStatus.READY:
		from bop_erp.orders.ingestion import is_order_ingestion_complete

		is_complete, missing = is_order_ingestion_complete(so_name)
		if not is_complete:
			frappe.throw(
				_(
					"Operational Guard Violation: Sales Order '{0}' has incomplete stock reservations: {1}."
				).format(so_name, "; ".join(missing)),
				exc=OperationalGuardError,
				title=_("Operational Guard Incomplete Reservations"),
			)


def validate_operational_guard(doc, method=None) -> None:
	"""
	Document event hook auditing downstream fulfillment documents.
	Enforces operational protection on Pick List, Delivery Note, and Shipment.
	"""
	dt = getattr(doc, "doctype", None)
	if not dt:
		return

	if dt == "Pick List":
		locations = doc.get("locations") or []
		for loc in locations:
			so_id = getattr(loc, "sales_order", None) if not isinstance(loc, dict) else loc.get("sales_order")
			if so_id:
				assert_sales_order_ready_for_fulfillment(so_id)

	elif dt == "Delivery Note":
		items = doc.get("items") or []
		for item in items:
			so_id = getattr(item, "against_sales_order", None) if not isinstance(item, dict) else item.get("against_sales_order")
			if so_id:
				assert_sales_order_ready_for_fulfillment(so_id)

	elif dt == "Shipment":
		delivery_notes = doc.get("delivery_notes") or []
		for dn_row in delivery_notes:
			dn_id = getattr(dn_row, "delivery_note", None) if not isinstance(dn_row, dict) else dn_row.get("delivery_note")
			if dn_id:
				dn_items = frappe.get_all(
					"Delivery Note Item",
					filters={"parent": dn_id},
					pluck="against_sales_order",
				)
				for so_id in set(filter(None, dn_items)):
					assert_sales_order_ready_for_fulfillment(so_id)
