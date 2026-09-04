# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import frappe
from frappe import _
from frappe.model.document import Document


def compute_item_presentation_key(item: str, sales_channel: str) -> str:
	"""
	Computes a canonical SHA-256 hash for an [item, sales_channel] tuple.
	Uses canonical JSON array serialization for consistency and delimiter safety.
	"""
	t = [str(item).strip(), str(sales_channel).strip()]
	return hashlib.sha256(json.dumps(t, separators=(",", ":")).encode("utf-8")).hexdigest()


# Alias for compatibility with exact naming conventions
compute_presentation_uniqueness_key = compute_item_presentation_key


class ItemChannelPresentation(Document):
	def before_validate(self):
		for row in (self.localized_content or []):
			if row.language:
				row.language = str(row.language).strip().lower()

	def validate(self):
		self.validate_active_presentation_key()
		self.validate_unique_languages()
		self.validate_cover_image_count()
		self.validate_channel_categories()

	def validate_active_presentation_key(self):
		if not self.item or not self.sales_channel:
			return
		self.active_presentation_key = compute_item_presentation_key(self.item, self.sales_channel)

	def validate_unique_languages(self):
		seen_languages = set()
		for row in (self.localized_content or []):
			lang = (row.language or "").strip().lower()
			row.language = lang
			if lang in seen_languages:
				frappe.throw(_("Duplicate language code '{0}' found in localized content.").format(row.language))
			seen_languages.add(lang)

	def validate_cover_image_count(self):
		cover_count = 0
		for m in (self.media_items or []):
			if m.is_cover:
				cover_count += 1
		if cover_count > 1:
			frappe.throw(_("An Item Channel Presentation can have at most one cover image."))

	def validate_channel_categories(self):
		primary_count = 0
		seen_cats = set()
		for cat in (self.channel_categories or []):
			if cat.channel_category in seen_cats:
				frappe.throw(_("Channel category {0} cannot be added multiple times.").format(cat.channel_category))
			seen_cats.add(cat.channel_category)
			if getattr(cat, "is_default", 0) or getattr(cat, "is_primary", 0):
				primary_count += 1
		if primary_count > 1:
			frappe.throw(_("Only one channel category can be designated as primary."))

	def get_localized_content(self, language: str) -> dict:
		"""Returns the localized content row dict for a specific language code, if present."""
		target = (language or "").strip().lower()
		for row in (self.localized_content or []):
			if (row.language or "").strip().lower() == target:
				return row.as_dict() if hasattr(row, "as_dict") else dict(row)
		return {}

	def get_effective_item_name(self, language: str = None) -> str:
		"""
		Resolves effective item name using 5-tier fallback cascade:
		1. requested locale content
		2. channel default language content
		3. English ('en') content
		4. channel generic override (channel_item_name)
		5. master Item.item_name
		"""
		target_lang = (language or "").strip().lower()
		channel_lang = frappe.db.get_value("Sales Channel", self.sales_channel, "language")
		channel_lang = (channel_lang or "en").strip().lower()

		# Tier 1: Requested language
		if target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == target_lang and row.channel_item_name:
					return row.channel_item_name

		# Tier 2: Channel default language
		if channel_lang and channel_lang != target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == channel_lang and row.channel_item_name:
					return row.channel_item_name

		# Tier 3: English ('en')
		if target_lang != "en" and channel_lang != "en":
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == "en" and row.channel_item_name:
					return row.channel_item_name

		# Tier 4: Channel generic override
		if self.channel_item_name:
			return self.channel_item_name

		# Tier 5: Master operational Item name
		return frappe.db.get_value("Item", self.item, "item_name") or ""

	def get_effective_description(self, language: str = None) -> str:
		"""
		Resolves effective description using 5-tier fallback cascade:
		1. requested locale content
		2. channel default language content
		3. English ('en') content
		4. channel generic override (channel_description)
		5. master Item.description
		"""
		target_lang = (language or "").strip().lower()
		channel_lang = frappe.db.get_value("Sales Channel", self.sales_channel, "language")
		channel_lang = (channel_lang or "en").strip().lower()

		# Tier 1: Requested language
		if target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == target_lang and row.channel_description:
					return row.channel_description

		# Tier 2: Channel default language
		if channel_lang and channel_lang != target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == channel_lang and row.channel_description:
					return row.channel_description

		# Tier 3: English ('en')
		if target_lang != "en" and channel_lang != "en":
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == "en" and row.channel_description:
					return row.channel_description

		# Tier 4: Channel generic override
		if self.channel_description:
			return self.channel_description

		# Tier 5: Master operational Item description
		return frappe.db.get_value("Item", self.item, "description") or ""

	def get_effective_slug(self, language: str = None) -> str:
		"""
		Resolves effective SEO slug using 5-tier fallback cascade.
		"""
		target_lang = (language or "").strip().lower()
		channel_lang = frappe.db.get_value("Sales Channel", self.sales_channel, "language")
		channel_lang = (channel_lang or "en").strip().lower()

		if target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == target_lang and getattr(row, "channel_slug", None):
					return row.channel_slug

		if channel_lang and channel_lang != target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == channel_lang and getattr(row, "channel_slug", None):
					return row.channel_slug

		if target_lang != "en" and channel_lang != "en":
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == "en" and getattr(row, "channel_slug", None):
					return row.channel_slug

		if self.channel_slug:
			return self.channel_slug

		return frappe.scrub(self.get_effective_item_name(language))
