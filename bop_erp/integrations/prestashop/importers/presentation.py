# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
from typing import Dict, Any, List, Optional, Set
import frappe
from frappe import _
from frappe.utils.file_manager import save_file

from bop_erp.constants import ExternalEntityType
from bop_erp.integrations.prestashop.adapters.normalizers import extract_lang_field
from bop_erp.integrations.prestashop.importers.base import BaseImporter, ImportResult
from bop_erp.bop_erp.doctype.channel_category.channel_category import compute_channel_category_key
from bop_erp.bop_erp.doctype.item_media_asset.item_media_asset import compute_item_media_key


def _extract_localized_val(field_val: Any, lang_id: int) -> str:
	"""Extracts localized text matching specific PrestaShop language ID."""
	if not field_val:
		return ""
	if isinstance(field_val, list):
		for item in field_val:
			if isinstance(item, dict) and str(item.get("id")) == str(lang_id):
				return str(item.get("value", "")).strip()
	return ""


class PresentationImporter(BaseImporter):
	"""
	Authoritative Presentation and Media Inbound Synchronizer.
	Synchronizes:
	1. Channel Category storefront navigation tree (independent of master ERP Item Groups).
	2. Item Media Assets (shared physical file layer with SHA-256 deduplication).
	3. Item Channel Presentation (publication status, channel categories, media selection, localized marketing content).

	Guarantees:
	- Failure isolation per item presentation.
	- Respects dry_run flag (zero DB writes when True).
	- Preserves 100% idempotency upon re-import.
	- Zero stock ledger entries, zero bin modifications, zero price writes.
	"""

	def __init__(self, client, sales_channel: str, dry_run: bool = False):
		super().__init__(client, sales_channel, dry_run=dry_run)
		self._en_lang_id = 1
		self._es_lang_id = 7
		self._channel_cat_cache: Dict[str, str] = {}  # ps_cat_id -> channel_category_doc_name

	def sync_channel_categories(self, raw_categories: Optional[List[Dict[str, Any]]] = None) -> ImportResult:
		"""Synchronizes PrestaShop categories into storefront Channel Category records."""
		res = ImportResult(entity_type="CHANNEL_CATEGORY")
		if raw_categories is None:
			try:
				raw_categories = self.client.list_categories(limit=250, display="full")
			except Exception as e:
				frappe.log_error(title="PresentationImporter Channel Categories Fetch Error", message=str(e))
				res.failed += 1
				return res

		# Filter out system root categories (id=1, id=2)
		valid_cats = []
		cats_by_id = {}
		for c in raw_categories:
			cid = str(c.get("id", "")).strip()
			if not cid or cid in ("1", "2"):
				res.skipped += 1
				continue
			cats_by_id[cid] = c
			valid_cats.append(c)

		# Sort by depth so parents exist before children
		def get_depth(c, visited=None):
			if visited is None:
				visited = set()
			cid = str(c.get("id", "")).strip()
			pid = str(c.get("id_parent", "")).strip()
			if cid in visited or not pid or pid in ("0", "1", "2", cid) or pid not in cats_by_id:
				return 0
			visited.add(cid)
			return 1 + get_depth(cats_by_id[pid], visited)

		sorted_cats = sorted(valid_cats, key=get_depth)

		for c in sorted_cats:
			res.seen += 1
			cid = str(c.get("id")).strip()
			name = extract_lang_field(c.get("name"))
			link_rewrite = extract_lang_field(c.get("link_rewrite")) or frappe.scrub(name)
			slug = link_rewrite.strip().lower()
			active = str(c.get("active", "1")).strip() in ("1", "true", "True")
			parent_ps_id = str(c.get("id_parent", "")).strip()

			parent_channel_cat = None
			if parent_ps_id and parent_ps_id in self._channel_cat_cache:
				parent_channel_cat = self._channel_cat_cache[parent_ps_id]

			unique_key = compute_channel_category_key(self.sales_channel, slug)
			existing_name = frappe.db.get_value(
				"Channel Category",
				{"sales_channel": self.sales_channel, "unique_channel_slug": unique_key},
				"name",
			)

			if existing_name:
				self._channel_cat_cache[cid] = existing_name
				res.unchanged += 1
			else:
				if not self.dry_run:
					cat_doc = frappe.get_doc({
						"doctype": "Channel Category",
						"sales_channel": self.sales_channel,
						"category_name": name or f"Category {cid}",
						"category_slug": slug,
						"parent_channel_category": parent_channel_cat,
						"active": 1 if active else 0,
					})
					cat_doc.insert(ignore_permissions=True)
					self._channel_cat_cache[cid] = cat_doc.name
				else:
					self._channel_cat_cache[cid] = f"CC-{self.sales_channel}-{slug}"
				res.created += 1

		return res

	def sync_product_presentation_and_media(
		self, raw_products: Optional[List[Dict[str, Any]]] = None
	) -> ImportResult:
		"""Synchronizes presentation and media for products mapped to ERP Items."""
		res = ImportResult(entity_type="ITEM_CHANNEL_PRESENTATION")
		if raw_products is None:
			try:
				raw_products = self.client.list_products(limit=250, display="full")
			except Exception as e:
				frappe.log_error(title="PresentationImporter Products Fetch Error", message=str(e))
				res.failed += 1
				return res

		for p in raw_products:
			pid = str(p.get("id", "")).strip()
			if not pid:
				continue
			res.seen += 1

			# Find mapped ERP Item
			item_map = self.get_active_mapping(ExternalEntityType.PRODUCT, pid)
			if not item_map or not item_map.erp_document or not frappe.db.exists("Item", item_map.erp_document):
				res.skipped += 1
				continue

			item_code = item_map.erp_document
			savepoint = f"pres_{pid}"

			try:
				if not self.dry_run:
					frappe.db.savepoint(savepoint)

				status = self._sync_single_presentation(item_code, p)
				if status == "created":
					res.created += 1
				elif status == "updated":
					res.updated += 1
				else:
					res.unchanged += 1

			except Exception as e:
				if not self.dry_run:
					frappe.db.rollback(save_point=savepoint)
				res.failed += 1
				res.errors.append({"product_id": pid, "item_code": item_code, "error": str(e)})
				frappe.log_error(title=f"Presentation Sync Error: {pid}", message=str(e))

		return res

	def _sync_single_presentation(self, item_code: str, raw_product: Dict[str, Any]) -> str:
		pid = str(raw_product.get("id")).strip()
		name = extract_lang_field(raw_product.get("name"))
		link_rewrite = extract_lang_field(raw_product.get("link_rewrite")) or frappe.scrub(name)
		desc_short = extract_lang_field(raw_product.get("description_short"))
		desc_long = extract_lang_field(raw_product.get("description"))
		meta_title = extract_lang_field(raw_product.get("meta_title"))
		meta_desc = extract_lang_field(raw_product.get("meta_description"))
		meta_keywords = extract_lang_field(raw_product.get("meta_keywords"))
		active = str(raw_product.get("active", "1")).strip() in ("1", "true", "True")

		# Multilingual marketing extraction (ES)
		es_name = _extract_localized_val(raw_product.get("name"), self._es_lang_id)
		es_desc_short = _extract_localized_val(raw_product.get("description_short"), self._es_lang_id)
		es_desc_long = _extract_localized_val(raw_product.get("description"), self._es_lang_id)
		es_meta_title = _extract_localized_val(raw_product.get("meta_title"), self._es_lang_id)
		es_meta_desc = _extract_localized_val(raw_product.get("meta_description"), self._es_lang_id)

		# Media handling
		media_assets = []
		assocs = raw_product.get("associations", {})
		raw_images = []
		if isinstance(assocs, dict) and "images" in assocs:
			imgs = assocs["images"]
			if isinstance(imgs, list):
				raw_images = imgs

		cover_image_id = str(raw_product.get("id_default_image", "")).strip()
		if not cover_image_id and raw_images:
			cover_image_id = str(raw_images[0].get("id", "")).strip()

		for idx, img_info in enumerate(raw_images):
			img_id = str(img_info.get("id", "")).strip()
			if not img_id:
				continue
			asset_name = self._ensure_media_asset(item_code, pid, img_id, idx == 0)
			if asset_name:
				is_cover = 1 if (img_id == cover_image_id or (not cover_image_id and idx == 0)) else 0
				media_assets.append({
					"media_asset": asset_name,
					"is_cover": is_cover,
					"display_order": idx,
					"channel_alt_text": name,
				})

		# Channel categories
		channel_category_rows = []
		raw_cats = []
		if isinstance(assocs, dict) and "categories" in assocs:
			c_list = assocs["categories"]
			if isinstance(c_list, list):
				raw_cats = c_list

		default_cat_id = str(raw_product.get("id_category_default", "")).strip()
		seen_cats = set()
		for c_entry in raw_cats:
			cid = str(c_entry.get("id", "")).strip()
			if not cid or cid in ("1", "2"):
				continue
			channel_cat_doc = self._channel_cat_cache.get(cid)
			if not channel_cat_doc:
				# Try lookup
				slug_match = frappe.db.get_value(
					"Channel Category",
					{"sales_channel": self.sales_channel, "category_name": f"Category {cid}"},
					"name",
				)
				if slug_match:
					channel_cat_doc = slug_match

		primary_assigned = False
		# First pass: check if default_cat_id is in list
		for c_entry in raw_cats:
			cid = str(c_entry.get("id", "")).strip()
			if not cid or cid in ("1", "2"):
				continue
			channel_cat_doc = self._channel_cat_cache.get(cid)
			if channel_cat_doc and channel_cat_doc not in seen_cats:
				seen_cats.add(channel_cat_doc)
				is_default = 1 if (cid == default_cat_id and not primary_assigned) else 0
				if is_default:
					primary_assigned = True
				channel_category_rows.append({
					"channel_category": channel_cat_doc,
					"is_default": is_default,
				})

		# If no row got is_default=1 but rows exist, make the first row primary
		if channel_category_rows and not primary_assigned:
			channel_category_rows[0]["is_default"] = 1

		# Localized content rows
		localized_rows = []
		if es_name or es_desc_short or es_desc_long:
			localized_rows.append({
				"language": "es",
				"channel_item_name": es_name,
				"channel_short_description": es_desc_short,
				"channel_description": es_desc_long,
				"channel_meta_title": es_meta_title,
				"channel_meta_description": es_meta_desc,
			})

		# Check existing presentation
		existing_pres_name = frappe.db.get_value(
			"Item Channel Presentation",
			{"item": item_code, "sales_channel": self.sales_channel},
			"name",
		)

		if not self.dry_run:
			if existing_pres_name:
				pres = frappe.get_doc("Item Channel Presentation", existing_pres_name)
				pres.publication_status = "PUBLISHED" if active else "DISABLED"
				pres.channel_item_name = name
				pres.channel_slug = link_rewrite
				pres.channel_short_description = desc_short
				pres.channel_description = desc_long
				pres.channel_meta_title = meta_title
				pres.channel_meta_description = meta_desc
				pres.channel_meta_keywords = meta_keywords
				pres.set("channel_categories", channel_category_rows)
				pres.set("media_items", media_assets)
				pres.set("localized_content", localized_rows)
				pres.save(ignore_permissions=True)
				return "updated"
			else:
				pres = frappe.get_doc({
					"doctype": "Item Channel Presentation",
					"item": item_code,
					"sales_channel": self.sales_channel,
					"publication_status": "PUBLISHED" if active else "DISABLED",
					"channel_item_name": name,
					"channel_slug": link_rewrite,
					"channel_short_description": desc_short,
					"channel_description": desc_long,
					"channel_meta_title": meta_title,
					"channel_meta_description": meta_desc,
					"channel_meta_keywords": meta_keywords,
					"channel_categories": channel_category_rows,
					"media_items": media_assets,
					"localized_content": localized_rows,
				})
				pres.insert(ignore_permissions=True)
				return "created"
		else:
			return "updated" if existing_pres_name else "created"

	def _ensure_media_asset(self, item_code: str, product_id: str, image_id: str, is_primary: bool) -> Optional[str]:
		"""Fetches or links physical image file to Item Media Asset with SHA-256 deduplication."""
		# Deterministic uniqueness identifier based on external product and image ID
		dedup_tag = f"PS-{product_id}-{image_id}"
		unique_key = compute_item_media_key(item_code, dedup_tag)

		existing = frappe.db.get_value("Item Media Asset", {"asset_uniqueness_key": unique_key}, "name")
		if existing:
			return existing

		if self.dry_run:
			return f"MOCK-ASSET-{product_id}-{image_id}"

		try:
			img_bytes = self.client.get_product_image_binary(product_id, image_id)
			content_hash = hashlib.sha256(img_bytes).hexdigest()

			# Check if asset with this physical file content_hash already exists for this Item
			hash_key = compute_item_media_key(item_code, content_hash)
			existing_by_hash = frappe.db.get_value("Item Media Asset", {"asset_uniqueness_key": hash_key}, "name")
			if existing_by_hash:
				return existing_by_hash

			# Save file to Frappe public files
			filename = f"ps_{product_id}_{image_id}.jpg"
			file_doc = save_file(filename, img_bytes, None, None, is_private=0)

			asset = frappe.get_doc({
				"doctype": "Item Media Asset",
				"item": item_code,
				"file": file_doc.name,
				"media_type": "IMAGE",
				"title": f"PrestaShop Product {product_id} Image {image_id}",
				"content_hash": content_hash,
				"source": "PRESTASHOP_IMPORT",
				"is_primary": 1 if is_primary else 0,
			})
			asset.insert(ignore_permissions=True)
			return asset.name

		except Exception as e:
			frappe.log_error(title=f"Failed to ensure media asset {product_id}-{image_id}", message=str(e))
			return None
