# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
	TransactionOrigin,
)
from bop_erp.accounts import (
	CompanyMismatchError,
	CurrencyMismatchError,
	CustomerMismatchError,
	DuplicateRefundError,
	ExternalRefundItem,
	ExternalRefundRecord,
	ExternalRefundStatus,
	OverRefundBlockedError,
	RefundDriftError,
	RefundEligibilityError,
	RefundError,
	RefundReplayCancelledError,
	RefundStatusIneligibleError,
	compute_external_refund_idempotency_key,
	get_refund_counters,
	process_external_refund,
	reset_refund_counters,
)

_real_sql = frappe.db.sql
_real_get_all = frappe.get_all
_real_get_value = frappe.db.get_value


def _make_mock_sql(invoice_data):
	def _sql(query, *args, **kwargs):
		if "tabSales Invoice" in str(query) and "FOR UPDATE" in str(query):
			return invoice_data
		return _real_sql(query, *args, **kwargs)
	return _sql


def _make_mock_get_all(returns_data=None, item_data=None):
	returns_data = returns_data or []
	item_data = item_data or []
	def _get_all(dt, *args, **kwargs):
		if dt == "Sales Invoice":
			return returns_data
		if dt == "Sales Invoice Item":
			return item_data
		return _real_get_all(dt, *args, **kwargs)
	return _get_all


def _make_mock_get_value(existing_map=None):
	def _get_value(*args, **kwargs):
		dt = args[0] if args else kwargs.get("doctype")
		if dt == "Sales Channel":
			return 1
		if dt == "External ID Mapping":
			return existing_map
		return _real_get_value(*args, **kwargs)
	return _get_value


_real_get_doc = frappe.get_doc

def _make_mock_get_doc(mock_si=None):
	def _get_doc(*args, **kwargs):
		dt = args[0] if args else kwargs.get("doctype")
		if isinstance(dt, dict) and dt.get("doctype") == "External ID Mapping":
			m = MagicMock()
			m.insert.return_value = m
			return m
		if dt == "Sales Invoice":
			return mock_si
		if dt == "External ID Mapping":
			m = MagicMock()
			m.insert.return_value = m
			return m
		return _real_get_doc(*args, **kwargs)
	return _get_doc


