# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import re
from typing import Dict, Any, List, Optional, Tuple
import frappe
from frappe import _

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.adapters.normalizers import (
	normalize_product,
	normalize_combination,
	extract_lang_field,
)
from bop_erp.integrations.prestashop.importers.base import BaseImporter, ImportResult


MAX_ITEM_CODE_LENGTH = 140


def sanitize_item_code(sku: str, fallback_id: str) -> str:
	"""
	Determines and sanitizes the ERPNext item_code:
	- If sku is present: stripped, truncated to 140 chars max.
	- If missing/empty: deterministic fallback 'PS-{fallback_id}'.
	"""
	clean = (sku or "").strip()
	if not clean:
		return f"PS-{fallback_id}"[:MAX_ITEM_CODE_LENGTH]
	return clean[:MAX_ITEM_CODE_LENGTH]


class ProductImporter(BaseImporter):
	"""
	Imports PrestaShop simple products and combinations into native ERPNext Items.
	- Simple Product -> ERPNext Item (has_variants=0) + External ID Mapping (PRODUCT).
	- Product with Combinations ->
	    - ERPNext Template Item (has_variants=1) + External ID Mapping (PRODUCT)
	    - ERPNext Variant Items (variant_of=template) + External ID Mapping (PRODUCT_VARIANT)
	- Strict Field Ownership: Only synchronizes item_name, description, disabled, item_group.
	  Preserves manually set ERP attributes (valuation_method, custom fields, default warehouse).
	- Inventory is strictly untouched: 0 Stock Ledger / Bin modifications.
	"""

	def __init__(self, client, sales_channel: str, dry_run: bool = False):
		super().__init__(client, sales_channel, dry_run=dry_run)
		self._stock_uom = frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos"
		self._opt_val_cache: Dict[str, Tuple[str, str]] = {}  # val_id -> (attr_name, val_name)

	def _preload_attribute_mappings(self):
		"""Loads PrestaShop option value -> (Item Attribute, value) mappings into cache."""
		if self._opt_val_cache:
			return

		try:
			raw_opts = self.client.list_product_options(limit=250, display="full")
			raw_vals = self.client.list_product_option_values(limit=1000, display="full")
		except Exception as e:
			frappe.log_error(title="Preload Attribute Options Warning", message=str(e))
			return

		opt_names = {}
		for o in raw_opts:
			oid = str(o.get("id", "")).strip()
			oname = extract_lang_field(o.get("name")) or extract_lang_field(o.get("public_name")) or f"PS-Option-{oid}"
			opt_names[oid] = oname.strip()

		for v in raw_vals:
			vid = str(v.get("id", "")).strip()
			gid = str(v.get("id_attribute_group", "")).strip()
			vname = extract_lang_field(v.get("name")).strip() or f"Val-{vid}"
			attr_name = opt_names.get(gid, f"PS-Option-{gid}")
			self._opt_val_cache[vid] = (attr_name, vname)

	def import_products(
		self,
		products_list: Optional[List[Dict[str, Any]]] = None,
	) -> ImportResult:
		result = ImportResult(entity_type=ExternalEntityType.PRODUCT)
		self._preload_attribute_mappings()

		if products_list is None:
			raw_products = self.client.list_products(limit=250, display="full")
		else:
			raw_products = products_list

		for raw in raw_products:
			if not raw.get("id"):
				continue

			p = normalize_product(raw)
			result.seen += 1
			savepoint = f"prod_{p.external_id}"

			try:
				if not self.dry_run:
					frappe.db.savepoint(savepoint)

				sub_res = self._import_single_product(p, raw)
				result.created += sub_res.created
				result.updated += sub_res.updated
				result.unchanged += sub_res.unchanged
				result.failed += sub_res.failed
				result.errors.extend(sub_res.errors)

			except Exception as e:
				if not self.dry_run:
					frappe.db.rollback(save_point=savepoint)
				result.failed += 1
				result.errors.append({
					"external_id": p.external_id,
					"sku": p.sku,
					"error": str(e),
				})
				frappe.log_error(
					title=f"Product Import Error: {p.external_id}",
					message=str(e),
				)

		return result

	def _resolve_item_group(self, ps_category_id: Optional[str]) -> str:
		if ps_category_id:
			cat_map = self.get_active_mapping(ExternalEntityType.CATEGORY, ps_category_id)
			if cat_map and cat_map.erp_document and frappe.db.exists("Item Group", cat_map.erp_document):
				return cat_map.erp_document
		return "All Item Groups"

	def _import_single_product(self, p, raw: Dict[str, Any]) -> ImportResult:
		res = ImportResult(entity_type=ExternalEntityType.PRODUCT)
		has_combinations = bool(p.combination_ids)
		item_group = self._resolve_item_group(p.category_id)

		if not has_combinations:
			status = self._sync_simple_product(p, item_group)
			if status == "created":
				res.created += 1
			elif status == "updated":
				res.updated += 1
			else:
				res.unchanged += 1
		else:
			status = self._sync_template_and_variants(p, raw, item_group)
			res.merge(status)

		return res

	def _sync_simple_product(self, p, item_group: str) -> str:
		existing_map = self.get_active_mapping(ExternalEntityType.PRODUCT, p.external_id)
		desired_item_code = sanitize_item_code(p.sku, p.external_id)

		sync_payload = {
			"sku": desired_item_code,
			"name": p.name,
			"active": p.active,
			"item_group": item_group,
		}
		sync_hash = hashlib.sha256(json.dumps(sync_payload, sort_keys=True).encode("utf-8")).hexdigest()

		if existing_map and frappe.db.exists("Item", existing_map.erp_document):
			item_code = existing_map.erp_document
			if existing_map.sync_hash == sync_hash:
				return "unchanged"

			if not self.dry_run:
				doc = frappe.get_doc("Item", item_code)
				doc.item_name = p.name[:140]
				doc.disabled = 0 if p.active else 1
				doc.item_group = item_group
				doc.flags.ignore_permissions = True
				doc.save()

				self.set_mapping(
					ExternalEntityType.PRODUCT,
					p.external_id,
					"Item",
					item_code,
					sync_hash=sync_hash,
				)
			return "updated"

		# If not mapped, verify if item_code is taken by an unrelated item
		final_item_code = desired_item_code
		if frappe.db.exists("Item", final_item_code):
			# Collision with item not mapped to this external product
			final_item_code = f"{desired_item_code[:120]}-PS-{p.external_id}"[:MAX_ITEM_CODE_LENGTH]

		if not self.dry_run:
			item_doc = frappe.get_doc({
				"doctype": "Item",
				"item_code": final_item_code,
				"item_name": p.name[:140] or final_item_code,
				"item_group": item_group,
				"stock_uom": self._stock_uom,
				"is_stock_item": 1,
				"has_variants": 0,
				"disabled": 0 if p.active else 1,
			})
			item_doc.flags.ignore_permissions = True
			item_doc.insert()

			self.set_mapping(
				ExternalEntityType.PRODUCT,
				p.external_id,
				"Item",
				item_doc.name,
				sync_hash=sync_hash,
			)
			return "created"
		else:
			return "created"

	def _sync_template_and_variants(self, p, raw: Dict[str, Any], item_group: str) -> ImportResult:
		res = ImportResult(entity_type=ExternalEntityType.PRODUCT)

		# 1. Fetch all combinations first to determine required template attributes
		parsed_combinations = []
		template_attribute_names = set()

		for comb_id in p.combination_ids:
			try:
				comb_raw = self.client.get_combination(comb_id)
				comb = normalize_combination(comb_raw)
				parsed_combinations.append(comb)

				for aid in comb.attribute_ids:
					if aid in self._opt_val_cache:
						attr_name, _ = self._opt_val_cache[aid]
						template_attribute_names.add(attr_name)
			except Exception as comb_fetch_err:
				frappe.log_error(
					title=f"Combination Fetch Error: {comb_id}",
					message=str(comb_fetch_err),
				)

		# Fallback: if no attributes resolved, use a generic attribute
		if not template_attribute_names:
			default_attr = "PS-Option-Default"
			if not frappe.db.exists("Item Attribute", default_attr):
				if not self.dry_run:
					frappe.get_doc({
						"doctype": "Item Attribute",
						"item_attribute_name": default_attr,
						"item_attribute_values": [{"attribute_value": "Standard", "abbr": "STD"}],
					}).insert(ignore_permissions=True)
			template_attribute_names.add(default_attr)

		# 2. Sync Template Item
		template_sku = sanitize_item_code(p.sku, p.external_id)
		template_code_base = template_sku

		existing_tmpl_map = self.get_active_mapping(ExternalEntityType.PRODUCT, p.external_id)
		template_item_code = None

		sync_payload = {
			"sku": template_code_base,
			"name": p.name,
			"active": p.active,
			"item_group": item_group,
			"has_variants": 1,
			"attributes": sorted(list(template_attribute_names)),
		}
		tmpl_hash = hashlib.sha256(json.dumps(sync_payload, sort_keys=True).encode("utf-8")).hexdigest()

		if existing_tmpl_map and frappe.db.exists("Item", existing_tmpl_map.erp_document):
			template_item_code = existing_tmpl_map.erp_document
			if existing_tmpl_map.sync_hash == tmpl_hash:
				res.unchanged += 1
			else:
				if not self.dry_run:
					tmpl_doc = frappe.get_doc("Item", template_item_code)
					tmpl_doc.item_name = p.name[:140]
					tmpl_doc.disabled = 0 if p.active else 1
					tmpl_doc.item_group = item_group
					
					# Ensure attributes are present
					existing_attrs = {row.attribute for row in tmpl_doc.attributes}
					for attr_name in template_attribute_names:
						if attr_name not in existing_attrs:
							tmpl_doc.append("attributes", {"attribute": attr_name})

					tmpl_doc.flags.ignore_permissions = True
					tmpl_doc.save()
					self.set_mapping(
						ExternalEntityType.PRODUCT,
						p.external_id,
						"Item",
						template_item_code,
						sync_hash=tmpl_hash,
					)
				res.updated += 1
		else:
			final_tmpl_code = template_code_base
			if frappe.db.exists("Item", final_tmpl_code):
				final_tmpl_code = f"{template_code_base[:110]}-TMPL-{p.external_id}"[:MAX_ITEM_CODE_LENGTH]
			template_item_code = final_tmpl_code

			if not self.dry_run:
				tmpl_doc = frappe.get_doc({
					"doctype": "Item",
					"item_code": final_tmpl_code,
					"item_name": p.name[:140] or final_tmpl_code,
					"item_group": item_group,
					"stock_uom": self._stock_uom,
					"is_stock_item": 1,
					"has_variants": 1,
					"variant_based_on": "Item Attribute",
					"disabled": 0 if p.active else 1,
					"attributes": [{"attribute": a} for a in sorted(template_attribute_names)],
				})
				tmpl_doc.flags.ignore_permissions = True
				tmpl_doc.insert()
				self.set_mapping(
					ExternalEntityType.PRODUCT,
					p.external_id,
					"Item",
					tmpl_doc.name,
					sync_hash=tmpl_hash,
				)
			res.created += 1

		# 3. Sync Variants
		variant_results = self._sync_variants_for_product(
			p, parsed_combinations, template_item_code, item_group
		)
		res.merge(variant_results)
		return res

	def _sync_variants_for_product(
		self, p, combinations: List[Any], template_item_code: str, item_group: str
	) -> ImportResult:
		res = ImportResult(entity_type=ExternalEntityType.PRODUCT_VARIANT)

		for comb in combinations:
			res.seen += 1
			savepoint = f"comb_{comb.external_id}"
			try:
				if not self.dry_run:
					frappe.db.savepoint(savepoint)

				var_status = self._sync_single_variant(
					p, comb, template_item_code, item_group
				)

				if var_status == "created":
					res.created += 1
				elif var_status == "updated":
					res.updated += 1
				else:
					res.unchanged += 1

			except Exception as e:
				if not self.dry_run:
					frappe.db.rollback(save_point=savepoint)
				res.failed += 1
				res.errors.append({
					"product_id": p.external_id,
					"combination_id": comb.external_id,
					"error": str(e),
				})
				frappe.log_error(
					title=f"Variant Import Error: Prod {p.external_id} Comb {comb.external_id}",
					message=str(e),
				)

		return res

	def _sync_single_variant(
		self, p, comb, template_item_code: str, item_group: str
	) -> str:
		existing_var_map = self.get_active_mapping(
			ExternalEntityType.PRODUCT_VARIANT, p.external_id, external_variant_id=comb.external_id
		)

		# Resolve attributes from PrestaShop combination attribute_ids
		variant_attributes = []
		for aid in comb.attribute_ids:
			if aid in self._opt_val_cache:
				attr_name, val_name = self._opt_val_cache[aid]
				variant_attributes.append({
					"attribute": attr_name,
					"attribute_value": val_name,
				})

		desired_var_code = sanitize_item_code(comb.sku, f"{p.external_id}-{comb.external_id}")

		sync_payload = {
			"sku": desired_var_code,
			"template": template_item_code,
			"attributes": sorted([f"{a['attribute']}:{a['attribute_value']}" for a in variant_attributes]),
		}
		var_hash = hashlib.sha256(json.dumps(sync_payload, sort_keys=True).encode("utf-8")).hexdigest()

		if existing_var_map and frappe.db.exists("Item", existing_var_map.erp_document):
			var_item_code = existing_var_map.erp_document
			if existing_var_map.sync_hash == var_hash:
				return "unchanged"

			if not self.dry_run:
				vdoc = frappe.get_doc("Item", var_item_code)
				vdoc.item_name = f"{p.name} - {desired_var_code}"[:140]
				vdoc.disabled = 0 if p.active else 1
				vdoc.flags.ignore_permissions = True
				vdoc.save()

				self.set_mapping(
					ExternalEntityType.PRODUCT_VARIANT,
					p.external_id,
					"Item",
					var_item_code,
					external_variant_id=comb.external_id,
					sync_hash=var_hash,
				)
			return "updated"

		# New variant
		final_var_code = desired_var_code
		if frappe.db.exists("Item", final_var_code):
			final_var_code = f"{desired_var_code[:110]}-PS-{p.external_id}-{comb.external_id}"[:MAX_ITEM_CODE_LENGTH]

		if not self.dry_run:
			vdoc = frappe.get_doc({
				"doctype": "Item",
				"item_code": final_var_code,
				"item_name": f"{p.name} - {final_var_code}"[:140],
				"item_group": item_group,
				"stock_uom": self._stock_uom,
				"is_stock_item": 1,
				"has_variants": 0,
				"variant_of": template_item_code,
				"disabled": 0 if p.active else 1,
				"attributes": variant_attributes,
			})
			vdoc.flags.ignore_permissions = True
			vdoc.insert()

			self.set_mapping(
				ExternalEntityType.PRODUCT_VARIANT,
				p.external_id,
				"Item",
				vdoc.name,
				external_variant_id=comb.external_id,
				sync_hash=var_hash,
			)
			return "created"
		else:
			return "created"
