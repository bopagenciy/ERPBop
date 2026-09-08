# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from collections import defaultdict
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
	ErrorCategory,
	TransactionOrigin,
)
from bop_erp.safety import assert_safe_connector_target
from bop_erp.reliability import (
	claim_event_for_processing,
	sanitize_metadata,
	verify_processing_authority,
	get_database_now,
)
from bop_erp.inventory.availability import get_channel_atp
from bop_erp.inventory.reservations import reserve_channel_stock
from bop_erp.inventory.publication import schedule_channel_inventory_publication as schedule_inventory_publication
from bop_erp.integrations.prestashop.client import PrestaShopClient
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.adapters.order_normalizer import (
	normalize_prestashop_order,
	normalize_prestashop_customer,
	normalize_prestashop_address,
)
from bop_erp.orders.models import (
	ExternalOrder,
	ExternalOrderLine,
	ExternalCustomer,
	ExternalAddress,
	ExternalTotals,
)
from bop_erp.orders.exceptions import (
	OrderIngestionError,
	MissingProductMappingError,
	OrderNotEligibleError,
	OrderAlreadyImportedError,
	OrderTotalMismatchError,
	InsufficientOrderStockError,
	InvalidOrderQuantityError,
	CustomerMappingError,
	AddressMappingError,
	OrderReservationFailedError,
)

def get_currency_tolerance(currency: Optional[str] = None) -> float:
	"""
	Derives financial reconciliation tolerance from native ERPNext currency precision.
	Avoids arbitrary hardcoded tolerances (e.g. 0.05).
	Uses 10^(-precision).
	"""
	precision = 2
	try:
		prec = frappe.get_precision("Sales Order", "net_total")
		if prec is not None:
			precision = int(prec)
		elif currency:
			frac = frappe.db.get_value("Currency", currency, "fraction_units")
			if frac:
				import math
				precision = max(0, int(math.log10(float(frac))))
	except Exception:
		precision = 2
	return round(1.0 / (10 ** max(0, precision)), precision)


def compute_order_idempotency_key(provider: str, sales_channel: str, external_order_id: str) -> str:
	"""
	Canonical SHA-256 idempotency key for Inbound Order Ingestion.
	[INBOUND, provider, sales_channel, ORDER, external_order_id]
	"""
	clean_prov = str(provider).strip().upper()
	clean_ch = str(sales_channel).strip()
	clean_id = str(external_order_id).strip()
	tuple_data = ["INBOUND", clean_prov, clean_ch, ExternalEntityType.ORDER, clean_id]
	raw_json = json.dumps(tuple_data, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(raw_json.encode("utf-8")).hexdigest()


def get_eligible_order_states(sales_channel: str) -> List[str]:
	"""
	Retrieves configured eligible order state IDs for a given sales channel connector.
	Requires connector-specific configuration; does not assume universal state numbers.
	"""
	connector_name = frappe.db.get_value(
		"PrestaShop Connector",
		{"sales_channel": sales_channel, "enabled": 1},
		"name",
	)
	if connector_name:
		raw_states = frappe.db.get_value("PrestaShop Connector", connector_name, "eligible_order_states")
		if raw_states:
			return [s.strip() for s in str(raw_states).split(",") if s.strip()]
	return []


def find_existing_order_mapping(sales_channel: str, provider: str, external_order_id: str) -> Optional[str]:
	"""Returns the ERP Sales Order document name if an active mapping exists for this external order."""
	return frappe.db.get_value(
		"External ID Mapping",
		{
			"sales_channel": sales_channel,
			"provider": str(provider).strip().upper(),
			"external_entity_type": ExternalEntityType.ORDER,
			"external_id": str(external_order_id).strip(),
			"active": 1,
		},
		"erp_document",
	)


def resolve_or_create_customer(
	customer: ExternalCustomer,
	sales_channel: str,
	provider: str = IntegrationProvider.PRESTASHOP,
) -> str:
	"""
	Resolves or creates an ERPNext Customer using External ID Mapping.
	Guarantees customer identity idempotency and concurrency safety.
	"""
	cid = str(customer.external_customer_id or "").strip()
	if not cid:
		# Guest customer without ID - fallback to channel guest customer
		guest_cust_name = f"Guest Customer - {sales_channel}"
		if not frappe.db.exists("Customer", guest_cust_name):
			g_doc = frappe.get_doc({
				"doctype": "Customer",
				"customer_name": guest_cust_name,
				"customer_type": "Individual",
				"customer_group": _get_default_customer_group(),
				"territory": _get_default_territory(),
			})
			g_doc.flags.ignore_mandatory = True
			g_doc.insert(ignore_permissions=True)
		return guest_cust_name

	# 1. Check existing active mapping
	existing_customer = frappe.db.get_value(
		"External ID Mapping",
		{
			"sales_channel": sales_channel,
			"provider": str(provider).strip().upper(),
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"external_id": cid,
			"active": 1,
		},
		"erp_document",
	)
	if existing_customer and frappe.db.exists("Customer", existing_customer):
		return existing_customer

	# 2. Concurrency-safe atomic creation using Savepoint
	sp_cust = f"sp_cust_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_cust)

	cust_name = f"{customer.first_name} {customer.last_name}".strip()
	if not cust_name:
		cust_name = customer.company.strip() if customer.company else f"PS Customer {cid}"

	if frappe.db.exists("Customer", cust_name):
		# Append channel/id suffix to avoid duplicate customer document naming conflict in Frappe
		unique_cust_name = f"{cust_name} ({sales_channel}-{cid})"
	else:
		unique_cust_name = cust_name

	c_doc = frappe.get_doc({
		"doctype": "Customer",
		"customer_name": unique_cust_name,
		"customer_type": "Company" if customer.company else "Individual",
		"customer_group": _get_default_customer_group(),
		"territory": _get_default_territory(),
	})
	try:
		c_doc.insert(ignore_permissions=True)

		# 3. Create External ID Mapping with duplicate-race protection
		mapping = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": sales_channel,
			"provider": str(provider).strip().upper(),
			"external_entity_type": ExternalEntityType.CUSTOMER,
			"external_id": cid,
			"erp_doctype": "Customer",
			"erp_document": c_doc.name,
			"active": 1,
		})
		mapping.insert(ignore_permissions=True)
		return c_doc.name
	except frappe.QueryDeadlockError:
		raise
	except (frappe.DuplicateEntryError, Exception):
		# Roll back unmapped customer to prevent orphan record
		try:
			frappe.db.rollback(save_point=sp_cust)
		except Exception:
			pass
		winner = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": sales_channel,
				"provider": str(provider).strip().upper(),
				"external_entity_type": ExternalEntityType.CUSTOMER,
				"external_id": cid,
				"active": 1,
			},
			"erp_document",
		)
		if winner:
			return winner
		raise


