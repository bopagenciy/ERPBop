# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import time
from typing import Any, Dict, List, Optional
import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationStatus,
	ExternalEntityType,
	ErrorCategory,
)
from bop_erp.safety import assert_safe_write_target, ConnectorSafetyError
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.sync import get_active_connector_for_channel
from bop_erp.inventory.publication import process_inventory_publication_event
from bop_erp.reliability import get_database_now


DEFAULT_SCHEDULER_BATCH_LIMIT = 20


def process_pending_inventory_publications(
	sales_channel: str = "TID",
	max_events: int = DEFAULT_SCHEDULER_BATCH_LIMIT,
	worker_id: Optional[str] = None,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Authoritative, bounded worker processor for pending outbound inventory publication events.
	Enforces:
	1. Bounded batch querying: never scans or enqueues unbounded table rows.
	2. Runtime host safety: validates connector host before any socket/HTTP execution.
	3. Disabled connector/channel check: aborts before remote execution if connector is inactive.
	4. Failure isolation: error on one event does not abort processing of remaining events.
	5. Full event lifecycle execution via process_inventory_publication_event.
	6. Structured observability telemetry output (zero secrets).
	"""
	start_time = time.time()
	telemetry = {
		"sales_channel": sales_channel,
		"events_seen": 0,
		"events_claimed": 0,
		"published": 0,
		"no_op": 0,
		"stale": 0,
		"retry_pending": 0,
		"dead_letter": 0,
		"safety_blocked": 0,
		"failed": 0,
		"duration_seconds": 0.0,
		"processed_events": [],
	}

	# 1. Validate connector host and active status pre-flight
	try:
		if client is not None:
			if not client.config.write_enabled:
				telemetry["safety_blocked"] += 1
				telemetry["reason"] = f"PrestaShop connector for channel '{sales_channel}' has writes disabled"
				telemetry["duration_seconds"] = round(time.time() - start_time, 4)
				return telemetry
			assert_safe_write_target(client.config.environment, client.config.base_url)
		else:
			connector = get_active_connector_for_channel(sales_channel)
			if not connector:
				telemetry["safety_blocked"] += 1
				telemetry["reason"] = f"No active PrestaShop connector found for channel '{sales_channel}'"
				telemetry["duration_seconds"] = round(time.time() - start_time, 4)
				return telemetry

			if not connector.write_enabled:
				telemetry["safety_blocked"] += 1
				telemetry["reason"] = f"PrestaShop connector for channel '{sales_channel}' has writes disabled"
				telemetry["duration_seconds"] = round(time.time() - start_time, 4)
				return telemetry

			# Hard Host Safety Guard
			assert_safe_write_target(connector.environment, connector.base_url)

	except ConnectorSafetyError as cs_err:
		telemetry["safety_blocked"] += 1
		telemetry["reason"] = str(cs_err)
		telemetry["duration_seconds"] = round(time.time() - start_time, 4)
		return telemetry
	except Exception as exc:
		telemetry["safety_blocked"] += 1
		telemetry["reason"] = f"Connector pre-flight validation failed: {str(exc)}"
		telemetry["duration_seconds"] = round(time.time() - start_time, 4)
		return telemetry

	# 2. Query bounded claimable events (Pending or due Retry_Pending)
	db_now = get_database_now()
	event_rows = frappe.db.sql(
		"""
		SELECT name, status, next_retry_at
		FROM `tabIntegration Event`
		WHERE sales_channel = %s
		  AND direction = %s
		  AND entity_type = %s
		  AND (
		      status = %s
		      OR (status = %s AND next_retry_at IS NOT NULL AND next_retry_at <= %s)
		  )
		ORDER BY creation ASC
		LIMIT %s
		""",
		(
			sales_channel,
			IntegrationDirection.OUTBOUND,
			ExternalEntityType.INVENTORY,
			IntegrationStatus.PENDING,
			IntegrationStatus.RETRY_PENDING,
			db_now,
			int(max_events),
		),
		as_dict=True,
	)

	telemetry["events_seen"] = len(event_rows)
	if not event_rows:
		telemetry["duration_seconds"] = round(time.time() - start_time, 4)
		return telemetry

	# 3. Process each event with complete failure isolation
	for row in event_rows:
		event_name = row.name
		try:
			res = process_inventory_publication_event(
				event_name=event_name,
				worker_id=worker_id,
				client=client,
			)

			if not res.get("success"):
				reason = res.get("reason", "")
				if "CLAIM_REJECTED" in str(reason):
					# Claim lost to concurrent worker (overlap safe)
					continue

				telemetry["events_claimed"] += 1
				cur_st = frappe.db.get_value("Integration Event", event_name, "status")
				if cur_st == IntegrationStatus.RETRY_PENDING:
					telemetry["retry_pending"] += 1
				elif cur_st == IntegrationStatus.DEAD_LETTER:
					telemetry["dead_letter"] += 1
				else:
					telemetry["failed"] += 1

				telemetry["processed_events"].append({
					"event_name": event_name,
					"status": cur_st or "Failed",
					"error": res.get("error") or reason,
				})
				continue

			telemetry["events_claimed"] += 1

			# Event processed successfully or superseded
			if res.get("superseded"):
				telemetry["stale"] += 1
				telemetry["processed_events"].append({
					"event_name": event_name,
					"status": "SUPERSEDED",
					"reason": res.get("reason"),
				})
			else:
				inner_res = res.get("result", {})
				action_or_status = inner_res.get("status")
				if action_or_status == "Succeeded":
					telemetry["published"] += 1
				elif action_or_status == "No_Op":
					telemetry["no_op"] += 1
				elif "SUPERSEDED" in str(action_or_status):
					telemetry["stale"] += 1
				else:
					telemetry["published"] += 1

				telemetry["processed_events"].append({
					"event_name": event_name,
					"status": action_or_status or "Succeeded",
					"changed": inner_res.get("changed", False),
				})

		except ConnectorSafetyError as cs_err:
			telemetry["safety_blocked"] += 1
			telemetry["processed_events"].append({
				"event_name": event_name,
				"status": "SAFETY_BLOCKED",
				"error": str(cs_err),
			})
		except Exception as exc:
			telemetry["failed"] += 1
			telemetry["processed_events"].append({
				"event_name": event_name,
				"status": "ERROR",
				"error": str(exc)[:300],
			})

	telemetry["duration_seconds"] = round(time.time() - start_time, 4)
	return telemetry


def enqueue_scheduled_inventory_publication(
	sales_channel: str = "TID",
	max_events: int = DEFAULT_SCHEDULER_BATCH_LIMIT,
):
	"""
	Entry point for Frappe scheduler / cron background jobs.
	Dispatches process_pending_inventory_publications onto Frappe background queue.
	"""
	frappe.enqueue(
		"bop_erp.inventory.scheduler.process_pending_inventory_publications",
		queue="default",
		sales_channel=sales_channel,
		max_events=max_events,
		now=frappe.flags.in_test or False,
	)
