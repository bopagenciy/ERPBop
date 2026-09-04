import frappe
from frappe.model.document import Document


class ItemChannelMedia(Document):
	def get_effective_alt_text(self, language: str = None) -> str:
		"""Resolves localized alt text using 5-tier fallback cascade via parent presentation."""
		if hasattr(self, "parent") and self.parent:
			try:
				parent_doc = self.get_parent_doc() if hasattr(self, "get_parent_doc") else frappe.get_doc("Item Channel Presentation", self.parent)
				if hasattr(parent_doc, "get_effective_media_alt_text"):
					return parent_doc.get_effective_media_alt_text(self.media_asset, language=language)
			except Exception:
				pass

		target_lang = (language or "en").strip().lower()
		if target_lang == "es" and getattr(self, "channel_alt_text_es", None):
			return self.channel_alt_text_es
		if getattr(self, "channel_alt_text", None):
			return self.channel_alt_text
		return ""

