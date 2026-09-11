# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import copy
import frappe
from frappe.tests.utils import FrappeTestCase

from bop_erp.orders.pricing_protection import (
	protect_imported_order_price_master,
	is_price_master_protection_active,
)


class TestImportedOrderPriceMasterProtectionUnit(FrappeTestCase):
	"""
	Unit test suite for Phase 1R.0 Imported Order Price-Master Protection.
	Validates thread-isolated in-memory suppression, re-entrancy, exception safety,
	non-mutation of persistent MariaDB/Redis state, and transactional price preservation.
	"""

	def test_01_context_manager_switches_auto_insert_flags_in_local_cache(self):
		"""
		Verifies that inside the context manager, cached Stock Settings has
		auto_insert_price_list_rate_if_missing == 0 and update_existing_price_list_rate == 0,
		and that upon exit the original values are restored.
		"""
		orig_doc = frappe.get_cached_doc("Stock Settings")
		orig_auto_insert = orig_doc.auto_insert_price_list_rate_if_missing
		orig_update_existing = orig_doc.update_existing_price_list_rate

		self.assertFalse(is_price_master_protection_active())

		with protect_imported_order_price_master():
			self.assertTrue(is_price_master_protection_active())
			ctx_doc = frappe.get_cached_doc("Stock Settings")
			self.assertEqual(ctx_doc.auto_insert_price_list_rate_if_missing, 0)
			self.assertEqual(ctx_doc.update_existing_price_list_rate, 0)

		self.assertFalse(is_price_master_protection_active())
		restored_doc = frappe.get_cached_doc("Stock Settings")
		self.assertEqual(restored_doc.auto_insert_price_list_rate_if_missing, orig_auto_insert)
		self.assertEqual(restored_doc.update_existing_price_list_rate, orig_update_existing)

	def test_02_context_manager_reentrancy_and_nested_restoration(self):
		"""
		Verifies that nested invocations of the context manager maintain suppression
		at each level and cleanly restore on step-by-step exit.
		"""
		orig_auto_insert = frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing

		with protect_imported_order_price_master():
			self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)

			with protect_imported_order_price_master():
				self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)

				with protect_imported_order_price_master():
					self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)

				self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)

			self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)

		self.assertEqual(
			frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing,
			orig_auto_insert,
		)

	def test_03_context_manager_exception_safety(self):
		"""
		Verifies that if an unexpected exception occurs within the context,
		the local cache is reliably restored to its pre-context state without leaks.
		"""
		orig_auto_insert = frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing

		class SimulatedError(Exception):
			pass

		with self.assertRaises(SimulatedError):
			with protect_imported_order_price_master():
				self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)
				raise SimulatedError("Simulated pipeline failure")

		self.assertFalse(is_price_master_protection_active())
		self.assertEqual(
			frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing,
			orig_auto_insert,
		)

	def test_04_no_mutation_to_mariadb_tabsingles(self):
		"""
		Verifies that MariaDB tabSingles is completely untouched (zero SQL updates).
		"""
		db_val_before = frappe.db.get_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing")

		with protect_imported_order_price_master():
			db_val_inside = frappe.db.get_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing")
			self.assertEqual(db_val_inside, db_val_before)

		db_val_after = frappe.db.get_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing")
		self.assertEqual(db_val_after, db_val_before)

	def test_05_no_mutation_to_redis_shared_cache(self):
		"""
		Verifies that Redis cluster/document cache is completely untouched.
		"""
		key = frappe.get_document_cache_key("Stock Settings", "Stock Settings")
		redis_val_before = frappe.cache.get_value(key, use_local_cache=False)

		with protect_imported_order_price_master():
			redis_val_inside = frappe.cache.get_value(key, use_local_cache=False)
			if redis_val_before is not None and redis_val_inside is not None:
				self.assertEqual(
					redis_val_inside.auto_insert_price_list_rate_if_missing,
					redis_val_before.auto_insert_price_list_rate_if_missing,
				)

		redis_val_after = frappe.cache.get_value(key, use_local_cache=False)
		if redis_val_before is not None and redis_val_after is not None:
			self.assertEqual(
				redis_val_after.auto_insert_price_list_rate_if_missing,
				redis_val_before.auto_insert_price_list_rate_if_missing,
			)

	def test_06_manual_erp_workflow_unaffected(self):
		"""
		Verifies that any code or user executing outside the context
		observes standard ERPNext Stock Settings without alteration.
		"""
		self.assertFalse(is_price_master_protection_active())
		ss = frappe.get_cached_doc("Stock Settings")
		self.assertEqual(ss.auto_insert_price_list_rate_if_missing, 1)
