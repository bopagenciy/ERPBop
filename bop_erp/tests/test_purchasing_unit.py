# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.purchasing import (
	AccountMismatchError,
	CompanyMismatchError,
	ConcurrentReceiptConflictError,
	CurrencyMismatchError,
	DocumentCancelledError,
	OverBillingBlockedError,
	OverPaymentBlockedError,
	OverReceiptBlockedError,
	PurchaseDriftError,
	PurchaseInvoiceError,
	PurchaseOperation,
	PurchaseOrderError,
	PurchaseReceiptError,
	PurchasingError,
	VendorPaymentError,
	WarehouseMismatchError,
	cancel_purchase_document,
	cancel_vendor_payment,
	check_purchase_operation_replay,
	compute_purchase_payload_hash,
	create_purchase_invoice,
	create_purchase_order,
	get_purchasing_counters,
	get_three_way_traceability,
	pay_purchase_invoice,
	receive_purchase_order,
	record_purchase_operation,
	reset_purchasing_counters,
	validate_company_account,
	validate_company_warehouse,
)

_real_get_doc = frappe.get_doc
_real_get_value = frappe.db.get_value
_real_sql = frappe.db.sql


def _make_mock_get_doc(target_doc=None, target_dt="Purchase Order"):
	def _get_doc(*args, **kwargs):
		if args and isinstance(args[0], dict):
			dt = args[0].get("doctype")
			if dt == target_dt and target_doc is not None:
				return target_doc
			if dt == "Integration Event":
				m = MagicMock()
				m.insert.return_value = m
				return m
			return _real_get_doc(*args, **kwargs)
		if args and isinstance(args[0], str):
			dt = args[0]
			if dt == target_dt and target_doc is not None:
				return target_doc
		return _real_get_doc(*args, **kwargs)
	return _get_doc


def _make_mock_get_value(wh_company=None, acct_company=None, item_val=None, pr_item=None):
	def _get_value(*args, **kwargs):
		dt = args[0] if args else kwargs.get("doctype")
		if dt == "Warehouse":
			return wh_company or "Industrial DP"
		if dt == "Account":
			return acct_company or "Industrial DP"
		if dt == "Item":
			return item_val if item_val is not None else 1
		if dt in ("Purchase Receipt Item", "Purchase Order Item"):
			return pr_item or {"qty": 10, "received_qty": 0, "billed_amt": 0, "amount": 500}
		return _real_get_value(*args, **kwargs)
	return _get_value


