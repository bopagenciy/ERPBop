# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional
import frappe
from frappe import _
from frappe.utils import get_datetime

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationDirection,
	IntegrationOperation,
	IntegrationProvider,
	IntegrationStatus,
	ErrorCategory,
)
from bop_erp.safety import (
	ConnectorSafetyError,
	assert_safe_connector_target,
)
from bop_erp.reliability import (
	claim_event_for_processing,
	verify_processing_authority,
	get_database_now,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopError,
	PrestaShopAuthError,
	PrestaShopNotFoundError,
	PrestaShopRateLimitError,
	PrestaShopValidationError,
	PrestaShopServerError,
	PrestaShopTransientError,
)

logger = logging.getLogger("bop_erp.orders.fulfillment_writeback")

ORDER_WRITEBACK_COUNTERS = {
	"order_state_events_created": 0,
	"order_state_events_claimed": 0,
	"order_state_writes_attempted": 0,
	"order_state_writes_succeeded": 0,
	"order_state_writes_noop": 0,
	"order_state_writes_retryable_failed": 0,
	"order_state_writes_permanent_failed": 0,
	"order_state_writes_blocked_remote_state": 0,
	"order_state_writes_superseded": 0,
	"order_state_dead_lettered": 0,
}


def get_order_state_writeback_telemetry() -> Dict[str, int]:
	"""Returns a snapshot copy of writeback telemetry counters."""
	return dict(ORDER_WRITEBACK_COUNTERS)


def reset_order_state_writeback_telemetry() -> None:
	"""Resets writeback telemetry counters to zero."""
	for k in ORDER_WRITEBACK_COUNTERS:
		ORDER_WRITEBACK_COUNTERS[k] = 0


# =========================================================================
# 1. TRANSACTIONAL OUTBOX INTENT CREATION
# =========================================================================

