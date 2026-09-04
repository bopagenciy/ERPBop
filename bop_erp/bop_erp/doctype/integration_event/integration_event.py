# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import uuid
from datetime import timedelta
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime
from bop_erp.constants import (
	IntegrationStatus,
	ErrorCategory,
)
from bop_erp.reliability import (
	compute_active_idempotency_key,
	sanitize_metadata,
	claim_event_for_processing,
)
from bop_erp.bop_erp.doctype.bop_erp_settings.bop_erp_settings import get_settings

VALID_TRANSITIONS = {
	IntegrationStatus.RECEIVED: {
		IntegrationStatus.PENDING,
		IntegrationStatus.PROCESSING,
		IntegrationStatus.CANCELLED,
	},
	IntegrationStatus.PENDING: {
		IntegrationStatus.PROCESSING,
		IntegrationStatus.CANCELLED,
	},
	IntegrationStatus.PROCESSING: {
		IntegrationStatus.SUCCEEDED,
		IntegrationStatus.FAILED,
		IntegrationStatus.RETRY_PENDING,
		IntegrationStatus.DEAD_LETTER,
	},
	IntegrationStatus.FAILED: {
		IntegrationStatus.RETRY_PENDING,
		IntegrationStatus.DEAD_LETTER,
	},
	IntegrationStatus.RETRY_PENDING: {
		IntegrationStatus.PROCESSING,
		IntegrationStatus.DEAD_LETTER,
		IntegrationStatus.CANCELLED,
	},
	IntegrationStatus.SUCCEEDED: set(),
	IntegrationStatus.DEAD_LETTER: set(),
	IntegrationStatus.CANCELLED: set(),
}

class IntegrationEvent(Document):
	def validate(self):
		self.ensure_event_id()
		self.validate_state_transition()
		self.sanitize_payload_and_metadata()
		self.set_active_idempotency_key()
		self.validate_idempotency_uniqueness()

	def ensure_event_id(self):
		if not self.event_id:
			self.event_id = str(uuid.uuid4())
		elif not self.is_new():
			# Verify immutability against database state
			persisted_event_id = frappe.db.get_value("Integration Event", self.name, "event_id")
			if persisted_event_id and persisted_event_id != self.event_id:
				frappe.throw(_("Event ID is immutable and cannot be modified."))

	def validate_state_transition(self):
		if self.is_new():
			return

		old_status = frappe.db.get_value("Integration Event", self.name, "status")
		if not old_status or old_status == self.status:
			return

		allowed = VALID_TRANSITIONS.get(old_status, set())
		if self.status not in allowed:
			frappe.throw(
				_("Invalid Integration Event state transition from '{0}' to '{1}'.").format(
					old_status, self.status
				)
			)

	def sanitize_payload_and_metadata(self):
		if self.request_metadata:
			self.request_metadata = sanitize_metadata(self.request_metadata)
		if self.response_metadata:
			self.response_metadata = sanitize_metadata(self.response_metadata)

	def set_active_idempotency_key(self):
		if self.idempotency_key and self.status != IntegrationStatus.CANCELLED:
			self.active_idempotency_key = compute_active_idempotency_key(
				self.provider,
				self.sales_channel,
				self.entity_type,
				self.operation,
				self.idempotency_key,
			)
		else:
			self.active_idempotency_key = None

	def validate_idempotency_uniqueness(self):
		if not self.active_idempotency_key:
			return

		existing = frappe.db.get_value(
			"Integration Event",
			{"active_idempotency_key": self.active_idempotency_key},
			["name", "event_id", "status"],
			as_dict=True,
		)
		if existing and existing.name != self.name:
			frappe.throw(
				_(
					"Idempotency violation: An active Integration Event already exists for Provider '{0}', "
					"Channel '{1}', Type '{2}', Operation '{3}', and Key '{4}' (Event ID: {5}, Status: {6})."
				).format(
					self.provider,
					self.sales_channel or "Global",
					self.entity_type,
					self.operation,
					self.idempotency_key,
					existing.event_id,
					existing.status,
				)
			)

	def start_processing(self, worker_id=None):
		claimed, worker = claim_event_for_processing(self.name, worker_id=worker_id)
		if claimed:
			self.reload()
			return True
		return False

	def mark_succeeded(self, response_metadata=None):
		if self.status != IntegrationStatus.PROCESSING:
			self.status = IntegrationStatus.PROCESSING
		self.status = IntegrationStatus.SUCCEEDED
		self.processing_finished_at = now_datetime()
		self.worker_id = None
		if response_metadata:
			self.response_metadata = sanitize_metadata(response_metadata)
		self.save()

	def mark_failed(self, error_code, error_message, error_category=None):
		now = now_datetime()
		self.last_error_code = error_code
		self.last_error_message = error_message
		self.last_error_at = now
		self.processing_finished_at = now

		# Evaluate retry policy
		is_retryable = error_category in ErrorCategory.RETRYABLE if error_category else True
		can_retry = is_retryable and (self.attempt_count < self.max_attempts)

		if can_retry:
			self.schedule_retry(error_code, error_message)
		else:
			self.mark_dead_letter(error_code, error_message)

	def schedule_retry(self, error_code, error_message):
		self.last_error_code = error_code
		self.last_error_message = error_message
		self.last_error_at = now_datetime()
		self.status = IntegrationStatus.RETRY_PENDING
		self.worker_id = None

		try:
			settings = get_settings()
			delay_seconds = settings.get_backoff_delay(self.attempt_count)
		except Exception:
			delay_seconds = 60

		self.next_retry_at = now_datetime() + timedelta(seconds=delay_seconds)
		self.save()

	def mark_dead_letter(self, error_code, error_message):
		self.last_error_code = error_code
		self.last_error_message = error_message
		self.last_error_at = now_datetime()
		self.status = IntegrationStatus.DEAD_LETTER
		self.worker_id = None
		self.next_retry_at = None
		self.save()

	def cancel(self, reason=None):
		self.status = IntegrationStatus.CANCELLED
		self.worker_id = None
		self.next_retry_at = None
		self.active_idempotency_key = None
		if reason:
			self.last_error_message = f"Cancelled: {reason}"
		self.save()

	def associate_erp_document(self, erp_doctype, erp_document):
		self.erp_doctype = erp_doctype
		self.erp_document = erp_document
		self.save()
