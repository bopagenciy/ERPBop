# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from datetime import timedelta
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime
from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	ErrorCategory,
	ExternalEntityType,
)
from bop_erp.reliability import (
	compute_active_idempotency_key,
	compute_payload_hash,
	sanitize_metadata,
	claim_event_for_processing,
	recover_stale_events,
	process_integration_event,
	get_integration_metrics,
	get_events_by_correlation,
	get_events_by_external_id,
	create_replay_event,
	get_existing_idempotent_event,
)
from frappe.translate import get_translations_from_apps

class TestIntegrationReliability(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		self.ensure_channels()

	def ensure_channels(self):
		for ch in ["TID", "BAMAL"]:
			if not frappe.db.exists("Sales Channel", ch):
				frappe.get_doc({
					"doctype": "Sales Channel",
					"channel_id": ch,
					"channel_name": ch,
					"channel_type": "PRESTASHOP",
					"company": self.company,
					"active": 1,
				}).insert()

	def test_event_creation_and_immutable_event_id(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		self.assertTrue(doc.event_id)
		self.assertEqual(len(doc.event_id), 36) # Standard UUID length

		# Attempting to modify event_id must raise ValidationError
		doc.event_id = "TAMPERED-EVENT-ID-12345"
		self.assertRaises(frappe.ValidationError, doc.save)

		frappe.delete_doc("Integration Event", doc.name)

	def test_valid_state_transitions(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.WEBHOOK,
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		# RECEIVED -> PENDING
		doc.status = IntegrationStatus.PENDING
		doc.save()
		self.assertEqual(doc.status, IntegrationStatus.PENDING)

		# PENDING -> PROCESSING
		doc.status = IntegrationStatus.PROCESSING
		doc.save()
		self.assertEqual(doc.status, IntegrationStatus.PROCESSING)

		# PROCESSING -> SUCCEEDED
		doc.status = IntegrationStatus.SUCCEEDED
		doc.save()
		self.assertEqual(doc.status, IntegrationStatus.SUCCEEDED)

		frappe.delete_doc("Integration Event", doc.name)

	def test_invalid_state_transition_rejected(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.WEBHOOK,
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		# Invalid jump: RECEIVED -> SUCCEEDED
		doc.status = IntegrationStatus.SUCCEEDED
		self.assertRaises(frappe.ValidationError, doc.save)

		frappe.delete_doc("Integration Event", doc.name)

	def test_correlation_and_erp_reference_persistence(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.CUSTOMER,
			"operation": IntegrationOperation.CREATE,
			"correlation_id": "CORR-UNIQUE-9999",
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		doc.associate_erp_document("Company", self.company)
		self.assertEqual(doc.erp_doctype, "Company")
		self.assertEqual(doc.erp_document, self.company)

		events = get_events_by_correlation("CORR-UNIQUE-9999")
		self.assertEqual(len(events), 1)
		self.assertEqual(events[0].name, doc.name)

		frappe.delete_doc("Integration Event", doc.name)

	def test_idempotency_duplicate_rejected(self):
		e1 = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-ORDER-1001",
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		# Same provider, channel, entity, operation, key must be rejected
		e2 = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-ORDER-1001",
			"status": IntegrationStatus.RECEIVED,
		})
		self.assertRaises(frappe.ValidationError, e2.insert)

		frappe.delete_doc("Integration Event", e1.name)

	def test_idempotency_same_key_allowed_across_channels(self):
		e1 = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-SHARED-KEY",
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		e2 = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "BAMAL",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-SHARED-KEY",
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		self.assertTrue(e1.name and e2.name)
		self.assertNotEqual(e1.active_idempotency_key, e2.active_idempotency_key)

		frappe.delete_doc("Integration Event", e1.name)
		frappe.delete_doc("Integration Event", e2.name)

	def test_idempotency_same_key_allowed_across_providers(self):
		e1 = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-PROV-KEY",
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		e2 = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.EDI,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-PROV-KEY",
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		self.assertTrue(e1.name and e2.name)
		frappe.delete_doc("Integration Event", e1.name)
		frappe.delete_doc("Integration Event", e2.name)

	def test_concurrent_duplicate_idempotency_key_blocked_at_db(self):
		e1 = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-DB-CONC-1",
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		# Bypass Frappe validate() to simulate concurrent DB race
		e2 = frappe.get_doc({
			"doctype": "Integration Event",
			"event_id": "different-uuid-conc",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "DIFFERENT-KEY",
			"active_idempotency_key": e1.active_idempotency_key, # Injected collision
			"status": IntegrationStatus.RECEIVED,
		})
		self.assertRaises(Exception, e2.db_insert)

		frappe.delete_doc("Integration Event", e1.name)

	def test_atomic_claim_and_second_worker_blocked(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
		}).insert()

		# Worker 1 claims
		claimed_1, worker_1, token_1 = claim_event_for_processing(doc.name, worker_id="WORKER-ALPHA")
		self.assertTrue(claimed_1)
		self.assertEqual(worker_1, "WORKER-ALPHA")
		self.assertTrue(token_1)

		# Worker 2 attempts claim on same event -> must fail
		claimed_2, worker_2, token_2 = claim_event_for_processing(doc.name, worker_id="WORKER-BETA")
		self.assertFalse(claimed_2)
		self.assertIsNone(worker_2)
		self.assertIsNone(token_2)

		frappe.delete_doc("Integration Event", doc.name)

	def test_processing_success_transition(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
		}).insert()

		def test_handler(event_doc):
			return {"synced_order": "SO-12345"}

		success = process_integration_event(doc.name, handler=test_handler)
		self.assertTrue(success)

		doc.reload()
		self.assertEqual(doc.status, IntegrationStatus.SUCCEEDED)
		self.assertIsNone(doc.worker_id)
		self.assertTrue(doc.processing_finished_at)
		self.assertIn("SO-12345", doc.response_metadata)

		frappe.delete_doc("Integration Event", doc.name)

	def test_processing_retryable_failure(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
			"attempt_count": 0,
			"max_attempts": 3,
		}).insert()

		class TransientError(Exception):
			error_category = ErrorCategory.TRANSIENT
			error_code = "NETWORK_TIMEOUT"

		def failing_handler(event_doc):
			raise TransientError("Connection timed out to provider.")

		success = process_integration_event(doc.name, handler=failing_handler)
		self.assertFalse(success)

		doc.reload()
		self.assertEqual(doc.status, IntegrationStatus.RETRY_PENDING)
		self.assertEqual(doc.attempt_count, 1)
		self.assertEqual(doc.last_error_code, "NETWORK_TIMEOUT")
		self.assertTrue(doc.next_retry_at)
		self.assertIsNone(doc.worker_id)

		frappe.delete_doc("Integration Event", doc.name)

	def test_processing_non_retryable_failure(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
			"attempt_count": 0,
			"max_attempts": 5,
		}).insert()

		class ValidationError(Exception):
			error_category = ErrorCategory.VALIDATION
			error_code = "SCHEMA_INVALID"

		def failing_handler(event_doc):
			raise ValidationError("Invalid JSON payload structure.")

		success = process_integration_event(doc.name, handler=failing_handler)
		self.assertFalse(success)

		doc.reload()
		self.assertEqual(doc.status, IntegrationStatus.DEAD_LETTER)
		self.assertEqual(doc.last_error_code, "SCHEMA_INVALID")
		self.assertIsNone(doc.next_retry_at)

		frappe.delete_doc("Integration Event", doc.name)

	def test_max_attempts_leads_to_dead_letter(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
			"attempt_count": 4,
			"max_attempts": 5,
		}).insert()

		class RetryableError(Exception):
			error_category = ErrorCategory.TRANSIENT
			error_code = "TEMP_UNAVAILABLE"

		def failing_handler(event_doc):
			raise RetryableError("Server busy.")

		# Claim increments attempt_count to 5 (equal to max_attempts)
		process_integration_event(doc.name, handler=failing_handler)

		doc.reload()
		self.assertEqual(doc.attempt_count, 5)
		self.assertEqual(doc.status, IntegrationStatus.DEAD_LETTER)

		frappe.delete_doc("Integration Event", doc.name)

	def test_stale_processing_recovery(self):
		# Create a stale event (started 30 minutes ago)
		stale_doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
		}).insert()
		claim_event_for_processing(stale_doc.name, worker_id="DEAD-WORKER")

		past_time = now_datetime() - timedelta(minutes=30)
		frappe.db.set_value("Integration Event", stale_doc.name, {
			"processing_started_at": past_time,
			"lease_expires_at": past_time,
		})

		# Create fresh event (started 2 minutes ago)
		fresh_doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
		}).insert()
		claim_event_for_processing(fresh_doc.name, worker_id="ALIVE-WORKER")

		# Run stale recovery with 15 min threshold
		recovered = recover_stale_events(timeout_minutes=15)
		self.assertEqual(recovered, 1)

		stale_doc.reload()
		self.assertEqual(stale_doc.status, IntegrationStatus.RETRY_PENDING)
		self.assertEqual(stale_doc.last_error_code, "LEASE_TIMEOUT")
		self.assertIsNone(stale_doc.worker_id)
		self.assertIsNone(stale_doc.processing_token)
		self.assertIsNone(stale_doc.lease_expires_at)

		fresh_doc.reload()
		self.assertEqual(fresh_doc.status, IntegrationStatus.PROCESSING)
		self.assertEqual(fresh_doc.worker_id, "ALIVE-WORKER")

		# Idempotence: running recovery again recovers 0 events
		self.assertEqual(recover_stale_events(timeout_minutes=15), 0)

		frappe.delete_doc("Integration Event", stale_doc.name)
		frappe.delete_doc("Integration Event", fresh_doc.name)

	def test_security_metadata_redaction(self):
		raw_metadata = {
			"authorization": "Bearer super-secret-token-12345",
			"password": "db_password_cleartext",
			"client_secret": "my-client-secret-999",
			"order_id": "ORD-100",
			"customer_email": "test@example.com",
			"nested": {
				"api_key": "api_secret_key_abc",
				"items_count": 5,
			},
		}

		sanitized = sanitize_metadata(raw_metadata)
		parsed = json.loads(sanitized)

		self.assertEqual(parsed["authorization"], "[REDACTED]")
		self.assertEqual(parsed["password"], "[REDACTED]")
		self.assertEqual(parsed["client_secret"], "[REDACTED]")
		self.assertEqual(parsed["nested"]["api_key"], "[REDACTED]")
		self.assertEqual(parsed["order_id"], "ORD-100")
		self.assertEqual(parsed["nested"]["items_count"], 5)

	def test_security_metadata_size_limit(self):
		huge_dict = {"data": "A" * 6000}
		sanitized = sanitize_metadata(huge_dict, max_length=500)
		self.assertTrue(len(sanitized) <= 550)
		self.assertIn("... [TRUNCATED]", sanitized)

	def test_payload_hashing(self):
		payload1 = {"b_key": 2, "a_key": 1, "token": "secret123"}
		payload2 = {"a_key": 1, "b_key": 2, "token": "different_secret"}

		# token is redacted before hashing; keys are sorted deterministically
		hash1 = compute_payload_hash(payload1)
		hash2 = compute_payload_hash(payload2)

		self.assertEqual(len(hash1), 64)
		self.assertEqual(hash1, hash2) # Both have [REDACTED] for token and same canonical keys

	def test_observability_metrics(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
		}).insert()

		metrics = get_integration_metrics()
		self.assertIn(IntegrationStatus.PENDING, metrics["counts_by_status"])
		self.assertIn("PRESTASHOP", metrics["counts_by_provider"])
		self.assertIn("TID", metrics["counts_by_channel"])

		frappe.delete_doc("Integration Event", doc.name)

	def test_stale_worker_fencing_token_rejected(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
		}).insert()

		# Worker A claims
		claimed_a, worker_a, token_a = claim_event_for_processing(doc.name, worker_id="WORKER-ALPHA")
		self.assertTrue(claimed_a)
		self.assertTrue(token_a)

		# Simulate lease expiration in past
		past = now_datetime() - timedelta(minutes=20)
		frappe.db.set_value("Integration Event", doc.name, {
			"processing_started_at": past,
			"lease_expires_at": past,
		})

		# System recovery recovers stale event to RETRY_PENDING
		recovered = recover_stale_events(timeout_minutes=15)
		self.assertEqual(recovered, 1)

		# Allow immediate retry for Worker B
		frappe.db.set_value("Integration Event", doc.name, "next_retry_at", now_datetime() - timedelta(seconds=1))

		# Worker B claims
		claimed_b, worker_b, token_b = claim_event_for_processing(doc.name, worker_id="WORKER-BETA")
		self.assertTrue(claimed_b)
		self.assertTrue(token_b)
		self.assertNotEqual(token_a, token_b)

		# Worker A attempts to mark succeeded using stale token A -> MUST BE REJECTED
		doc_a = frappe.get_doc("Integration Event", doc.name)
		self.assertRaises(
			frappe.ValidationError,
			doc_a.mark_succeeded,
			processing_token=token_a,
			response_metadata={"worker": "ALPHA"},
		)

		# Worker B marks succeeded with token B -> SUCCEEDS
		doc_b = frappe.get_doc("Integration Event", doc.name)
		doc_b.mark_succeeded(processing_token=token_b, response_metadata={"worker": "BETA"})

		doc.reload()
		self.assertEqual(doc.status, IntegrationStatus.SUCCEEDED)
		self.assertIsNone(doc.worker_id)
		self.assertIsNone(doc.processing_token)
		self.assertIn("BETA", doc.response_metadata)

		frappe.delete_doc("Integration Event", doc.name)

	def test_retry_due_time_enforcement(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
		}).insert()

		# Transition to RETRY_PENDING with future next_retry_at (1 hour in future)
		future_retry = now_datetime() + timedelta(hours=1)
		frappe.db.set_value("Integration Event", doc.name, {
			"status": IntegrationStatus.RETRY_PENDING,
			"next_retry_at": future_retry,
		})

		# Claim before due time MUST be rejected
		claimed_early, worker_early, token_early = claim_event_for_processing(doc.name, worker_id="EARLY-WORKER")
		self.assertFalse(claimed_early)
		self.assertIsNone(token_early)

		# Set next_retry_at to past (due now)
		past_retry = now_datetime() - timedelta(minutes=2)
		frappe.db.set_value("Integration Event", doc.name, "next_retry_at", past_retry)

		# Claim at or after due time MUST be accepted
		claimed_due, worker_due, token_due = claim_event_for_processing(doc.name, worker_id="DUE-WORKER")
		self.assertTrue(claimed_due)
		self.assertEqual(worker_due, "DUE-WORKER")
		self.assertTrue(token_due)

		doc.reload()
		self.assertEqual(doc.status, IntegrationStatus.PROCESSING)

		frappe.delete_doc("Integration Event", doc.name)

	def test_attempt_count_boundary_and_dead_letter(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.SYNC,
			"status": IntegrationStatus.PENDING,
			"attempt_count": 4,
			"max_attempts": 5,
		}).insert()

		# Claim #5 (attempt 5 out of 5)
		claimed_5, worker_5, token_5 = claim_event_for_processing(doc.name, worker_id="WORKER-5")
		self.assertTrue(claimed_5)

		doc.reload()
		self.assertEqual(doc.attempt_count, 5)

		# Fail attempt 5 -> must transition directly to DEAD_LETTER
		doc.mark_failed(
			processing_token=token_5,
			error_code="TIMEOUT",
			error_message="Attempt 5 timed out",
			error_category=ErrorCategory.TRANSIENT,
		)

		doc.reload()
		self.assertEqual(doc.status, IntegrationStatus.DEAD_LETTER)
		self.assertIsNone(doc.next_retry_at)

		# Attempting claim #6 must be completely impossible
		claimed_6, worker_6, token_6 = claim_event_for_processing(doc.name, worker_id="WORKER-6")
		self.assertFalse(claimed_6)

		frappe.delete_doc("Integration Event", doc.name)

	def test_terminal_idempotency_survives_cancelled(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-TERM-CANCEL-1",
			"status": IntegrationStatus.PENDING,
		}).insert()

		doc.cancel(reason="Operator cancelled")
		self.assertEqual(doc.status, IntegrationStatus.CANCELLED)
		self.assertTrue(doc.active_idempotency_key)

		# Duplicate arrival with same idempotency key MUST be rejected
		dup = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-TERM-CANCEL-1",
			"status": IntegrationStatus.RECEIVED,
		})
		self.assertRaises(frappe.ValidationError, dup.insert)

		frappe.delete_doc("Integration Event", doc.name)

	def test_terminal_idempotency_survives_dead_letter(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-TERM-DEAD-1",
			"status": IntegrationStatus.PENDING,
		}).insert()

		claimed, worker, token = claim_event_for_processing(doc.name)
		doc.mark_dead_letter(processing_token=token, error_code="FATAL", error_message="Unrecoverable")

		self.assertEqual(doc.status, IntegrationStatus.DEAD_LETTER)
		self.assertTrue(doc.active_idempotency_key)

		# Duplicate arrival with same idempotency key MUST be rejected
		dup = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-TERM-DEAD-1",
			"status": IntegrationStatus.RECEIVED,
		})
		self.assertRaises(frappe.ValidationError, dup.insert)

		frappe.delete_doc("Integration Event", doc.name)

	def test_terminal_idempotency_survives_succeeded(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-TERM-SUCC-1",
			"status": IntegrationStatus.PENDING,
		}).insert()

		claimed, worker, token = claim_event_for_processing(doc.name)
		doc.mark_succeeded(processing_token=token)

		self.assertEqual(doc.status, IntegrationStatus.SUCCEEDED)
		self.assertTrue(doc.active_idempotency_key)

		# Duplicate arrival with same idempotency key MUST be rejected
		dup = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-TERM-SUCC-1",
			"status": IntegrationStatus.RECEIVED,
		})
		self.assertRaises(frappe.ValidationError, dup.insert)

		frappe.delete_doc("Integration Event", doc.name)

	def test_controlled_replay_foundation(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-REPLAY-BASE",
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps({"order_id": 9999}),
		}).insert()

		claimed, worker, token = claim_event_for_processing(doc.name)
		doc.mark_succeeded(processing_token=token)

		# Create controlled replay
		replay = create_replay_event(doc.name, reason="Customer re-requested order sync")
		self.assertNotEqual(replay.name, doc.name)
		self.assertNotEqual(replay.event_id, doc.event_id)
		self.assertEqual(replay.replay_of, doc.name)
		self.assertEqual(replay.status, IntegrationStatus.RECEIVED)
		self.assertEqual(replay.attempt_count, 0)
		self.assertTrue(replay.idempotency_key.startswith("IDEMP-REPLAY-BASE-replay-"))

		# Original event remains unmodified in terminal SUCCEEDED state
		doc.reload()
		self.assertEqual(doc.status, IntegrationStatus.SUCCEEDED)

		# Attempting to tamper with replay_of is rejected
		replay.replay_of = "TAMPERED-LINK"
		self.assertRaises(frappe.ValidationError, replay.save)

		# Attempting to replay a non-terminal event must be rejected
		pending_doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"status": IntegrationStatus.RECEIVED,
		}).insert()
		self.assertRaises(frappe.ValidationError, create_replay_event, pending_doc.name)

		frappe.delete_doc("Integration Event", replay.name)
		frappe.delete_doc("Integration Event", pending_doc.name)
		frappe.delete_doc("Integration Event", doc.name)

	def test_duplicate_event_lookup_helper(self):
		doc = frappe.get_doc({
			"doctype": "Integration Event",
			"direction": IntegrationDirection.INBOUND,
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": "TID",
			"entity_type": ExternalEntityType.ORDER,
			"operation": IntegrationOperation.CREATE,
			"idempotency_key": "IDEMP-LOOKUP-EXISTS",
			"status": IntegrationStatus.RECEIVED,
		}).insert()

		lookup = get_existing_idempotent_event(
			IntegrationProvider.PRESTASHOP,
			"TID",
			ExternalEntityType.ORDER,
			IntegrationOperation.CREATE,
			"IDEMP-LOOKUP-EXISTS",
		)
		self.assertIsNotNone(lookup)
		self.assertEqual(lookup["event_id"], doc.event_id)
		self.assertEqual(lookup["status"], IntegrationStatus.RECEIVED)

		# Nonexistent key returns None
		none_lookup = get_existing_idempotent_event(
			IntegrationProvider.PRESTASHOP,
			"TID",
			ExternalEntityType.ORDER,
			IntegrationOperation.CREATE,
			"IDEMP-NON-EXISTENT",
		)
		self.assertIsNone(none_lookup)

		frappe.delete_doc("Integration Event", doc.name)

	def test_hardened_secret_redaction_variants(self):
		complex_metadata = {
			"Authorization": "Bearer secret-auth-header",
			"authorization": "Basic secret-lower",
			"X-API-Key": "my-x-api-key",
			"x_api_key": "my-x-api-key-2",
			"api-key": "my-api-key-dash",
			"ApiKey": "my-camel-api-key",
			"access_token": "token-xyz-1",
			"refresh_token": "token-xyz-2",
			"client-secret": "client-secret-val",
			"private-key": "priv-key-val",
			"password": "pass-clear",
			"passwd": "passwd-clear",
			"card_number": "4111111111111111",
			"card-number": "4111111111111111",
			"cvv": "999",
			"order_id": "ORD-12345",
			"customer_name": "Acme Industrial Corp",
			"total_amount": 1500.50,
		}

		sanitized = sanitize_metadata(complex_metadata)
		parsed = json.loads(sanitized)

		# Verify all secrets are redacted
		self.assertEqual(parsed["Authorization"], "[REDACTED]")
		self.assertEqual(parsed["authorization"], "[REDACTED]")
		self.assertEqual(parsed["X-API-Key"], "[REDACTED]")
		self.assertEqual(parsed["x_api_key"], "[REDACTED]")
		self.assertEqual(parsed["api-key"], "[REDACTED]")
		self.assertEqual(parsed["ApiKey"], "[REDACTED]")
		self.assertEqual(parsed["access_token"], "[REDACTED]")
		self.assertEqual(parsed["refresh_token"], "[REDACTED]")
		self.assertEqual(parsed["client-secret"], "[REDACTED]")
		self.assertEqual(parsed["private-key"], "[REDACTED]")
		self.assertEqual(parsed["password"], "[REDACTED]")
		self.assertEqual(parsed["passwd"], "[REDACTED]")
		self.assertEqual(parsed["card_number"], "[REDACTED]")
		self.assertEqual(parsed["card-number"], "[REDACTED]")
		self.assertEqual(parsed["cvv"], "[REDACTED]")

		# Verify business data is completely preserved
		self.assertEqual(parsed["order_id"], "ORD-12345")
		self.assertEqual(parsed["customer_name"], "Acme Industrial Corp")
		self.assertEqual(parsed["total_amount"], 1500.50)

	def test_i18n_translation_and_language_independence(self):
		# Verify Spanish translations are loaded for bop_erp
		translations = get_translations_from_apps("es", ["bop_erp"])
		self.assertTrue(len(translations) >= 25)

		self.assertEqual(translations.get("Sales Channel"), "Canal de Ventas")
		self.assertEqual(translations.get("Integration Event"), "Evento de Integración")
		self.assertEqual(translations.get("Processing Token"), "Token de Procesamiento")
		self.assertEqual(translations.get("Lease Expires At"), "Expiración del Arriendo")
		self.assertEqual(translations.get("Replay Of"), "Replay de")

		# Verify technical identities remain English constants
		self.assertEqual(IntegrationStatus.SUCCEEDED, "SUCCEEDED")
		self.assertEqual(IntegrationStatus.PROCESSING, "PROCESSING")
		self.assertEqual(IntegrationStatus.DEAD_LETTER, "DEAD_LETTER")
		self.assertEqual(IntegrationDirection.INBOUND, "INBOUND")
