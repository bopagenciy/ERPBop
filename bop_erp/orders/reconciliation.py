# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import frappe
from frappe import _
from frappe.utils import flt, now_datetime, nowdate, get_datetime

from bop_erp.constants import (
	IntegrationDirection,
	IntegrationProvider,
	IntegrationStatus,
	IntegrationOperation,
	IntegrationReadinessStatus,
	ExternalEntityType,
	ExternalOrderStateAction,
	ErrorCategory,
	TransactionOrigin,
)
from bop_erp.safety import assert_safe_connector_target
from bop_erp.reliability import (
	claim_event_for_processing,
	verify_processing_authority,
	get_database_now,
)
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.adapters.order_normalizer import (
	normalize_prestashop_order,
)
from bop_erp.orders.models import (
	ExternalOrder,
	ExternalOrderLine,
)
from bop_erp.orders.exceptions import (
	OrderIngestionError,
	OrderReservationFailedError,
)
from bop_erp.orders.ingestion import (
	find_existing_order_mapping,
	find_affected_channel_items_for_scopes,
	persist_publication_outbox_intents,
	register_post_commit_wake,
	resolve_order_line_item,
	get_currency_tolerance,
)


def resolve_external_order_state_action(
	sales_channel: str,
	provider: str,
	external_state_id: str,
) -> Tuple[str, str]:
	"""
	Provider-neutral resolver mapping an external order state ID to a canonical
	ExternalOrderStateAction (ACTIVE, CANCEL_BEFORE_FULFILLMENT, REVIEW_REQUIRED, IGNORE).

	Reads connector configuration dynamically for the given sales channel.
	Never assumes hardcoded universal state IDs across stores.
	"""
	clean_prov = str(provider).strip().upper() if provider else IntegrationProvider.PRESTASHOP
	clean_state = str(external_state_id).strip()

	if clean_prov == IntegrationProvider.PRESTASHOP:
		conn = frappe.db.get_value(
			"PrestaShop Connector",
			{"sales_channel": sales_channel, "enabled": 1},
			[
				"cancellation_order_states",
				"review_order_states",
				"eligible_order_states",
			],
			as_dict=True,
		)
		if conn:
			# 1. Cancellation states (e.g. 6)
			raw_canc = conn.get("cancellation_order_states") or "6"
			canc_states = [s.strip() for s in str(raw_canc).split(",") if s.strip()]
			if clean_state in canc_states:
				return ExternalOrderStateAction.CANCEL_BEFORE_FULFILLMENT, "Canceled"

			# 2. Review states (e.g. 7=Refunded, 8=Payment error)
			raw_rev = conn.get("review_order_states") or "7,8"
			rev_states = [s.strip() for s in str(raw_rev).split(",") if s.strip()]
			if clean_state in rev_states:
				state_label = "Refunded" if clean_state == "7" else "Payment Error" if clean_state == "8" else "Review Required"
				return ExternalOrderStateAction.REVIEW_REQUIRED, state_label

			# 3. Active / eligible states (e.g. 2, 3, 4, 5, 9, 11)
			raw_elig = conn.get("eligible_order_states") or ""
			elig_states = [s.strip() for s in str(raw_elig).split(",") if s.strip()]
			if clean_state in elig_states:
				return ExternalOrderStateAction.ACTIVE, "Active"

			# States 4 (Shipped) and 5 (Delivered) are active fulfillment states
			if clean_state in ("4", "5"):
				return ExternalOrderStateAction.ACTIVE, "Fulfillment Completed"

			# Default unpaid/pending validation states
			if clean_state in ("1", "10", "12", "13"):
				return ExternalOrderStateAction.IGNORE, "Ignored Pre-payment State"

	# Fallback safe behavior: if unrecognized, route to REVIEW_REQUIRED
	return ExternalOrderStateAction.REVIEW_REQUIRED, "Unmapped State"


