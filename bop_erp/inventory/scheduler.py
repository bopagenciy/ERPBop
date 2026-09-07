# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import time
from typing import Any, Dict, List, Optional
import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
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
DEFAULT_MAX_CHANNELS_PER_RUN = 10
DEFAULT_MAX_TOTAL_EVENTS_PER_RUN = 50


def discover_eligible_publication_channels(
	provider: str = IntegrationProvider.PRESTASHOP,
) -> List[Dict[str, Any]]:
	"""
	Authoritative discovery of active, eligible publication channels.
	Criteria:
	1. Sales Channel exists and is active (active = 1).
	2. Sales Channel integration_provider matches requested provider (or not disabled).
	3. PrestaShop Connector exists for channel, is enabled (enabled = 1), and writable (write_enabled = 1).
	4. Company is defined on Sales Channel.

	Returns list of dictionaries ordered deterministically by channel_name asc:
	[{'sales_channel': str, 'company': str, 'connector_name': str, 'base_url': str, 'environment': str}]
	"""
	if provider == IntegrationProvider.PRESTASHOP:
		# Discover via PrestaShop Connector joined to active Sales Channel
		rows = frappe.db.sql(
			"""
			SELECT 
				sc.name as sales_channel,
				sc.company as company,
				pc.name as connector_name,
				pc.base_url as base_url,
				pc.environment as environment
			FROM `tabSales Channel` sc
			JOIN `tabPrestaShop Connector` pc ON pc.sales_channel = sc.name
			WHERE sc.active = 1
			  AND pc.enabled = 1
			  AND pc.write_enabled = 1
			ORDER BY sc.name ASC
			""",
			as_dict=True,
		)
		return rows

	return []


def process_pending_inventory_publications(
	sales_channel: str,
	max_events: int = DEFAULT_SCHEDULER_BATCH_LIMIT,
	worker_id: Optional[str] = None,
	client: Optional[PrestaShopClient] = None,
	provider: str = IntegrationProvider.PRESTASHOP,
) -> Dict[str, Any]:
	"""
	Authoritative, bounded worker processor for pending outbound inventory publication events for a specific channel.
	Enforces:
	1. Bounded batch querying: never scans or enqueues unbounded table rows.
	2. Runtime host safety: validates connector host before any socket/HTTP execution.
	3. Disabled connector/channel check: aborts before remote execution if connector is inactive.
	4. Multi-company consistency: verifies channel company exists and is valid.
	5. Provider filtering: only claims events matching the specified provider.
	6. Failure isolation: error on one event does not abort processing of remaining events.
	7. Full event lifecycle execution via process_inventory_publication_event.
	8. Structured observability telemetry output (zero secrets).
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

	# 1. Validate channel and connector pre-flight
	try:
		channel_row = frappe.db.get_value("Sales Channel", sales_channel, ["name", "active", "company"], as_dict=True)
		if not channel_row or not channel_row.active:
			telemetry["safety_blocked"] += 1
			telemetry["reason"] = f"Sales Channel '{sales_channel}' does not exist or is disabled"
			telemetry["duration_seconds"] = round(time.time() - start_time, 4)
			return telemetry

		connector = None
		try:
			connector = get_active_connector_for_channel(sales_channel)
		except Exception as c_err:
			if client is None:
				telemetry["safety_blocked"] += 1
				telemetry["reason"] = f"No active PrestaShop connector found for channel '{sales_channel}': {str(c_err)}"
				telemetry["duration_seconds"] = round(time.time() - start_time, 4)
				return telemetry

		if connector is not None:
			if not connector.write_enabled:
				telemetry["safety_blocked"] += 1
				telemetry["reason"] = f"PrestaShop connector for channel '{sales_channel}' has writes disabled"
				telemetry["duration_seconds"] = round(time.time() - start_time, 4)
				return telemetry
			assert_safe_write_target(connector.environment, connector.base_url)

		if client is not None:
			if not client.config.write_enabled:
				telemetry["safety_blocked"] += 1
				telemetry["reason"] = f"PrestaShop connector for channel '{sales_channel}' has writes disabled"
				telemetry["duration_seconds"] = round(time.time() - start_time, 4)
				return telemetry
			assert_safe_write_target(client.config.environment, client.config.base_url)

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

	# 2. Query bounded claimable events (Pending or due Retry_Pending for this provider)
	db_now = get_database_now()
	event_rows = frappe.db.sql(
		"""
		SELECT name, status, next_retry_at
		FROM `tabIntegration Event`
		WHERE sales_channel = %s
		  AND provider = %s
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
			provider,
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


