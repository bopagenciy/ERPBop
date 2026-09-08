# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional, Tuple
import frappe
from frappe import _
from frappe.utils import flt

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
	IntegrationDirection,
	IntegrationStatus,
)
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.inventory.publication import (
	normalize_publishable_quantity,
	resolve_item_mapping,
	schedule_channel_inventory_publication,
)
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.sync import get_active_connector_for_channel


def get_canonical_publishable_quantity(
	sales_channel: str,
	item_code: str,
	provider: str = IntegrationProvider.PRESTASHOP,
) -> Tuple[int, float]:
	"""
	Canonical source of truth for publishable quantity across ERPBop.
	Used identically by:
	1. Normal outbound publication pipeline (`publish_item_inventory`)
	2. Outbound Integration Event scheduler
	3. Drift detection reconciler (`reconcile_inventory_item`)
	4. Periodic channel reconciliation jobs
	
	Returns:
	(publishable_qty: int, raw_atp: float)
	"""
	atp_obj = get_channel_atp(item_code, sales_channel)
	raw_atp = atp_obj.aggregate_atp_qty
	publishable_qty = normalize_publishable_quantity(raw_atp, item_code)
	return publishable_qty, raw_atp


def get_channel_inventory_reconciliation(
	sales_channel: str,
	item_codes: Optional[List[str]] = None,
	limit: int = 100,
	client: Optional[PrestaShopClient] = None,
	provider: str = IntegrationProvider.PRESTASHOP,
) -> List[Dict[str, Any]]:
	"""
	Generates an auditable, read-only reconciliation report between ERP Channel ATP and remote PrestaShop stock.
	Does NOT execute any outbound write.
	Returns detailed row for each item:
	- item_code
	- sales_channel
	- provider
	- erp_atp
	- publishable_qty
	- remote_qty
	- delta (publishable_qty - remote_qty)
	- mapping_status: 'MAPPED' or 'MAPPING_MISSING'
	- stock_status: 'IN_SYNC', 'DRIFTED', 'UNMAPPED', or 'REMOTE_ERROR'
	- stock_available_id
	- external_product_id
	- external_variant_id
	"""
	if not item_codes:
		# Retrieve all ERP Items that have active mappings
		mappings = frappe.get_all(
			"External ID Mapping",
			filters={
				"sales_channel": sales_channel,
				"erp_doctype": "Item",
				"active": 1,
				"external_entity_type": ["in", [ExternalEntityType.PRODUCT, ExternalEntityType.PRODUCT_VARIANT]],
			},
			fields=["erp_document"],
			limit=limit,
		)
		item_codes = sorted(list({m.erp_document for m in mappings}))

	if client is None and provider == IntegrationProvider.PRESTASHOP:
		connector = get_active_connector_for_channel(sales_channel)
		config = PrestaShopConfig.from_connector_doc(connector)
		client = PrestaShopClient(config=config)

	report_rows = []
	for ic in item_codes:
		# 1. Compute canonical ERP ATP
		try:
			pub_qty, atp_qty = get_canonical_publishable_quantity(sales_channel, ic, provider=provider)
		except Exception:
			atp_qty = 0.0
			pub_qty = 0

		# 2. Check Mapping
		try:
			mapping = resolve_item_mapping(sales_channel, ic)
			prod_id = mapping["product_id"]
			var_id = mapping["variant_id"]
			mapping_status = "MAPPED"
		except Exception:
			report_rows.append({
				"item_code": ic,
				"sales_channel": sales_channel,
				"provider": provider,
				"erp_atp": atp_qty,
				"publishable_qty": pub_qty,
				"remote_qty": None,
				"delta": None,
				"mapping_status": "MAPPING_MISSING",
				"stock_status": "UNMAPPED",
				"stock_available_id": None,
				"external_product_id": None,
				"external_variant_id": None,
			})
			continue

		# 3. Resolve remote stock
		try:
			sa_id = client.resolve_stock_available_id(prod_id, var_id)
			sa_detail = client.get_stock_available(sa_id)
			remote_qty = int(sa_detail.get("quantity", 0))
			delta = pub_qty - remote_qty
			stock_status = "IN_SYNC" if delta == 0 else "DRIFTED"

			if stock_status == "IN_SYNC":
				frappe.logger("bop_erp").info(
					f"RECONCILIATION_IN_SYNC channel={sales_channel} item={ic} expected={pub_qty} remote={remote_qty}"
				)
			else:
				frappe.logger("bop_erp").warning(
					f"RECONCILIATION_DRIFT_DETECTED channel={sales_channel} item={ic} expected={pub_qty} remote={remote_qty} delta={delta}"
				)

			report_rows.append({
				"item_code": ic,
				"sales_channel": sales_channel,
				"provider": provider,
				"erp_atp": atp_qty,
				"publishable_qty": pub_qty,
				"remote_qty": remote_qty,
				"delta": delta,
				"mapping_status": mapping_status,
				"stock_status": stock_status,
				"stock_available_id": sa_id,
				"external_product_id": prod_id,
				"external_variant_id": var_id,
			})
		except Exception as sa_err:
			report_rows.append({
				"item_code": ic,
				"sales_channel": sales_channel,
				"provider": provider,
				"erp_atp": atp_qty,
				"publishable_qty": pub_qty,
				"remote_qty": None,
				"delta": None,
				"mapping_status": mapping_status,
				"stock_status": f"REMOTE_ERROR: {sa_err}",
				"stock_available_id": None,
				"external_product_id": prod_id,
				"external_variant_id": var_id,
			})

	return report_rows


