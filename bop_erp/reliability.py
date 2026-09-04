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

SENSITIVE_FIELD_STEMS = {
	"authorization",
	"password",
	"passwd",
	"token",
	"secret",
	"apikey",
	"privatekey",
	"clientsecret",
	"cvv",
	"cardnumber",
	"accesstoken",
	"refreshtoken",
}

def is_sensitive_key(key_str):
	"""
	Normalizes key name (lowercasing, stripping all non-alphanumeric chars)
	to avoid false negatives across casing and separator variants (-, _, spaces).
	Solely used for secret detection; does not mutate external identifiers.
	"""
	normalized = "".join(c for c in str(key_str).lower() if c.isalnum())
	return any(stem in normalized for stem in SENSITIVE_FIELD_STEMS)

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
			if is_sensitive_key(k):
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

def claim_event_for_processing(event_name, worker_id=None, lease_timeout_seconds=None):
	"""
	Atomic database-level claim with worker fencing and retry due-time enforcement.
	Transitions an event from claimable states (RECEIVED, PENDING, or due RETRY_PENDING)
	to PROCESSING, assigning worker_id, a fresh UUID processing_token, processing_started_at,
	lease_expires_at, and increments attempt_count.
	Enforces attempt_count < max_attempts.
	Returns (claimed, worker_id, processing_token).
	"""
	if not worker_id:
		worker_id = f"worker-{uuid.uuid4().hex[:12]}"

	processing_token = str(uuid.uuid4())
	now = now_datetime()

	if lease_timeout_seconds is None:
		try:
			timeout_minutes = get_settings().integration_processing_timeout_minutes or 15
			lease_timeout_seconds = int(timeout_minutes) * 60
		except Exception:
			lease_timeout_seconds = 900

	lease_expires_at = now + timedelta(seconds=lease_timeout_seconds)

	# Atomic conditional UPDATE
	frappe.db.sql(
		"""
		UPDATE `tabIntegration Event`
		SET status = %s,
			worker_id = %s,
			processing_token = %s,
			processing_started_at = %s,
			lease_expires_at = %s,
			attempt_count = attempt_count + 1,
			modified = %s
		WHERE name = %s
		  AND attempt_count < max_attempts
		  AND (
			status IN (%s, %s)
			OR (
				status = %s
				AND next_retry_at IS NOT NULL
				AND next_retry_at <= %s
			)
		  )
		""",
		(
			IntegrationStatus.PROCESSING,
			worker_id,
			processing_token,
			now,
			lease_expires_at,
			now,
			event_name,
			IntegrationStatus.RECEIVED,
			IntegrationStatus.PENDING,
			IntegrationStatus.RETRY_PENDING,
			now,
		),
	)

	current_status, current_worker, current_token = frappe.db.get_value(
		"Integration Event", event_name, ["status", "worker_id", "processing_token"]
	) or (None, None, None)

	if current_status == IntegrationStatus.PROCESSING and current_worker == worker_id and current_token == processing_token:
		return True, worker_id, processing_token
	return False, None, None