def create_order_fulfillment_writeback_intent(delivery_note_doc: Any) -> Optional[Any]:
	"""
	Persists an outbound Integration Event for order fulfillment status writeback
	in the same database transaction as the Delivery Note submission.
	Ensures crash safety before after_commit wake hook.

	Eligibility rules (Section 7):
	- Delivery Note docstatus == 1 (Submitted)
	- Linked to a valid Sales Order (docstatus == 1, integration_status == READY)
	- Sales Order has external identity (external_order_id, integration_provider != NONE)
	- Sales Order is not in review or cancelled status
	- Connector exists for channel and company matches
	- Manual/native ERP Sales Orders create NO external writeback event
	"""
	dn = delivery_note_doc
	if isinstance(dn, str):
		dn = frappe.get_doc("Delivery Note", dn)

	dn_docstatus = getattr(dn, "docstatus", None)
	if dn_docstatus is None and isinstance(dn, dict):
		dn_docstatus = dn.get("docstatus")

	dn_name = getattr(dn, "name", None)
	if dn_name is None and isinstance(dn, dict):
		dn_name = dn.get("name")

	dn_company = getattr(dn, "company", None)
	if dn_company is None and isinstance(dn, dict):
		dn_company = dn.get("company")

	if dn_docstatus != 1:
		logger.debug("Delivery Note '%s' is not submitted (docstatus=%s). Skipping writeback intent.", dn_name, dn_docstatus)
		return None

	# Resolve linked Sales Order(s) from Delivery Note items
	items = dn.get("items") if isinstance(dn, dict) else getattr(dn, "items", [])
	items = items or []
	so_names = {
		(it.get("against_sales_order") if isinstance(it, dict) else getattr(it, "against_sales_order", None))
		for it in items
	}
	so_names = {n for n in so_names if n}

	if not so_names:
		logger.debug("Delivery Note '%s' has no linked Sales Orders. Skipping writeback intent.", dn_name)
		return None

	created_events = []
	for so_name in sorted(so_names):
		so_row_raw = frappe.db.get_value(
			"Sales Order",
			so_name,
			["name", "docstatus", "company", "sales_channel", "integration_status"],
			as_dict=True,
		)
		so_row = frappe._dict(so_row_raw) if so_row_raw else None
		if not so_row or so_row.docstatus != 1:
			logger.debug("Linked Sales Order '%s' is not active/submitted. Skipping.", so_name)
			continue

		# Must have integration_status == READY (Section 7)
		if so_row.integration_status != "READY":
			logger.info(
				"Sales Order '%s' integration_status is '%s' (expected READY). Skipping writeback intent.",
				so_name,
				so_row.integration_status,
			)
			continue

		# Company isolation (Section 16)
		if so_row.company != dn_company:
			logger.warning(
				"Company mismatch: Sales Order '%s' company '%s' != Delivery Note '%s' company '%s'. Skipping.",
				so_name,
				so_row.company,
				dn_name,
				dn_company,
			)
			continue

		so_doc = frappe.get_doc("Sales Order", so_name)
		external_order_id = getattr(so_doc, "external_order_id", None)
		provider = getattr(so_doc, "integration_provider", None)
		sales_channel = getattr(so_doc, "sales_channel", None) or (dn.get("sales_channel") if isinstance(dn, dict) else getattr(dn, "sales_channel", None))

		# Manual/native ERP Sales Orders with no external identity -> no writeback (Section 7, 14)
		if not external_order_id or not provider or provider == IntegrationProvider.NONE:
			logger.debug("Sales Order '%s' has no external provider/order identity. Skipping writeback.", so_name)
			continue

		# Deterministic canonical idempotency key (Section 11)
		target_state_semantic = "SHIPPED"
		idempotency_key = f"order-writeback:{provider}:{sales_channel}:{external_order_id}:{target_state_semantic}:{dn_name}"

		# Duplicate event check
		existing_event = frappe.db.get_value("Integration Event", {"idempotency_key": idempotency_key}, "name")
		if existing_event:
			logger.info("Found existing writeback event '%s' for idempotency key '%s'.", existing_event, idempotency_key)
			created_events.append(frappe.get_doc("Integration Event", existing_event))
			continue

		# Transactional insert within current DB transaction
		event_doc = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": provider,
			"sales_channel": sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
			"entity_type": ExternalEntityType.ORDER,
			"external_id": str(external_order_id),
			"status": IntegrationStatus.PENDING,
			"idempotency_key": idempotency_key,
			"erp_doctype": "Delivery Note",
			"erp_document": dn_name,
			"request_metadata": json.dumps({
				"sales_order": so_name,
				"delivery_note": dn_name,
				"target_state_semantic": target_state_semantic,
				"company": dn_company,
			}),
		}).insert(ignore_permissions=True, ignore_links=True)

		ORDER_WRITEBACK_COUNTERS["order_state_events_created"] += 1
		logger.info(
			"Created durable outbound fulfillment writeback event '%s' (DN='%s', Order='%s', Channel='%s').",
			event_doc.name,
			dn_name,
			external_order_id,
			sales_channel,
		)
		created_events.append(event_doc)

	# Register wake dispatcher after successful commit (Section 6)
	if created_events:
		frappe.db.after_commit(enqueue_order_fulfillment_writeback_dispatcher)

	return created_events[0] if created_events else None


def handle_delivery_note_submit(doc: Any, method: Optional[str] = None) -> None:
	"""Delivery Note on_submit hook handler."""
	create_order_fulfillment_writeback_intent(doc)


