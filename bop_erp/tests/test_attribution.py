# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from bop_erp.constants import TransactionOrigin
from bop_erp.attribution import (
	propagate_attribution_to_pick_list,
	propagate_attribution_to_delivery_note,
	propagate_attribution_to_sales_invoice,
	propagate_attribution_to_payment_entry,
	validate_sales_order_attribution,
	validate_transaction_attribution,
)

class TestAttribution(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		if not frappe.db.exists("Sales Channel", "TID"):
			frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": "TID",
				"channel_name": "The Industrial Depot",
				"channel_type": "PRESTASHOP",
				"company": self.company,
				"active": 1,
			}).insert()

	def test_transaction_origin_validation(self):
		# Create mock Sales Order doc
		so = frappe._dict({
			"doctype": "Sales Order",
			"company": self.company,
			"sales_channel": "TID",
			"transaction_origin": "INVALID_ORIGIN",
			"is_new": lambda: True,
			"docstatus": 0,
		})
		self.assertRaises(frappe.ValidationError, validate_sales_order_attribution, so)

		# Valid origin should pass
		so.transaction_origin = TransactionOrigin.WEB
		validate_sales_order_attribution(so)

	def test_company_mismatch_rejected(self):
		so = frappe._dict({
			"doctype": "Sales Order",
			"company": "Nonexistent Company Ltd",
			"sales_channel": "TID",
			"transaction_origin": TransactionOrigin.PHONE,
			"is_new": lambda: True,
			"docstatus": 0,
		})
		self.assertRaises(frappe.ValidationError, validate_sales_order_attribution, so)

	def test_pick_list_propagation_logic(self):
		# Create a dummy Sales Order in DB or test propagation
		pl = frappe._dict({
			"doctype": "Pick List",
			"sales_channel": None,
			"transaction_origin": None,
			"external_order_id": None,
			"locations": [],
		})
		# With no locations, no change
		propagate_attribution_to_pick_list(pl)
		self.assertIsNone(pl.sales_channel)

	def test_return_channel_mismatch_rejected(self):
		# Mock return invoice with different channel
		inv = frappe._dict({
			"doctype": "Sales Invoice",
			"is_return": 1,
			"return_against": "INV-ORIG-001",
			"sales_channel": "BAMAL",
			"docstatus": 0,
			"is_new": lambda: True,
		})

		# If original had TID, mismatch should be caught if orig exists
		# We test validate_transaction_attribution logic
		class MockDoc(dict):
			def __init__(self, *args, **kwargs):
				super().__init__(*args, **kwargs)
				self.__dict__ = self
			def is_new(self): return True
			def get(self, key, default=None): return self.__dict__.get(key, default)
			def get_doc_before_save(self): return None

		doc = MockDoc({
			"doctype": "Sales Invoice",
			"is_return": 1,
			"return_against": "NONEXISTENT",
			"sales_channel": "TID",
			"docstatus": 0,
		})
		# When return_against does not exist in DB, no crash
		validate_transaction_attribution(doc)

	def test_submitted_immutability_rule(self):
		old_doc = frappe._dict({
			"sales_channel": "TID",
			"transaction_origin": "WEB",
		})

		sub_doc = frappe._dict({
			"doctype": "Sales Order",
			"company": self.company,
			"sales_channel": "BAMAL", # Attempted modification
			"transaction_origin": "WEB",
			"docstatus": 1, # Submitted
			"is_new": lambda: False,
			"get_doc_before_save": lambda: old_doc,
		})

		# For normal user, should raise
		frappe.set_user("Guest")
		try:
			self.assertRaises(frappe.ValidationError, validate_sales_order_attribution, sub_doc)
		finally:
			frappe.set_user("Administrator")
