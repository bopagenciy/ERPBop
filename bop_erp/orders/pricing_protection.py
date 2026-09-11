# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import copy
from contextlib import contextmanager
from typing import Generator

import frappe


@contextmanager
def protect_imported_order_price_master() -> Generator[None, None, None]:
	"""
	Guarantees that automated imported external order transactions NEVER create
	or mutate master ERPNext Item Price records.

	Zero-Core Architecture:
	Temporarily overrides the thread-local / request-local cached Stock Settings
	(in frappe.local.cache) with:
	  - auto_insert_price_list_rate_if_missing = 0
	  - update_existing_price_list_rate = 0

	Properties:
	  - ZERO database writes (MariaDB tabSingles is completely untouched).
	  - ZERO Redis writes (shared Redis document cache is completely untouched).
	  - ZERO effect on concurrent requests/workers (thread-isolated via frappe.local).
	  - ZERO effect on native/manual ERP user workflows outside this context.
	  - ZERO monkeypatching of ERPNext or Frappe.
	  - ZERO validation bypass (native Sales Order validation, pricing rules, taxes,
	    currency conversion, and Stock Reservations run 100% natively).
	  - Re-entrant and exception-safe (thread-local stack ensures deterministic restoration).
	"""
	key = frappe.get_document_cache_key("Stock Settings", "Stock Settings")
	cached_key = frappe.cache.make_key(key)

	if not hasattr(frappe.local, "_bop_price_protect_stack"):
		frappe.local._bop_price_protect_stack = []

	has_local = cached_key in frappe.local.cache
	prev_cached_doc = frappe.local.cache.get(cached_key)
	frappe.local._bop_price_protect_stack.append((has_local, prev_cached_doc))

	# Retrieve current Stock Settings
	current_ss = frappe.get_cached_doc("Stock Settings")
	ss_copy = copy.copy(current_ss)
	ss_copy.auto_insert_price_list_rate_if_missing = 0
	ss_copy.update_existing_price_list_rate = 0
	frappe.local.cache[cached_key] = ss_copy

	try:
		yield
	finally:
		has_orig, orig_doc = frappe.local._bop_price_protect_stack.pop()
		if has_orig:
			frappe.local.cache[cached_key] = orig_doc
		else:
			frappe.local.cache.pop(cached_key, None)


def is_price_master_protection_active() -> bool:
	"""
	Returns True if the current thread is executing within an active
	protect_imported_order_price_master() context.
	"""
	return bool(getattr(frappe.local, "_bop_price_protect_stack", None))
