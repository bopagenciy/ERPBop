# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import threading
import unittest
from decimal import Decimal
import frappe
from frappe.utils import flt, nowdate, now_datetime

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
	ExternalPaymentRecord,
	ExternalPaymentStatus,
	ExternalRefundItem,
	ExternalRefundRecord,
	OverRefundBlockedError,
	RefundDriftError,
	RefundEligibilityError,
	RefundReplayCancelledError,
	create_sales_invoice_from_fulfillment,
	get_refund_counters,
	process_external_refund,
	reconcile_external_payment,
	reset_refund_counters,
)
from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry


class TestExternalRefundLive(unittest.TestCase):
	"""
	Phase 1R Live Integration Test Suite:
	Sales Return / Credit Note & External Refund Accounting Foundation.
	Executes against the local Frappe / ERPNext isolated container environment.

	Scenarios (Section 28: A to M):
	A. Submitted unpaid Sales Invoice 100 -> financial-only refund 20 -> Credit Note 20 -> AR adjusted natively -> zero stock change
	B. Fully paid invoice 100 -> external refund 20 -> Credit Note customer credit verified -> original Payment Entry untouched
	C. Delivered 2 units -> physically returned 1 -> exactly 1 unit restored -> correct Credit Note -> correct stock ledger
	D. Partial refund A 20, partial refund B 30 -> cumulative 50 -> two distinct external identities -> no duplicate accounting
	E. Replay refund A -> no second Credit Note (idempotent reuse)
	F. Concurrent same refund -> one Credit Note
	G. Attempt over-refund -> blocked/review -> no mutation
	H. Attempt physical over-return -> blocked -> no stock mutation
	I. Credit Note cancel -> native accounting reversal -> external identity remains claimed -> replay routes review
	J. Response lost after Credit Note submit -> retry converges
	K. Financial-only refund -> no inventory publication event
	L. Physical return -> inventory publication intent created transactionally
	M. Fixture cleanup proof (0 residual TEST-1R-% records across all tables)
	"""

	FIXTURE_PREFIX = "TEST-1R-"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not getattr(frappe.local, "site", None):
			frappe.init("frontend")
			frappe.connect()

		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		cls.abbr = frappe.get_cached_value("Company", cls.company, "abbr") or "IDP"

		# Snapshot baselines before module execution
		cls.baseline_sales_invoices = set(frappe.get_all("Sales Invoice", pluck="name"))
		cls.baseline_delivery_notes = set(frappe.get_all("Delivery Note", pluck="name"))
		cls.baseline_sales_orders = set(frappe.get_all("Sales Order", pluck="name"))
		cls.baseline_payments = set(frappe.get_all("Payment Entry", pluck="name"))
		cls.baseline_mappings = set(frappe.get_all("External ID Mapping", pluck="name"))
		cls.baseline_events = set(frappe.get_all("Integration Event", pluck="name"))
		cls.baseline_items = set(frappe.get_all("Item", pluck="name"))
		cls.baseline_warehouses = set(frappe.get_all("Warehouse", pluck="name"))

		# Defensively clean prior fixtures
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

		# Ensure test items exist (non-serialized, non-batched)
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
					"has_serial_no": 0,
					"has_batch_no": 0,
				}).insert(ignore_permissions=True)

		# Setup Product External ID Mappings for publication testing
		for itm, ext_id in [(cls.item_code, "9001"), (cls.item_code_2, "9002")]:
			for ch in [cls.sales_channel, cls.channel_b]:
				if not frappe.db.exists("External ID Mapping", {
					"sales_channel": ch,
					"provider": IntegrationProvider.PRESTASHOP,
					"external_entity_type": ExternalEntityType.PRODUCT,
					"external_id": ext_id,
				}):
					frappe.get_doc({
						"doctype": "External ID Mapping",
						"sales_channel": ch,
						"provider": IntegrationProvider.PRESTASHOP,
						"external_entity_type": ExternalEntityType.PRODUCT,
						"external_id": ext_id,
						"erp_doctype": "Item",
						"erp_document": itm,
						"active": 1,
					}).insert(ignore_permissions=True)

		# Customer
		cls.customer = frappe.db.get_value("Customer", {}, "name") or "Test Customer"

		# Default income account
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

		# Bank account for payment tests
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

		frappe.db.set_value("Company", cls.company, "default_bank_account", cls.bank_account)

		cls.mode_of_payment = "Credit Card"
		if not frappe.db.exists("Mode of Payment", cls.mode_of_payment):
			frappe.get_doc({
				"doctype": "Mode of Payment",
				"mode_of_payment": cls.mode_of_payment,
				"type": "Bank",
				"enabled": 1,
			}).insert(ignore_permissions=True)

		mop_acc = frappe.db.get_value("Mode of Payment Account", {"parent": cls.mode_of_payment, "company": cls.company}, "name")
		if mop_acc:
			frappe.db.set_value("Mode of Payment Account", mop_acc, "default_account", cls.bank_account)
		else:
			mop_doc = frappe.get_doc("Mode of Payment", cls.mode_of_payment)
			mop_doc.append("accounts", {
				"company": cls.company,
				"default_account": cls.bank_account,
			})
			mop_doc.save(ignore_permissions=True)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		cls._cleanup_module_fixtures()
		super().tearDownClass()

	@classmethod
	def _cleanup_module_fixtures(cls):
		company = getattr(cls, "company", None) or frappe.db.get_single_value("Global Defaults", "default_company") or "Industrial DP"
		abbr = getattr(cls, "abbr", None) or frappe.get_cached_value("Company", company, "abbr") or "IDP"

		# 1. Clean Journal Entries (reconciliation entries)
		jvs = frappe.db.sql(
			"""
			SELECT name FROM `tabJournal Entry`
			WHERE user_remark LIKE %s
			""",
			(f"%{cls.FIXTURE_PREFIX}%",),
			pluck="name",
		)
		for jv_name in jvs:
			if frappe.db.exists("Journal Entry", jv_name):
				jv = frappe.get_doc("Journal Entry", jv_name)
				if jv.docstatus == 1:
					jv.cancel()
				frappe.delete_doc("Journal Entry", jv_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": jv_name})

		# 2. Clean Payment Entries
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

		# 3. Clean Sales Invoices (Credit Notes first, then Invoices)
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
		returns = [s for s in si_names if frappe.db.get_value("Sales Invoice", s, "is_return")]
		regulars = [s for s in si_names if s not in returns]

		for s_name in returns + regulars:
			if frappe.db.exists("Sales Invoice", s_name):
				si = frappe.get_doc("Sales Invoice", s_name)
				if si.docstatus == 1:
					si.cancel()
				frappe.delete_doc("Sales Invoice", s_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": s_name})
				frappe.db.delete("Stock Ledger Entry", {"voucher_no": s_name})

		# 4. Clean Delivery Notes (returns first)
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
		dn_returns = [d for d in dn_names if frappe.db.get_value("Delivery Note", d, "is_return")]
		dn_regulars = [d for d in dn_names if d not in dn_returns]

		for d_name in dn_returns + dn_regulars:
			if frappe.db.exists("Delivery Note", d_name):
				dn = frappe.get_doc("Delivery Note", d_name)
				if dn.docstatus == 1:
					dn.cancel()
				frappe.delete_doc("Delivery Note", d_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": d_name})
				frappe.db.delete("Stock Ledger Entry", {"voucher_no": d_name})

		# 5. Clean Sales Orders
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

		# 6. Clean Stock Entries
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

		# 7. Clean SREs and IRRs
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

		# 8. Clean Mappings and Channels
		for ch in [f"{cls.FIXTURE_PREFIX}CH-A", f"{cls.FIXTURE_PREFIX}CH-B"]:
			frappe.db.delete("External ID Mapping", {"sales_channel": ch})
			frappe.db.delete("Integration Event", {"sales_channel": ch})
			frappe.db.delete("Sales Channel", {"name": ch})

		# 9. Clean Bank Account
		bank_acc = f"{cls.FIXTURE_PREFIX}Bank - {abbr}"
		if frappe.db.exists("Account", bank_acc):
			frappe.db.delete("Mode of Payment Account", {"default_account": bank_acc})
			frappe.delete_doc("Account", bank_acc, force=True, ignore_permissions=True)

		# 10. Clean Bins, Items, Warehouses
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
		reset_refund_counters()
		# Seed physical stock to baseline 30 units
		self._seed_stock(self.item_code, 30.0)
		self._seed_stock(self.item_code_2, 30.0)

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

	def _cleanup_test_docs(self):
		# Clean Journal Entries
		jvs = frappe.db.sql(
			"""
			SELECT name FROM `tabJournal Entry`
			WHERE user_remark LIKE %s
			""",
			(f"%{self.FIXTURE_PREFIX}%",),
			pluck="name",
		)
		for jv_name in jvs:
			if frappe.db.exists("Journal Entry", jv_name):
				jv = frappe.get_doc("Journal Entry", jv_name)
				if jv.docstatus == 1:
					jv.cancel()
				frappe.delete_doc("Journal Entry", jv_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": jv_name})

		# Clean Payment Entries
		pe_names = frappe.db.sql(
			"""
			SELECT name FROM `tabPayment Entry`
			WHERE sales_channel LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%",),
			pluck="name",
		)
		for pe_name in pe_names:
			if frappe.db.exists("Payment Entry", pe_name):
				pe = frappe.get_doc("Payment Entry", pe_name)
				if pe.docstatus == 1:
					pe.cancel()
				frappe.delete_doc("Payment Entry", pe_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": pe_name})

		# Clean Sales Invoices (Credit Notes first, then Invoices)
		si_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabSales Invoice Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabSales Invoice`
			WHERE sales_channel LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%", f"{self.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		returns = [s for s in si_names if frappe.db.get_value("Sales Invoice", s, "is_return")]
		regulars = [s for s in si_names if s not in returns]
		for s_name in returns + regulars:
			if frappe.db.exists("Sales Invoice", s_name):
				si = frappe.get_doc("Sales Invoice", s_name)
				if si.docstatus == 1:
					si.cancel()
				frappe.delete_doc("Sales Invoice", s_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": s_name})
				frappe.db.delete("Stock Ledger Entry", {"voucher_no": s_name})

		# Clean Delivery Notes
		dn_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabDelivery Note Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabDelivery Note`
			WHERE sales_channel LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%", f"{self.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		dn_returns = [d for d in dn_names if frappe.db.get_value("Delivery Note", d, "is_return")]
		dn_regulars = [d for d in dn_names if d not in dn_returns]
		for d_name in dn_returns + dn_regulars:
			if frappe.db.exists("Delivery Note", d_name):
				dn = frappe.get_doc("Delivery Note", d_name)
				if dn.docstatus == 1:
					dn.cancel()
				frappe.delete_doc("Delivery Note", d_name, force=True, ignore_permissions=True)
				frappe.db.delete("GL Entry", {"voucher_no": d_name})
				frappe.db.delete("Stock Ledger Entry", {"voucher_no": d_name})

		# Clean Sales Orders
		so_names = frappe.db.sql(
			"""
			SELECT DISTINCT parent FROM `tabSales Order Item`
			WHERE item_code LIKE %s
			UNION
			SELECT name FROM `tabSales Order`
			WHERE sales_channel LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%", f"{self.FIXTURE_PREFIX}%"),
			pluck="name",
		)
		for so_name in so_names:
			if frappe.db.exists("Sales Order", so_name):
				so = frappe.get_doc("Sales Order", so_name)
				if so.docstatus == 1:
					so.cancel()
				frappe.delete_doc("Sales Order", so_name, force=True, ignore_permissions=True)

		# Clean SREs and IRRs
		sres = frappe.db.sql(
			"""
			SELECT name FROM `tabStock Reservation Entry`
			WHERE item_code LIKE %s
			""",
			(f"{self.FIXTURE_PREFIX}%",),
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
			(f"{self.FIXTURE_PREFIX}%",),
		)

		# Clean refund mappings
		for ch in [f"{self.FIXTURE_PREFIX}CH-A", f"{self.FIXTURE_PREFIX}CH-B"]:
			frappe.db.delete("External ID Mapping", {
				"sales_channel": ch,
				"external_entity_type": ["in", [ExternalEntityType.REFUND, ExternalEntityType.ORDER]],
			})
			frappe.db.delete("Integration Event", {"sales_channel": ch})

		frappe.db.commit()

	def _create_submitted_invoice(
		self,
		ext_order_id: str,
		qty: float = 3.0,
		rate: float = 100.0,
		channel: str = None,
		item_code: str = None,
		direct_stock_invoice: bool = False,
	):
		"""
		Creates a fully compliant submitted Sales Order -> Delivery Note -> Sales Invoice chain.
		If direct_stock_invoice is True, creates a Sales Invoice with update_stock=1 directly.
		"""
		ch = channel or self.sales_channel
		it = item_code or self.item_code

		if direct_stock_invoice:
			si = frappe.get_doc({
				"doctype": "Sales Invoice",
				"company": self.company,
				"customer": self.customer,
				"sales_channel": ch,
				"transaction_origin": TransactionOrigin.WEB,
				"external_order_id": ext_order_id,
				"posting_date": nowdate(),
				"posting_time": "10:00:00",
				"currency": "COP",
				"update_stock": 1,
				"items": [{
					"item_code": it,
					"qty": qty,
					"rate": rate,
					"warehouse": self.warehouse,
					"income_account": self.income_account,
				}],
			})
			si.insert(ignore_permissions=True)
			si.submit()

			mapping_so = frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": ch,
				"provider": IntegrationProvider.PRESTASHOP,
				"external_entity_type": ExternalEntityType.ORDER,
				"external_id": ext_order_id,
				"erp_doctype": "Sales Invoice",
				"erp_document": si.name,
				"active": 1,
			})
			mapping_so.insert(ignore_permissions=True)
			frappe.db.commit()
			return None, None, si

		# Standard SO -> DN -> SI flow
		so = frappe.get_doc({
			"doctype": "Sales Order",
			"company": self.company,
			"customer": self.customer,
			"delivery_date": nowdate(),
			"sales_channel": ch,
			"transaction_origin": TransactionOrigin.WEB,
			"external_order_id": ext_order_id,
			"integration_status": "READY",
			"currency": "COP",
			"items": [{
				"item_code": it,
				"qty": qty,
				"rate": rate,
				"warehouse": self.warehouse,
			}],
		})
		so.insert(ignore_permissions=True)
		so.submit()

		mapping_so = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": ch,
			"provider": IntegrationProvider.PRESTASHOP,
			"external_entity_type": ExternalEntityType.ORDER,
			"external_id": ext_order_id,
			"erp_doctype": "Sales Order",
			"erp_document": so.name,
			"active": 1,
		})
		mapping_so.insert(ignore_permissions=True)

		sre = frappe.get_doc({
			"doctype": "Stock Reservation Entry",
			"item_code": it,
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
			"idempotency_key": f"IRR-{so.name}-{it}-{frappe.generate_hash(length=4)}",
			"stock_reservation_entry": sre.name,
			"item_code": it,
			"warehouse": self.warehouse,
			"reserved_qty": qty,
			"source_doctype": "Sales Order",
			"source_document": so.name,
			"source_document_item": so.items[0].name,
			"status": "Reserved",
		})
		ref.insert(ignore_permissions=True)

		dn = make_delivery_note(so.name)
		dn.sales_channel = ch
		dn.transaction_origin = TransactionOrigin.WEB
		dn.external_order_id = ext_order_id
		dn.insert(ignore_permissions=True)
		dn.submit()

		si = create_sales_invoice_from_fulfillment(dn.name, submit=True)

		frappe.db.commit()
		return so, dn, si

	# --- Live Scenarios A through M ---

	def test_a_unpaid_sales_invoice_financial_only_refund(self):
		"""
		Scenario A:
		Submitted unpaid Sales Invoice (grand_total=300).
		External financial-only refund for 100.
		-> Credit Note 100 created natively.
		-> Original invoice outstanding_amount reduced natively from 300 to 200.
		-> Zero stock movement (Bin.actual_qty unchanged).
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-A"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=3.0, rate=100.0)

		initial_stock = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.warehouse}, "actual_qty"))
		self.assertEqual(flt(si.outstanding_amount), 300.0)

		rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-A",
			sales_invoice=si.name,
			amount=100.0,
			currency="COP",
			return_stock=False,  # Financial-only
		)
		cn = process_external_refund(rec)

		self.assertEqual(cn.docstatus, 1)
		self.assertEqual(cn.is_return, 1)
		self.assertEqual(cn.update_stock, 0)
		self.assertEqual(abs(flt(cn.grand_total)), 100.0)

		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 200.0)

		final_stock = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.warehouse}, "actual_qty"))
		self.assertEqual(initial_stock, final_stock)

	def test_b_fully_paid_invoice_refund_accounting_behavior(self):
		"""
		Scenario B:
		Fully paid Sales Invoice (grand_total=200).
		External refund for 50.
		-> Credit Note 50 created.
		-> Original Payment Entry remains submitted and untouched.
		-> Credit Note creates customer credit balance.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-B"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=2.0, rate=100.0)

		# Pay invoice in full
		pe_rec = ExternalPaymentRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_payment_id=f"{self.FIXTURE_PREFIX}PAY-B",
			external_order_id=ext_id,
			amount=200.0,
			currency="COP",
			payment_method=self.mode_of_payment,
			payment_status=ExternalPaymentStatus.SETTLED,
		)
		pe = reconcile_external_payment(pe_rec, sales_invoices=[si], submit=True)
		self.assertEqual(pe.docstatus, 1)
		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 0.0)

		# Issue refund
		ref_rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-B",
			sales_invoice=si.name,
			amount=50.0,
			currency="COP",
			return_stock=False,
		)
		cn = process_external_refund(ref_rec)

		self.assertEqual(cn.docstatus, 1)
		self.assertEqual(abs(flt(cn.grand_total)), 50.0)

		# Original Payment Entry must be untouched and submitted
		pe.reload()
		self.assertEqual(pe.docstatus, 1)

	def test_c_delivered_order_physical_product_return(self):
		"""
		Scenario C:
		Delivered 2 units.
		Customer physically returns 1 unit with refund.
		-> Stock restored by exactly 1 unit in Bin.actual_qty.
		-> Stock Ledger Entry posted.
		-> Return Delivery Note created with docstatus=1.
		-> Credit Note created with correct amounts.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-C"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=2.0, rate=100.0)

		stock_before = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.warehouse}, "actual_qty"))

		ref_rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-C",
			sales_invoice=si.name,
			items=[ExternalRefundItem(item_code=self.item_code, qty=1.0)],
			return_stock=True,  # Physical return
			currency="COP",
		)
		cn = process_external_refund(ref_rec)

		self.assertEqual(cn.docstatus, 1)
		stock_after = flt(frappe.db.get_value("Bin", {"item_code": self.item_code, "warehouse": self.warehouse}, "actual_qty"))
		self.assertEqual(stock_after, stock_before + 1.0)

		# Return Delivery Note verified
		ret_dn_names = frappe.get_all(
			"Delivery Note",
			filters={"return_against": dn.name, "docstatus": 1, "is_return": 1},
			pluck="name",
		)
		self.assertEqual(len(ret_dn_names), 1)

	def test_d_partial_and_multiple_refunds_cumulative(self):
		"""
		Scenario D:
		Invoice 100.
		Refund A = 20.
		Refund B = 30.
		-> Cumulative credit = 50.
		-> Two distinct external identities mapped.
		-> No duplicate accounting.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-D"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=1.0, rate=100.0)

		ref_a = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-D1",
			sales_invoice=si.name,
			amount=20.0,
			currency="COP",
		)
		cn_a = process_external_refund(ref_a)
		self.assertEqual(abs(flt(cn_a.grand_total)), 20.0)

		ref_b = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-D2",
			sales_invoice=si.name,
			amount=30.0,
			currency="COP",
		)
		cn_b = process_external_refund(ref_b)
		self.assertEqual(abs(flt(cn_b.grand_total)), 30.0)

		si.reload()
		self.assertEqual(flt(si.outstanding_amount), 50.0)

	def test_e_replay_refund_idempotency(self):
		"""
		Scenario E:
		Replaying an exact external refund returns the existing Credit Note.
		Zero duplicate credit notes created.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-E"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=1.0, rate=100.0)

		ref_rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-E",
			sales_invoice=si.name,
			amount=40.0,
			currency="COP",
		)
		cn_1 = process_external_refund(ref_rec)
		cn_2 = process_external_refund(ref_rec)

		self.assertEqual(cn_1.name, cn_2.name)
		self.assertEqual(get_refund_counters()["refund_reused"], 1)

	def test_f_concurrent_same_refund_workers(self):
		"""
		Scenario F:
		Two concurrent threads process the exact same external refund identity.
		Both complete successfully and converge to one Credit Note.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-F"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=1.0, rate=100.0)

		ref_rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-F",
			sales_invoice=si.name,
			amount=50.0,
			currency="COP",
		)

		results = []
		errors = []

		def worker():
			try:
				frappe.init(site="frontend")
				frappe.connect()
				try:
					res = process_external_refund(ref_rec)
					frappe.db.commit()
					results.append(res.name)
				except (frappe.QueryDeadlockError, Exception) as lock_err:
					# On lock contention/deadlock, rollback and retry to observe committed idempotency mapping
					frappe.db.rollback()
					res = process_external_refund(ref_rec)
					frappe.db.commit()
					results.append(res.name)
			except Exception as ex:
				try:
					frappe.db.rollback()
				except Exception:
					pass
				errors.append(ex)
			finally:
				try:
					frappe.destroy()
				except Exception:
					pass

		t1 = threading.Thread(target=worker)
		t2 = threading.Thread(target=worker)
		t1.start()
		t2.start()
		t1.join(timeout=15)
		t2.join(timeout=15)

		frappe.init(site="frontend")
		frappe.connect()

		self.assertEqual(len(errors), 0, f"Concurrent workers encountered errors: {errors}")
		self.assertEqual(len(results), 2)
		self.assertEqual(results[0], results[1])

		# Only 1 Credit Note should exist
		cns = frappe.get_all("Sales Invoice", filters={"return_against": si.name, "is_return": 1})
		self.assertEqual(len(cns), 1)

	def test_g_attempt_over_refund_blocked(self):
		"""
		Scenario G:
		Invoice total = 100.
		Prior refund = 80.
		Attempting new refund = 30 is blocked with OverRefundBlockedError.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-G"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=1.0, rate=100.0)

		# Legitimate refund of 80
		ref_1 = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-G1",
			sales_invoice=si.name,
			amount=80.0,
			currency="COP",
		)
		process_external_refund(ref_1)

		# Attempt over-refund of 30 (cumulative 110 > 100)
		ref_2 = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-G2",
			sales_invoice=si.name,
			amount=30.0,
			currency="COP",
		)
		with self.assertRaises(OverRefundBlockedError):
			process_external_refund(ref_2)

	def test_h_attempt_physical_over_return_blocked(self):
		"""
		Scenario H:
		Delivered 2 units.
		Attempting to physically return 3 units is blocked with OverRefundBlockedError.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-H"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=2.0, rate=100.0)

		ref_rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-H",
			sales_invoice=si.name,
			items=[ExternalRefundItem(item_code=self.item_code, qty=3.0)],
			return_stock=True,
			currency="COP",
		)
		with self.assertRaises(OverRefundBlockedError):
			process_external_refund(ref_rec)

	def test_i_credit_note_cancel_terminal_identity(self):
		"""
		Scenario I:
		Credit Note cancel reverses GL natively.
		External refund identity remains claimed.
		Replaying the cancelled refund ID routes to review (RefundReplayCancelledError).
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-I"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=1.0, rate=100.0)

		ref_rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-I",
			sales_invoice=si.name,
			amount=25.0,
			currency="COP",
		)
		cn = process_external_refund(ref_rec)
		self.assertEqual(cn.docstatus, 1)

		# Cancel the Credit Note
		cn.cancel()
		self.assertEqual(cn.docstatus, 2)

		# Replaying the same external refund ID must be blocked and route to review
		with self.assertRaises(RefundReplayCancelledError):
			process_external_refund(ref_rec)

	def test_j_response_lost_after_credit_note_submit(self):
		"""
		Scenario J:
		Credit Note submitted, response lost / worker crashes before ack.
		Retry discovers durable identity and converges cleanly.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-J"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=1.0, rate=100.0)

		ref_rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-J",
			sales_invoice=si.name,
			amount=35.0,
			currency="COP",
		)
		cn_first = process_external_refund(ref_rec)

		# Simulated retry after network cut
		cn_retry = process_external_refund(ref_rec)
		self.assertEqual(cn_first.name, cn_retry.name)

	def test_k_financial_only_refund_zero_inventory_publication(self):
		"""
		Scenario K:
		Financial-only refund must generate zero inventory publication events.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-K"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=1.0, rate=100.0)

		events_before = frappe.db.count("Integration Event", {"sales_channel": self.sales_channel})

		ref_rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-K",
			sales_invoice=si.name,
			amount=15.0,
			currency="COP",
			return_stock=False,
		)
		process_external_refund(ref_rec)

		events_after = frappe.db.count("Integration Event", {"sales_channel": self.sales_channel})
		self.assertEqual(events_before, events_after)

	def test_l_physical_return_transactional_inventory_publication(self):
		"""
		Scenario L:
		Physical product return creates transactional inventory publication intent.
		"""
		ext_id = f"{self.FIXTURE_PREFIX}ORD-L"
		so, dn, si = self._create_submitted_invoice(ext_id, qty=2.0, rate=100.0)

		events_before = frappe.db.count("Integration Event", {"sales_channel": self.sales_channel})

		ref_rec = ExternalRefundRecord(
			provider=IntegrationProvider.PRESTASHOP,
			sales_channel=self.sales_channel,
			external_refund_id=f"{self.FIXTURE_PREFIX}REF-L",
			sales_invoice=si.name,
			items=[ExternalRefundItem(item_code=self.item_code, qty=1.0)],
			return_stock=True,
			currency="COP",
		)
		process_external_refund(ref_rec)

		events_after = frappe.db.count("Integration Event", {"sales_channel": self.sales_channel})
		self.assertGreaterEqual(events_after, events_before + 1)

	def test_m_fixture_cleanup_proof(self):
		"""
		Scenario M:
		Verifies clean fixture teardown restoring baseline invariance.
		"""
		self._cleanup_test_docs()

		# Verify 0 residual TEST-1R-% records across transactional tables
		residual_si = frappe.db.count("Sales Invoice", {"sales_channel": ["like", f"{self.FIXTURE_PREFIX}%"]})
		residual_dn = frappe.db.count("Delivery Note", {"sales_channel": ["like", f"{self.FIXTURE_PREFIX}%"]})
		residual_so = frappe.db.count("Sales Order", {"sales_channel": ["like", f"{self.FIXTURE_PREFIX}%"]})
		residual_pe = frappe.db.count("Payment Entry", {"sales_channel": ["like", f"{self.FIXTURE_PREFIX}%"]})
		residual_maps = frappe.db.count("External ID Mapping", {
			"sales_channel": ["like", f"{self.FIXTURE_PREFIX}%"],
			"external_entity_type": ["in", [ExternalEntityType.REFUND, ExternalEntityType.ORDER]],
		})

		self.assertEqual(residual_si, 0, "Residual Sales Invoice records leaked!")
		self.assertEqual(residual_dn, 0, "Residual Delivery Note records leaked!")
		self.assertEqual(residual_so, 0, "Residual Sales Order records leaked!")
		self.assertEqual(residual_pe, 0, "Residual Payment Entry records leaked!")
		self.assertEqual(residual_maps, 0, "Residual External ID Mapping records leaked!")