def audit_sales_order_cancellation_safety(so_name: str) -> Tuple[bool, List[str]]:
	"""
	Audits whether an imported ERP Sales Order is operationally safe to automatically cancel.

	Safety predicate:
	- Sales Order must exist and be an imported document.
	- Must have ZERO submitted or active downstream fulfillment/accounting documents:
	  1. Pick List (active/submitted, not cancelled)
	  2. Delivery Note (docstatus = 1)
	  3. Shipment (docstatus = 1)
	  4. Sales Invoice (docstatus = 1)
	  5. Payment Entry (docstatus = 1)
	- If downstream documents exist, auto-cancellation is strictly blocked and returns (False, [reasons]).
	"""
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		return False, [f"Sales Order '{so_name}' does not exist."]

	blocking_reasons: List[str] = []

	# 1. Pick List: submitted or active Pick Lists referencing this SO
	pl_rows = frappe.db.sql(
		"""
		SELECT DISTINCT pl.name
		FROM `tabPick List Item` pli
		JOIN `tabPick List` pl ON pl.name = pli.parent
		WHERE pli.sales_order = %s
		  AND pl.docstatus != 2
		  AND pl.status NOT IN ('Cancelled')
		""",
		(so_name,),
		as_dict=True,
	)
	if pl_rows:
		pl_names = ", ".join(str(r.get("name") if isinstance(r, dict) else getattr(r, "name", str(r))) for r in pl_rows)
		blocking_reasons.append(
			_("Active Pick List ({0}) references Sales Order '{1}'.").format(pl_names, so_name)
		)

	# 2. Delivery Note: submitted Delivery Notes (docstatus = 1)
	dn_rows = frappe.db.sql(
		"""
		SELECT DISTINCT dn.name
		FROM `tabDelivery Note Item` dni
		JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.against_sales_order = %s
		  AND dn.docstatus = 1
		""",
		(so_name,),
		as_dict=True,
	)
	if dn_rows:
		dn_names = ", ".join(str(r.get("name") if isinstance(r, dict) else getattr(r, "name", str(r))) for r in dn_rows)
		blocking_reasons.append(
			_("Submitted Delivery Note ({0}) exists for Sales Order '{1}'.").format(dn_names, so_name)
		)

	# 3. Shipment: submitted Shipments (docstatus = 1)
	shipment_rows = frappe.db.sql(
		"""
		SELECT DISTINCT s.name
		FROM `tabShipment Delivery Note` sdn
		JOIN `tabShipment` s ON s.name = sdn.parent
		JOIN `tabDelivery Note Item` dni ON dni.parent = sdn.delivery_note
		WHERE dni.against_sales_order = %s
		  AND s.docstatus = 1
		""",
		(so_name,),
		as_dict=True,
	)
	if shipment_rows:
		s_names = ", ".join(str(r.get("name") if isinstance(r, dict) else getattr(r, "name", str(r))) for r in shipment_rows)
		blocking_reasons.append(
			_("Submitted Shipment ({0}) exists for Sales Order '{1}'.").format(s_names, so_name)
		)

	# 4. Sales Invoice: submitted Sales Invoices (docstatus = 1)
	si_rows = frappe.db.sql(
		"""
		SELECT DISTINCT si.name
		FROM `tabSales Invoice Item` sii
		JOIN `tabSales Invoice` si ON si.name = sii.parent
		WHERE sii.sales_order = %s
		  AND si.docstatus = 1
		""",
		(so_name,),
		as_dict=True,
	)
	if si_rows:
		si_names = ", ".join(str(r.get("name") if isinstance(r, dict) else getattr(r, "name", str(r))) for r in si_rows)
		blocking_reasons.append(
			_("Submitted Sales Invoice ({0}) exists for Sales Order '{1}'.").format(si_names, so_name)
		)

	# 5. Payment Entry: submitted Payment Entries (docstatus = 1) referencing this SO
	pe_rows = frappe.db.sql(
		"""
		SELECT DISTINCT pe.name
		FROM `tabPayment Entry Reference` per
		JOIN `tabPayment Entry` pe ON pe.name = per.parent
		WHERE per.reference_doctype = 'Sales Order'
		  AND per.reference_name = %s
		  AND pe.docstatus = 1
		""",
		(so_name,),
		as_dict=True,
	)
	if pe_rows:
		pe_names = ", ".join(str(r.get("name") if isinstance(r, dict) else getattr(r, "name", str(r))) for r in pe_rows)
		blocking_reasons.append(
			_("Submitted Payment Entry ({0}) exists for Sales Order '{1}'.").format(pe_names, so_name)
		)

	is_safe = len(blocking_reasons) == 0
	return is_safe, blocking_reasons


