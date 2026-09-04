# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

class ExternalIDMapping(Document):
	def validate(self):
		self.clean_fields()
		self.set_uniqueness_keys()
		self.validate_linked_document()
		self.validate_external_uniqueness()
		self.validate_erp_document_uniqueness()

	def clean_fields(self):
		if self.external_id:
			self.external_id = str(self.external_id).strip()
		if self.external_variant_id:
			self.external_variant_id = str(self.external_variant_id).strip()

	def set_uniqueness_keys(self):
		if self.active:
			var_suffix = f"::{self.external_variant_id}" if self.external_variant_id else ""
			self.active_external_key = f"{self.sales_channel}::{self.external_entity_type}::{self.external_id}{var_suffix}"
			self.active_erp_key = f"{self.sales_channel}::{self.external_entity_type}::{self.erp_doctype}::{self.erp_document}{var_suffix}"
		else:
			self.active_external_key = None
			self.active_erp_key = None

	def validate_linked_document(self):
		if not frappe.db.exists(self.erp_doctype, self.erp_document):
			frappe.throw(
				_("Linked ERP Document {0} '{1}' does not exist in the database.").format(
					self.erp_doctype, self.erp_document
				)
			)

	def validate_external_uniqueness(self):
		if not self.active or not self.active_external_key:
			return

		existing = frappe.db.get_value(
			"External ID Mapping",
			{"active_external_key": self.active_external_key},
			["name", "erp_doctype", "erp_document"],
			as_dict=True,
		)
		if existing and existing.name != self.name:
			frappe.throw(
				_("An active mapping already exists for Channel '{0}', Type '{1}', and External ID '{2}' (mapped to {3} '{4}').").format(
					self.sales_channel, self.external_entity_type, self.external_id, existing.erp_doctype, existing.erp_document
				)
			)

	def validate_erp_document_uniqueness(self):
		if not self.active or not self.active_erp_key:
			return

		existing = frappe.db.get_value(
			"External ID Mapping",
			{"active_erp_key": self.active_erp_key},
			["name", "external_id"],
			as_dict=True,
		)
		if existing and existing.name != self.name:
			frappe.throw(
				_("An active mapping already exists for {0} '{1}' in Channel '{2}' (External ID: '{3}'). Cannot create duplicate active mapping.").format(
					self.erp_doctype, self.erp_document, self.sales_channel, existing.external_id
				)
			)