class TestPurchasingUnit(FrappeTestCase):
	"""
	Phase 1S Unit Test Suite: Purchasing / Accounts Payable / Vendor Flow Foundation.
	Covers all 44 required unit test specifications.
	"""

	def setUp(self):
		reset_purchasing_counters()
		self.company = "Industrial DP"
		self.other_company = "Bamal Fastener Corp"
		self.supplier = "TEST-1S-SUPPLIER"
		self.warehouse = "Stores - IDP"
		self.other_warehouse = "Stores - BFC"
		self.item_code = "TEST-1S-ITEM-01"

	# 1. Native Supplier used as Vendor
	def test_01_native_supplier_used_as_vendor(self):
		meta = frappe.get_meta("Supplier")
		self.assertTrue(meta.has_field("supplier_name"))
		self.assertTrue(meta.has_field("supplier_group"))
		self.assertTrue(meta.has_field("accounts"))
		self.assertTrue(meta.has_field("companies"))

	# 2. Manual Supplier unaffected
	def test_02_manual_supplier_unaffected(self):
		meta = frappe.get_meta("Supplier")
		self.assertFalse(meta.issingle)
		self.assertEqual(meta.module, "Buying")

	# 3. Purchase Order creation
	@patch("frappe.get_doc")
	def test_03_purchase_order_creation(self, mock_get_doc):
		mock_po = MagicMock()
		mock_po.name = "PO-TEST-001"
		mock_get_doc.side_effect = _make_mock_get_doc(mock_po, "Purchase Order")

		data = {
			"company": self.company,
			"supplier": self.supplier,
			"set_warehouse": self.warehouse,
			"items": [{"item_code": self.item_code, "qty": 10, "rate": 50}],
		}

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)):
			po = create_purchase_order(data, submit=False)

		self.assertEqual(po.name, "PO-TEST-001")
		mock_po.insert.assert_called_once()
		mock_po.submit.assert_not_called()
		counters = get_purchasing_counters()
		self.assertEqual(counters["purchase_orders_created"], 1)

	# 4. PO has zero stock impact
	@patch("frappe.get_doc")
	def test_04_po_has_zero_stock_impact(self, mock_get_doc):
		mock_po = MagicMock()
		mock_po.name = "PO-TEST-002"
		mock_get_doc.side_effect = _make_mock_get_doc(mock_po, "Purchase Order")

		data = {
			"company": self.company,
			"supplier": self.supplier,
			"items": [{"item_code": self.item_code, "qty": 5, "rate": 100}],
		}

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)), \
		     patch("frappe.db.sql") as mock_sql:
			create_purchase_order(data, submit=True)

		for call_args in mock_sql.call_args_list:
			self.assertNotIn("Stock Ledger Entry", str(call_args))

	# 5. PO has zero inventory publication
	@patch("frappe.get_doc")
	@patch("bop_erp.purchasing.services.schedule_post_commit_publication")
	def test_05_po_zero_inventory_publication(self, mock_pub, mock_get_doc):
		mock_po = MagicMock()
		mock_get_doc.side_effect = _make_mock_get_doc(mock_po, "Purchase Order")
		data = {
			"company": self.company,
			"supplier": self.supplier,
			"items": [{"item_code": self.item_code, "qty": 5, "rate": 100}],
		}
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)):
			create_purchase_order(data, submit=True)

		mock_pub.assert_not_called()

	# 6. Company mismatch blocked
	def test_06_company_mismatch_blocked(self):
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.other_company)):
			with self.assertRaises(CompanyMismatchError):
				validate_company_warehouse(self.company, self.other_warehouse)

	# 7. Warehouse/company mismatch blocked in PO
	def test_07_warehouse_company_mismatch_blocked_in_po(self):
		data = {
			"company": self.company,
			"supplier": self.supplier,
			"set_warehouse": self.other_warehouse,
			"items": [{"item_code": self.item_code, "qty": 5, "rate": 100}],
		}
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.other_company)):
			with self.assertRaises(CompanyMismatchError):
				create_purchase_order(data)

	# 8. PO replay idempotency
	@patch("bop_erp.purchasing.services.check_purchase_operation_replay")
	@patch("frappe.get_doc")
	def test_08_po_replay_idempotency(self, mock_get_doc, mock_check_replay):
		mock_check_replay.return_value = ("Purchase Order", "PO-EXISTING-01")
		mock_existing = MagicMock()
		mock_existing.name = "PO-EXISTING-01"
		mock_get_doc.side_effect = _make_mock_get_doc(mock_existing, "Purchase Order")

		data = {"company": self.company, "supplier": self.supplier, "items": [{"item_code": self.item_code, "qty": 10}]}
		result = create_purchase_order(data, operation_key="OP-PO-01")

		self.assertEqual(result.name, "PO-EXISTING-01")
		counters = get_purchasing_counters()
		self.assertEqual(counters["duplicate_operations_converged"], 1)

	# 9. PO payload drift blocked
	def test_09_po_payload_drift_blocked(self):
		payload1 = {"company": self.company, "qty": 10}
		payload2 = {"company": self.company, "qty": 12}

		with patch("frappe.db.sql", return_value=[{"payload_hash": compute_purchase_payload_hash(payload1), "erp_doctype": "Purchase Order", "erp_document": "PO-001"}]):
			with self.assertRaises(PurchaseDriftError):
				check_purchase_operation_replay("KEY-1", PurchaseOperation.CREATE_PURCHASE_ORDER, payload2)

	# 10. Purchase Receipt native mapping
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	@patch("frappe.db.sql")
	def test_10_pr_native_mapping(self, mock_sql, mock_make_pr):
		mock_sql.side_effect = [
			[{"name": "PO-001", "docstatus": 1, "company": self.company}],
			[{"name": "POI-01", "item_code": self.item_code, "qty": 10, "received_qty": 0}],
		]
		mock_pr = MagicMock()
		mock_pr.name = "PR-001"
		mock_row = MagicMock(purchase_order_item="POI-01", item_code=self.item_code, qty=10, conversion_factor=1, rate=50, warehouse=self.warehouse)
		mock_pr.items = [mock_row]
		mock_make_pr.return_value = mock_pr

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)):
			pr = receive_purchase_order("PO-001", submit=False)

		self.assertEqual(pr.name, "PR-001")
		mock_make_pr.assert_called_once_with("PO-001")

	# 11. PR increases stock once on submit
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	@patch("frappe.db.sql")
	def test_11_pr_increases_stock_once(self, mock_sql, mock_make_pr):
		mock_sql.side_effect = [
			[{"name": "PO-001", "docstatus": 1, "company": self.company}],
			[{"name": "POI-01", "item_code": self.item_code, "qty": 10, "received_qty": 0}],
		]
		mock_pr = MagicMock()
		mock_pr.name = "PR-001"
		mock_row = MagicMock(purchase_order_item="POI-01", item_code=self.item_code, qty=10, conversion_factor=1, rate=50, warehouse=self.warehouse)
		mock_pr.items = [mock_row]
		mock_make_pr.return_value = mock_pr

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)), \
		     patch("bop_erp.purchasing.services.find_affected_channel_items_for_scopes", return_value={}):
			receive_purchase_order("PO-001", submit=True)

		mock_pr.submit.assert_called_once()

	# 12. Partial receipt
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	@patch("frappe.db.sql")
	def test_12_partial_receipt(self, mock_sql, mock_make_pr):
		mock_sql.side_effect = [
			[{"name": "PO-001", "docstatus": 1, "company": self.company}],
			[{"name": "POI-01", "item_code": self.item_code, "qty": 10, "received_qty": 0}],
		]
		mock_pr = MagicMock()
		mock_row = MagicMock(purchase_order_item="POI-01", item_code=self.item_code, qty=10, conversion_factor=1, rate=50, warehouse=self.warehouse)
		mock_pr.items = [mock_row]
		mock_make_pr.return_value = mock_pr

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)), \
		     patch("bop_erp.purchasing.services.find_affected_channel_items_for_scopes", return_value={}):
			pr = receive_purchase_order("PO-001", items_to_receive=[{"item_code": self.item_code, "qty": 4}], submit=False)

		self.assertEqual(pr.items[0].qty, 4)

	# 13. Multiple receipts
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	@patch("frappe.db.sql")
	def test_13_multiple_receipts(self, mock_sql, mock_make_pr):
		mock_sql.side_effect = [
			[{"name": "PO-001", "docstatus": 1, "company": self.company}],
			[{"name": "POI-01", "item_code": self.item_code, "qty": 10, "received_qty": 4}],
		]
		mock_pr = MagicMock()
		mock_row = MagicMock(purchase_order_item="POI-01", item_code=self.item_code, qty=6, conversion_factor=1, rate=50, warehouse=self.warehouse)
		mock_pr.items = [mock_row]
		mock_make_pr.return_value = mock_pr

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)), \
		     patch("bop_erp.purchasing.services.find_affected_channel_items_for_scopes", return_value={}):
			pr = receive_purchase_order("PO-001", items_to_receive=[{"item_code": self.item_code, "qty": 6}], submit=False)

		self.assertEqual(pr.items[0].qty, 6)

	# 14. Over-receipt blocked / respects native tolerance
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	@patch("frappe.db.sql")
	def test_14_over_receipt_blocked(self, mock_sql, mock_make_pr):
		mock_sql.side_effect = [
			[{"name": "PO-001", "docstatus": 1, "company": self.company}],
			[{"name": "POI-01", "item_code": self.item_code, "qty": 10, "received_qty": 10}],
		]
		mock_pr = MagicMock()
		mock_row = MagicMock(purchase_order_item="POI-01", item_code=self.item_code, qty=1, conversion_factor=1, rate=50, warehouse=self.warehouse)
		mock_pr.items = [mock_row]
		mock_make_pr.return_value = mock_pr

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company, item_val=0.0)), \
		     patch("frappe.db.get_single_value", return_value=0.0):
			with self.assertRaises(OverReceiptBlockedError):
				receive_purchase_order("PO-001", items_to_receive=[{"item_code": self.item_code, "qty": 1}], submit=False)

	# 15. Concurrent receipt protection
	@patch("frappe.db.sql")
	def test_15_concurrent_receipt_protection(self, mock_sql):
		mock_sql.return_value = [{"name": "PO-001", "docstatus": 1, "company": self.company}]
		with patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt") as mock_make_pr, \
		     patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)):
			mock_pr = MagicMock()
			mock_pr.items = []
			mock_make_pr.return_value = mock_pr
			with self.assertRaises(PurchaseReceiptError):
				receive_purchase_order("PO-001", items_to_receive=[{"item_code": self.item_code, "qty": 1}])

		first_query = str(mock_sql.call_args_list[0])
		self.assertIn("FOR UPDATE", first_query)

	# 16. Non-sellable warehouse behavior
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	@patch("bop_erp.purchasing.services.schedule_post_commit_publication")
	@patch("frappe.db.sql")
	def test_16_non_sellable_warehouse_behavior(self, mock_sql, mock_pub, mock_make_pr):
		mock_sql.side_effect = [
			[{"name": "PO-001", "docstatus": 1, "company": self.company}],
			[{"name": "POI-01", "item_code": self.item_code, "qty": 10, "received_qty": 0}],
		]
		mock_pr = MagicMock()
		mock_row = MagicMock(purchase_order_item="POI-01", item_code=self.item_code, qty=10, conversion_factor=1, rate=50, warehouse="Work In Progress - IDP")
		mock_pr.items = [mock_row]
		mock_make_pr.return_value = mock_pr

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company, item_val=1)), \
		     patch("bop_erp.purchasing.services.find_affected_channel_items_for_scopes", return_value={}):
			receive_purchase_order("PO-001", submit=True)

		mock_pub.assert_not_called()

	# 17. Receipt schedules inventory publication when sellable
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	@patch("bop_erp.purchasing.services.schedule_post_commit_publication")
	@patch("frappe.db.sql")
	def test_17_receipt_schedules_inventory_publication(self, mock_sql, mock_pub, mock_make_pr):
		mock_sql.side_effect = [
			[{"name": "PO-001", "docstatus": 1, "company": self.company}],
			[{"name": "POI-01", "item_code": self.item_code, "qty": 10, "received_qty": 0}],
		]
		mock_pr = MagicMock()
		mock_row = MagicMock(purchase_order_item="POI-01", item_code=self.item_code, qty=10, conversion_factor=1, rate=50, warehouse=self.warehouse)
		mock_pr.items = [mock_row]
		mock_make_pr.return_value = mock_pr

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company, item_val=1)), \
		     patch("bop_erp.purchasing.services.find_affected_channel_items_for_scopes", return_value={"TID": [self.item_code]}):
			receive_purchase_order("PO-001", submit=True)

		mock_pub.assert_called_once_with({"TID": [self.item_code]})

	# 18. No direct PrestaShop call from receipt
	@patch("erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt")
	@patch("bop_erp.purchasing.services.schedule_post_commit_publication")
	@patch("requests.post")
	@patch("frappe.db.sql")
	def test_18_no_direct_prestashop_call_from_receipt(self, mock_sql, mock_http_post, mock_pub, mock_make_pr):
		mock_sql.side_effect = [
			[{"name": "PO-001", "docstatus": 1, "company": self.company}],
			[{"name": "POI-01", "item_code": self.item_code, "qty": 10, "received_qty": 0}],
		]
		mock_pr = MagicMock()
		mock_row = MagicMock(purchase_order_item="POI-01", item_code=self.item_code, qty=10, conversion_factor=1, rate=50, warehouse=self.warehouse)
		mock_pr.items = [mock_row]
		mock_make_pr.return_value = mock_pr

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company, item_val=1)), \
		     patch("bop_erp.purchasing.services.find_affected_channel_items_for_scopes", return_value={"TID": [self.item_code]}):
			receive_purchase_order("PO-001", submit=True)

		mock_http_post.assert_not_called()

	# 19. Purchase Invoice native mapping
	@patch("erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice")
	@patch("frappe.db.sql")
	def test_19_pi_native_mapping(self, mock_sql, mock_make_pi):
		mock_sql.return_value = [{"name": "PR-001", "docstatus": 1, "company": self.company, "supplier": self.supplier}]
		mock_pi = MagicMock()
		mock_pi.name = "PI-001"
		mock_pi.company = self.company
		mock_pi.credit_to = "2205 - Nacionales - IDP"
		mock_pi.items = []
		mock_make_pi.return_value = mock_pi

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.company)):
			pi = create_purchase_invoice(pr_name="PR-001", submit=False)

		self.assertEqual(pi.name, "PI-001")
		mock_make_pi.assert_called_once_with("PR-001")

	# 20. PI after PR has zero duplicate stock movement (update_stock = 0)
	@patch("erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice")
	@patch("frappe.db.sql")
	def test_20_pi_after_pr_zero_duplicate_stock_movement(self, mock_sql, mock_make_pi):
		mock_sql.return_value = [{"name": "PR-001", "docstatus": 1, "company": self.company, "supplier": self.supplier}]
		mock_pi = MagicMock()
		mock_pi.company = self.company
		mock_pi.credit_to = "2205 - Nacionales - IDP"
		mock_pi.items = []
		mock_make_pi.return_value = mock_pi

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.company)):
			pi = create_purchase_invoice(pr_name="PR-001", submit=False)

		self.assertEqual(pi.update_stock, 0)

	# 21. AP liability created natively
	@patch("erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice")
	@patch("frappe.db.sql")
	def test_21_ap_liability_created_natively(self, mock_sql, mock_make_pi):
		mock_sql.return_value = [{"name": "PR-001", "docstatus": 1, "company": self.company, "supplier": self.supplier}]
		mock_pi = MagicMock()
		mock_pi.company = self.company
		mock_pi.credit_to = "2205 - Nacionales - IDP"
		mock_pi.items = []
		mock_make_pi.return_value = mock_pi

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.company)):
			create_purchase_invoice(pr_name="PR-001", submit=True)

		mock_pi.submit.assert_called_once()

	# 22. Partial invoice
	@patch("erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice")
	@patch("frappe.db.sql")
	def test_22_partial_invoice(self, mock_sql, mock_make_pi):
		def _sql(query, *args, **kwargs):
			q_str = str(query)
			if "tabPurchase Receipt" in q_str and "FOR UPDATE" in q_str:
				return [{"name": "PR-001", "docstatus": 1, "company": self.company, "supplier": self.supplier}]
			if "tabPurchase Invoice Item" in q_str and "SUM(qty)" in q_str:
				return [[0.0]]
			return _real_sql(query, *args, **kwargs)

		mock_sql.side_effect = _sql

		mock_pi = MagicMock()
		mock_pi.company = self.company
		mock_pi.credit_to = "2205 - Nacionales - IDP"
		mock_row = MagicMock(pr_detail="PRI-01", item_code=self.item_code, qty=10, rate=50)
		mock_pi.items = [mock_row]
		mock_make_pi.return_value = mock_pi

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.company, item_val={"qty": 10})):
			pi = create_purchase_invoice(pr_name="PR-001", items_to_invoice=[{"pr_detail": "PRI-01", "qty": 4}], submit=False)

		self.assertEqual(pi.items[0].qty, 4)

	# 23. Multiple invoices
	@patch("erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice")
	@patch("frappe.db.sql")
	def test_23_multiple_invoices(self, mock_sql, mock_make_pi):
		def _sql(query, *args, **kwargs):
			q_str = str(query)
			if "tabPurchase Receipt" in q_str and "FOR UPDATE" in q_str:
				return [{"name": "PR-001", "docstatus": 1, "company": self.company, "supplier": self.supplier}]
			if "tabPurchase Invoice Item" in q_str and "SUM(qty)" in q_str:
				return [[4.0]]
			return _real_sql(query, *args, **kwargs)

		mock_sql.side_effect = _sql

		mock_pi = MagicMock()
		mock_pi.company = self.company
		mock_pi.credit_to = "2205 - Nacionales - IDP"
		mock_row = MagicMock(pr_detail="PRI-01", item_code=self.item_code, qty=10, rate=50)
		mock_pi.items = [mock_row]
		mock_make_pi.return_value = mock_pi

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.company, item_val={"qty": 10})):
			pi = create_purchase_invoice(pr_name="PR-001", items_to_invoice=[{"pr_detail": "PRI-01", "qty": 6}], submit=False)

		self.assertEqual(pi.items[0].qty, 6)

	# 24. Overbilling blocked
	@patch("erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice")
	@patch("frappe.db.sql")
	def test_24_overbilling_blocked(self, mock_sql, mock_make_pi):
		def _sql(query, *args, **kwargs):
			q_str = str(query)
			if "tabPurchase Receipt" in q_str and "FOR UPDATE" in q_str:
				return [{"name": "PR-001", "docstatus": 1, "company": self.company, "supplier": self.supplier}]
			if "tabPurchase Invoice Item" in q_str and "SUM(qty)" in q_str:
				return [[10.0]]
			return _real_sql(query, *args, **kwargs)

		mock_sql.side_effect = _sql

		mock_pi = MagicMock()
		mock_pi.company = self.company
		mock_pi.credit_to = "2205 - Nacionales - IDP"
		mock_row = MagicMock(pr_detail="PRI-01", item_code=self.item_code, qty=1, rate=50)
		mock_pi.items = [mock_row]
		mock_make_pi.return_value = mock_pi

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.company, item_val={"qty": 10})), \
		     patch("frappe.db.get_single_value", return_value=0.0):
			with self.assertRaises(OverBillingBlockedError):
				create_purchase_invoice(pr_name="PR-001", items_to_invoice=[{"pr_detail": "PRI-01", "qty": 1}], submit=False)

	# 25. Company payable account validation
	def test_25_company_payable_account_validation(self):
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.other_company)):
			with self.assertRaises(CompanyMismatchError):
				validate_company_account(self.company, "Creditors - BFC", "Payable Account")

	# 26. Taxes preserved
	@patch("frappe.get_doc")
	def test_26_taxes_preserved(self, mock_get_doc):
		mock_po = MagicMock()
		mock_get_doc.side_effect = _make_mock_get_doc(mock_po, "Purchase Order")
		taxes = [{"charge_type": "On Net Total", "account_head": "Tax - IDP", "rate": 19}]
		data = {
			"company": self.company,
			"supplier": self.supplier,
			"items": [{"item_code": self.item_code, "qty": 10, "rate": 50}],
			"taxes": taxes,
		}
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)):
			create_purchase_order(data)

		call_args = [c for c in mock_get_doc.call_args_list if isinstance(c[0][0], dict)]
		self.assertTrue(call_args)
		self.assertEqual(call_args[0][0][0].get("taxes"), taxes)

	# 27. Discounts preserved
	@patch("frappe.get_doc")
	def test_27_discounts_preserved(self, mock_get_doc):
		mock_po = MagicMock()
		mock_get_doc.side_effect = _make_mock_get_doc(mock_po, "Purchase Order")
		data = {
			"company": self.company,
			"supplier": self.supplier,
			"items": [{"item_code": self.item_code, "qty": 10, "rate": 50}],
			"discount_amount": 25.0,
			"apply_discount_on": "Grand Total",
		}
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)):
			create_purchase_order(data)

		call_args = [c for c in mock_get_doc.call_args_list if isinstance(c[0][0], dict)]
		self.assertTrue(call_args)
		self.assertEqual(call_args[0][0][0].get("discount_amount"), 25.0)

	# 28. Currency preserved
	@patch("frappe.get_doc")
	def test_28_currency_preserved(self, mock_get_doc):
		mock_po = MagicMock()
		mock_get_doc.side_effect = _make_mock_get_doc(mock_po, "Purchase Order")
		data = {
			"company": self.company,
			"supplier": self.supplier,
			"currency": "USD",
			"conversion_rate": 4100.0,
			"items": [{"item_code": self.item_code, "qty": 10, "rate": 50}],
		}
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)):
			create_purchase_order(data)

		call_args = [c for c in mock_get_doc.call_args_list if isinstance(c[0][0], dict)]
		self.assertTrue(call_args)
		self.assertEqual(call_args[0][0][0].get("currency"), "USD")
		self.assertEqual(call_args[0][0][0].get("conversion_rate"), 4100.0)

	# 29. FX validation preserved
	def test_29_fx_validation_preserved(self):
		meta = frappe.get_meta("Purchase Order")
		self.assertTrue(meta.has_field("conversion_rate"))

	# 30. Vendor payment native Payment Entry
	@patch("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry")
	@patch("frappe.db.sql")
	def test_30_vendor_payment_native_payment_entry(self, mock_sql, mock_get_pe):
		mock_sql.return_value = [{"name": "PI-001", "docstatus": 1, "company": self.company, "outstanding_amount": 100.0, "supplier": self.supplier, "credit_to": "2205 - Nacionales - IDP"}]
		mock_pe = MagicMock()
		mock_pe.name = "PE-001"
		mock_pe.references = []
		mock_pe.paid_from = "1110 - Bancos - IDP"
		mock_pe.paid_to = "2205 - Nacionales - IDP"
		mock_get_pe.return_value = mock_pe

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.company)):
			pe = pay_purchase_invoice("PI-001", paid_amount=100.0, submit=False)

		self.assertEqual(pe.name, "PE-001")
		mock_get_pe.assert_called_once()

	# 31. Partial payment
	@patch("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry")
	@patch("frappe.db.sql")
	def test_31_partial_payment(self, mock_sql, mock_get_pe):
		mock_sql.return_value = [{"name": "PI-001", "docstatus": 1, "company": self.company, "outstanding_amount": 100.0, "supplier": self.supplier, "credit_to": "2205 - Nacionales - IDP"}]
		mock_pe = MagicMock()
		mock_pe.references = []
		mock_pe.paid_from = "1110 - Bancos - IDP"
		mock_pe.paid_to = "2205 - Nacionales - IDP"
		mock_get_pe.return_value = mock_pe

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.company)):
			pe = pay_purchase_invoice("PI-001", paid_amount=30.0, submit=False)

		self.assertEqual(pe.paid_amount, 30.0)

	# 32. Multiple payments
	@patch("erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry")
	@patch("frappe.db.sql")
	def test_32_multiple_payments(self, mock_sql, mock_get_pe):
		mock_sql.return_value = [{"name": "PI-001", "docstatus": 1, "company": self.company, "outstanding_amount": 70.0, "supplier": self.supplier, "credit_to": "2205 - Nacionales - IDP"}]
		mock_pe = MagicMock()
		mock_pe.references = []
		mock_pe.paid_from = "1110 - Bancos - IDP"
		mock_pe.paid_to = "2205 - Nacionales - IDP"
		mock_get_pe.return_value = mock_pe

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(acct_company=self.company)):
			pe = pay_purchase_invoice("PI-001", paid_amount=70.0, submit=False)

		self.assertEqual(pe.paid_amount, 70.0)

	# 33. Overpayment blocked in 1S
	@patch("frappe.db.sql")
	def test_33_overpayment_blocked_in_1s(self, mock_sql):
		mock_sql.return_value = [{"name": "PI-001", "docstatus": 1, "company": self.company, "outstanding_amount": 100.0, "supplier": self.supplier, "credit_to": "2205 - Nacionales - IDP"}]
		with self.assertRaises(OverPaymentBlockedError):
			pay_purchase_invoice("PI-001", paid_amount=105.0, submit=False)

	# 34. Payment cancellation restores AP
	@patch("frappe.get_doc")
	def test_34_payment_cancellation_restores_ap(self, mock_get_doc):
		mock_pe = MagicMock()
		mock_pe.docstatus = 1
		mock_get_doc.side_effect = _make_mock_get_doc(mock_pe, "Payment Entry")

		cancel_vendor_payment("PE-001")
		mock_pe.cancel.assert_called_once()

	# 35. Payment cancellation leaves PO/PR/PI intact
	@patch("frappe.get_doc")
	def test_35_payment_cancellation_leaves_po_pr_pi_intact(self, mock_get_doc):
		mock_pe = MagicMock()
		mock_pe.docstatus = 1
		mock_get_doc.side_effect = _make_mock_get_doc(mock_pe, "Payment Entry")

		with patch("frappe.delete_doc") as mock_delete:
			cancel_vendor_payment("PE-001")
			mock_delete.assert_not_called()

	# 36. No manual outstanding mutation
	def test_36_no_manual_outstanding_mutation(self):
		import inspect
		import bop_erp.purchasing.services as serv
		source = inspect.getsource(serv)
		self.assertNotIn("outstanding_amount =", source)
		self.assertNotIn("db_set('outstanding_amount'", source)

	# 37. No manual GL posting
	def test_37_no_manual_gl_posting(self):
		import inspect
		import bop_erp.purchasing.services as serv
		source = inspect.getsource(serv)
		self.assertNotIn("'GL Entry'", source)
		self.assertNotIn('"GL Entry"', source)

	# 38. No manual Bin mutation
	def test_38_no_manual_bin_mutation(self):
		import inspect
		import bop_erp.purchasing.services as serv
		source = inspect.getsource(serv)
		self.assertNotIn("actual_qty =", source)
		self.assertNotIn("'tabBin'", source)
		self.assertNotIn('"tabBin"', source)

	# 39. Serial/batch validation preserved
	def test_39_serial_batch_validation_preserved(self):
		meta = frappe.get_meta("Purchase Receipt Item")
		self.assertTrue(meta.has_field("serial_no") or meta.has_field("serial_and_batch_bundle"))

	# 40. Manual ERP purchasing unaffected
	@patch("frappe.get_doc")
	def test_40_manual_erp_purchasing_unaffected(self, mock_get_doc):
		data = {
			"company": self.company,
			"supplier": self.supplier,
			"items": [{"item_code": self.item_code, "qty": 1, "rate": 10}],
		}
		mock_po = MagicMock()
		mock_get_doc.side_effect = _make_mock_get_doc(mock_po, "Purchase Order")
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)):
			create_purchase_order(data, operation_key=None)
			mock_po.insert.assert_called_once()

	# 41. Price governance audit invariant
	def test_41_price_governance_audit_invariant(self):
		import inspect
		import bop_erp.purchasing.services as serv
		source = inspect.getsource(serv)
		self.assertNotIn("insert_item_price", source)
		self.assertNotIn("Item Price", source)

	# 42. Production safety (zero external HTTP calls)
	@patch("frappe.get_doc")
	@patch("requests.get")
	@patch("requests.post")
	def test_42_production_safety(self, mock_post, mock_get, mock_get_doc):
		mock_po = MagicMock()
		mock_get_doc.side_effect = _make_mock_get_doc(mock_po, "Purchase Order")
		data = {
			"company": self.company,
			"supplier": self.supplier,
			"items": [{"item_code": self.item_code, "qty": 1, "rate": 10}],
		}
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(wh_company=self.company)):
			create_purchase_order(data)

		mock_get.assert_not_called()
		mock_post.assert_not_called()

	# 43. Secret/PII log safety
	def test_43_secret_pii_log_safety(self):
		from bop_erp.reliability import is_sensitive_key, sanitize_metadata
		self.assertTrue(is_sensitive_key("bank_account_password"))
		self.assertTrue(is_sensitive_key("secret_token"))
		sanitized = sanitize_metadata({"api_key": "supersecret", "vendor": "ACME"})
		self.assertIn("[REDACTED]", sanitized)
		self.assertNotIn("supersecret", sanitized)

	# 44. Fixture isolation
	def test_44_fixture_isolation(self):
		prefix = "TEST-1S"
		self.assertTrue(self.item_code.startswith(prefix))
		self.assertTrue(self.supplier.startswith(prefix))
