# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import uuid
from typing import Any, Dict, Optional, Tuple

import frappe
from frappe.utils import now_datetime
from bop_erp.constants import (
	IntegrationDirection,
	IntegrationOperation,
	IntegrationProvider,
	IntegrationStatus,
)
from bop_erp.reliability import compute_active_idempotency_key, compute_payload_hash
from bop_erp.purchasing.exceptions import (
	PurchaseDriftError,
	PurchaseReplayCancelledError,
)


class PurchaseOperation:
	CREATE_PURCHASE_ORDER = "CREATE_PURCHASE_ORDER"
	RECEIVE_PURCHASE_ORDER = "RECEIVE_PURCHASE_ORDER"
	CREATE_PURCHASE_INVOICE = "CREATE_PURCHASE_INVOICE"
	PAY_PURCHASE_INVOICE = "PAY_PURCHASE_INVOICE"

	ALL = (
		CREATE_PURCHASE_ORDER,
		RECEIVE_PURCHASE_ORDER,
		CREATE_PURCHASE_INVOICE,
		PAY_PURCHASE_INVOICE,
	)


def get_entity_type_for_operation(operation: str) -> str:
	if operation == PurchaseOperation.CREATE_PURCHASE_ORDER:
		return "ORDER"
	elif operation in (PurchaseOperation.RECEIVE_PURCHASE_ORDER,):
		return "SHIPMENT"
	elif operation == PurchaseOperation.CREATE_PURCHASE_INVOICE:
		return "INVOICE"
	elif operation == PurchaseOperation.PAY_PURCHASE_INVOICE:
		return "PAYMENT"
	return "OTHER"


def compute_purchase_payload_hash(payload: Dict[str, Any]) -> str:
	"""Computes canonical deterministic SHA-256 hash for purchasing operation payload."""
	return compute_payload_hash(payload) or ""


def compute_canonical_operation_key(
	operation_key: str,
	company: Optional[str] = None,
	operation_type: Optional[str] = None,
) -> str:
	"""
	Computes the canonical operation_key scoped by company, operation type, and request key.
	Formula: [company]:[operation_type]:[request_idempotency_key]
	"""
	comp = str(company or "").strip()
	op = str(operation_type or "").strip().upper()
	req_key = str(operation_key or "").strip()
	return f"{comp}:{op}:{req_key}"


def check_purchase_operation_replay(
	operation_key: str,
	operation_type: str,
	payload: Dict[str, Any],
	company: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
	"""
	Checks if a purchasing operation has already been executed for this scoped operation_key.
	Returns (erp_doctype, erp_document) on exact replay.
	Raises PurchaseReplayCancelledError if the linked ERP document was cancelled.
	Raises PurchaseDriftError if operation_key exists but payload has drifted.
	Returns None if operation is new.
	"""
	if not operation_key:
		return None

	canonical_op_key = compute_canonical_operation_key(operation_key, company, operation_type)
	entity_type = get_entity_type_for_operation(operation_type)
	active_key = compute_active_idempotency_key(
		provider=IntegrationProvider.INTERNAL,
		sales_channel="",
		entity_type=entity_type,
		operation=IntegrationOperation.CREATE,
		idempotency_key=canonical_op_key,
	)

	# Query existing Integration Event
	event = frappe.db.sql(
		"""
		SELECT name, erp_doctype, erp_document, payload_hash, status
		FROM `tabIntegration Event`
		WHERE active_idempotency_key = %s
		   OR (provider = %s AND idempotency_key = %s AND entity_type = %s)
		LIMIT 1
		""",
		(active_key, IntegrationProvider.INTERNAL, canonical_op_key, entity_type),
		as_dict=True,
	)

	if not event:
		return None

	row = event[0]
	row_payload_hash = row.get("payload_hash") if isinstance(row, dict) else getattr(row, "payload_hash", None)
	row_erp_doctype = row.get("erp_doctype") if isinstance(row, dict) else getattr(row, "erp_doctype", None)
	row_erp_document = row.get("erp_document") if isinstance(row, dict) else getattr(row, "erp_document", None)

	current_hash = compute_purchase_payload_hash(payload)

	# 1. Payload Drift Check
	if row_payload_hash and row_payload_hash != current_hash:
		raise PurchaseDriftError(
			f"Payload drift detected for operation key '{operation_key}' ({operation_type}). "
			f"Original hash: {row_payload_hash}, Current hash: {current_hash}"
		)

	# 2. Terminal Identity & Document Cancellation Check
	if row_erp_doctype and row_erp_document:
		if frappe.db.exists(row_erp_doctype, row_erp_document):
			docstatus = frappe.db.get_value(row_erp_doctype, row_erp_document, "docstatus")
			if docstatus == 2:
				raise PurchaseReplayCancelledError(
					f"Operation key '{operation_key}' ({operation_type}) was previously completed and linked to "
					f"'{row_erp_doctype}' '{row_erp_document}' which is now CANCELLED. "
					f"Replaying a cancelled operation is blocked. A new explicit request key is required."
				)
		return row_erp_doctype, row_erp_document

	return None


def record_purchase_operation(
	operation_key: str,
	operation_type: str,
	payload: Dict[str, Any],
	erp_doctype: str,
	erp_document: str,
	company: Optional[str] = None,
) -> str:
	"""
	Persists a completed purchasing operation into Integration Event for deterministic idempotency
	and DB-level unique constraint protection.
	"""
	if not operation_key:
		return ""

	canonical_op_key = compute_canonical_operation_key(operation_key, company, operation_type)
	entity_type = get_entity_type_for_operation(operation_type)
	active_key = compute_active_idempotency_key(
		provider=IntegrationProvider.INTERNAL,
		sales_channel="",
		entity_type=entity_type,
		operation=IntegrationOperation.CREATE,
		idempotency_key=canonical_op_key,
	)
	payload_hash = compute_purchase_payload_hash(payload)

	# Check if record already exists
	existing = frappe.db.get_value(
		"Integration Event",
		{"active_idempotency_key": active_key},
		["name", "payload_hash"],
		as_dict=True,
	)

	if existing:
		existing_hash = existing.get("payload_hash") if isinstance(existing, dict) else getattr(existing, "payload_hash", None)
		if existing_hash and existing_hash != payload_hash:
			raise PurchaseDriftError(
				f"Cannot overwrite operation '{operation_key}' with drifted payload."
			)
		return existing.get("name") if isinstance(existing, dict) else getattr(existing, "name", "")

	event_id = f"PUR-{uuid.uuid4().hex[:16]}"
	event_doc = frappe.get_doc({
		"doctype": "Integration Event",
		"event_id": event_id,
		"direction": IntegrationDirection.INBOUND,
		"provider": IntegrationProvider.INTERNAL,
		"entity_type": entity_type,
		"operation": IntegrationOperation.CREATE,
		"status": IntegrationStatus.SUCCEEDED,
		"idempotency_key": canonical_op_key,
		"active_idempotency_key": active_key,
		"payload_hash": payload_hash,
		"erp_doctype": erp_doctype,
		"erp_document": erp_document,
		"request_metadata": json.dumps({
			"purchase_operation": operation_type,
			"company": company or "",
			"client_operation_key": operation_key,
		}),
		"processing_finished_at": now_datetime(),
	})
	event_doc.insert(ignore_permissions=True)
	return event_doc.name