def detect_material_order_changes(
	so_doc: Any,
	external_order: ExternalOrder,
) -> Tuple[bool, List[str]]:
	"""
	Compares fresh external order representation against the existing submitted ERP Sales Order.
	Audits changes to:
	- Line item quantities, rates, count
	- Currency
	- Total product value
	- Addresses

	Returns (has_changes: bool, change_descriptions: List[str]).
	"""
	changes: List[str] = []

	# 1. Compare line count
	so_items = getattr(so_doc, "items", None)
	if so_items is None and hasattr(so_doc, "get"):
		so_items = so_doc.get("items")
	so_items = so_items or []
	ext_lines = external_order.lines or []

	if len(so_items) != len(ext_lines):
		changes.append(
			f"Order line count changed: ERP has {len(so_items)} items, external order has {len(ext_lines)} lines."
		)

	# 2. Compare line item details (quantities, rates, items)
	so_item_map = {}
	for item in so_items:
		ic = getattr(item, "item_code", None) or (item.get("item_code") if isinstance(item, dict) else None)
		qty = getattr(item, "qty", None) or (item.get("qty") if isinstance(item, dict) else 0.0)
		if ic:
			so_item_map[ic] = flt(qty)
	tolerance = get_currency_tolerance(getattr(so_doc, "currency", None) or (so_doc.get("currency") if hasattr(so_doc, "get") else "USD"))

	for line in ext_lines:
		try:
			resolved_item = resolve_order_line_item(line, external_order.sales_channel, external_order.provider)
		except Exception as e:
			changes.append(f"Cannot resolve line product {line.external_product_id}: {e}")
			continue

		if resolved_item not in so_item_map:
			changes.append(f"New product {resolved_item} added to external order.")
		else:
			erp_qty = so_item_map[resolved_item]
			ext_qty = flt(line.quantity)
			if abs(erp_qty - ext_qty) > 0.001:
				direction = "increased" if ext_qty > erp_qty else "decreased"
				changes.append(
					f"Quantity {direction} for item {resolved_item}: ERP={erp_qty}, External={ext_qty}."
				)

	# 3. Compare currency
	ext_curr = external_order.currency or "USD"
	if so_doc.currency and ext_curr != so_doc.currency:
		changes.append(f"Currency changed: ERP={so_doc.currency}, External={ext_curr}.")

	# 4. Compare product totals
	ext_prod_tot = flt(external_order.totals.total_products_ex_tax) if external_order.totals else 0.0
	so_net_total = flt(so_doc.net_total)
	if ext_prod_tot > 0.0 and abs(so_net_total - ext_prod_tot) > (tolerance * 2):
		changes.append(
			f"Total product value mismatch: ERP net_total={so_net_total}, External={ext_prod_tot}."
		)

	return len(changes) > 0, changes


