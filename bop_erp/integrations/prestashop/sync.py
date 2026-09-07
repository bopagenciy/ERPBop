# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
import frappe
from frappe import _
from frappe.utils import now_datetime

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	ExternalEntityType,
	ErrorCategory,
)
from bop_erp.reliability import (
	claim_event_for_processing,
	sanitize_metadata,
)
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopError,
	PrestaShopAuthError,
	PrestaShopNotFoundError,
	PrestaShopRateLimitError,
	PrestaShopValidationError,
	PrestaShopServerError,
	PrestaShopTransientError,
	PrestaShopMalformedResponseError,
	PrestaShopStalePublicationError,
)
from bop_erp.integrations.prestashop.adapters.normalizers import (
	normalize_category,
	normalize_product,
	normalize_combination,
	normalize_stock,
	normalize_customer,
	normalize_address,
	normalize_order,
	normalize_order_line,
)


def map_prestashop_exception_to_error_category(exc: Exception) -> Tuple[str, str]:
	"""Translates PrestaShop exceptions into generic reliability ErrorCategory and error code."""
	if isinstance(exc, PrestaShopAuthError):
		return (ErrorCategory.AUTHENTICATION, "PS_AUTH_FAILED")
	if isinstance(exc, PrestaShopNotFoundError):
		return (ErrorCategory.NOT_FOUND, "PS_NOT_FOUND")
	if isinstance(exc, PrestaShopRateLimitError):
		return (ErrorCategory.RATE_LIMIT, "PS_RATE_LIMITED")
	if isinstance(exc, PrestaShopValidationError):
		return (ErrorCategory.VALIDATION, "PS_VALIDATION_ERROR")
	if isinstance(exc, PrestaShopServerError):
		return (ErrorCategory.PROVIDER_ERROR, "PS_SERVER_ERROR")
	if isinstance(exc, PrestaShopTransientError):
		return (ErrorCategory.TRANSIENT, "PS_NETWORK_TIMEOUT")
	if isinstance(exc, PrestaShopMalformedResponseError):
		return (ErrorCategory.INTERNAL_ERROR, "PS_MALFORMED_RESPONSE")
	if isinstance(exc, PrestaShopStalePublicationError):
		return (ErrorCategory.NON_RETRYABLE, "PS_STALE_SUPERSEDED")
	return (ErrorCategory.INTERNAL_ERROR, "PS_UNHANDLED_EXCEPTION")


def get_active_connector_for_channel(sales_channel: str):
	"""Retrieves the active PrestaShop Connector configured for a given sales channel."""
	if not frappe.db.exists("Sales Channel", sales_channel):
		frappe.throw(_("Sales Channel '{0}' does not exist.").format(sales_channel), frappe.ValidationError)

	conn_name = frappe.db.get_value(
		"PrestaShop Connector",
		{"sales_channel": sales_channel, "enabled": 1},
		"name",
	)
	if not conn_name:
		frappe.throw(
			_("No active PrestaShop Connector found for Sales Channel '{0}'.").format(sales_channel),
			frappe.ValidationError,
		)

	return frappe.get_doc("PrestaShop Connector", conn_name)


