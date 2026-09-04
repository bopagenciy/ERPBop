# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import frappe
from frappe import _
from frappe.model.document import Document


def compute_channel_category_key(sales_channel: str, category_slug: str) -> str:
	t = [str(sales_channel).strip(), str(category_slug).strip().lower()]
	return hashlib.sha256(json.dumps(t, separators=(",", ":")).encode("utf-8")).hexdigest()


class ChannelCategory(Document):
	def validate(self):
		if not self.category_slug:
			self.category_slug = frappe.scrub(self.category_name)
		self.category_slug = self.category_slug.strip().lower()
		self.unique_channel_slug = compute_channel_category_key(self.sales_channel, self.category_slug)
