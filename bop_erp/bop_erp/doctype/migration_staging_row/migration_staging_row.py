# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class MigrationStagingRow(Document):
	def validate(self):
		self.validate_immutability()

	def validate_immutability(self):
		if not self.is_new():
			old_doc = frappe.db.get_value(
				"Migration Staging Row",
				self.name,
				["source_payload_hash", "source_payload_json", "staging_identity_key"],
				as_dict=True,
			)
			if old_doc:
				if old_doc.source_payload_hash != self.source_payload_hash:
					frappe.throw(
						_("Source payload hash of Migration Staging Row '{0}' is immutable and cannot be changed.").format(
							self.name
						),
						frappe.ValidationError,
					)
				if old_doc.source_payload_json != self.source_payload_json:
					frappe.throw(
						_("Source payload JSON of Migration Staging Row '{0}' is immutable and cannot be changed.").format(
							self.name
						),
						frappe.ValidationError,
					)
				if old_doc.staging_identity_key != self.staging_identity_key:
					frappe.throw(
						_("Staging identity key of Migration Staging Row '{0}' is immutable and cannot be changed.").format(
							self.name
						),
						frappe.ValidationError,
					)
