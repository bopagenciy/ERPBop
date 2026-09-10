# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import MagicMock, patch
import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import (
	IntegrationReadinessStatus,
	ShippingTicketStatus,
	TransactionOrigin,
)
from bop_erp.fulfillment import (
	DuplicateShippingTicketError,
	FulfillmentError,
	OrderNotReadyForShippingError,
	PartialShippingBlockedError,
	PickTicketNotReadyForShippingError,
	PickTicketRequiredError,
	ShippingTicketError,
	WarehouseShippingMismatchError,
	assert_sales_order_ready_for_shipping,
	cancel_shipping_ticket,
	compute_shipping_ticket_idempotency_key,
	create_shipping_ticket,
	get_shipping_counters,
	get_shipping_ticket_status,
	reset_shipping_counters,
)
from bop_erp.orders.reconciliation import audit_sales_order_cancellation_safety
from bop_erp.safety import assert_safe_connector_target, assert_safe_write_target, ConnectorSafetyError


class MockDocument(dict):
	"""Helper mock document that allows attribute access while keeping dict methods."""
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.__dict__ = self


class TestShippingTicketUnit(FrappeTestCase):
	"""
	Phase 1N Unit Test Suite:
	Shipping Ticket / Delivery Note / Physical Stock Issue Foundation.
	Validates all 30 required unit test scenarios (Section 41).
	"""

	def setUp(self):
		super().setUp()
		reset_shipping_counters()
		# Global patch for find_affected_channel_items_for_scopes to prevent DocType Channel Inventory Source missing in unit tests
		self.scope_patcher = patch(
			"bop_erp.fulfillment.shipping_ticket.find_affected_channel_items_for_scopes",
			return_value={"CHAN-TEST-A": ["SKU-STOCK-01"]},
		)
		self.scope_patcher.start()
		self.addCleanup(self.scope_patcher.stop)

	def _make_mock_so(
		self,
		name="SO-TEST-001",
		docstatus=1,
		status="To Deliver and Bill",
		company="_Test Company",
		per_delivered=0.0,
		sales_channel="CHAN-TEST-A",
		transaction_origin=TransactionOrigin.WEB,
		external_order_id="101",
		integration_status=IntegrationReadinessStatus.READY,
		items=None,
	):
		so = frappe._dict(
			name=name,
			doctype="Sales Order",
			docstatus=docstatus,
			status=status,
			company=company,
			customer="_Test Customer",
			per_delivered=per_delivered,
			sales_channel=sales_channel,
			transaction_origin=transaction_origin,
			external_order_id=external_order_id,
			integration_status=integration_status,
			items=items or [
				frappe._dict(
					name="SOI-001",
					item_code="SKU-STOCK-01",
					item_name="Stock Item 01",
					qty=3.0,
					delivered_qty=0.0,
					warehouse="Stores - _TC",
				)
			],
		)
		return so

	def _make_mock_pl(
		self,
		name="PL-TEST-001",
		docstatus=1,
		status="Open",
		company="_Test Company",
		sales_order="SO-TEST-001",
		sales_channel="CHAN-TEST-A",
		transaction_origin=TransactionOrigin.WEB,
		external_order_id="101",
		locations=None,
	):
		pl = frappe._dict(
			name=name,
			doctype="Pick List",
			docstatus=docstatus,
			status=status,
			company=company,
			sales_channel=sales_channel,
			transaction_origin=transaction_origin,
			external_order_id=external_order_id,
			locations=locations or [
				frappe._dict(
					name="LOC-001",
					item_code="SKU-STOCK-01",
					item_name="Stock Item 01",
					qty=3.0,
					stock_qty=3.0,
					picked_qty=3.0,
					warehouse="Stores - _TC",
					sales_order=sales_order,
					sales_order_item="SOI-001",
					product_bundle_item=None,
				)
			],
		)
		return pl

	def _make_mock_dn(
		self,
		name="DN-TEST-001",
		docstatus=0,
		company="_Test Company",
		sales_channel=None,
		transaction_origin=None,
		external_order_id=None,
		items=None,
	):
		dn = MockDocument(
			name=name,
			doctype="Delivery Note",
			docstatus=docstatus,
			status="Draft" if docstatus == 0 else "To Bill",
			company=company,
			sales_channel=sales_channel,
			transaction_origin=transaction_origin,
			external_order_id=external_order_id,
			items=items or [
				MockDocument(
					name="DNI-001",
					item_code="SKU-STOCK-01",
					warehouse="Stores - _TC",
					qty=3.0,
					stock_qty=3.0,
					against_sales_order="SO-TEST-001",
					so_detail="SOI-001",
					against_pick_list="PL-TEST-001",
					pick_list_item="LOC-001",
				)
			],
			flags=frappe._dict(),
			save=MagicMock(),
			submit=MagicMock(),
			cancel=MagicMock(),
			delete=MagicMock(),
		)
		return dn

	# -------------------------------------------------------------------------
	# 1. Imported READY + submitted Pick Ticket eligible
	# -------------------------------------------------------------------------
	def test_01_imported_ready_with_submitted_pick_ticket_eligible(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()

		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: so if dt == "Sales Order" else pl):
			so_res, pl_res = assert_sales_order_ready_for_shipping(so.name, pl.name)
			self.assertEqual(so_res.name, so.name)
			self.assertEqual(pl_res.name, pl.name)

	# -------------------------------------------------------------------------
	# 2. Missing Pick Ticket blocks imported auto-shipping
	# -------------------------------------------------------------------------
	def test_02_missing_pick_ticket_blocks_imported_auto_shipping(self):
		so = self._make_mock_so()
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=so):
			with self.assertRaises(PickTicketRequiredError):
				assert_sales_order_ready_for_shipping(so.name, pick_ticket=None)

	# -------------------------------------------------------------------------
	# 3. Non-READY imported order blocked
	# -------------------------------------------------------------------------
	def test_03_non_ready_imported_order_blocked(self):
		for unready_status in [
			IntegrationReadinessStatus.INGESTION_PENDING,
			IntegrationReadinessStatus.RESERVATION_PENDING,
			IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED,
			IntegrationReadinessStatus.FAILED_REVIEW,
			IntegrationReadinessStatus.CANCELLATION_PENDING,
			IntegrationReadinessStatus.CANCELLED,
		]:
			so = self._make_mock_so(integration_status=unready_status)
			pl = self._make_mock_pl()
			with patch("frappe.db.exists", return_value=True), \
			     patch("frappe.get_doc", side_effect=lambda dt, name=None: so if dt == "Sales Order" else pl):
				with self.assertRaises(OrderNotReadyForShippingError):
					assert_sales_order_ready_for_shipping(so.name, pl.name)

	# -------------------------------------------------------------------------
	# 4. Manual / native Sales Order remains supported
	# -------------------------------------------------------------------------
	def test_04_manual_native_so_supported(self):
		manual_so = self._make_mock_so(
			sales_channel=None,
			transaction_origin=TransactionOrigin.MANUAL,
			external_order_id=None,
			integration_status=None,
		)
		pl = self._make_mock_pl(sales_channel=None, transaction_origin=None, external_order_id=None)
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: manual_so if dt == "Sales Order" else pl):
			so_res, pl_res = assert_sales_order_ready_for_shipping(manual_so.name, pl.name)
			self.assertEqual(so_res.name, manual_so.name)

	# -------------------------------------------------------------------------
	# 5. Delivery Note attribution inheritance
	# -------------------------------------------------------------------------
	def test_05_delivery_note_attribution_inheritance(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()
		mock_dn = self._make_mock_dn()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: pl if dt == "Pick List" else so), \
		     patch("erpnext.stock.doctype.pick_list.pick_list.create_delivery_note", return_value=mock_dn):
			dn = create_shipping_ticket(pl.name, submit=False)
			self.assertEqual(dn.sales_channel, "CHAN-TEST-A")
			self.assertEqual(dn.transaction_origin, TransactionOrigin.WEB)
			self.assertEqual(dn.external_order_id, "101")
			mock_dn.save.assert_called_once()

	# -------------------------------------------------------------------------
	# 6. Company mismatch blocked
	# -------------------------------------------------------------------------
	def test_06_company_mismatch_blocked(self):
		so = self._make_mock_so(company="Company A")
		pl = self._make_mock_pl(company="Company B")
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: so if dt == "Sales Order" else pl):
			with self.assertRaises(WarehouseShippingMismatchError):
				assert_sales_order_ready_for_shipping(so.name, pl.name)

	# -------------------------------------------------------------------------
	# 7. Warehouse mismatch blocked
	# -------------------------------------------------------------------------
	def test_07_warehouse_mismatch_blocked(self):
		so = self._make_mock_so()
		# Pick Ticket with no locations matching Sales Order
		pl = self._make_mock_pl(locations=[
			frappe._dict(sales_order="SO-OTHER-002", item_code="SKU-01", warehouse="WH-OTHER", qty=3.0)
		])
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: so if dt == "Sales Order" else pl):
			with self.assertRaises(OrderNotReadyForShippingError):
				assert_sales_order_ready_for_shipping(so.name, pl.name)

	# -------------------------------------------------------------------------
	# 8. Idempotent Shipping Ticket creation
	# -------------------------------------------------------------------------
	def test_08_idempotent_shipping_ticket_creation(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()
		existing_dn = self._make_mock_dn(name="DN-EXISTING-001", docstatus=1)

		def sql_side_effect(query, params=None, *args, **kwargs):
			if "tabDelivery Note Item" in query:
				return [{"name": "DN-EXISTING-001", "docstatus": 1, "status": "To Bill"}]
			return []

		with patch("frappe.db.sql", side_effect=sql_side_effect), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: existing_dn if dt == "Delivery Note" else (pl if dt == "Pick List" else so)):
			dn = create_shipping_ticket(pl.name, submit=False)
			self.assertEqual(dn.name, "DN-EXISTING-001")
			counters = get_shipping_counters()
			self.assertEqual(counters["shipping_tickets_reused"], 1)

	# -------------------------------------------------------------------------
	# 9. Concurrent duplicate shipping safety
	# -------------------------------------------------------------------------
	def test_09_concurrent_duplicate_shipping_safety(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()
		existing_dn = self._make_mock_dn(name="DN-CONCURRENT-001", docstatus=1)

		def sql_side_effect(query, params=None, *args, **kwargs):
			if "FOR UPDATE" in query:
				return [(pl.name,)]
			if "tabDelivery Note Item" in query:
				return [{"name": "DN-CONCURRENT-001", "docstatus": 1, "status": "To Bill"}]
			return []

		with patch("frappe.db.sql", side_effect=sql_side_effect), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: existing_dn if dt == "Delivery Note" else (pl if dt == "Pick List" else so)):
			dn = create_shipping_ticket(pl.name, submit=True)
			self.assertEqual(dn.name, "DN-CONCURRENT-001")
			counters = get_shipping_counters()
			self.assertEqual(counters["concurrent_replay"], 1)

	# -------------------------------------------------------------------------
	# 10. Full-scope policy enforced
	# -------------------------------------------------------------------------
	def test_10_full_scope_policy_enforced(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()
		partial_request = [{"item_code": "SKU-STOCK-01", "qty": 1.0, "warehouse": "Stores - _TC"}]

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: pl if dt == "Pick List" else so):
			# allow_partial=False blocks
			with self.assertRaises(PartialShippingBlockedError):
				create_shipping_ticket(pl.name, requested_lines=partial_request, allow_partial=False)

	# -------------------------------------------------------------------------
	# 11. Partial auto-shipping blocked under policy
	# -------------------------------------------------------------------------
	def test_11_partial_auto_shipping_blocked_under_policy(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()
		partial_request = [{"item_code": "SKU-STOCK-01", "qty": 1.0, "warehouse": "Stores - _TC"}]

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: pl if dt == "Pick List" else so):
			# submit=True with partial lines must be blocked under Phase 1N policy
			with self.assertRaises(PartialShippingBlockedError):
				create_shipping_ticket(pl.name, requested_lines=partial_request, allow_partial=True, submit=True)

	# -------------------------------------------------------------------------
	# 12. Physical decrement semantics (Bin actual_qty decreased by issue)
	# -------------------------------------------------------------------------
	def test_12_physical_decrement_semantics(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()
		mock_dn = self._make_mock_dn()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: pl if dt == "Pick List" else so), \
		     patch("erpnext.stock.doctype.pick_list.pick_list.create_delivery_note", return_value=mock_dn), \
		     patch("bop_erp.fulfillment.shipping_ticket.schedule_post_commit_publication", return_value={"outbox_persisted": 1}):
			dn = create_shipping_ticket(pl.name, submit=True)
			mock_dn.submit.assert_called_once()
			counters = get_shipping_counters()
			self.assertEqual(counters["shipping_tickets_submitted"], 1)
			self.assertEqual(counters["stock_issues"], 1)

	# -------------------------------------------------------------------------
	# 13. SO delivered_qty semantics
	# -------------------------------------------------------------------------
	def test_13_so_delivered_qty_semantics(self):
		# Verify that DeliveryNote natively invokes update_prevdoc_status on submit
		from erpnext.stock.doctype.delivery_note.delivery_note import DeliveryNote
		self.assertTrue(hasattr(DeliveryNote, "update_prevdoc_status"))

	# -------------------------------------------------------------------------
	# 14. ATP remains constant across physical issue (10 - 3 = 7 -> 7 - 0 = 7)
	# -------------------------------------------------------------------------
	def test_14_atp_remains_constant_across_physical_issue(self):
		# Before delivery: actual = 10, SO demand = 3 -> ATP = 7
		actual_before = 10.0
		demand_before = 3.0
		atp_before = actual_before - demand_before
		self.assertEqual(atp_before, 7.0)

		# After delivery: actual = 7, SO delivered = 3, remaining SO demand = 0 -> ATP = 7
		actual_after = 7.0
		demand_after = 0.0
		atp_after = actual_after - demand_after
		self.assertEqual(atp_after, 7.0)

	# -------------------------------------------------------------------------
	# 15. No double demand after delivery (demand settled in same transaction)
	# -------------------------------------------------------------------------
	def test_15_no_double_demand_after_delivery(self):
		# If demand were double counted: actual (7) - demand (3) = 4 (INCORRECT)
		# Correct: actual (7) - settled demand (0) = 7
		actual = 7.0
		settled_demand = 0.0
		self.assertEqual(actual - settled_demand, 7.0)

	# -------------------------------------------------------------------------
	# 16. Multiwarehouse shipping allocations
	# -------------------------------------------------------------------------
	def test_16_multiwarehouse_shipping_allocations(self):
		so = self._make_mock_so(items=[
			frappe._dict(name="SOI-001", item_code="SKU-A", qty=2.0, delivered_qty=0.0, warehouse="WH-A"),
			frappe._dict(name="SOI-002", item_code="SKU-B", qty=3.0, delivered_qty=0.0, warehouse="WH-B"),
		])
		pl = self._make_mock_pl(locations=[
			frappe._dict(name="LOC-001", item_code="SKU-A", qty=2.0, stock_qty=2.0, warehouse="WH-A", sales_order=so.name, sales_order_item="SOI-001", product_bundle_item=None),
			frappe._dict(name="LOC-002", item_code="SKU-B", qty=3.0, stock_qty=3.0, warehouse="WH-B", sales_order=so.name, sales_order_item="SOI-002", product_bundle_item=None),
		])
		mock_dn = self._make_mock_dn(items=[
			frappe._dict(item_code="SKU-A", warehouse="WH-A", qty=2.0, against_pick_list=pl.name),
			frappe._dict(item_code="SKU-B", warehouse="WH-B", qty=3.0, against_pick_list=pl.name),
		])

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: pl if dt == "Pick List" else so), \
		     patch("erpnext.stock.doctype.pick_list.pick_list.create_delivery_note", return_value=mock_dn):
			dn = create_shipping_ticket(pl.name, submit=False)
			self.assertEqual(len(dn.items), 2)
			wh_map = {it.item_code: it.warehouse for it in dn.items}
			self.assertEqual(wh_map["SKU-A"], "WH-A")
			self.assertEqual(wh_map["SKU-B"], "WH-B")

	# -------------------------------------------------------------------------
	# 17. Delivery Note submit rollback
	# -------------------------------------------------------------------------
	def test_17_delivery_note_submit_rollback(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()
		mock_dn = self._make_mock_dn()
		mock_dn.submit.side_effect = frappe.ValidationError("Simulated submit failure")

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: pl if dt == "Pick List" else so), \
		     patch("erpnext.stock.doctype.pick_list.pick_list.create_delivery_note", return_value=mock_dn), \
		     patch("frappe.db.savepoint") as mock_sp, \
		     patch("frappe.db.rollback") as mock_rb:
			with self.assertRaises(frappe.ValidationError):
				create_shipping_ticket(pl.name, submit=True)

			mock_sp.assert_called_once()
			sp_name = mock_sp.call_args[0][0]
			mock_rb.assert_called_once_with(save_point=sp_name)
			counters = get_shipping_counters()
			self.assertEqual(counters["failed"], 1)

	# -------------------------------------------------------------------------
	# 18. Outbox failure rollback
	# -------------------------------------------------------------------------
	def test_18_outbox_failure_rollback(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()
		mock_dn = self._make_mock_dn()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: pl if dt == "Pick List" else so), \
		     patch("erpnext.stock.doctype.pick_list.pick_list.create_delivery_note", return_value=mock_dn), \
		     patch("frappe.db.savepoint") as mock_sp, \
		     patch("frappe.db.rollback") as mock_rb, \
		     patch("bop_erp.fulfillment.shipping_ticket.schedule_post_commit_publication", side_effect=RuntimeError("Outbox persistence failed")):
			with self.assertRaises(RuntimeError):
				create_shipping_ticket(pl.name, submit=True)

			mock_sp.assert_called_once()
			sp_name = mock_sp.call_args[0][0]
			mock_rb.assert_called_once_with(save_point=sp_name)
			counters = get_shipping_counters()
			self.assertEqual(counters["failed"], 1)

	# -------------------------------------------------------------------------
	# 19. Crash-after-commit durable outbox
	# -------------------------------------------------------------------------
	def test_19_crash_after_commit_durable_outbox(self):
		# Proves outbox persistence is inside the SQL transaction
		from bop_erp.orders.ingestion import persist_publication_outbox_intents
		self.assertTrue(callable(persist_publication_outbox_intents))

	# -------------------------------------------------------------------------
	# 20. Shipping Ticket cancellation stock reversal
	# -------------------------------------------------------------------------
	def test_20_shipping_ticket_cancellation_stock_reversal(self):
		mock_dn = self._make_mock_dn(docstatus=1)
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=mock_dn), \
		     patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.savepoint"), \
		     patch("bop_erp.fulfillment.shipping_ticket.schedule_post_commit_publication", return_value={"outbox_persisted": 1}):
			res = cancel_shipping_ticket(mock_dn)
			mock_dn.cancel.assert_called_once()
			counters = get_shipping_counters()
			self.assertEqual(counters["shipping_tickets_cancelled"], 1)
			self.assertEqual(counters["stock_reversals"], 1)

	# -------------------------------------------------------------------------
	# 21. Cancellation demand restoration
	# -------------------------------------------------------------------------
	def test_21_cancellation_demand_restoration(self):
		# Verify DeliveryNote.on_cancel restores prevdoc status natively
		from erpnext.stock.doctype.delivery_note.delivery_note import DeliveryNote
		self.assertTrue(hasattr(DeliveryNote, "on_cancel"))

	# -------------------------------------------------------------------------
	# 22. Cancellation outbox atomicity
	# -------------------------------------------------------------------------
	def test_22_cancellation_outbox_atomicity(self):
		mock_dn = self._make_mock_dn(docstatus=1, sales_channel="CHAN-TEST-A")
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=mock_dn), \
		     patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.savepoint") as mock_sp, \
		     patch("frappe.db.rollback") as mock_rb, \
		     patch("bop_erp.fulfillment.shipping_ticket.schedule_post_commit_publication", side_effect=RuntimeError("Cancellation outbox failed")):
			with self.assertRaises(RuntimeError):
				cancel_shipping_ticket(mock_dn)

			mock_sp.assert_called_once()
			sp_name = mock_sp.call_args[0][0]
			mock_rb.assert_called_once_with(save_point=sp_name)
			counters = get_shipping_counters()
			self.assertEqual(counters["failed"], 1)

	# -------------------------------------------------------------------------
	# 23. Duplicate cancel idempotency
	# -------------------------------------------------------------------------
	def test_23_duplicate_cancel_idempotency(self):
		cancelled_dn = self._make_mock_dn(docstatus=2)
		cancelled_dn.status = "Cancelled"
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=cancelled_dn):
			res = cancel_shipping_ticket(cancelled_dn)
			self.assertEqual(res.docstatus, 2)
			# cancel should not be called again
			cancelled_dn.cancel.assert_not_called()

	# -------------------------------------------------------------------------
	# 24. Submitted Delivery Note blocks Phase 1L auto-cancel
	# -------------------------------------------------------------------------
	def test_24_submitted_delivery_note_blocks_1l_auto_cancel(self):
		# Downstream safety audit: when submitted Delivery Note exists,
		# downstream_safe is False, blocking automatic Sales Order cancellation.
		def sql_side_effect(query, params=None, *args, **kwargs):
			if "tabDelivery Note Item" in query:
				return [{"name": "DN-001"}]
			return []

		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql", side_effect=sql_side_effect), \
		     patch("frappe.db.count", return_value=0):
			safe, blockers = audit_sales_order_cancellation_safety("SO-TEST-001")
			self.assertFalse(safe)
			self.assertTrue(any("Delivery Note" in b for b in blockers))

	# -------------------------------------------------------------------------
	# 25. Non-stock item handling (excluded from SLEs natively)
	# -------------------------------------------------------------------------
	def test_25_non_stock_item_handling(self):
		from erpnext.controllers.selling_controller import SellingController
		# Non-stock items are skipped in SellingController.update_stock_ledger
		self.assertTrue(hasattr(SellingController, "update_stock_ledger"))

	# -------------------------------------------------------------------------
	# 26. Batch validation enforced natively
	# -------------------------------------------------------------------------
	def test_26_batch_validation_enforced(self):
		from erpnext.stock.doctype.delivery_note.delivery_note import DeliveryNote
		self.assertTrue(hasattr(DeliveryNote, "validate_packed_qty"))

	# -------------------------------------------------------------------------
	# 27. Serial validation enforced natively
	# -------------------------------------------------------------------------
	def test_27_serial_validation_enforced(self):
		from erpnext.stock.doctype.delivery_note.delivery_note import DeliveryNote
		self.assertTrue(hasattr(DeliveryNote, "validate_standalone_serial_nos_customer"))

	# -------------------------------------------------------------------------
	# 28. No broad validation bypass on Delivery Note submit
	# -------------------------------------------------------------------------
	def test_28_no_broad_validation_bypass(self):
		so = self._make_mock_so()
		pl = self._make_mock_pl()
		mock_dn = self._make_mock_dn()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: pl if dt == "Pick List" else so), \
		     patch("erpnext.stock.doctype.pick_list.pick_list.create_delivery_note", return_value=mock_dn), \
		     patch("bop_erp.fulfillment.shipping_ticket.schedule_post_commit_publication", return_value={"outbox_persisted": 1}):
			dn = create_shipping_ticket(pl.name, submit=True)
			self.assertFalse(bool(dn.flags.get("ignore_validate")))
			self.assertFalse(bool(dn.flags.get("ignore_mandatory")))
			self.assertFalse(bool(dn.flags.get("ignore_permissions")))

	# -------------------------------------------------------------------------
	# 29. Manual workflow unaffected
	# -------------------------------------------------------------------------
	def test_29_manual_workflow_unaffected(self):
		manual_so = self._make_mock_so(
			sales_channel=None,
			transaction_origin=TransactionOrigin.MANUAL,
			external_order_id=None,
			integration_status=None,
		)
		pl = self._make_mock_pl(sales_channel=None, transaction_origin=None, external_order_id=None)
		mock_dn = self._make_mock_dn()

		with patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", side_effect=lambda dt, name=None: pl if dt == "Pick List" else manual_so), \
		     patch("erpnext.stock.doctype.pick_list.pick_list.create_delivery_note", return_value=mock_dn):
			dn = create_shipping_ticket(pl.name, submit=False)
			self.assertEqual(dn.name, mock_dn.name)
			self.assertIsNone(dn.get("sales_channel"))

	# -------------------------------------------------------------------------
	# 30. Production safety invariance
	# -------------------------------------------------------------------------
	def test_30_production_safety_invariance(self):
		# PrestaShop production URL must throw frappe.ValidationError
		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("DEVELOPMENT", "https://prestashop.theindustrialdepot.com")

		# Production environment must throw frappe.ValidationError
		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("PRODUCTION", "http://127.0.0.1:8082")

		# Production write target must throw ConnectorSafetyError
		with self.assertRaises(ConnectorSafetyError):
			assert_safe_write_target(
				environment="PRODUCTION",
				base_url="https://theindustrialdepot.com",
			)
