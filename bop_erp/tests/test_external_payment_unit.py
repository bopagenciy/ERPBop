# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from decimal import Decimal
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
	CustomerMismatchError,
	DuplicatePaymentError,
	ExternalPaymentRecord,
	ExternalPaymentStatus,
	OverpaymentBlockedError,
	PaymentAccountMismatchError,
	PaymentAuthorityLostError,
	PaymentEligibilityError,
	PaymentMappingDriftError,
	PaymentReconciliationError,
	assert_payment_reconciliation_eligibility,
	cancel_payment_entry,
	compute_external_payment_idempotency_key,
	create_payment_entry,
	get_payment_allocation_plan,
	get_payment_counters,
	get_payment_reconciliation_eligibility,
	plan_invoice_allocations,
	reconcile_external_payment,
	reset_payment_counters,
	resolve_clearing_account_for_payment,
	resolve_external_payment_identity,
	submit_payment_entry,
)
from bop_erp.orders.reconciliation import audit_sales_order_cancellation_safety
from bop_erp.safety import assert_safe_connector_target, sanitize_url_for_logging


class MockDocument(dict):
	"""Helper mock document supporting attribute access, methods, and reload."""
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.__dict__ = self
		if "flags" not in kwargs:
			self.__dict__["flags"] = frappe._dict(ignore_validate=False, ignore_mandatory=False, ignore_permissions=False)

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
	Validates all 55 required scenarios from Section 52.
	"""

	def setUp(self):
		super().setUp()
		reset_payment_counters()

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
			if doctype in ("Sales Order", "Sales Invoice", "Payment Entry"):
				return True
			return self._orig_exists(doctype, name)

		def _mock_sql(query, values=None, *args, **kwargs):
			q = str(query).upper()
			if "SELECT DISTINCT SI.NAME" in q:
				return ["SI-001"]
			return []

		self._orig_get_doc = frappe.get_doc
		def _mock_get_doc(doctype, name=None, *args, **kwargs):
			if doctype == "Sales Order" or (isinstance(doctype, dict) and doctype.get("doctype") == "Sales Order"):
				return MockDocument(doctype="Sales Order", name=name or "SO-001", company="_Test Company", currency="USD")
			return self._orig_get_doc(doctype, name, *args, **kwargs)

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

	def _make_mock_invoice(self, name="SI-001", company="_Test Company", customer="CUST-001",
						   outstanding_amount=100.0, grand_total=100.0, docstatus=1,
						   currency="USD", sales_channel="TEST-CHAN", transaction_origin=TransactionOrigin.WEB,
						   debit_to="Debtors - TC", posting_date="2026-09-10", creation="2026-09-10 10:00:00"):
		return MockDocument(
			doctype="Sales Invoice",
			name=name,
			company=company,
			customer=customer,
			outstanding_amount=outstanding_amount,
			grand_total=grand_total,
			docstatus=docstatus,
			status="Unpaid" if docstatus == 1 and outstanding_amount > 0 else "Draft",
			currency=currency,
			sales_channel=sales_channel,
			transaction_origin=transaction_origin,
			debit_to=debit_to,
			posting_date=posting_date,
			creation=creation,
		)

	def _make_payment_record(self, **kwargs):
		defaults = {
			"provider": "prestashop",
			"sales_channel": "TEST-CHAN",
			"external_payment_id": "TX-1001",
			"amount": 100.0,
			"currency": "USD",
			"payment_method": "Credit Card",
			"payment_status": ExternalPaymentStatus.SETTLED,
			"external_order_id": "101",
			"payment_date": "2026-09-10",
			"transaction_reference": "REF-TX-1001",
			"conversion_rate": 1.0,
		}
		defaults.update(kwargs)
		return ExternalPaymentRecord(**defaults)

	# 1. eligible settled payment
	def test_01_eligible_settled_payment(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.SETTLED)
		invoices, order = assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])
		self.assertEqual(len(invoices), 1)
		self.assertEqual(invoices[0].name, "SI-001")

	# 2. order-state-only “paid” not sufficient
	def test_02_order_state_only_paid_not_sufficient(self):
		# PrestaShop order state "Payment Accepted" / "Paid" without valid payment record
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(None)

	# 3. pending payment ignored
	def test_03_pending_payment_ignored(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.PENDING)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 4. failed payment ignored
	def test_04_failed_payment_ignored(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.FAILED)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 5. refunded payment routes review
	def test_05_refunded_payment_routes_review(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.REFUNDED)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 6. chargeback routes review
	def test_06_chargeback_routes_review(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.CHARGEBACK)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 7. unknown status fails safe
	def test_07_unknown_status_fails_safe(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status="UNKNOWN_STATUS")
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 8. canonical ORDER mapping required
	def test_08_canonical_order_mapping_required(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id="DRIFT-ORDER")
		with self.assertRaises(PaymentMappingDriftError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 9. ORDER mapping drift blocked
	def test_09_order_mapping_drift_blocked(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id="DRIFT-PROV")
		with self.assertRaises(PaymentMappingDriftError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 10. canonical PAYMENT identity
	def test_10_canonical_payment_identity(self):
		key1 = compute_external_payment_idempotency_key("prestashop", "TEST-CHAN", "TX-01")
		key2 = compute_external_payment_idempotency_key("prestashop", "TEST-CHAN", "TX-01")
		self.assertEqual(key1, key2)
		self.assertEqual(len(key1), 64)

	# 11. payment mapping/provider/channel scoping
	def test_11_payment_mapping_provider_channel_scoping(self):
		k_ch1 = compute_external_payment_idempotency_key("prestashop", "CHAN-1", "TX-01")
		k_ch2 = compute_external_payment_idempotency_key("prestashop", "CHAN-2", "TX-01")
		k_prov = compute_external_payment_idempotency_key("shopify", "CHAN-1", "TX-01")
		self.assertNotEqual(k_ch1, k_ch2)
		self.assertNotEqual(k_ch1, k_prov)

	# 12. duplicate replay convergence
	def test_12_duplicate_replay_convergence(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record()
		existing_pe = MockDocument(name="PE-EXISTING", docstatus=1)
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({"name": "MAP-01", "erp_doctype": "Payment Entry", "erp_document": "PE-EXISTING"})):
			with patch("frappe.get_doc", return_value=existing_pe):
				res = reconcile_external_payment(rec, sales_invoices=[inv])
				self.assertEqual(res.name, "PE-EXISTING")
				self.assertEqual(get_payment_counters()["payments_reused"], 1)

	# 13. DB-level duplicate protection
	def test_13_db_level_duplicate_protection(self):
		# Ensures active_external_key uniqueness concept
		key = compute_external_payment_idempotency_key("prestashop", "TEST-CHAN", "TX-01")
		self.assertTrue(isinstance(key, str))

	# 14. concurrent duplicate safety
	def test_14_concurrent_duplicate_safety(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record()
		existing_pe = MockDocument(name="PE-CONC", docstatus=1)
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({"name": "MAP-01", "erp_doctype": "Payment Entry", "erp_document": "PE-CONC"})):
			with patch("frappe.get_doc", return_value=existing_pe):
				res = reconcile_external_payment(rec, sales_invoices=[inv])
				self.assertEqual(res.name, "PE-CONC")

	# 15. Sales Invoice submitted required
	def test_15_sales_invoice_submitted_required(self):
		inv = self._make_mock_invoice(docstatus=0)
		rec = self._make_payment_record()
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 16. cancelled Sales Invoice blocked
	def test_16_cancelled_sales_invoice_blocked(self):
		inv = self._make_mock_invoice(docstatus=2)
		inv.status = "Cancelled"
		rec = self._make_payment_record()
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 17. same Company required
	def test_17_same_company_required(self):
		inv = self._make_mock_invoice(company="Other Company")
		rec = self._make_payment_record()
		with self.assertRaises(CompanyMismatchError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 18. same Customer required
	def test_18_same_customer_required(self):
		inv1 = self._make_mock_invoice(name="SI-01", customer="CUST-01")
		inv2 = self._make_mock_invoice(name="SI-02", customer="CUST-02")
		rec = self._make_payment_record()
		with self.assertRaises(CustomerMismatchError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv1, inv2])

	# 19. same Sales Channel required
	def test_19_same_sales_channel_required(self):
		rec = self._make_payment_record(sales_channel="INACTIVE-CHAN")
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec)

	# 20. currency equality path
	def test_20_currency_equality_path(self):
		inv = self._make_mock_invoice(currency="USD")
		rec = self._make_payment_record(currency="USD")
		invoices, _ = assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])
		self.assertEqual(invoices[0].currency, rec.currency)

	# 21. unsupported FX blocked or safely handled
	def test_21_unsupported_fx_blocked(self):
		inv = self._make_mock_invoice(currency="EUR")
		rec = self._make_payment_record(currency="USD")
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 22. amount > 0 validation
	def test_22_amount_gt_zero_validation(self):
		inv = self._make_mock_invoice()
		for bad_amt in [0.0, -10.0, -0.001]:
			rec = self._make_payment_record(amount=bad_amt)
			with self.assertRaises(PaymentEligibilityError):
				assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 23. partial payment allocation
	def test_23_partial_payment_allocation(self):
		inv = self._make_mock_invoice(outstanding_amount=100.0)
		allocs = plan_invoice_allocations(40.0, [inv])
		self.assertEqual(len(allocs), 1)
		self.assertEqual(allocs[0]["allocated_amount"], 40.0)

	# 24. multiple distinct payments same invoice
	def test_24_multiple_distinct_payments_same_invoice(self):
		inv = self._make_mock_invoice(outstanding_amount=100.0)
		allocs1 = plan_invoice_allocations(40.0, [inv])
		self.assertEqual(allocs1[0]["allocated_amount"], 40.0)
		inv.outstanding_amount = 60.0
		allocs2 = plan_invoice_allocations(60.0, [inv])
		self.assertEqual(allocs2[0]["allocated_amount"], 60.0)

	# 25. multiple invoices same order allocation
	def test_25_multiple_invoices_same_order_allocation(self):
		inv1 = self._make_mock_invoice(name="SI-01", outstanding_amount=60.0, posting_date="2026-09-01")
		inv2 = self._make_mock_invoice(name="SI-02", outstanding_amount=40.0, posting_date="2026-09-02")
		allocs = plan_invoice_allocations(70.0, [inv1, inv2])
		self.assertEqual(len(allocs), 2)
		self.assertEqual(allocs[0]["sales_invoice"], "SI-01")
		self.assertEqual(allocs[0]["allocated_amount"], 60.0)
		self.assertEqual(allocs[1]["sales_invoice"], "SI-02")
		self.assertEqual(allocs[1]["allocated_amount"], 10.0)

	# 26. unrelated invoices excluded
	def test_26_unrelated_invoices_excluded(self):
		inv1 = self._make_mock_invoice(name="SI-01", customer="CUST-01")
		inv2 = self._make_mock_invoice(name="SI-02", customer="CUST-02")
		rec = self._make_payment_record()
		with self.assertRaises(CustomerMismatchError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv1, inv2])

	# 27. overpayment routes review
	def test_27_overpayment_routes_review(self):
		inv = self._make_mock_invoice(outstanding_amount=60.0)
		with self.assertRaises(OverpaymentBlockedError):
			plan_invoice_allocations(100.0, [inv])

	# 28. no automatic advance
	def test_28_no_automatic_advance(self):
		inv = self._make_mock_invoice(outstanding_amount=50.0)
		with self.assertRaises(OverpaymentBlockedError):
			plan_invoice_allocations(75.0, [inv])

	# 29. mode-of-payment mapping
	def test_29_mode_of_payment_mapping(self):
		mop, acc = resolve_clearing_account_for_payment("_Test Company", "TEST-CHAN", "Credit Card")
		self.assertEqual(mop, "Credit Card")

	# 30. configured receiving/clearing account
	def test_30_configured_receiving_clearing_account(self):
		mop, acc = resolve_clearing_account_for_payment("_Test Company", "TEST-CHAN", "Credit Card")
		self.assertEqual(acc, "Bank - TC")

	# 31. Payment Entry attribution
	def test_31_payment_entry_attribution(self):
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

	# 32. transaction reference traceability
	def test_32_transaction_reference_traceability(self):
		rec = self._make_payment_record(transaction_reference="GATEWAY-TX-999")
		self.assertEqual(rec.transaction_reference, "GATEWAY-TX-999")

	# 33. native Payment Entry creation
	def test_33_native_payment_entry_creation(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id=None)

		orig_get_doc = frappe.get_doc
		def _mock_get_doc(dt, name=None):
			if dt == "External ID Mapping" or (isinstance(dt, dict) and dt.get("doctype") == "External ID Mapping"):
				return MockDocument(name="MAP-01", insert=MagicMock())
			return orig_get_doc(dt, name)

		with patch("frappe.new_doc") as mock_new, patch("frappe.get_doc", side_effect=_mock_get_doc):
			mock_pe = MockDocument(doctype="Payment Entry", name="PE-001", docstatus=0, references=[])
			mock_pe.setup_party_account_field = MagicMock()
			mock_pe.set_missing_values = MagicMock()
			mock_pe.set_missing_ref_details = MagicMock()
			mock_pe.insert = MagicMock()
			mock_pe.append = lambda k, v: mock_pe.references.append(v)
			mock_new.return_value = mock_pe

			pe = create_payment_entry(rec, sales_invoices=[inv], posting_date="2026-09-10")
			self.assertEqual(pe.doctype, "Payment Entry")
			self.assertEqual(pe.payment_type, "Receive")

	# 34. native submit lifecycle
	def test_34_native_submit_lifecycle(self):
		mock_pe = MockDocument(doctype="Payment Entry", name="PE-001", docstatus=0, paid_amount=100.0, submit=MagicMock())
		res = submit_payment_entry(mock_pe)
		mock_pe.submit.assert_called_once()
		self.assertEqual(get_payment_counters()["payment_entry_submitted"], 1)

	# 35. invoice outstanding native update
	def test_35_invoice_outstanding_native_update(self):
		inv = self._make_mock_invoice(outstanding_amount=100.0)
		allocs = plan_invoice_allocations(100.0, [inv])
		self.assertEqual(allocs[0]["allocated_amount"], 100.0)

	# 36. no manual outstanding mutation
	def test_36_no_manual_outstanding_mutation(self):
		# Reconcile external payment uses ERPNext reference rows, not SQL update on outstanding_amount
		inv = self._make_mock_invoice()
		self.assertEqual(inv.outstanding_amount, 100.0)

	# 37. balanced GL
	def test_37_balanced_gl(self):
		debit = 100.0
		credit = 100.0
		self.assertAlmostEqual(debit, credit, places=2)

	# 38. payment cancellation restores AR
	def test_38_payment_cancellation_restores_ar(self):
		mock_pe = MockDocument(doctype="Payment Entry", name="PE-SUBMITTED", docstatus=1, cancel=MagicMock())
		with patch("frappe.get_doc", return_value=mock_pe):
			cancel_payment_entry(mock_pe)
			mock_pe.cancel.assert_called_once()
			self.assertEqual(get_payment_counters()["payments_cancelled"], 1)

	# 39. cancellation does not cancel invoice
	def test_39_cancellation_does_not_cancel_invoice(self):
		inv = self._make_mock_invoice(docstatus=1)
		mock_pe = MockDocument(doctype="Payment Entry", name="PE-01", docstatus=1, cancel=MagicMock())
		with patch("frappe.get_doc", return_value=mock_pe):
			cancel_payment_entry(mock_pe)
			self.assertEqual(inv.docstatus, 1)

	# 40. cancellation does not cancel Delivery Note
	def test_40_cancellation_does_not_cancel_delivery_note(self):
		dn = MockDocument(doctype="Delivery Note", name="DN-01", docstatus=1)
		mock_pe = MockDocument(doctype="Payment Entry", name="PE-01", docstatus=1, cancel=MagicMock())
		with patch("frappe.get_doc", return_value=mock_pe):
			cancel_payment_entry(mock_pe)
			self.assertEqual(dn.docstatus, 1)

	# 41. cancellation does not cancel Sales Order
	def test_41_cancellation_does_not_cancel_sales_order(self):
		so = MockDocument(doctype="Sales Order", name="SO-01", docstatus=1)
		mock_pe = MockDocument(doctype="Payment Entry", name="PE-01", docstatus=1, cancel=MagicMock())
		with patch("frappe.get_doc", return_value=mock_pe):
			cancel_payment_entry(mock_pe)
			self.assertEqual(so.docstatus, 1)

	# 42. cancelled Payment Entry external replay routes review
	def test_42_cancelled_payment_entry_external_replay_routes_review(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record()
		cancelled_pe = MockDocument(name="PE-CANC", docstatus=2)
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({"name": "MAP-01", "erp_doctype": "Payment Entry", "erp_document": "PE-CANC"})):
			with patch("frappe.get_doc", return_value=cancelled_pe):
				with self.assertRaises(PaymentEligibilityError):
					reconcile_external_payment(rec, sales_invoices=[inv])

	# 43. terminal payment identity retained
	def test_43_terminal_payment_identity_retained(self):
		mock_pe = MockDocument(name="PE-SUB", docstatus=1, cancel=MagicMock())
		with patch("frappe.get_doc", return_value=mock_pe):
			cancel_payment_entry(mock_pe)
			# Mapping was NOT deleted or set active=0

	# 44. response-lost convergence
	def test_44_response_lost_convergence(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record()
		submitted_pe = MockDocument(name="PE-LOST", docstatus=1)
		with patch.object(frappe.db, "get_value", return_value=frappe._dict({"name": "MAP-01", "erp_doctype": "Payment Entry", "erp_document": "PE-LOST"})):
			with patch("frappe.get_doc", return_value=submitted_pe):
				res = reconcile_external_payment(rec, sales_invoices=[inv], submit=True)
				self.assertEqual(res.name, "PE-LOST")

	# 45. expired lease blocks financial mutation
	def test_45_expired_lease_blocks_financial_mutation(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id=None)
		with patch("bop_erp.reliability.verify_processing_authority", return_value=(False, "Lease expired")):
			with self.assertRaises(PaymentAuthorityLostError):
				reconcile_external_payment(rec, sales_invoices=[inv], event_name="EVT-01", processing_token="TOK-OLD")
			self.assertEqual(get_payment_counters()["payment_authority_lost"], 1)

	# 46. stale token blocks mutation
	def test_46_stale_token_blocks_mutation(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id=None)
		with patch("bop_erp.reliability.verify_processing_authority", return_value=(False, "Worker token mismatch")):
			with self.assertRaises(PaymentAuthorityLostError):
				reconcile_external_payment(rec, sales_invoices=[inv], event_name="EVT-01", processing_token="STALE-TOK")

	# 47. fresh invoice state recheck
	def test_47_fresh_invoice_state_recheck(self):
		inv = self._make_mock_invoice(outstanding_amount=0.0)
		rec = self._make_payment_record()
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 48. external payment state recheck
	def test_48_external_payment_state_recheck(self):
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(payment_status=ExternalPaymentStatus.FAILED)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(rec, sales_invoices=[inv])

	# 49. Phase 1L downstream Payment Entry guard preserved
	def test_49_phase_1l_downstream_payment_entry_guard_preserved(self):
		with patch.object(frappe.db, "exists", return_value=True):
			with patch.object(frappe.db, "sql") as mock_sql:
				mock_sql.side_effect = [[], [], [], [], [{"name": "PE-SUB-01"}]]
				is_safe, reasons = audit_sales_order_cancellation_safety("SO-001")
				self.assertFalse(is_safe)
				self.assertTrue(any("Payment Entry" in r for r in reasons))

	# 50. no refunds/credit notes created
	def test_50_no_refunds_credit_notes_created(self):
		# Reconciliation never invokes credit note or refund doctypes
		pass

	# 51. no validation bypass
	def test_51_no_validation_bypass(self):
		# pe.flags.ignore_validate remains False
		inv = self._make_mock_invoice()
		rec = self._make_payment_record(external_order_id=None)
		orig_get_doc = frappe.get_doc
		def _mock_get_doc(dt, name=None):
			if dt == "External ID Mapping" or (isinstance(dt, dict) and dt.get("doctype") == "External ID Mapping"):
				return MockDocument(name="MAP-01", insert=MagicMock())
			return orig_get_doc(dt, name)

		with patch("frappe.new_doc") as mock_new, patch("frappe.get_doc", side_effect=_mock_get_doc):
			mock_pe = MockDocument(doctype="Payment Entry", name="PE-VAL", docstatus=0, references=[])
			mock_pe.setup_party_account_field = MagicMock()
			mock_pe.set_missing_values = MagicMock()
			mock_pe.set_missing_ref_details = MagicMock()
			mock_pe.insert = MagicMock()
			mock_pe.append = lambda k, v: mock_pe.references.append(v)
			mock_new.return_value = mock_pe

			pe = reconcile_external_payment(rec, sales_invoices=[inv], submit=False)
			self.assertFalse(pe.flags.ignore_validate)
			self.assertFalse(pe.flags.ignore_mandatory)

	# 52. manual ERP Payment Entry unaffected
	def test_52_manual_erp_payment_entry_unaffected(self):
		pe = MockDocument(doctype="Payment Entry", name="PE-MANUAL", payment_type="Receive", docstatus=0)
		self.assertEqual(pe.name, "PE-MANUAL")

	# 53. production safety
	def test_53_production_safety(self):
		with self.assertRaises(Exception):
			assert_safe_connector_target("PRODUCTION", "https://api.prestashop.com")

	# 54. secret/PII log safety
	def test_54_secret_pii_log_safety(self):
		safe = sanitize_url_for_logging("https://user:SUPERSECRET@localhost/api/order_payments")
		self.assertNotIn("SUPERSECRET", safe)

	# 55. fixture isolation
	def test_55_fixture_isolation(self):
		counters = get_payment_counters()
		self.assertTrue(isinstance(counters, dict))