def execute_sales_order_cancellation(
	so_name: str,
	external_order_id: str,
	sales_channel: str,
	provider: str,
	state_id: str,
	state_name: str,
	state_updated_at: Optional[str] = None,
	event_name: Optional[str] = None,
) -> Dict[str, Any]:
	"""
	Atomically executes safe pre-fulfillment cancellation of an imported Sales Order:
	1. Checks idempotency: if already CANCELLED (docstatus=2), safely converges without duplicate actions.
	2. Evaluates downstream safety predicate (zero submitted Pick Lists, Delivery Notes, Invoices, Payments).
	3. Transitions integration_status -> CANCELLATION_PENDING.
	4. Captures (item_code, warehouse) scopes.
	5. Executes native Sales Order cancellation (so_doc.cancel()), which automatically triggers
	   ERPNext v16 cancel_stock_reservation_entries(), releasing SREs and Bin reserved stock.
	6. Updates Inventory Reservation Reference status to Cancelled.
	7. Sets integration_status -> CANCELLED and writes external state metadata.
	8. Discovers all channels sharing physical inventory pools.
	9. Persists transactional outbound publication intents (status=PENDING) into MariaDB within this transaction.
	10. Registers lightweight post-commit wake hook.

	Atomic: Any failure during cancellation or outbox persistence rolls back the savepoint cleanly.
	"""
	so_doc = frappe.get_doc("Sales Order", so_name)

	# 1. Safe Replay / Convergence Check
	if so_doc.docstatus == 2:
		# Already cancelled natively
		status = so_doc.get("integration_status")
		if status != IntegrationReadinessStatus.CANCELLED:
			frappe.db.set_value(
				"Sales Order",
				so_name,
				{
					"integration_status": IntegrationReadinessStatus.CANCELLED,
					"external_order_state": str(state_id),
					"external_order_state_name": str(state_name),
					"external_order_state_updated_at": state_updated_at or get_database_now(),
				},
				update_modified=False,
			)
		return {
			"success": True,
			"sales_order": so_name,
			"already_converged": True,
			"replayed": True,
			"reservations_released": 0,
			"outbox_persisted": 0,
		}

	# 2. Downstream Safety Predicate
	is_safe, blocking_reasons = audit_sales_order_cancellation_safety(so_name)
	if not is_safe:
		frappe.db.set_value(
			"Sales Order",
			so_name,
			{
				"integration_status": IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED,
				"external_order_state": str(state_id),
				"external_order_state_name": str(state_name),
				"external_order_state_updated_at": state_updated_at or get_database_now(),
			},
		)
		return {
			"success": False,
			"sales_order": so_name,
			"category": "DOWNSTREAM_DOCS_EXIST",
			"blocking_reasons": blocking_reasons,
		}

	# 3. Multi-resource atomic transaction protected by Savepoint
	sp_cancel = f"sp_ord_cancel_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_cancel)

	try:
		# Mark CANCELLATION_PENDING
		frappe.db.set_value(
			"Sales Order",
			so_name,
			"integration_status",
			IntegrationReadinessStatus.CANCELLATION_PENDING,
		)
		so_doc.reload()

		# 4. Capture scopes from Sales Order items and active SREs before cancellation
		reserved_scopes: List[Tuple[str, str]] = []
		for item in so_doc.items:
			wh = getattr(item, "warehouse", None)
			if item.item_code and wh:
				reserved_scopes.append((item.item_code, wh))

		# Query active SREs to record exact count
		active_sres = frappe.get_all(
			"Stock Reservation Entry",
			filters={
				"voucher_type": "Sales Order",
				"voucher_no": so_name,
				"docstatus": 1,
			},
			pluck="name",
		)
		released_count = len(active_sres)

		# 5. Native Sales Order cancellation
		so_doc.flags.ignore_permissions = True
		so_doc.cancel()

		# Ensure SREs are cancelled and update tracking references
		for sre_name in active_sres:
			if frappe.db.exists("Stock Reservation Entry", sre_name):
				sre_status = frappe.db.get_value("Stock Reservation Entry", sre_name, "docstatus")
				if sre_status == 1:
					frappe.get_doc("Stock Reservation Entry", sre_name).cancel()
			frappe.db.set_value(
				"Inventory Reservation Reference",
				{"stock_reservation_entry": sre_name},
				"status",
				"Cancelled",
			)

		# Clean up any remaining references by voucher_no
		frappe.db.sql(
			"""
			UPDATE `tabInventory Reservation Reference`
			SET status = 'Cancelled'
			WHERE source_document = %s AND status != 'Cancelled'
			""",
			(so_name,),
		)

		# 6. Set terminal integration status & external state metadata
		now_ts = state_updated_at or get_database_now()
		update_fields = {
			"integration_status": IntegrationReadinessStatus.CANCELLED,
			"external_order_state": str(state_id),
			"external_order_state_name": str(state_name),
			"external_order_state_updated_at": now_ts,
		}
		if event_name:
			update_fields["latest_integration_event"] = event_name

		frappe.db.set_value("Sales Order", so_name, update_fields, update_modified=False)

		# 7. Discover affected channels for shared inventory restoration
		channel_items_map = find_affected_channel_items_for_scopes(reserved_scopes, source_channel=sales_channel)

		# 8. Transactional Outbox Persistence (MariaDB only, inside this transaction)
		outbox_res = persist_publication_outbox_intents(channel_items_map)

		# 9. Register post-commit wake
		register_post_commit_wake()

		return {
			"success": True,
			"sales_order": so_name,
			"already_converged": False,
			"reservations_released": released_count,
			"affected_channels": sorted(list(channel_items_map.keys())),
			"channel_items": channel_items_map,
			"outbox_persisted": outbox_res.get("outbox_persisted", 0),
		}

	except Exception as cancel_err:
		try:
			frappe.db.rollback(save_point=sp_cancel)
		except Exception:
			pass
		frappe.logger("bop_erp").error(
			f"Error executing safe cancellation for order '{so_name}': {cancel_err}"
		)
		raise


