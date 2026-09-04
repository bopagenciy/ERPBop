# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import frappe
from frappe import _
from frappe.model.document import Document


def compute_channel_category_key(sales_channel: str, category_key: str) -> str:
	"""
	Computes a deterministic hash key for a Channel Category node.
	Uses canonical JSON tuple serialization of [sales_channel, category_key].
	Node identity does NOT change when translated title/slug changes.
	"""
	t = [str(sales_channel).strip(), str(category_key).strip().lower()]
	return hashlib.sha256(json.dumps(t, separators=(",", ":")).encode("utf-8")).hexdigest()


class ChannelCategory(Document):
	def before_validate(self):
		self.validate_stable_identity()

	def autoname(self):
		self.validate_stable_identity()
		slug = self.category_slug or (frappe.scrub(self.category_name) if self.category_name else None) or self.category_key
		self.category_slug = slug
		base_name = f"CC-{self.sales_channel}-{slug}"
		name = base_name
		idx = 1
		while frappe.db.exists("Channel Category", name):
			name = f"{base_name}-{idx}"
			idx += 1
		self.name = name

	def validate(self):
		self.validate_stable_identity()
		self.validate_unique_languages()

	def validate_stable_identity(self):
		if not self.category_key:
			if self.category_slug and not self.category_slug.startswith("ps-"):
				self.category_key = self.category_slug
			else:
				self.category_key = f"cat_{frappe.generate_hash(length=12)}"
		self.category_key = str(self.category_key).strip().lower()

		if not self.category_slug:
			self.category_slug = frappe.scrub(self.category_name) if self.category_name else self.category_key
		self.category_slug = str(self.category_slug).strip().lower()

		self.unique_channel_slug = compute_channel_category_key(self.sales_channel, self.category_key)

	def validate_unique_languages(self):
		seen_languages = set()
		for row in (self.localized_content or []):
			lang = (row.language or "").strip().lower()
			row.language = lang
			if lang in seen_languages:
				frappe.throw(_("Duplicate language code '{0}' found in localized content.").format(row.language))
			seen_languages.add(lang)

	def get_effective_category_name(self, language: str = None) -> str:
		"""
		Resolves effective storefront category name using 4-tier fallback:
		1. requested language in localized_content
		2. channel default language
		3. English ('en')
		4. master category_name
		"""
		target_lang = (language or "").strip().lower()
		channel_lang = frappe.db.get_value("Sales Channel", self.sales_channel, "language")
		channel_lang = (channel_lang or "en").strip().lower()

		# 1. Requested language
		if target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == target_lang and row.category_name:
					return row.category_name

		# 2. Channel default language
		if channel_lang and channel_lang != target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == channel_lang and row.category_name:
					return row.category_name

		# 3. English ('en')
		if target_lang != "en" and channel_lang != "en":
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == "en" and row.category_name:
					return row.category_name

		# 4. Master node category_name
		return self.category_name or ""

	def get_effective_slug(self, language: str = None) -> str:
		"""
		Resolves localized storefront slug using 4-tier fallback.
		"""
		target_lang = (language or "").strip().lower()
		channel_lang = frappe.db.get_value("Sales Channel", self.sales_channel, "language")
		channel_lang = (channel_lang or "en").strip().lower()

		if target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == target_lang and row.category_slug:
					return row.category_slug

		if channel_lang and channel_lang != target_lang:
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == channel_lang and row.category_slug:
					return row.category_slug

		if target_lang != "en" and channel_lang != "en":
			for row in (self.localized_content or []):
				if (row.language or "").strip().lower() == "en" and row.category_slug:
					return row.category_slug

		return self.category_slug or self.category_key or ""
