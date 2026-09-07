# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional
import frappe
from frappe import _

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
)
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.inventory.publication import (
	normalize_publishable_quantity,
	resolve_item_mapping,
)
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.sync import get_active_connector_for_channel


def get_channel_inventory_reconciliation(
	sales_channel: str,
	item_codes: Optional[List[str]] = None,
	limit: int = 100,
	client: Optional[PrestaShopClient] = None,
) -> List[Dict[str, Any]]:
	"""
	Generates an auditable, read-only reconciliation report between ERP Channel ATP and remote PrestaShop stock.
	Does NOT execute any outbound write.
	Returns detailed row for each item:
	- item_code
	- erp_atp
	- publishable_qty
	- remote_qty
	- delta (publishable_qty - remote_qty)
	- mapping_status: 'MAPPED' or 'MAPPING_MISSING'
	- stock_status: 'SYNCED', 'DRIFT', or 'UNMAPPED'
	- stock_available_id
	- external_product_id
	- external_variant_id
	"""
	if not item_codes:
		# Retrieve all ERP Items that have active mappings or are sellable
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
		item_codes = list({m.erp_document for m in mappings})

	if client is None:
		connector = get_active_connector_for_channel(sales_channel)
		config = PrestaShopConfig.from_connector_doc(connector)
		client = PrestaShopClient(config=config)

	report_rows = []
	for ic in item_codes:
		# 1. Compute ERP ATP
		try:
			atp_obj = get_channel_atp(ic, sales_channel)
			atp_qty = atp_obj.aggregate_atp_qty
			pub_qty = normalize_publishable_quantity(atp_qty, ic)
		except Exception as e:
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
			stock_status = "SYNCED" if delta == 0 else "DRIFT"

			report_rows.append({
				"item_code": ic,
				"sales_channel": sales_channel,
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
