# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from typing import Any, Dict, List, Optional
import frappe
from frappe import _
from frappe.utils import now_datetime, add_to_date, get_datetime

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	ExternalEntityType,
)
from bop_erp.safety import assert_safe_connector_target
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import PrestaShopValidationError
from bop_erp.reliability import get_existing_idempotent_event
from bop_erp.orders.ingestion import (
	compute_order_idempotency_key,
	find_existing_order_mapping,
	get_eligible_order_states,
)

DEFAULT_MAX_CHANNELS = 10
DEFAULT_MAX_ORDERS_PER_CHANNEL = 20
DEFAULT_GLOBAL_MAX_ORDERS = 50
DEFAULT_LOOKBACK_HOURS = 48
DEFAULT_OVERLAP_MINUTES = 30
DEFAULT_PAGE_SIZE = 20


def discover_eligible_connectors(provider: str = IntegrationProvider.PRESTASHOP) -> List[Dict[str, Any]]:
	"""
	Dynamically discovers all enabled, read-authorized connectors for inbound order discovery.
	Enforces host safety assertions on every connector before returning.
	"""
	if provider == IntegrationProvider.PRESTASHOP:
		connectors = frappe.get_all(
			"PrestaShop Connector",
			filters={"enabled": 1, "read_enabled": 1},
			fields=[
				"name",
				"sales_channel",
				"environment",
				"base_url",
				"credential_reference",
				"eligible_order_states",
				"last_order_watermark",
				"last_order_id",
			],
			order_by="sales_channel asc",
		)
		safe_connectors = []
		for conn in connectors:
			try:
				assert_safe_connector_target(conn.environment, conn.base_url)
				safe_connectors.append(conn)
			except Exception as e:
				frappe.logger("bop_erp").warning(
					f"PrestaShop Connector '{conn.name}' failed safety checks and will be skipped: {e}"
				)
		return safe_connectors

	return []


def get_fair_channel_order(channels: List[Dict[str, Any]], cache_key: str = "order_discovery_cursor") -> List[Dict[str, Any]]:
	"""
	Applies a rotating cursor round-robin fairness strategy.
	Rotates channel sequence based on the last processed channel index stored in cache,
	preventing starvation of lexicographically later channels when budgets are reached.
	"""
	if not channels or len(channels) <= 1:
		return channels

	cursor = frappe.cache().get_value(cache_key)
	start_idx = 0
	if cursor is not None:
		try:
			start_idx = (int(cursor) + 1) % len(channels)
		except (ValueError, TypeError):
			start_idx = 0

	rotated = channels[start_idx:] + channels[:start_idx]
	return rotated


