# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


@dataclass
class PrestaShopCategory:
	external_id: str
	name: str
	parent_id: Optional[str] = None
	active: bool = True
	description: str = ""
	raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)


@dataclass
class PrestaShopStock:
	external_id: str
	product_id: str
	combination_id: Optional[str] = None
	quantity: int = 0
	raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)


@dataclass
class PrestaShopCombination:
	external_id: str
	parent_product_id: str
	sku: str
	price_offset: float = 0.0
	quantity: int = 0
	attribute_ids: List[str] = field(default_factory=list)
	raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)


@dataclass
class PrestaShopProduct:
	external_id: str
	sku: str
	name: str
	price: float = 0.0
	wholesale_price: float = 0.0
	category_id: Optional[str] = None
	active: bool = True
	combination_ids: List[str] = field(default_factory=list)
	raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)


@dataclass
class PrestaShopCustomer:
	external_id: str
	firstname: str
	lastname: str
	email: str
	company: str = ""
	active: bool = True
	raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)


@dataclass
class PrestaShopAddress:
	external_id: str
	customer_id: str
	address1: str
	city: str
	state_id: Optional[str] = None
	postcode: str = ""
	country_id: Optional[str] = None
	company: str = ""
	raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)


@dataclass
class PrestaShopOrderLine:
	external_id: str
	product_id: str
	combination_id: Optional[str] = None
	sku: str = ""
	name: str = ""
	quantity: int = 1
	unit_price: float = 0.0
	total_price: float = 0.0
	raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)


@dataclass
class PrestaShopOrder:
	external_id: str
	reference: str
	customer_id: str
	address_delivery_id: str
	address_invoice_id: str
	order_state_id: str
	total_paid: float = 0.0
	total_products: float = 0.0
	lines: List[PrestaShopOrderLine] = field(default_factory=list)
	raw_data: Optional[Dict[str, Any]] = field(default=None, repr=False)