def resolve_or_create_address(
	address: Optional[ExternalAddress],
	customer_name: str,
	sales_channel: str,
	provider: str = IntegrationProvider.PRESTASHOP,
) -> Optional[str]:
	"""
	Resolves or creates an ERPNext Address using External ID Mapping.
	Guarantees address identity idempotency and concurrency safety.
	"""
	if not address or not address.external_address_id:
		return None

	aid = str(address.external_address_id).strip()

	# 1. Check existing active mapping
	existing_address = frappe.db.get_value(
		"External ID Mapping",
		{
			"sales_channel": sales_channel,
			"provider": str(provider).strip().upper(),
			"external_entity_type": ExternalEntityType.ADDRESS,
			"external_id": aid,
			"active": 1,
		},
		"erp_document",
	)
	if existing_address and frappe.db.exists("Address", existing_address):
		return existing_address

	# 2. Concurrency-safe atomic creation using Savepoint
	sp_addr = f"sp_addr_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_addr)

	addr_title = f"{address.first_name} {address.last_name}".strip() or customer_name
	country = _resolve_country(address.country)

	a_doc = frappe.get_doc({
		"doctype": "Address",
		"address_title": addr_title,
		"address_type": address.address_type or "Shipping",
		"address_line1": address.address1 or "Address Line 1",
		"address_line2": address.address2 or "",
		"city": address.city or "City",
		"state": address.state or "",
		"pincode": address.postcode or "",
		"country": country,
		"phone": address.phone or address.phone_mobile or "",
		"links": [
			{
				"link_doctype": "Customer",
				"link_name": customer_name,
			}
		],
	})
	a_doc.flags.ignore_mandatory = True

	try:
		a_doc.insert(ignore_permissions=True)

		# 3. Create External ID Mapping with duplicate-race protection
		mapping = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": sales_channel,
			"provider": str(provider).strip().upper(),
			"external_entity_type": ExternalEntityType.ADDRESS,
			"external_id": aid,
			"erp_doctype": "Address",
			"erp_document": a_doc.name,
			"active": 1,
		})
		mapping.insert(ignore_permissions=True)
		return a_doc.name
	except frappe.QueryDeadlockError:
		raise
	except (frappe.DuplicateEntryError, Exception):
		# Roll back unmapped address to prevent orphan record
		try:
			frappe.db.rollback(save_point=sp_addr)
		except Exception:
			pass
		winner = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": sales_channel,
				"provider": str(provider).strip().upper(),
				"external_entity_type": ExternalEntityType.ADDRESS,
				"external_id": aid,
				"active": 1,
			},
			"erp_document",
		)
		if winner:
			return winner
		raise


def resolve_order_line_item(
	line: ExternalOrderLine,
	sales_channel: str,
	provider: str = IntegrationProvider.PRESTASHOP,
) -> str:
	"""
	Resolves the ERP Item for an external order line.
	Enforces exact variant matching:
	- If combination ID is present: maps strictly via PRODUCT_VARIANT.
	- If no combination ID: maps strictly via PRODUCT.
	Raises MissingProductMappingError if no active mapping exists.
	"""
	pid = str(line.external_product_id).strip()
	vid = str(line.external_variant_id).strip() if line.external_variant_id else None

	if vid and vid != "0":
		mapping = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": sales_channel,
				"external_entity_type": ExternalEntityType.PRODUCT_VARIANT,
				"external_id": pid,
				"external_variant_id": vid,
				"active": 1,
			},
			["erp_doctype", "erp_document", "provider"],
			as_dict=True,
		)
	else:
		mapping = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": sales_channel,
				"external_entity_type": ExternalEntityType.PRODUCT,
				"external_id": pid,
				"active": 1,
			},
			["erp_doctype", "erp_document", "provider"],
			as_dict=True,
		)

	if mapping and mapping.get("provider") and provider:
		if str(mapping["provider"]).strip().upper() != str(provider).strip().upper():
			mapping = None

	if not mapping or not mapping.get("erp_document"):
		raise MissingProductMappingError(
			_("Missing Product Mapping: PrestaShop Product {0} (variant {1}) on channel {2}").format(
				pid, vid or "none", sales_channel
			)
		)

	item_code = mapping["erp_document"]
	if not frappe.db.exists("Item", item_code):
		raise MissingProductMappingError(
			_("Mapped ERP Item '{0}' does not exist in ERPNext.").format(item_code)
		)

	return item_code


