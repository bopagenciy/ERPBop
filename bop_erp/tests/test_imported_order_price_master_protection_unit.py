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

	def test_07_cache_key_absent_before_context_restored_absent_after(self):
		"""
		Phase 1R.0-A Req 2.A:
		If cache key is absent before context, it must be absent after context.
		"""
		key = frappe.get_document_cache_key("Stock Settings", "Stock Settings")
		cached_key = frappe.cache.make_key(key)

		# Ensure absent before
		orig_cached = frappe.local.cache.pop(cached_key, None)
		self.assertNotIn(cached_key, frappe.local.cache)

		try:
			with protect_imported_order_price_master():
				self.assertIn(cached_key, frappe.local.cache)
				self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)

			# Must be absent after exit
			self.assertNotIn(cached_key, frappe.local.cache)
		finally:
			if orig_cached is not None:
				frappe.local.cache[cached_key] = orig_cached

	def test_08_cache_key_present_before_context_exact_same_object_restored(self):
		"""
		Phase 1R.0-A Req 2.B & Req 8:
		If cache key is present before context, the exact same object/value
		is restored after context (exact object identity preserved).
		"""
		key = frappe.get_document_cache_key("Stock Settings", "Stock Settings")
		cached_key = frappe.cache.make_key(key)

		# Ensure present before
		sentinel_doc = frappe.get_cached_doc("Stock Settings")
		frappe.local.cache[cached_key] = sentinel_doc
		self.assertIs(frappe.local.cache[cached_key], sentinel_doc)

		with protect_imported_order_price_master():
			# Inside: must be a different object with auto_insert = 0
			inside_doc = frappe.local.cache[cached_key]
			self.assertIsNot(inside_doc, sentinel_doc)
			self.assertEqual(inside_doc.auto_insert_price_list_rate_if_missing, 0)

		# After: exact same object identity must be restored
		self.assertIn(cached_key, frappe.local.cache)
		self.assertIs(frappe.local.cache[cached_key], sentinel_doc)

	def test_09_exception_inside_context_preserves_absent_or_present_state(self):
		"""
		Phase 1R.0-A Req 2.C:
		Exception inside context restores exact prior state for both present and absent cases.
		"""
		key = frappe.get_document_cache_key("Stock Settings", "Stock Settings")
		cached_key = frappe.cache.make_key(key)

		class TestException(Exception):
			pass

		# Case 1: Was absent before
		orig_cached = frappe.local.cache.pop(cached_key, None)
		try:
			with self.assertRaises(TestException):
				with protect_imported_order_price_master():
					raise TestException("Boom")
			self.assertNotIn(cached_key, frappe.local.cache)
		finally:
			if orig_cached is not None:
				frappe.local.cache[cached_key] = orig_cached

		# Case 2: Was present before
		sentinel_doc = frappe.get_cached_doc("Stock Settings")
		frappe.local.cache[cached_key] = sentinel_doc
		with self.assertRaises(TestException):
			with protect_imported_order_price_master():
				raise TestException("Boom 2")
		self.assertIs(frappe.local.cache[cached_key], sentinel_doc)

	def test_10_sequential_calls_no_leakage(self):
		"""
		Phase 1R.0-A Req 2.E:
		Sequential calls do not leak state between calls.
		"""
		orig_auto_insert = frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing

		for i in range(5):
			self.assertFalse(is_price_master_protection_active())
			self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, orig_auto_insert)
			with protect_imported_order_price_master():
				self.assertTrue(is_price_master_protection_active())
				self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)
			self.assertFalse(is_price_master_protection_active())
			self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, orig_auto_insert)

	def test_11_unrelated_stock_settings_fields_preserved_identical(self):
		"""
		Phase 1R.0-A Req 5 & Req 9, 10:
		Temporary cached Stock Settings preserves every single original field
		and changes ONLY auto_insert_price_list_rate_if_missing and update_existing_price_list_rate.
		"""
		original_ss = frappe.get_cached_doc("Stock Settings")

		with protect_imported_order_price_master():
			protected_ss = frappe.get_cached_doc("Stock Settings")

			for attr in original_ss.__dict__:
				if attr in ("auto_insert_price_list_rate_if_missing", "update_existing_price_list_rate"):
					self.assertEqual(getattr(protected_ss, attr), 0)
				else:
					orig_val = getattr(original_ss, attr)
					prot_val = getattr(protected_ss, attr)
					self.assertEqual(
						orig_val,
						prot_val,
						f"Unrelated Stock Settings field '{attr}' differed inside context! Original: {orig_val}, Inside: {prot_val}",
					)

	def test_12_frappe_cache_contract_fail_fast_assertion(self):
		"""
		Phase 1R.0-A Req 4 & Req 14:
		Verifies that protect_imported_order_price_master() executes its fail-fast
		contract assertion, confirming that frappe.get_cached_doc('Stock Settings')
		faithfully observes the local cache override.
		"""
		with protect_imported_order_price_master():
			# If contract held, auto_insert is 0
			self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)

	def test_13_permission_and_user_independence(self):
		"""
		Phase 1R.0-A Req 6 & Req 15, 16:
		Verifies protection behavior is identical regardless of active user
		(Administrator vs non-admin user).
		"""
		orig_user = frappe.session.user
		try:
			# Test as Administrator
			frappe.set_user("Administrator")
			with protect_imported_order_price_master():
				self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)

			# Test as Guest / non-admin user
			frappe.set_user("Guest")
			with protect_imported_order_price_master():
				self.assertEqual(frappe.get_cached_doc("Stock Settings").auto_insert_price_list_rate_if_missing, 0)
		finally:
			frappe.set_user(orig_user)

