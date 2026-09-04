# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from bop_erp.constants import ChannelType, IntegrationProvider

def seed_test_channels():
	company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
	if not company:
		return

	channels = [
		{
			"channel_id": "TID",
			"channel_name": "The Industrial Depot",
			"channel_type": ChannelType.PRESTASHOP,
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"company": company,
			"active": 1,
		},
		{
			"channel_id": "BAMAL",
			"channel_name": "Bamal",
			"channel_type": ChannelType.PRESTASHOP,
			"integration_provider": IntegrationProvider.PRESTASHOP,
			"company": company,
			"active": 1,
		},
		{
			"channel_id": "PHONE",
			"channel_name": "Phone Sales",
			"channel_type": ChannelType.PHONE,
			"integration_provider": IntegrationProvider.NONE,
			"company": company,
			"active": 1,
		},
		{
			"channel_id": "COUNTER",
			"channel_name": "Counter Sales",
			"channel_type": ChannelType.COUNTER,
			"integration_provider": IntegrationProvider.NONE,
			"company": company,
			"active": 1,
		},
	]

	for ch in channels:
		if not frappe.db.exists("Sales Channel", ch["channel_id"]):
			doc = frappe.get_doc({
				"doctype": "Sales Channel",
				**ch
			})
			doc.insert(ignore_permissions=True)
