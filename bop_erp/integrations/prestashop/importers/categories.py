# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
from typing import Dict, Any, List, Optional
import frappe
from frappe import _

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.adapters.normalizers import normalize_category
from bop_erp.integrations.prestashop.importers.base import BaseImporter, ImportResult


class CategoryImporter(BaseImporter):
	"""
	Imports PrestaShop categories into native ERPNext Item Groups.
	- Respects category tree hierarchy (parent item groups must exist before children).
	- Anchors root categories to ERPNext 'All Item Groups'.
	- Maps PrestaShop Category ID -> External ID Mapping (CATEGORY).
	- Preserves idempotency: re-running does not duplicate or corrupt Item Groups.
	"""

	def import_categories(self, category_list: Optional[List[Dict[str, Any]]] = None) -> ImportResult:
		result = ImportResult(entity_type=ExternalEntityType.CATEGORY)

		if category_list is None:
			raw_categories = self.client.list_categories(limit=250, display="full")
		else:
			raw_categories = category_list

		# Normalize all categories
		normalized_cats = []
		for raw in raw_categories:
			# Skip if minimal stub without name/id
			if not raw.get("id"):
				continue
			cat = normalize_category(raw)
			normalized_cats.append(cat)

		# Topological sort or iterative resolution (parents before children)
		# Root or parent=0 / parent=1 (PrestaShop root category) maps to "All Item Groups"
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

		# Order by tree depth: if parent is missing or is 0/1/root, depth=0, else depth=parent.depth + 1
		def get_depth(c, visited=None):
			if visited is None:
				visited = set()
			if c.external_id in visited or not c.parent_id or c.parent_id not in cats_by_id or c.parent_id in ("0", "1", c.external_id):
				return 0
			visited.add(c.external_id)
			return 1 + get_depth(cats_by_id[c.parent_id], visited)

		sorted_cats = sorted(normalized_cats, key=get_depth)

		for cat in sorted_cats:
			result.seen += 1
			# Skip PrestaShop internal virtual Root (id 1) if name is "Root"
			if cat.external_id == "1" and cat.name.lower() in ("root", "inicio"):
				result.unchanged += 1
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
		elif cat.parent_id and cat.parent_id not in ("0", "1"):
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
				return "unchanged", group_name

			# Need update
			if not self.dry_run:
				doc = frappe.get_doc("Item Group", group_name)
				# Update parent if changed and valid
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
			if not self.dry_run:
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
			return "created", desired_name
