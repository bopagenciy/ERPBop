import frappe
from frappe.model.document import Document


class ItemChannelLocalizedContent(Document):
	def before_validate(self):
		if self.language:
			self.language = str(self.language).strip().lower()

