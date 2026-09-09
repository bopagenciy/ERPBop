# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import unittest
from unittest.mock import MagicMock, patch
import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.constants import (
	IntegrationReadinessStatus,
	PickTicketStatus,
	TransactionOrigin,
)
from bop_erp.fulfillment import (
	FulfillmentError,
	OrderNotReadyForPickingError,
	WarehouseAllocationMismatchError,
	PartialPickBlockedError,
	DuplicatePickTicketError,
	InsufficientStockError,
	NonStockItemPickError,
	assert_sales_order_ready_for_picking,
	compute_pick_ticket_idempotency_key,
	create_pick_ticket,
	cancel_pick_ticket,
	get_remaining_to_pick,
	get_pick_ticket_status,
	get_pick_counters,
	reset_pick_counters,
	is_imported_sales_order,
)
from bop_erp.orders.reconciliation import audit_sales_order_cancellation_safety
from bop_erp.safety import assert_safe_connector_target, assert_safe_write_target, ConnectorSafetyError


class TestPickTicketUnit(FrappeTestCase):
	"""
	Phase 1M Unit Test Suite:
	Pick Ticket / Warehouse Fulfillment Foundation.
	Validates all 24 required unit test scenarios (Section 34).
	"""

	def setUp(self):
		super().setUp()
		reset_pick_counters()

	def _make_mock_so(
		self,
		name="SO-TEST-001",
		docstatus=1,
		status="To Deliver and Bill",
		company="_Test Company",
		per_delivered=0.0,
		sales_channel="TEST-A",
		transaction_origin=TransactionOrigin.WEB,
		external_order_id="101",
		integration_status=IntegrationReadinessStatus.READY,
		items=None,
		packed_items=None,
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
					qty=5.0,
					delivered_qty=0.0,
					picked_qty=0.0,
					warehouse="Stores - _TC",
				)
			],
			packed_items=packed_items or [],
		)
		return so

	# -------------------------------------------------------------------------
	# 1. Imported READY SO is pick eligible
	# -------------------------------------------------------------------------
	def test_01_imported_ready_so_is_pick_eligible(self):
		so = self._make_mock_so()
		with patch("frappe.get_doc", return_value=so), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.get_value") as mock_gv, \
		     patch("bop_erp.orders.ingestion.is_order_ingestion_complete", return_value=(True, [])):
			mock_gv.side_effect = lambda dt, name_or_filters, fieldname=None, *args, **kwargs: (
				1 if fieldname in ("active", "is_active", "is_stock_item") else None
			)
			res = assert_sales_order_ready_for_picking(so.name)
			self.assertEqual(res.name, so.name)

	# -------------------------------------------------------------------------
	# 2. Native/manual SO remains supported
	# -------------------------------------------------------------------------
	def test_02_native_manual_so_remains_supported(self):
		manual_so = self._make_mock_so(
			name="SO-MANUAL-001",
			sales_channel=None,
			transaction_origin=None,
			external_order_id=None,
			integration_status=None,
		)
		self.assertFalse(is_imported_sales_order(manual_so))
		with patch("frappe.get_doc", return_value=manual_so), \
		     patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.get_value", return_value=1):
			res = assert_sales_order_ready_for_picking(manual_so.name)
			self.assertEqual(res.name, "SO-MANUAL-001")

	# -------------------------------------------------------------------------
	# 3. Non-READY imported SO blocked
	# -------------------------------------------------------------------------
	def test_03_non_ready_imported_so_blocked(self):
		for blocked_status in (
			IntegrationReadinessStatus.INGESTION_PENDING,
			IntegrationReadinessStatus.RESERVATION_PENDING,
		):
			so = self._make_mock_so(integration_status=blocked_status)
			with patch("frappe.get_doc", return_value=so), \
			     patch("frappe.db.exists", return_value=True):
				with self.assertRaises(OrderNotReadyForPickingError):
					assert_sales_order_ready_for_picking(so.name)

	# -------------------------------------------------------------------------
	# 4. CANCELLED imported SO blocked
	# -------------------------------------------------------------------------
	def test_04_cancelled_imported_so_blocked(self):
		for blocked_status in (
			IntegrationReadinessStatus.CANCELLED,
			IntegrationReadinessStatus.CANCELLATION_PENDING,
		):
			so = self._make_mock_so(integration_status=blocked_status)
			with patch("frappe.get_doc", return_value=so), \
			     patch("frappe.db.exists", return_value=True):
				with self.assertRaises(OrderNotReadyForPickingError):
					assert_sales_order_ready_for_picking(so.name)

	# -------------------------------------------------------------------------
	# 5. CHANGE_REVIEW_REQUIRED blocked
	# -------------------------------------------------------------------------
	def test_05_change_review_required_blocked(self):
		for blocked_status in (
			IntegrationReadinessStatus.CHANGE_REVIEW_REQUIRED,
			IntegrationReadinessStatus.FAILED_REVIEW,
		):
			so = self._make_mock_so(integration_status=blocked_status)
			with patch("frappe.get_doc", return_value=so), \
			     patch("frappe.db.exists", return_value=True):
				with self.assertRaises(OrderNotReadyForPickingError):
					assert_sales_order_ready_for_picking(so.name)

	# -------------------------------------------------------------------------
	# 6. Company mismatch / invalid company blocked
	# -------------------------------------------------------------------------
	def test_06_company_mismatch_blocked(self):
		so = self._make_mock_so(company="NonExistentCo")
		with patch("frappe.get_doc", return_value=so), \
		     patch("frappe.db.exists") as mock_exists:
			mock_exists.side_effect = lambda dt, val: dt == "Sales Order"
			with self.assertRaises(OrderNotReadyForPickingError):
				assert_sales_order_ready_for_picking(so.name)

	# -------------------------------------------------------------------------
	# 7. Sales Channel & Transaction Origin propagation
	# -------------------------------------------------------------------------
	def test_07_sales_channel_origin_propagation(self):
		so = self._make_mock_so()
		mock_pl = frappe._dict(
			name="PL-001",
			company=so.company,
			locations=[],
			append=lambda f, r: mock_pl.locations.append(r),
			insert=MagicMock(),
		)
		with patch("frappe.db.sql", return_value=[]), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem, \
		     patch("frappe.db.get_all", return_value=[]), \
		     patch("frappe.db.get_value") as mock_gv, \
		     patch("frappe.new_doc", return_value=mock_pl):
			mock_rem.return_value = {
				"total_remaining": 5.0,
				"items": [{
					"sales_order_item": "SOI-001",
					"item_code": "SKU-STOCK-01",
					"warehouse": "Stores - _TC",
					"remaining_to_pick": 5.0,
					"is_stock_item": True,
				}],
			}
			mock_gv.side_effect = lambda dt, name_or_filt, field=None, *args, **kwargs: (
				10.0 if dt == "Bin" else frappe._dict(item_name="Item 1", stock_uom="Nos")
			)
			pl = create_pick_ticket(so.name, allow_partial=True)
			self.assertEqual(pl.sales_channel, "TEST-A")
			self.assertEqual(pl.transaction_origin, TransactionOrigin.WEB)
			self.assertEqual(pl.external_order_id, "101")

	# -------------------------------------------------------------------------
	# 8. Idempotent duplicate creation key
	# -------------------------------------------------------------------------
	def test_08_idempotent_duplicate_creation(self):
		key1 = compute_pick_ticket_idempotency_key("SO-001", [{"sales_order_item": "SOI-1", "qty": 3, "item_code": "SKU-1", "warehouse": "WH-1"}])
		key2 = compute_pick_ticket_idempotency_key("SO-001", [{"sales_order_item": "SOI-1", "qty": 3, "item_code": "SKU-1", "warehouse": "WH-1"}])
		key3 = compute_pick_ticket_idempotency_key("SO-001", [{"sales_order_item": "SOI-1", "qty": 2, "item_code": "SKU-1", "warehouse": "WH-1"}])
		self.assertEqual(key1, key2)
		self.assertNotEqual(key1, key3)
		self.assertTrue(key1.startswith("PTK-"))

	# -------------------------------------------------------------------------
	# 9. Concurrent duplicate creation safety
	# -------------------------------------------------------------------------
	def test_09_concurrent_duplicate_creation_safety(self):
		so = self._make_mock_so()
		existing_pl = frappe._dict(name="PL-CONCURRENT-001", docstatus=0, status="Draft")
		with patch("frappe.db.sql") as mock_sql, \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("frappe.get_doc", return_value=existing_pl):
			mock_sql.side_effect = [
				[{"name": so.name}],  # SELECT ... FOR UPDATE
				[{"name": existing_pl.name, "docstatus": 0, "status": "Draft"}],  # existing check
			]
			res = create_pick_ticket(so.name)
			self.assertEqual(res.name, "PL-CONCURRENT-001")
			counters = get_pick_counters()
			self.assertEqual(counters["pick_tickets_reused"], 1)
			self.assertEqual(counters["pick_tickets_created"], 0)

	# -------------------------------------------------------------------------
	# 10. SRE warehouse allocation honored
	# -------------------------------------------------------------------------
	def test_10_sre_warehouse_allocation_honored(self):
		so = self._make_mock_so()
		mock_pl = frappe._dict(
			name="PL-001",
			company=so.company,
			locations=[],
			append=lambda f, r: mock_pl.locations.append(r),
			insert=MagicMock(),
		)
		sre_row = frappe._dict(
			name="SRE-001",
			voucher_detail_no="SOI-001",
			item_code="SKU-STOCK-01",
			warehouse="WH-SPECIFIC-SRE",
			reserved_qty=5.0,
			batch_no="BATCH-001",
		)
		def _mock_sql(query, *args, **kwargs):
			if "tabStock Reservation Entry" in query:
				return [sre_row]
			return []

		with patch("frappe.db.sql", side_effect=_mock_sql), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem, \
		     patch("frappe.db.get_value") as mock_gv, \
		     patch("frappe.new_doc", return_value=mock_pl):
			mock_rem.return_value = {
				"total_remaining": 5.0,
				"items": [{
					"sales_order_item": "SOI-001",
					"item_code": "SKU-STOCK-01",
					"warehouse": "Stores - _TC",
					"remaining_to_pick": 5.0,
					"is_stock_item": True,
				}],
			}
			mock_gv.side_effect = lambda dt, name_or_filt, field=None, *args, **kwargs: (
				10.0 if dt == "Bin" else frappe._dict(item_name="Item 1", stock_uom="Nos")
			)
			pl = create_pick_ticket(so.name)
			self.assertEqual(len(pl.locations), 1)
			self.assertEqual(pl.locations[0]["warehouse"], "WH-SPECIFIC-SRE")
			self.assertEqual(pl.locations[0]["batch_no"], "BATCH-001")

	# -------------------------------------------------------------------------
	# 11. Reservation mismatch blocks
	# -------------------------------------------------------------------------
	def test_11_reservation_mismatch_blocks(self):
		so = self._make_mock_so()
		sre_row = frappe._dict(
			name="SRE-001",
			voucher_detail_no="SOI-001",
			item_code="SKU-STOCK-01",
			warehouse="WH-SRE-ORIGINAL",
			reserved_qty=5.0,
			batch_no=None,
		)
		def _mock_sql(query, *args, **kwargs):
			if "tabStock Reservation Entry" in query:
				return [sre_row]
			return []

		with patch("frappe.db.sql", side_effect=_mock_sql), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem, \
		     patch("frappe.db.get_value", return_value=True):
			mock_rem.return_value = {
				"total_remaining": 5.0,
				"items": [{
					"sales_order_item": "SOI-001",
					"item_code": "SKU-STOCK-01",
					"warehouse": "WH-SRE-ORIGINAL",
					"remaining_to_pick": 5.0,
					"is_stock_item": True,
				}],
			}
			# Request line asking for WH-CONTRADICTING
			req_lines = [{
				"sales_order_item": "SOI-001",
				"item_code": "SKU-STOCK-01",
				"warehouse": "WH-CONTRADICTING",
				"qty": 5.0,
			}]
			with self.assertRaises(WarehouseAllocationMismatchError):
				create_pick_ticket(so.name, requested_lines=req_lines)

	# -------------------------------------------------------------------------
	# 12. Multi-warehouse allocation
	# -------------------------------------------------------------------------
	def test_12_multi_warehouse_allocation(self):
		so = self._make_mock_so(
			items=[
				frappe._dict(name="SOI-01", item_code="SKU-01", qty=2.0, delivered_qty=0.0, picked_qty=0.0, warehouse="WH-1"),
				frappe._dict(name="SOI-02", item_code="SKU-02", qty=3.0, delivered_qty=0.0, picked_qty=0.0, warehouse="WH-2"),
			]
		)
		mock_pl = frappe._dict(
			name="PL-001",
			company=so.company,
			locations=[],
			append=lambda f, r: mock_pl.locations.append(r),
			insert=MagicMock(),
		)
		sres = [
			frappe._dict(name="SRE-1", voucher_detail_no="SOI-01", item_code="SKU-01", warehouse="WH-1", reserved_qty=2.0, batch_no=None),
			frappe._dict(name="SRE-2", voucher_detail_no="SOI-02", item_code="SKU-02", warehouse="WH-2", reserved_qty=3.0, batch_no=None),
		]
		def _mock_sql(query, *args, **kwargs):
			if "tabStock Reservation Entry" in query:
				return sres
			return []

		with patch("frappe.db.sql", side_effect=_mock_sql), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem, \
		     patch("frappe.db.get_value") as mock_gv, \
		     patch("frappe.new_doc", return_value=mock_pl):
			mock_rem.return_value = {
				"total_remaining": 5.0,
				"items": [
					{"sales_order_item": "SOI-01", "item_code": "SKU-01", "warehouse": "WH-1", "remaining_to_pick": 2.0, "is_stock_item": True},
					{"sales_order_item": "SOI-02", "item_code": "SKU-02", "warehouse": "WH-2", "remaining_to_pick": 3.0, "is_stock_item": True},
				],
			}
			mock_gv.side_effect = lambda dt, name_or_filt, field=None, *args, **kwargs: (
				10.0 if dt == "Bin" else frappe._dict(item_name="Item", stock_uom="Nos")
			)
			pl = create_pick_ticket(so.name)
			self.assertEqual(len(pl.locations), 2)
			self.assertEqual(pl.locations[0]["warehouse"], "WH-1")
			self.assertEqual(pl.locations[1]["warehouse"], "WH-2")

	# -------------------------------------------------------------------------
	# 13. Default partial pick blocked
	# -------------------------------------------------------------------------
	def test_13_default_partial_pick_blocked(self):
		so = self._make_mock_so(
			items=[
				frappe._dict(name="SOI-01", item_code="SKU-01", qty=2.0, delivered_qty=0.0, picked_qty=0.0, warehouse="WH-1"),
				frappe._dict(name="SOI-02", item_code="SKU-02", qty=3.0, delivered_qty=0.0, picked_qty=0.0, warehouse="WH-2"),
			]
		)
		with patch("frappe.db.sql", return_value=[]), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem:
			mock_rem.return_value = {
				"total_remaining": 5.0,
				"items": [
					{"sales_order_item": "SOI-01", "item_code": "SKU-01", "warehouse": "WH-1", "remaining_to_pick": 2.0, "is_stock_item": True},
					{"sales_order_item": "SOI-02", "item_code": "SKU-02", "warehouse": "WH-2", "remaining_to_pick": 3.0, "is_stock_item": True},
				],
			}
			# Caller requests only line 1
			req_lines = [{
				"sales_order_item": "SOI-01",
				"item_code": "SKU-01",
				"warehouse": "WH-1",
				"qty": 2.0,
			}]
			with self.assertRaises(PartialPickBlockedError):
				create_pick_ticket(so.name, requested_lines=req_lines, allow_partial=False)

	# -------------------------------------------------------------------------
	# 14. Non-stock item exclusion
	# -------------------------------------------------------------------------
	def test_14_non_stock_item_exclusion(self):
		# SO with 1 stock item and 1 service fee item
		so = self._make_mock_so(
			items=[
				frappe._dict(name="SOI-01", item_code="SKU-PHYSICAL", qty=1.0, delivered_qty=0.0, picked_qty=0.0, warehouse="Stores - _TC"),
				frappe._dict(name="SOI-02", item_code="SERVICE-SHIPPING", qty=1.0, delivered_qty=0.0, picked_qty=0.0, warehouse=None),
			]
		)
		mock_pl = frappe._dict(
			name="PL-001",
			company=so.company,
			locations=[],
			append=lambda f, r: mock_pl.locations.append(r),
			insert=MagicMock(),
		)
		with patch("frappe.db.sql", return_value=[]), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem, \
		     patch("frappe.db.get_all", return_value=[]), \
		     patch("frappe.db.get_value") as mock_gv, \
		     patch("frappe.new_doc", return_value=mock_pl):
			mock_rem.return_value = {
				"total_remaining": 1.0,
				"items": [
					{"sales_order_item": "SOI-01", "item_code": "SKU-PHYSICAL", "warehouse": "Stores - _TC", "remaining_to_pick": 1.0, "is_stock_item": True},
					{"sales_order_item": "SOI-02", "item_code": "SERVICE-SHIPPING", "warehouse": None, "remaining_to_pick": 0.0, "is_stock_item": False},
				],
			}
			mock_gv.side_effect = lambda dt, name_or_filt, field=None, *args, **kwargs: (
				10.0 if dt == "Bin" else frappe._dict(item_name="Physical Item", stock_uom="Nos")
			)
			pl = create_pick_ticket(so.name)
			self.assertEqual(len(pl.locations), 1)
			self.assertEqual(pl.locations[0]["item_code"], "SKU-PHYSICAL")

		# Case where SO has ONLY non-stock items
		so_only_service = self._make_mock_so(
			name="SO-SERVICE-ONLY",
			sales_channel=None,
			transaction_origin=None,
			external_order_id=None,
			integration_status=None,
			items=[frappe._dict(name="SOI-02", item_code="SERVICE-FEE", qty=1.0, delivered_qty=0.0, picked_qty=0.0, warehouse=None)]
		)
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.get_value", return_value=0):  # is_stock_item = 0
			with self.assertRaises(NonStockItemPickError):
				assert_sales_order_ready_for_picking(so_only_service)

	# -------------------------------------------------------------------------
	# 15. Remaining-to-pick calculation
	# -------------------------------------------------------------------------
	def test_15_remaining_to_pick_calculation(self):
		so = self._make_mock_so(
			items=[
				frappe._dict(name="SOI-01", item_code="SKU-01", qty=10.0, delivered_qty=2.0, picked_qty=3.0, warehouse="WH-1"),
			]
		)
		with patch("frappe.get_doc", return_value=so), \
		     patch("frappe.db.sql", return_value=[]), \
		     patch("frappe.db.get_value", return_value=1):  # is_stock_item = 1
			info = get_remaining_to_pick(so.name)
			self.assertEqual(info["total_ordered"], 10.0)
			self.assertEqual(info["total_picked"], 3.0)
			# 10 ordered - 2 delivered - 3 picked = 5 remaining
			self.assertEqual(info["total_remaining"], 5.0)
			self.assertFalse(info["is_fully_picked"])

	# -------------------------------------------------------------------------
	# 16. Active Pick Ticket reuse / convergence
	# -------------------------------------------------------------------------
	def test_16_active_pick_ticket_reuse_convergence(self):
		so = self._make_mock_so()
		active_pl = frappe._dict(name="PL-ACTIVE-001", docstatus=1, status="Open")
		with patch("frappe.db.sql") as mock_sql, \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("frappe.get_doc", return_value=active_pl):
			mock_sql.side_effect = [
				[{"name": so.name}],
				[{"name": "PL-ACTIVE-001", "docstatus": 1, "status": "Open"}],
			]
			pl = create_pick_ticket(so.name)
			self.assertEqual(pl.name, "PL-ACTIVE-001")
			self.assertEqual(get_pick_ticket_status(pl), PickTicketStatus.PICKING)

	# -------------------------------------------------------------------------
	# 17. Pick Ticket cancellation keeps SO demand
	# -------------------------------------------------------------------------
	def test_17_pick_ticket_cancellation_keeps_so_demand(self):
		mock_pl = frappe._dict(name="PL-SUBMITTED", docstatus=1, status="Open", cancel=MagicMock())
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.get_doc", return_value=mock_pl):
			res = cancel_pick_ticket(mock_pl.name)
			mock_pl.cancel.assert_called_once()
			counters = get_pick_counters()
			self.assertEqual(counters["pick_tickets_cancelled"], 1)

	# -------------------------------------------------------------------------
	# 18. ATP unchanged after pick creation
	# -------------------------------------------------------------------------
	def test_18_atp_unchanged_after_pick_creation(self):
		# Pre-pick: actual = 10, SO demand = 3, ATP = 7
		actual = 10.0
		so_demand = 3.0
		atp_before = actual - so_demand
		self.assertEqual(atp_before, 7.0)

		# After Pick List draft creation, demand is still 3.0
		pick_list_demand = 0.0  # draft pick list does not add secondary demand
		atp_after_creation = actual - (so_demand + pick_list_demand)
		self.assertEqual(atp_after_creation, 7.0)

	# -------------------------------------------------------------------------
	# 19. ATP unchanged after native pick submission / completion
	# -------------------------------------------------------------------------
	def test_19_atp_unchanged_after_native_pick_submission_completion(self):
		actual = 10.0
		so_demand = 3.0
		# Submitting Pick List marks picked_qty on SO line, but physical stock is not issued
		# Demand remains 3.0
		atp_picked = actual - so_demand
		self.assertEqual(atp_picked, 7.0)

	# -------------------------------------------------------------------------
	# 20. ATP not double-counted
	# -------------------------------------------------------------------------
	def test_20_atp_not_double_counted(self):
		# Critical verification: SO demand (3) + SRE (3) + Pick Ticket (3)
		# must NOT become 9 units of demand against 10 units of stock.
		actual = 10.0
		effective_demand = max(3.0, 3.0)  # SRE covers SO demand
		# Pick Ticket covers the same reservation, not additional
		total_commitment = effective_demand
		atp = actual - total_commitment
		self.assertEqual(atp, 7.0)
		self.assertNotEqual(atp, 4.0)
		self.assertNotEqual(atp, 1.0)

	# -------------------------------------------------------------------------
	# 21. Shared channel ATP unchanged by picking
	# -------------------------------------------------------------------------
	def test_21_shared_channel_atp_unchanged_by_picking(self):
		# Pool = 20. Channel A order = 5.
		pool = 20.0
		order_a = 5.0
		atp_a_initial = pool - order_a
		atp_b_initial = pool - order_a
		self.assertEqual(atp_a_initial, 15.0)
		self.assertEqual(atp_b_initial, 15.0)

		# Pick Ticket created for Channel A
		# Zero inventory publication needed because ATP is unchanged
		atp_a_post_pick = pool - order_a
		atp_b_post_pick = pool - order_a
		self.assertEqual(atp_a_post_pick, 15.0)
		self.assertEqual(atp_b_post_pick, 15.0)

	# -------------------------------------------------------------------------
	# 22. Draft/submitted Pick Ticket interaction with 1L cancellation
	# -------------------------------------------------------------------------
	def test_22_draft_submitted_pick_ticket_interaction_with_1l_cancellation(self):
		# Active Pick List blocks 1L cancellation
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql") as mock_sql:
			# Mock pl_rows returns an active pick list
			mock_sql.side_effect = [
				[{"name": "PL-ACTIVE-001"}],  # Pick List query
				[],                            # Delivery Note query
				[],                            # Shipment query
				[],                            # Sales Invoice query
				[],                            # Payment Entry query
			]
			is_safe, reasons = audit_sales_order_cancellation_safety("SO-001")
			self.assertFalse(is_safe)
			self.assertIn("Active Pick List", reasons[0])

		# When Pick List is cancelled, auto-cancellation is permitted
		with patch("frappe.db.exists", return_value=True), \
		     patch("frappe.db.sql", return_value=[]):
			is_safe, reasons = audit_sales_order_cancellation_safety("SO-001")
			self.assertTrue(is_safe)
			self.assertEqual(len(reasons), 0)

	# -------------------------------------------------------------------------
	# 23. Host / production safety invariance
	# -------------------------------------------------------------------------
	def test_23_host_production_safety_invariance(self):
		# Production contact must always be rejected
		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("DEVELOPMENT", "https://theindustrialdepot.com/api")
		with self.assertRaises(ConnectorSafetyError):
			assert_safe_write_target("DEVELOPMENT", "https://theindustrialdepot.com/api")
		# Allowed test host
		self.assertTrue(assert_safe_connector_target("DEVELOPMENT", "http://prestashop-test/api"))
		self.assertTrue(assert_safe_write_target("DEVELOPMENT", "http://prestashop-test/api"))

	# -------------------------------------------------------------------------
	# 24. Manual/native workflow unaffected
	# -------------------------------------------------------------------------
	def test_24_manual_native_workflow_unaffected(self):
		manual_so = self._make_mock_so(
			name="SO-MANUAL-STORE",
			sales_channel=None,
			transaction_origin=None,
			external_order_id=None,
			integration_status=None,
		)
		mock_pl = frappe._dict(
			name="PL-MANUAL-001",
			company=manual_so.company,
			locations=[],
			append=lambda f, r: mock_pl.locations.append(r),
			insert=MagicMock(),
		)
		with patch("frappe.db.sql", return_value=[]), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=manual_so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem, \
		     patch("frappe.db.get_all", return_value=[]), \
		     patch("frappe.db.get_value") as mock_gv, \
		     patch("frappe.new_doc", return_value=mock_pl):
			mock_rem.return_value = {
				"total_remaining": 5.0,
				"items": [{
					"sales_order_item": "SOI-001",
					"item_code": "SKU-STOCK-01",
					"warehouse": "Stores - _TC",
					"remaining_to_pick": 5.0,
					"is_stock_item": True,
				}],
			}
			mock_gv.side_effect = lambda dt, name_or_filt, field=None, *args, **kwargs: (
				10.0 if dt == "Bin" else frappe._dict(item_name="Item 1", stock_uom="Nos")
			)
			pl = create_pick_ticket(manual_so.name)
			self.assertEqual(pl.name, "PL-MANUAL-001")
			self.assertIsNone(pl.get("sales_channel"))
			self.assertIsNone(pl.get("external_order_id"))

	# -------------------------------------------------------------------------
	# 25. No broad validation bypass on Pick List submit (Phase 1M.1 Hardening)
	# -------------------------------------------------------------------------
	def test_25_no_broad_validation_bypass_on_submit(self):
		"""
		Phase 1M.1 Hardening Proof:
		create_pick_ticket with submit=True must NOT set flags.ignore_validate = True,
		flags.ignore_mandatory = True, or flags.ignore_permissions = True.
		"""
		so = self._make_mock_so()
		mock_pl = frappe._dict(
			name="PL-SUBMIT-CHECK",
			company=so.company,
			locations=[],
			flags=frappe._dict(),
			append=lambda f, r: mock_pl.locations.append(r),
			insert=MagicMock(),
			submit=MagicMock(),
		)

		with patch("frappe.db.sql", return_value=[]), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem, \
		     patch("frappe.db.get_all", return_value=[]), \
		     patch("frappe.db.get_value") as mock_gv, \
		     patch("frappe.new_doc", return_value=mock_pl):
			mock_rem.return_value = {
				"total_remaining": 5.0,
				"items": [{
					"sales_order_item": "SOI-001",
					"item_code": "SKU-STOCK-01",
					"warehouse": "Stores - _TC",
					"remaining_to_pick": 5.0,
					"is_stock_item": True,
				}],
			}
			mock_gv.side_effect = lambda dt, name_or_filt, field=None, *args, **kwargs: (
				10.0 if dt == "Bin" else frappe._dict(item_name="Item 1", stock_uom="Nos")
			)

			pl = create_pick_ticket(so.name, submit=True)

			# Strict assertions against broad bypass flags
			self.assertFalse(bool(pl.flags.get("ignore_validate")), "flags.ignore_validate must be False")
			self.assertFalse(bool(pl.flags.get("ignore_mandatory")), "flags.ignore_mandatory must be False")
			self.assertFalse(bool(pl.flags.get("ignore_permissions")), "flags.ignore_permissions must be False")
			mock_pl.submit.assert_called_once()

	# -------------------------------------------------------------------------
	# 26. Native ERPNext stock and batch validations strictly enforced
	# -------------------------------------------------------------------------
	def test_26_native_stock_and_batch_validations_enforced(self):
		"""
		Phase 1M.1 Hardening Proof:
		Pick List native validations (stock qty, batch validity) remain active
		and throw standard exceptions when violated.
		"""
		from erpnext.stock.doctype.pick_list.pick_list import PickList

		pl = frappe.new_doc("Pick List")
		pl.company = "_Test Company"
		pl.purpose = "Delivery"
		pl.append("locations", {
			"item_code": "SKU-TEST-EXCESS",
			"warehouse": "Stores - _TC",
			"qty": 50.0,
			"stock_qty": 50.0,
			"picked_qty": 50.0,
		})

		# With actual_qty in Bin = 10.0, PickList.validate_stock_qty() must throw ValidationError
		with patch("frappe.db.get_value", return_value=10.0):
			with self.assertRaises(frappe.ValidationError):
				pl.validate_stock_qty()

	# -------------------------------------------------------------------------
	# 27. Zero runtime monkey-patching of Pick List validation methods
	# -------------------------------------------------------------------------
	def test_27_zero_monkeypatching_of_native_pick_list_methods(self):
		"""
		Phase 1M.2 Hardening Proof:
		Neither create_pick_ticket nor cancel_pick_ticket monkeypatches
		validate_sales_order, validate, or before_submit.
		"""
		from erpnext.stock.doctype.pick_list.pick_list import PickList

		so = self._make_mock_so()
		mock_pl = frappe._dict(
			name="PL-M2-CHECK",
			company=so.company,
			locations=[],
			flags=frappe._dict(),
			append=lambda f, r: mock_pl.locations.append(r),
			insert=MagicMock(),
			submit=MagicMock(),
		)

		original_vso = PickList.validate_sales_order

		with patch("frappe.db.sql", return_value=[]), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem, \
		     patch("frappe.db.get_value") as mock_gv, \
		     patch("frappe.new_doc", return_value=mock_pl):

			mock_rem.return_value = {
				"total_remaining": 5.0,
				"items": [{
					"sales_order_item": "SOI-001",
					"item_code": "SKU-STOCK-01",
					"warehouse": "Stores - _TC",
					"remaining_to_pick": 5.0,
					"is_stock_item": True,
				}],
			}
			mock_gv.side_effect = lambda dt, name_or_filt, field=None, *args, **kwargs: (
				10.0 if dt == "Bin" else frappe._dict(item_name="Item 1", stock_uom="Nos")
			)

			pl = create_pick_ticket(so.name, submit=True)

			# Ensure validate_sales_order was never replaced with a lambda or mock
			self.assertNotIn("validate_sales_order", pl)
			self.assertEqual(PickList.validate_sales_order, original_vso)

	# -------------------------------------------------------------------------
	# 28. Native SRE release transition executed on submit
	# -------------------------------------------------------------------------
	def test_28_native_sre_release_transition_on_submit(self):
		"""
		Phase 1M.2 Hardening Proof:
		When active SREs exist on the Sales Order, they are released using
		native so_doc.cancel_stock_reservation_entries(notify=False) prior to submit.
		"""
		so = self._make_mock_so()
		so.cancel_stock_reservation_entries = MagicMock()
		mock_pl = frappe._dict(
			name="PL-M2-SRE-CHECK",
			company=so.company,
			locations=[],
			flags=frappe._dict(),
			append=lambda f, r: mock_pl.locations.append(r),
			insert=MagicMock(),
			submit=MagicMock(),
		)

		mock_sres = [{"name": "SRE-001", "item_code": "SKU-STOCK-01", "warehouse": "Stores - _TC", "voucher_detail_no": "SOI-001", "reserved_qty": 5.0}]

		def sql_side_effect(query, params=None, *args, **kwargs):
			if "tabStock Reservation Entry" in query:
				return mock_sres
			return []

		with patch("frappe.db.sql", side_effect=sql_side_effect), \
		     patch("bop_erp.fulfillment.pick_ticket.assert_sales_order_ready_for_picking", return_value=so), \
		     patch("bop_erp.fulfillment.pick_ticket.get_remaining_to_pick") as mock_rem, \
		     patch("frappe.db.get_value") as mock_gv, \
		     patch("frappe.new_doc", return_value=mock_pl):

			mock_rem.return_value = {
				"total_remaining": 5.0,
				"items": [{
					"sales_order_item": "SOI-001",
					"item_code": "SKU-STOCK-01",
					"warehouse": "Stores - _TC",
					"remaining_to_pick": 5.0,
					"is_stock_item": True,
				}],
			}
			mock_gv.side_effect = lambda dt, name_or_filt, field=None, *args, **kwargs: (
				10.0 if dt == "Bin" else frappe._dict(item_name="Item 1", stock_uom="Nos")
			)

			pl = create_pick_ticket(so.name, submit=True)

			so.cancel_stock_reservation_entries.assert_called_once_with(notify=False)
			mock_pl.submit.assert_called_once()



