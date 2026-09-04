# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import frappe
from frappe import _


@dataclass
class ImportResult:
	entity_type: str
	seen: int = 0
	created: int = 0
	updated: int = 0
	unchanged: int = 0
	skipped: int = 0
	failed: int = 0
	errors: List[Dict[str, Any]] = field(default_factory=list)

	def to_dict(self) -> Dict[str, Any]:
		return {
			"entity_type": self.entity_type,
			"seen": self.seen,
			"created": self.created,
			"updated": self.updated,
			"unchanged": self.unchanged,
			"skipped": self.skipped,
			"failed": self.failed,
			"errors": self.errors,
		}

	def merge(self, other: "ImportResult"):
		self.seen += other.seen
		self.created += other.created
		self.updated += other.updated
		self.unchanged += other.unchanged
		self.skipped += other.skipped
		self.failed += other.failed
		self.errors.extend(other.errors)


class BaseImporter:
	"""
	Base class for entity importers.
	Provides:
	- dry_run handling and proposed action estimation
	- savepoint boundaries per entity to guarantee failure isolation
	- counter tracking
	- External ID Mapping creation/retrieval helpers
	"""

	def __init__(self, client, sales_channel: str, dry_run: bool = False):
		self.client = client
		self.sales_channel = sales_channel
		self.dry_run = dry_run
		# Track mapping operations
		self.mappings_created = 0
		self.mappings_reused = 0
		self.mappings_updated = 0

	def get_active_mapping(
		self,
		external_entity_type: str,
		external_id: str,
		external_variant_id: Optional[str] = None,
	) -> Optional[Dict[str, Any]]:
		"""Retrieves existing active External ID Mapping for given external identity."""
		filters = {
			"sales_channel": self.sales_channel,
			"external_entity_type": external_entity_type,
			"external_id": str(external_id),
			"active": 1,
		}
		if external_variant_id is not None and str(external_variant_id).strip() != "":
			filters["external_variant_id"] = str(external_variant_id)

		mapping = frappe.db.get_value(
			"External ID Mapping",
			filters,
			["name", "erp_doctype", "erp_document", "sync_hash"],
			as_dict=True,
		)
		return mapping

	def set_mapping(
		self,
		external_entity_type: str,
		external_id: str,
		erp_doctype: str,
		erp_document: str,
		external_variant_id: Optional[str] = None,
		sync_hash: Optional[str] = None,
	):
		"""Creates or updates an active External ID Mapping record."""
		if self.dry_run:
			existing = self.get_active_mapping(external_entity_type, external_id, external_variant_id)
			if existing:
				if existing.sync_hash != sync_hash or existing.erp_document != erp_document:
					self.mappings_updated += 1
				else:
					self.mappings_reused += 1
			else:
				self.mappings_created += 1
			return

		existing = self.get_active_mapping(external_entity_type, external_id, external_variant_id)
		if existing:
			doc = frappe.get_doc("External ID Mapping", existing.name)
			if doc.erp_doctype != erp_doctype or doc.erp_document != erp_document or doc.sync_hash != sync_hash:
				doc.erp_doctype = erp_doctype
				doc.erp_document = erp_document
				doc.sync_hash = sync_hash
				doc.last_synced_at = frappe.utils.now_datetime()
				doc.save(ignore_permissions=True)
				self.mappings_updated += 1
			else:
				self.mappings_reused += 1
		else:
			doc = frappe.get_doc({
				"doctype": "External ID Mapping",
				"sales_channel": self.sales_channel,
				"external_entity_type": external_entity_type,
				"external_id": str(external_id),
				"external_variant_id": str(external_variant_id) if external_variant_id is not None else None,
				"erp_doctype": erp_doctype,
				"erp_document": erp_document,
				"active": 1,
				"sync_hash": sync_hash,
				"last_synced_at": frappe.utils.now_datetime(),
			})
			doc.insert(ignore_permissions=True)
			self.mappings_created += 1