def run_prestashop_read_sync_job(
	sales_channel: str,
	entity_type: str,
	worker_id: str = "PS-READ-WORKER",
	limit: int = 50,
	offset: int = 0,
	filters: Optional[Dict[str, Any]] = None,
	correlation_id: Optional[str] = None,
) -> Dict[str, Any]:
	"""
	Executes a read-only integration synchronization job through the Integration Event reliability lifecycle.
	Participates fully in:
	- Idempotency & hashing
	- Worker leases and fencing tokens
	- Error categorization
	- Retry or Dead Letter transitions
	"""
	connector = get_active_connector_for_channel(sales_channel)
	client = connector.get_client()

	# Create inbound Integration Event record
	request_summary = {
		"sales_channel": sales_channel,
		"entity_type": entity_type,
		"limit": limit,
		"offset": offset,
		"filters": filters or {},
	}
	payload_str = json.dumps(request_summary, sort_keys=True)
	payload_hash = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

	event_doc = frappe.get_doc({
		"doctype": "Integration Event",
		"direction": IntegrationDirection.INBOUND,
		"provider": IntegrationProvider.PRESTASHOP,
		"sales_channel": sales_channel,
		"entity_type": entity_type,
		"operation": IntegrationOperation.SYNC,
		"status": IntegrationStatus.PENDING,
		"correlation_id": correlation_id,
		"payload_hash": payload_hash,
		"request_metadata": json.dumps(request_summary),
	}).insert(ignore_permissions=True)

	claimed, active_worker, token = claim_event_for_processing(event_doc.name, worker_id=worker_id)
	if not claimed:
		frappe.throw(
			_("Could not claim event '{0}' for processing: claimed by '{1}'.").format(
				event_doc.name, active_worker
			),
			frappe.ValidationError,
		)

	event = frappe.get_doc("Integration Event", event_doc.name)

	try:
		raw_items = []
		normalized_items = []

		if entity_type == ExternalEntityType.CATEGORY:
			raw_items = client.list_categories(limit=limit, offset=offset, filters=filters, display="full")
			normalized_items = [normalize_category(item) for item in raw_items]

		elif entity_type == ExternalEntityType.PRODUCT:
			raw_items = client.list_products(limit=limit, offset=offset, filters=filters, display="full")
			normalized_items = [normalize_product(item) for item in raw_items]

		elif entity_type == ExternalEntityType.PRODUCT_VARIANT:
			raw_items = client.list_combinations(limit=limit, offset=offset, filters=filters, display="full")
			normalized_items = [normalize_combination(item) for item in raw_items]

		elif entity_type == ExternalEntityType.INVENTORY:
			raw_items = client.list_stock_availables(limit=limit, offset=offset, filters=filters, display="full")
			normalized_items = [normalize_stock(item) for item in raw_items]

		elif entity_type == ExternalEntityType.CUSTOMER:
			raw_items = client.list_customers(limit=limit, offset=offset, filters=filters, display="full")
			normalized_items = [normalize_customer(item) for item in raw_items]

		elif entity_type == ExternalEntityType.ADDRESS:
			raw_items = client.list_addresses(limit=limit, offset=offset, filters=filters, display="full")
			normalized_items = [normalize_address(item) for item in raw_items]

		elif entity_type == ExternalEntityType.ORDER:
			raw_items = client.list_orders(limit=limit, offset=offset, filters=filters, display="full")
			normalized_items = [normalize_order(item) for item in raw_items]

		else:
			frappe.throw(
				_("Unsupported entity type '{0}' for PrestaShop read synchronization.").format(entity_type),
				frappe.ValidationError,
			)

		response_summary = {
			"status": "OK",
			"count": len(normalized_items),
			"sample_ids": [item.external_id for item in normalized_items[:5]],
		}

		event.mark_succeeded(
			processing_token=token,
			response_metadata=response_summary,
		)

		return {
			"status": "SUCCESS",
			"event_name": event.name,
			"count": len(normalized_items),
			"items": normalized_items,
		}

	except Exception as exc:
		err_cat, err_code = map_prestashop_exception_to_error_category(exc)
		err_msg = str(exc)[:250]

		if err_cat in ErrorCategory.RETRYABLE:
			event.schedule_retry(
				processing_token=token,
				error_code=err_code,
				error_message=err_msg,
			)
		else:
			event.mark_failed(
				processing_token=token,
				error_code=err_code,
				error_message=err_msg,
			)

		return {
			"status": "FAILED",
			"event_name": event.name,
			"error_category": err_cat,
			"error_code": err_code,
			"error_message": err_msg,
			"items": [],
		}
