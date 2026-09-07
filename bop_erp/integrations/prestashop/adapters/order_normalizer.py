# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional
from frappe.utils import flt

from bop_erp.constants import IntegrationProvider
from bop_erp.orders.models import (
	ExternalOrder,
	ExternalOrderLine,
	ExternalCustomer,
	ExternalAddress,
	ExternalTotals,
)


def normalize_prestashop_customer(raw_customer: Optional[Dict[str, Any]], fallback_id: str = "") -> ExternalCustomer:
	if not raw_customer:
		return ExternalCustomer(external_customer_id=str(fallback_id or ""))

	cid = str(raw_customer.get("id") or fallback_id or "")
	is_guest = False
	if "is_guest" in raw_customer:
		try:
			is_guest = bool(int(raw_customer.get("is_guest") or 0))
		except (ValueError, TypeError):
			is_guest = False

	return ExternalCustomer(
		external_customer_id=cid,
		email=str(raw_customer.get("email") or "").strip(),
		first_name=str(raw_customer.get("firstname") or "").strip(),
		last_name=str(raw_customer.get("lastname") or "").strip(),
		company=str(raw_customer.get("company") or "").strip(),
		phone=str(raw_customer.get("phone") or raw_customer.get("phone_mobile") or "").strip(),
		is_guest=is_guest,
	)


def normalize_prestashop_address(
	raw_address: Optional[Dict[str, Any]],
	address_type: str = "Shipping",
	fallback_id: str = "",
) -> Optional[ExternalAddress]:
	if not raw_address:
		return None

	aid = str(raw_address.get("id") or fallback_id or "")
	if not aid:
		return None

	return ExternalAddress(
		external_address_id=aid,
		address_type=address_type,
		first_name=str(raw_address.get("firstname") or "").strip(),
		last_name=str(raw_address.get("lastname") or "").strip(),
		company=str(raw_address.get("company") or "").strip(),
		address1=str(raw_address.get("address1") or "").strip(),
		address2=str(raw_address.get("address2") or "").strip(),
		city=str(raw_address.get("city") or "").strip(),
		state=str(raw_address.get("id_state") or raw_address.get("state") or "").strip(),
		postcode=str(raw_address.get("postcode") or "").strip(),
		country=str(raw_address.get("id_country") or raw_address.get("country") or "").strip(),
		phone=str(raw_address.get("phone") or "").strip(),
		phone_mobile=str(raw_address.get("phone_mobile") or "").strip(),
	)