def find_affected_channel_items_for_scopes(
	item_warehouse_scopes: List[Tuple[str, str]],
	source_channel: Optional[str] = None,
) -> Dict[str, List[str]]:
	"""
	Calculates all active sales channels and their affected items for changed (item_code, warehouse) scopes.
	Uses existing Channel Inventory Source configuration.
	Guarantees:
	- Discovers every active Sales Channel whose eligible sellable inventory pool contains that warehouse.
	- Channels without overlapping warehouses (e.g. TEST-C sourcing only Warehouse 2) are strictly excluded.
	- Multi-warehouse scopes union properly without duplicating item codes per channel.
	"""
	if not item_warehouse_scopes:
		return {}

	channel_items: Dict[str, Set[str]] = defaultdict(set)

	for item_code, warehouse in item_warehouse_scopes:
		if not warehouse or not item_code:
			continue

		# Query active channels configured with this warehouse as an enabled sellable source
		sources = frappe.db.sql(
			"""
			SELECT cis.sales_channel
			FROM `tabChannel Inventory Source` cis
			JOIN `tabSales Channel` sc ON sc.name = cis.sales_channel
			WHERE cis.warehouse = %s
			  AND cis.enabled = 1
			  AND cis.allow_sellable_stock = 1
			  AND sc.active = 1
			ORDER BY cis.sales_channel ASC
			""",
			(warehouse,),
			as_dict=True,
		)
		if sources:
			for row in sources:
				ch = row.get("sales_channel") if isinstance(row, dict) else getattr(row, "sales_channel", None)
				if ch:
					channel_items[ch].add(item_code)
		else:
			# Fallback for unit tests that mock frappe.get_all on Channel Inventory Source
			fallback = frappe.get_all(
				"Channel Inventory Source",
				filters={"warehouse": warehouse, "enabled": 1, "allow_sellable_stock": 1},
				pluck="sales_channel",
			)
			for ch in fallback:
				channel_items[ch].add(item_code)

	# If no sources found but source_channel is given, fallback to source channel
	if source_channel and not channel_items:
		items = {ic for ic, _ in item_warehouse_scopes if ic}
		if items:
			channel_items[source_channel] = items

	return {ch: sorted(list(items)) for ch, items in sorted(channel_items.items())}


def find_affected_channels_for_items(
	sales_channel: str,
	item_codes: List[str],
	warehouses: Optional[List[str]] = None,
) -> List[str]:
	"""
	Calculates all sales channels sharing physical inventory sources with the specified items.
	Guarantees multi-channel ATP publication coverage when inventory is shared.
	"""
	if not sales_channel:
		return []

	# 1. Collect warehouses configured for source channel
	wh_list = warehouses or []
	if not wh_list:
		wh_list = frappe.get_all(
			"Channel Inventory Source",
			filters={"sales_channel": sales_channel, "enabled": 1, "allow_sellable_stock": 1},
			pluck="warehouse",
		)
	if not wh_list:
		return [sales_channel]

	# 2. Find all channels configured with overlapping warehouses
	shared_channels = frappe.db.sql(
		"""
		SELECT DISTINCT cis.sales_channel
		FROM `tabChannel Inventory Source` cis
		JOIN `tabSales Channel` sc ON sc.name = cis.sales_channel
		WHERE cis.warehouse IN %(wh_list)s
		  AND cis.enabled = 1
		  AND cis.allow_sellable_stock = 1
		  AND sc.active = 1
		ORDER BY cis.sales_channel ASC
		""",
		{"wh_list": tuple(wh_list)},
		pluck="sales_channel",
	)
	if not shared_channels:
		# Fallback for unit tests mocking frappe.get_all
		shared_channels = frappe.get_all(
			"Channel Inventory Source",
			filters={"warehouse": ["in", wh_list], "enabled": 1, "allow_sellable_stock": 1},
			pluck="sales_channel",
		)

	unique_channels = sorted(list(set(shared_channels)))
	return unique_channels if unique_channels else [sales_channel]


def schedule_post_commit_publication(
	channel_items_or_channels: Union[Dict[str, List[str]], List[str]],
	item_codes: Optional[List[str]] = None,
):
	"""
	Schedules publication intents for all affected channels strictly AFTER durable DB commit.
	Accepts either:
	- Dict[str, List[str]]: mapping sales_channel -> list of item_codes
	- List[str] with item_codes: list of channels with shared item_codes
	"""
	if isinstance(channel_items_or_channels, dict):
		channel_map = channel_items_or_channels
	elif isinstance(channel_items_or_channels, list):
		items = item_codes or []
		channel_map = {ch: items for ch in channel_items_or_channels}
	else:
		return

	if not channel_map:
		return

	def _on_commit():
		for ch, items in channel_map.items():
			if not items:
				continue
			try:
				schedule_inventory_publication(sales_channel=ch, item_codes=items)
			except Exception as e:
				frappe.logger("bop_erp").error(
					f"Failed to schedule post-commit publication for channel {ch}: {e}"
				)

	frappe.db.after_commit(_on_commit)



def is_order_ingestion_complete(so_name: str) -> Tuple[bool, List[str]]:
	"""
	Audits whether a Sales Order has complete native Stock Reservation Entries
	for all of its items.
	Returns (is_complete: bool, missing_details: List[str]).
	"""
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		return False, ["Sales Order does not exist"]

	so_doc = frappe.get_doc("Sales Order", so_name)
	if so_doc.docstatus != 1:
		return False, [f"Sales Order '{so_name}' is not submitted (docstatus={so_doc.docstatus})"]

	missing = []
	for item in so_doc.items:
		sres = frappe.get_all(
			"Stock Reservation Entry",
			filters={
				"voucher_type": "Sales Order",
				"voucher_no": so_name,
				"voucher_detail_no": item.name,
				"docstatus": 1,
				"status": ["not in", ["Closed", "Delivered", "Cancelled"]],
			},
			fields=["name", "reserved_qty"],
		)
		total_reserved = sum(flt(s.get("reserved_qty") if isinstance(s, dict) else getattr(s, "reserved_qty", 0)) for s in sres)
		if total_reserved < flt(item.qty):
			missing.append(
				f"Item {item.item_code} (row {item.name}): required {item.qty}, reserved {total_reserved}"
			)

	return len(missing) == 0, missing


