import hashlib
import frappe
from frappe.model.document import Document

class ItemChannelPresentation(Document):
    def validate(self):
        self.validate_active_presentation_key()
        self.validate_cover_image_count()
        self.validate_channel_categories()

    def validate_active_presentation_key(self):
        if not self.item or not self.sales_channel:
            return
        raw = f'{self.item.strip()}::{self.sales_channel.strip()}'
        self.active_presentation_key = hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def validate_cover_image_count(self):
        cover_count = 0
        for m in (self.media_items or []):
            if m.is_cover:
                cover_count += 1
        if cover_count > 1:
            frappe.throw(frappe._('An Item Channel Presentation can have at most one cover image.'))

    def validate_channel_categories(self):
        primary_count = 0
        seen_cats = set()
        for cat in (self.channel_categories or []):
            if cat.channel_category in seen_cats:
                frappe.throw(frappe._('Channel category {0} cannot be added multiple times.').format(cat.channel_category))
            seen_cats.add(cat.channel_category)
            if getattr(cat, 'is_default', 0) or getattr(cat, 'is_primary', 0):
                primary_count += 1
        if primary_count > 1:
            frappe.throw(frappe._('Only one channel category can be designated as primary.'))

    def get_effective_item_name(self, language=None):
        if language:
            for row in (self.localized_content or []):
                if row.language == language and row.channel_item_name:
                    return row.channel_item_name
        if self.channel_item_name:
            return self.channel_item_name
        return frappe.db.get_value('Item', self.item, 'item_name') or ''

    def get_effective_description(self, language=None):
        if language:
            for row in (self.localized_content or []):
                if row.language == language and row.channel_description:
                    return row.channel_description
        if self.channel_description:
            return self.channel_description
        return frappe.db.get_value('Item', self.item, 'description') or ''
