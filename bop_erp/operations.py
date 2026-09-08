# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional
import json
import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime

from bop_erp.constants import IntegrationStatus, IntegrationReadinessStatus, IntegrationDirection
from bop_erp.reliability import recover_stale_events
from bop_erp.inventory.publication import process_inventory_publication_event
from bop_erp.inventory.reconciliation import reconcile_inventory_item, reconcile_channel_inventory
from bop_erp.orders.ingestion import process_order_ingestion_event


# ======================================================================
# OPERATOR INSPECTION SERVICES (No direct SQL required)
# ======================================================================

def get_failed_review_orders(limit: int = 50) -> List[Dict[str, Any]]:
	"""Retrieves Sales Orders that were ingested but failed operational validation / reservation."""
	return frappe.get_all(
		"Sales Order",
		filters={"integration_status": IntegrationReadinessStatus.FAILED_REVIEW},
		fields=["name", "customer", "transaction_date", "grand_total", "sales_channel", "integration_error"],
		order_by="modified desc",
		limit=limit,
	)


def get_dead_letter_events(limit: int = 50) -> List[Dict[str, Any]]:
	"""Retrieves Integration Events that reached terminal DEAD_LETTER status."""
	return frappe.get_all(
		"Integration Event",
		filters={"status": IntegrationStatus.DEAD_LETTER},
		fields=[
			"name",
			"sales_channel",
			"provider",
			"direction",
			"entity_type",
			"erp_doctype",
			"erp_document",
			"attempt_count",
			"last_error_code",
			"last_error_message",
			"modified",
		],
		order_by="modified desc",
		limit=limit,
	)


def get_retry_pending_events(limit: int = 50) -> List[Dict[str, Any]]:
	"""Retrieves Integration Events currently awaiting backoff retry."""
	return frappe.get_all(
		"Integration Event",
		filters={"status": IntegrationStatus.RETRY_PENDING},
		fields=[
			"name",
			"sales_channel",
			"direction",
			"entity_type",
			"erp_document",
			"attempt_count",
			"next_retry_at",
			"last_error_code",
			"last_error_message",
		],
		order_by="next_retry_at asc",
		limit=limit,
	)


def get_stale_processing_events(limit: int = 50) -> List[Dict[str, Any]]:
	"""Retrieves Integration Events whose processing lease has expired without completion."""
	now = now_datetime()
	return frappe.db.sql(
		"""
		SELECT name, sales_channel, direction, entity_type, erp_document, worker_id, lease_expires_at, processing_token
		FROM `tabIntegration Event`
		WHERE status = %s
		  AND lease_expires_at IS NOT NULL
		  AND lease_expires_at < %s
		ORDER BY lease_expires_at asc
		LIMIT %s
		""",
		(IntegrationStatus.PROCESSING, now, limit),
		as_dict=True,
	)


def inspect_order_failure_reason(sales_order_name: str) -> Dict[str, Any]:
	"""Retrieves detailed operational and error attribution for an order."""
	so = frappe.get_doc("Sales Order", sales_order_name)
	return {
		"name": so.name,
		"sales_channel": getattr(so, "sales_channel", None),
		"integration_status": getattr(so, "integration_status", None),
		"integration_error": getattr(so, "integration_error", None),
		"docstatus": so.docstatus,
		"items": [
			{"item_code": d.item_code, "qty": d.qty, "warehouse": d.warehouse}
			for d in so.items
		],
	}


# ======================================================================
# MANUAL RECOVERY OPERATIONS (Preserves fencing, leases & idempotency)
# ======================================================================

def retry_failed_publication_event(event_name: str) -> Dict[str, Any]:
	"""
	Allows an operator to safely make a DEAD_LETTER or RETRY_PENDING outbound event
	eligible for immediate retry by resetting its next_retry_at or status to RETRY_PENDING.
	Does NOT force success; worker must still claim and publish through standard pipeline.
	"""
	ev = frappe.get_doc("Integration Event", event_name)
	if ev.direction != IntegrationDirection.OUTBOUND:
		frappe.throw(_("Event '{0}' is not an outbound event.").format(event_name))

	if ev.status not in (IntegrationStatus.DEAD_LETTER, IntegrationStatus.RETRY_PENDING):
		frappe.throw(
			_("Event '{0}' is in status '{1}'. Only DEAD_LETTER or RETRY_PENDING events can be retried.").format(
				event_name, ev.status
			)
		)

	# Reset attempt or set next_retry_at to now
	frappe.db.sql(
		"""
		UPDATE `tabIntegration Event`
		SET status = %s,
			next_retry_at = %s,
			modified = %s
		WHERE name = %s
		""",
		(IntegrationStatus.RETRY_PENDING, now_datetime(), now_datetime(), event_name),
	)
	frappe.db.commit()

	return {"success": True, "event_name": event_name, "status": IntegrationStatus.RETRY_PENDING}


def recover_stale_publication_events(timeout_minutes: Optional[int] = None) -> Dict[str, Any]:
	"""Operator entry point to trigger immediate recovery of stale processing leases."""
	recovered_count = recover_stale_events(timeout_minutes=timeout_minutes)
	return {"success": True, "recovered_events": recovered_count}


def reprocess_inbound_order_event(event_name: str) -> Dict[str, Any]:
	"""
	Allows an operator to reprocess an eligible inbound order event.
	Enforces standard claim and pipeline execution.
	"""
	ev = frappe.get_doc("Integration Event", event_name)
	if ev.direction != IntegrationDirection.INBOUND:
		frappe.throw(_("Event '{0}' is not an inbound event.").format(event_name))

	return process_order_ingestion_event(event_name)