def handle_delivery_note_cancel(doc: Any, method: Optional[str] = None) -> None:
	"""
	Delivery Note on_cancel hook handler.
	Enforces:
	- If writeback is PENDING or RETRY_PENDING: supersedes/cancels the event (Section 20, 49).
	- If writeback already SUCCEEDED: does NOT unship remote order (Section 18).
	  Routes to review required / logs reconciliation obligation.
	"""
	dn_name = getattr(doc, "name", str(doc))
	events = frappe.get_all(
		"Integration Event",
		filters={
			"erp_doctype": "Delivery Note",
			"erp_document": dn_name,
			"operation": IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
		},
		fields=["name", "status", "external_id", "sales_channel"],
	)

	for ev in events:
		if ev.status in (IntegrationStatus.PENDING, IntegrationStatus.RETRY_PENDING):
			frappe.db.set_value("Integration Event", ev.name, {
				"status": IntegrationStatus.CANCELLED,
				"last_error_message": _("Delivery Note '{0}' was cancelled before fulfillment writeback executed.").format(dn_name),
			})
			ORDER_WRITEBACK_COUNTERS["order_state_writes_superseded"] += 1
			logger.info("Cancelled pending order writeback event '%s' due to Delivery Note cancellation.", ev.name)
		elif ev.status == IntegrationStatus.SUCCEEDED:
			# Section 18: No automatic remote unship. Flag review requirement.
			logger.warning(
				"Delivery Note '%s' was cancelled after remote fulfillment writeback already succeeded for PrestaShop order '%s'. "
				"No automatic remote unship is performed. Manual operational review required.",
				dn_name,
				ev.external_id,
			)


# =========================================================================
# 2. WORKER EXECUTION & AUTHORITY
# =========================================================================

def get_active_connector_for_channel(sales_channel: str):
	"""Retrieves the active PrestaShop Connector document for a channel."""
	connectors = frappe.get_all(
		"PrestaShop Connector",
		filters={"sales_channel": sales_channel, "enabled": 1},
		fields=["name"],
		limit=1,
	)
	if not connectors:
		return None
	return frappe.get_doc("PrestaShop Connector", connectors[0].name)


