# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import MagicMock, patch
import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
	IntegrationReadinessStatus,
	TransactionOrigin,
)
from bop_erp.accounts import (
	CompanyMismatchError,
	DuplicatePaymentError,
	ExternalPaymentRecord,
	ExternalPaymentStatus,
	OverpaymentBlockedError,
	PaymentAccountMismatchError,
	PaymentEligibilityError,
	PaymentMappingDriftError,
	PaymentReconciliationError,
	assert_payment_reconciliation_eligibility,
	cancel_payment_entry,
	compute_external_payment_idempotency_key,
	get_payment_counters,
	plan_invoice_allocations,
	reconcile_external_payment,
	reset_payment_counters,
	resolve_clearing_account_for_payment,
	submit_payment_entry,
)
from bop_erp.orders.reconciliation import audit_sales_order_cancellation_safety


class MockDocument(dict):
	"""Helper mock document supporting attribute access and reload."""
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.__dict__ = self
		if "flags" not in kwargs:
			self.__dict__["flags"] = frappe._dict()

	def reload(self):
		return self

	def get(self, key, default=None):
		return super().get(key, default)

	def set(self, key, value):
		self[key] = value
		self.__dict__[key] = value

	def append(self, key, value):
		if key not in self or not isinstance(self[key], list):
			self[key] = []
			self.__dict__[key] = self[key]
		self[key].append(value)




