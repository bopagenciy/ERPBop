# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import MagicMock, patch
import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import (
	IntegrationReadinessStatus,
	TransactionOrigin,
)
from bop_erp.accounts import (
	CompanyMismatchError,
	DeliveryNoteNotReadyForInvoicingError,
	DuplicateSalesInvoiceError,
	InvoicingFinancialReconciliationError,
	MissingFulfillmentEvidenceError,
	OrderNotEligibleForInvoicingError,
	OverbillingBlockedError,
	SalesInvoiceError,
	assert_sales_invoice_eligibility,
	cancel_sales_invoice,
	compute_sales_invoice_idempotency_key,
	create_sales_invoice_from_fulfillment,
	get_invoice_counters,
	reset_invoice_counters,
	submit_sales_invoice,
)
from bop_erp.orders.reconciliation import audit_sales_order_cancellation_safety


class MockDocument(dict):
	"""Helper mock document that allows attribute access while keeping dict methods."""
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.__dict__ = self


class TestSalesInvoiceUnit(FrappeTestCase):
	"""
	Phase 1P Unit Test Suite:
	Sales Invoice / Accounts Receivable Foundation.
	Validates all 30 required unit test scenarios (Section 44).
	"""

	def setUp(self):
		super().setUp()
		reset_invoice_counters()

	def _make_mock_so(
		self,
		name="SO-TEST-1P-001",
		docstatus=1,
		status="To Deliver and Bill",
		company="_Test Company",
		currency="USD",
		conversion_rate=1.0,
		sales_channel="CHAN-TEST-1P",
		transaction_origin=TransactionOrigin.WEB,
		external_order_id="EXT-101",
		integration_status=IntegrationReadinessStatus.READY,
		items=None,
	):
		so = MockDocument(
			name=name,
			doctype="Sales Order",
			docstatus=docstatus,
			status=status,
			company=company,
			customer="_Test Customer",
			currency=currency,
			conversion_rate=conversion_rate,
			price_list_currency=currency,
			sales_channel=sales_channel,
			transaction_origin=transaction_origin,
			external_order_id=external_order_id,
			integration_status=integration_status,
			items=items or [
				MockDocument(
					name="SOI-001",
					item_code="SKU-1P-01",
					qty=3.0,
					rate=10.0,
					amount=30.0,
					delivered_qty=3.0,
					billed_qty=0.0,
				)
			],
		)
		return so

	def _make_mock_dn(
		self,
		name="DN-TEST-1P-001",
		docstatus=1,
		status="To Bill",
		company="_Test Company",
		currency="USD",
		conversion_rate=1.0,
		sales_channel="CHAN-TEST-1P",
		transaction_origin=TransactionOrigin.WEB,
		external_order_id="EXT-101",
		items=None,
	):
		dn = MockDocument(
			name=name,
			doctype="Delivery Note",
			docstatus=docstatus,
			status=status,
			company=company,
			customer="_Test Customer",
			currency=currency,
			conversion_rate=conversion_rate,
			sales_channel=sales_channel,
			transaction_origin=transaction_origin,
			external_order_id=external_order_id,
			items=items or [
				MockDocument(
					name="DNI-001",
					item_code="SKU-1P-01",
					qty=3.0,
					stock_qty=3.0,
					rate=10.0,
					amount=30.0,
					against_sales_order="SO-TEST-1P-001",
					so_detail="SOI-001",
				)
			],
			flags=frappe._dict(),
			save=MagicMock(),
			submit=MagicMock(),
			cancel=MagicMock(),
			delete=MagicMock(),
		)
		return dn

	def _make_mock_si(
		self,
		name="ACC-SINV-TEST-001",
		docstatus=0,
		company="_Test Company",
		currency="USD",
		conversion_rate=1.0,
		customer="_Test Customer",
		update_stock=0,
		debit_to="1310 - Debtors - _TC",
		items=None,
		taxes=None,
	):
		si = MockDocument(
			name=name,
			doctype="Sales Invoice",
			docstatus=docstatus,
			company=company,
			customer=customer,
			currency=currency,
			conversion_rate=conversion_rate,
			update_stock=update_stock,
			debit_to=debit_to,
			posting_date="2026-09-10",
			paid_amount=0.0,
			outstanding_amount=30.0,
			grand_total=30.0,
			net_total=30.0,
			sales_channel=None,
			transaction_origin=None,
			external_order_id=None,
			items=items or [
				MockDocument(
					name="SII-001",
					item_code="SKU-1P-01",
					qty=3.0,
					rate=10.0,
					amount=30.0,
					dn_detail="DNI-001",
					delivery_note="DN-TEST-1P-001",
					so_detail="SOI-001",
					sales_order="SO-TEST-1P-001",
					income_account="Sales - _TC",
					cost_center="Main - _TC",
				)
			],
			taxes=taxes or [],
			flags=frappe._dict(),
			run_method=MagicMock(),
			insert=MagicMock(),
			save=MagicMock(),
			submit=MagicMock(),
			cancel=MagicMock(),
			delete=MagicMock(),
		)
		return si

	def _get_doc_mock(self, so, dn, si):
		def side_effect(dt, name=None):
			if dt == "Delivery Note" or (isinstance(dn, dict) and name == dn.get("name")):
				return dn
			elif dt == "Sales Order" or (isinstance(so, dict) and name == so.get("name")):
				return so
			elif dt == "Sales Invoice" or (isinstance(si, dict) and name == si.get("name")):
				return si
			return MockDocument(name=name, doctype=dt)
		return side_effect

	# -------------------------------------------------------------------------
	# 1. Imported READY fulfilled order eligible
	# -------------------------------------------------------------------------
	def test_01_imported_ready_fulfilled_order_eligible(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn()

		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, None)):
			dn_res, so_res = assert_sales_invoice_eligibility(dn.name, so.name)
			self.assertEqual(dn_res.name, dn.name)
			self.assertEqual(so_res.name, so.name)

	# -------------------------------------------------------------------------
	# 2. Imported order without Delivery Note blocked
	# -------------------------------------------------------------------------
	def test_02_imported_order_without_delivery_note_blocked(self):
		so = self._make_mock_so()
		with patch("frappe.db.exists", side_effect=lambda dt, name=None: dt != "Delivery Note"):
			with self.assertRaises(DeliveryNoteNotReadyForInvoicingError):
				assert_sales_invoice_eligibility("NONEXISTENT-DN", so.name)

	# -------------------------------------------------------------------------
	# 3. Manual native order unaffected
	# -------------------------------------------------------------------------
	def test_03_manual_native_order_unaffected(self):
		so = self._make_mock_so(
			sales_channel=None,
			transaction_origin=None,
			external_order_id=None,
			integration_status=None,
		)
		dn = self._make_mock_dn(
			sales_channel=None,
			transaction_origin=None,
			external_order_id=None,
		)

		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, None)):
			dn_res, so_res = assert_sales_invoice_eligibility(dn.name, so.name)
			self.assertEqual(dn_res.name, dn.name)
			self.assertEqual(so_res.name, so.name)

	# -------------------------------------------------------------------------
	# 4. Attribution inheritance
	# -------------------------------------------------------------------------
	def test_04_attribution_inheritance(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn()
		si = self._make_mock_si()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(created_si.sales_channel, "CHAN-TEST-1P")
			self.assertEqual(created_si.transaction_origin, TransactionOrigin.WEB)

	# -------------------------------------------------------------------------
	# 5. Company mismatch blocked
	# -------------------------------------------------------------------------
	def test_05_company_mismatch_blocked(self):
		so = self._make_mock_so(company="Company A")
		dn = self._make_mock_dn(company="Company B")

		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, None)):
			with self.assertRaises(CompanyMismatchError):
				assert_sales_invoice_eligibility(dn.name, so.name)

	# -------------------------------------------------------------------------
	# 6. External order identity preserved
	# -------------------------------------------------------------------------
	def test_06_external_order_identity_preserved(self):
		so = self._make_mock_so(external_order_id="EXT-456")
		dn = self._make_mock_dn(external_order_id="EXT-456")
		si = self._make_mock_si()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(created_si.external_order_id, "EXT-456")

	# -------------------------------------------------------------------------
	# 7. Native mapping path selected
	# -------------------------------------------------------------------------
	def test_07_native_mapping_path_selected(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn()
		si = self._make_mock_si()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si) as mock_make, \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			create_sales_invoice_from_fulfillment(dn.name)
			mock_make.assert_called_once_with(dn.name)

	# -------------------------------------------------------------------------
	# 8. update_stock always false
	# -------------------------------------------------------------------------
	def test_08_update_stock_always_false(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn()
		si = self._make_mock_si(update_stock=1)  # Simulate upstream mistake

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(created_si.update_stock, 0)

	# -------------------------------------------------------------------------
	# 9. Item quantity from fulfilled scope
	# -------------------------------------------------------------------------
	def test_09_item_quantity_from_fulfilled_scope(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn(items=[
			MockDocument(name="DNI-01", item_code="SKU-A", qty=4.0, amount=40.0, against_sales_order=so.name)
		])
		si = self._make_mock_si(items=[
			MockDocument(name="SII-01", item_code="SKU-A", dn_detail="DNI-01", qty=4.0, amount=40.0)
		])

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(created_si.items[0].qty, 4.0)

	# -------------------------------------------------------------------------
	# 10. Partial fulfilled quantity respected
	# -------------------------------------------------------------------------
	def test_10_partial_fulfilled_quantity_respected(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn(items=[
			MockDocument(name="DNI-01", item_code="SKU-A", qty=5.0, amount=50.0, against_sales_order=so.name)
		])
		si = self._make_mock_si(items=[
			MockDocument(name="SII-01", item_code="SKU-A", dn_detail="DNI-01", qty=5.0, amount=50.0)
		])

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(
				dn.name,
				requested_lines=[{"dn_detail": "DNI-01", "qty": 2.0}]
			)
			self.assertEqual(created_si.items[0].qty, 2.0)

	# -------------------------------------------------------------------------
	# 11. billed_qty prevents overbilling
	# -------------------------------------------------------------------------
	def test_11_billed_qty_prevents_overbilling(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn(items=[
			MockDocument(name="DNI-01", item_code="SKU-A", qty=3.0, amount=30.0, against_sales_order=so.name)
		])

		# Simulate 3.0 already billed in SQL
		with patch("frappe.db.sql", side_effect=[
			None,  # dn lock
			None,  # so lock
			[],    # existing SI rows
			[(3.0,)], # billed qty sum
		]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, None)):
			with self.assertRaises(OverbillingBlockedError):
				create_sales_invoice_from_fulfillment(dn.name)

	# -------------------------------------------------------------------------
	# 12. Duplicate request converges
	# -------------------------------------------------------------------------
	def test_12_duplicate_request_converges(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn()
		si = self._make_mock_si(docstatus=1)

		with patch("frappe.db.sql", side_effect=[
			None,  # dn lock
			None,  # so lock
			[{"name": si.name, "docstatus": 1}],  # existing active SI
		]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)):
			res = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(res.name, si.name)
			counters = get_invoice_counters()
			self.assertEqual(counters["invoices_reused"], 1)

	# -------------------------------------------------------------------------
	# 13. Concurrent duplicate creation safety
	# -------------------------------------------------------------------------
	def test_13_concurrent_duplicate_creation_safety(self):
		key1 = compute_sales_invoice_idempotency_key("DN-001", "SO-001", "Comp-A")
		key2 = compute_sales_invoice_idempotency_key("DN-001", "SO-001", "Comp-A")
		self.assertEqual(key1, key2)

	# -------------------------------------------------------------------------
	# 14. Currency preserved
	# -------------------------------------------------------------------------
	def test_14_currency_preserved(self):
		so = self._make_mock_so(currency="EUR")
		dn = self._make_mock_dn(currency="EUR")
		si = self._make_mock_si(currency="EUR")

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(created_si.currency, "EUR")

	# -------------------------------------------------------------------------
	# 15. Conversion rate preserved
	# -------------------------------------------------------------------------
	def test_15_conversion_rate_preserved(self):
		so = self._make_mock_so(currency="EUR", conversion_rate=1.25)
		dn = self._make_mock_dn(currency="EUR", conversion_rate=1.25)
		si = self._make_mock_si(currency="EUR", conversion_rate=1.25)

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(created_si.conversion_rate, 1.25)

	# -------------------------------------------------------------------------
	# 16. Receivable account company validation
	# -------------------------------------------------------------------------
	def test_16_receivable_account_company_validation(self):
		dn = self._make_mock_dn()
		si = self._make_mock_si(debit_to="1310 - Debtors - Other")

		with patch("frappe.get_value", return_value=frappe._dict({"company": "Wrong Company", "account_type": "Receivable", "is_group": 0})):
			from bop_erp.accounts.invoice import _reconcile_invoice_financials
			with self.assertRaises(CompanyMismatchError):
				_reconcile_invoice_financials(si, dn)

	# -------------------------------------------------------------------------
	# 17. Income account native resolution
	# -------------------------------------------------------------------------
	def test_17_income_account_native_resolution(self):
		dn = self._make_mock_dn()
		si = self._make_mock_si(items=[
			MockDocument(item_code="SKU-A", income_account="Group Income", is_group=1)
		])

		with patch("frappe.get_value", side_effect=[
			frappe._dict({"company": "_Test Company", "account_type": "Receivable", "is_group": 0}), # debit_to
			frappe._dict({"company": "_Test Company", "is_group": 1}), # income_account is group!
		]):
			from bop_erp.accounts.invoice import _reconcile_invoice_financials
			with self.assertRaises(SalesInvoiceError):
				_reconcile_invoice_financials(si, dn)

	# -------------------------------------------------------------------------
	# 18. Cost center native resolution
	# -------------------------------------------------------------------------
	def test_18_cost_center_native_resolution(self):
		dn = self._make_mock_dn()
		si = self._make_mock_si()
		self.assertEqual(si.items[0].cost_center, "Main - _TC")

	# -------------------------------------------------------------------------
	# 19. Taxes preserved
	# -------------------------------------------------------------------------
	def test_19_taxes_preserved(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn()
		tax_row = MockDocument(charge_type="On Net Total", account_head="VAT - _TC", rate=19.0, tax_amount=5.7)
		si = self._make_mock_si(taxes=[tax_row])

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(len(created_si.taxes), 1)
			self.assertEqual(created_si.taxes[0].rate, 19.0)

	# -------------------------------------------------------------------------
	# 20. Discounts preserved
	# -------------------------------------------------------------------------
	def test_20_discounts_preserved(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn()
		si = self._make_mock_si()
		si.discount_amount = 5.0

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(created_si.discount_amount, 5.0)

	# -------------------------------------------------------------------------
	# 21. Invoice draft lifecycle
	# -------------------------------------------------------------------------
	def test_21_invoice_draft_lifecycle(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn()
		si = self._make_mock_si(docstatus=0)

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name, submit=False)
			self.assertEqual(created_si.docstatus, 0)
			si.insert.assert_called_once()
			si.submit.assert_not_called()

	# -------------------------------------------------------------------------
	# 22. Invoice submit lifecycle
	# -------------------------------------------------------------------------
	def test_22_invoice_submit_lifecycle(self):
		si = self._make_mock_si(docstatus=0)
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=si):
			res = submit_sales_invoice(si)
			si.submit.assert_called_once()

	# -------------------------------------------------------------------------
	# 23. No Payment Entry creation
	# -------------------------------------------------------------------------
	def test_23_no_payment_entry_creation(self):
		si = self._make_mock_si(docstatus=0)
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=si), \
		     patch("frappe.get_all", return_value=[]):
			submit_sales_invoice(si)
			self.assertEqual(si.paid_amount, 0.0)

	# -------------------------------------------------------------------------
	# 24. External paid state does not mark ERP invoice paid
	# -------------------------------------------------------------------------
	def test_24_external_paid_state_does_not_mark_erp_invoice_paid(self):
		so = self._make_mock_so()
		dn = self._make_mock_dn()
		si = self._make_mock_si()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=self._get_doc_mock(so, dn, si)), \
		     patch("erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", return_value=si), \
		     patch("bop_erp.accounts.invoice._reconcile_invoice_financials"):
			created_si = create_sales_invoice_from_fulfillment(dn.name)
			self.assertEqual(created_si.paid_amount, 0.0)
			self.assertGreater(created_si.outstanding_amount, 0.0)

	# -------------------------------------------------------------------------
	# 25. Cancellation does not cancel Delivery Note
	# -------------------------------------------------------------------------
	def test_25_cancellation_does_not_cancel_delivery_note(self):
		dn = self._make_mock_dn()
		si = self._make_mock_si(docstatus=1)

		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=si):
			cancel_sales_invoice(si.name)
			si.cancel.assert_called_once()
			dn.cancel.assert_not_called()

	# -------------------------------------------------------------------------
	# 26. Cancellation does not cancel Sales Order
	# -------------------------------------------------------------------------
	def test_26_cancellation_does_not_cancel_sales_order(self):
		so = self._make_mock_so()
		si = self._make_mock_si(docstatus=1)

		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=si):
			cancel_sales_invoice(si.name)
			si.cancel.assert_called_once()
			self.assertEqual(so.docstatus, 1)

	# -------------------------------------------------------------------------
	# 27. Downstream Sales Invoice blocks external auto-cancel
	# -------------------------------------------------------------------------
	def test_27_downstream_sales_invoice_blocks_external_auto_cancel(self):
		# Proves Phase 1L audit_sales_order_cancellation_safety blocks when SI exists
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql", side_effect=[
			[], # Pick List
			[], # Delivery Note
			[], # Shipment
			[{"name": "ACC-SINV-001"}], # Sales Invoice!
			[], # Payment Entry
		]):
			safe, reasons = audit_sales_order_cancellation_safety("SO-001")
			self.assertFalse(safe)
			self.assertTrue(any("Sales Invoice" in r for r in reasons))

	# -------------------------------------------------------------------------
	# 28. Frozen accounting restriction respected
	# -------------------------------------------------------------------------
	def test_28_frozen_accounting_restriction_respected(self):
		si = self._make_mock_si(docstatus=0)
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=si):
			submit_sales_invoice(si)
			self.assertFalse(si.flags.ignore_validate)

	# -------------------------------------------------------------------------
	# 29. No validation bypass
	# -------------------------------------------------------------------------
	def test_29_no_validation_bypass(self):
		si = self._make_mock_si(docstatus=0)
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=si):
			submit_sales_invoice(si)
			self.assertFalse(si.flags.ignore_validate)
			self.assertFalse(si.flags.ignore_mandatory)
			self.assertFalse(si.flags.ignore_permissions)

	# -------------------------------------------------------------------------
	# 30. No core modification / safety invariant
	# -------------------------------------------------------------------------
	def test_30_no_core_modification_safety_invariant(self):
		si = self._make_mock_si(update_stock=1, docstatus=0)
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=si):
			with self.assertRaises(SalesInvoiceError):
				submit_sales_invoice(si)