def process_order_fulfillment_writeback_event(
	event_name: str,
	worker_id: Optional[str] = None,
	client: Optional[PrestaShopClient] = None,
	pre_write_hook: Optional[Any] = None,
) -> bool:
	"""
	Core authoritative execution unit for an outbound order fulfillment writeback event.
	Enforces:
	1. Atomic worker claim with processing token and lease.
	2. Initial authority verification against database authoritative clock.
	3. Fresh ERP state verification (Delivery Note still submitted, SO active, external mapping valid).
	4. Company and channel boundary enforcement.
	5. Connector capability validation (order_state_write_enabled == 1, shipping_state_id configured).
	6. Fresh PrestaShop remote GET check:
	   - If already in target state -> NO-OP SUCCEEDED (no redundant history insert).
	   - If in CANCELED, REFUNDED, PAYMENT_ERROR -> BLOCKED / REVIEW, zero remote write.
	   - If already DELIVERED -> NO-OP / SUPERSEDED, do not regress state.
	7. Second authority verification immediately before remote write.
	8. Remote order history creation via PrestaShopClient.
	9. Idempotent success acknowledgment or structured retry / dead-letter handling.
	"""
	# 1. Claim event
	is_claimed, worker_id, processing_token = claim_event_for_processing(
		event_name=event_name,
		worker_id=worker_id,
	)
	if not is_claimed:
		logger.debug("Event '%s' could not be claimed by worker '%s'.", event_name, worker_id)
		return False

	ORDER_WRITEBACK_COUNTERS["order_state_events_claimed"] += 1

	# 2. Initial processing authority check (Section 13)
	is_auth, auth_reason = verify_processing_authority(event_name, processing_token)
	if not is_auth:
		logger.warning("Worker '%s' lacks initial authority for event '%s': %s", worker_id, event_name, auth_reason)
		return False

	event_doc = frappe.get_doc("Integration Event", event_name)

	try:
		# 3. Fresh ERP State Check (Section 8, 20)
		# 3. Fresh ERP State Check (Section 8, 20)
		dn_name = event_doc.erp_document
		dn_row = None
		if dn_name:
			dn_raw = frappe.db.get_value(
				"Delivery Note",
				dn_name,
				["name", "docstatus", "company", "sales_channel"],
				as_dict=True,
			)
			dn_row = frappe._dict(dn_raw) if dn_raw else None

		if not dn_row or dn_row.docstatus != 1:
			# Delivery Note cancelled or missing
			event_doc.cancel(
				reason=f"Delivery Note '{dn_name}' is no longer in submitted state (docstatus={dn_row.docstatus if dn_row else 'None'}). Superseded.",
				processing_token=processing_token,
			)
			ORDER_WRITEBACK_COUNTERS["order_state_writes_superseded"] += 1
			return False

		# Re-read Sales Order from request metadata
		meta = json.loads(event_doc.request_metadata or "{}")
		so_name = meta.get("sales_order")
		if so_name:
			so_raw = frappe.db.get_value(
				"Sales Order",
				so_name,
				["name", "docstatus", "company", "integration_status"],
				as_dict=True,
			)
			so_row = frappe._dict(so_raw) if so_raw else None
			if not so_row or so_row.docstatus != 1:
				event_doc.cancel(
					reason=f"Sales Order '{so_name}' is no longer submitted. Superseded.",
					processing_token=processing_token,
				)
				ORDER_WRITEBACK_COUNTERS["order_state_writes_superseded"] += 1
				return False

			if so_row.integration_status in ("CHANGE_REVIEW_REQUIRED", "FAILED_REVIEW", "CANCELLATION_PENDING", "CANCELLED"):
				event_doc.mark_failed(
					processing_token=processing_token,
					error_code="ERP_STATE_BLOCKED",
					error_message=f"Sales Order '{so_name}' is in blocked status '{so_row.integration_status}'. Writeback blocked.",
					error_category=ErrorCategory.NON_RETRYABLE,
				)
				ORDER_WRITEBACK_COUNTERS["order_state_writes_permanent_failed"] += 1
				return False

		# Validate Canonical External ID Mapping (Section 17)
		ext_mapping_raw = frappe.db.get_value(
			"External ID Mapping",
			{
				"provider": event_doc.provider,
				"sales_channel": event_doc.sales_channel,
				"external_entity_type": ExternalEntityType.ORDER,
				"external_id": str(event_doc.external_id),
			},
			["name", "erp_doctype", "erp_document"],
			as_dict=True,
		)
		ext_mapping = frappe._dict(ext_mapping_raw) if ext_mapping_raw else None
		if ext_mapping and so_name and ext_mapping.erp_document != so_name:
			event_doc.mark_failed(
				processing_token=processing_token,
				error_code="IDENTITY_DRIFT",
				error_message=f"External ID Mapping points to '{ext_mapping.erp_document}', but event belongs to '{so_name}'. Writeback blocked.",
				error_category=ErrorCategory.NON_RETRYABLE,
			)
			ORDER_WRITEBACK_COUNTERS["order_state_writes_permanent_failed"] += 1
			return False

		# 4. Resolve Connector & Validate Scoped Permissions (Section 3, 15, 16, 24)
		connector = get_active_connector_for_channel(event_doc.sales_channel)
		if not connector:
			raise PrestaShopValidationError(
				f"No active PrestaShop Connector found for channel '{event_doc.sales_channel}'."
			)

		# Company verification
		channel_company = frappe.db.get_value("Sales Channel", event_doc.sales_channel, "company")
		if channel_company and dn_row.company != channel_company:
			raise PrestaShopValidationError(
				f"Company mismatch: Delivery Note company '{dn_row.company}' does not match Sales Channel company '{channel_company}'."
			)

		# Scoped write enablement check
		if not getattr(connector, "order_state_write_enabled", False):
			event_doc.mark_failed(
				processing_token=processing_token,
				error_code="ORDER_STATE_WRITES_DISABLED",
				error_message=_("PrestaShop connector for channel '{0}' has order state writes disabled.").format(event_doc.sales_channel),
				error_category=ErrorCategory.NON_RETRYABLE,
			)
			ORDER_WRITEBACK_COUNTERS["order_state_writes_permanent_failed"] += 1
			return False

		# Store-specific shipping state configuration check (Section 3)
		shipping_state_id = getattr(connector, "shipping_state_id", None)
		if not shipping_state_id or not str(shipping_state_id).strip():
			event_doc.mark_failed(
				processing_token=processing_token,
				error_code="SHIPPING_STATE_NOT_CONFIGURED",
				error_message=_("PrestaShop connector for channel '{0}' has no shipping_state_id configured. Writeback blocked.").format(event_doc.sales_channel),
				error_category=ErrorCategory.NON_RETRYABLE,
			)
			ORDER_WRITEBACK_COUNTERS["order_state_writes_permanent_failed"] += 1
			return False

		target_state_id = int(str(shipping_state_id).strip())

		# 5. Resolve Client & Revalidate Runtime Host Safety (Section 25)
		if client is None:
			client = connector.get_client()

		assert_safe_connector_target(
			environment=connector.environment,
			base_url=connector.base_url,
		)

		# 6. Fresh PrestaShop Remote State Check (Section 9, 10, 11, 12, 19, 22)
		remote_order = client.get_order(event_doc.external_id)
		if not remote_order:
			raise PrestaShopNotFoundError(f"Order '{event_doc.external_id}' not found in PrestaShop.")

		remote_current_state = int(remote_order.get("current_state", 0))

		# Policy A: Already in target state -> NO-OP CONVERGENCE (Section 10, 12, 35)
		if remote_current_state == target_state_id:
			event_doc.mark_succeeded(
				processing_token=processing_token,
				response_metadata={
					"changed": False,
					"noop": True,
					"reason": "ALREADY_SHIPPED",
					"current_state": remote_current_state,
					"target_state_id": target_state_id,
				},
			)
			ORDER_WRITEBACK_COUNTERS["order_state_writes_noop"] += 1
			logger.info(
				"PrestaShop order '%s' is already in target Shipped state (%s). Converged as NO-OP.",
				event_doc.external_id,
				target_state_id,
			)
			return True

		# Policy B: Already Delivered -> Do NOT regress state (Section 10, 22)
		delivered_state_id = getattr(connector, "delivered_state_id", None)
		if delivered_state_id and str(delivered_state_id).strip():
			deliv_id = int(str(delivered_state_id).strip())
			if remote_current_state == deliv_id:
				event_doc.mark_succeeded(
					processing_token=processing_token,
					response_metadata={
						"changed": False,
						"noop": True,
						"reason": "ALREADY_DELIVERED",
						"current_state": remote_current_state,
						"delivered_state_id": deliv_id,
					},
				)
				ORDER_WRITEBACK_COUNTERS["order_state_writes_noop"] += 1
				logger.info(
					"PrestaShop order '%s' is already Delivered (%s). Regression prevented, converged as NO-OP.",
					event_doc.external_id,
					deliv_id,
				)
				return True

		# Policy C: Remote Cancellation -> Do NOT overwrite cancellation (Section 10, 19, 37)
		cancellation_states = {
			int(s.strip())
			for s in (connector.cancellation_order_states or "").split(",")
			if s.strip().isdigit()
		}
		if cancellation_states and remote_current_state in cancellation_states:
			event_doc.mark_failed(
				processing_token=processing_token,
				error_code="REMOTE_CANCELED",
				error_message=f"PrestaShop order '{event_doc.external_id}' is in cancellation state ({remote_current_state}). Overwrite strictly blocked.",
				error_category=ErrorCategory.NON_RETRYABLE,
			)
			ORDER_WRITEBACK_COUNTERS["order_state_writes_blocked_remote_state"] += 1
			logger.warning(
				"PrestaShop order '%s' has been canceled externally (state=%s). Writeback blocked.",
				event_doc.external_id,
				remote_current_state,
			)
			return False

		# Policy D: Remote Review / Error / Refund states -> Do NOT overwrite (Section 10, 22)
		review_states = {
			int(s.strip())
			for s in (connector.review_order_states or "").split(",")
			if s.strip().isdigit()
		}
		if review_states and remote_current_state in review_states:
			event_doc.mark_failed(
				processing_token=processing_token,
				error_code="REMOTE_REVIEW_REQUIRED",
				error_message=f"PrestaShop order '{event_doc.external_id}' is in terminal review state ({remote_current_state}). Overwrite blocked.",
				error_category=ErrorCategory.NON_RETRYABLE,
			)
			ORDER_WRITEBACK_COUNTERS["order_state_writes_blocked_remote_state"] += 1
			logger.warning(
				"PrestaShop order '%s' is in review state (%s). Writeback blocked.",
				event_doc.external_id,
				remote_current_state,
			)
			return False

		# Policy E: Remote Ineligible / Unmapped state -> Fail safe to review
		eligible_states = {
			int(s.strip())
			for s in (connector.eligible_order_states or "").split(",")
			if s.strip().isdigit()
		}
		if eligible_states and remote_current_state not in eligible_states:
			event_doc.mark_failed(
				processing_token=processing_token,
				error_code="REMOTE_REVIEW_REQUIRED",
				error_message=f"PrestaShop order '{event_doc.external_id}' is in unmapped/ineligible state ({remote_current_state}). Overwrite blocked.",
				error_category=ErrorCategory.NON_RETRYABLE,
			)
			ORDER_WRITEBACK_COUNTERS["order_state_writes_blocked_remote_state"] += 1
			logger.warning(
				"PrestaShop order '%s' is in unmapped/ineligible state (%s). Writeback blocked.",
				event_doc.external_id,
				remote_current_state,
			)
			return False

		if not cancellation_states and not eligible_states:
			event_doc.mark_failed(
				processing_token=processing_token,
				error_code="SEMANTIC_STATES_NOT_CONFIGURED",
				error_message=f"PrestaShop connector for channel '{event_doc.sales_channel}' has no semantic order states (cancellation/eligible) configured. Writeback blocked.",
				error_category=ErrorCategory.NON_RETRYABLE,
			)
			ORDER_WRITEBACK_COUNTERS["order_state_writes_permanent_failed"] += 1
			logger.warning(
				"PrestaShop connector for channel '%s' lacks semantic state configuration. Writeback blocked.",
				event_doc.sales_channel,
			)
			return False

		# 7. Pre-mutation Hook & Second Authority Check (Section 13, 14, 39, 40)
		if pre_write_hook is not None:
			pre_write_hook()

		is_auth_pre, reason_pre = verify_processing_authority(event_name, processing_token)
		if not is_auth_pre:
			ORDER_WRITEBACK_COUNTERS["order_state_writes_retryable_failed"] += 1
			logger.warning(
				"Worker '%s' lost processing authority immediately before remote write for event '%s': %s",
				worker_id,
				event_name,
				reason_pre,
			)
			return False

		# 8. Execute Remote Order State Mutation (Section 2, 50)
		send_email = bool(getattr(connector, "order_state_send_email", False))
		ORDER_WRITEBACK_COUNTERS["order_state_writes_attempted"] += 1
		res = client.update_order_state(
			order_id=event_doc.external_id,
			target_state_id=target_state_id,
			send_email=send_email,
		)

		ORDER_WRITEBACK_COUNTERS["order_state_writes_succeeded"] += 1
		event_doc.mark_succeeded(processing_token=processing_token, response_metadata=res)
		logger.info(
			"Successfully updated PrestaShop order '%s' to Shipped (state=%s) via event '%s'.",
			event_doc.external_id,
			target_state_id,
			event_name,
		)
		return True

	except (PrestaShopTransientError, PrestaShopServerError, PrestaShopRateLimitError) as retry_err:
		ORDER_WRITEBACK_COUNTERS["order_state_writes_retryable_failed"] += 1
		delay = getattr(retry_err, "retry_after", None) or 60
		event_doc.mark_failed(
			processing_token=processing_token,
			error_code=type(retry_err).__name__,
			error_message=str(retry_err),
			error_category=ErrorCategory.TRANSIENT,
			delay_seconds=delay,
		)
		if event_doc.status == IntegrationStatus.DEAD_LETTER:
			ORDER_WRITEBACK_COUNTERS["order_state_dead_lettered"] += 1
		logger.warning("Retryable error processing event '%s': %s", event_name, str(retry_err))
		return False

	except (PrestaShopAuthError, PrestaShopNotFoundError, PrestaShopValidationError, ConnectorSafetyError, frappe.ValidationError) as perm_err:
		ORDER_WRITEBACK_COUNTERS["order_state_writes_permanent_failed"] += 1
		event_doc.mark_failed(
			processing_token=processing_token,
			error_code=type(perm_err).__name__,
			error_message=str(perm_err),
			error_category=ErrorCategory.NON_RETRYABLE,
		)
		logger.error("Permanent error processing event '%s': %s", event_name, str(perm_err))
		return False

	except Exception as unk_err:
		ORDER_WRITEBACK_COUNTERS["order_state_writes_permanent_failed"] += 1
		event_doc.mark_failed(
			processing_token=processing_token,
			error_code="UNHANDLED_EXCEPTION",
			error_message=f"Unhandled error during order writeback: {str(unk_err)}",
			error_category=ErrorCategory.NON_RETRYABLE,
		)
		logger.exception("Unexpected error processing event '%s': %s", event_name, str(unk_err))
		return False


