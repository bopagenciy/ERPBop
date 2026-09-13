# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import re
from typing import Any, Optional, Tuple

import frappe


def canonical_source_system(source_system: Any) -> str:
	"""
	Canonicalizes a source system identifier:
	- strip leading/trailing whitespace
	- uppercase
	- fail-closed if empty or whitespace-only
	"""
	if not source_system or not str(source_system).strip():
		raise ValueError("source_system must be a non-empty string.")
	return str(source_system).strip().upper()


def canonical_source_instance_id(source_instance_id: Any) -> str:
	"""
	Canonicalizes a source instance identifier:
	- default to 'DEFAULT' if blank/null/whitespace
	- strip leading/trailing whitespace
	- uppercase
	"""
	if not source_instance_id or not str(source_instance_id).strip():
		return "DEFAULT"
	return str(source_instance_id).strip().upper()


def canonical_source_namespace(
	source_system: Any,
	source_instance_id: Any = None,
) -> Tuple[str, str]:
	"""
	Returns canonical (source_system, source_instance_id) pair.
	"""
	return canonical_source_system(source_system), canonical_source_instance_id(source_instance_id)


def canonical_provider(
	source_system: Any,
	source_instance_id: Any = None,
) -> str:
	"""
	Returns the canonical provider string for External ID Mapping:
	'{CANONICAL_SOURCE_SYSTEM}:{CANONICAL_SOURCE_INSTANCE_ID}'
	"""
	sys, inst = canonical_source_namespace(source_system, source_instance_id)
	return f"{sys}:{inst}"


def canonical_company_tag(company: str) -> str:
	"""
	Returns a deterministic, uppercase identifier tag for a Company:
	- If Company exists in DB and has an abbr, uses abbr
	- Otherwise uses cleaned alphanumeric uppercase company name (up to 12 chars)
	- Fallback to 8-char SHA-256 hash if empty
	"""
	if not company or not str(company).strip():
		raise ValueError("Company must not be empty.")

	clean_co = str(company).strip()
	abbr = None
	try:
		if hasattr(frappe, "db") and frappe.db:
			abbr = frappe.get_cached_value("Company", clean_co, "abbr")
	except Exception:
		abbr = None

	if abbr and str(abbr).strip():
		tag = re.sub(r"[^A-Z0-9_-]", "", str(abbr).strip().upper())
		if tag:
			return tag

	cleaned = re.sub(r"[^A-Z0-9_-]", "", clean_co.upper())
	if cleaned and len(cleaned) <= 12:
		return cleaned

	co_hash = hashlib.sha256(clean_co.upper().encode("utf-8")).hexdigest()[:8].upper()
	return f"{cleaned[:6]}-{co_hash}" if cleaned else co_hash


def compute_migration_channel_id(
	company: str,
	source_system: str,
	source_instance_id: Optional[str] = None,
) -> str:
	"""
	Computes the deterministic, company-isolated migration Sales Channel ID:
	MIG-{COMPANY_TAG}-{SOURCE_SYSTEM}-{SOURCE_INSTANCE_ID}
	"""
	co_tag = canonical_company_tag(company)
	sys, inst = canonical_source_namespace(source_system, source_instance_id)
	return f"MIG-{co_tag}-{sys}-{inst}"
