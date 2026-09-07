# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import math
import uuid
from typing import Any, Dict, List, Optional
import frappe
from frappe import _
from frappe.utils import now_datetime, flt

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	ExternalEntityType,
	ErrorCategory,
)
from bop_erp.safety import assert_safe_write_target, ConnectorSafetyError
from bop_erp.reliability import claim_event_for_processing, sanitize_metadata
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopError,
	PrestaShopAuthError,
	PrestaShopNotFoundError,
	PrestaShopRateLimitError,
	PrestaShopValidationError,
	PrestaShopServerError,
	PrestaShopTransientError,
	PrestaShopMalformedResponseError,
)
from bop_erp.integrations.prestashop.sync import map_prestashop_exception_to_error_category, get_active_connector_for_channel


def normalize_publishable_quantity(raw_atp: float, item_code: Optional[str] = None) -> int:
	"""
	Normalizes ERP Channel ATP into PrestaShop integer quantity representation.
	- Any negative ATP is strictly clamped to 0.
	- Positive fractional quantities are safely floored to the nearest whole integer:
	  e.g. 10.8 -> 10, never 11 (strict anti-overselling guard).
	- Zero remains 0.
	"""
	val = flt(raw_atp)
	if val <= 0.0:
		return 0
	return int(math.floor(val))


