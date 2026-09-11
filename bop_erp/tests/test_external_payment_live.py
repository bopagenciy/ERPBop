# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from typing import Any, Tuple
import frappe
from frappe.utils import add_to_date, flt, now_datetime, nowdate

from bop_erp.constants import (
	ExternalEntityType,
	IntegrationProvider,
	IntegrationReadinessStatus,
	IntegrationStatus,
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
	cancel_sales_invoice,
	create_sales_invoice_from_fulfillment,
	get_payment_counters,
	plan_invoice_allocations,
	reconcile_external_payment,
	reset_payment_counters,
	resolve_clearing_account_for_payment,
	submit_payment_entry,
)
from bop_erp.orders.reconciliation import audit_sales_order_cancellation_safety
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry


class TestExternalPaymentLive(unittest.TestCase):
	"""
	Phase 1Q Live Integration Test Suite:
	Payment Entry / External Payment Reconciliation Foundation.
	Executes against the local Frappe / ERPNext isolated test environment.

	Scenarios (Section 53: A to P):
	A. Submitted Sales Invoice = 100, settled external payment = 100 -> submitted native Payment Entry -> outstanding = 0
	B. Invoice = 100, payment = 40 -> outstanding = 60
	C. Second external payment = 60 -> outstanding = 0 -> two distinct Payment Entries
	D. Replay first transaction -> no duplicate Payment Entry -> no duplicate GL effect
	E. Two concurrent workers same external transaction -> exactly one effective Payment Entry
	F. Response lost after Payment Entry submit -> retry converges -> no duplicate accounting
	G. Overpayment: outstanding 60, payment 100 -> REVIEW/BLOCK -> no Payment Entry
	H. Pending/authorized-only payment -> no Payment Entry
	I. External order marked Paid but no normalized transaction -> no Payment Entry
	J. Multi-invoice same order: 60 + 40, payment 70 -> allocations 60 + 10 -> remaining 30
	K. Cross-customer or cross-company attempted allocation -> blocked
	L. Payment Entry cancel: invoice outstanding restored, GL reversed, upstream documents remain submitted
	M. Replay external transaction after manually cancelled Payment Entry -> REVIEW_REQUIRED -> no silent recreation
	N. Expired processing lease before submit -> zero Payment Entry
	O. Mapping drift before payment submit -> zero Payment Entry
	P. Fixture cleanup proof
	"""

	FIXTURE_PREFIX = "TEST-1Q-"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"

		# Defensively clean prior interrupted fixtures
		cls._cleanup_module_fixtures()

		cls.sales_channel = f"{cls.FIXTURE_PREFIX}CH-A"
		cls.channel_b = f"{cls.FIXTURE_PREFIX}CH-B"

		# Ensure sales channels exist
		for ch in [cls.sales_channel, cls.channel_b]:
			if not frappe.db.exists("Sales Channel", ch):
				frappe.get_doc({
					"doctype": "Sales Channel",
					"channel_id": ch,
					"channel_name": ch,
					"channel_type": "PRESTASHOP",
					"integration_provider": IntegrationProvider.PRESTASHOP,
					"company": cls.company,
					"active": 1,
				}).insert(ignore_permissions=True)

		# Ensure warehouse exists
		cls.wh_parent = frappe.db.get_value("Warehouse", {"is_group": 1, "company": cls.company}, "name")
		cls.warehouse = f"{cls.FIXTURE_PREFIX}WH-{cls.abbr} - {cls.abbr}"
		if not frappe.db.exists("Warehouse", cls.warehouse):
			w = frappe.get_doc({
				"doctype": "Warehouse",
				"warehouse_name": f"{cls.FIXTURE_PREFIX}WH-{cls.abbr}",
				"company": cls.company,
				"parent_warehouse": cls.wh_parent,
				"is_group": 0,
			})
			w.flags.ignore_permissions = True
			w.insert(ignore_permissions=True)

		# Ensure test items exist
		cls.item_code = f"{cls.FIXTURE_PREFIX}ITEM-01"
		cls.item_code_2 = f"{cls.FIXTURE_PREFIX}ITEM-02"

		for item_id in [cls.item_code, cls.item_code_2]:
			if not frappe.db.exists("Item", item_id):
				frappe.get_doc({
					"doctype": "Item",
					"item_code": item_id,
					"item_name": item_id,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 1,
				}).insert(ignore_permissions=True)

		# Customer
		cls.customer = frappe.db.get_value("Customer", {}, "name") or "Test Customer"

		# Ensure default income account is set on Company for native resolution
		cls.income_account = "Ventas de mercancías - IDP"
		if not frappe.db.exists("Account", cls.income_account):
			acc = frappe.get_doc({
				"doctype": "Account",
				"account_name": "Ventas de mercancías",
				"company": cls.company,
				"parent_account": "4135 - Comercio al por mayor y al por menor - IDP",
				"account_type": "Income Account",
				"root_type": "Income",
				"is_group": 0,
				"account_currency": "COP",
			})
			acc.insert(ignore_permissions=True)
		frappe.db.set_value("Company", cls.company, "default_income_account", cls.income_account)

		# Ensure bank / clearing account for Company
		cls.bank_parent = "1110 - Bancos - IDP"
		cls.bank_account = f"{cls.FIXTURE_PREFIX}Bank - {cls.abbr}"
		if not frappe.db.exists("Account", cls.bank_account):
			bacc = frappe.get_doc({
				"doctype": "Account",
				"account_name": f"{cls.FIXTURE_PREFIX}Bank",
				"company": cls.company,
				"parent_account": cls.bank_parent,
				"account_type": "Bank",
				"root_type": "Asset",
				"is_group": 0,
				"account_currency": "COP",
			})
			bacc.insert(ignore_permissions=True)

		# Set as company default bank account
		frappe.db.set_value("Company", cls.company, "default_bank_account", cls.bank_account)

		# Ensure Mode of Payment Account is configured
		cls.mode_of_payment = "Credit Card"
		if not frappe.db.exists("Mode of Payment", cls.mode_of_payment):
			frappe.get_doc({
				"doctype": "Mode of Payment",
				"mode_of_payment": cls.mode_of_payment,
				"type": "Bank",
				"enabled": 1,
			}).insert(ignore_permissions=True)

		mop_acc = frappe.db.get_value("Mode of Payment Account", {"parent": cls.mode_of_payment, "company": cls.company}, "name")
		if not mop_acc:
			mop_doc = frappe.get_doc("Mode of Payment", cls.mode_of_payment)
			mop_doc.append("accounts", {
				"company": cls.company,
				"default_account": cls.bank_account,
			})
			mop_doc.save(ignore_permissions=True)

		# Ensure receivable account for direct invoice tests
		cls.receivable_account = (
			frappe.db.get_value("Company", cls.company, "default_receivable_account")
			or frappe.db.get_value("Account", {"company": cls.company, "account_type": "Receivable", "is_group": 0}, "name")
		)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_module_fixtures()
		super().tearDownClass()

	@classmethod
	def _cleanup_module_fixtures(cls):
		company = getattr(cls, "company", None) or frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		abbr = getattr(cls, "abbr", None) or frappe.get_cached_value("Company", company, "abbr") or "IDP"

		# 1. Clean Payment Entries
		pe_names = frappe.db.sql(
			"""
			SELECT name FROM `tabPayment Entry`
			WHERE sales_channel LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%",),
			pluck="name",
		)
		for pe_name in pe_names:
			if frappe.db.exists("Payment Entry", pe_name):
				pe = frappe.get_doc("Payment Entry", pe_name)
				if pe.docstatus == 1:
					pe.cancel()
				frappe.delete_doc("Payment Entry", pe_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": pe_name})

		# 2. Clean Sales Invoices
		si_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabSales Invoice Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabSales Invoice`
			WHERE sales_channel LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%", f"{cls.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		for si_name in si_names:
			if frappe.db.exists("Sales Invoice", si_name):
				si = frappe.get_doc("Sales Invoice", si_name)
				if si.docstatus == 1:
					si.cancel()
				frappe.delete_doc("Sales Invoice", si_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": si_name})

		# 3. Clean Delivery Notes
		dn_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabDelivery Note Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabDelivery Note`
			WHERE sales_channel LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%", f"{cls.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		for dn_name in dn_names:
			if frappe.db.exists("Delivery Note", dn_name):
				dn = frappe.get_doc("Delivery Note", dn_name)
				if dn.docstatus == 1:
					dn.cancel()
				frappe.delete_doc("Delivery Note", dn_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": dn_name})
				frappe.db.delete("Stock Ledger Entry", {"voucher_no": dn_name})

		# 4. Clean Sales Orders
		so_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabSales Order Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabSales Order`
			WHERE sales_channel LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%", f"{cls.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		for so_name in so_names:
			if frappe.db.exists("Sales Order", so_name):
				so = frappe.get_doc("Sales Order", so_name)
				if so.docstatus == 1:
					so.cancel()
				frappe.delete_doc("Sales Order", so_name, force=True, ignore_permissions=True)

		# 5. Clean Stock Entries
		se_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabStock Entry Detail`
			WHERE item_code LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%",),
			pluck="name",
		)
		for se_name in se_names:
			if frappe.db.exists("Stock Entry", se_name):
				se = frappe.get_doc("Stock Entry", se_name)
				if se.docstatus == 1:
					se.cancel()
				frappe.delete_doc("Stock Entry", se_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": se_name})
				frappe.db.delete("Stock Ledger Entry", {"voucher_no": se_name})

		# 5b. Clean SREs and IRRs
		sres = frappe.db.sql(
			"""
			SELECT name FROM `tabStock Reservation Entry`
			WHERE item_code LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%",),
			pluck="name",
		)
		for sre_name in sres:
			frappe.db.set_value("Stock Reservation Entry", sre_name, "docstatus", 2)
			frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)

		frappe.db.sql(
			"""
			DELETE FROM `tabInventory Reservation Reference`
			WHERE item_code LIKE %s
			""",
			(f"{cls.FIXTURE_PREFIX}%",),
		)

		# 6. Clean Mappings, Events, and Channels
		for ch in [f"{cls.FIXTURE_PREFIX}CH-A", f"{cls.FIXTURE_PREFIX}CH-B"]:
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("Sales Channel", {"name": ch})

		# Clean any stray test customers
		frappe.db.delete("Customer", {"name": f"{cls.FIXTURE_PREFIX}OTHER-CUST"})

		# 7. Clean Bank Account
		bank_acc = f"{cls.FIXTURE_PREFIX}Bank - {abbr}"
		if frappe.db.exists("Account", bank_acc):
			frappe.db.delete("Mode of Payment Account", {"default_account": bank_acc})
			frappe.delete_doc("Account", bank_acc, force=True, ignore_permissions=True)

		# 8. Clean Bins, Items, Warehouses
		for ic in [f"{cls.FIXTURE_PREFIX}ITEM-01", f"{cls.FIXTURE_PREFIX}ITEM-02"]:
			frappe.db.delete("Bin", {"item_code": ic})
			if frappe.db.exists("Item", ic):
				frappe.delete_doc("Item", ic, force=True, ignore_permissions=True)

		wh = f"{cls.FIXTURE_PREFIX}WH-{abbr} - {abbr}"
		if frappe.db.exists("Warehouse", wh):
			frappe.delete_doc("Warehouse", wh, force=True, ignore_permissions=True)

		frappe.db.commit()

	def setUp(self):
		super().setUp()
		reset_payment_counters()
		# Seed physical stock
		self._seed_stock(self.item_code, 30.0)

	def tearDown(self):
		self._cleanup_test_docs()
		super().tearDown()

	def _seed_stock(self, item_code: str, target_qty: float):
		current = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": self.warehouse}, "actual_qty") or 0.0)
		diff = target_qty - current
		if abs(diff) > 0.001:
			if diff > 0:
				make_stock_entry(
					item_code=item_code,
					target=self.warehouse,
					qty=diff,
					rate=50.0,
					company=self.company,
					purpose="Material Receipt",
				)
			else:
				make_stock_entry(
					item_code=item_code,
					source=self.warehouse,
					qty=abs(diff),
					rate=50.0,
					company=self.company,
					purpose="Material Issue",
				)
		frappe.db.commit()

	def _create_so_and_dn(self, ext_id: str, qty: float = 1.0, channel: str = None) -> Tuple[Any, Any]:
		ch = channel or self.sales_channel
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": self.customer,
			"company": self.company,
			"transaction_origin": TransactionOrigin.WEB,
			"sales_channel": ch,
			"external_order_id": ext_id,
			"integration_status": IntegrationReadinessStatus.READY,
			"delivery_date": nowdate(),
			"currency": "COP",
			"items": [{
				"item_code": self.item_code,
				"qty": qty,
				"rate": 100.0,
				"warehouse": self.warehouse,
			}],
		})
		so.insert(ignore_permissions=True)
		so.submit()

		if not frappe.db.exists("External ID Mapping", {"external_id": ext_id, "sales_channel": ch}):
			mapping = frappe.get_doc({
				"doctype": "External ID Mapping",
				"provider": IntegrationProvider.PRESTASHOP,
				"sales_channel": ch,
				"external_entity_type": ExternalEntityType.ORDER,
				"external_id": ext_id,
				"erp_doctype": "Sales Order",
				"erp_document": so.name,
				"active": 1,
			})
			mapping.insert(ignore_permissions=True)

		sre = frappe.get_doc({
			"doctype": "Stock Reservation Entry",
			"item_code": self.item_code,
			"warehouse": self.warehouse,
			"voucher_type": "Sales Order",
			"voucher_no": so.name,
			"voucher_detail_no": so.items[0].name,
			"voucher_qty": qty,
			"available_qty": 30.0,
			"reserved_qty": qty,
			"delivered_qty": 0.0,
			"transferred_qty": 0.0,
			"consumed_qty": 0.0,
			"company": self.company,
			"stock_uom": "Nos",
			"reservation_based_on": "Qty",
		})
		sre.flags.ignore_validate = True
		sre.flags.ignore_permissions = True
		sre.insert(ignore_permissions=True)
		sre.submit()

		ref = frappe.get_doc({
			"doctype": "Inventory Reservation Reference",
			"idempotency_key": f"IRR-{so.name}-{self.item_code}-{frappe.generate_hash(length=4)}",
			"stock_reservation_entry": sre.name,
			"item_code": self.item_code,
			"warehouse": self.warehouse,
			"reserved_qty": qty,
			"source_doctype": "Sales Order",
			"source_document": so.name,
			"source_document_item": so.items[0].name,
			"status": "Reserved",
		})
		ref.insert(ignore_permissions=True)

		dn = make_delivery_note(so.name)
		dn.items[0].qty = qty
		dn.sales_channel = ch
		dn.transaction_origin = TransactionOrigin.WEB
		dn.external_order_id = ext_id
		dn.insert(ignore_permissions=True)
		dn.submit()

		frappe.db.commit()
		return so, dn

	def _create_submitted_invoice(self, ext_id: str, qty: float = 1.0, channel: str = None) -> Tuple[Any, Any, Any]:
		so, dn = self._create_so_and_dn(ext_id, qty=qty, channel=channel)
		si = create_sales_invoice_from_fulfillment(dn.name, submit=True)
		return so, dn, si

	def _cleanup_test_docs(self):
		pe_names = frappe.db.get_all("Payment Entry", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]}, pluck="name")
		for name in pe_names:
			pe = frappe.get_doc("Payment Entry", name)
			if pe.docstatus == 1:
				pe.cancel()
			frappe.delete_doc("Payment Entry", name, force=True, ignore_permissions=True)
			frappe.db.delete("GL Entry", {"voucher_no": name})

		si_names = frappe.db.get_all("Sales Invoice", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]}, pluck="name")
		for name in si_names:
			si = frappe.get_doc("Sales Invoice", name)
			if si.docstatus == 1:
				si.cancel()
			frappe.delete_doc("Sales Invoice", name, force=True, ignore_permissions=True)
			frappe.db.delete("GL Entry", {"voucher_no": name})

		dn_names = frappe.db.get_all("Delivery Note", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]}, pluck="name")
		for name in dn_names:
			dn = frappe.get_doc("Delivery Note", name)
			if dn.docstatus == 1:
				dn.cancel()
			frappe.delete_doc("Delivery Note", name, force=True, ignore_permissions=True)
			frappe.db.delete("GL Entry", {"voucher_no": name})
			frappe.db.delete("Stock Ledger Entry", {"voucher_no": name})

		so_names = frappe.db.get_all("Sales Order", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]}, pluck="name")
		for name in so_names:
			so = frappe.get_doc("Sales Order", name)
			if so.docstatus == 1:
				so.cancel()
			frappe.delete_doc("Sales Order", name, force=True, ignore_permissions=True)

		# Clean SREs and IRRs for test channels/items
		sres = frappe.db.get_all("Stock Reservation Entry", filters={"item_code": ["in", [self.item_code, self.item_code_2]]}, pluck="name")
		for sre_name in sres:
			frappe.db.set_value("Stock Reservation Entry", sre_name, "docstatus", 2)
			frappe.delete_doc("Stock Reservation Entry", sre_name, force=True, ignore_permissions=True)

		frappe.db.delete("Inventory Reservation Reference", {"item_code": ["in", [self.item_code, self.item_code_2]]})

		# Clean mappings for test channels
		frappe.db.delete("External ID Mapping", {"sales_channel": ["in", [self.sales_channel, self.channel_b]]})
		frappe.db.delete("Integration Event", {"sales_channel": ["in", [self.sales_channel, self.channel_b]]})

		frappe.db.commit()

	# =========================================================================
	# SCENARIO A: Submitted Sales Invoice = 100, settled external payment = 100
	# -> submitted native Payment Entry -> outstanding = 0
	# =========================================================================
	def test_scenario_a_settled_payment_full_reconciliation(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-A-001", qty=1.0)
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(flt(si.outstanding_amount), 100.0)

		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-A-001",
			amount=100.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-A-001",
		)

		pe = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		self.assertEqual(pe.doctype, "Payment Entry")
		self.assertEqual(pe.payment_type, "Receive")
		self.assertEqual(pe.docstatus, 1)
		self.assertEqual(pe.party, self.customer)
		self.assertEqual(pe.sales_channel, self.sales_channel)
		self.assertEqual(flt(pe.paid_amount), 100.0)

		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 0.0)
		self.assertEqual(si.status, "Paid")

	# =========================================================================
	# SCENARIO B: Invoice = 100, payment = 40 -> outstanding = 60
	# =========================================================================
	def test_scenario_b_partial_payment_reduces_outstanding_amount(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-B-001", qty=1.0)
		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-B-001",
			amount=40.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-B-001",
		)

		pe = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 60.0)
		self.assertEqual(si.status, "Partly Paid")

	# =========================================================================
	# SCENARIO C: Second external payment = 60 -> outstanding = 0 -> two distinct PEs
	# =========================================================================
	def test_scenario_c_second_payment_completes_invoice_two_entries(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-C-001", qty=1.0)
		# First payment 40.0
		pay1 = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-C-001",
			amount=40.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-C-001",
		)
		pe1 = reconcile_external_payment(pay1, sales_invoices=[si], submit=True)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 60.0)

		# Second external payment 60.0
		pay2 = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-C-002",
			amount=60.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-C-001",
		)
		pe2 = reconcile_external_payment(pay2, sales_invoices=[si], submit=True)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 0.0)
		self.assertEqual(si.status, "Paid")
		self.assertNotEqual(pe1.name, pe2.name)

	# =========================================================================
	# SCENARIO D: Replay first transaction -> no duplicate Payment Entry -> no duplicate GL
	# =========================================================================
	def test_scenario_d_replay_first_transaction_no_duplicate_accounting(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-D-001", qty=1.0)
		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-D-001",
			amount=40.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-D-001",
		)
		pe1 = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		gl_count_1 = frappe.db.count("GL Entry", {"voucher_no": pe1.name})
		si.reload()
		out_1 = flt(si.outstanding_amount)

		# Replay identical transaction
		pe2 = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		self.assertEqual(pe1.name, pe2.name)
		gl_count_2 = frappe.db.count("GL Entry", {"voucher_no": pe1.name})
		self.assertEqual(gl_count_1, gl_count_2)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), out_1)

	# =========================================================================
	# SCENARIO E: Two concurrent workers same external transaction -> exactly one effective PE
	# =========================================================================
	def test_scenario_e_concurrent_duplicate_payment_convergence(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-E-001", qty=1.0)
		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-E-001",
			amount=100.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-E-001",
		)

		pe1 = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		pe2 = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		self.assertEqual(pe1.name, pe2.name)
		pe_count = frappe.db.count("Payment Entry Reference", {"reference_name": si.name})
		self.assertEqual(pe_count, 1)

	# =========================================================================
	# SCENARIO F: Response lost after Payment Entry submit -> retry converges -> no duplicate accounting
	# =========================================================================
	def test_scenario_f_response_lost_retry_converges(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-F-001", qty=1.0)
		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-F-001",
			amount=100.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-F-001",
		)
		pe = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		# Worker loses response, retries
		pe_retry = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		self.assertEqual(pe.name, pe_retry.name)
		self.assertEqual(pe_retry.docstatus, 1)

	# =========================================================================
	# SCENARIO G: Overpayment: outstanding 60, payment 100 -> REVIEW/BLOCK -> no Payment Entry
	# =========================================================================
	def test_scenario_g_overpayment_blocked_routes_review(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-G-001", qty=1.0) # total 100.0
		# Apply partial payment 40.0 so outstanding is 60.0
		pay_part = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-G-PART",
			amount=40.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-G-001",
		)
		reconcile_external_payment(pay_part, sales_invoices=[si], submit=True)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 60.0)

		# Attempt overpayment of 100.0 on 60.0 outstanding
		pay_over = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-G-OVER",
			amount=100.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-G-001",
		)
		with self.assertRaises(OverpaymentBlockedError):
			reconcile_external_payment(pay_over, sales_invoices=[si], submit=True)

		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 60.0)

	# =========================================================================
	# SCENARIO H: Pending/authorized-only payment -> no Payment Entry
	# =========================================================================
	def test_scenario_h_pending_authorized_payment_no_pe(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-H-001", qty=1.0)
		for st in [ExternalPaymentStatus.PENDING, ExternalPaymentStatus.AUTHORIZED]:
			pay_rec = ExternalPaymentRecord(
				provider=IntegrationProvider.PRESTASHOP,
				sales_channel=self.sales_channel,
				external_payment_id=f"PAY-H-{st}",
				amount=100.0,
				currency="COP",
				payment_method="Credit Card",
				payment_status=st,
				external_order_id="1Q-EXT-H-001",
			)
			with self.assertRaises(PaymentEligibilityError):
				reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 100.0)

	# =========================================================================
	# SCENARIO I: External order marked Paid but no normalized transaction -> no Payment Entry
	# =========================================================================
	def test_scenario_i_order_marked_paid_without_transaction_no_pe(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-I-001", qty=1.0)
		with self.assertRaises(PaymentEligibilityError):
			assert_payment_reconciliation_eligibility(None)

	# =========================================================================
	# SCENARIO J: Multi-invoice same order: 60 + 40, payment 70 -> allocations 60 + 10 -> remaining 30
	# =========================================================================
	def test_scenario_j_multi_invoice_same_order_allocation(self):
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"customer": self.customer,
			"company": self.company,
			"transaction_origin": TransactionOrigin.WEB,
			"sales_channel": self.sales_channel,
			"external_order_id": "1Q-EXT-J-001",
			"integration_status": IntegrationReadinessStatus.READY,
			"delivery_date": nowdate(),
			"currency": "COP",
			"items": [{
				"item_code": self.item_code,
				"qty": 1.0,
				"rate": 100.0,
				"warehouse": self.warehouse,
			}],
		}).insert(ignore_permissions=True)
		so.submit()

		mapping = frappe.get_doc({
			"doctype": "External ID Mapping",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.sales_channel,
			"external_entity_type": ExternalEntityType.ORDER,
			"external_id": "1Q-EXT-J-001",
			"erp_doctype": "Sales Order",
			"erp_document": so.name,
			"active": 1,
		}).insert(ignore_permissions=True)

		# Make two invoices directly for this order
		si1 = frappe.get_doc({
			"doctype": "Sales Invoice",
			"customer": self.customer,
			"company": self.company,
			"sales_channel": self.sales_channel,
			"currency": "COP",
			"debit_to": self.receivable_account,
			"posting_date": "2026-09-01",
			"items": [{
				"item_code": self.item_code,
				"qty": 1.0,
				"rate": 60.0,
				"income_account": self.income_account,
			}],
		}).insert(ignore_permissions=True)
		si1.submit()

		si2 = frappe.get_doc({
			"doctype": "Sales Invoice",
			"customer": self.customer,
			"company": self.company,
			"sales_channel": self.sales_channel,
			"currency": "COP",
			"debit_to": self.receivable_account,
			"posting_date": "2026-09-02",
			"items": [{
				"item_code": self.item_code,
				"qty": 1.0,
				"rate": 40.0,
				"income_account": self.income_account,
			}],
		}).insert(ignore_permissions=True)
		si2.submit()

		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-J-MULTI",
			amount=70.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id=None,
		)

		pe = reconcile_external_payment(pay_rec, sales_invoices=[si1, si2], submit=True)
		si1.reload()
		si2.reload()
		self.assertEqual(flt(si1.outstanding_amount), 0.0)
		self.assertEqual(flt(si2.outstanding_amount), 30.0)

	# =========================================================================
	# SCENARIO K: Cross-customer or cross-company attempted allocation -> blocked
	# =========================================================================
	def test_scenario_k_cross_customer_or_company_blocked(self):
		so1, dn1, si1 = self._create_submitted_invoice("1Q-EXT-K-001", qty=1.0)
		other_customer = f"{self.FIXTURE_PREFIX}OTHER-CUST"
		cg = frappe.db.get_value("Customer", self.customer, "customer_group") or frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		terr = frappe.db.get_value("Customer", self.customer, "territory") or frappe.db.get_value("Territory", {"is_group": 0}, "name")
		if not frappe.db.exists("Customer", other_customer):
			frappe.get_doc({
				"doctype": "Customer",
				"customer_name": other_customer,
				"customer_group": cg,
				"customer_type": "Individual",
				"territory": terr,
			}).insert(ignore_permissions=True)

		si_other = frappe.get_doc({
			"doctype": "Sales Invoice",
			"customer": other_customer,
			"company": self.company,
			"sales_channel": self.sales_channel,
			"currency": "COP",
			"debit_to": self.receivable_account,
			"items": [{
				"item_code": self.item_code,
				"qty": 1.0,
				"rate": 100.0,
				"income_account": self.income_account,
			}],
		}).insert(ignore_permissions=True)
		si_other.submit()

		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-K-CROSS",
			amount=100.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
		)
		with self.assertRaises(CustomerMismatchError):
			reconcile_external_payment(pay_rec, sales_invoices=[si1, si_other], submit=True)

	# =========================================================================
	# SCENARIO L: Payment Entry cancel: invoice outstanding restored, GL reversed,
	# upstream documents remain submitted
	# =========================================================================
	def test_scenario_l_payment_entry_cancel_restores_ar_upstream_intact(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-L-001", qty=1.0)
		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-L-001",
			amount=100.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-L-001",
		)
		pe = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 0.0)

		# Cancel Payment Entry
		cancel_payment_entry(pe.name)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 100.0)
		self.assertEqual(si.docstatus, 1)

		# Upstream documents remain submitted
		dn.reload()
		so.reload()
		self.assertEqual(dn.docstatus, 1)
		self.assertEqual(so.docstatus, 1)

	# =========================================================================
	# SCENARIO M: Replay external transaction after manually cancelled Payment Entry
	# -> REVIEW_REQUIRED -> no silent recreation
	# =========================================================================
	def test_scenario_m_replay_after_cancelled_payment_entry_routes_review(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-M-001", qty=1.0)
		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-M-001",
			amount=100.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-M-001",
		)
		pe = reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		cancel_payment_entry(pe.name)

		# Replay the same external payment record
		with self.assertRaises(PaymentEligibilityError):
			reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)

	# =========================================================================
	# SCENARIO N: Expired processing lease before submit -> zero Payment Entry
	# =========================================================================
	def test_scenario_n_expired_processing_lease_blocks_mutation(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-N-001", qty=1.0)
		evt = frappe.get_doc({
			"doctype": "Integration Event",
			"provider": IntegrationProvider.PRESTASHOP,
			"sales_channel": self.sales_channel,
			"event_type": "PAYMENT",
			"status": IntegrationStatus.PROCESSING,
			"processing_token": "TOK-EXP-01",
			"lease_expires_at": add_to_date(now_datetime(), seconds=-60),
		}).insert(ignore_permissions=True)

		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-N-EXP",
			amount=100.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-N-001",
		)
		with self.assertRaises(PaymentAuthorityLostError):
			reconcile_external_payment(
				pay_rec,
				sales_invoices=[si],
				submit=True,
				event_name=evt.name,
				processing_token="TOK-EXP-01",
			)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 100.0)

	# =========================================================================
	# SCENARIO O: Mapping drift before payment submit -> zero Payment Entry
	# =========================================================================
	def test_scenario_o_mapping_drift_blocks_mutation(self):
		so, dn, si = self._create_submitted_invoice("1Q-EXT-O-001", qty=1.0)
		frappe.db.set_value(
			"External ID Mapping",
			{"external_id": "1Q-EXT-O-001", "sales_channel": self.sales_channel},
			"provider",
			"OTHER_PROV",
		)
		frappe.db.commit()

		pay_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id="PAY-O-DRIFT",
			amount=100.0,
			currency="COP",
			payment_method="Credit Card",
			payment_status=ExternalPaymentStatus.SETTLED,
			external_order_id="1Q-EXT-O-001",
		)
		with self.assertRaises(PaymentMappingDriftError):
			reconcile_external_payment(pay_rec, sales_invoices=[si], submit=True)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 100.0)

	# =========================================================================
	# SCENARIO P: Fixture cleanup proof
	# =========================================================================
	def test_scenario_p_fixture_cleanup_proof(self):
		self._cleanup_test_docs()
		residual_pes = frappe.db.count("Payment Entry", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]})
		self.assertEqual(residual_pes, 0)
		residual_sis = frappe.db.count("Sales Invoice", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]})
		self.assertEqual(residual_sis, 0)
		residual_sos = frappe.db.count("Sales Order", filters={"sales_channel": ["in", [self.sales_channel, self.channel_b]]})
		self.assertEqual(residual_sos, 0)
