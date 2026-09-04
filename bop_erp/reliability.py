# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import uuid
from datetime import timedelta
import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime
from bop_erp.constants import (
	IntegrationStatus,
	IntegrationDirection,
	IntegrationOperation,
	ErrorCategory,
	ExternalEntityType,
)
from bop_erp.bop_erp.doctype.bop_erp_settings.bop_erp_settings import get_settings

SENSITIVE_FIELD_NAMES = {
	"authorization",
	"password",
	"token",
	"secret",
	"api_key",
	"apikey",
	"private_key",
	"client_secret",
	"cvv",
	"card_number",
	"access_token",
	"refresh_token",
}

def sanitize_metadata(data, max_length=None):
	"""
	Recursively redacts sensitive keys from a dict/list and serializes to JSON.
	Enforces maximum character length from Bop ERP Settings.
	"""
	if data is None:
		return None

	if isinstance(data, str):
		try:
			parsed = json.loads(data)
			data = parsed
		except Exception:
			# Plain string: check for basic sensitive keywords or truncate
			pass

	if isinstance(data, (dict, list)):
		sanitized = _redact_sensitive_keys(data)
		serialized = json.dumps(sanitized, ensure_ascii=False, indent=2)
	else:
		serialized = str(data)

	if max_length is None:
		try:
			max_length = get_settings().integration_metadata_max_length or 5000
		except Exception:
			max_length = 5000

	if len(serialized) > max_length:
		serialized = serialized[:max_length] + "... [TRUNCATED]"

	return serialized

def _redact_sensitive_keys(obj):
	if isinstance(obj, dict):
		cleaned = {}
		for k, v in obj.items():
			lower_key = str(k).lower().strip()
			if any(s in lower_key for s in SENSITIVE_FIELD_NAMES):
				cleaned[k] = "[REDACTED]"
			else:
				cleaned[k] = _redact_sensitive_keys(v)
		return cleaned
	elif isinstance(obj, list):
		return [_redact_sensitive_keys(item) for item in obj]
	return obj

def compute_payload_hash(payload):
	"""
	Deterministic SHA-256 hash over canonical JSON payload or string/bytes.
	Redacts sensitive material before hashing.
	"""
	if payload is None:
		return None

	if isinstance(payload, str):
		try:
			payload = json.loads(payload)
		except Exception:
			pass

	if isinstance(payload, (dict, list)):
		redacted = _redact_sensitive_keys(payload)
		canonical_json = json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
		return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
	elif isinstance(payload, bytes):
		return hashlib.sha256(payload).hexdigest()
	else:
		return hashlib.sha256(str(payload).encode("utf-8")).hexdigest()

