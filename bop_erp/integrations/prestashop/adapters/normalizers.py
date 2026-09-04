# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional
from bop_erp.integrations.prestashop.schemas.models import (
	PrestaShopCategory,
	PrestaShopProduct,
	PrestaShopCombination,
	PrestaShopStock,
	PrestaShopCustomer,
	PrestaShopAddress,
	PrestaShopOrderLine,
	PrestaShopOrder,
)


def extract_lang_field(val: Any) -> str:
	"""
	Extracts a plain string from PrestaShop localized field structures.
	PrestaShop can return:
	- "Direct String"
	- [{"id": "1", "value": "Direct String"}]
	- {"language": [{"id": "1", "value": "..."}]}
	"""
	if val is None:
		return ""
	if isinstance(val, str):
		return val.strip()
	if isinstance(val, list):
		for entry in val:
			if isinstance(entry, dict) and "value" in entry:
				txt = str(entry["value"]).strip()
				if txt:
					return txt
		if val and isinstance(val[0], dict) and "value" in val[0]:
			return str(val[0]["value"]).strip()
		return str(val[0]).strip() if val else ""
	if isinstance(val, dict):
		if "value" in val:
			return str(val["value"]).strip()
		if "language" in val:
			return extract_lang_field(val["language"])
	return str(val).strip()


def normalize_category(raw: Dict[str, Any]) -> PrestaShopCategory:
	"""Normalizes PrestaShop category payload into PrestaShopCategory."""
	ext_id = str(raw.get("id", "")).strip()
	name = extract_lang_field(raw.get("name"))
	desc = extract_lang_field(raw.get("description"))
	parent = str(raw.get("id_parent", "")).strip() or None
	active = str(raw.get("active", "1")).strip() in ("1", "true", "True")

	return PrestaShopCategory(
		external_id=ext_id,
		name=name,
		parent_id=parent,
		active=active,
		description=desc,
		raw_data=raw,
	)


def normalize_product(raw: Dict[str, Any]) -> PrestaShopProduct:
	"""Normalizes PrestaShop product payload into PrestaShopProduct."""
	ext_id = str(raw.get("id", "")).strip()
	sku = str(raw.get("reference", "")).strip()
	name = extract_lang_field(raw.get("name"))
	price = float(raw.get("price", 0.0) or 0.0)
	wholesale_price = float(raw.get("wholesale_price", 0.0) or 0.0)
	cat_id = str(raw.get("id_category_default", "")).strip() or None
	active = str(raw.get("active", "1")).strip() in ("1", "true", "True")

	# Combinations association
	comb_ids = []
	assocs = raw.get("associations", {})
	if isinstance(assocs, dict) and "combinations" in assocs:
		combs = assocs["combinations"]
		if isinstance(combs, list):
			for c in combs:
				if isinstance(c, dict) and "id" in c:
					comb_ids.append(str(c["id"]).strip())

	return PrestaShopProduct(
		external_id=ext_id,
		sku=sku,
		name=name,
		price=price,
		wholesale_price=wholesale_price,
		category_id=cat_id,
		active=active,
		combination_ids=comb_ids,
		raw_data=raw,
	)


def normalize_combination(raw: Dict[str, Any], parent_product_id: Optional[str] = None) -> PrestaShopCombination:
	"""Normalizes PrestaShop combination payload into PrestaShopCombination."""
	ext_id = str(raw.get("id", "")).strip()
	prod_id = parent_product_id or str(raw.get("id_product", "")).strip()
	sku = str(raw.get("reference", "")).strip()
	price_offset = float(raw.get("price", 0.0) or 0.0)
	qty = int(raw.get("quantity", 0) or 0)

	attr_ids = []
	assocs = raw.get("associations", {})
	if isinstance(assocs, dict) and "product_option_values" in assocs:
		pov = assocs["product_option_values"]
		if isinstance(pov, list):
			for item in pov:
				if isinstance(item, dict) and "id" in item:
					attr_ids.append(str(item["id"]).strip())

	return PrestaShopCombination(
		external_id=ext_id,
		parent_product_id=prod_id,
		sku=sku,
		price_offset=price_offset,
		quantity=qty,
		attribute_ids=attr_ids,
		raw_data=raw,
	)


