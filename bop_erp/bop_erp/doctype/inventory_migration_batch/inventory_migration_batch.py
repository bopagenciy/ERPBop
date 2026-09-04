# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class InventoryMigrationBatch(Document):
	def validate(self):
		if self.is_new():
			return
		old = self.get_doc_before_save()
		if old and old.status == "APPLIED":
			if self.status != "APPLIED":
				frappe.throw(_("Applied inventory migration batch cannot change status."))
			if self.input_hash != old.input_hash:
				frappe.throw(_("Cannot modify input_hash of an applied migration batch."))
			if self.company != old.company:
				frappe.throw(_("Cannot modify company of an applied migration batch."))