def compute_publication_hash(
	provider: str,
	sales_channel: str,
	item_code: str,
	external_product_id: Any,
	external_variant_id: Any,
	stock_available_id: Any,
	publishable_quantity: int,
) -> str:
	"""
	Calculates a deterministic SHA-256 hash over the canonical publication state tuple:
	[
	    provider,
	    sales_channel,
	    item_code,
	    str(external_product_id),
	    str(external_variant_id) if external_variant_id else None,
	    str(stock_available_id),
	    int(publishable_quantity),
	]
	"""
	tuple_data = [
		str(provider).strip().upper(),
		str(sales_channel).strip(),
		str(item_code).strip(),
		str(external_product_id).strip() if external_product_id is not None else None,
		str(external_variant_id).strip() if external_variant_id is not None and str(external_variant_id).strip() else None,
		str(stock_available_id).strip() if stock_available_id is not None else None,
		int(publishable_quantity),
	]
	canonical_json = json.dumps(tuple_data, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def get_or_create_publication_state(
	sales_channel: str,
	item_code: str,
	provider: str = IntegrationProvider.PRESTASHOP,
	external_entity_type: str = ExternalEntityType.PRODUCT,
	external_id: Optional[str] = None,
	external_variant_id: Optional[str] = None,
	stock_available_id: Optional[str] = None,
) -> Any:
	"""
	Retrieves existing Inventory Publication State or initializes a new record.
	"""
	filters = {
		"sales_channel": sales_channel,
		"item_code": item_code,
		"provider": provider,
	}
	existing = frappe.db.get_value("Inventory Publication State", filters, "name")
	if existing:
		doc = frappe.get_doc("Inventory Publication State", existing)
		if external_id and not doc.external_id:
			doc.external_id = str(external_id)
		if external_variant_id and not doc.external_variant_id:
			doc.external_variant_id = str(external_variant_id)
		if stock_available_id and not doc.stock_available_id:
			doc.stock_available_id = str(stock_available_id)
		if external_entity_type and not doc.external_entity_type:
			doc.external_entity_type = external_entity_type
		return doc

	doc = frappe.get_doc({
		"doctype": "Inventory Publication State",
		"sales_channel": sales_channel,
		"provider": provider,
		"item_code": item_code,
		"external_entity_type": external_entity_type,
		"external_id": str(external_id) if external_id else "",
		"external_variant_id": str(external_variant_id) if external_variant_id else None,
		"stock_available_id": str(stock_available_id) if stock_available_id else "",
		"publication_version": 1,
		"status": "Pending",
	})
	doc.insert(ignore_permissions=True)
	return doc


def resolve_item_mapping(sales_channel: str, item_code: str) -> Dict[str, Any]:
	"""
	Resolves External ID Mapping for an Item in the given sales channel.
	First checks PRODUCT_VARIANT (for variants), then PRODUCT (for simple items).
	Fails safely if mapping is missing.
	"""
	variant_mapping = frappe.db.get_value(
		"External ID Mapping",
		{
			"sales_channel": sales_channel,
			"erp_doctype": "Item",
			"erp_document": item_code,
			"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"active": 1,
		},
		["name", "external_id", "external_variant_id", "provider"],
		as_dict=True,
	)
	if variant_mapping:
		return {
			"mapping_name": variant_mapping.name,
			"entity_type": ExternalEntityType.PRODUCT_VARIANT,
			"product_id": int(variant_mapping.external_id),
			"variant_id": int(variant_mapping.external_variant_id) if variant_mapping.external_variant_id else None,
			"provider": variant_mapping.provider or IntegrationProvider.PRESTASHOP,
		}

	product_mapping = frappe.db.get_value(
		"External ID Mapping",
		{
			"sales_channel": sales_channel,
			"erp_doctype": "Item",
			"erp_document": item_code,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"active": 1,
		},
		["name", "external_id", "provider"],
		as_dict=True,
	)
	if product_mapping:
		return {
			"mapping_name": product_mapping.name,
			"entity_type": ExternalEntityType.PRODUCT,
			"product_id": int(product_mapping.external_id),
			"variant_id": None,
			"provider": product_mapping.provider or IntegrationProvider.PRESTASHOP,
		}

	raise PrestaShopValidationError(
		_("MAPPING_MISSING: No active External ID Mapping found for Item '{0}' in Sales Channel '{1}'.").format(
			item_code, sales_channel
		)
	)


def publish_item_inventory(
	sales_channel: str,
	item_code: str,
	force: bool = False,
	worker_id: str = "INVENTORY-PUBLISHER",
	intended_atp: Optional[float] = None,
	event_doc: Optional[Any] = None,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Authoritative inventory publication pipeline from Bop-ERP to PrestaShop.
	Enforces:
	1. Host write safety guard BEFORE any external call.
	2. Live Channel ATP computation (authoritative ERP state).
	3. Stale event protection: recomputes current ATP. If intended_atp was passed from an older event
	   and current ATP has diverged, the older intent is marked STALE / SUPERSEDED.
	4. Mapping resolution & identity validation.
	5. Resolution of stock_available ID.
	6. Canonical hashing and publication state versioning.
	7. Read-before-write delta check:
	   - Remote qty == desired qty: NO-OP recorded without sending HTTP PUT.
	   - Remote qty != desired qty: executes safe Read-Modify-Write XML PUT.
	8. Integration Event logging and error categorization.
	"""
	# 1. Validate Connector and Safety Guard
	if client is not None:
		assert_safe_write_target(client.config.environment, client.config.base_url)
		if not client.config.write_enabled:
			raise PrestaShopValidationError(_("PrestaShop Connector write operations are disabled (write_enabled=0)."))
	else:
		connector = get_active_connector_for_channel(sales_channel)
		assert_safe_write_target(connector.environment, connector.base_url)

		if not connector.write_enabled:
			raise PrestaShopValidationError(_("PrestaShop Connector write operations are disabled (write_enabled=0)."))

		config = PrestaShopConfig.from_connector_doc(connector)
		client = PrestaShopClient(config=config)

	# 2. Compute live Authoritative Channel ATP
	live_channel_atp = get_channel_atp(item_code, sales_channel)
	current_atp_qty = live_channel_atp.aggregate_atp_qty
	target_publish_qty = normalize_publishable_quantity(current_atp_qty, item_code)

	# 3. Resolve Mapping
	mapping = resolve_item_mapping(sales_channel, item_code)
	product_id = mapping["product_id"]
	variant_id = mapping["variant_id"]
	entity_type = mapping["entity_type"]
	provider = mapping["provider"]

	# 4. Resolve stock_available ID
	stock_available_id = client.resolve_stock_available_id(product_id, variant_id)

	# 6. Retrieve / Lock Publication State
	pub_state = get_or_create_publication_state(
		sales_channel=sales_channel,
		item_code=item_code,
		provider=provider,
		external_entity_type=entity_type,
		external_id=str(product_id),
		external_variant_id=str(variant_id) if variant_id else None,
		stock_available_id=str(stock_available_id),
	)

	# 7. Monotonic Versioning & Stale Event Protection
	if intended_atp is not None:
		normalized_intended = normalize_publishable_quantity(intended_atp, item_code)
		if normalized_intended != target_publish_qty:
			pub_state.status = "Stale"
			pub_state.last_error = f"Event intended qty ({normalized_intended}) supersedes by live ATP ({target_publish_qty})."
			pub_state.save(ignore_permissions=True)
			if event_doc:
				event_doc.mark_succeeded(
					response_metadata={
						"action": "SUPERSEDED_BY_FRESHER_ATP",
						"intended_qty": normalized_intended,
						"current_atp_qty": target_publish_qty,
					}
				)
			return {
				"item_code": item_code,
				"sales_channel": sales_channel,
				"status": "STALE_SUPERSEDED",
				"intended_qty": normalized_intended,
				"current_atp_qty": target_publish_qty,
				"changed": False,
				"reason": "STALE_EVENT_PROTECTION",
			}

	# 8. Compute Canonical Hash
	pub_hash = compute_publication_hash(
		provider=provider,
		sales_channel=sales_channel,
		item_code=item_code,
		external_product_id=product_id,
		external_variant_id=variant_id,
		stock_available_id=stock_available_id,
		publishable_quantity=target_publish_qty,
	)

	# 9. Read-before-write and Execute Update
	try:
		update_result = client.update_stock_available_quantity(
			stock_available_id=stock_available_id,
			quantity=target_publish_qty,
			expected_product_id=product_id,
			expected_variant_id=variant_id,
		)

		changed = update_result.get("changed", False)
		prev_remote_qty = update_result.get("previous_qty")
		resulting_remote_qty = update_result.get("resulting_qty")

		# 10. Update Publication State
		now = now_datetime()
		pub_state.last_computed_atp = current_atp_qty
		pub_state.last_published_qty = target_publish_qty
		pub_state.last_published_hash = pub_hash
		pub_state.last_published_at = now
		pub_state.last_remote_observed_qty = resulting_remote_qty
		pub_state.publication_version = int(pub_state.publication_version or 0) + (1 if changed else 0)
		pub_state.status = "Succeeded" if changed else "No_Op"
		pub_state.last_error = ""
		pub_state.save(ignore_permissions=True)

		return {
			"item_code": item_code,
			"sales_channel": sales_channel,
			"erp_atp": current_atp_qty,
			"publishable_qty": target_publish_qty,
			"external_product_id": product_id,
			"external_variant_id": variant_id,
			"stock_available_id": stock_available_id,
			"remote_previous_qty": prev_remote_qty,
			"remote_resulting_qty": resulting_remote_qty,
			"changed": changed,
			"publication_state": pub_state.name,
			"publication_hash": pub_hash,
			"reason": update_result.get("reason", "SUCCESS"),
		}

	except Exception as exc:
		pub_state.status = "Failed"
		pub_state.last_error = str(exc)[:500]
		pub_state.save(ignore_permissions=True)
		raise


def schedule_channel_inventory_publication(
	sales_channel: str,
	item_codes: Optional[List[str]] = None,
	batch_size: int = 50,
) -> List[str]:
	"""
	Schedules bounded bulk publication using Phase 1C Integration Events.
	Creates one bounded Integration Event per item.
	"""
	if not item_codes:
		mappings = frappe.get_all(
			"External ID Mapping",
			filters={
				"sales_channel": sales_channel,
				"erp_doctype": "Item",
				"active": 1,
				"external_entity_type": ["in", [ExternalEntityType.PRODUCT, ExternalEntityType.PRODUCT_VARIANT]],
			},
			fields=["erp_document"],
			limit=batch_size,
		)
		item_codes = list({m.erp_document for m in mappings})

	event_names = []
	for ic in item_codes[:batch_size]:
		idempotency_key = f"PUB-{sales_channel}-{ic}-{now_datetime().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"idempotency_key": idempotency_key,
			"status": IntegrationStatus.PENDING,
			"request_payload": json.dumps({"item_code": ic, "sales_channel": sales_channel}),
		})
		event.insert(ignore_permissions=True)
		event_names.append(event.name)

	return event_names