def normalize_stock(raw: Dict[str, Any]) -> PrestaShopStock:
	"""Normalizes PrestaShop stock_available payload into PrestaShopStock."""
	ext_id = str(raw.get("id", "")).strip()
	prod_id = str(raw.get("id_product", "")).strip()
	comb_id = str(raw.get("id_product_attribute", "")).strip()
	if comb_id == "0":
		comb_id = None
	qty = int(raw.get("quantity", 0) or 0)

	return PrestaShopStock(
		external_id=ext_id,
		product_id=prod_id,
		combination_id=comb_id,
		quantity=qty,
		raw_data=raw,
	)


def normalize_customer(raw: Dict[str, Any]) -> PrestaShopCustomer:
	"""Normalizes PrestaShop customer payload into PrestaShopCustomer."""
	ext_id = str(raw.get("id", "")).strip()
	firstname = str(raw.get("firstname", "")).strip()
	lastname = str(raw.get("lastname", "")).strip()
	email = str(raw.get("email", "")).strip()
	company = str(raw.get("company", "")).strip()
	active = str(raw.get("active", "1")).strip() in ("1", "true", "True")

	return PrestaShopCustomer(
		external_id=ext_id,
		firstname=firstname,
		lastname=lastname,
		email=email,
		company=company,
		active=active,
		raw_data=raw,
	)


def normalize_address(raw: Dict[str, Any]) -> PrestaShopAddress:
	"""Normalizes PrestaShop address payload into PrestaShopAddress."""
	ext_id = str(raw.get("id", "")).strip()
	cust_id = str(raw.get("id_customer", "")).strip()
	addr1 = str(raw.get("address1", "")).strip()
	city = str(raw.get("city", "")).strip()
	state_id = str(raw.get("id_state", "")).strip() or None
	postcode = str(raw.get("postcode", "")).strip()
	country_id = str(raw.get("id_country", "")).strip() or None
	company = str(raw.get("company", "")).strip()

	return PrestaShopAddress(
		external_id=ext_id,
		customer_id=cust_id,
		address1=addr1,
		city=city,
		state_id=state_id,
		postcode=postcode,
		country_id=country_id,
		company=company,
		raw_data=raw,
	)


def normalize_order_line(raw: Dict[str, Any]) -> PrestaShopOrderLine:
	"""Normalizes PrestaShop order_detail payload into PrestaShopOrderLine."""
	ext_id = str(raw.get("id", "")).strip()
	prod_id = str(raw.get("product_id", "")).strip()
	comb_id = str(raw.get("product_attribute_id", "")).strip()
	if comb_id == "0":
		comb_id = None
	sku = str(raw.get("product_reference", "")).strip()
	name = str(raw.get("product_name", "")).strip()
	qty = int(raw.get("product_quantity", 1) or 1)
	unit_price = float(raw.get("unit_price_tax_incl", raw.get("product_price", 0.0)) or 0.0)
	total_price = float(raw.get("total_price_tax_incl", 0.0) or 0.0)

	return PrestaShopOrderLine(
		external_id=ext_id,
		product_id=prod_id,
		combination_id=comb_id,
		sku=sku,
		name=name,
		quantity=qty,
		unit_price=unit_price,
		total_price=total_price,
		raw_data=raw,
	)


def normalize_order(raw: Dict[str, Any], lines: Optional[List[PrestaShopOrderLine]] = None) -> PrestaShopOrder:
	"""Normalizes PrestaShop order payload into PrestaShopOrder."""
	ext_id = str(raw.get("id", "")).strip()
	ref = str(raw.get("reference", "")).strip()
	cust_id = str(raw.get("id_customer", "")).strip()
	addr_del = str(raw.get("id_address_delivery", "")).strip()
	addr_inv = str(raw.get("id_address_invoice", "")).strip()
	state_id = str(raw.get("current_state", "")).strip()
	total_paid = float(raw.get("total_paid", 0.0) or 0.0)
	total_prod = float(raw.get("total_products", 0.0) or 0.0)

	return PrestaShopOrder(
		external_id=ext_id,
		reference=ref,
		customer_id=cust_id,
		address_delivery_id=addr_del,
		address_invoice_id=addr_inv,
		order_state_id=state_id,
		total_paid=total_paid,
		total_products=total_prod,
		lines=lines or [],
		raw_data=raw,
	)
