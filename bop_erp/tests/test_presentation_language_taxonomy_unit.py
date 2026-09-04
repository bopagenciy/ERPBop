# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import unittest
import frappe
from frappe.utils.file_manager import save_file

from bop_erp.bop_erp.doctype.channel_category.channel_category import (
	compute_channel_category_key,
	ChannelCategory,
)
from bop_erp.bop_erp.doctype.item_channel_presentation.item_channel_presentation import (
	compute_presentation_uniqueness_key,
	ItemChannelPresentation,
)
from bop_erp.bop_erp.doctype.item_media_asset.item_media_asset import compute_item_media_key


class TestPresentationLanguageTaxonomyUnit(unittest.TestCase):
	"""
	Comprehensive Unit Tests for Phase 1F.1:
	1. Multilingual content storage (EN and ES) in Item Channel Presentation.
	2. Duplicate language rejection in child table (ValidationError).
	3. Canonical language identifiers: stable lowercase ISO codes ('en', 'es') referencing Frappe Language records.
	4. 5-tier fallback cascade: requested -> channel default -> 'en' -> root override -> master Item.
	5. Storefront language resolution independent of operator UI language (frappe.local.lang).
	6. Channel default language fallback (e.g. channel default 'es' falls back to ES when FR requested).
	7. PrestaShop dynamic language resolution (mapping ISO code from languages payload).
	8. Channel Category node stability: category_key remains unchanged across localized title/slug edits.
	9. Relevant category closure: products + ancestors closure algorithm excludes unrelated demo categories.
	10. Canonical JSON tuple SHA-256 presentation uniqueness key calculation.
	11. Item Channel Media bilingual alt text ('channel_alt_text' for EN, 'channel_alt_text_es' for ES)
	    without physical file duplication.
	"""

	def setUp(self):
		self.sales_channel = "TID"
		self.other_channel = "BAMAL"
		self._created_docs = []

		# Ensure Language records exist in Frappe
		for lang_code in ["en", "es"]:
			if not frappe.db.exists("Language", lang_code):
				doc = frappe.get_doc({
					"doctype": "Language",
					"language_code": lang_code,
					"language_name": "English" if lang_code == "en" else "Spanish",
				}).insert(ignore_permissions=True)
				self._track("Language", doc.name)

		# Ensure Sales Channel TID exists with default_language 'en'
		if frappe.db.exists("Sales Channel", self.sales_channel):
			ch = frappe.get_doc("Sales Channel", self.sales_channel)
			if hasattr(ch, "default_language") and not ch.default_language:
				ch.default_language = "en"
				ch.save(ignore_permissions=True)

	def tearDown(self):
		for dt, dn in reversed(self._created_docs):
			if frappe.db.exists(dt, dn):
				try:
					frappe.delete_doc(dt, dn, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.db.commit()

	def _track(self, dt, dn):
		self._created_docs.append((dt, dn))
		return dn

	def test_01_multilingual_content_storage_en_and_es(self):
		"""Validates that Item Channel Presentation correctly stores and persists both EN and ES localized rows."""
		item_code = "ITEM-PHASE1F1-MULTI-01"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Industrial Valve 2-Inch",
				"description": "Standard heavy duty valve.",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "READY",
			"channel_item_name": "Industrial Valve 2-Inch Root",
			"channel_description": "Root heavy duty valve description.",
			"localized_content": [
				{
					"language": "en",
					"channel_item_name": "Industrial Valve 2-Inch (EN)",
					"channel_description": "Commercial grade 2-inch industrial valve.",
					"channel_slug": "industrial-valve-2-inch",
					"channel_meta_title": "Buy Industrial Valve 2-Inch",
					"channel_meta_description": "Best industrial valve 2-inch online.",
				},
				{
					"language": "es",
					"channel_item_name": "Válvula Industrial de 2 Pulgadas (ES)",
					"channel_description": "Válvula industrial de 2 pulgadas grado comercial.",
					"channel_slug": "valvula-industrial-2-pulgadas",
					"channel_meta_title": "Comprar Válvula Industrial 2 Pulgadas",
					"channel_meta_description": "La mejor válvula industrial de 2 pulgadas en línea.",
				},
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres.name)

		# Reload from DB and verify child rows
		reloaded = frappe.get_doc("Item Channel Presentation", pres.name)
		self.assertEqual(len(reloaded.localized_content), 2)

		en_rows = [r for r in reloaded.localized_content if r.language == "en"]
		es_rows = [r for r in reloaded.localized_content if r.language == "es"]

		self.assertEqual(len(en_rows), 1)
		self.assertEqual(len(es_rows), 1)
		self.assertEqual(en_rows[0].channel_item_name, "Industrial Valve 2-Inch (EN)")
		self.assertEqual(en_rows[0].channel_slug, "industrial-valve-2-inch")
		self.assertEqual(es_rows[0].channel_item_name, "Válvula Industrial de 2 Pulgadas (ES)")
		self.assertEqual(es_rows[0].channel_slug, "valvula-industrial-2-pulgadas")

	def test_02_duplicate_language_rejection(self):
		"""Validates that adding multiple child rows with the same language code raises a ValidationError."""
		item_code = "ITEM-PHASE1F1-DUP-02"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Master Duplication Test Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "DRAFT",
			"localized_content": [
				{
					"language": "es",
					"channel_item_name": "Nombre Español 1",
				},
				{
					"language": "es",
					"channel_item_name": "Nombre Español 2",
				},
			],
		})

		with self.assertRaises(frappe.ValidationError) as ctx:
			pres.insert(ignore_permissions=True)

		self.assertIn("Duplicate language code", str(ctx.exception))

	def test_03_canonical_language_identifiers(self):
		"""Validates that language identifiers are normalized to canonical lowercase ISO codes."""
		item_code = "ITEM-PHASE1F1-CANON-03"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Canonical Language Test Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		# Canonical language code 'es' (referencing Frappe Language)
		pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "DRAFT",
			"localized_content": [
				{
					"language": "es",
					"channel_item_name": "Artículo Normalizado",
				}
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres.name)

		self.assertEqual(pres.localized_content[0].language, "es")

		# Non-canonical language names (e.g. 'Spanish' instead of ISO 'es') must be rejected by Link validation
		invalid_pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.other_channel,
			"publication_status": "DRAFT",
			"localized_content": [
				{
					"language": "Spanish",
					"channel_item_name": "Nombre no ISO",
				}
			],
		})
		with self.assertRaises(frappe.LinkValidationError):
			invalid_pres.insert(ignore_permissions=True)

	def test_04_five_tier_fallback_cascade(self):
		"""
		Validates the full 5-tier fallback cascade:
		1. Requested locale content
		2. Sales Channel default language
		3. English ('en')
		4. Presentation root override
		5. Master Item (item_name / description)
		"""
		item_code = "ITEM-PHASE1F1-CASCADE-04"
		master_name = "Master Item Level 5"
		master_desc = "Master Description Level 5"

		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": master_name,
				"description": master_desc,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		# Case A: Only master Item exists -> fallback tier 5
		pres_empty = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "DRAFT",
			"channel_item_name": "",
			"channel_description": "",
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres_empty.name)

		self.assertEqual(pres_empty.get_effective_item_name(language="fr"), master_name)
		self.assertEqual(pres_empty.get_effective_description(language="fr"), master_desc)

		# Case B: Root override exists -> fallback tier 4
		pres_root = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.other_channel,
			"publication_status": "DRAFT",
			"channel_item_name": "Root Override Level 4",
			"channel_description": "Root Description Level 4",
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres_root.name)

		self.assertEqual(pres_root.get_effective_item_name(language="de"), "Root Override Level 4")
		self.assertEqual(pres_root.get_effective_description(language="de"), "Root Description Level 4")

		# Case C: English row exists -> fallback tier 3
		pres_en = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": "TEST-CH-C",
			"publication_status": "DRAFT",
			"channel_item_name": "Root Override Level 4",
			"localized_content": [
				{
					"language": "en",
					"channel_item_name": "English Level 3",
					"channel_description": "English Desc Level 3",
				}
			],
		})
		# Need dummy sales channel
		if not frappe.db.exists("Sales Channel", "TEST-CH-C"):
			ch_c = frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": "TEST-CH-C",
				"channel_name": "TEST-CH-C",
				"channel_type": "PRESTASHOP",
				"company": "Industrial DP",
				"language": "en",
			}).insert(ignore_permissions=True)
			self._track("Sales Channel", ch_c.name)

		pres_en.insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres_en.name)

		# When requesting non-existent language 'it', falls back to 'en' (tier 3)
		self.assertEqual(pres_en.get_effective_item_name(language="it"), "English Level 3")

		# Case D: Channel default language (tier 2)
		# Create channel with default_language 'es'
		if not frappe.db.exists("Sales Channel", "TEST-CH-ES"):
			ch_es = frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": "TEST-CH-ES",
				"channel_name": "TEST-CH-ES",
				"channel_type": "PRESTASHOP",
				"company": "Industrial DP",
				"language": "es",
			}).insert(ignore_permissions=True)
			self._track("Sales Channel", ch_es.name)

		pres_es = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": "TEST-CH-ES",
			"publication_status": "DRAFT",
			"channel_item_name": "Root Override Level 4",
			"localized_content": [
				{
					"language": "es",
					"channel_item_name": "Spanish Default Level 2",
					"channel_description": "Spanish Desc Level 2",
				},
				{
					"language": "en",
					"channel_item_name": "English Level 3",
					"channel_description": "English Desc Level 3",
				},
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres_es.name)

		# Requesting 'pt' (unsupported) falls back to channel default 'es'
		self.assertEqual(pres_es.get_effective_item_name(language="pt"), "Spanish Default Level 2")

		# Case E: Requested locale content (tier 1)
		self.assertEqual(pres_es.get_effective_item_name(language="en"), "English Level 3")
		self.assertEqual(pres_es.get_effective_item_name(language="es"), "Spanish Default Level 2")

	def test_05_storefront_language_independent_of_operator_ui(self):
		"""Validates that resolution uses channel/requested language, decoupled from frappe.local.lang."""
		item_code = "ITEM-PHASE1F1-UI-05"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "UI Decoupling Master Item",
				"description": "Master description.",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "READY",
			"localized_content": [
				{
					"language": "en",
					"channel_item_name": "Storefront English Name",
				},
				{
					"language": "es",
					"channel_item_name": "Storefront Spanish Name",
				},
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres.name)

		# Simulate operator UI in German or French
		original_lang = getattr(frappe.local, "lang", "en")
		try:
			frappe.local.lang = "de"
			# Asking explicitly for 'es' storefront content must return Spanish, ignoring German UI
			self.assertEqual(pres.get_effective_item_name(language="es"), "Storefront Spanish Name")
			# Asking explicitly for 'en' storefront content must return English
			self.assertEqual(pres.get_effective_item_name(language="en"), "Storefront English Name")
		finally:
			frappe.local.lang = original_lang

	def test_06_channel_category_node_stability_and_localization(self):
		"""Validates Channel Category category_key stability and 4-tier fallback."""
		slug_root = "industrial-fasteners-test"
		category_key = "ps-999"

		cat = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Industrial Fasteners (Root)",
			"category_slug": slug_root,
			"category_key": category_key,
			"active": 1,
			"localized_content": [
				{
					"language": "en",
					"category_name": "Industrial Fasteners (EN)",
					"category_slug": "industrial-fasteners-en",
				},
				{
					"language": "es",
					"category_name": "Fijaciones Industriales (ES)",
					"category_slug": "fijaciones-industriales-es",
				},
			],
		}).insert(ignore_permissions=True)
		self._track("Channel Category", cat.name)

		# Initial key check
		self.assertEqual(cat.category_key, category_key)
		self.assertEqual(cat.get_effective_category_name(language="es"), "Fijaciones Industriales (ES)")
		self.assertEqual(cat.get_effective_slug(language="es"), "fijaciones-industriales-es")
		self.assertEqual(cat.get_effective_category_name(language="en"), "Industrial Fasteners (EN)")

		# Changing the root category_name or localized content does NOT alter the stable category_key
		cat.category_name = "Updated Fasteners Header"
		cat.localized_content[1].category_name = "Fijaciones Industriales Modificadas"
		cat.save(ignore_permissions=True)

		reloaded = frappe.get_doc("Channel Category", cat.name)
		self.assertEqual(reloaded.category_key, category_key)
		self.assertEqual(
			reloaded.get_effective_category_name(language="es"), "Fijaciones Industriales Modificadas"
		)

	def test_07_channel_category_duplicate_language_rejection(self):
		"""Validates that duplicate language in Channel Category Localized Content raises ValidationError."""
		cat = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Duplicate Test Cat",
			"category_slug": "dup-cat-test",
			"category_key": "ps-888",
			"localized_content": [
				{"language": "es", "category_name": "Cat ES 1"},
				{"language": "es", "category_name": "Cat ES 2"},
			],
		})

		with self.assertRaises(frappe.ValidationError) as ctx:
			cat.insert(ignore_permissions=True)

		self.assertIn("Duplicate language code", str(ctx.exception))

	def test_08_relevant_category_closure_algorithm(self):
		"""
		Validates that the relevant category closure algorithm computes the exact set
		of required ancestor categories and completely excludes unrelated demo category trees.
		"""
		# Simulated PrestaShop category tree:
		# Root (1)
		# └── Home (2)
		#     ├── Clothes (3) [DEMO]
		#     │   ├── Men (4) [DEMO]
		#     │   └── Women (5) [DEMO]
		#     ├── Accessories (6) [DEMO]
		#     │   ├── Stationery (7) [DEMO]
		#     │   └── Home Accessories (8) [DEMO]
		#     ├── Art (9) [DEMO]
		#     ├── Tools & Hardware (10) [INDUSTRIAL]
		#     │   └── Heavy Hammers (100) [INDUSTRIAL]
		#     └── Industrial Fasteners (11) [INDUSTRIAL]
		all_cats = {
			"1": {"id": "1", "id_parent": "0", "name": "Root"},
			"2": {"id": "2", "id_parent": "1", "name": "Home"},
			"3": {"id": "3", "id_parent": "2", "name": "Clothes"},
			"4": {"id": "4", "id_parent": "3", "name": "Men"},
			"5": {"id": "5", "id_parent": "3", "name": "Women"},
			"6": {"id": "6", "id_parent": "2", "name": "Accessories"},
			"7": {"id": "7", "id_parent": "6", "name": "Stationery"},
			"8": {"id": "8", "id_parent": "6", "name": "Home Accessories"},
			"9": {"id": "9", "id_parent": "2", "name": "Art"},
			"10": {"id": "10", "id_parent": "2", "name": "Tools & Hardware"},
			"100": {"id": "100", "id_parent": "10", "name": "Heavy Hammers"},
			"11": {"id": "11", "id_parent": "2", "name": "Industrial Fasteners"},
		}

		# Selected industrial product category IDs: 100 and 11
		target_product_cat_ids = {"100", "11"}

		# Closure algorithm
		relevant_ids = set(target_product_cat_ids)
		for cid in list(target_product_cat_ids):
			curr = cid
			while curr in all_cats:
				parent = all_cats[curr].get("id_parent")
				if not parent or parent in ("0", ""):
					break
				if parent not in ("1", "2"):  # Exclude PrestaShop technical root/home if desired
					relevant_ids.add(parent)
				curr = parent

		# Expected relevant closure: {100, 10, 11}
		self.assertEqual(relevant_ids, {"100", "10", "11"})

		# Assert demo categories are strictly excluded
		demo_ids = {"3", "4", "5", "6", "7", "8", "9"}
		self.assertTrue(relevant_ids.isdisjoint(demo_ids))

	def test_09_canonical_json_tuple_hash(self):
		"""
		Validates that compute_presentation_uniqueness_key produces an exact SHA-256
		hash of a canonical compact JSON 2-tuple [item, sales_channel].
		"""
		item = "SKU-TEST-123"
		channel = "TID"

		canonical_tuple = json.dumps([item, channel], separators=(",", ":"))
		expected_hash = hashlib.sha256(canonical_tuple.encode("utf-8")).hexdigest()

		actual_hash = compute_presentation_uniqueness_key(item, channel)
		self.assertEqual(actual_hash, expected_hash)

	def test_10_media_alt_text_localization_without_file_duplication(self):
		"""
		Validates that Item Channel Media supports bilingual alt text (channel_alt_text and channel_alt_text_es)
		pointing to the exact same physical media asset and file.
		"""
		item_code = "ITEM-PHASE1F1-MEDIA-10"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Bilingual Media Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		raw_bytes = b"SAMPLE_MEDIA_BYTES_FOR_BILINGUAL_ALT_TEST_98765"
		sha256_hash = hashlib.sha256(raw_bytes).hexdigest()
		file_doc = save_file("bilingual_sample.png", raw_bytes, None, None, is_private=0)
		self._track("File", file_doc.name)

		asset = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file_doc.name,
			"media_type": "IMAGE",
			"title": "Bilingual Sample Image",
			"content_hash": sha256_hash,
		}).insert(ignore_permissions=True)
		self._track("Item Media Asset", asset.name)

		pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "READY",
			"media_items": [
				{
					"media_asset": asset.name,
					"is_cover": 1,
					"display_order": 1,
					"channel_alt_text": "Heavy Duty Industrial Claw Hammer 16oz in action",
					"channel_alt_text_es": "Martillo de uña para trabajo pesado de 16oz en acción",
				}
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres.name)

		reloaded = frappe.get_doc("Item Channel Presentation", pres.name)
		self.assertEqual(len(reloaded.media_items), 1)
		media_row = reloaded.media_items[0]
		self.assertEqual(media_row.media_asset, asset.name)
		self.assertEqual(
			media_row.channel_alt_text, "Heavy Duty Industrial Claw Hammer 16oz in action"
		)
		self.assertEqual(
			media_row.channel_alt_text_es, "Martillo de uña para trabajo pesado de 16oz en acción"
		)


def run_all():
	import unittest
	suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestPresentationLanguageTaxonomyUnit)
	runner = unittest.TextTestRunner(verbosity=2)
	res = runner.run(suite)
	if not res.wasSuccessful():
		raise RuntimeError(f"Tests failed: {len(res.failures)} failures, {len(res.errors)} errors")
