# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from bop_erp.constants import ExternalEntityType
from bop_erp.bop_erp.doctype.channel_category.channel_category import compute_channel_category_key


def execute():
	# Ensure DocTypes are updated in schema
	frappe.reload_doc("bop_erp", "doctype", "item_channel_media_localized_content")
	frappe.reload_doc("bop_erp", "doctype", "item_channel_presentation")
	frappe.reload_doc("bop_erp", "doctype", "channel_category")
	frappe.reload_doc("bop_erp", "doctype", "external_id_mapping")

	# 1. Migrate Channel Categories with legacy ps-XX keys
	cats = frappe.get_all(
		"Channel Category",
		fields=["name", "sales_channel", "category_key", "category_name", "category_slug"],
	)
	for cat in cats:
		ckey = str(cat.category_key or "").strip()
		if ckey.startswith("ps-"):
			ext_id = ckey.replace("ps-", "").strip()
			neutral_key = f"cat_{frappe.generate_hash(length=12)}"
			frappe.db.set_value(
				"Channel Category",
				cat.name,
				{
					"category_key": neutral_key,
					"unique_channel_slug": compute_channel_category_key(cat.sales_channel, neutral_key),
				},
				update_modified=False,
			)

			# Ensure External ID Mapping exists
			if ext_id and not frappe.db.exists(
				"External ID Mapping",
				{
					"sales_channel": cat.sales_channel,
					"external_entity_type": ExternalEntityType.CATEGORY,
					"external_id": ext_id,
					"erp_doctype": "Channel Category",
					"erp_document": cat.name,
					"active": 1,
				},
			):
				m = frappe.get_doc({
					"doctype": "External ID Mapping",
					"sales_channel": cat.sales_channel,
					"provider": "PRESTASHOP",
					"external_entity_type": ExternalEntityType.CATEGORY,
					"external_id": ext_id,
					"erp_doctype": "Channel Category",
					"erp_document": cat.name,
					"active": 1,
				})
				m.insert(ignore_permissions=True)

	# 2. Migrate existing Item Channel Media alt texts to child table
	presentations = frappe.get_all("Item Channel Presentation", pluck="name")
	for pres_name in presentations:
		pres = frappe.get_doc("Item Channel Presentation", pres_name)
		modified = False
		existing_keys = {
			((r.media_asset or "").strip(), (r.language or "").strip().lower())
			for r in (pres.media_localized_content or [])
		}
		for m in (pres.media_items or []):
			asset = (m.media_asset or "").strip()
			if not asset:
				continue
			en_alt = getattr(m, "channel_alt_text", None)
			es_alt = getattr(m, "channel_alt_text_es", None)
			if en_alt and (asset, "en") not in existing_keys:
				pres.append("media_localized_content", {
					"media_asset": asset,
					"language": "en",
					"alt_text": en_alt,
				})
				existing_keys.add((asset, "en"))
				modified = True
			if es_alt and (asset, "es") not in existing_keys:
				pres.append("media_localized_content", {
					"media_asset": asset,
					"language": "es",
					"alt_text": es_alt,
				})
				existing_keys.add((asset, "es"))
				modified = True
		if modified:
			pres.save(ignore_permissions=True)