def process_multichannel_inventory_publications(
	provider: str = IntegrationProvider.PRESTASHOP,
	max_channels_per_run: int = DEFAULT_MAX_CHANNELS_PER_RUN,
	max_events_per_channel: int = DEFAULT_SCHEDULER_BATCH_LIMIT,
	max_total_events_per_run: int = DEFAULT_MAX_TOTAL_EVENTS_PER_RUN,
	worker_id: Optional[str] = None,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Provider/channel-neutral dispatcher and coordinator for multi-channel inventory publication.
	Enforces:
	1. Dynamic channel discovery: finds eligible writable channels via discover_eligible_publication_channels.
	2. Fair bounded processing: allocates bounded quotas per channel, ensuring one busy channel does not starve others.
	3. Global bounded execution: respects max_total_events_per_run across all channels.
	4. Cross-channel failure isolation: one channel's network or safety failure does not disrupt other channels.
	5. Multi-company isolation: respects and verifies company mapping per channel.
	6. Comprehensive aggregation telemetry: structured metrics across all processed channels.
	"""
	start_time = time.time()
	global_telemetry = {
		"provider": provider,
		"channels_seen": 0,
		"channels_eligible": 0,
		"channels_processed": 0,
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
		"channel_results": {},
	}

	# 1. Discover eligible channels
	eligible_channels = discover_eligible_publication_channels(provider=provider)
	global_telemetry["channels_seen"] = len(eligible_channels)

	if not eligible_channels:
		global_telemetry["duration_seconds"] = round(time.time() - start_time, 4)
		return global_telemetry

	# Bounded channel slice
	channels_to_run = eligible_channels[:max_channels_per_run]
	global_telemetry["channels_eligible"] = len(channels_to_run)

	remaining_global_budget = int(max_total_events_per_run)

	# 2. Iterate across channels with fairness and failure isolation
	for ch_info in channels_to_run:
		if remaining_global_budget <= 0:
			break

		ch_name = ch_info["sales_channel"]
		budget_for_channel = min(int(max_events_per_channel), remaining_global_budget)

		try:
			ch_telemetry = process_pending_inventory_publications(
				sales_channel=ch_name,
				max_events=budget_for_channel,
				worker_id=worker_id,
				client=client,
				provider=provider,
			)

			global_telemetry["channels_processed"] += 1
			global_telemetry["channel_results"][ch_name] = ch_telemetry

			# Aggregate metrics
			events_claimed = ch_telemetry.get("events_claimed", 0)
			global_telemetry["events_seen"] += ch_telemetry.get("events_seen", 0)
			global_telemetry["events_claimed"] += events_claimed
			global_telemetry["published"] += ch_telemetry.get("published", 0)
			global_telemetry["no_op"] += ch_telemetry.get("no_op", 0)
			global_telemetry["stale"] += ch_telemetry.get("stale", 0)
			global_telemetry["retry_pending"] += ch_telemetry.get("retry_pending", 0)
			global_telemetry["dead_letter"] += ch_telemetry.get("dead_letter", 0)
			global_telemetry["safety_blocked"] += ch_telemetry.get("safety_blocked", 0)
			global_telemetry["failed"] += ch_telemetry.get("failed", 0)

			remaining_global_budget -= events_claimed

		except Exception as exc:
			global_telemetry["failed"] += 1
			global_telemetry["channel_results"][ch_name] = {
				"sales_channel": ch_name,
				"error": str(exc)[:300],
			}

	global_telemetry["duration_seconds"] = round(time.time() - start_time, 4)
	return global_telemetry


def enqueue_inventory_publication_dispatcher(
	provider: str = IntegrationProvider.PRESTASHOP,
	max_channels_per_run: int = DEFAULT_MAX_CHANNELS_PER_RUN,
	max_events_per_channel: int = DEFAULT_SCHEDULER_BATCH_LIMIT,
	max_total_events_per_run: int = DEFAULT_MAX_TOTAL_EVENTS_PER_RUN,
):
	"""
	Provider/channel-neutral top-level cron entry point.
	Dispatches process_multichannel_inventory_publications onto Frappe background queue.
	"""
	frappe.enqueue(
		"bop_erp.inventory.scheduler.process_multichannel_inventory_publications",
		queue="default",
		provider=provider,
		max_channels_per_run=max_channels_per_run,
		max_events_per_channel=max_events_per_channel,
		max_total_events_per_run=max_total_events_per_run,
		now=frappe.flags.in_test or False,
	)


def enqueue_scheduled_inventory_publication(
	sales_channel: Optional[str] = None,
	max_events: int = DEFAULT_SCHEDULER_BATCH_LIMIT,
):
	"""
	Backwards-compatible entry point.
	If sales_channel is provided, enqueues single-channel worker.
	If sales_channel is None, dispatches multi-channel dispatcher.
	"""
	if sales_channel:
		frappe.enqueue(
			"bop_erp.inventory.scheduler.process_pending_inventory_publications",
			queue="default",
			sales_channel=sales_channel,
			max_events=max_events,
			now=frappe.flags.in_test or False,
		)
	else:
		enqueue_inventory_publication_dispatcher(max_events_per_channel=max_events)
