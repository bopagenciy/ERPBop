# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import unittest
import frappe
from frappe.utils.file_manager import save_file

from bop_erp.bop_erp.doctype.channel_category.channel_category import compute_channel_category_key
from bop_erp.bop_erp.doctype.item_media_asset.item_media_asset import compute_item_media_key


class TestItemChannelPresentationUnit(unittest.TestCase):
	"""
	Unit tests for Phase 1F:
	1. Master 3-tier Item Group hierarchy (Fasteners -> Bolts -> Hex Bolts).
	2. Independent Channel Category hierarchy decoupled from ERP Item Groups.
	3. Item Channel Presentation uniqueness key enforcement (Item + Channel).
	4. Presentation fallback text (item_name & description fall back to master Item).
	5. Multilingual localized marketing content override (ES override, EN fallback).
	6. Item Media Asset deduplication via SHA-256 content_hash.
	7. Presentation cover image constraint (at most one cover image).
	"""

	def setUp(self):
		self.sales_channel = "TID"
		self.other_channel = "BAMAL"
		self._created_docs = []

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

	def test_01_three_tier_master_item_group_hierarchy(self):
		"""Validates Category -> Subcategory -> Sub-subcategory in native ERPNext Item Groups."""
		tier1_name = "Phase1F-Fasteners"
		tier2_name = "Phase1F-Bolts"
		tier3_name = "Phase1F-Hex-Bolts"

		# Tier 1 (Root child of 'All Item Groups')
		if not frappe.db.exists("Item Group", tier1_name):
			t1 = frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": tier1_name,
				"parent_item_group": "All Item Groups",
				"is_group": 1,
			}).insert(ignore_permissions=True)
			self._track("Item Group", t1.name)

		# Tier 2 (Child of Tier 1)
		if not frappe.db.exists("Item Group", tier2_name):
			t2 = frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": tier2_name,
				"parent_item_group": tier1_name,
				"is_group": 1,
			}).insert(ignore_permissions=True)
			self._track("Item Group", t2.name)

		# Tier 3 (Child of Tier 2)
		if not frappe.db.exists("Item Group", tier3_name):
			t3 = frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": tier3_name,
				"parent_item_group": tier2_name,
				"is_group": 0,
			}).insert(ignore_permissions=True)
			self._track("Item Group", t3.name)

		# Verify hierarchy in DB
		doc3 = frappe.get_doc("Item Group", tier3_name)
		self.assertEqual(doc3.parent_item_group, tier2_name)
		doc2 = frappe.get_doc("Item Group", tier2_name)
		self.assertEqual(doc2.parent_item_group, tier1_name)

	def test_02_independent_channel_category_hierarchy(self):
		"""Validates that Channel Category provides a distinct storefront navigation hierarchy."""
		slug_root = "clearance-deals"
		slug_child = "clearance-hardware"

		key_root = compute_channel_category_key(self.sales_channel, slug_root)
		key_child = compute_channel_category_key(self.sales_channel, slug_child)

		# Root storefront category
		cat_root = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Clearance Deals",
			"category_slug": slug_root,
			"active": 1,
		}).insert(ignore_permissions=True)
		self._track("Channel Category", cat_root.name)
		self.assertEqual(cat_root.unique_channel_slug, key_root)

		# Child storefront category
		cat_child = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Clearance Hardware",
			"category_slug": slug_child,
			"parent_channel_category": cat_root.name,
			"active": 1,
		}).insert(ignore_permissions=True)
		self._track("Channel Category", cat_child.name)
		self.assertEqual(cat_child.unique_channel_slug, key_child)
		self.assertEqual(cat_child.parent_channel_category, cat_root.name)

		# Duplicate category slug in same channel must raise DuplicateEntryError
		duplicate_cat = frappe.get_doc({
			"doctype": "Channel Category",
			"sales_channel": self.sales_channel,
			"category_name": "Duplicate Clearance Deals",
			"category_slug": slug_root,
			"active": 1,
		})
		with self.assertRaises(frappe.DuplicateEntryError):
			duplicate_cat.insert(ignore_permissions=True)

	def test_03_item_channel_presentation_uniqueness_and_decoupling(self):
		"""Validates compound uniqueness of [item, sales_channel] and publication status decoupling."""
		item_code = "ITEM-PHASE1F-TEST-01"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Master Item Test 01",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"disabled": 0,
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		# Create presentation for TID
		pres_tid = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "PUBLISHED",
			"channel_item_name": "TID Industrial Fastener",
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres_tid.name)

		# Presentation for BAMAL with status DISABLED (independent from master item and TID)
		pres_bamal = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.other_channel,
			"publication_status": "DISABLED",
			"channel_item_name": "Bamal OEM Fastener",
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres_bamal.name)

		# Master item remains active (disabled=0)
		self.assertEqual(frappe.db.get_value("Item", item_code, "disabled"), 0)
		self.assertEqual(pres_tid.publication_status, "PUBLISHED")
		self.assertEqual(pres_bamal.publication_status, "DISABLED")

		# Second presentation for same Item and TID must fail on active_presentation_key uniqueness
		duplicate_pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "DRAFT",
		})
		with self.assertRaises((frappe.DuplicateEntryError, frappe.UniqueValidationError)):
			duplicate_pres.insert(ignore_permissions=True)

	def test_04_presentation_text_fallback_and_localization(self):
		"""Validates that empty channel titles/descriptions fall back to master Item values."""
		item_code = "ITEM-PHASE1F-FALLBACK-02"
		master_name = "Master Precision Gear"
		master_desc = "Standard commercial grade steel gear."

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

		# Presentation with empty channel_item_name and description
		pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "READY",
			"channel_item_name": "",
			"channel_description": "",
			"localized_content": [
				{
					"language": "es",
					"channel_item_name": "Engranaje de Precisión Maestro",
					"channel_description": "Engranaje de acero de grado comercial estándar.",
				}
			],
		}).insert(ignore_permissions=True)
		self._track("Item Channel Presentation", pres.name)

		# Fallback to master Item when no language or English specified without channel override
		self.assertEqual(pres.get_effective_item_name(), master_name)
		self.assertEqual(pres.get_effective_description(), master_desc)
		self.assertEqual(pres.get_effective_item_name(language="en"), master_name)

		# Spanish localized marketing content override
		self.assertEqual(pres.get_effective_item_name(language="es"), "Engranaje de Precisión Maestro")
		self.assertEqual(pres.get_effective_description(language="es"), "Engranaje de acero de grado comercial estándar.")

	def test_05_media_asset_deduplication_via_content_hash(self):
		"""Validates SHA-256 deduplication of physical image files across channels."""
		item_code = "ITEM-PHASE1F-MEDIA-03"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Master Cable Assembly",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		raw_bytes = b"IMAGE_CONTENT_SAMPLE_FOR_TEST_DEDUPLICATION_HASH_12345"
		sha256_hash = hashlib.sha256(raw_bytes).hexdigest()

		file_doc = save_file("cable_assembly.png", raw_bytes, None, None, is_private=0)
		self._track("File", file_doc.name)

		# Create shared master media asset
		asset1 = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file_doc.name,
			"media_type": "IMAGE",
			"title": "Cable Assembly Isometric",
			"content_hash": sha256_hash,
		}).insert(ignore_permissions=True)
		self._track("Item Media Asset", asset1.name)

		expected_key = compute_item_media_key(item_code, sha256_hash)
		self.assertEqual(asset1.asset_uniqueness_key, expected_key)

		# Attempting to insert duplicate asset for same Item and content_hash must raise DuplicateEntryError
		duplicate_asset = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file_doc.name,
			"media_type": "IMAGE",
			"title": "Duplicate Cable Assembly Image",
			"content_hash": sha256_hash,
		})
		with self.assertRaises((frappe.DuplicateEntryError, frappe.UniqueValidationError)):
			duplicate_asset.insert(ignore_permissions=True)

	def test_06_single_cover_image_validation(self):
		"""Validates that an Item Channel Presentation cannot specify more than one cover image."""
		item_code = "ITEM-PHASE1F-COVER-04"
		if not frappe.db.exists("Item", item_code):
			item = frappe.get_doc({
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Master Valve Test",
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
			}).insert(ignore_permissions=True)
			self._track("Item", item.name)

		file1 = save_file("valve1.png", b"valve1_bytes", None, None, is_private=0)
		file2 = save_file("valve2.png", b"valve2_bytes", None, None, is_private=0)
		self._track("File", file1.name)
		self._track("File", file2.name)

		asset1 = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file1.name,
			"media_type": "IMAGE",
			"title": "Valve 1",
			"content_hash": "hash_valve_1",
		}).insert(ignore_permissions=True)
		self._track("Item Media Asset", asset1.name)

		asset2 = frappe.get_doc({
			"doctype": "Item Media Asset",
			"item": item_code,
			"file": file2.name,
			"media_type": "IMAGE",
			"title": "Valve 2",
			"content_hash": "hash_valve_2",
		}).insert(ignore_permissions=True)
		self._track("Item Media Asset", asset2.name)

		# Attempt presentation with TWO cover images
		invalid_pres = frappe.get_doc({
			"doctype": "Item Channel Presentation",
			"item": item_code,
			"sales_channel": self.sales_channel,
			"publication_status": "READY",
			"media_items": [
				{"media_asset": asset1.name, "is_cover": 1},
				{"media_asset": asset2.name, "is_cover": 1},
			],
		})
		with self.assertRaises(frappe.ValidationError):
			invalid_pres.insert(ignore_permissions=True)
