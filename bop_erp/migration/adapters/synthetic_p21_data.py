# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from decimal import Decimal
from typing import Any, Dict, List


def get_synthetic_p21_dataset(
	company_id: str = "COMPANY_1",
	alt_company_id: str = "COMPANY_2",
) -> Dict[str, List[Dict[str, Any]]]:
	"""
	Generates a rich synthetic dataset mimicking Prophet 21 source tables.
	Exercises:
	- standard records
	- multiple source companies (for boundary discrimination)
	- NULL, empty string, zero, and False preservation
	- Decimal preservation
	"""
	customers = [
		{
			"customer_id": "P21-CUST-001",
			"customer_name": "Acme Fasteners Corp",
			"email_address": "orders@acmefasteners.com",
			"phone_number": "+1-555-0100",
			"tax_id_number": "TAX-11111",
			"currency_code": "USD",
			"terms_code": "NET30",
			"company_id": company_id,
		},
		{
			"customer_id": "P21-CUST-002",
			"customer_name": "Apex Tool & Supply",
			"email_address": "ap@apextool.com",
			"phone_number": "",  # Empty string preservation
			"tax_id_number": None,  # NULL preservation
			"currency_code": "USD",
			"terms_code": "NET60",
			"company_id": company_id,
		},
		{
			"customer_id": "P21-CUST-003",
			"customer_name": "Precision Machine Works",
			"email_address": "contact@precisionmachine.com",
			"phone_number": "+1-555-0102",
			"tax_id_number": "TAX-33333",
			"currency_code": "USD",
			"terms_code": "DUE_ON_RECEIPT",
			"company_id": company_id,
		},
		{
			"customer_id": "P21-CUST-ALT-999",
			"customer_name": "Other Company Customer",
			"email_address": "other@altcompany.com",
			"phone_number": "+1-555-9999",
			"tax_id_number": "TAX-99999",
			"currency_code": "USD",
			"terms_code": "NET30",
			"company_id": alt_company_id,  # Excluded by company_id boundary
		},
	]

	vendors = [
		{
			"vendor_id": "P21-VEND-001",
			"vendor_name": "Global Steel Industries",
			"email_address": "sales@globalsteel.com",
			"phone_number": "+1-555-0200",
			"tax_id_number": "VTAX-1001",
			"currency_code": "USD",
			"terms_code": "NET45",
			"company_id": company_id,
		},
		{
			"vendor_id": "P21-VEND-002",
			"vendor_name": "National Bolt & Screw",
			"email_address": "invoicing@nationalbolt.com",
			"phone_number": "+1-555-0201",
			"tax_id_number": "VTAX-1002",
			"currency_code": "USD",
			"terms_code": "NET30",
			"company_id": company_id,
		},
		{
			"vendor_id": "P21-VEND-ALT-888",
			"vendor_name": "Excluded Alternate Vendor",
			"email_address": "alt@altcorp.com",
			"phone_number": "+1-555-8888",
			"tax_id_number": "VTAX-8888",
			"currency_code": "USD",
			"terms_code": "NET30",
			"company_id": alt_company_id,
		},
	]

	items = [
		{
			"item_id": "P21-ITEM-001",
			"item_code": "BOLT-SS-M8-30",
			"item_description": "Hex Bolt Stainless Steel M8x30",
			"extended_description": "High tensile 316 stainless steel hex head bolt.",
			"unit_of_measure": "Nos",
			"product_group": "Fasteners",
			"stockable_flag": 1,
			"serialized_flag": 0,  # Zero preservation
			"lot_tracked_flag": False,  # False preservation
			"company_id": company_id,
		},
		{
			"item_id": "P21-ITEM-002",
			"item_code": "NUT-NYLOC-M8",
			"item_description": "Nyloc Nut M8 Grade 8",
			"extended_description": "Self-locking nylon insert hex nut.",
			"unit_of_measure": "Nos",
			"product_group": "Fasteners",
			"stockable_flag": 1,
			"serialized_flag": 0,
			"lot_tracked_flag": False,
			"company_id": company_id,
		},
		{
			"item_id": "P21-ITEM-003",
			"item_code": "WASHER-FLAT-M8",
			"item_description": "Flat Washer M8 DIN 125",
			"extended_description": "Standard form A zinc-plated flat washer.",
			"unit_of_measure": "Nos",
			"product_group": "Washers",
			"stockable_flag": 0,  # Zero stockable flag
			"serialized_flag": False,
			"lot_tracked_flag": False,
			"company_id": company_id,
		},
		{
			"item_id": "P21-ITEM-ALT-777",
			"item_code": "ALT-EXCLUSIVE-ITEM",
			"item_description": "Item of Alt Company",
			"extended_description": "Alt company only.",
			"unit_of_measure": "Nos",
			"product_group": "Alt Group",
			"stockable_flag": 1,
			"serialized_flag": 0,
			"lot_tracked_flag": False,
			"company_id": alt_company_id,
		},
	]

	warehouses = [
		{
			"location_id": "P21-LOC-001",
			"location_code": "MAIN-WH",
			"location_name": "Main Distribution Center",
			"parent_location_code": None,
			"company_id": company_id,
		},
		{
			"location_id": "P21-LOC-002",
			"location_code": "STAGING-WH",
			"location_name": "Staging Warehouse",
			"parent_location_code": "MAIN-WH",
			"company_id": company_id,
		},
		{
			"location_id": "P21-LOC-ALT-666",
			"location_code": "ALT-WH",
			"location_name": "Alt Company Facility",
			"parent_location_code": None,
			"company_id": alt_company_id,
		},
	]

	return {
		"synthetic_customer": customers,
		"synthetic_vendor": vendors,
		"synthetic_item": items,
		"synthetic_warehouse": warehouses,
	}