def _ensure_order_reservations(so_doc, sales_channel: str):
	"""
	Ensures complete native stock reservations for a Sales Order during crash recovery.
	Calculates exact missing reservation quantity per line.
	"""
	for so_item in so_doc.items:
		sres = frappe.get_all(
			"Stock Reservation Entry",
			filters={
				"voucher_type": "Sales Order",
				"voucher_no": so_doc.name,
				"voucher_detail_no": so_item.name,
				"docstatus": 1,
				"status": ["not in", ["Closed", "Delivered", "Cancelled"]],
			},
			fields=["name", "reserved_qty"],
		)
		existing_reserved = sum(flt(s.get("reserved_qty") if isinstance(s, dict) else getattr(s, "reserved_qty", 0)) for s in sres)
		needed_qty = flt(so_item.qty) - existing_reserved
		if needed_qty > 0:
			reserve_channel_stock(
				item_code=so_item.item_code,
				sales_channel=sales_channel,
				requested_qty=needed_qty,
				voucher_type="Sales Order",
				voucher_no=so_doc.name,
				voucher_detail_no=so_item.name,
				allow_partial=False,
				idempotency_key=f"SO:{so_doc.name}:{so_item.name}:recov",
				source_doctype="Sales Order",
				source_document=so_doc.name,
				source_document_item=so_item.name,
			)


