# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import frappe
from frappe.model.document import Document


def compute_migration_row_key(batch: str, source_record_id: str) -> str:
	"""Computes deterministic unique key for a migration row in a batch."""
	t = [str(batch).strip(), str(source_record_id).strip()]
	return hashlib.sha256(json.dumps(t, separators=(",", ":")).encode("utf-8")).hexdigest()


class InventoryMigrationRow(Document):
	def before_validate(self):
		if self.batch and self.source_record_id:
			self.unique_row_key = compute_migration_row_key(self.batch, self.source_record_id)
