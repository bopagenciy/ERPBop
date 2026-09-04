# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import re
import frappe
from frappe import _
from frappe.model.document import Document

class SalesChannel(Document):
	def validate(self):
		self.validate_channel_id_format()
		self.validate_immutability()

	def validate_channel_id_format(self):
		if not self.channel_id:
			return
		cleaned = self.channel_id.strip().upper()
		if not re.match(r"^[A-Z0-9_-]+$", cleaned):
			frappe.throw(
				_("Channel ID '{0}' is invalid. Only uppercase letters, numbers, hyphens, and underscores are allowed.").format(self.channel_id)
			)
		self.channel_id = cleaned

	def validate_immutability(self):
		if self.is_new():
			return

		old_doc = self.get_doc_before_save()
		if not old_doc:
			return

		if old_doc.channel_id != self.channel_id:
			if self.has_transaction_references(old_doc.channel_id):
				frappe.throw(
					_("Channel ID cannot be modified from '{0}' to '{1}' because it is already referenced in transactions or external mappings. You may update Channel Name or disable the channel instead.").format(
						old_doc.channel_id, self.channel_id
					)
				)

	def on_trash(self):
		if self.has_transaction_references(self.name):
			frappe.throw(
				_("Sales Channel '{0}' cannot be deleted because it is referenced in transactions or external mappings. You may uncheck 'Active' to disable it instead.").format(self.name)
			)

	def has_transaction_references(self, channel_id):
		check_doctypes = [
			("Sales Order", "sales_channel"),
			("Pick List", "sales_channel"),
			("Delivery Note", "sales_channel"),
			("Sales Invoice", "sales_channel"),
			("Payment Entry", "sales_channel"),
			("External ID Mapping", "sales_channel"),
		]
		for doctype, field in check_doctypes:
			if frappe.db.exists("DocType", doctype) and frappe.db.has_column(doctype, field):
				if frappe.db.count(doctype, {field: channel_id}) > 0:
					return True
		return False
