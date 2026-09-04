# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from typing import Dict, Any, List, Optional, Set
import frappe
from frappe import _

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.adapters.normalizers import normalize_category
from bop_erp.integrations.prestashop.importers.base import BaseImporter, ImportResult


# System / Provider virtual categories that must never be created as commercial Item Groups
SYSTEM_CATEGORY_NAMES = {
	"root",
	"korijenski",
	"početak",
	"home",
	"inicio",
	"accueil",
}


class CategoryImporter(BaseImporter):
	"""
	Imports PrestaShop categories into native ERPNext Item Groups.
	- Respects category tree hierarchy (parent item groups must exist before children).
	- Filters out PrestaShop virtual/system root categories (id=1, id=2, root flags).
	- Anchors top-level commercial categories to ERPNext 'All Item Groups'.
	- Maps PrestaShop Category ID -> External ID Mapping (CATEGORY).
	- Preserves idempotency: re-running does not duplicate or corrupt Item Groups.
	"""

	def is_system_root_category(self, cat) -> bool:
		"""Detects whether a category is a PrestaShop internal root/system node."""
		if cat.external_id in ("1", "2"):
			return True
		raw = cat.raw_data or {}
		if str(raw.get("is_root_category", "0")).strip() in ("1", "true", "True"):
			return True
		if cat.name.strip().lower() in SYSTEM_CATEGORY_NAMES:
			return True
		return False

	def import_categories(
		self,
		category_list: Optional[List[Dict[str, Any]]] = None,
		category_ids: Optional[Set[str]] = None,
	) -> ImportResult:
		result = ImportResult(entity_type=ExternalEntityType.CATEGORY)

		if category_list is None:
			raw_categories = self.client.list_categories(limit=250, display="full")
		else:
			raw_categories = category_list

		# Normalize all categories
		normalized_cats = []
		for raw in raw_categories:
			if not raw.get("id"):
				continue
			cat = normalize_category(raw)
			normalized_cats.append(cat)

		cats_by_id = {c.external_id: c for c in normalized_cats}
		resolved_groups: Dict[str, str] = {}  # ext_id -> item_group_name

		# Ensure "All Item Groups" exists as ERP root
		erp_root = "All Item Groups"
		if not frappe.db.exists("Item Group", erp_root):
			if not self.dry_run:
				root_doc = frappe.get_doc({
					"doctype": "Item Group",
					"item_group_name": erp_root,
					"is_group": 1,
				})
				root_doc.flags.ignore_permissions = True
				root_doc.insert()

		def get_depth(c, visited=None):
			if visited is None:
				visited = set()
			if c.external_id in visited or not c.parent_id or c.parent_id not in cats_by_id or c.parent_id in ("0", "1", "2", c.external_id):
				return 0
			visited.add(c.external_id)
			return 1 + get_depth(cats_by_id[c.parent_id], visited)

		sorted_cats = sorted(normalized_cats, key=get_depth)

		for cat in sorted_cats:
			result.seen += 1

			# Filter 1: Optional category scope filter
			if category_ids is not None and cat.external_id not in category_ids:
				result.skipped += 1
				continue

			# Filter 2: PrestaShop system / root categories
			if self.is_system_root_category(cat):
				result.skipped += 1
				resolved_groups[cat.external_id] = erp_root
				continue

			savepoint = f"cat_{cat.external_id}"
			try:
				if not self.dry_run:
					frappe.db.savepoint(savepoint)

				status, group_name = self._import_single_category(cat, resolved_groups, erp_root)
				resolved_groups[cat.external_id] = group_name

				if status == "created":
					result.created += 1
				elif status == "updated":
					result.updated += 1
				else:
					result.unchanged += 1

			except Exception as e:
				if not self.dry_run:
					frappe.db.rollback(save_point=savepoint)
				result.failed += 1
				result.errors.append({
					"external_id": cat.external_id,
					"name": cat.name,
					"error": str(e),
				})
				frappe.log_error(
					title=f"Category Import Error: {cat.external_id}",
					message=str(e),
				)

		return result

	def _import_single_category(
		self, cat, resolved_groups: Dict[str, str], erp_root: str
	) -> (str, str):
		# Determine parent item group
		parent_group = erp_root
		if cat.parent_id and cat.parent_id in resolved_groups:
			parent_group = resolved_groups[cat.parent_id]
		elif cat.parent_id and cat.parent_id not in ("0", "1", "2"):
			# Check if parent is mapped in DB
			parent_map = self.get_active_mapping(ExternalEntityType.CATEGORY, cat.parent_id)
			if parent_map and parent_map.erp_document:
				parent_group = parent_map.erp_document

		# Check existing mapping
		existing_map = self.get_active_mapping(ExternalEntityType.CATEGORY, cat.external_id)

		# Compute payload hash for change detection
		sync_payload = {
			"name": cat.name,
			"parent": parent_group,
		}
		sync_hash = hashlib.sha256(json.dumps(sync_payload, sort_keys=True).encode("utf-8")).hexdigest()

		if existing_map and frappe.db.exists("Item Group", existing_map.erp_document):
			group_name = existing_map.erp_document
			if existing_map.sync_hash == sync_hash:
				self.set_mapping(
					ExternalEntityType.CATEGORY,
					cat.external_id,
					"Item Group",
					group_name,
					sync_hash=sync_hash,
				)
				return "unchanged", group_name

			# Need update
			if not self.dry_run:
				doc = frappe.get_doc("Item Group", group_name)
				if doc.parent_item_group != parent_group and parent_group != doc.name:
					doc.parent_item_group = parent_group
					doc.save(ignore_permissions=True)
			self.set_mapping(
				ExternalEntityType.CATEGORY,
				cat.external_id,
				"Item Group",
				group_name,
				sync_hash=sync_hash,
			)
			return "updated", group_name

		# If not mapped, check if an Item Group with this exact name already exists
		desired_name = cat.name.strip() or f"PS-Category-{cat.external_id}"
		existing_group_by_name = frappe.db.get_value("Item Group", desired_name, "name")

		if existing_group_by_name:
			group_name = existing_group_by_name
			self.set_mapping(
				ExternalEntityType.CATEGORY,
				cat.external_id,
				"Item Group",
				group_name,
				sync_hash=sync_hash,
			)
			return "updated", group_name

		# Create new Item Group
		if not self.dry_run:
			new_doc = frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": desired_name,
				"parent_item_group": parent_group,
				"is_group": 0,
			})
			new_doc.flags.ignore_permissions = True
			new_doc.insert()
			group_name = new_doc.name

			# Ensure parent is marked as group
			if parent_group and parent_group != group_name:
				if not frappe.db.get_value("Item Group", parent_group, "is_group"):
					frappe.db.set_value("Item Group", parent_group, "is_group", 1)

			self.set_mapping(
				ExternalEntityType.CATEGORY,
				cat.external_id,
				"Item Group",
				group_name,
				sync_hash=sync_hash,
			)
			return "created", group_name
		else:
			self.set_mapping(
				ExternalEntityType.CATEGORY,
				cat.external_id,
				"Item Group",
				desired_name,
				sync_hash=sync_hash,
			)
			return "created", desired_name
