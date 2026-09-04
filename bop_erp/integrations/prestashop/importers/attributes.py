# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Dict, Any, List, Optional
import frappe
from frappe import _

from bop_erp.integrations.prestashop.adapters.normalizers import extract_lang_field
from bop_erp.integrations.prestashop.importers.base import BaseImporter, ImportResult


class AttributeImporter(BaseImporter):
	"""
	Imports PrestaShop Product Options (Attribute Groups) and Product Option Values
	into native ERPNext Item Attribute and Item Attribute Value records.
	- Option (e.g. 'Size', 'Color') -> DocType 'Item Attribute'
	- Option Value (e.g. 'S', 'M', 'L', 'Red', 'Blue') -> Child table 'Item Attribute Value'
	- Idempotent: Does not create duplicates if attribute or value already exists.
	"""

	def import_attributes(
		self,
		options_list: Optional[List[Dict[str, Any]]] = None,
		values_list: Optional[List[Dict[str, Any]]] = None,
	) -> ImportResult:
		result = ImportResult(entity_type="ATTRIBUTE")

		# 1. Fetch Option Groups from PrestaShop
		if options_list is None:
			raw_options = self.client.list_product_options(limit=250, display="full")
		else:
			raw_options = options_list

		# 2. Fetch Option Values from PrestaShop
		if values_list is None:
			raw_values = self.client.list_product_option_values(limit=1000, display="full")
		else:
			raw_values = values_list

		# Map option values by option_id (group id)
		# PrestaShop option value record has: 'id', 'id_attribute_group', 'name'
		values_by_group: Dict[str, List[Dict[str, Any]]] = {}
		for val in raw_values:
			grp_id = str(val.get("id_attribute_group", "")).strip()
			if grp_id:
				values_by_group.setdefault(grp_id, []).append(val)

		# Import each option group
		for opt in raw_options:
			opt_id = str(opt.get("id", "")).strip()
			if not opt_id:
				continue

			result.seen += 1
			savepoint = f"opt_{opt_id}"

			try:
				if not self.dry_run:
					frappe.db.savepoint(savepoint)

				opt_name = extract_lang_field(opt.get("name")) or extract_lang_field(opt.get("public_name")) or f"PS-Option-{opt_id}"
				opt_name = opt_name.strip()

				# Option values for this group
				grp_values = values_by_group.get(opt_id, [])

				status = self._import_single_attribute(opt_id, opt_name, grp_values)
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
					"option_id": opt_id,
					"name": opt.get("name"),
					"error": str(e),
				})
				frappe.log_error(
					title=f"Attribute Import Error: {opt_id}",
					message=str(e),
				)

		return result

	def _import_single_attribute(
		self, opt_id: str, opt_name: str, values: List[Dict[str, Any]]
	) -> str:
		existing_attr = frappe.db.get_value("Item Attribute", {"attribute_name": opt_name}, "name")

		normalized_values = []
		for v in values:
			val_str = extract_lang_field(v.get("name")).strip()
			if not val_str:
				val_str = f"Val-{v.get('id')}"
			abbr = val_str[:10].strip() or val_str
			normalized_values.append((val_str, abbr))

		if existing_attr:
			# Check existing values and append missing
			if not self.dry_run:
				attr_doc = frappe.get_doc("Item Attribute", existing_attr)
				existing_vals = {row.attribute_value.lower(): row for row in attr_doc.item_attribute_values}
				updated = False

				for val_str, abbr in normalized_values:
					if val_str.lower() not in existing_vals:
						# Ensure unique abbr
						existing_abbrs = {row.abbr for row in attr_doc.item_attribute_values}
						final_abbr = abbr
						counter = 1
						while final_abbr in existing_abbrs:
							final_abbr = f"{abbr[:7]}_{counter}"
							counter += 1

						attr_doc.append("item_attribute_values", {
							"attribute_value": val_str,
							"abbr": final_abbr,
						})
						updated = True

				if updated:
					attr_doc.flags.ignore_permissions = True
					attr_doc.save()
					return "updated"
				return "unchanged"
			return "unchanged"

		# Create new Item Attribute
		if not self.dry_run:
			attr_doc = frappe.get_doc({
				"doctype": "Item Attribute",
				"attribute_name": opt_name,
				"item_attribute_values": [],
			})

			seen_abbrs = set()
			seen_vals = set()

			for val_str, abbr in normalized_values:
				if val_str.lower() in seen_vals:
					continue
				seen_vals.add(val_str.lower())

				final_abbr = abbr
				counter = 1
				while final_abbr in seen_abbrs:
					final_abbr = f"{abbr[:7]}_{counter}"
					counter += 1
				seen_abbrs.add(final_abbr)

				attr_doc.append("item_attribute_values", {
					"attribute_value": val_str,
					"abbr": final_abbr,
				})

			attr_doc.flags.ignore_permissions = True
			attr_doc.insert()
			return "created"
		else:
			return "created"
