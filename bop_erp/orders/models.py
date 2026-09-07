# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExternalOrderLine:
	external_line_id: str
	external_product_id: str
	external_variant_id: Optional[str] = None
	sku: str = ""
	description: str = ""
	quantity: float = 1.0
	unit_price_ex_tax: float = 0.0
	unit_price_inc_tax: float = 0.0
	line_total_ex_tax: float = 0.0
	line_total_inc_tax: float = 0.0
	tax_rate: float = 0.0
	discount_amount: float = 0.0


@dataclass
class ExternalCustomer:
	external_customer_id: str
	email: str = ""
	first_name: str = ""
	last_name: str = ""
	company: str = ""
	phone: str = ""
	is_guest: bool = False


@dataclass
class ExternalAddress:
	external_address_id: str
	address_type: str = "Shipping"  # "Shipping" or "Billing"
	first_name: str = ""
	last_name: str = ""
	company: str = ""
	address1: str = ""
	address2: str = ""
	city: str = ""
	state: str = ""
	postcode: str = ""
	country: str = ""
	phone: str = ""
	phone_mobile: str = ""


@dataclass
class ExternalTotals:
	total_products_ex_tax: float = 0.0
	total_products_inc_tax: float = 0.0
	total_shipping_ex_tax: float = 0.0
	total_shipping_inc_tax: float = 0.0
	total_discounts_ex_tax: float = 0.0
	total_discounts_inc_tax: float = 0.0
	total_tax: float = 0.0
	total_paid: float = 0.0
	currency: str = "USD"


@dataclass
class ExternalOrder:
	provider: str
	sales_channel: str
	external_order_id: str
	external_reference: str
	order_state_id: str
	order_state_name: str = ""
	date_add: str = ""
	date_upd: str = ""
	currency: str = "USD"
	payment_method: str = ""
	customer: ExternalCustomer = field(default_factory=lambda: ExternalCustomer(external_customer_id=""))
	delivery_address: Optional[ExternalAddress] = None
	invoice_address: Optional[ExternalAddress] = None
	lines: List[ExternalOrderLine] = field(default_factory=list)
	totals: ExternalTotals = field(default_factory=ExternalTotals)
	raw_data: Dict[str, Any] = field(default_factory=dict)