def compute_active_idempotency_key(provider, sales_channel, entity_type, operation, idempotency_key):
	"""
	Deterministic SHA-256 hash over canonical identity tuple:
	[provider, sales_channel, entity_type, operation, idempotency_key]
	"""
	if not idempotency_key:
		return None

	identity_tuple = [
		str(provider or "").strip(),
		str(sales_channel or "").strip(),
		str(entity_type or "").strip(),
		str(operation or "").strip(),
		str(idempotency_key).strip(),
	]
	canonical_json = json.dumps(identity_tuple, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

def claim_event_for_processing(event_name, worker_id=None):
	"""
	Atomic database-level claim.
	Transitions an event from claimable states (RECEIVED, PENDING, RETRY_PENDING)
	to PROCESSING, assigning worker_id and processing_started_at.
	Returns True if claim succeeded, False if already claimed or non-claimable.
	"""
	if not worker_id:
		worker_id = f"worker-{uuid.uuid4().hex[:12]}"

	now = now_datetime()
	claimable_statuses = (
		IntegrationStatus.RECEIVED,
		IntegrationStatus.PENDING,
		IntegrationStatus.RETRY_PENDING,
	)

	affected = frappe.db.sql(
		"""
		UPDATE `tabIntegration Event`
		SET status = %s,
			worker_id = %s,
			processing_started_at = %s,
			attempt_count = attempt_count + 1,
			modified = %s
		WHERE name = %s
		  AND status IN %s
		""",
		(IntegrationStatus.PROCESSING, worker_id, now, now, event_name, claimable_statuses),
	)

	# In MariaDB connector, affected rows can be checked via frappe.db.sql or rowcount
	# For safety, verify if the doc currently has our worker_id and PROCESSING status
	current_status, current_worker = frappe.db.get_value(
		"Integration Event", event_name, ["status", "worker_id"]
	) or (None, None)

	if current_status == IntegrationStatus.PROCESSING and current_worker == worker_id:
		return True, worker_id
	return False, None

def recover_stale_events(timeout_minutes=None):
	"""
	Scans for events stuck in PROCESSING beyond timeout_minutes and transitions them
	to safe retry or dead letter if max attempts exceeded.
	Idempotent and safe for multi-worker background cron execution.
	"""
	if timeout_minutes is None:
		try:
			timeout_minutes = get_settings().integration_processing_timeout_minutes or 15
		except Exception:
			timeout_minutes = 15

	cutoff = now_datetime() - timedelta(minutes=int(timeout_minutes))

	stale_events = frappe.db.get_all(
		"Integration Event",
		filters={
			"status": IntegrationStatus.PROCESSING,
			"processing_started_at": ["<", cutoff],
		},
		fields=["name", "attempt_count", "max_attempts"],
	)

	recovered_count = 0
	for item in stale_events:
		doc = frappe.get_doc("Integration Event", item.name)
		error_msg = _("Processing lease expired (worker timeout > {0} minutes). Recovered by system.").format(timeout_minutes)
		if doc.attempt_count >= doc.max_attempts:
			doc.mark_dead_letter("LEASE_TIMEOUT", error_msg)
		else:
			doc.schedule_retry("LEASE_TIMEOUT", error_msg)
		recovered_count += 1

	return recovered_count

def enqueue_integration_event(event_name):
	"""
	Queues an integration event for asynchronous background processing.
	If background workers are available, uses frappe.enqueue; otherwise direct execute.
	"""
	doc = frappe.get_doc("Integration Event", event_name)
	if doc.status == IntegrationStatus.RECEIVED:
		doc.status = IntegrationStatus.PENDING
		doc.save()

	# Enqueue using Frappe background queue
	frappe.enqueue(
		"bop_erp.reliability.process_integration_event",
		queue="default",
		event_name=event_name,
		now=frappe.flags.in_test or False,
	)

def process_integration_event(event_name, handler=None):
	"""
	Executes an Integration Event handler with atomic claim, error classification,
	and state progression.
	"""
	claimed, worker_id = claim_event_for_processing(event_name)
	if not claimed:
		return False

	doc = frappe.get_doc("Integration Event", event_name)

	try:
		if handler:
			result = handler(doc)
		else:
			# Default no-op deterministic success
			result = {"success": True}

		doc.mark_succeeded(response_metadata=result)
		return True
	except Exception as e:
		error_category = getattr(e, "error_category", ErrorCategory.INTERNAL_ERROR)
		error_code = getattr(e, "error_code", "EXECUTION_ERROR")
		error_message = str(e)
		doc.mark_failed(error_code=error_code, error_message=error_message, error_category=error_category)
		return False

def get_integration_metrics():
	"""
	Returns dictionary with operational observability metrics:
	- counts_by_status
	- counts_by_provider
	- counts_by_channel
	- oldest_pending
	- avg_attempts
	"""
	status_rows = frappe.db.sql(
		"""
		SELECT status, count(*) as count
		FROM `tabIntegration Event`
		GROUP BY status
		""",
		as_dict=True,
	)
	counts_by_status = {r.status: r.count for r in status_rows}

	provider_rows = frappe.db.sql(
		"""
		SELECT provider, count(*) as count
		FROM `tabIntegration Event`
		GROUP BY provider
		""",
		as_dict=True,
	)
	counts_by_provider = {r.provider: r.count for r in provider_rows}

	channel_rows = frappe.db.sql(
		"""
		SELECT ifnull(sales_channel, 'Global') as channel, count(*) as count
		FROM `tabIntegration Event`
		GROUP BY sales_channel
		""",
		as_dict=True,
	)
	counts_by_channel = {r.channel: r.count for r in channel_rows}

	oldest_pending = frappe.db.get_value(
		"Integration Event",
		{"status": ["in", [IntegrationStatus.PENDING, IntegrationStatus.RETRY_PENDING]]},
		["name", "event_id", "creation"],
		order_by="creation asc",
		as_dict=True,
	)

	avg_attempts_val = frappe.db.sql(
		"""
		SELECT avg(attempt_count) as avg_att
		FROM `tabIntegration Event`
		"""
	)
	avg_attempts = avg_attempts_val[0][0] if avg_attempts_val and avg_attempts_val[0][0] is not None else 0.0

	return {
		"counts_by_status": counts_by_status,
		"counts_by_provider": counts_by_provider,
		"counts_by_channel": counts_by_channel,
		"oldest_pending": oldest_pending,
		"avg_attempts": float(avg_attempts),
	}

def get_events_by_correlation(correlation_id):
	return frappe.get_all(
		"Integration Event",
		filters={"correlation_id": correlation_id},
		fields=["name", "event_id", "status", "entity_type", "operation", "creation"],
		order_by="creation asc",
	)

def get_events_by_external_id(external_id, provider=None, sales_channel=None):
	filters = {"external_id": external_id}
	if provider:
		filters["provider"] = provider
	if sales_channel:
		filters["sales_channel"] = sales_channel
	return frappe.get_all(
		"Integration Event",
		filters=filters,
		fields=["name", "event_id", "status", "entity_type", "operation", "creation"],
		order_by="creation desc",
	)

