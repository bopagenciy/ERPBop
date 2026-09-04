# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from unittest.mock import patch
import frappe
from frappe.tests.utils import FrappeTestCase
from bop_erp.constants import TransactionOrigin
from bop_erp.attribution import (
	propagate_attribution_to_pick_list,
	propagate_attribution_to_delivery_note,
	propagate_attribution_to_sales_invoice,
	propagate_attribution_to_shipment,
	propagate_attribution_to_payment_entry,
	get_payment_channel_breakdown,
	get_payment_origin_breakdown,
	validate_sales_order_attribution,
	validate_transaction_attribution,
	validate_submitted_immutability,
)

class TestAttribution(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")
		for ch, name in [("TID", "The Industrial Depot"), ("BAMAL", "Bamal Fasteners")]:
			if not frappe.db.exists("Sales Channel", ch):
				frappe.get_doc({
					"doctype": "Sales Channel",
					"channel_id": ch,
					"channel_name": name,
					"channel_type": "PRESTASHOP",
					"company": self.company,
					"active": 1,
				}).insert()

	def test_transaction_origin_validation(self):
		so = frappe._dict({
			"doctype": "Sales Order",
			"company": self.company,
			"sales_channel": "TID",
			"transaction_origin": "INVALID_ORIGIN",
			"is_new": lambda: True,
			"docstatus": 0,
		})
		self.assertRaises(frappe.ValidationError, validate_sales_order_attribution, so)

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

	def test_homogeneous_pick_list_receives_sales_channel(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order" and name in ("SO-001", "SO-002"):
				return frappe._dict({
					"sales_channel": "TID",
					"transaction_origin": TransactionOrigin.WEB,
				})
			return None

		pl = frappe._dict({
			"doctype": "Pick List",
			"sales_channel": None,
			"transaction_origin": None,
			"locations": [
				frappe._dict({"sales_order": "SO-001"}),
				frappe._dict({"sales_order": "SO-002"}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_pick_list(pl)

		self.assertEqual(pl.sales_channel, "TID")
		self.assertEqual(pl.transaction_origin, TransactionOrigin.WEB)

	def test_mixed_pick_list_receives_none(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order":
				if name == "SO-001":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": TransactionOrigin.WEB})
				if name == "SO-002":
					return frappe._dict({"sales_channel": "BAMAL", "transaction_origin": TransactionOrigin.EDI})
			return None

		pl = frappe._dict({
			"doctype": "Pick List",
			"sales_channel": "TID",
			"transaction_origin": TransactionOrigin.WEB,
			"locations": [
				frappe._dict({"sales_order": "SO-001"}),
				frappe._dict({"sales_order": "SO-002"}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_pick_list(pl)

		self.assertIsNone(pl.sales_channel)
		self.assertIsNone(pl.transaction_origin)

	def test_pick_list_same_channel_different_origins(self):
		# Independent resolution: same channel inherits channel, but differing origins set origin to None
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order":
				if name == "SO-001":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": TransactionOrigin.WEB})
				if name == "SO-002":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": TransactionOrigin.PHONE})
			return None

		pl = frappe._dict({
			"doctype": "Pick List",
			"sales_channel": None,
			"transaction_origin": None,
			"locations": [
				frappe._dict({"sales_order": "SO-001"}),
				frappe._dict({"sales_order": "SO-002"}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_pick_list(pl)

		self.assertEqual(pl.sales_channel, "TID")
		self.assertIsNone(pl.transaction_origin)

	def test_delivery_note_mixed_channel_rejected(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order":
				if name == "SO-TID":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB", "external_order_id": "101"})
				if name == "SO-BAMAL":
					return frappe._dict({"sales_channel": "BAMAL", "transaction_origin": "EDI", "external_order_id": "202"})
			return None

		dn = frappe._dict({
			"doctype": "Delivery Note",
			"is_return": 0,
			"items": [
				frappe._dict({"against_sales_order": "SO-TID"}),
				frappe._dict({"against_sales_order": "SO-BAMAL"}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			self.assertRaises(frappe.ValidationError, propagate_attribution_to_delivery_note, dn)

	def test_delivery_note_homogeneous_propagates(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order" and name == "SO-TID":
				return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB", "external_order_id": "101"})
			return None

		dn = frappe._dict({
			"doctype": "Delivery Note",
			"is_return": 0,
			"items": [
				frappe._dict({"against_sales_order": "SO-TID"}),
				frappe._dict({"against_sales_order": "SO-TID"}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_delivery_note(dn)

		self.assertEqual(dn.sales_channel, "TID")
		self.assertEqual(dn.transaction_origin, "WEB")
		self.assertEqual(dn.external_order_id, "101")

	def test_delivery_note_same_channel_different_origins_supported(self):
		# Delivery Note should NOT reject same-channel orders with differing origins
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order":
				if name == "SO-1":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB", "external_order_id": "101"})
				if name == "SO-2":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": "PHONE", "external_order_id": "102"})
			return None

		dn = frappe._dict({
			"doctype": "Delivery Note",
			"is_return": 0,
			"items": [
				frappe._dict({"against_sales_order": "SO-1"}),
				frappe._dict({"against_sales_order": "SO-2"}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_delivery_note(dn)

		self.assertEqual(dn.sales_channel, "TID")
		self.assertIsNone(dn.transaction_origin)
		self.assertIsNone(dn.external_order_id)

	def test_sales_invoice_mixed_channel_rejected(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order":
				if name == "SO-TID":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB", "external_order_id": "101"})
				if name == "SO-BAMAL":
					return frappe._dict({"sales_channel": "BAMAL", "transaction_origin": "EDI", "external_order_id": "202"})
			return None

		si = frappe._dict({
			"doctype": "Sales Invoice",
			"is_return": 0,
			"items": [
				frappe._dict({"sales_order": "SO-TID", "delivery_note": None}),
				frappe._dict({"sales_order": "SO-BAMAL", "delivery_note": None}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			self.assertRaises(frappe.ValidationError, propagate_attribution_to_sales_invoice, si)

	def test_sales_invoice_homogeneous_propagates(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order" and name == "SO-TID":
				return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB", "external_order_id": "101"})
			return None

		si = frappe._dict({
			"doctype": "Sales Invoice",
			"is_return": 0,
			"items": [
				frappe._dict({"sales_order": "SO-TID", "delivery_note": None}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_sales_invoice(si)

		self.assertEqual(si.sales_channel, "TID")
		self.assertEqual(si.transaction_origin, "WEB")
		self.assertEqual(si.external_order_id, "101")

	def test_sales_invoice_same_channel_different_origins_supported(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order":
				if name == "SO-1":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB", "external_order_id": "101"})
				if name == "SO-2":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": "PHONE", "external_order_id": "102"})
			return None

		si = frappe._dict({
			"doctype": "Sales Invoice",
			"is_return": 0,
			"items": [
				frappe._dict({"sales_order": "SO-1", "delivery_note": None}),
				frappe._dict({"sales_order": "SO-2", "delivery_note": None}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_sales_invoice(si)

		self.assertEqual(si.sales_channel, "TID")
		self.assertIsNone(si.transaction_origin)
		self.assertIsNone(si.external_order_id)

	def test_shipment_mixed_channel_rejected(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Delivery Note":
				if name == "DN-TID":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB", "external_order_id": "101"})
				if name == "DN-BAMAL":
					return frappe._dict({"sales_channel": "BAMAL", "transaction_origin": "EDI", "external_order_id": "202"})
			return None

		shp = frappe._dict({
			"doctype": "Shipment",
			"delivery_notes": [
				frappe._dict({"delivery_note": "DN-TID"}),
				frappe._dict({"delivery_note": "DN-BAMAL"}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			self.assertRaises(frappe.ValidationError, propagate_attribution_to_shipment, shp)

	def test_shipment_homogeneous_propagates(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Delivery Note" and name == "DN-TID":
				return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB", "external_order_id": "101"})
			return None

		shp = frappe._dict({
			"doctype": "Shipment",
			"delivery_notes": [
				frappe._dict({"delivery_note": "DN-TID"}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_shipment(shp)

		self.assertEqual(shp.sales_channel, "TID")
		self.assertEqual(shp.transaction_origin, "WEB")
		self.assertEqual(shp.external_order_id, "101")

	def test_shipment_same_channel_different_origins_supported(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Delivery Note":
				if name == "DN-1":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB", "external_order_id": "101"})
				if name == "DN-2":
					return frappe._dict({"sales_channel": "TID", "transaction_origin": "EDI", "external_order_id": "102"})
			return None

		shp = frappe._dict({
			"doctype": "Shipment",
			"delivery_notes": [
				frappe._dict({"delivery_note": "DN-1"}),
				frappe._dict({"delivery_note": "DN-2"}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_shipment(shp)

		self.assertEqual(shp.sales_channel, "TID")
		self.assertIsNone(shp.transaction_origin)

	def test_payment_entry_homogeneous_attribution(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Invoice" and name in ("INV-TID-1", "INV-TID-2"):
				if fieldname == "sales_channel":
					return "TID"
				if fieldname == "transaction_origin":
					return TransactionOrigin.WEB
			return None

		pe = frappe._dict({
			"doctype": "Payment Entry",
			"sales_channel": None,
			"transaction_origin": None,
			"references": [
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-TID-1", "allocated_amount": 100.0}),
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-TID-2", "allocated_amount": 150.0}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_payment_entry(pe)

		self.assertEqual(pe.sales_channel, "TID")
		self.assertEqual(pe.transaction_origin, TransactionOrigin.WEB)

	def test_payment_entry_same_channel_different_origins(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Invoice":
				if name == "INV-1":
					return "TID" if fieldname == "sales_channel" else TransactionOrigin.WEB
				if name == "INV-2":
					return "TID" if fieldname == "sales_channel" else TransactionOrigin.PHONE
			return None

		pe = frappe._dict({
			"doctype": "Payment Entry",
			"sales_channel": None,
			"transaction_origin": None,
			"references": [
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-1", "allocated_amount": 100.0}),
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-2", "allocated_amount": 50.0}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_payment_entry(pe)

		self.assertEqual(pe.sales_channel, "TID")
		self.assertIsNone(pe.transaction_origin)

	def test_payment_entry_multichannel_sets_none(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Invoice":
				if name == "INV-TID":
					return "TID" if fieldname == "sales_channel" else TransactionOrigin.WEB
				if name == "INV-BAMAL":
					return "BAMAL" if fieldname == "sales_channel" else TransactionOrigin.EDI
			return None

		pe = frappe._dict({
			"doctype": "Payment Entry",
			"sales_channel": "TID",
			"transaction_origin": TransactionOrigin.WEB,
			"references": [
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-TID", "allocated_amount": 100.0}),
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-BAMAL", "allocated_amount": 50.0}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			propagate_attribution_to_payment_entry(pe)

		self.assertIsNone(pe.sales_channel)
		self.assertIsNone(pe.transaction_origin)

	def test_payment_entry_allocation_breakdown(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Invoice":
				if name == "INV-TID":
					return "TID"
				if name == "INV-BAMAL":
					return "BAMAL"
			return None

		pe = frappe._dict({
			"doctype": "Payment Entry",
			"references": [
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-TID", "allocated_amount": 250.0}),
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-BAMAL", "allocated_amount": 150.0}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			breakdown = get_payment_channel_breakdown(pe)

		self.assertEqual(breakdown, {"TID": 250.0, "BAMAL": 150.0})

	def test_payment_entry_origin_breakdown(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Invoice":
				if name == "INV-1":
					return TransactionOrigin.WEB
				if name == "INV-2":
					return TransactionOrigin.PHONE
			return None

		pe = frappe._dict({
			"doctype": "Payment Entry",
			"references": [
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-1", "allocated_amount": 120.0}),
				frappe._dict({"reference_doctype": "Sales Invoice", "reference_name": "INV-2", "allocated_amount": 80.0}),
			],
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			breakdown = get_payment_origin_breakdown(pe)

		self.assertEqual(breakdown, {TransactionOrigin.WEB: 120.0, TransactionOrigin.PHONE: 80.0})

	def test_return_channel_mismatch_rejected(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Invoice" and name == "INV-ORIG-001":
				return "TID"
			return None

		doc = frappe._dict({
			"doctype": "Sales Invoice",
			"name": "INV-RET-001",
			"is_return": 1,
			"return_against": "INV-ORIG-001",
			"sales_channel": "BAMAL",
			"docstatus": 0,
			"is_new": lambda: True,
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			self.assertRaises(frappe.ValidationError, validate_transaction_attribution, doc)

	def test_submitted_immutability_reads_persisted_db_state(self):
		def mock_db_get_value(doctype, name, fieldname, *args, **kwargs):
			if doctype == "Sales Order" and name == "SO-SUBMITTED-01":
				return frappe._dict({"sales_channel": "TID", "transaction_origin": "WEB"})
			return None

		sub_doc = frappe._dict({
			"doctype": "Sales Order",
			"name": "SO-SUBMITTED-01",
			"company": self.company,
			"sales_channel": "BAMAL",
			"transaction_origin": "WEB",
			"docstatus": 1,
			"is_new": lambda: False,
			"get": lambda key, default=None: "BAMAL" if key == "sales_channel" else "WEB",
			"get_doc_before_save": lambda: frappe._dict({"sales_channel": "BAMAL", "transaction_origin": "WEB"}),
		})

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			self.assertRaises(frappe.ValidationError, validate_submitted_immutability, sub_doc)