# =========================================================================
# 3. SCHEDULER & DISPATCHER
# =========================================================================

def process_pending_order_fulfillment_writebacks(
	sales_channel: Optional[str] = None,
	max_events: int = 25,
	worker_id: Optional[str] = None,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Bounded, fair scheduler worker for outbound order fulfillment writeback events (Section 31, 32).
	Enforces:
	- Bounded batch processing (max_events limit)
	- Database-authoritative time for retry due-time comparisons
	- Multi-channel round-robin fairness
	- Failure isolation between items
	"""
	import time
	start_time = time.time()
	if not worker_id:
		import uuid
		worker_id = f"worker-owb-{uuid.uuid4().hex[:8]}"

	db_now = get_database_now()

	# Query candidate events
	filters_sql = [
		"direction = %s",
		"operation = %s",
		"entity_type = %s",
		"(status = %s OR (status = %s AND next_retry_at IS NOT NULL AND next_retry_at <= %s))",
	]
	params = [
		IntegrationDirection.OUTBOUND,
		IntegrationOperation.UPDATE_ORDER_FULFILLMENT_STATE,
		ExternalEntityType.ORDER,
		IntegrationStatus.PENDING,
		IntegrationStatus.RETRY_PENDING,
		db_now,
	]

	if sales_channel:
		filters_sql.append("sales_channel = %s")
		params.append(sales_channel)

	query = f"""
		SELECT name, sales_channel, status, next_retry_at, creation
		FROM `tabIntegration Event`
		WHERE {' AND '.join(filters_sql)}
		ORDER BY creation ASC
		LIMIT %s
	"""
	params.append(int(max_events) * 2)

	candidate_rows = frappe.db.sql(query, tuple(params), as_dict=True)

	# Multi-channel fairness: interleave candidate events across sales channels (Section 32)
	by_channel = defaultdict(list)
	for r in candidate_rows:
		by_channel[r.sales_channel].append(r)

	interleaved_events = []
	while by_channel and len(interleaved_events) < max_events:
		empty_channels = []
		for ch, ch_events in list(by_channel.items()):
			if ch_events:
				interleaved_events.append(ch_events.pop(0))
				if len(interleaved_events) >= max_events:
					break
			else:
				empty_channels.append(ch)
		for ech in empty_channels:
			by_channel.pop(ech, None)

	telemetry = {
		"worker_id": worker_id,
		"events_seen": len(interleaved_events),
		"events_processed": 0,
		"succeeded": 0,
		"failed": 0,
		"duration_seconds": 0.0,
	}

	for row in interleaved_events:
		ev_name = row.name
		success = process_order_fulfillment_writeback_event(
			event_name=ev_name,
			worker_id=worker_id,
			client=client,
		)
		telemetry["events_processed"] += 1
		if success:
			telemetry["succeeded"] += 1
		else:
			telemetry["failed"] += 1

	telemetry["duration_seconds"] = round(time.time() - start_time, 4)
	return telemetry


def enqueue_order_fulfillment_writeback_dispatcher(sales_channel: Optional[str] = None) -> None:
	"""Enqueues background execution of pending fulfillment writeback events."""
	frappe.enqueue(
		"bop_erp.orders.fulfillment_writeback.process_pending_order_fulfillment_writebacks",
		queue="default",
		sales_channel=sales_channel,
		max_events=25,
		now=frappe.flags.in_test or False,
	)