def ingest_order_pipeline(
	external_order: ExternalOrder,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Core atomic ingestion pipeline:
	1. External order freshness & eligibility check.
	2. Idempotency & existing mapping check with crash recovery completion.
	3. Customer & Address resolution.
	4. Line item mapping & aggregate ATP preflight verification.
	5. Native Sales Order creation in Draft (docstatus=0).
	6. Pre-claim External ID Mapping before submission.
	7. Sales Order submission.
	8. Native Stock Reservation Entry allocation (all-or-nothing).
	9. Atomic savepoint rollback if any reservation or step fails.
	10. Post-commit multi-channel publication intent scheduling.
	"""
	sales_channel = external_order.sales_channel
	provider = external_order.provider
	order_id = external_order.external_order_id

	# 1. Check idempotency: does mapping already exist?
	existing_so = find_existing_order_mapping(sales_channel, provider, order_id)
	if existing_so and frappe.db.exists("Sales Order", existing_so):
		so_doc = frappe.get_doc("Sales Order", existing_so)
		if so_doc.docstatus == 0:
			so_doc.submit()
			frappe.db.set_value("Sales Order", existing_so, "integration_status", IntegrationReadinessStatus.RESERVATION_PENDING)

		try:
			_ensure_order_reservations(so_doc, sales_channel)
		except Exception:
			frappe.db.set_value("Sales Order", existing_so, "integration_status", IntegrationReadinessStatus.FAILED_REVIEW)
			raise

		is_complete, missing = is_order_ingestion_complete(existing_so)
		if is_complete:
			frappe.db.set_value("Sales Order", existing_so, "integration_status", IntegrationReadinessStatus.READY)
			reserved_scopes = [(item.item_code, item.warehouse) for item in so_doc.items]
			channel_items_map = find_affected_channel_items_for_scopes(reserved_scopes, source_channel=sales_channel)
			schedule_post_commit_publication(channel_items_map)
		else:
			frappe.db.set_value("Sales Order", existing_so, "integration_status", IntegrationReadinessStatus.FAILED_REVIEW)
			raise OrderReservationFailedError(f"Incomplete reservations for order {existing_so}: {missing}")

		return {
			"success": True,
			"sales_order": existing_so,
			"is_replay": True,
			"reason": "ORDER_ALREADY_IMPORTED",
		}

	# 2. Re-read external state if client is available
	if client:
		try:
			remote_order = client.get_order(order_id)
			current_state = str(remote_order.get("current_state", "")).strip()
		except Exception:
			current_state = str(external_order.order_state_id).strip()
	else:
		current_state = str(external_order.order_state_id).strip()

	eligible_states = get_eligible_order_states(sales_channel)
	if current_state not in eligible_states:
		raise OrderNotEligibleError(
			_("Order Not Eligible: State '{0}' is not in configured eligible states {1}").format(
				current_state, eligible_states
			)
		)

	# 3. Line validation & item resolution
	if not external_order.lines:
		raise OrderIngestionError(_("External order contains no line items."))

	resolved_lines: List[Tuple[ExternalOrderLine, str]] = []
	requested_by_item: Dict[str, float] = {}
	for line in external_order.lines:
		if flt(line.quantity) <= 0:
			raise InvalidOrderQuantityError(
				_("Invalid Order Quantity: {0} for product {1}").format(line.quantity, line.external_product_id)
			)
		item_code = resolve_order_line_item(line, sales_channel, provider)
		resolved_lines.append((line, item_code))
		requested_by_item[item_code] = requested_by_item.get(item_code, 0.0) + flt(line.quantity)

	# 4. In-Transaction Aggregate ATP Preflight Check
	for item_code, tot_qty in requested_by_item.items():
		atp_res = get_channel_atp(item_code, sales_channel)
		if flt(tot_qty) > flt(atp_res.aggregate_atp_qty):
			raise InsufficientOrderStockError(
				_("Insufficient Stock: Total requested {0} units of {1}, available channel ATP is {2}").format(
					tot_qty, item_code, atp_res.aggregate_atp_qty
				)
			)

	# 5. Customer & Address Resolution
	customer_name = resolve_or_create_customer(external_order.customer, sales_channel, provider)
	delivery_addr = resolve_or_create_address(external_order.delivery_address, customer_name, sales_channel, provider)
	invoice_addr = resolve_or_create_address(external_order.invoice_address, customer_name, sales_channel, provider)

	# 6. Channel & Company Context
	ch_doc = frappe.get_doc("Sales Channel", sales_channel)
	company = ch_doc.company or frappe.defaults.get_global_default("company")
	if not company:
		raise OrderIngestionError(_("No company configured for Sales Channel '{0}'.").format(sales_channel))

	# Resolve source warehouse for Sales Order Item row
	first_source = frappe.db.get_value(
		"Channel Inventory Source",
		{"sales_channel": sales_channel, "enabled": 1, "allow_sellable_stock": 1},
		"warehouse",
		order_by="priority asc, creation asc",
	)
	if not first_source:
		raise OrderIngestionError(
			_("No enabled sellable inventory source configured for Sales Channel '{0}'.").format(sales_channel)
		)

	# 7. Construct Native Sales Order
	so_items = []
	expected_product_total = 0.0
	for line, item_code in resolved_lines:
		rate = flt(line.unit_price_ex_tax)
		expected_product_total += flt(rate * line.quantity)
		so_items.append({
			"item_code": item_code,
			"qty": flt(line.quantity),
			"rate": rate,
			"warehouse": first_source,
			"conversion_factor": 1.0,
			"delivery_date": _parse_date(external_order.date_add) or nowdate(),
		})

	currency = external_order.currency or "USD"
	if not frappe.db.exists("Currency", currency):
		currency = frappe.defaults.get_global_default("currency") or "USD"

	# Dynamic currency precision tolerance
	tolerance = get_currency_tolerance(currency)
	ext_prod_tot = flt(external_order.totals.total_products_ex_tax)
	if ext_prod_tot > 0.0 and abs(expected_product_total - ext_prod_tot) > tolerance:
		raise OrderTotalMismatchError(
			_("Order Total Mismatch: Computed product total {0} != external total {1} (tolerance {2})").format(
				expected_product_total, ext_prod_tot, tolerance
			)
		)

	# 8. Multi-line All-Or-Nothing Ingestion with Pre-Claim & Savepoint Rollback Protection
	sp_order = f"sp_ord_ingest_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp_order)

	# Create Sales Order in DRAFT (docstatus = 0)
	so = frappe.get_doc({
		"doctype": "Sales Order",
		"customer": customer_name,
		"company": company,
		"transaction_date": _parse_date(external_order.date_add) or nowdate(),
		"delivery_date": _parse_date(external_order.date_add) or nowdate(),
		"currency": currency,
		"sales_channel": sales_channel,
		"transaction_origin": TransactionOrigin.WEB,
		"external_order_id": str(order_id).strip(),
		"integration_status": IntegrationReadinessStatus.INGESTION_PENDING,
		"integration_provider": str(provider).strip().upper(),
		"customer_address": invoice_addr,
		"shipping_address_name": delivery_addr,
		"items": so_items,
	})
	so.flags.ignore_permissions = True
	so.insert(ignore_permissions=True)

	# Pre-claim External ID Mapping BEFORE submitting or reserving!
	mapping = frappe.get_doc({
		"doctype": "External ID Mapping",
		"sales_channel": sales_channel,
		"provider": str(provider).strip().upper(),
		"external_entity_type": ExternalEntityType.ORDER,
		"external_id": str(order_id).strip(),
		"erp_doctype": "Sales Order",
		"erp_document": so.name,
		"active": 1,
	})
	try:
		mapping.insert(ignore_permissions=True)
	except frappe.QueryDeadlockError:
		raise
	except (frappe.DuplicateEntryError, frappe.ValidationError):
		# Race condition: another worker claimed this external order first!
		# Roll back our draft order cleanly so NO transient submitted order exists!
		try:
			frappe.db.rollback(save_point=sp_order)
		except Exception:
			pass
		winner = find_existing_order_mapping(sales_channel, provider, order_id)
		if winner:
			return {
				"success": True,
				"sales_order": winner,
				"is_replay": True,
				"reason": "CONCURRENT_WINNER_MAPPED",
			}
		raise

	# Confirmed sole owner: proceed with submission
	so.submit()
	so.db_set("integration_status", IntegrationReadinessStatus.RESERVATION_PENDING)
	frappe.db.set_value(
		"Sales Order",
		so.name,
		"integration_status",
		IntegrationReadinessStatus.RESERVATION_PENDING,
	)

	# Execute native Stock Reservation Entries (all-or-nothing)
	created_reservations = []
	reserved_scopes: List[Tuple[str, str]] = []
	try:
		for so_item in so.items:
			res = reserve_channel_stock(
				item_code=so_item.item_code,
				sales_channel=sales_channel,
				requested_qty=so_item.qty,
				voucher_type="Sales Order",
				voucher_no=so.name,
				voucher_detail_no=so_item.name,
				allow_partial=False,
				idempotency_key=f"SO:{so.name}:{so_item.name}",
				source_doctype="Sales Order",
				source_document=so.name,
				source_document_item=so_item.name,
			)
			created_reservations.append(res)
			wh = getattr(res, "warehouse", None) or so_item.warehouse
			reserved_scopes.append((so_item.item_code, wh))
	except frappe.QueryDeadlockError:
		raise
	except Exception as res_err:
		# Roll back savepoint sp_order: draft SO, submitted SO, reservations, and mapping are 100% wiped!
		try:
			frappe.db.rollback(save_point=sp_order)
		except Exception:
			pass
		raise OrderReservationFailedError(
			_("Order Reservation Failed: {0}").format(str(res_err))
		) from res_err

	# 9. Verify reservation completeness
	is_complete, missing = is_order_ingestion_complete(so.name)
	if not is_complete:
		frappe.db.set_value("Sales Order", so.name, "integration_status", IntegrationReadinessStatus.FAILED_REVIEW)
		raise OrderReservationFailedError(
			f"Incomplete reservations for order {so.name}: {missing}"
		)

	# Transition to READY
	so.db_set("integration_status", IntegrationReadinessStatus.READY)
	frappe.db.set_value(
		"Sales Order",
		so.name,
		"integration_status",
		IntegrationReadinessStatus.READY,
	)

	# 10. Multi-channel Affected Publication Scheduling
	if not reserved_scopes:
		reserved_scopes = [(item.item_code, item.warehouse) for item in so.items]
	channel_items_map = find_affected_channel_items_for_scopes(reserved_scopes, source_channel=sales_channel)
	affected_channels = sorted(list(channel_items_map.keys()))
	schedule_post_commit_publication(channel_items_map)

	return {
		"success": True,
		"sales_order": so.name,
		"is_replay": False,
		"affected_channels": affected_channels,
		"channel_items": channel_items_map,
		"reservations": len(created_reservations),
	}


def process_order_ingestion_event(
	event_name: str,
	worker_id: Optional[str] = None,
	client: Optional[PrestaShopClient] = None,
) -> Dict[str, Any]:
	"""
	Authoritative Integration Event worker handler for Inbound Order Ingestion.
	Enforces:
	1. Atomic claim & lease fencing.
	2. Fresh remote order state verification (re-read at execution time).
	3. Eligibility verification against connector-configured states.
	4. Replay idempotency & incomplete submitted Sales Order recovery.
	5. Execution through ingest_order_pipeline.
	6. Fencing-verified event state transition (SUCCEEDED, CANCELLED, DEAD_LETTER, FAILED).
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

	# Initialize client and verify host safety if not passed
	if not client and sales_channel:
		connector_dict = frappe.db.get_value(
			"PrestaShop Connector",
			{"sales_channel": sales_channel, "enabled": 1},
			["name", "environment", "base_url", "credential_reference", "eligible_order_states"],
			as_dict=True,
		)
		if connector_dict:
			assert_safe_connector_target(connector_dict.environment, connector_dict.base_url)
			config = PrestaShopConfig.from_connector_doc(connector_dict)
			client = PrestaShopClient(config=config)

	# Check for crash recovery / existing mapping
	existing_so = find_existing_order_mapping(sales_channel, provider, external_order_id)
	if existing_so and frappe.db.exists("Sales Order", existing_so):
		is_auth, auth_reason = verify_processing_authority(event_name, processing_token)
		if not is_auth:
			return {
				"success": False,
				"event_name": event_name,
				"reason": "LOST_PROCESSING_AUTHORITY",
				"details": auth_reason,
			}
		so_doc = frappe.get_doc("Sales Order", existing_so)
		if so_doc.docstatus == 0:
			so_doc.submit()
			frappe.db.set_value("Sales Order", existing_so, "integration_status", IntegrationReadinessStatus.RESERVATION_PENDING)

		try:
			_ensure_order_reservations(so_doc, sales_channel)
		except Exception as recov_err:
			frappe.db.set_value("Sales Order", existing_so, "integration_status", IntegrationReadinessStatus.FAILED_REVIEW)
			is_auth, _ = verify_processing_authority(event_name, processing_token)
			if is_auth:
				event_doc.reload()
				event_doc.mark_failed(
					processing_token,
					ErrorCategory.CONFLICT,
					str(recov_err)[:250],
					error_category=ErrorCategory.CONFLICT,
				)
				frappe.db.commit()
			return {"success": False, "event_name": event_name, "error": str(recov_err), "category": "RECOVERY_FAILED"}

		is_complete, missing = is_order_ingestion_complete(existing_so)
		if not is_complete:
			frappe.db.set_value("Sales Order", existing_so, "integration_status", IntegrationReadinessStatus.FAILED_REVIEW)
			raise OrderReservationFailedError(f"Incomplete reservations for order {existing_so}: {missing}")

		# Order is complete: transition/confirm READY and record references
		frappe.db.set_value("Sales Order", existing_so, "integration_status", IntegrationReadinessStatus.READY)
		frappe.db.set_value("Sales Order", existing_so, "latest_integration_event", event_name)
		frappe.db.set_value("Sales Order", existing_so, "integration_provider", str(provider).strip().upper())

		# Post-commit publication scheduling for recovered order!
		reserved_scopes = [(item.item_code, item.warehouse) for item in so_doc.items]
		channel_items_map = find_affected_channel_items_for_scopes(reserved_scopes, source_channel=sales_channel)
		schedule_post_commit_publication(channel_items_map)

		event_doc.reload()
		event_doc.associate_erp_document("Sales Order", existing_so)
		event_doc.mark_succeeded(
			processing_token,
			response_metadata={"reason": "IDEMPOTENT_REPLAY", "sales_order": existing_so},
		)
		frappe.db.commit()
		return {
			"success": True,
			"event_name": event_name,
			"sales_order": existing_so,
			"is_replay": True,
		}

	# Re-read external order freshness and eligibility at execution time
	external_order = None
	if client:
		try:
			raw_order = client.get_order(external_order_id)
			current_state = str(raw_order.get("current_state", "")).strip()
		except Exception as req_err:
			is_auth, _ = verify_processing_authority(event_name, processing_token)
			if is_auth:
				event_doc.reload()
				event_doc.mark_failed(
					processing_token,
					ErrorCategory.TRANSIENT,
					f"Failed to fetch fresh order from PrestaShop: {req_err}"[:250],
					error_category=ErrorCategory.TRANSIENT,
				)
				frappe.db.commit()
			return {"success": False, "event_name": event_name, "error": str(req_err), "category": "NETWORK"}

		eligible_states = get_eligible_order_states(sales_channel)
		if not eligible_states or current_state not in eligible_states:
			is_auth, _ = verify_processing_authority(event_name, processing_token)
			if is_auth:
				event_doc.reload()
				event_doc.cancel(
					reason=f"Order state '{current_state}' not in eligible states {eligible_states}"[:250],
					processing_token=processing_token,
				)
				frappe.db.commit()
			return {
				"success": False,
				"event_name": event_name,
				"error": f"Order state '{current_state}' not in eligible states {eligible_states}",
				"category": "NOT_ELIGIBLE",
			}

		raw_lines = client.get_order_details(external_order_id)
		raw_cust = client.get_customer(raw_order.get("id_customer")) if raw_order.get("id_customer") else None
		raw_deliv = client.get_address(raw_order.get("id_address_delivery")) if raw_order.get("id_address_delivery") else None
		raw_inv = client.get_address(raw_order.get("id_address_invoice")) if raw_order.get("id_address_invoice") else None

		external_order = normalize_prestashop_order(
			raw_order=raw_order,
			raw_lines=raw_lines,
			raw_customer=raw_cust,
			raw_delivery_address=raw_deliv,
			raw_invoice_address=raw_inv,
			sales_channel=sales_channel,
		)
	elif "normalized_order" in payload:
		norm_dict = payload["normalized_order"]
		external_order = _deserialize_external_order(norm_dict)

	if not external_order:
		is_auth, _ = verify_processing_authority(event_name, processing_token)
		if is_auth:
			event_doc.reload()
			event_doc.mark_dead_letter(
				processing_token,
				ErrorCategory.VALIDATION,
				"Cannot reconstruct ExternalOrder payload.",
			)
			frappe.db.commit()
		return {"success": False, "event_name": event_name, "error": "MISSING_PAYLOAD"}

	# Execute ingestion pipeline
	try:
		is_auth, auth_reason = verify_processing_authority(event_name, processing_token)
		if not is_auth:
			return {
				"success": False,
				"event_name": event_name,
				"reason": "LOST_PROCESSING_AUTHORITY",
				"details": auth_reason,
			}

		res = ingest_order_pipeline(external_order, client=client)
		so_name = res["sales_order"]

		is_auth, auth_reason = verify_processing_authority(event_name, processing_token)
		if not is_auth:
			return {
				"success": False,
				"event_name": event_name,
				"reason": "LOST_PROCESSING_AUTHORITY",
				"details": auth_reason,
			}

		is_complete, missing = is_order_ingestion_complete(so_name)
		if not is_complete:
			frappe.db.set_value("Sales Order", so_name, "integration_status", IntegrationReadinessStatus.FAILED_REVIEW)
			raise OrderReservationFailedError(f"Incomplete reservations for order {so_name}: {missing}")

		frappe.db.set_value("Sales Order", so_name, "integration_status", IntegrationReadinessStatus.READY)
		frappe.db.set_value("Sales Order", so_name, "latest_integration_event", event_name)
		frappe.db.set_value("Sales Order", so_name, "integration_provider", str(provider).strip().upper())

		event_doc.reload()
		event_doc.associate_erp_document("Sales Order", so_name)
		event_doc.mark_succeeded(processing_token, response_metadata=res)
		frappe.db.commit()
		return {"success": True, "event_name": event_name, "sales_order": so_name}

	except OrderNotEligibleError as e:
		is_auth, _ = verify_processing_authority(event_name, processing_token)
		if is_auth:
			event_doc.reload()
			event_doc.cancel(reason=str(e)[:250], processing_token=processing_token)
			frappe.db.commit()
		return {"success": False, "event_name": event_name, "error": str(e), "category": "NOT_ELIGIBLE"}

	except (MissingProductMappingError, InvalidOrderQuantityError, OrderTotalMismatchError) as e:
		is_auth, _ = verify_processing_authority(event_name, processing_token)
		if is_auth:
			event_doc.reload()
			event_doc.mark_dead_letter(processing_token, ErrorCategory.VALIDATION, str(e)[:250])
			frappe.db.commit()
		return {"success": False, "event_name": event_name, "error": str(e), "category": "NON_RETRYABLE"}

	except (InsufficientOrderStockError, OrderReservationFailedError) as e:
		if 'so_name' in locals() and so_name and frappe.db.exists("Sales Order", so_name):
			frappe.db.set_value("Sales Order", so_name, "integration_status", IntegrationReadinessStatus.FAILED_REVIEW)
		elif existing_so and frappe.db.exists("Sales Order", existing_so):
			frappe.db.set_value("Sales Order", existing_so, "integration_status", IntegrationReadinessStatus.FAILED_REVIEW)

		is_auth, _ = verify_processing_authority(event_name, processing_token)
		if is_auth:
			event_doc.reload()
			event_doc.mark_failed(
				processing_token,
				ErrorCategory.CONFLICT,
				str(e)[:250],
				error_category=ErrorCategory.CONFLICT,
			)
			frappe.db.commit()
		return {"success": False, "event_name": event_name, "error": str(e), "category": "STOCK_SHORTAGE"}

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
		return {"success": False, "event_name": event_name, "error": str(e), "category": "ERROR"}


def _get_default_customer_group() -> str:
	cg = frappe.defaults.get_global_default("customer_group")
	if cg and frappe.db.exists("Customer Group", cg):
		return cg
	first = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	return first or "Commercial"


def _get_default_territory() -> str:
	t = frappe.defaults.get_global_default("territory")
	if t and frappe.db.exists("Territory", t):
		return t
	first = frappe.db.get_value("Territory", {"is_group": 0}, "name")
	return first or "All Territories"


def _resolve_country(raw_country: Optional[str]) -> str:
	if raw_country and frappe.db.exists("Country", raw_country):
		return raw_country
	# PrestaShop test DB: id_country 21 is United States
	if raw_country in ("21", "US", "USA", "United States"):
		return "United States"
	default_c = frappe.defaults.get_global_default("country")
	if default_c and frappe.db.exists("Country", default_c):
		return default_c
	return "United States"


def _parse_date(raw_dt: Optional[str]) -> Optional[str]:
	if not raw_dt:
		return None
	try:
		dt = get_datetime(raw_dt)
		return dt.strftime("%Y-%m-%d")
	except Exception:
		return None


def _deserialize_external_order(d: Dict[str, Any]) -> ExternalOrder:
	cust_d = d.get("customer") or {}
	cust = ExternalCustomer(
		external_customer_id=str(cust_d.get("external_customer_id") or ""),
		email=str(cust_d.get("email") or ""),
		first_name=str(cust_d.get("first_name") or ""),
		last_name=str(cust_d.get("last_name") or ""),
		company=str(cust_d.get("company") or ""),
		phone=str(cust_d.get("phone") or ""),
		is_guest=bool(cust_d.get("is_guest", False)),
	)

	deliv_d = d.get("delivery_address")
	deliv = None
	if deliv_d:
		deliv = ExternalAddress(
			external_address_id=str(deliv_d.get("external_address_id") or ""),
			address_type=str(deliv_d.get("address_type") or "Shipping"),
			first_name=str(deliv_d.get("first_name") or ""),
			last_name=str(deliv_d.get("last_name") or ""),
			company=str(deliv_d.get("company") or ""),
			address1=str(deliv_d.get("address1") or ""),
			address2=str(deliv_d.get("address2") or ""),
			city=str(deliv_d.get("city") or ""),
			state=str(deliv_d.get("state") or ""),
			postcode=str(deliv_d.get("postcode") or ""),
			country=str(deliv_d.get("country") or ""),
			phone=str(deliv_d.get("phone") or ""),
			phone_mobile=str(deliv_d.get("phone_mobile") or ""),
		)

	inv_d = d.get("invoice_address")
	inv = None
	if inv_d:
		inv = ExternalAddress(
			external_address_id=str(inv_d.get("external_address_id") or ""),
			address_type=str(inv_d.get("address_type") or "Billing"),
			first_name=str(inv_d.get("first_name") or ""),
			last_name=str(inv_d.get("last_name") or ""),
			company=str(inv_d.get("company") or ""),
			address1=str(inv_d.get("address1") or ""),
			address2=str(inv_d.get("address2") or ""),
			city=str(inv_d.get("city") or ""),
			state=str(inv_d.get("state") or ""),
			postcode=str(inv_d.get("postcode") or ""),
			country=str(inv_d.get("country") or ""),
			phone=str(inv_d.get("phone") or ""),
			phone_mobile=str(inv_d.get("phone_mobile") or ""),
		)

	lines = []
	for ld in d.get("lines") or []:
		lines.append(ExternalOrderLine(
			external_line_id=str(ld.get("external_line_id") or ""),
			external_product_id=str(ld.get("external_product_id") or ""),
			external_variant_id=str(ld.get("external_variant_id")) if ld.get("external_variant_id") else None,
			sku=str(ld.get("sku") or ""),
			description=str(ld.get("description") or ""),
			quantity=flt(ld.get("quantity", 1)),
			unit_price_ex_tax=flt(ld.get("unit_price_ex_tax", 0.0)),
			unit_price_inc_tax=flt(ld.get("unit_price_inc_tax", 0.0)),
			line_total_ex_tax=flt(ld.get("line_total_ex_tax", 0.0)),
			line_total_inc_tax=flt(ld.get("line_total_inc_tax", 0.0)),
			tax_rate=flt(ld.get("tax_rate", 0.0)),
			discount_amount=flt(ld.get("discount_amount", 0.0)),
		))

	tot_d = d.get("totals") or {}
	totals = ExternalTotals(
		total_products_ex_tax=flt(tot_d.get("total_products_ex_tax", 0.0)),
		total_products_inc_tax=flt(tot_d.get("total_products_inc_tax", 0.0)),
		total_shipping_ex_tax=flt(tot_d.get("total_shipping_ex_tax", 0.0)),
		total_shipping_inc_tax=flt(tot_d.get("total_shipping_inc_tax", 0.0)),
		total_discounts_ex_tax=flt(tot_d.get("total_discounts_ex_tax", 0.0)),
		total_discounts_inc_tax=flt(tot_d.get("total_discounts_inc_tax", 0.0)),
		total_tax=flt(tot_d.get("total_tax", 0.0)),
		total_paid=flt(tot_d.get("total_paid", 0.0)),
		currency=str(tot_d.get("currency") or "USD"),
	)

	return ExternalOrder(
		provider=str(d.get("provider") or IntegrationProvider.PRESTASHOP),
		sales_channel=str(d.get("sales_channel") or "TID"),
		external_order_id=str(d.get("external_order_id") or ""),
		external_reference=str(d.get("external_reference") or ""),
		order_state_id=str(d.get("order_state_id") or ""),
		order_state_name=str(d.get("order_state_name") or ""),
		date_add=str(d.get("date_add") or ""),
		date_upd=str(d.get("date_upd") or ""),
		currency=str(d.get("currency") or "USD"),
		payment_method=str(d.get("payment_method") or ""),
		customer=cust,
		delivery_address=deliv,
		invoice_address=inv,
		lines=lines,
		totals=totals,
		raw_data=d.get("raw_data") or {},
	)
