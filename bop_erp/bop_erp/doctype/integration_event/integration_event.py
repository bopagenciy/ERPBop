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
		IntegrationStatus.CANCELLED,
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
		self.validate_replay_of()
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

	def validate_replay_of(self):
		if self.replay_of:
			if self.replay_of == self.name:
				frappe.throw(_("An event cannot be a replay of itself."))
			if not self.is_new():
				persisted_replay = frappe.db.get_value("Integration Event", self.name, "replay_of")
				if persisted_replay and persisted_replay != self.replay_of:
					frappe.throw(_("Field 'replay_of' is immutable and cannot be modified."))

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
		# Idempotency survives terminal states (CANCELLED, DEAD_LETTER, SUCCEEDED)
		# A received idempotency key remains historically registered forever
		if self.idempotency_key:
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
		claimed, worker, token = claim_event_for_processing(self.name, worker_id=worker_id)
		if claimed:
			self.reload()
			return self.processing_token
		return None

	def _verify_processing_lease(self, processing_token):
		from bop_erp.reliability import verify_processing_authority

		self.reload()
		is_auth, reason = verify_processing_authority(self.name, processing_token)
		if not is_auth:
			frappe.throw(
				_(reason),
				frappe.ValidationError,
			)

	def mark_succeeded(self, processing_token, response_metadata=None):
		self._verify_processing_lease(processing_token)
		self.status = IntegrationStatus.SUCCEEDED
		self.processing_finished_at = now_datetime()
		self.worker_id = None
		self.processing_token = None
		self.lease_expires_at = None
		if response_metadata:
			self.response_metadata = sanitize_metadata(response_metadata)
		self.save()

	def mark_failed(self, processing_token, error_code, error_message, error_category=None, delay_seconds=None):
		self._verify_processing_lease(processing_token)
		now = now_datetime()
		self.last_error_code = error_code
		self.last_error_message = error_message
		self.last_error_at = now
		self.processing_finished_at = now

		# Evaluate retry policy
		is_retryable = error_category in ErrorCategory.RETRYABLE if error_category else True
		can_retry = is_retryable and (self.attempt_count < self.max_attempts)

		if can_retry:
			self._schedule_retry_internal(error_code, error_message, delay_seconds=delay_seconds)
		else:
			self._mark_dead_letter_internal(error_code, error_message)

	def schedule_retry(self, processing_token, error_code, error_message, delay_seconds=None):
		self._verify_processing_lease(processing_token)
		self._schedule_retry_internal(error_code, error_message, delay_seconds=delay_seconds)

	def mark_dead_letter(self, processing_token, error_code, error_message):
		self._verify_processing_lease(processing_token)
		self._mark_dead_letter_internal(error_code, error_message)

	def _schedule_retry_internal(self, error_code, error_message, delay_seconds=None):
		now = now_datetime()
		self.last_error_code = error_code
		self.last_error_message = error_message
		self.last_error_at = now
		self.status = IntegrationStatus.RETRY_PENDING
		self.worker_id = None
		self.processing_token = None
		self.lease_expires_at = None

		if delay_seconds is None:
			try:
				settings = get_settings()
				delay_seconds = settings.get_backoff_delay(self.attempt_count)
			except Exception:
				delay_seconds = 60

		self.next_retry_at = now + timedelta(seconds=int(delay_seconds))
		self.save()

	def _mark_dead_letter_internal(self, error_code, error_message):
		now = now_datetime()
		self.last_error_code = error_code
		self.last_error_message = error_message
		self.last_error_at = now
		self.status = IntegrationStatus.DEAD_LETTER
		self.worker_id = None
		self.processing_token = None
		self.lease_expires_at = None
		self.next_retry_at = None
		self.save()

	def cancel(self, reason=None, processing_token=None):
		old_status = frappe.db.get_value("Integration Event", self.name, "status")
		if old_status == IntegrationStatus.PROCESSING or processing_token:
			self._verify_processing_lease(processing_token)
		self.status = IntegrationStatus.CANCELLED
		self.worker_id = None
		self.processing_token = None
		self.lease_expires_at = None
		self.next_retry_at = None
		self.processing_finished_at = now_datetime()
		# Idempotency key remains reserved
		if reason:
			self.last_error_message = _("Cancelled: {0}").format(reason)
		self.save()

	def associate_erp_document(self, erp_doctype, erp_document):
		self.erp_doctype = erp_doctype
		self.erp_document = erp_document
		self.flags.ignore_links = True
		self.save()
