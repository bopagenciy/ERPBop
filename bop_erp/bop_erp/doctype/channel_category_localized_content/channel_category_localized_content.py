# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from frappe.model.document import Document

class ChannelCategoryLocalizedContent(Document):
	def before_validate(self):
		if self.language:
			self.language = str(self.language).strip().lower()