def process_order_state_reconciliation_event(
	event_name: str,
	worker_id: Optional[str] = None,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Integration Event worker handler for RECONCILE_ORDER_STATE.
	Lifecycle:
	1. Atomic claim & lease fencing.
	2. Host safety assertion on connector target.
	3. Fresh external order read from remote provider.
	4. External state action resolution (provider-neutral).
	5. Resolve existing Sales Order strictly via External ID Mapping.
	6. Execute cancellation or route to review/ignore based on action.
	7. Terminal event transition (SUCCEEDED, CANCELLED, DEAD_LETTER, FAILED).
	"""
	claimed, worker_id, processing_token = claim_event_for_processing(event_name, worker_id=worker_id)
	if not claimed:
		return {
			"success": False,
			"event_name": event_name,
			"reason": "CLAIM_REJECTED_ALREADY_CLAIMED_OR_TERMINAL",
		}

	is_auth, auth_reason = verify_processing_authority(event_name, processing_token)
	if not is_auth:
		return {
			"success": False,
			"event_name": event_name,
			"reason": "LOST_PROCESSING_AUTHORITY",
			"details": auth_reason,
		}

	event_doc = frappe.get_doc("Integration Event", event_name)
	payload = {}
	raw_meta = event_doc.request_metadata
	if raw_meta:
		try:
			payload = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
		except Exception:
			payload = {}

	sales_channel = event_doc.sales_channel or payload.get("sales_channel")
	provider = event_doc.provider or payload.get("provider") or IntegrationProvider.PRESTASHOP
	external_order_id = event_doc.external_id or payload.get("external_order_id")

	# Verify connector host safety
	if not client and sales_channel:
		connector_dict = frappe.db.get_value(
			"PrestaShop Connector",
			{"sales_channel": sales_channel, "enabled": 1},
			["name", "environment", "base_url", "credential_reference", "cancellation_order_states", "review_order_states"],
			as_dict=True,
		)
		if connector_dict:
			assert_safe_connector_target(connector_dict.environment, connector_dict.base_url)
			try:
				config = PrestaShopConfig.from_connector_doc(connector_dict)
				client = PrestaShopClient(config=config)
			except Exception:
				client = None

	# 1. Fresh Remote Read at Execution Time
	current_state_id = None
	current_state_name = None
	current_date_upd = None
	raw_order = None

	if client:
		try:
			raw_order = client.get_order(external_order_id)
			current_state_id = str(raw_order.get("current_state", "")).strip()
			current_date_upd = str(raw_order.get("date_upd", "")).strip()
		except Exception as req_err:
			is_auth, _ = verify_processing_authority(event_name, processing_token)
			if is_auth:
				event_doc.reload()
				event_doc.mark_failed(
					processing_token,
					ErrorCategory.TRANSIENT,
					f"Failed to fetch fresh order from remote provider: {req_err}"[:250],
					error_category=ErrorCategory.TRANSIENT,
				)
				frappe.db.commit()
			return {"success": False, "event_name": event_name, "error": str(req_err), "category": "NETWORK"}
	else:
		current_state_id = str(payload.get("external_state_id") or "").strip()
		current_date_upd = str(payload.get("external_updated_at") or "").strip()

	# Resolve state action
	action, state_name = resolve_external_order_state_action(sales_channel, provider, current_state_id)

	# 2. Resolve Existing Sales Order Identity strictly via External ID Mapping
	existing_so = find_existing_order_mapping(sales_channel, provider, external_order_id)
	if not existing_so or not frappe.db.exists("Sales Order", existing_so):
		is_auth, _ = verify_processing_authority(event_name, processing_token)
		if is_auth:
			event_doc.reload()
			event_doc.mark_dead_letter(
				processing_token,
				ErrorCategory.NOT_FOUND,
				f"No existing Sales Order mapping found for external order '{external_order_id}' on channel '{sales_channel}'."[:250],
			)
			frappe.db.commit()
		return {
			"success": False,
			"event_name": event_name,
			"error": "EXISTING_ORDER_MAPPING_NOT_FOUND",
			"category": "NOT_FOUND",
		}

	# 3. Handle by Resolved Action
	if action == ExternalOrderStateAction.CANCEL_BEFORE_FULFILLMENT:
		try:
			is_auth, auth_reason = verify_processing_authority(event_name, processing_token)
			if not is_auth:
				return {"success": False, "event_name": event_name, "reason": "LOST_PROCESSING_AUTHORITY"}

			cancel_res = execute_sales_order_cancellation(
				so_name=existing_so,
				external_order_id=external_order_id,
				sales_channel=sales_channel,
				provider=provider,
				state_id=current_state_id,
				state_name=state_name,
				state_updated_at=current_date_upd,
				event_name=event_name,
			)

			if cancel_res.get("success"):
				event_doc.reload()
				event_doc.associate_erp_document("Sales Order", existing_so)
				event_doc.mark_succeeded(processing_token, response_metadata=cancel_res)
				frappe.db.commit()
				return {
					"success": True,
					"event_name": event_name,
					"sales_order": existing_so,
					"action": action,
					"details": cancel_res,
				}
			else:
				# Downstream documents blocked auto-cancellation
				event_doc.reload()
				event_doc.associate_erp_document("Sales Order", existing_so)
				event_doc.mark_failed(
					processing_token,
					ErrorCategory.CONFLICT,
					f"Cancellation blocked: {cancel_res.get('blocking_reasons')}"[:250],
					error_category=ErrorCategory.CONFLICT,
				)
				frappe.db.commit()
				return {
					"success": False,
					"event_name": event_name,
					"sales_order": existing_so,
					"category": cancel_res.get("category"),
					"blocking_reasons": cancel_res.get("blocking_reasons"),
				}

		except Exception as e:
			is_auth, _ = verify_processing_authority(event_name, processing_token)
			if is_auth:
				event_doc.reload()
				event_doc.mark_failed(
					processing_token,
					ErrorCategory.INTERNAL_ERROR,
					str(e)[:250],
					error_category=ErrorCategory.INTERNAL_ERROR,
				)
				frappe.db.commit()
			return {"success": False, "event_name": event_name, "error": str(e), "category": "INTERNAL_ERROR"}

	elif action == ExternalOrderStateAction.ACTIVE:
		# Check if material content modified post-ingestion
		so_doc = frappe.get_doc("Sales Order", existing_so)

		has_material_changes = False
		change_details = []

		if raw_order and client:
			try:
				raw_lines = client.get_order_details(external_order_id)
				raw_cust = client.get_customer(raw_order.get("id_customer")) if raw_order.get("id_customer") else None
				raw_deliv = client.get_address(raw_order.get("id_address_delivery")) if raw_order.get("id_address_delivery") else None
				raw_inv = client.get_address(raw_order.get("id_address_invoice")) if raw_order.get("id_address_invoice") else None

				ext_order_obj = normalize_prestashop_order(
					raw_order=raw_order,
					raw_lines=raw_lines,
					raw_customer=raw_cust,
					raw_delivery_address=raw_deliv,
					raw_invoice_address=raw_inv,
					sales_channel=sales_channel,
				)
				has_material_changes, change_details = detect_material_order_changes(so_doc, ext_order_obj)
			except Exception as norm_err:
				frappe.logger("bop_erp").warning(f"Could not compare order changes: {norm_err}")

		if has_material_changes:
			frappe.db.set_value(
				"Sales Order",
				existing_so,
				{
					"integration_status": IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED,
					"external_order_state": str(current_state_id),
					"external_order_state_name": str(state_name),
					"external_order_state_updated_at": current_date_upd or get_database_now(),
				},
			)
			event_doc.reload()
			event_doc.associate_erp_document("Sales Order", existing_so)
			event_doc.mark_failed(
				processing_token,
				ErrorCategory.VALIDATION,
				f"Material order modifications detected: {change_details}"[:250],
				error_category=ErrorCategory.VALIDATION,
			)
			frappe.db.commit()
			return {
				"success": False,
				"event_name": event_name,
				"sales_order": existing_so,
				"category": "CHANGE_REVIEW_REQUIRED",
				"details": change_details,
			}
		else:
			# Unchanged active state: update metadata and succeed
			frappe.db.set_value(
				"Sales Order",
				existing_so,
				{
					"external_order_state": str(current_state_id),
					"external_order_state_name": str(state_name),
					"external_order_state_updated_at": current_date_upd or get_database_now(),
					"latest_integration_event": event_name,
				},
				update_modified=False,
			)
			event_doc.reload()
			event_doc.associate_erp_document("Sales Order", existing_so)
			event_doc.mark_succeeded(processing_token, response_metadata={"action": "ACTIVE", "converged": True})
			frappe.db.commit()
			return {"success": True, "event_name": event_name, "sales_order": existing_so, "action": "ACTIVE"}

	elif action == ExternalOrderStateAction.REVIEW_REQUIRED:
		frappe.db.set_value(
			"Sales Order",
			existing_so,
			{
				"integration_status": IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED,
				"external_order_state": str(current_state_id),
				"external_order_state_name": str(state_name),
				"external_order_state_updated_at": current_date_upd or get_database_now(),
				"latest_integration_event": event_name,
			},
		)
		event_doc.reload()
		event_doc.associate_erp_document("Sales Order", existing_so)
		event_doc.mark_failed(
			processing_token,
			ErrorCategory.CONFLICT,
			f"External order transitioned to state '{current_state_id}' ({state_name}) requiring operational review."[:250],
			error_category=ErrorCategory.CONFLICT,
		)
		frappe.db.commit()
		return {
			"success": False,
			"event_name": event_name,
			"sales_order": existing_so,
			"category": "REVIEW_REQUIRED",
			"state_name": state_name,
		}

	else:  # ExternalOrderStateAction.IGNORE
		event_doc.reload()
		event_doc.cancel(
			reason=f"Ignored order state '{current_state_id}' ({state_name})"[:250],
			processing_token=processing_token,
		)
		frappe.db.commit()
		return {"success": True, "event_name": event_name, "action": "IGNORE"}
