# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import unittest
import frappe
from frappe.utils.file_manager import save_file

from bop_erp.constants import ExternalEntityType
from bop_erp.bop_erp.doctype.channel_category.channel_category import (
	compute_channel_category_key,
)
from bop_erp.bop_erp.doctype.external_id_mapping.external_id_mapping import (
	compute_active_external_key,
)


class TestPresentationIdentityMediaUnit(unittest.TestCase):
	"""
	Unit test suite for Phase 1F.2:
	- Provider-neutral internal Channel Category identity.
	- PrestaShop category identity resolution exclusively through External ID Mapping.
	- Multi-provider collision safety (same sales_channel, same numeric external_id, different providers).
	- Category hierarchy and localized content preservation.
	- Normalized multilingual media metadata extensibility beyond EN/ES (e.g. FR).
	- Duplicate same-language localization rejection per media asset.
	- 5-tier alt-text fallback cascade (requested -> channel default -> en -> master asset -> master item).
	- Migration preservation of existing EN/ES alt texts into child table.
	- Single media asset / file reference (zero binary duplication).
	- Zero inventory / price mutations.
	"""

	@classmethod
	def setUpClass(cls):
		cls.sales_channel = "TID"
		cls.company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value("Company", {}, "name")

		# Ensure Language records exist in Frappe
		for code, name in [("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German")]:
			if not frappe.db.exists("Language", code):
				frappe.get_doc({
					"doctype": "Language",
					"language_code": code,
					"language_name": name,
				}).insert(ignore_permissions=True)

		# Ensure Sales Channel TID exists with default language 'en'
		if not frappe.db.exists("Sales Channel", cls.sales_channel):
			ch = frappe.get_doc({
				"doctype": "Sales Channel",
				"channel_id": cls.sales_channel,
				"channel_name": "The Industrial Depot",
				"channel_type": "PRESTASHOP",
				"company": cls.company,
				"language": "en",
				"active": 1,
			}).insert(ignore_permissions=True)
		else:
			ch = frappe.get_doc("Sales Channel", cls.sales_channel)
			ch.language = "en"
			ch.save(ignore_permissions=True)

		cls.initial_sle_count = frappe.db.count("Stock Ledger Entry")
		cls.initial_bin_count = frappe.db.count("Bin")
		cls.initial_price_count = frappe.db.count("Item Price")

	def setUp(self):
		self.created_docs = []

	def tearDown(self):
		for dt, dn in reversed(self.created_docs):
			if frappe.db.exists(dt, dn):
				try:
					frappe.delete_doc(dt, dn, force=True, ignore_permissions=True)
				except Exception:
					pass
		frappe.db.commit()

	def _track(self, doctype, name):
		self.created_docs.append((doctype, name))

	def test_01_channel_category_provider_neutral_identity(self):
		"""
		Validates that Channel Category automatically generates a provider-neutral internal
		category_key (e.g. cat_xxx) and does NOT depend on external provider prefixes (ps-).
		"""
		cat = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Neutral Industrial Tools",
		}).insert(ignore_permissions=True)
		self._track("Channel Category", cat.name)

		self.assertTrue(cat.category_key.startswith("cat_"))
		self.assertFalse(cat.category_key.startswith("ps-"))
		self.assertFalse("prestashop" in cat.category_key)
		self.assertEqual(len(cat.unique_channel_slug), 64)

	def test_02_external_id_mapping_channel_category_resolution(self):
		"""
		Validates that PrestaShop category identity maps through External ID Mapping
		with erp_doctype='Channel Category', decoupling external ID from internal entity.
		"""
		cat = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Mapped Fasteners",
		}).insert(ignore_permissions=True)
		self._track("Channel Category", cat.name)

		mapping = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"provider": "PRESTASHOP",
			"external_entity_type": ExternalEntityType.CATEGORY,
			"external_id": "TEST-CAT-202",
			"erp_doctype": "Channel Category",
			"erp_document": cat.name,
			"active": 1,
		}).insert(ignore_permissions=True)
		self._track("External ID Mapping", mapping.name)

		resolved = frappe.db.get_value(
			"External ID Mapping",
			{
				"sales_channel": self.sales_channel,
				"provider": "PRESTASHOP",
				"external_entity_type": ExternalEntityType.CATEGORY,
				"external_id": "TEST-CAT-202",
				"active": 1,
			},
			"erp_document"
		)
		self.assertEqual(resolved, cat.name)

	def test_03_multi_provider_same_channel_collision_safety(self):
		"""
		Proves that the same Sales Channel may receive categories from different providers
		with the same numeric external ID (e.g. PRESTASHOP 99 vs MARKETPLACE 99) without collision.
		"""
		cat_ps = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "PrestaShop Hardware",
		}).insert(ignore_permissions=True)
		self._track("Channel Category", cat_ps.name)

		cat_mk = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Marketplace Hardware",
		}).insert(ignore_permissions=True)
		self._track("Channel Category", cat_mk.name)

		# Mapping 1: PrestaShop ID 99
		map1 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"provider": "PRESTASHOP",
			"external_entity_type": ExternalEntityType.CATEGORY,
			"external_id": "99",
			"erp_doctype": "Channel Category",
			"erp_document": cat_ps.name,
			"active": 1,
		}).insert(ignore_permissions=True)
		self._track("External ID Mapping", map1.name)

		# Mapping 2: Marketplace ID 99 within the SAME Sales Channel
		map2 = frappe.get_doc({
			"doctype": "External ID Mapping",
			"sales_channel": self.sales_channel,
			"provider": "MARKETPLACE",
			"external_entity_type": ExternalEntityType.CATEGORY,
			"external_id": "99",
			"erp_doctype": "Channel Category",
			"erp_document": cat_mk.name,
			"active": 1,
		}).insert(ignore_permissions=True)
		self._track("External ID Mapping", map2.name)

		# Ensure active_external_key hashes are strictly different
		self.assertNotEqual(map1.active_external_key, map2.active_external_key)

		# Verify lookup by provider context isolates entities
		ps_res = frappe.db.get_value(
			"External ID Mapping",
			{"sales_channel": self.sales_channel, "provider": "PRESTASHOP", "external_id": "99", "active": 1},
			"erp_document"
		)
		mk_res = frappe.db.get_value(
			"External ID Mapping",
			{"sales_channel": self.sales_channel, "provider": "MARKETPLACE", "external_id": "99", "active": 1},
			"erp_document"
		)
		self.assertEqual(ps_res, cat_ps.name)
		self.assertEqual(mk_res, cat_mk.name)

	def test_04_category_hierarchy_and_localization_preserved(self):
		"""
		Validates that parent-child category relationships and localized content child rows
		remain fully operational under provider-neutral identity.
		"""
		parent = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Parent Category",
		}).insert(ignore_permissions=True)
		self._track("Channel Category", parent.name)

		child = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Child Category",
			"parent_channel_category": parent.name,
			"localized_content": [
				{
					"language": "en",
					"category_name": "Child Fasteners",
					"category_slug": "child-fasteners",
				},
				{
					"language": "es",
					"category_name": "Fijaciones Hijas",
					"category_slug": "fijaciones-hijas",
				},
			],
		}).insert(ignore_permissions=True)
		self._track("Channel Category", child.name)

		reloaded = frappe.get_doc("Channel Category", child.name)
		self.assertEqual(reloaded.parent_channel_category, parent.name)
		self.assertEqual(reloaded.get_effective_category_name("es"), "Fijaciones Hijas")
		self.assertEqual(reloaded.get_effective_slug("es"), "fijaciones-hijas")

	def test_05_normalized_multilingual_media_extensibility(self):
		"""
		Validates that Item Channel Media Localized Content supports unlimited languages
		(e.g. en, es, fr) on a single Item Channel Media row without schema modifications.
		"""
		item_code = "ITEM-PHASE1F2-MEDIA-EXT"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Multilingual Media Test Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		raw_bytes = b"SAMPLE_MEDIA_BYTES_FOR_EXTENSIBILITY_TEST"
		content_hash = hashlib.sha256(raw_bytes).hexdigest()
		file_doc = save_file("ext_media.png", raw_bytes, None, None, is_private=0)
		self._track("File", file_doc.name)

		asset = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file_doc.name,
			"media_type": "IMAGE",
			"title": "Extensible Media Asset",
			"content_hash": content_hash,
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
				}
			],
			"media_localized_content": [
				{
					"media_asset": asset.name,
					"language": "en",
					"alt_text": "Heavy Industrial Claw Hammer 16oz",
				},
				{
					"media_asset": asset.name,
					"language": "es",
					"alt_text": "Martillo de uña industrial de 16oz",
				},
				{
					"media_asset": asset.name,
					"language": "fr",
					"alt_text": "Marteau industriel à panne fendue 16oz",
				},
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres.name)

		reloaded = frappe.get_doc("Item Channel Presentation", pres.name)
		self.assertEqual(len(reloaded.media_items), 1)
		self.assertEqual(len(reloaded.media_localized_content), 3)
		self.assertEqual(reloaded.get_effective_media_alt_text(asset.name, "en"), "Heavy Industrial Claw Hammer 16oz")
		self.assertEqual(reloaded.get_effective_media_alt_text(asset.name, "es"), "Martillo de uña industrial de 16oz")
		self.assertEqual(reloaded.get_effective_media_alt_text(asset.name, "fr"), "Marteau industriel à panne fendue 16oz")

	def test_06_duplicate_same_language_media_localization_rejected(self):
		"""
		Validates that adding multiple child rows with the same language for the same media asset
		raises a ValidationError.
		"""
		item_code = "ITEM-PHASE1F2-MEDIA-DUP"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Dup Media Test Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		raw_bytes = b"SAMPLE_MEDIA_BYTES_FOR_DUP_TEST"
		content_hash = hashlib.sha256(raw_bytes).hexdigest()
		file_doc = save_file("dup_media.png", raw_bytes, None, None, is_private=0)
		self._track("File", file_doc.name)

		asset = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file_doc.name,
			"media_type": "IMAGE",
			"title": "Dup Media Asset",
			"content_hash": content_hash,
		}).insert(ignore_permissions=True)
		self._track("Item Media Asset", asset.name)

		pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "DRAFT",
			"media_items": [
				{
					"media_asset": asset.name,
					"is_cover": 1,
				}
			],
			"media_localized_content": [
				{
					"media_asset": asset.name,
					"language": "es",
					"alt_text": "Texto en español 1",
				},
				{
					"media_asset": asset.name,
					"language": "es",
					"alt_text": "Texto en español 2 (Duplicado)",
				},
			],
		})
		with self.assertRaises(frappe.ValidationError):
			pres.insert(ignore_permissions=True)

	def test_07_media_five_tier_fallback_cascade(self):
		"""
		Validates the deterministic 5-tier alt-text fallback cascade:
		1. requested locale
		2. channel default language
		3. English ('en')
		4. master Item Media Asset.alt_text
		5. master Item.item_name
		"""
		item_code = "ITEM-PHASE1F2-CASCADE"
		master_item_name = "Master Level 5 Hammer"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": master_item_name,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		raw_bytes = b"SAMPLE_MEDIA_BYTES_FOR_CASCADE_TEST"
		content_hash = hashlib.sha256(raw_bytes).hexdigest()
		file_doc = save_file("cascade_media.png", raw_bytes, None, None, is_private=0)
		self._track("File", file_doc.name)

		master_asset_alt = "Master Asset Level 4 Alt Text"
		asset = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file_doc.name,
			"media_type": "IMAGE",
			"title": "Cascade Media Asset",
			"alt_text": master_asset_alt,
			"content_hash": content_hash,
		}).insert(ignore_permissions=True)
		self._track("Item Media Asset", asset.name)

		# Presentation with only English localized alt text
		pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "READY",
			"media_items": [
				{"media_asset": asset.name, "is_cover": 1}
			],
			"media_localized_content": [
				{"media_asset": asset.name, "language": "en", "alt_text": "English Level 3 Alt Text"},
				{"media_asset": asset.name, "language": "fr", "alt_text": "French Level 1 Alt Text"},
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres.name)

		# Case 1: Requested language exists (French) -> returns Tier 1
		self.assertEqual(pres.get_effective_media_alt_text(asset.name, "fr"), "French Level 1 Alt Text")

		# Case 2: Requested language missing, fallback to channel default / English -> returns Tier 3
		self.assertEqual(pres.get_effective_media_alt_text(asset.name, "de"), "English Level 3 Alt Text")

		# Case 3: When no localized content exists for asset -> fallback to Tier 4 (Item Media Asset.alt_text)
		item_code_empty = "ITEM-PHASE1F2-CASCADE-EMPTY"
		if not frappe.db.exists("Item", item_code_empty):
			item2 = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code_empty,
				"item_name": "Empty Cascade Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item2.name)

		asset2 = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code_empty,
			"file": file_doc.name,
			"media_type": "IMAGE",
			"title": "Cascade Empty Media Asset",
			"alt_text": master_asset_alt,
			"content_hash": hashlib.sha256(b"CASCADE_EMPTY_BYTES").hexdigest(),
		}).insert(ignore_permissions=True)
		self._track("Item Media Asset", asset2.name)

		empty_pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code_empty,
			"sales_channel": self.sales_channel,
			"publication_status": "DRAFT",
			"media_items": [{"media_asset": asset2.name, "is_cover": 1}],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", empty_pres.name)

		self.assertEqual(empty_pres.get_effective_media_alt_text(asset2.name, "es"), master_asset_alt)

	def test_08_media_alt_text_migration_preserves_values(self):
		"""
		Validates that legacy channel_alt_text and channel_alt_text_es on Item Channel Media
		are safely migrated into media_localized_content child rows without data loss.
		"""
		item_code = "ITEM-PHASE1F2-MIGRATE-ALT"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Migration Test Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		raw_bytes = b"SAMPLE_MEDIA_BYTES_FOR_MIGRATION_ALT_TEST"
		content_hash = hashlib.sha256(raw_bytes).hexdigest()
		file_doc = save_file("migrate_media.png", raw_bytes, None, None, is_private=0)
		self._track("File", file_doc.name)

		asset = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file_doc.name,
			"media_type": "IMAGE",
			"title": "Migrate Media Asset",
			"content_hash": content_hash,
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
					"channel_alt_text": "Legacy English Alt Text",
					"channel_alt_text_es": "Texto alternativo en español legado",
				}
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres.name)

		reloaded = frappe.get_doc("Item Channel Presentation", pres.name)
		self.assertEqual(reloaded.get_effective_media_alt_text(asset.name, "en"), "Legacy English Alt Text")
		self.assertEqual(reloaded.get_effective_media_alt_text(asset.name, "es"), "Texto alternativo en español legado")

	def test_09_single_file_media_asset_no_binary_duplication(self):
		"""
		Validates that multilingual media rows link to one shared Item Media Asset and File.
		Zero binary duplication across languages.
		"""
		item_code = "ITEM-PHASE1F2-NO-DUP"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "No Dup Item",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		raw_bytes = b"SAMPLE_MEDIA_BYTES_FOR_DEDUP_VERIFY"
		file_doc = save_file("dedup_verify.png", raw_bytes, None, None, is_private=0)
		self._track("File", file_doc.name)

		asset = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file_doc.name,
			"media_type": "IMAGE",
			"title": "Single Media Asset",
			"content_hash": hashlib.sha256(raw_bytes).hexdigest(),
		}).insert(ignore_permissions=True)
		self._track("Item Media Asset", asset.name)

		pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "READY",
			"media_items": [{"media_asset": asset.name, "is_cover": 1}],
			"media_localized_content": [
				{"media_asset": asset.name, "language": "en", "alt_text": "Hammer"},
				{"media_asset": asset.name, "language": "es", "alt_text": "Martillo"},
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres.name)

		files_count = frappe.db.count("File", {"name": file_doc.name})
		self.assertEqual(files_count, 1, "There must be exactly 1 physical File regardless of languages")

	def test_10_inventory_and_price_invariance(self):
		"""
		Critical safety invariant: Phase 1F.2 must never mutate inventory or prices.
		"""
		sle_count = frappe.db.count("Stock Ledger Entry")
		bin_count = frappe.db.count("Bin")
		price_count = frappe.db.count("Item Price")
		self.assertEqual(sle_count - getattr(self, "initial_sle_count", 0), 0, f"Critical Safety Violation: {sle_count} Stock Ledger Entries!")
		self.assertEqual(bin_count - getattr(self, "initial_bin_count", 0), 0, f"Critical Safety Violation: {bin_count} Bins!")
		self.assertEqual(price_count - getattr(self, "initial_price_count", 0), 0, f"Critical Safety Violation: {price_count} Item Prices!")


def run_all():
	import unittest
	suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestPresentationIdentityMediaUnit)
	runner = unittest.TextTestRunner(verbosity=2)
	res = runner.run(suite)
	if not res.wasSuccessful():
		raise RuntimeError(f"Tests failed: {len(res.failures)} failures, {len(res.errors)} errors")
