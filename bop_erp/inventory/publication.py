# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import math
import uuid
from typing import Any, Dict, List, Optional, Tuple
import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime, flt

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	ExternalEntityType,
	ErrorCategory,
)
from bop_erp.safety import assert_safe_write_target, ConnectorSafetyError
from bop_erp.reliability import (
	claim_event_for_processing,
	sanitize_metadata,
	verify_processing_authority,
	get_database_now,
)
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
	PrestaShopStalePublicationError,
)
from bop_erp.integrations.prestashop.sync import (
	map_prestashop_exception_to_error_category,
	get_active_connector_for_channel,
)


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


def compute_publication_idempotency_key(
	provider: str,
	sales_channel: str,
	item_code: str,
	external_id: Any,
	external_variant_id: Any,
	publication_version: int,
	desired_state_hash: str,
) -> str:
	"""
	Calculates a deterministic SHA-256 idempotency key identifying an outbound publication intent:
	[provider, sales_channel, item_code, external_id, external_variant_id, publication_version, desired_state_hash]
	"""
	canonical_tuple = [
		str(provider).strip().upper(),
		str(sales_channel).strip(),
		str(item_code).strip(),
		str(external_id).strip() if external_id is not None else None,
		str(external_variant_id).strip() if external_variant_id is not None and str(external_variant_id).strip() else None,
		int(publication_version),
		str(desired_state_hash).strip(),
	]
	canonical_json = json.dumps(canonical_tuple, ensure_ascii=False, separators=(",", ":"))
	return f"PUB-{hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()}"


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
	Handles concurrent creation safely.
	"""
	filters = {
		"sales_channel": sales_channel,
		"item_code": item_code,
		"provider": provider,
	}
	existing = frappe.db.get_value("Inventory Publication State", filters, "name")
	if existing:
		doc = frappe.get_doc("Inventory Publication State", existing)
		changed = False
		if external_id and not doc.external_id:
			doc.external_id = str(external_id)
			changed = True
		if external_variant_id and not doc.external_variant_id:
			doc.external_variant_id = str(external_variant_id)
			changed = True
		if stock_available_id and not doc.stock_available_id:
			doc.stock_available_id = str(stock_available_id)
			changed = True
		if external_entity_type and not doc.external_entity_type:
			doc.external_entity_type = external_entity_type
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return doc

	try:
		doc = frappe.get_doc({
			"doctype": "Inventory Publication State",
			"sales_channel": sales_channel,
			"provider": provider,
			"item_code": item_code,
			"external_entity_type": external_entity_type,
			"external_id": str(external_id) if external_id else "0",
			"external_variant_id": str(external_variant_id) if external_variant_id else None,
			"stock_available_id": str(stock_available_id) if stock_available_id else "0",
			"publication_version": 1,
			"status": "Pending",
		})
		doc.insert(ignore_permissions=True)
		return doc
	except Exception:
		existing = frappe.db.get_value("Inventory Publication State", filters, "name")
		if existing:
			return frappe.get_doc("Inventory Publication State", existing)
		raise


def allocate_publication_version(
	sales_channel: str,
	item_code: str,
	provider: str = IntegrationProvider.PRESTASHOP,
	external_entity_type: str = ExternalEntityType.PRODUCT,
	external_id: Optional[str] = None,
	external_variant_id: Optional[str] = None,
	stock_available_id: Optional[str] = None,
) -> Tuple[str, int]:
	"""
	Atomically allocates the next monotonic publication version for the given item.
	Guarantees race-free version ordering across concurrent workers using DB-level atomic increment.
	"""
	pub_state = get_or_create_publication_state(
		sales_channel=sales_channel,
		item_code=item_code,
		provider=provider,
		external_entity_type=external_entity_type,
		external_id=external_id,
		external_variant_id=external_variant_id,
		stock_available_id=stock_available_id,
	)

	now = now_datetime()
	frappe.db.sql(
		"""
		UPDATE `tabInventory Publication State`
		SET publication_version = publication_version + 1,
		    modified = %s
		WHERE name = %s
		""",
		(now, pub_state.name),
	)
	new_version = frappe.db.get_value("Inventory Publication State", pub_state.name, "publication_version")
	return pub_state.name, int(new_version or 1)


def commit_publication_state(
	pub_state_name: str,
	target_version: int,
	publish_qty: int,
	pub_hash: str,
	remote_observed_qty: int,
	computed_atp: float,
	status: str = "Succeeded",
	last_event_name: Optional[str] = None,
	last_error: str = "",
	processing_token: Optional[str] = None,
) -> bool:
	"""
	Atomically updates Inventory Publication State enforcing state ownership fencing.
	Only updates if:
	1. If associated with an Integration Event, the worker still possesses valid unexpired authority.
	2. target_version >= the row's current publication_version.
	An older version or expired worker can NEVER overwrite state written by a newer or active version.
	Returns True if committed; False if superseded/fenced/unauthorized.
	"""
	if last_event_name and processing_token:
		is_auth, _ = verify_processing_authority(last_event_name, processing_token)
		if not is_auth:
			return False

	now = now_datetime()
	frappe.db.sql(
		"""
		UPDATE `tabInventory Publication State`
		SET last_published_qty = %s,
		    last_published_hash = %s,
		    last_published_at = %s,
		    last_remote_observed_qty = %s,
		    last_computed_atp = %s,
		    status = %s,
		    last_event = %s,
		    last_error = %s,
		    publication_version = GREATEST(publication_version, %s),
		    modified = %s
		WHERE name = %s
		  AND publication_version <= %s
		""",
		(
			int(publish_qty),
			str(pub_hash),
			now,
			int(remote_observed_qty),
			flt(computed_atp),
			str(status),
			last_event_name,
			str(last_error or "")[:500],
			int(target_version),
			now,
			pub_state_name,
			int(target_version),
		),
	)
	row_count = frappe.db.sql("SELECT ROW_COUNT()")[0][0]
	return bool(row_count > 0)


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
	publication_version: Optional[int] = None,
	event_doc: Optional[Any] = None,
	processing_token: Optional[str] = None,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Authoritative inventory publication pipeline from Bop-ERP to PrestaShop.
	Enforces:
	1. Host write safety guard before any external network activity.
	2. Mapping resolution & identity validation.
	3. Live Channel ATP computation (authoritative ERP state).
	4. DB-safe monotonic publication version allocation and state retrieval.
	5. Stale event & supersession detection.
	6. Canonical hashing of desired state.
	7. Immediate pre-PUT freshness verification (host guard, version fencing, token lease, ATP).
	8. Read-before-write delta comparison and Read-Modify-Write XML PUT.
	9. DB-fenced publication state commit preventing older overwrites.
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

	# 2. Resolve Mapping (authoritative ERP mapping at runtime)
	mapping = resolve_item_mapping(sales_channel, item_code)
	product_id = mapping["product_id"]
	variant_id = mapping["variant_id"]
	entity_type = mapping["entity_type"]
	provider = mapping["provider"]

	# 3. Compute live Authoritative Channel ATP
	live_channel_atp = get_channel_atp(item_code, sales_channel)
	current_atp_qty = live_channel_atp.aggregate_atp_qty
	target_publish_qty = normalize_publishable_quantity(current_atp_qty, item_code)

	# 4. Resolve stock_available ID from remote PrestaShop
	stock_available_id = client.resolve_stock_available_id(product_id, variant_id)

	# 5. Retrieve / Initialize Publication State & Monotonic Version
	pub_state = get_or_create_publication_state(
		sales_channel=sales_channel,
		item_code=item_code,
		provider=provider,
		external_entity_type=entity_type,
		external_id=str(product_id),
		external_variant_id=str(variant_id) if variant_id else None,
		stock_available_id=str(stock_available_id),
	)

	if publication_version is None:
		pub_state_name, version = allocate_publication_version(
			sales_channel=sales_channel,
			item_code=item_code,
			provider=provider,
			external_entity_type=entity_type,
			external_id=str(product_id),
			external_variant_id=str(variant_id) if variant_id else None,
			stock_available_id=str(stock_available_id),
		)
	else:
		pub_state_name = pub_state.name
		version = int(publication_version)

	# 6. Database Version Supersession Check
	current_db_v = frappe.db.get_value("Inventory Publication State", pub_state_name, "publication_version")
	if current_db_v and int(current_db_v) > version:
		return {
			"item_code": item_code,
			"sales_channel": sales_channel,
			"status": "STALE_SUPERSEDED",
			"version": version,
			"current_db_version": int(current_db_v),
			"changed": False,
			"reason": "SUPERSEDED_BY_NEWER_VERSION",
		}

	# 7. Stale Event Intended ATP Protection
	if intended_atp is not None:
		normalized_intended = normalize_publishable_quantity(intended_atp, item_code)
		if normalized_intended != target_publish_qty:
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

	# 9. Immediate Pre-PUT Freshness Check Hook
	def immediate_pre_put_freshness_check():
		# Re-verify host safety target immediately before PUT
		if client is not None:
			assert_safe_write_target(client.config.environment, client.config.base_url)
		else:
			connector_now = get_active_connector_for_channel(sales_channel)
			assert_safe_write_target(connector_now.environment, connector_now.base_url)

		# Re-verify publication state version in DB
		db_v = frappe.db.get_value("Inventory Publication State", pub_state_name, "publication_version")
		if db_v and int(db_v) > version:
			raise PrestaShopStalePublicationError(
				f"PRE_PUT_FRESHNESS_CHECK: Publication version superseded (worker={version}, db={db_v}). Aborting PUT."
			)

		# Re-verify lease and authoritative worker processing status if processing an Integration Event
		if event_doc and processing_token:
			is_auth, reason_auth = verify_processing_authority(event_doc.name, processing_token)
			if not is_auth:
				raise PrestaShopStalePublicationError(
					f"PRE_PUT_FRESHNESS_CHECK: {reason_auth}. Aborting PUT."
				)

		# Re-verify live ATP freshness if intended_atp was passed
		if intended_atp is not None:
			fresh_live = get_channel_atp(item_code, sales_channel).aggregate_atp_qty
			if normalize_publishable_quantity(fresh_live, item_code) != target_publish_qty:
				raise PrestaShopStalePublicationError(
					f"PRE_PUT_FRESHNESS_CHECK: Live ATP changed from {target_publish_qty} to {fresh_live} right before PUT. Aborting."
				)

	# 10. Read-before-write and Execute Update
	try:
		update_result = client.update_stock_available_quantity(
			stock_available_id=stock_available_id,
			quantity=target_publish_qty,
			expected_product_id=product_id,
			expected_variant_id=variant_id,
			pre_put_hook=immediate_pre_put_freshness_check,
		)

		changed = update_result.get("changed", False)
		prev_remote_qty = update_result.get("previous_qty")
		resulting_remote_qty = update_result.get("resulting_qty")
		reason = update_result.get("reason", "SUCCESS")

		# 11. State Ownership Fenced Commit
		committed = commit_publication_state(
			pub_state_name=pub_state_name,
			target_version=version,
			publish_qty=target_publish_qty,
			pub_hash=pub_hash,
			remote_observed_qty=resulting_remote_qty,
			computed_atp=current_atp_qty,
			status="Succeeded" if changed else "No_Op",
			last_event_name=event_doc.name if event_doc else None,
			last_error="",
			processing_token=processing_token,
		)

		if not committed:
			# Fenced by a newer concurrent version or lost authority!
			return {
				"item_code": item_code,
				"sales_channel": sales_channel,
				"status": "SUPERSEDED_FENCED",
				"version": version,
				"changed": False,
				"reason": "SUPERSEDED_BY_CONCURRENT_NEWER_VERSION_OR_REVOKED_AUTHORITY",
			}

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
			"publication_state": pub_state_name,
			"publication_version": version,
			"publication_hash": pub_hash,
			"status": "Succeeded" if changed else "No_Op",
			"reason": reason,
		}

	except Exception as exc:
		commit_publication_state(
			pub_state_name=pub_state_name,
			target_version=version,
			publish_qty=target_publish_qty,
			pub_hash=pub_hash,
			remote_observed_qty=-1,
			computed_atp=current_atp_qty,
			status="Failed",
			last_event_name=event_doc.name if event_doc else None,
			last_error=str(exc)[:500],
			processing_token=processing_token,
		)
		raise


def schedule_channel_inventory_publication(
	sales_channel: str,
	item_codes: Optional[List[str]] = None,
	batch_size: int = 50,
) -> List[str]:
	"""
	Schedules bounded bulk publication using Phase 1C Integration Events.
	Creates one bounded Integration Event per item with canonical identity tuple:
	(provider, sales_channel, item_code, external_id, external_variant_id, publication_version, desired_state_hash).
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
		try:
			mapping = resolve_item_mapping(sales_channel, ic)
			prod_id = mapping.get("product_id")
			var_id = mapping.get("variant_id")
			entity_type = mapping.get("entity_type") or ExternalEntityType.PRODUCT
			provider = mapping.get("provider") or IntegrationProvider.PRESTASHOP
		except Exception:
			prod_id = None
			var_id = None
			entity_type = ExternalEntityType.PRODUCT
			provider = IntegrationProvider.PRESTASHOP

		try:
			pub_state_name, version = allocate_publication_version(
				sales_channel=sales_channel,
				item_code=ic,
				provider=provider,
				external_entity_type=entity_type,
				external_id=str(prod_id) if prod_id else "0",
				external_variant_id=str(var_id) if var_id else None,
			)
		except Exception:
			pub_state_name = None
			version = 1

		try:
			live_channel_atp = get_channel_atp(ic, sales_channel)
			live_atp_qty = live_channel_atp.aggregate_atp_qty
			target_publish_qty = normalize_publishable_quantity(live_atp_qty, ic)
		except Exception:
			live_atp_qty = 0.0
			target_publish_qty = 0

		stock_avail_id = None
		if pub_state_name:
			try:
				stock_avail_id = frappe.db.get_value("Inventory Publication State", pub_state_name, "stock_available_id")
			except Exception:
				stock_avail_id = None

		pub_hash = compute_publication_hash(
			provider=provider,
			sales_channel=sales_channel,
			item_code=ic,
			external_product_id=prod_id,
			external_variant_id=var_id,
			stock_available_id=stock_avail_id,
			publishable_quantity=target_publish_qty,
		)

		idempotency_key = compute_publication_idempotency_key(
			provider=provider,
			sales_channel=sales_channel,
			item_code=ic,
			external_id=prod_id,
			external_variant_id=var_id,
			publication_version=version,
			desired_state_hash=pub_hash,
		)

		payload_dict = {
			"item_code": ic,
			"sales_channel": sales_channel,
			"intended_atp": live_atp_qty,
			"publication_version": version,
			"desired_state_hash": pub_hash,
		}

		# Coalescing & Idempotency guard: check for existing PENDING event
		existing_pending = frappe.db.get_value(
			"Integration Event",
			{
				"provider": provider,
				"sales_channel": sales_channel,
				"direction": IntegrationDirection.OUTBOUND,
				"entity_type": ExternalEntityType.INVENTORY,
				"erp_doctype": "Item",
				"erp_document": ic,
				"status": IntegrationStatus.PENDING,
			},
			["name", "request_metadata", "idempotency_key"],
			as_dict=True,
		)
		if existing_pending:
			meta = {}
			if existing_pending.request_metadata:
				try:
					meta = json.loads(existing_pending.request_metadata) if isinstance(existing_pending.request_metadata, str) else existing_pending.request_metadata
				except Exception:
					meta = {}
			if meta.get("desired_state_hash") == pub_hash and flt(meta.get("intended_atp")) == flt(live_atp_qty):
				# Safe coalescence: identical intent already queued
				event_names.append(existing_pending.name)
				continue
			else:
				# Coalesce: update existing pending intent with latest state
				existing_doc = frappe.get_doc("Integration Event", existing_pending.name)
				existing_doc.request_metadata = json.dumps(payload_dict)
				existing_doc.idempotency_key = idempotency_key
				existing_doc.save(ignore_permissions=True)
				event_names.append(existing_doc.name)
				continue

		event = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": provider,
			"sales_channel": sales_channel,
			"direction": IntegrationDirection.OUTBOUND,
			"entity_type": ExternalEntityType.INVENTORY,
			"operation": IntegrationOperation.UPDATE,
			"erp_doctype": "Item",
			"erp_document": ic,
			"idempotency_key": idempotency_key,
			"status": IntegrationStatus.PENDING,
			"request_metadata": json.dumps(payload_dict),
		})
		event.insert(ignore_permissions=True)
		event_names.append(event.name)

	return event_names