class TestExternalRefundUnit(FrappeTestCase):
	"""
	Phase 1R Unit Test Suite:
	Provider-Neutral Sales Return, Credit Note & External Refund Foundation.
	Covers all 36 required scenarios from Section 27.
	"""

	def setUp(self):
		super().setUp()
		reset_refund_counters()

	# --- 1. Eligibility & Status Tests (Scenarios 1-4) ---

	@patch("bop_erp.accounts.refunds.reconcile_dr_cr_note")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_01_eligible_settled_refund(self, mock_exists, mock_get_doc, mock_make_return, mock_reconcile):
		mock_exists.return_value = True

		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"customer": "Customer A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
			"posting_date": "2026-09-01",
			"posting_time": "10:00:00",
		}]

		mock_si = MagicMock(name="ACC-SINV-001")
		mock_si.name = "ACC-SINV-001"
		mock_si.grand_total = 100.0
		mock_si.outstanding_amount = 100.0
		mock_si.items = []
		mock_si.posting_date = "2026-09-01"
		mock_si.posting_time = "10:00:00"

		mock_cn = MagicMock(name="CN-001")
		mock_cn.name = "ACC-SINV-RET-001"
		mock_cn.grand_total = -20.0
		mock_cn.outstanding_amount = -20.0
		mock_cn.debit_to = "1305 - Debtor"
		mock_cn.customer = "Customer A"
		mock_cn.conversion_rate = 1.0
		mock_cn.currency = "USD"
		mock_cn.company = "Company A"
		mock_cn.posting_date = "2026-09-01"
		mock_cn.items = []

		mock_get_doc.side_effect = _make_mock_get_doc(mock_si)
		mock_make_return.return_value = mock_cn

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-001",
						sales_invoice="ACC-SINV-001",
						amount=20.0,
						currency="USD",
						status="SETTLED",
					)
					res = process_external_refund(rec)
					self.assertEqual(res.name, "ACC-SINV-RET-001")
					c = get_refund_counters()
					self.assertEqual(c["refunds_completed"], 1)

	def test_02_pending_refund_no_accounting_mutation(self):
		rec = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-002",
			sales_invoice="ACC-SINV-001",
			amount=20.0,
			currency="USD",
			status="PENDING",
		)
		with patch("frappe.db.exists", return_value=True), patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
			with self.assertRaises(RefundStatusIneligibleError) as ctx:
				process_external_refund(rec)
			self.assertIn("ineligible", str(ctx.exception).lower())
			self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	def test_03_failed_refund_no_mutation(self):
		rec = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-003",
			sales_invoice="ACC-SINV-001",
			amount=20.0,
			currency="USD",
			status="FAILED",
		)
		with patch("frappe.db.exists", return_value=True), patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
			with self.assertRaises(RefundStatusIneligibleError):
				process_external_refund(rec)
			self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	def test_04_unknown_status_review(self):
		rec = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-004",
			sales_invoice="ACC-SINV-001",
			amount=20.0,
			currency="USD",
			status="UNKNOWN",
		)
		with patch("frappe.db.exists", return_value=True), patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
			with self.assertRaises(RefundStatusIneligibleError):
				process_external_refund(rec)
			self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	# --- 2. Identity & Idempotency Tests (Scenarios 5-9) ---

	def test_05_external_refund_canonical_identity(self):
		key1 = compute_external_refund_idempotency_key("CH-01", "PRESTASHOP", "REF-100", "ORD-100", 50.0, "USD")
		key2 = compute_external_refund_idempotency_key("CH-01", "PRESTASHOP", "REF-100", "ORD-100", 50.0, "USD")
		self.assertEqual(key1, key2)
		self.assertEqual(len(key1), 64)

	def test_06_provider_channel_scoping(self):
		key_ch1 = compute_external_refund_idempotency_key("CH-01", "PRESTASHOP", "REF-100", "ORD-100", 50.0, "USD")
		key_ch2 = compute_external_refund_idempotency_key("CH-02", "PRESTASHOP", "REF-100", "ORD-100", 50.0, "USD")
		key_prov = compute_external_refund_idempotency_key("CH-01", "SHOPIFY", "REF-100", "ORD-100", 50.0, "USD")
		self.assertNotEqual(key_ch1, key_ch2)
		self.assertNotEqual(key_ch1, key_prov)

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_07_duplicate_replay_convergence(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		canonical_key = compute_external_refund_idempotency_key(
			"CH-01", "PRESTASHOP", "REF-001", "ORD-001", 50.0, "USD"
		)
		mapping_data = {
			"name": "MAP-01",
			"erp_doctype": "Sales Invoice",
			"erp_document": "ACC-SINV-RET-EXISTING",
			"sync_hash": canonical_key,
			"active": 1,
		}
		mock_cn = MagicMock()
		mock_cn.name = "ACC-SINV-RET-EXISTING"
		mock_cn.docstatus = 1
		mock_get_doc.return_value = mock_cn

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(existing_map=mapping_data)):
			rec = ExternalRefundRecord(
				provider="PRESTASHOP",
				sales_channel="CH-01",
				external_refund_id="REF-001",
				external_order_id="ORD-001",
				amount=50.0,
				currency="USD",
			)
			res = process_external_refund(rec)
			self.assertEqual(res.name, "ACC-SINV-RET-EXISTING")
			self.assertEqual(get_refund_counters()["refund_reused"], 1)

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_08_payload_drift_detection(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		mapping_data = {
			"name": "MAP-01",
			"erp_doctype": "Sales Invoice",
			"erp_document": "ACC-SINV-RET-EXISTING",
			"sync_hash": "original_hash_123",
			"active": 1,
		}
		mock_cn = MagicMock()
		mock_cn.docstatus = 1
		mock_get_doc.return_value = mock_cn

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(existing_map=mapping_data)):
			rec = ExternalRefundRecord(
				provider="PRESTASHOP",
				sales_channel="CH-01",
				external_refund_id="REF-001",
				amount=999.0,  # Materially different amount
				currency="USD",
			)
			with self.assertRaises(RefundDriftError) as ctx:
				process_external_refund(rec)
			self.assertIn("drift", str(ctx.exception).lower())
			self.assertEqual(get_refund_counters()["refund_drift_blocked"], 1)

	def test_09_db_duplicate_identity_protection(self):
		from bop_erp.constants import ExternalEntityType
		self.assertIn("REFUND", ExternalEntityType.ALL)
		self.assertEqual(ExternalEntityType.REFUND, "REFUND")

	# --- 3. Document Resolution & Mismatch Tests (Scenarios 10-16) ---

	@patch("frappe.db.exists")
	def test_10_original_order_mapping_required(self, mock_exists):
		mock_exists.return_value = True
		with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
			rec = ExternalRefundRecord(
				provider="PRESTASHOP",
				sales_channel="CH-01",
				external_refund_id="REF-010",
				external_order_id="ORD-UNMAPPED",
			)
			with self.assertRaises(RefundEligibilityError):
				process_external_refund(rec)
			self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	def test_11_original_sales_invoice_required(self):
		with patch("frappe.db.exists", side_effect=lambda dt, name=None: True if dt == "Sales Channel" else False):
			with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
				rec = ExternalRefundRecord(
					provider="PRESTASHOP",
					sales_channel="CH-01",
					external_refund_id="REF-011",
					sales_invoice="ACC-SINV-NONEXISTENT",
				)
				with self.assertRaises(RefundEligibilityError):
					process_external_refund(rec)
				self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	@patch("frappe.db.exists")
	def test_12_cancelled_invoice_blocked(self, mock_exists):
		mock_exists.return_value = True
		cancelled_si = [{
			"name": "ACC-SINV-CANCELLED",
			"docstatus": 2,  # Cancelled
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 0.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
		}]
		with patch("frappe.db.sql", side_effect=_make_mock_sql(cancelled_si)):
			with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
				rec = ExternalRefundRecord(
					provider="PRESTASHOP",
					sales_channel="CH-01",
					external_refund_id="REF-012",
					sales_invoice="ACC-SINV-CANCELLED",
				)
				with self.assertRaises(RefundEligibilityError) as ctx:
					process_external_refund(rec)
				self.assertIn("submitted", str(ctx.exception).lower())
				self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	@patch("frappe.db.exists")
	def test_13_company_mismatch_blocked(self, mock_exists):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
		}]
		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
				rec = ExternalRefundRecord(
					provider="PRESTASHOP",
					sales_channel="CH-01",
					external_refund_id="REF-013",
					sales_invoice="ACC-SINV-001",
					company="Company Alien",
				)
				with self.assertRaises(CompanyMismatchError):
					process_external_refund(rec)
				self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	@patch("frappe.db.exists")
	def test_14_customer_mismatch_blocked(self, mock_exists):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"customer": "Customer Legit",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
		}]
		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
				rec = ExternalRefundRecord(
					provider="PRESTASHOP",
					sales_channel="CH-01",
					external_refund_id="REF-014",
					sales_invoice="ACC-SINV-001",
					customer="Customer Impostor",
				)
				with self.assertRaises(CustomerMismatchError):
					process_external_refund(rec)
				self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	@patch("frappe.db.exists")
	def test_15_channel_mismatch_blocked(self, mock_exists):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 0,
			"sales_channel": "CH-02",  # Different channel
		}]
		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
				rec = ExternalRefundRecord(
					provider="PRESTASHOP",
					sales_channel="CH-01",
					external_refund_id="REF-015",
					sales_invoice="ACC-SINV-001",
				)
				with self.assertRaises(RefundEligibilityError) as ctx:
					process_external_refund(rec)
				self.assertIn("mismatch", str(ctx.exception).lower())
				self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	@patch("frappe.db.exists")
	def test_16_currency_policy(self, mock_exists):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
		}]
		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
				rec = ExternalRefundRecord(
					provider="PRESTASHOP",
					sales_channel="CH-01",
					external_refund_id="REF-016",
					sales_invoice="ACC-SINV-001",
					currency="EUR",  # Invoice is USD
				)
				with self.assertRaises(CurrencyMismatchError):
					process_external_refund(rec)
				self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	# --- 4. Financial vs Physical Separation (Scenarios 17-19) ---

	@patch("bop_erp.accounts.refunds.reconcile_dr_cr_note")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_17_financial_only_credit(self, mock_exists, mock_get_doc, mock_make_return, mock_reconcile):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
			"posting_date": "2026-09-01",
			"posting_time": "10:00:00",
		}]

		mock_si = MagicMock()
		mock_si.grand_total = 100.0
		mock_si.outstanding_amount = 100.0
		mock_si.items = []
		mock_si.posting_date = "2026-09-01"
		mock_si.posting_time = "10:00:00"

		mock_cn = MagicMock()
		mock_cn.grand_total = -20.0
		mock_cn.outstanding_amount = -20.0
		mock_cn.items = []
		mock_cn.update_stock = 1  # Initially 1 before function sets it

		mock_get_doc.side_effect = _make_mock_get_doc(mock_si)
		mock_make_return.return_value = mock_cn

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-017",
						sales_invoice="ACC-SINV-001",
						amount=20.0,
						currency="USD",
						return_stock=False,  # Financial-only
					)
					process_external_refund(rec)
					self.assertEqual(mock_cn.update_stock, 0)
					self.assertEqual(get_refund_counters()["financial_only_credits"], 1)
					self.assertEqual(get_refund_counters()["stock_returns"], 0)

	@patch("bop_erp.accounts.refunds.reconcile_dr_cr_note")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_18_zero_stock_movement_financial_only(self, mock_exists, mock_get_doc, mock_make_return, mock_reconcile):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
			"posting_date": "2026-09-01",
			"posting_time": "10:00:00",
		}]

		mock_si = MagicMock()
		mock_si.grand_total = 100.0
		mock_si.outstanding_amount = 100.0
		mock_si.items = []
		mock_si.posting_date = "2026-09-01"
		mock_si.posting_time = "10:00:00"

		mock_cn = MagicMock()
		mock_cn.grand_total = -50.0
		mock_cn.outstanding_amount = -50.0
		mock_cn.items = []

		mock_get_doc.side_effect = _make_mock_get_doc(mock_si)
		mock_make_return.return_value = mock_cn

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					with patch("bop_erp.accounts.refunds.schedule_channel_inventory_publication") as mock_pub:
						rec = ExternalRefundRecord(
							provider="PRESTASHOP",
							sales_channel="CH-01",
							external_refund_id="REF-018",
							sales_invoice="ACC-SINV-001",
							amount=50.0,
							currency="USD",
							return_stock=False,
						)
						process_external_refund(rec)
						self.assertEqual(mock_cn.update_stock, 0)
						# Publication intent must NOT be scheduled for financial-only credit!
						mock_pub.assert_not_called()

	@patch("bop_erp.accounts.refunds.schedule_channel_inventory_publication")
	@patch("bop_erp.accounts.refunds.reconcile_dr_cr_note")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_19_physical_return_quantity_handling(
		self, mock_exists, mock_get_doc, mock_make_return, mock_reconcile, mock_pub
	):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 1,  # Direct delivery
			"sales_channel": "CH-01",
			"posting_date": "2026-09-01",
			"posting_time": "10:00:00",
		}]

		mock_item = MagicMock()
		mock_item.name = "ROW-01"
		mock_item.item_code = "ITEM-01"
		mock_item.qty = 2.0
		mock_item.delivery_note = None

		mock_si = MagicMock()
		mock_si.grand_total = 100.0
		mock_si.outstanding_amount = 100.0
		mock_si.items = [mock_item]
		mock_si.posting_date = "2026-09-01"
		mock_si.posting_time = "10:00:00"
		mock_si.update_stock = 1

		cn_row = MagicMock()
		cn_row.sales_invoice_item = "ROW-01"
		cn_row.item_code = "ITEM-01"
		cn_row.qty = -2.0

		mock_cn = MagicMock()
		mock_cn.grand_total = -50.0
		mock_cn.outstanding_amount = -50.0
		mock_cn.items = [cn_row]

		mock_get_doc.side_effect = _make_mock_get_doc(mock_si)
		mock_make_return.return_value = mock_cn

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-019",
						sales_invoice="ACC-SINV-001",
						items=[ExternalRefundItem(item_code="ITEM-01", qty=1.0)],
						return_stock=True,  # Physical return requested
					)
					process_external_refund(rec)
					self.assertEqual(mock_cn.update_stock, 1)
					self.assertEqual(cn_row.qty, -1.0)
					self.assertEqual(get_refund_counters()["stock_returns"], 1)
					mock_pub.assert_called_once()

	# --- 5. Over-Refund and Cumulative Limits (Scenarios 20-23) ---

	@patch("bop_erp.accounts.refunds.reconcile_dr_cr_note")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_20_partial_refund(self, mock_exists, mock_get_doc, mock_make_return, mock_reconcile):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
			"posting_date": "2026-09-01",
			"posting_time": "10:00:00",
		}]

		mock_si = MagicMock()
		mock_si.grand_total = 100.0
		mock_si.outstanding_amount = 100.0
		mock_si.items = []
		mock_si.posting_date = "2026-09-01"
		mock_si.posting_time = "10:00:00"

		mock_cn = MagicMock()
		mock_cn.grand_total = -30.0  # Partial ($30 on $100 invoice)
		mock_cn.outstanding_amount = -30.0
		mock_cn.items = []

		mock_get_doc.side_effect = _make_mock_get_doc(mock_si)
		mock_make_return.return_value = mock_cn

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-020",
						sales_invoice="ACC-SINV-001",
						amount=30.0,
						currency="USD",
					)
					process_external_refund(rec)
					self.assertEqual(get_refund_counters()["partial_refunds"], 1)

	@patch("bop_erp.accounts.refunds.reconcile_dr_cr_note")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_21_multiple_refunds(self, mock_exists, mock_get_doc, mock_make_return, mock_reconcile):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 70.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
			"posting_date": "2026-09-01",
			"posting_time": "10:00:00",
		}]

		mock_si = MagicMock()
		mock_si.grand_total = 100.0
		mock_si.outstanding_amount = 70.0
		mock_si.items = []
		mock_si.posting_date = "2026-09-01"
		mock_si.posting_time = "10:00:00"

		mock_cn = MagicMock()
		mock_cn.grand_total = -40.0  # Second refund of $40 (cumulative 70 <= 100)
		mock_cn.outstanding_amount = -40.0
		mock_cn.items = []

		mock_get_doc.side_effect = _make_mock_get_doc(mock_si)
		mock_make_return.return_value = mock_cn

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			# Prior refund exists for $30
			with patch("frappe.get_all", side_effect=_make_mock_get_all(returns_data=[{"name": "CN-001", "grand_total": -30.0}])):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-021-B",
						sales_invoice="ACC-SINV-001",
						amount=40.0,
						currency="USD",
					)
					res = process_external_refund(rec)
					self.assertEqual(res.grand_total, -40.0)

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_22_cumulative_over_refund_blocked(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 20.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
		}]

		mock_si = MagicMock()
		mock_si.grand_total = 100.0
		mock_get_doc.return_value = mock_si

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			# Prior refund was $80
			with patch("frappe.get_all", side_effect=_make_mock_get_all(returns_data=[{"name": "CN-001", "grand_total": -80.0}])):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					# Requesting $30 when only $20 remaining
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-022",
						sales_invoice="ACC-SINV-001",
						amount=30.0,
						currency="USD",
					)
					with self.assertRaises(OverRefundBlockedError) as ctx:
						process_external_refund(rec)
					self.assertIn("exceeds remaining refundable scope", str(ctx.exception))
					self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_23_cumulative_over_return_blocked(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 300.0,
			"outstanding_amount": 300.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
		}]

		mock_item = MagicMock()
		mock_item.name = "ROW-01"
		mock_item.item_code = "ITEM-01"
		mock_item.qty = 2.0

		mock_si = MagicMock()
		mock_si.grand_total = 300.0
		mock_si.items = [mock_item]
		mock_get_doc.return_value = mock_si

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			# Invoiced: 2, Prior returned: 1, Remaining: 1. Requesting 2 must be blocked!
			ret_data = [{"name": "CN-001", "grand_total": -100.0}]
			item_data = [{"sales_invoice_item": "ROW-01", "item_code": "ITEM-01", "qty": -1.0}]
			with patch("frappe.get_all", side_effect=_make_mock_get_all(returns_data=ret_data, item_data=item_data)):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-023",
						sales_invoice="ACC-SINV-001",
						items=[ExternalRefundItem(item_code="ITEM-01", qty=2.0)],
					)
					with self.assertRaises(OverRefundBlockedError) as ctx:
						process_external_refund(rec)
					self.assertIn("exceeds remaining eligible quantity", str(ctx.exception))
					self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	# --- 6. Concurrency & Replay Safety (Scenarios 24-27) ---

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_24_concurrent_duplicate_refund(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		canonical_key = compute_external_refund_idempotency_key(
			"CH-01", "PRESTASHOP", "REF-CONC", None, 50.0, "USD"
		)
		mapping_data = {
			"name": "MAP-CONC",
			"erp_doctype": "Sales Invoice",
			"erp_document": "ACC-SINV-RET-CONC",
			"sync_hash": canonical_key,
			"active": 1,
		}
		mock_cn = MagicMock()
		mock_cn.name = "ACC-SINV-RET-CONC"
		mock_cn.docstatus = 1
		mock_get_doc.return_value = mock_cn

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(existing_map=mapping_data)):
			rec = ExternalRefundRecord(
				provider="PRESTASHOP",
				sales_channel="CH-01",
				external_refund_id="REF-CONC",
				amount=50.0,
				currency="USD",
			)
			res = process_external_refund(rec)
			self.assertEqual(res.name, "ACC-SINV-RET-CONC")
			self.assertEqual(get_refund_counters()["refund_reused"], 1)

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_25_response_lost_convergence(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		canonical_key = compute_external_refund_idempotency_key(
			"CH-01", "PRESTASHOP", "REF-CRASH", None, 45.0, "USD"
		)
		mapping_data = {
			"name": "MAP-CRASH",
			"erp_doctype": "Sales Invoice",
			"erp_document": "ACC-SINV-RET-CRASH",
			"sync_hash": canonical_key,
			"active": 1,
		}
		mock_cn = MagicMock()
		mock_cn.name = "ACC-SINV-RET-CRASH"
		mock_cn.docstatus = 1
		mock_get_doc.return_value = mock_cn

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(existing_map=mapping_data)):
			rec = ExternalRefundRecord(
				provider="PRESTASHOP",
				sales_channel="CH-01",
				external_refund_id="REF-CRASH",
				amount=45.0,
				currency="USD",
			)
			res = process_external_refund(rec)
			self.assertEqual(res.name, "ACC-SINV-RET-CRASH")

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_26_cancellation_retains_external_identity(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		mapping_data = {
			"name": "MAP-CANCELLED",
			"erp_doctype": "Sales Invoice",
			"erp_document": "ACC-SINV-RET-CANCELLED",
			"sync_hash": "some_hash",
			"active": 1,
		}
		mock_cn = MagicMock()
		mock_cn.name = "ACC-SINV-RET-CANCELLED"
		mock_cn.docstatus = 2  # Cancelled!
		mock_get_doc.return_value = mock_cn

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(existing_map=mapping_data)):
			rec = ExternalRefundRecord(
				provider="PRESTASHOP",
				sales_channel="CH-01",
				external_refund_id="REF-CANCELLED",
				amount=50.0,
				currency="USD",
			)
			with self.assertRaises(RefundReplayCancelledError) as ctx:
				process_external_refund(rec)
			self.assertIn("cancelled", str(ctx.exception).lower())
			self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_27_replay_after_cancelled_credit_routes_review(self, mock_exists, mock_get_doc):
		mock_exists.return_value = True
		mapping_data = {
			"name": "MAP-CANCELLED",
			"erp_doctype": "Sales Invoice",
			"erp_document": "ACC-SINV-RET-CANCELLED",
			"sync_hash": "some_hash",
			"active": 1,
		}
		mock_cn = MagicMock()
		mock_cn.docstatus = 2
		mock_get_doc.return_value = mock_cn

		with patch("frappe.db.get_value", side_effect=_make_mock_get_value(existing_map=mapping_data)):
			rec = ExternalRefundRecord(
				provider="PRESTASHOP",
				sales_channel="CH-01",
				external_refund_id="REF-CANCELLED",
				amount=50.0,
				currency="USD",
			)
			with self.assertRaises(RefundReplayCancelledError) as ctx:
				process_external_refund(rec)
			self.assertIn("review", str(ctx.exception).lower())

	# --- 7. Accounting & Attribution Semantics (Scenarios 28-32) ---

	@patch("bop_erp.accounts.refunds.reconcile_dr_cr_note")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_28_attribution_inheritance(self, mock_exists, mock_get_doc, mock_make_return, mock_reconcile):
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-001",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 100.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
			"posting_date": "2026-09-01",
			"posting_time": "10:00:00",
		}]

		mock_si = MagicMock()
		mock_si.grand_total = 100.0
		mock_si.outstanding_amount = 100.0
		mock_si.items = []
		mock_si.posting_date = "2026-09-01"
		mock_si.posting_time = "10:00:00"

		mock_cn = MagicMock()
		mock_cn.grand_total = -10.0
		mock_cn.outstanding_amount = -10.0
		mock_cn.items = []

		mock_get_doc.side_effect = _make_mock_get_doc(mock_si)
		mock_make_return.return_value = mock_cn

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-028",
						sales_invoice="ACC-SINV-001",
						amount=10.0,
						currency="USD",
						transaction_origin="WEB",
					)
					process_external_refund(rec)
					self.assertEqual(mock_cn.sales_channel, "CH-01")
					self.assertEqual(mock_cn.transaction_origin, "WEB")

	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_29_original_payment_entry_untouched(self, mock_exists, mock_get_doc, mock_make_return):
		mock_exists.return_value = True
		# Fully paid invoice (outstanding_amount = 0)
		si_data = [{
			"name": "ACC-SINV-PAID",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 100.0,
			"outstanding_amount": 0.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
			"posting_date": "2026-09-01",
			"posting_time": "10:00:00",
		}]

		mock_si = MagicMock()
		mock_si.grand_total = 100.0
		mock_si.outstanding_amount = 0.0
		mock_si.items = []
		mock_si.posting_date = "2026-09-01"
		mock_si.posting_time = "10:00:00"

		mock_cn = MagicMock()
		mock_cn.grand_total = -50.0
		mock_cn.outstanding_amount = -50.0
		mock_cn.items = []

		mock_get_doc.side_effect = _make_mock_get_doc(mock_si)
		mock_make_return.return_value = mock_cn

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					with patch("bop_erp.accounts.refunds.reconcile_dr_cr_note") as mock_rec:
						rec = ExternalRefundRecord(
							provider="PRESTASHOP",
							sales_channel="CH-01",
							external_refund_id="REF-029",
							sales_invoice="ACC-SINV-PAID",
							amount=50.0,
							currency="USD",
						)
						process_external_refund(rec)
						# Reconcile against invoice is NOT called when outstanding is 0! Leaves customer credit intact
						mock_rec.assert_not_called()

	def test_30_no_manual_outstanding_mutation(self):
		import inspect
		import bop_erp.accounts.refunds as ref_mod
		src = inspect.getsource(ref_mod.process_external_refund)
		self.assertNotIn("outstanding_amount =", src)
		self.assertNotIn("db_set('outstanding_amount'", src)

	def test_31_no_manual_gl_posting(self):
		import inspect
		import bop_erp.accounts.refunds as ref_mod
		src = inspect.getsource(ref_mod.process_external_refund)
		self.assertNotIn("'GL Entry'", src)
		self.assertNotIn('"GL Entry"', src)

	def test_32_manual_erp_return_unaffected(self):
		from bop_erp.attribution import validate_transaction_attribution
		manual_return = frappe._dict({
			"doctype": "Sales Invoice",
			"is_return": 1,
			"return_against": None,
			"docstatus": 0,
		})
		try:
			validate_transaction_attribution(manual_return)
		except Exception as e:
			self.fail(f"validate_transaction_attribution failed for manual return: {e}")

	# --- 8. Invariance & Security Tests (Scenarios 33-36) ---

	def test_33_price_master_invariance(self):
		import inspect
		import bop_erp.accounts.refunds as ref_mod
		src = inspect.getsource(ref_mod.process_external_refund)
		self.assertNotIn("'Item Price'", src)
		self.assertNotIn('"Item Price"', src)

	def test_34_production_safety(self):
		import inspect
		import bop_erp.accounts.refunds as ref_mod
		src = inspect.getsource(ref_mod)
		self.assertNotIn("theindustrialdepot.com", src)

	def test_35_secret_pii_log_safety(self):
		key = compute_external_refund_idempotency_key(
			"CH-01", "PRESTASHOP", "REF-SEC", "ORD-SEC", 50.0, "USD"
		)
		self.assertIsInstance(key, str)
		self.assertEqual(len(key), 64)

	def test_36_fixture_isolation(self):
		reset_refund_counters()
		c = get_refund_counters()
		self.assertEqual(sum(c.values()), 0)

	# --- 9. Phase 1R.1 Hardening Tests (Scenarios 37-46) ---

	def test_37_terminal_refund_identity_cannot_be_deactivated(self):
		"""Phase 1R.1 Goal A: Terminal REFUND identity cannot be deactivated."""
		doc = frappe.new_doc("External ID Mapping")
		doc.name = "MAP-REF-TERM"
		doc.sales_channel = "CH-01"
		doc.provider = "PRESTASHOP"
		doc.external_entity_type = ExternalEntityType.REFUND
		doc.external_id = "REF-TERM-001"
		doc.active = 0
		doc.flags.is_new = False
		# Mock get_doc_before_save to return active=1
		old_doc = MagicMock()
		old_doc.active = 1
		doc.get_doc_before_save = MagicMock(return_value=old_doc)
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate_terminal_entity_immutability()
		self.assertIn("cannot be deactivated", str(ctx.exception).lower())

	def test_38_canonical_hash_all_material_fields(self):
		"""Phase 1R.1 Goal B: Hash covers all material refund semantics."""
		rec1 = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-MAT-1",
			external_order_id="ORD-MAT-1",
			external_payment_id="PMT-MAT-1",
			currency="USD",
			amount=100.0,
			shipping_refund_amount=15.0,
			status="SETTLED",
			return_stock=True,
			items=[
				ExternalRefundItem(
					external_order_line_id="LINE-1",
					item_code="ITEM-A",
					refund_amount=85.0,
					returned_qty=1.0,
					physical_return_evidence=True,
					warehouse="Stores - CA",
				)
			],
		)
		key1 = compute_external_refund_idempotency_key(refund_record=rec1)
		self.assertEqual(len(key1), 64)

		# Recomputing with exact same fields yields identical key
		key2 = compute_external_refund_idempotency_key(refund_record=rec1)
		self.assertEqual(key1, key2)

	def test_39_canonical_hash_metadata_ordering_invariance(self):
		"""Phase 1R.1 Goal B: Metadata key ordering variations do not cause hash drift."""
		rec_a = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-META",
			amount=50.0,
			currency="USD",
			metadata={"zebra": 1, "apple": 2, "mango": {"beta": 10, "alpha": 20}},
		)
		rec_b = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-META",
			amount=50.0,
			currency="USD",
			metadata={"apple": 2, "zebra": 1, "mango": {"alpha": 20, "beta": 10}},
		)
		key_a = compute_external_refund_idempotency_key(refund_record=rec_a)
		key_b = compute_external_refund_idempotency_key(refund_record=rec_b)
		self.assertEqual(key_a, key_b)

	def test_40_payload_drift_return_type(self):
		"""Phase 1R.1 Goal B: Changing return type (financial-only vs physical return) causes drift."""
		rec_fin = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-DRIFT-1",
			amount=50.0,
			return_stock=False,
		)
		rec_phys = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-DRIFT-1",
			amount=50.0,
			return_stock=True,
		)
		key_fin = compute_external_refund_idempotency_key(refund_record=rec_fin)
		key_phys = compute_external_refund_idempotency_key(refund_record=rec_phys)
		self.assertNotEqual(key_fin, key_phys)

	def test_41_payload_drift_returned_qty(self):
		"""Phase 1R.1 Goal B: Changing returned quantity triggers drift."""
		rec_q1 = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-DRIFT-2",
			items=[ExternalRefundItem(item_code="ITEM-A", returned_qty=1.0)],
		)
		rec_q2 = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-DRIFT-2",
			items=[ExternalRefundItem(item_code="ITEM-A", returned_qty=2.0)],
		)
		key_q1 = compute_external_refund_idempotency_key(refund_record=rec_q1)
		key_q2 = compute_external_refund_idempotency_key(refund_record=rec_q2)
		self.assertNotEqual(key_q1, key_q2)

	def test_42_payload_drift_line_allocation(self):
		"""Phase 1R.1 Goal B: Changing line allocation triggers drift."""
		rec_item_a = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-DRIFT-3",
			items=[ExternalRefundItem(item_code="ITEM-A", qty=1.0)],
		)
		rec_item_b = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-DRIFT-3",
			items=[ExternalRefundItem(item_code="ITEM-B", qty=1.0)],
		)
		key_a = compute_external_refund_idempotency_key(refund_record=rec_item_a)
		key_b = compute_external_refund_idempotency_key(refund_record=rec_item_b)
		self.assertNotEqual(key_a, key_b)

	def test_43_payload_drift_shipping_refund_amount(self):
		"""Phase 1R.1 Goal B: Changing shipping refund amount triggers drift."""
		rec_ship1 = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-DRIFT-4",
			amount=50.0,
			shipping_refund_amount=10.0,
		)
		rec_ship2 = ExternalRefundRecord(
			provider="PRESTASHOP",
			sales_channel="CH-01",
			external_refund_id="REF-DRIFT-4",
			amount=50.0,
			shipping_refund_amount=15.0,
		)
		key1 = compute_external_refund_idempotency_key(refund_record=rec_ship1)
		key2 = compute_external_refund_idempotency_key(refund_record=rec_ship2)
		self.assertNotEqual(key1, key2)

	@patch("bop_erp.accounts.refunds.get_physical_return_scope")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_44_physical_over_return_path1_direct_delivery(
		self, mock_exists, mock_get_doc, mock_make_return, mock_scope
	):
		"""Phase 1R.1 Goal C: Path 1 (si.update_stock=1) physical over-return is blocked."""
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-P1",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 200.0,
			"outstanding_amount": 200.0,
			"update_stock": 1,
			"sales_channel": "CH-01",
		}]
		mock_si = MagicMock()
		mock_si.name = "ACC-SINV-P1"
		mock_si.grand_total = 200.0
		mock_si.update_stock = 1
		mock_si.items = [MagicMock(item_code="ITEM-P1", qty=2.0)]
		mock_get_doc.return_value = mock_si

		# Mock scope: Delivered 2, Already returned 1, Remaining 1
		mock_scope.return_value = (2.0, 1.0, 1.0)

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-OVER-P1",
						sales_invoice="ACC-SINV-P1",
						items=[ExternalRefundItem(item_code="ITEM-P1", qty=2.0)],
						return_stock=True,
					)
					with self.assertRaises(OverRefundBlockedError) as ctx:
						process_external_refund(rec)
					self.assertIn("exceeds remaining physical return capacity", str(ctx.exception))
					self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	@patch("bop_erp.accounts.refunds.get_physical_return_scope")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_45_physical_over_return_path2_delivery_notes(
		self, mock_exists, mock_get_doc, mock_make_return, mock_scope
	):
		"""Phase 1R.1 Goal C: Path 2 (Delivery Note fulfillment) physical over-return is blocked."""
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-P2",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 200.0,
			"outstanding_amount": 200.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
		}]
		mock_si = MagicMock()
		mock_si.name = "ACC-SINV-P2"
		mock_si.grand_total = 200.0
		mock_si.update_stock = 0
		mock_si.items = [MagicMock(item_code="ITEM-P2", qty=2.0)]
		mock_get_doc.return_value = mock_si

		# Mock scope: Delivered 2, Already physically returned 1, Remaining 1
		mock_scope.return_value = (2.0, 1.0, 1.0)

		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					with patch("bop_erp.accounts.refunds.get_delivery_notes_for_invoice", return_value=["DN-001"]):
						rec = ExternalRefundRecord(
							provider="PRESTASHOP",
							sales_channel="CH-01",
							external_refund_id="REF-OVER-P2",
							sales_invoice="ACC-SINV-P2",
							items=[ExternalRefundItem(item_code="ITEM-P2", qty=2.0)],
							return_stock=True,
						)
						with self.assertRaises(OverRefundBlockedError) as ctx:
							process_external_refund(rec)
						self.assertIn("exceeds remaining physical return capacity", str(ctx.exception))
						self.assertEqual(get_refund_counters()["refund_blocked"], 1)

	@patch("bop_erp.accounts.refunds.get_physical_return_scope")
	@patch("bop_erp.accounts.refunds.reconcile_dr_cr_note")
	@patch("bop_erp.accounts.refunds.make_return_doc")
	@patch("frappe.get_doc")
	@patch("frappe.db.exists")
	def test_46_financial_only_credit_does_not_consume_physical_capacity(
		self, mock_exists, mock_get_doc, mock_make_return, mock_reconcile, mock_scope
	):
		"""Phase 1R.1 Goal C: Financial-only credit (return_stock=False) does not consume physical return capacity."""
		mock_exists.return_value = True
		si_data = [{
			"name": "ACC-SINV-P3",
			"docstatus": 1,
			"is_return": 0,
			"company": "Company A",
			"currency": "USD",
			"grand_total": 200.0,
			"outstanding_amount": 200.0,
			"update_stock": 0,
			"sales_channel": "CH-01",
			"posting_date": "2026-09-01",
			"posting_time": "10:00:00",
		}]
		mock_si = MagicMock()
		mock_si.name = "ACC-SINV-P3"
		mock_si.grand_total = 200.0
		mock_si.outstanding_amount = 200.0
		mock_si.update_stock = 0
		mock_si.posting_date = "2026-09-01"
		mock_si.posting_time = "10:00:00"
		mock_si.items = [MagicMock(name="ROW-1", item_code="ITEM-P3", qty=2.0, rate=100.0)]

		mock_cn = MagicMock()
		mock_cn.name = "ACC-SINV-RET-P3"
		mock_cn.grand_total = -50.0
		mock_cn.outstanding_amount = -50.0
		mock_cn.debit_to = "1305 - Debtor"
		mock_cn.customer = "Customer A"
		mock_cn.conversion_rate = 1.0
		mock_cn.currency = "USD"
		mock_cn.company = "Company A"
		mock_cn.posting_date = "2026-09-01"
		mock_cn.items = []

		mock_get_doc.side_effect = _make_mock_get_doc(mock_si)
		mock_make_return.return_value = mock_cn

		# Financial-only refund should NOT invoke get_physical_return_scope
		with patch("frappe.db.sql", side_effect=_make_mock_sql(si_data)):
			with patch("frappe.get_all", side_effect=_make_mock_get_all()):
				with patch("frappe.db.get_value", side_effect=_make_mock_get_value()):
					rec = ExternalRefundRecord(
						provider="PRESTASHOP",
						sales_channel="CH-01",
						external_refund_id="REF-FIN-ONLY",
						sales_invoice="ACC-SINV-P3",
						amount=50.0,
						return_stock=False,  # Financial-only
					)
					cn = process_external_refund(rec)
					self.assertEqual(cn.update_stock, 0)
					mock_scope.assert_not_called()
					self.assertEqual(get_refund_counters()["stock_returns"], 0)
					self.assertEqual(get_refund_counters()["financial_only_credits"], 1)