def reconcile_inventory_item(
	sales_channel: str,
	item_code: str,
	provider: str = IntegrationProvider.PRESTASHOP,
	repair: bool = True,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Targeted reconciliation service for a single item and sales channel.
	Audits expected vs actual remote inventory.
	If drift is detected and repair=True, routes repair strictly through the
	durable outbox path (creating/coalescing an outbound Integration Event).
	"""
	rows = get_channel_inventory_reconciliation(
		sales_channel=sales_channel,
		item_codes=[item_code],
		client=client,
		provider=provider,
	)
	if not rows:
		return {
			"sales_channel": sales_channel,
			"item_code": item_code,
			"provider": provider,
			"status": "UNMAPPED",
			"repaired": False,
		}

	result_row = rows[0]
	stock_status = result_row.get("stock_status")

	repair_event = None
	if stock_status == "DRIFTED" and repair:
		# Route repair strictly through durable transactional outbox!
		events = schedule_channel_inventory_publication(
			sales_channel=sales_channel,
			item_codes=[item_code],
		)
		if events:
			repair_event = events[0]
			frappe.logger("bop_erp").info(
				f"RECONCILIATION_REPAIR_ENQUEUED channel={sales_channel} item={item_code} outbox_event={repair_event}"
			)

	return {
		"sales_channel": sales_channel,
		"item_code": item_code,
		"provider": provider,
		"erp_atp": result_row.get("erp_atp"),
		"publishable_qty": result_row.get("publishable_qty"),
		"remote_qty": result_row.get("remote_qty"),
		"delta": result_row.get("delta"),
		"stock_status": stock_status,
		"mapping_status": result_row.get("mapping_status"),
		"repair_enqueued": bool(repair_event),
		"repair_event": repair_event,
	}


def reconcile_channel_inventory(
	sales_channel: str,
	provider: str = IntegrationProvider.PRESTASHOP,
	repair: bool = False,
	limit: int = 100,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Bounded multi-item channel reconciliation service suitable for administrative triggers
	or scheduled batch execution.
	"""
	report_rows = get_channel_inventory_reconciliation(
		sales_channel=sales_channel,
		limit=limit,
		client=client,
		provider=provider,
	)

	in_sync_count = 0
	drifted_count = 0
	unmapped_count = 0
	error_count = 0
	drifted_items: List[str] = []

	for row in report_rows:
		st = row.get("stock_status")
		if st == "IN_SYNC":
			in_sync_count += 1
		elif st == "DRIFTED":
			drifted_count += 1
			drifted_items.append(row["item_code"])
		elif st == "UNMAPPED":
			unmapped_count += 1
		else:
			error_count += 1

	repair_events = []
	if repair and drifted_items:
		repair_events = schedule_channel_inventory_publication(
			sales_channel=sales_channel,
			item_codes=drifted_items,
		)
		frappe.logger("bop_erp").info(
			f"RECONCILIATION_REPAIR_ENQUEUED channel={sales_channel} count={len(repair_events)}"
		)

	return {
		"sales_channel": sales_channel,
		"provider": provider,
		"total_items": len(report_rows),
		"in_sync": in_sync_count,
		"drifted": drifted_count,
		"unmapped": unmapped_count,
		"errors": error_count,
		"repairs_enqueued": len(repair_events),
		"repair_events": repair_events,
		"report": report_rows,
	}


def repair_channel_inventory_drift(
	sales_channel: str,
	item_codes: Optional[List[str]] = None,
	limit: int = 100,
	client: Optional[PrestaShopClient] = None,
) -> List[Dict[str, Any]]:
	"""
	Backward-compatible helper for Phase 1J tests.
	Republishes authoritative ERP inventory through publish_item_inventory.
	"""
	from bop_erp.inventory.publication import publish_item_inventory

	reconciliation_rows = get_channel_inventory_reconciliation(
		sales_channel=sales_channel,
		item_codes=item_codes,
		limit=limit,
		client=client,
	)

	repair_results = []
	for row in reconciliation_rows:
		if row.get("stock_status") in ("DRIFT", "DRIFTED"):
			ic = row["item_code"]
			res = publish_item_inventory(
				sales_channel=sales_channel,
				item_code=ic,
				client=client,
			)
			repair_results.append(res)

	return repair_results


