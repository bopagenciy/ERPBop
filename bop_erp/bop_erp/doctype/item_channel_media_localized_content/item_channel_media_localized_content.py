# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ItemChannelMediaLocalizedContent(Document):
	def before_validate(self):
		if self.language:
			self.language = str(self.language).strip().lower()

	def validate(self):
		if self.language and not frappe.db.exists("Language", self.language):
			frappe.throw(_("Language '{0}' does not exist in Frappe.").format(self.language))