def normalize_prestashop_order(
	raw_order: Dict[str, Any],
	raw_lines: List[Dict[str, Any]],
	raw_customer: Optional[Dict[str, Any]] = None,
	raw_delivery_address: Optional[Dict[str, Any]] = None,
	raw_invoice_address: Optional[Dict[str, Any]] = None,
	sales_channel: str = "TID",
	currency_iso: str = "USD",
	state_name: str = "",
) -> ExternalOrder:
	"""
	Transforms PrestaShop Webservice order payloads into a normalized ExternalOrder instance.
	Decouples PrestaShop data representations from ERP business models.
	"""
	order_id = str(raw_order.get("id") or "").strip()
	ref = str(raw_order.get("reference") or f"PS-{order_id}").strip()
	state_id = str(raw_order.get("current_state") or "").strip()

	customer = normalize_prestashop_customer(
		raw_customer,
		fallback_id=str(raw_order.get("id_customer") or ""),
	)

	delivery_addr = normalize_prestashop_address(
		raw_delivery_address,
		address_type="Shipping",
		fallback_id=str(raw_order.get("id_address_delivery") or ""),
	)

	invoice_addr = normalize_prestashop_address(
		raw_invoice_address,
		address_type="Billing",
		fallback_id=str(raw_order.get("id_address_invoice") or ""),
	)

	# If delivery and invoice addresses are the same PrestaShop ID and only delivery was fetched:
	if not invoice_addr and delivery_addr and str(raw_order.get("id_address_delivery")) == str(raw_order.get("id_address_invoice")):
		invoice_addr = normalize_prestashop_address(
			raw_delivery_address,
			address_type="Billing",
			fallback_id=str(raw_order.get("id_address_invoice") or ""),
		)

	lines: List[ExternalOrderLine] = []
	for l in raw_lines:
		line_id = str(l.get("id") or "").strip()
		prod_id = str(l.get("product_id") or "").strip()
		attr_id_raw = l.get("product_attribute_id")
		try:
			attr_id_int = int(attr_id_raw or 0)
		except (ValueError, TypeError):
			attr_id_int = 0
		var_id = str(attr_id_int) if attr_id_int > 0 else None

		qty = flt(l.get("product_quantity", 1))
		unit_price_ex = flt(l.get("unit_price_tax_excl", 0.0))
		unit_price_inc = flt(l.get("unit_price_tax_incl", 0.0))

		line_tot_ex = flt(l.get("total_price_tax_excl", 0.0))
		if line_tot_ex <= 0.0 and unit_price_ex > 0.0:
			line_tot_ex = flt(unit_price_ex * qty)

		line_tot_inc = flt(l.get("total_price_tax_incl", 0.0))
		if line_tot_inc <= 0.0 and unit_price_inc > 0.0:
			line_tot_inc = flt(unit_price_inc * qty)

		lines.append(
			ExternalOrderLine(
				external_line_id=line_id,
				external_product_id=prod_id,
				external_variant_id=var_id,
				sku=str(l.get("product_reference") or "").strip(),
				description=str(l.get("product_name") or "").strip(),
				quantity=qty,
				unit_price_ex_tax=unit_price_ex,
				unit_price_inc_tax=unit_price_inc,
				line_total_ex_tax=line_tot_ex,
				line_total_inc_tax=line_tot_inc,
				tax_rate=flt(l.get("tax_rate", 0.0)),
				discount_amount=flt(l.get("reduction_amount", 0.0)),
			)
		)

	tot_prod_ex = flt(raw_order.get("total_products", 0.0))
	tot_prod_inc = flt(raw_order.get("total_products_wt", 0.0))
	tot_ship_ex = flt(raw_order.get("total_shipping_tax_excl", 0.0))
	tot_ship_inc = flt(raw_order.get("total_shipping_tax_incl", 0.0)) or flt(raw_order.get("total_shipping", 0.0))
	tot_disc_ex = flt(raw_order.get("total_discounts_tax_excl", 0.0))
	tot_disc_inc = flt(raw_order.get("total_discounts_tax_incl", 0.0)) or flt(raw_order.get("total_discounts", 0.0))
	tot_paid = flt(raw_order.get("total_paid", 0.0))

	# Compute tax
	tot_tax = max(0.0, flt((tot_prod_inc + tot_ship_inc) - (tot_prod_ex + tot_ship_ex)))

	totals = ExternalTotals(
		total_products_ex_tax=tot_prod_ex,
		total_products_inc_tax=tot_prod_inc,
		total_shipping_ex_tax=tot_ship_ex,
		total_shipping_inc_tax=tot_ship_inc,
		total_discounts_ex_tax=tot_disc_ex,
		total_discounts_inc_tax=tot_disc_inc,
		total_tax=tot_tax,
		total_paid=tot_paid,
		currency=currency_iso,
	)

	return ExternalOrder(
		provider=IntegrationProvider.PRESTASHOP,
		sales_channel=sales_channel,
		external_order_id=order_id,
		external_reference=ref,
		order_state_id=state_id,
		order_state_name=state_name,
		date_add=str(raw_order.get("date_add") or ""),
		date_upd=str(raw_order.get("date_upd") or ""),
		currency=currency_iso,
		payment_method=str(raw_order.get("payment") or raw_order.get("module") or "").strip(),
		customer=customer,
		delivery_address=delivery_addr,
		invoice_address=invoice_addr,
		lines=lines,
		totals=totals,
		raw_data=raw_order,
	)