def recover_stale_events(timeout_minutes=None):
	"""
	Scans for events stuck in PROCESSING where lease_expires_at <= now()
	(or fallback to processing_started_at + timeout_minutes if lease_expires_at is null)
	and transitions them to RETRY_PENDING or DEAD_LETTER.
	Atomically clears worker_id, processing_token, lease_expires_at.
	Idempotent and safe for multi-worker background cron execution.
	"""
	now = now_datetime()
	if timeout_minutes is None:
		try:
			timeout_minutes = get_settings().integration_processing_timeout_minutes or 15
		except Exception:
			timeout_minutes = 15

	fallback_cutoff = now - timedelta(minutes=int(timeout_minutes))

	stale_events = frappe.db.sql(
		"""
		SELECT name, attempt_count, max_attempts, processing_token
		FROM `tabIntegration Event`
		WHERE status = %s
		  AND (
			(lease_expires_at IS NOT NULL AND lease_expires_at <= %s)
			OR (lease_expires_at IS NULL AND processing_started_at IS NOT NULL AND processing_started_at < %s)
		  )
		""",
		(IntegrationStatus.PROCESSING, now, fallback_cutoff),
		as_dict=True,
	)

	recovered_count = 0
	for item in stale_events:
		event_name = item.name
		attempt_count = item.attempt_count or 0
		max_attempts = item.max_attempts or 5
		old_token = item.processing_token

		error_msg = _("Processing lease expired (worker timeout). Recovered by system.")
		next_status = IntegrationStatus.DEAD_LETTER if attempt_count >= max_attempts else IntegrationStatus.RETRY_PENDING

		if next_status == IntegrationStatus.RETRY_PENDING:
			try:
				settings = get_settings()
				delay_seconds = settings.get_backoff_delay(attempt_count)
			except Exception:
				delay_seconds = 60
			next_retry_at = now + timedelta(seconds=delay_seconds)
		else:
			next_retry_at = None

		frappe.db.sql(
			"""
			UPDATE `tabIntegration Event`
			SET status = %s,
				worker_id = NULL,
				processing_token = NULL,
				lease_expires_at = NULL,
				next_retry_at = %s,
				last_error_code = 'LEASE_TIMEOUT',
				last_error_message = %s,
				last_error_at = %s,
				modified = %s
			WHERE name = %s
			  AND status = %s
			  AND (processing_token = %s OR (%s IS NULL AND processing_token IS NULL))
			""",
			(
				next_status,
				next_retry_at,
				error_msg,
				now,
				now,
				event_name,
				IntegrationStatus.PROCESSING,
				old_token,
				old_token,
			),
		)
		updated_status = frappe.db.get_value("Integration Event", event_name, "status")
		if updated_status == next_status:
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
	Executes an Integration Event handler with atomic claim, fencing token verification,
	error classification, and state progression.
	"""
	claimed, worker_id, processing_token = claim_event_for_processing(event_name)
	if not claimed:
		return False

	doc = frappe.get_doc("Integration Event", event_name)

	try:
		if handler:
			result = handler(doc)
		else:
			# Default no-op deterministic success
			result = {"success": True}

		doc.mark_succeeded(processing_token=processing_token, response_metadata=result)
		return True
	except Exception as e:
		error_category = getattr(e, "error_category", ErrorCategory.INTERNAL_ERROR)
		error_code = getattr(e, "error_code", "EXECUTION_ERROR")
		error_message = str(e)
		doc.mark_failed(
			processing_token=processing_token,
			error_code=error_code,
			error_message=error_message,
			error_category=error_category,
		)
		return False

def create_replay_event(original_event_name, new_idempotency_key=None, reason=None, user=None):
	"""
	Creates a NEW Integration Event as a controlled replay of a terminal event.
	The original event remains immutable in its terminal state.
	The new event links back via `replay_of` and begins a fresh lifecycle.
	"""
	original = frappe.get_doc("Integration Event", original_event_name)
	terminal_statuses = {
		IntegrationStatus.SUCCEEDED,
		IntegrationStatus.DEAD_LETTER,
		IntegrationStatus.CANCELLED,
	}
	if original.status not in terminal_statuses:
		frappe.throw(
			_("Cannot replay event '{0}'. Only terminal events ({1}) can be replayed.").format(
				original_event_name, ", ".join(sorted(terminal_statuses))
			),
			frappe.ValidationError,
		)

	if not new_idempotency_key and original.idempotency_key:
		new_idempotency_key = f"{original.idempotency_key}-replay-{uuid.uuid4().hex[:8]}"

	replay_metadata = {}
	if original.request_metadata:
		try:
			replay_metadata = json.loads(original.request_metadata) if isinstance(original.request_metadata, str) else original.request_metadata
		except Exception:
			replay_metadata = {"raw": str(original.request_metadata)}
	if isinstance(replay_metadata, dict):
		replay_metadata["_replay_info"] = {
			"original_event_id": original.event_id,
			"original_name": original.name,
			"replayed_by": user or (getattr(frappe.session, "user", None) if hasattr(frappe, "session") else "Administrator"),
			"replay_reason": reason or "Manual operator replay",
			"replayed_at": str(now_datetime()),
		}

	replay_doc = frappe.get_doc({
		"doctype": "Integration Event",
		"direction": original.direction,
		"provider": original.provider,
		"sales_channel": original.sales_channel,
		"entity_type": original.entity_type,
		"operation": original.operation,
		"status": IntegrationStatus.RECEIVED,
		"external_id": original.external_id,
		"erp_doctype": original.erp_doctype,
		"erp_document": original.erp_document,
		"correlation_id": original.correlation_id,
		"idempotency_key": new_idempotency_key,
		"payload_hash": original.payload_hash,
		"request_metadata": json.dumps(replay_metadata, ensure_ascii=False) if isinstance(replay_metadata, dict) else str(replay_metadata),
		"replay_of": original.name,
		"attempt_count": 0,
		"max_attempts": original.max_attempts or 5,
	})
	replay_doc.insert()
	return replay_doc

def get_existing_idempotent_event(provider, sales_channel, entity_type, operation, idempotency_key, as_doc=False):
	"""
	Deterministically looks up an existing Integration Event by its canonical idempotency tuple.
	Returns IntegrationEvent Document (if as_doc=True) or dict with key fields, or None if not found.
	"""
	if not idempotency_key:
		return None

	active_key = compute_active_idempotency_key(
		provider, sales_channel, entity_type, operation, idempotency_key
	)
	if not active_key:
		return None

	event_name = frappe.db.get_value("Integration Event", {"active_idempotency_key": active_key}, "name")
	if not event_name:
		return None

	if as_doc:
		return frappe.get_doc("Integration Event", event_name)

	return frappe.db.get_value(
		"Integration Event",
		event_name,
		[
			"name",
			"event_id",
			"status",
			"direction",
			"provider",
			"sales_channel",
			"entity_type",
			"operation",
			"correlation_id",
			"idempotency_key",
			"attempt_count",
			"creation",
			"modified",
		],
		as_dict=True,
	)

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