class TestExternalPaymentUnit(FrappeTestCase):
	"""
	Phase 1Q Unit Test Suite:
	Payment Entry / External Payment Reconciliation Foundation.
	Validates 55 required unit test scenarios covering:
	- Eligibility validation (positive amount, status, channel, company, currency)
	- Allocation planning (oldest invoice first, partial, multi-invoice, overpayment blocked)
	- Idempotency and convergence
	- Mode of payment and clearing account resolution
	- Attribution preservation
	- Cancellation safety
	"""

	def setUp(self):
		super().setUp()
		reset_payment_counters()

		# Patch frappe.db.get_value for unit test isolation
		self._orig_get_value = frappe.db.get_value
		self._orig_exists = frappe.db.exists
		self._orig_sql = frappe.db.sql

		def _mock_get_value(doctype, filters=None, fieldname=None, as_dict=False, **kwargs):
			if doctype == "Sales Channel":
				ch = filters if isinstance(filters, str) else (filters.get("channel_id") or filters.get("name") if isinstance(filters, dict) else "TEST-CHAN")
				if ch == "INACTIVE-CHAN":
					res = {"name": ch, "company": "_Test Company", "active": 0}
				elif ch == "NONEXISTENT-CHAN":
					return None
				else:
					res = {"name": ch, "company": "_Test Company", "active": 1}
				return frappe._dict(res) if as_dict else res.get(fieldname, "TEST")

			if doctype == "External ID Mapping":
				if isinstance(filters, dict):
					if filters.get("external_id") == "DRIFT-ORDER":
						return None
					if filters.get("external_id") == "DRIFT-PROV":
						res = {"name": "MAP-DRIFT", "provider": "OTHER_PROV", "erp_doctype": "Sales Order", "erp_document": "SO-001"}
						return frappe._dict(res) if as_dict else "MAP-DRIFT"
				res = {"name": "MAP-PAY-01", "provider": "prestashop", "erp_doctype": "Sales Order", "erp_document": "SO-001"}
				return frappe._dict(res) if as_dict else "MAP-PAY-01"

			if doctype == "Mode of Payment":
				return "Credit Card"

			if doctype == "Mode of Payment Account":
				return "Bank - TC"

			if doctype == "Company":
				if fieldname == "default_bank_account":
					return "Bank - TC"
				if fieldname == "default_cash_account":
					return "Cash - TC"
				return "_Test Company"

			if doctype == "Account":
				acc_name = filters if isinstance(filters, str) else (filters.get("name") if isinstance(filters, dict) else "Bank - TC")
				if acc_name == "GROUP-ACC":
					res = {"name": acc_name, "company": "_Test Company", "account_type": "Bank", "is_group": 1}
				elif acc_name == "OTHER-COMP-ACC":
					res = {"name": acc_name, "company": "Other Company", "account_type": "Bank", "is_group": 0}
				elif acc_name == "EXPENSE-ACC":
					res = {"name": acc_name, "company": "_Test Company", "account_type": "Expense Account", "is_group": 0}
				else:
					res = {"name": acc_name, "company": "_Test Company", "account_type": "Bank", "is_group": 0, "account_currency": "USD"}
				return frappe._dict(res) if as_dict else res.get(fieldname, "USD")

			return self._orig_get_value(doctype, filters=filters, fieldname=fieldname, as_dict=as_dict, **kwargs)

		def _mock_exists(doctype, name=None):
			if doctype == "Mode of Payment":
				return name != "UNKNOWN_METHOD"
			if doctype == "Account":
				return name != "NONEXISTENT-ACC"
			if doctype == "Sales Channel":
				return name != "NONEXISTENT-CHAN"
			if doctype == "Sales Order":
				return True
			if doctype == "Sales Invoice":
				return True
			if doctype == "Payment Entry":
				return True
			return self._orig_exists(doctype, name)

		def _mock_sql(query, values=None, *args, **kwargs):
			if isinstance(query, str):
				if "tabSales Invoice" in query and "FOR UPDATE" in query:
					return []
				if "tabSales Order" in query and "FOR UPDATE" in query:
					return []
			if values is not None:
				return self._orig_sql(query, values, *args, **kwargs)
			return self._orig_sql(query, *args, **kwargs)


		self._orig_get_doc = frappe.get_doc

		def _mock_get_doc(dt, name=None, **kwargs):
			if dt == "Sales Order":
				return MockDocument(name=name or "SO-001", doctype="Sales Order", company="_Test Company")
			if dt == "External ID Mapping" or (isinstance(dt, dict) and dt.get("doctype") == "External ID Mapping"):
				return MockDocument(name="MAP-01", insert=MagicMock())
			return self._orig_get_doc(dt, name, **kwargs)

		frappe.db.get_value = _mock_get_value
		frappe.db.exists = _mock_exists
		frappe.db.sql = _mock_sql
		frappe.get_doc = _mock_get_doc

	def tearDown(self):

		frappe.db.get_value = self._orig_get_value
		frappe.db.exists = self._orig_exists
		frappe.db.sql = self._orig_sql
		frappe.get_doc = self._orig_get_doc
		super().tearDown()


	def _make_mock_invoice(
		self,
		name="ACC-SINV-2026-00001",
		docstatus=1,
		status="Unpaid",
		company="_Test Company",
		customer="_Test Customer",
		grand_total=100.0,
		outstanding_amount=100.0,
		currency="USD",
		posting_date="2026-09-10",
		creation="2026-09-10 10:00:00",
		sales_channel="TEST-CHAN",
		transaction_origin=TransactionOrigin.WEB,
		external_order_id="EXT-ORD-01",
		debit_to="1310 - Debtors - TC",
	):
		inv = MockDocument(
			name=name,
			doctype="Sales Invoice",
			docstatus=docstatus,
			status=status,
			company=company,
			customer=customer,
			grand_total=grand_total,
			outstanding_amount=outstanding_amount,
			currency=currency,
			posting_date=posting_date,
			creation=creation,
			sales_channel=sales_channel,
			transaction_origin=transaction_origin,
			external_order_id=external_order_id,
			debit_to=debit_to,
		)
		return inv

	def _make_payment_record(
		self,
		amount=100.0,
		currency="USD",
		payment_status=ExternalPaymentStatus.SETTLED,
		sales_channel="TEST-CHAN",
		external_payment_id="PAY-001",
		external_order_id="EXT-ORD-01",
		payment_method="Credit Card",
		provider="prestashop",
	):
		return ExternalPaymentRecord(
			provider=provider,
			sales_channel=sales_channel,
			external_payment_id=external_payment_id,
			amount=amount,
			currency=currency,
			payment_method=payment_method,
			payment_status=payment_status,
			external_order_id=external_order_id,
		)

	# =========================================================================
	# GROUP 1: Eligibility & Validation Scenarios (1 - 15)
	# =========================================================================
	def test_01_positive_amount_valid(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(amount=100.0)
		invs, so = assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])
		self.assertEqual(len(invs), 1)

	def test_02_zero_amount_blocked(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(amount=0.0)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])
		self.assertEqual(get_payment_counters()["payments_blocked"], 1)

	def test_03_negative_amount_blocked(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(amount=-50.0)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_04_status_settled_eligible(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.SETTLED)
		invs, so = assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])
		self.assertEqual(len(invs), 1)

	def test_05_status_captured_eligible(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.CAPTURED)
		invs, so = assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])
		self.assertEqual(len(invs), 1)

	def test_06_status_completed_eligible(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.COMPLETED)
		invs, so = assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])
		self.assertEqual(len(invs), 1)

	def test_07_status_pending_ineligible(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.PENDING)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_08_status_authorized_ineligible(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.AUTHORIZED)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_09_status_failed_ineligible(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.FAILED)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_10_status_refunded_ineligible(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.REFUNDED)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_11_status_chargeback_ineligible(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.CHARGEBACK)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_12_nonexistent_sales_channel_blocked(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(sales_channel="NONEXISTENT-CHAN")
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_13_inactive_sales_channel_blocked(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(sales_channel="INACTIVE-CHAN")
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_14_unsubmitted_draft_invoice_blocked(self):
		inv = self._make_mock_invoice(docstatus=0)
		rec = self._make_payment_record()
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_15_cancelled_invoice_blocked(self):
		inv = self._make_mock_invoice(docstatus=2, status="Cancelled")
		rec = self._make_payment_record()
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# =========================================================================
	# GROUP 2: Financial Boundary & Mapping Scenarios (16 - 30)
	# =========================================================================
	def test_16_fully_paid_zero_outstanding_invoice_blocked(self):
		inv = self._make_mock_invoice(outstanding_amount=0.0)
		rec = self._make_payment_record()
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_17_cross_company_invoice_mismatch_blocked(self):
		inv = self._make_mock_invoice(company="Other Company")
		rec = self._make_payment_record()
		with self.assertRaises(CompanyMismatchError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_18_currency_mismatch_blocked(self):
		inv = self._make_mock_invoice(currency="EUR")
		rec = self._make_payment_record(currency="USD")
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_19_missing_canonical_order_mapping_drift_blocked(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id="DRIFT-ORDER")
		with self.assertRaises(PaymentMappingDriftError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_20_order_mapping_provider_mismatch_drift_blocked(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id="DRIFT-PROV", provider="prestashop")
		with self.assertRaises(PaymentMappingDriftError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	def test_21_mode_of_payment_resolution_credit_card(self):
		mop, acc = resolve_clearing_account_for_payment("_Test Company", "TEST-CHAN", "Credit Card")
		self.assertEqual(mop, "Credit Card")
		self.assertEqual(acc, "Bank - TC")

	def test_22_mode_of_payment_fallback_resolution(self):
		mop, acc = resolve_clearing_account_for_payment("_Test Company", "TEST-CHAN", "UNKNOWN_METHOD")
		self.assertIn(mop, ["Credit Card", "Wire Transfer", "Bank Draft", "Cash", "Cheque"])

	def test_23_clearing_account_group_account_blocked(self):
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({"name": "GROUP-ACC", "company": "_Test Company", "account_type": "Bank", "is_group": 1})):
			with self.assertRaises(PaymentAccountMismatchError):
				resolve_clearing_account_for_payment("_Test Company", "TEST-CHAN", "Credit Card")

	def test_24_clearing_account_cross_company_blocked(self):
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({"name": "OTHER-COMP-ACC", "company": "Other Company", "account_type": "Bank", "is_group": 0})):
			with self.assertRaises(CompanyMismatchError):
				resolve_clearing_account_for_payment("_Test Company", "TEST-CHAN", "Credit Card")

	def test_25_clearing_account_invalid_account_type_blocked(self):
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({"name": "EXPENSE-ACC", "company": "_Test Company", "account_type": "Expense Account", "is_group": 0})):
			with self.assertRaises(PaymentAccountMismatchError):
				resolve_clearing_account_for_payment("_Test Company", "TEST-CHAN", "Credit Card")

	def test_26_idempotency_key_deterministic(self):
		k1 = compute_external_payment_idempotency_key("prestashop", "TEST-CHAN", "PAY-001")
		k2 = compute_external_payment_idempotency_key("PRESTASHOP", "TEST-CHAN", "PAY-001")
		self.assertEqual(k1, k2)

	def test_27_idempotency_key_sensitive_to_channel(self):
		k1 = compute_external_payment_idempotency_key("prestashop", "CHAN-A", "PAY-001")
		k2 = compute_external_payment_idempotency_key("prestashop", "CHAN-B", "PAY-001")
		self.assertNotEqual(k1, k2)

	def test_28_idempotency_key_sensitive_to_payment_id(self):
		k1 = compute_external_payment_idempotency_key("prestashop", "TEST-CHAN", "PAY-001")
		k2 = compute_external_payment_idempotency_key("prestashop", "TEST-CHAN", "PAY-002")
		self.assertNotEqual(k1, k2)

	def test_29_idempotency_key_sensitive_to_provider(self):
		k1 = compute_external_payment_idempotency_key("prestashop", "TEST-CHAN", "PAY-001")
		k2 = compute_external_payment_idempotency_key("marketplace", "TEST-CHAN", "PAY-001")
		self.assertNotEqual(k1, k2)

	def test_30_no_invoices_found_blocked(self):
		rec = self._make_payment_record(external_order_id=None)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[])

	# =========================================================================
	# GROUP 3: Allocation Planning Scenarios (31 - 42)
	# =========================================================================
	def test_31_exact_single_invoice_allocation(self):
		inv = self._make_mock_invoice(outstanding_amount=100.0)
		allocs = plan_invoice_allocations(100.0, [inv])
		self.assertEqual(len(allocs), 1)
		self.assertEqual(allocs[0]["allocated_amount"], 100.0)

	def test_32_partial_single_invoice_allocation(self):
		inv = self._make_mock_invoice(outstanding_amount=100.0)
		allocs = plan_invoice_allocations(40.0, [inv])
		self.assertEqual(len(allocs), 1)
		self.assertEqual(allocs[0]["allocated_amount"], 40.0)

	def test_33_overpayment_beyond_single_invoice_blocked(self):
		inv = self._make_mock_invoice(outstanding_amount=100.0)
		with self.assertRaises(OverpaymentBlockedError):
			plan_invoice_allocations(120.0, [inv])
		self.assertEqual(get_payment_counters()["overpayment_blocked"], 1)

	def test_34_two_invoices_exact_allocation(self):
		inv1 = self._make_mock_invoice(name="SI-01", outstanding_amount=50.0, posting_date="2026-09-01")
		inv2 = self._make_mock_invoice(name="SI-02", outstanding_amount=50.0, posting_date="2026-09-02")
		allocs = plan_invoice_allocations(100.0, [inv1, inv2])
		self.assertEqual(len(allocs), 2)
		self.assertEqual(allocs[0]["allocated_amount"], 50.0)
		self.assertEqual(allocs[1]["allocated_amount"], 50.0)

	def test_35_two_invoices_oldest_first_partial_second(self):
		inv1 = self._make_mock_invoice(name="SI-01", outstanding_amount=60.0, posting_date="2026-09-01")
		inv2 = self._make_mock_invoice(name="SI-02", outstanding_amount=60.0, posting_date="2026-09-02")
		allocs = plan_invoice_allocations(80.0, [inv2, inv1])  # passed unsorted
		self.assertEqual(len(allocs), 2)
		self.assertEqual(allocs[0]["sales_invoice"], "SI-01")
		self.assertEqual(allocs[0]["allocated_amount"], 60.0)
		self.assertEqual(allocs[1]["sales_invoice"], "SI-02")
		self.assertEqual(allocs[1]["allocated_amount"], 20.0)

	def test_36_multi_invoice_allocation_covers_first_only(self):
		inv1 = self._make_mock_invoice(name="SI-01", outstanding_amount=100.0, posting_date="2026-09-01")
		inv2 = self._make_mock_invoice(name="SI-02", outstanding_amount=50.0, posting_date="2026-09-02")
		allocs = plan_invoice_allocations(50.0, [inv1, inv2])
		self.assertEqual(len(allocs), 1)
		self.assertEqual(allocs[0]["sales_invoice"], "SI-01")
		self.assertEqual(allocs[0]["allocated_amount"], 50.0)

	def test_37_overpayment_beyond_multi_invoices_blocked(self):
		inv1 = self._make_mock_invoice(name="SI-01", outstanding_amount=50.0)
		inv2 = self._make_mock_invoice(name="SI-02", outstanding_amount=30.0)
		with self.assertRaises(OverpaymentBlockedError):
			plan_invoice_allocations(100.0, [inv1, inv2])

	def test_38_fractional_cent_allocation_precision(self):
		inv = self._make_mock_invoice(outstanding_amount=10.55)
		allocs = plan_invoice_allocations(10.55, [inv])
		self.assertEqual(allocs[0]["allocated_amount"], 10.55)

	def test_39_fractional_cent_partial_allocation(self):
		inv = self._make_mock_invoice(outstanding_amount=10.55)
		allocs = plan_invoice_allocations(5.23, [inv])
		self.assertEqual(allocs[0]["allocated_amount"], 5.23)

	def test_40_multiple_same_date_sorted_by_creation(self):
		inv1 = self._make_mock_invoice(name="SI-01", outstanding_amount=40.0, posting_date="2026-09-10", creation="2026-09-10 09:00:00")
		inv2 = self._make_mock_invoice(name="SI-02", outstanding_amount=40.0, posting_date="2026-09-10", creation="2026-09-10 10:00:00")
		allocs = plan_invoice_allocations(50.0, [inv2, inv1])
		self.assertEqual(allocs[0]["sales_invoice"], "SI-01")
		self.assertEqual(allocs[0]["allocated_amount"], 40.0)
		self.assertEqual(allocs[1]["sales_invoice"], "SI-02")
		self.assertEqual(allocs[1]["allocated_amount"], 10.0)

	def test_41_allocation_ignores_zero_outstanding_in_list(self):
		inv1 = self._make_mock_invoice(name="SI-01", outstanding_amount=0.0, posting_date="2026-09-01")
		inv2 = self._make_mock_invoice(name="SI-02", outstanding_amount=50.0, posting_date="2026-09-02")
		allocs = plan_invoice_allocations(30.0, [inv1, inv2])
		self.assertEqual(len(allocs), 1)
		self.assertEqual(allocs[0]["sales_invoice"], "SI-02")
		self.assertEqual(allocs[0]["allocated_amount"], 30.0)

	def test_42_allocation_empty_remaining_terminates_cleanly(self):
		inv1 = self._make_mock_invoice(name="SI-01", outstanding_amount=50.0, posting_date="2026-09-01")
		inv2 = self._make_mock_invoice(name="SI-02", outstanding_amount=50.0, posting_date="2026-09-02")
		inv3 = self._make_mock_invoice(name="SI-03", outstanding_amount=50.0, posting_date="2026-09-03")
		allocs = plan_invoice_allocations(50.0, [inv1, inv2, inv3])
		self.assertEqual(len(allocs), 1)
		self.assertEqual(allocs[0]["sales_invoice"], "SI-01")

	# =========================================================================
	# GROUP 4: Lifecycle, Replay & Attribution Scenarios (43 - 55)
	# =========================================================================
	def test_43_reconciliation_creates_payment_entry_draft(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id=None)

		orig_get_doc = frappe.get_doc
		def _mock_get_doc(dt, name=None):
			if dt == "External ID Mapping" or (isinstance(dt, dict) and dt.get("doctype") == "External ID Mapping"):
				m = MockDocument(name="MAP-01", insert=MagicMock())
				return m
			return orig_get_doc(dt, name)

		with patch("frappe.new_doc") as mock_new, patch("frappe.get_doc", side_effect=_mock_get_doc):
			mock_pe = MockDocument(doctype="Payment Entry", name="PE-001", docstatus=0, references=[])
			mock_pe.setup_party_account_field = MagicMock()
			mock_pe.set_missing_values = MagicMock()
			mock_pe.set_missing_ref_details = MagicMock()
			mock_pe.insert = MagicMock()
			mock_pe.append = lambda k, v: mock_pe.references.append(v)
			mock_new.return_value = mock_pe

			res = reconcile_external_payment(rec, sales_invoices=[inv], posting_date="2026-09-10", submit=False)
			self.assertEqual(res.name, "PE-001")
			self.assertEqual(get_payment_counters()["payments_created"], 1)

	def test_44_reconciliation_submits_payment_entry_when_requested(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id=None)

		orig_get_doc = frappe.get_doc
		def _mock_get_doc(dt, name=None):
			if dt == "External ID Mapping" or (isinstance(dt, dict) and dt.get("doctype") == "External ID Mapping"):
				m = MockDocument(name="MAP-01", insert=MagicMock())
				return m
			return orig_get_doc(dt, name)

		with patch("frappe.new_doc") as mock_new, patch("frappe.get_doc", side_effect=_mock_get_doc):
			mock_pe = MockDocument(doctype="Payment Entry", name="PE-001", docstatus=0, paid_amount=100.0, references=[])
			mock_pe.setup_party_account_field = MagicMock()
			mock_pe.set_missing_values = MagicMock()
			mock_pe.set_missing_ref_details = MagicMock()
			mock_pe.insert = MagicMock()
			mock_pe.submit = MagicMock()
			mock_pe.append = lambda k, v: mock_pe.references.append(v)
			mock_new.return_value = mock_pe

			res = reconcile_external_payment(rec, sales_invoices=[inv], posting_date="2026-09-10", submit=True)
			self.assertEqual(get_payment_counters()["payments_submitted"], 1)


	def test_45_duplicate_external_payment_converges_to_existing(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record()

		existing_pe = MockDocument(name="PE-EXISTING", docstatus=1)
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({"name": "MAP-01", "erp_doctype": "Payment Entry", "erp_document": "PE-EXISTING"})):
			with patch("frappe.get_doc", return_value=existing_pe):
				res = reconcile_external_payment(rec, sales_invoices=[inv])
				self.assertEqual(res.name, "PE-EXISTING")
				self.assertEqual(get_payment_counters()["payments_reused"], 1)
				self.assertEqual(get_payment_counters()["concurrent_replay"], 1)

	def test_46_duplicate_draft_payment_submits_on_demand(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record()

		existing_draft_pe = MockDocument(name="PE-DRAFT", docstatus=0, paid_amount=100.0, submit=MagicMock())
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({"name": "MAP-01", "erp_doctype": "Payment Entry", "erp_document": "PE-DRAFT"})):
			with patch("frappe.get_doc", return_value=existing_draft_pe):
				res = reconcile_external_payment(rec, sales_invoices=[inv], submit=True)
				self.assertEqual(get_payment_counters()["payments_submitted"], 1)

	def test_47_cancel_payment_entry_natively_cancels(self):
		mock_pe = MockDocument(name="PE-SUBMITTED", docstatus=1, cancel=MagicMock())
		with patch("frappe.get_doc", return_value=mock_pe), patch.object(frappe.db, "set_value") as mock_set:
			cancel_payment_entry(mock_pe)
			mock_pe.cancel.assert_called_once()
			self.assertEqual(get_payment_counters()["payments_cancelled"], 1)

	def test_48_cancel_draft_payment_entry_deletes(self):
		mock_pe = MockDocument(name="PE-DRAFT", docstatus=0, delete=MagicMock())
		with patch("frappe.get_doc", return_value=mock_pe), patch.object(frappe.db, "set_value") as mock_set:
			cancel_payment_entry(mock_pe)
			mock_pe.delete.assert_called_once()

	def test_49_cancel_already_cancelled_payment_entry_no_op(self):
		mock_pe = MockDocument(name="PE-CANC", docstatus=2)
		with patch("frappe.get_doc", return_value=mock_pe):
			res = cancel_payment_entry(mock_pe)
			self.assertEqual(res.docstatus, 2)

	def test_50_submit_cancelled_payment_entry_blocked(self):
		mock_pe = MockDocument(name="PE-CANC", docstatus=2)
		with patch("frappe.get_doc", return_value=mock_pe):
			with self.assertRaises(PaymentReconciliationError):
				submit_payment_entry(mock_pe)

	def test_51_commercial_attribution_preserved_on_payment_entry(self):
		inv = self._make_mock_invoice(sales_channel="CHAN-SPECIFIC", transaction_origin=TransactionOrigin.MARKETPLACE)
		rec = self._make_payment_record(sales_channel="CHAN-SPECIFIC", external_order_id=None)

		orig_get_doc = frappe.get_doc
		def _mock_get_doc(dt, name=None):
			if dt == "External ID Mapping" or (isinstance(dt, dict) and dt.get("doctype") == "External ID Mapping"):
				return MockDocument(name="MAP-01", insert=MagicMock())
			return orig_get_doc(dt, name)

		with patch("frappe.new_doc") as mock_new, patch("frappe.get_doc", side_effect=_mock_get_doc):
			mock_pe = MockDocument(doctype="Payment Entry", name="PE-ATTR", docstatus=0, references=[])
			mock_pe.setup_party_account_field = MagicMock()
			mock_pe.set_missing_values = MagicMock()
			mock_pe.set_missing_ref_details = MagicMock()
			mock_pe.insert = MagicMock()
			mock_pe.append = lambda k, v: mock_pe.references.append(v)
			mock_new.return_value = mock_pe

			pe = reconcile_external_payment(rec, sales_invoices=[inv], posting_date="2026-09-10", submit=False)
			self.assertEqual(pe.sales_channel, "CHAN-SPECIFIC")
			self.assertEqual(pe.transaction_origin, TransactionOrigin.MARKETPLACE)

	def test_52_phase_1l_cancellation_safety_blocks_so_cancel_with_submitted_payment(self):
		with patch.object(frappe.db, "exists", return_value=True):
			with patch.object(frappe.db, "sql") as mock_sql:
				mock_sql.side_effect = [
					[],  # PL
					[],  # DN
					[],  # Shipment
					[],  # SI
					[{"name": "PE-SUBMITTED-01"}],  # PE
				]
				is_safe, reasons = audit_sales_order_cancellation_safety("SO-001")
				self.assertFalse(is_safe)
				self.assertTrue(any("Payment Entry" in r for r in reasons))

	def test_53_phase_1l_cancellation_safety_allows_so_cancel_with_zero_downstream(self):
		with patch.object(frappe.db, "exists", return_value=True):
			with patch.object(frappe.db, "sql") as mock_sql:
				mock_sql.side_effect = [[], [], [], [], []]
				is_safe, reasons = audit_sales_order_cancellation_safety("SO-001")
				self.assertTrue(is_safe)
				self.assertEqual(len(reasons), 0)

	def test_54_counters_increment_and_reset(self):
		reset_payment_counters()
		c1 = get_payment_counters()
		self.assertEqual(c1["reconciliation_requests"], 0)
		self.assertEqual(c1["payments_created"], 0)

	def test_55_unhandled_exception_rolls_back_and_increments_failed_counter(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id=None)

		with patch("frappe.new_doc", side_effect=RuntimeError("DB write failure")):
			with self.assertRaises(RuntimeError):
				reconcile_external_payment(rec, sales_invoices=[inv], posting_date="2026-09-10")
			self.assertEqual(get_payment_counters()["failed"], 1)