def process_inventory_publication_event(
	event_name: str,
	worker_id: Optional[str] = None,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Authoritative Integration Event worker handler for Outbound Inventory publication.
	Enforces:
	1. Atomic claim & fencing token acquisition.
	2. Payload parsing.
	3. Freshness & supersession verification.
	4. Execution through publish_item_inventory with pre-PUT freshness check.
	5. Fencing-token verified event state transition (SUCCEEDED or FAILED/RETRY_PENDING/DEAD_LETTER).
	6. Retry-After header propagation on 429 rate limits.
	"""
	claimed, worker_id, processing_token = claim_event_for_processing(event_name, worker_id=worker_id)
	if not claimed:
		return {
			"success": False,
			"event_name": event_name,
			"reason": "CLAIM_REJECTED_ALREADY_CLAIMED_OR_TERMINAL",
		}

	event_doc = frappe.get_doc("Integration Event", event_name)

	payload = {}
	raw_meta = event_doc.request_metadata or getattr(event_doc, "request_payload", None)
	if raw_meta:
		try:
			payload = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
		except Exception:
			payload = {}

	item_code = payload.get("item_code") or event_doc.erp_document
	sales_channel = payload.get("sales_channel") or event_doc.sales_channel
	intended_atp = payload.get("intended_atp")
	publication_version = payload.get("publication_version")

	if not item_code or not sales_channel:
		event_doc.mark_failed(
			processing_token=processing_token,
			error_code="INVALID_PAYLOAD",
			error_message="Event missing required item_code or sales_channel",
			error_category=ErrorCategory.VALIDATION,
		)
		return {
			"success": False,
			"event_name": event_name,
			"reason": "INVALID_PAYLOAD",
		}

	try:
		result = publish_item_inventory(
			sales_channel=sales_channel,
			item_code=item_code,
			intended_atp=intended_atp,
			publication_version=publication_version,
			event_doc=event_doc,
			processing_token=processing_token,
			worker_id=worker_id,
			client=client,
		)

		status_str = result.get("status")
		if status_str in ("STALE_SUPERSEDED", "SUPERSEDED_PRE_PUT", "SUPERSEDED_FENCED"):
			event_doc.mark_succeeded(
				processing_token=processing_token,
				response_metadata={
					"action": "SUPERSEDED",
					"reason": result.get("reason"),
					"details": result,
				},
			)
		else:
			event_doc.mark_succeeded(
				processing_token=processing_token,
				response_metadata=result,
			)

		return {
			"success": True,
			"event_name": event_name,
			"result": result,
		}

	except PrestaShopStalePublicationError as stale_err:
		is_auth, _ = verify_processing_authority(event_name, processing_token)
		if is_auth:
			event_doc.mark_succeeded(
				processing_token=processing_token,
				response_metadata={
					"action": "SUPERSEDED",
					"reason": str(stale_err),
				},
			)
		return {
			"success": True,
			"event_name": event_name,
			"superseded": True,
			"reason": str(stale_err),
		}

	except Exception as exc:
		is_auth, auth_reason = verify_processing_authority(event_name, processing_token)
		if not is_auth:
			# Authority already revoked or expired; do not attempt invalid transition
			return {
				"success": False,
				"event_name": event_name,
				"error_code": "LEASE_EXPIRED_OR_REVOKED",
				"error_category": ErrorCategory.NON_RETRYABLE,
				"error": auth_reason,
			}

		error_category, error_code = map_prestashop_exception_to_error_category(exc)
		delay_seconds = getattr(exc, "retry_after", None)

		event_doc.mark_failed(
			processing_token=processing_token,
			error_code=error_code,
			error_message=str(exc),
			error_category=error_category,
			delay_seconds=delay_seconds,
		)

		return {
			"success": False,
			"event_name": event_name,
			"error_code": error_code,
			"error_category": error_category,
			"error": str(exc),
		}
