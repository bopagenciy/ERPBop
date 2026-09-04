# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import frappe
from frappe.model.document import Document


def compute_item_media_key(item: str, deduplication_ref: str) -> str:
	t = [str(item).strip(), str(deduplication_ref).strip()]
	return hashlib.sha256(json.dumps(t, separators=(",", ":")).encode("utf-8")).hexdigest()


class ItemMediaAsset(Document):
	def validate(self):
		ref = self.content_hash or self.file
		self.asset_uniqueness_key = compute_item_media_key(self.item, ref)