def discover_channel_orders(
	connector: Dict[str, Any],
	max_orders: int = DEFAULT_MAX_ORDERS_PER_CHANNEL,
	lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
	overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
	page_size: int = DEFAULT_PAGE_SIZE,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Discovers eligible orders for a single connector/sales channel.
	Enforces:
	1. Pre-network host safety verification.
	2. Connector-specific eligible order states (safe skip if empty).
	3. Durable watermark tracking with bounded overlap re-read window.
	4. Deterministic pagination and tie-breaker sorting.
	5. Canonical inbound Integration Event idempotency (PENDING only).
	6. Durable watermark persistence surviving worker/scheduler restarts.
	"""
	# Re-verify host safety pre-network
	assert_safe_connector_target(connector["environment"], connector["base_url"])

	sales_channel = connector["sales_channel"]
	provider = IntegrationProvider.PRESTASHOP
	raw_states = connector.get("eligible_order_states")
	eligible_states = [s.strip() for s in str(raw_states).split(",") if s.strip()] if raw_states else []

	if not eligible_states:
		frappe.logger("bop_erp").warning(
			f"PrestaShop Connector '{connector.get('name')}' has no configured eligible_order_states. Skipping order discovery."
		)
		return {
			"sales_channel": sales_channel,
			"orders_seen": 0,
			"events_created": 0,
			"duplicate_orders": 0,
			"ineligible_orders": 0,
			"warning": "NO_ELIGIBLE_STATES_CONFIGURED",
		}

	if not client:
		config = PrestaShopConfig.from_connector_doc(connector)
		client = PrestaShopClient(config=config)

	# Determine discovery window based on durable watermark + bounded overlap
	last_watermark = connector.get("last_order_watermark")
	if last_watermark:
		try:
			dt = add_to_date(get_datetime(last_watermark), minutes=-overlap_minutes, as_datetime=True)
			start_time = dt.strftime("%Y-%m-%d %H:%M:%S")
		except Exception:
			dt = add_to_date(now_datetime(), hours=-lookback_hours, as_datetime=True)
			start_time = dt.strftime("%Y-%m-%d %H:%M:%S")
	else:
		dt = add_to_date(now_datetime(), hours=-lookback_hours, as_datetime=True)
		start_time = dt.strftime("%Y-%m-%d %H:%M:%S")

	orders_seen = 0
	events_created = 0
	duplicate_orders = 0
	ineligible_orders = 0
	max_observed_date_upd = last_watermark
	max_observed_order_id = connector.get("last_order_id")

	offset = 0
	while orders_seen < max_orders:
		page_limit = min(page_size, max_orders - orders_seen)
		params = {
			"display": "full",
			"limit": f"{offset},{page_limit}",
			"sort": "[date_upd_ASC,id_ASC]",
			"filter[date_upd]": f">[{start_time}]",
		}

		try:
			raw_orders = client._request("GET", "orders", params=params)
		except PrestaShopValidationError:
			fallback_params = {
				"display": "full",
				"limit": f"{offset},{page_limit}",
				"sort": "[id_ASC]",
			}
			if eligible_states:
				fallback_params["filter[current_state]"] = f"[{'|'.join(eligible_states)}]"
			try:
				raw_orders = client._request("GET", "orders", params=fallback_params)
			except Exception as fb_err:
				frappe.logger("bop_erp").error(
					f"Error querying PrestaShop orders fallback for channel {sales_channel} at offset {offset}: {fb_err}"
				)
				break
		except Exception as req_err:
			frappe.logger("bop_erp").error(
				f"Error querying PrestaShop orders for channel {sales_channel} at offset {offset}: {req_err}"
			)
			break

		if isinstance(raw_orders, dict):
			orders_page = raw_orders.get("orders", [])
		elif isinstance(raw_orders, list):
			orders_page = raw_orders
		else:
			orders_page = []

		if not orders_page:
			break

		for o in orders_page:
			order_id = str(o.get("id") or "").strip()
			if not order_id:
				continue

			orders_seen += 1

			date_upd = str(o.get("date_upd") or "").strip()
			if date_upd:
				if not max_observed_date_upd or date_upd > str(max_observed_date_upd):
					max_observed_date_upd = date_upd
					max_observed_order_id = order_id
				elif date_upd == str(max_observed_date_upd) and order_id:
					if not max_observed_order_id or int(order_id) > int(max_observed_order_id):
						max_observed_order_id = order_id

			state_id = str(o.get("current_state") or "").strip()
			if state_id not in eligible_states:
				ineligible_orders += 1
				continue

			# Check if already mapped to a Sales Order
			if find_existing_order_mapping(sales_channel, provider, order_id):
				duplicate_orders += 1
				continue

			# Canonical idempotency key
			idem_key = compute_order_idempotency_key(provider, sales_channel, order_id)

			# Check if Integration Event already exists
			existing_event = get_existing_idempotent_event(
				provider=provider,
				sales_channel=sales_channel,
				entity_type=ExternalEntityType.ORDER,
				operation=IntegrationOperation.INGEST_ORDER,
				idempotency_key=idem_key,
			)
			if existing_event:
				duplicate_orders += 1
				continue

			# Create new inbound Integration Event with PENDING status
			payload = {
				"sales_channel": sales_channel,
				"provider": provider,
				"external_order_id": order_id,
				"raw_order": o,
			}

			event_doc = frappe.get_doc({
				"doctype": "Integration Event",
				"direction": IntegrationDirection.INBOUND,
				"provider": provider,
				"sales_channel": sales_channel,
				"entity_type": ExternalEntityType.ORDER,
				"operation": IntegrationOperation.INGEST_ORDER,
				"external_id": order_id,
				"idempotency_key": idem_key,
				"status": IntegrationStatus.PENDING,
				"request_metadata": json.dumps(payload),
				"max_attempts": 3,
			})
			try:
				event_doc.insert(ignore_permissions=True)
				events_created += 1
			except (frappe.DuplicateEntryError, frappe.ValidationError):
				duplicate_orders += 1

		if len(orders_page) < page_limit:
			# Less than requested page size means no more records
			break
		offset += len(orders_page)

	# Persist durable discovery watermark if new orders were observed
	connector_name = connector.get("name")
	if connector_name and max_observed_date_upd and max_observed_date_upd != last_watermark:
		try:
			frappe.db.set_value(
				"PrestaShop Connector",
				connector_name,
				{
					"last_order_watermark": max_observed_date_upd,
					"last_order_id": max_observed_order_id,
				},
				update_modified=False,
			)
			frappe.db.commit()
		except Exception as save_err:
			frappe.logger("bop_erp").warning(f"Could not update connector watermark: {save_err}")

	return {
		"sales_channel": sales_channel,
		"orders_seen": orders_seen,
		"events_created": events_created,
		"duplicate_orders": duplicate_orders,
		"ineligible_orders": ineligible_orders,
		"last_watermark": str(max_observed_date_upd or ""),
	}


def discover_multichannel_orders(
	max_channels: int = DEFAULT_MAX_CHANNELS,
	max_orders_per_channel: int = DEFAULT_MAX_ORDERS_PER_CHANNEL,
	global_max_orders: int = DEFAULT_GLOBAL_MAX_ORDERS,
	lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
	provider: str = IntegrationProvider.PRESTASHOP,
	clients_by_channel: Optional[Dict[str, PrestaShopClient]] = None,
) -> Dict[str, Any]:
	"""
	Provider-neutral global multi-channel order discovery coordinator.
	Enforces:
	1. Bounded channels and orders per channel.
	2. Global budget constraint.
	3. Fair rotating cursor channel processing.
	4. Isolated channel error handling.
	"""
	all_connectors = discover_eligible_connectors(provider=provider)
	if not all_connectors:
		return {
			"channels_seen": 0,
			"channels_processed": 0,
			"orders_seen": 0,
			"events_created": 0,
			"duplicate_orders": 0,
			"channel_results": {},
		}

	ordered_connectors = get_fair_channel_order(all_connectors)
	connectors_to_process = ordered_connectors[:max_channels]

	total_orders_seen = 0
	total_events_created = 0
	total_duplicate_orders = 0
	channel_results = {}
	last_processed_idx = 0

	for idx, conn in enumerate(connectors_to_process):
		ch = conn["sales_channel"]
		if total_events_created >= global_max_orders:
			break

		remaining_global = global_max_orders - total_events_created
		channel_max = min(max_orders_per_channel, remaining_global)

		cli = clients_by_channel.get(ch) if clients_by_channel else None

		try:
			res = discover_channel_orders(
				connector=conn,
				max_orders=channel_max,
				lookback_hours=lookback_hours,
				client=cli,
			)
			channel_results[ch] = res
			total_orders_seen += res["orders_seen"]
			total_events_created += res["events_created"]
			total_duplicate_orders += res["duplicate_orders"]
			last_processed_idx = idx
		except Exception as e:
			frappe.logger("bop_erp").error(f"Discovery error on channel {ch}: {e}")
			channel_results[ch] = {"error": str(e), "events_created": 0}

	# Update fair rotating cursor in cache
	if connectors_to_process:
		original_idx = all_connectors.index(connectors_to_process[last_processed_idx])
		frappe.cache().set_value("order_discovery_cursor", original_idx, expires_in_sec=86400)

	frappe.db.commit()

	return {
		"channels_seen": len(all_connectors),
		"channels_processed": len(channel_results),
		"orders_seen": total_orders_seen,
		"events_created": total_events_created,
		"duplicate_orders": total_duplicate_orders,
		"channel_results": channel_results,
	}


def enqueue_multichannel_order_discovery(
	max_channels: int = DEFAULT_MAX_CHANNELS,
	max_orders_per_channel: int = DEFAULT_MAX_ORDERS_PER_CHANNEL,
	global_max_orders: int = DEFAULT_GLOBAL_MAX_ORDERS,
):
	"""Enqueues multichannel order discovery to the default background queue."""
	frappe.enqueue(
		"bop_erp.orders.discovery.discover_multichannel_orders",
		queue="default",
		timeout=300,
		max_channels=max_channels,
		max_orders_per_channel=max_orders_per_channel,
		global_max_orders=global_max_orders,
		now=frappe.flags.in_test or False,
	)
