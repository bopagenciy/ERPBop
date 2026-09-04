# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import frappe
from frappe import _
from frappe.model.document import Document
from bop_erp.constants import ExternalEntityType

def compute_active_external_key(sales_channel, external_entity_type, external_id, external_variant_id=None):
	"""
	Canonical SHA-256 hash over deterministic JSON tuple:
	[sales_channel, external_entity_type, external_id, variant_id_if_applicable]
	Preserves exact case-sensitivity and eliminates delimiter ambiguity.
	"""
	variant_val = (
		str(external_variant_id).strip()
		if (external_entity_type == ExternalEntityType.PRODUCT_VARIANT and external_variant_id)
		else None
	)
	identity_tuple = [
		str(sales_channel).strip(),
		str(external_entity_type).strip(),
		str(external_id).strip(),
		variant_val,
	]
	canonical_json = json.dumps(identity_tuple, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

def compute_active_erp_key(sales_channel, external_entity_type, erp_doctype, erp_document, external_variant_id=None):
	"""
	Canonical SHA-256 hash over deterministic JSON tuple:
	[sales_channel, external_entity_type, erp_doctype, erp_document, variant_id_if_applicable]
	Preserves exact case-sensitivity and eliminates delimiter ambiguity.
	"""
	variant_val = (
		str(external_variant_id).strip()
		if (external_entity_type == ExternalEntityType.PRODUCT_VARIANT and external_variant_id)
		else None
	)
	identity_tuple = [
		str(sales_channel).strip(),
		str(external_entity_type).strip(),
		str(erp_doctype).strip(),
		str(erp_document).strip(),
		variant_val,
	]
	canonical_json = json.dumps(identity_tuple, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

class ExternalIDMapping(Document):
	def validate(self):
		self.clean_fields()
		self.validate_variant_semantics()
		self.set_uniqueness_keys()
		self.validate_linked_document()
		self.validate_external_uniqueness()
		self.validate_erp_document_uniqueness()

	def clean_fields(self):
		if self.external_id:
			# Whitespace stripping only; preserve exact case sensitivity
			self.external_id = str(self.external_id).strip()
		if self.external_variant_id:
			self.external_variant_id = str(self.external_variant_id).strip()

	def validate_variant_semantics(self):
		if self.external_entity_type == ExternalEntityType.PRODUCT_VARIANT:
			if not self.external_variant_id:
				frappe.throw(
					_("External Variant ID is required when External Entity Type is 'PRODUCT_VARIANT'.")
				)
		else:
			if self.external_variant_id:
				frappe.throw(
					_(
						"External Variant ID is only permitted for 'PRODUCT_VARIANT'. "
						"Entity type '{0}' must not have an External Variant ID."
					).format(self.external_entity_type)
				)

	def set_uniqueness_keys(self):
		if self.active:
			self.active_external_key = compute_active_external_key(
				self.sales_channel,
				self.external_entity_type,
				self.external_id,
				self.external_variant_id,
			)
			self.active_erp_key = compute_active_erp_key(
				self.sales_channel,
				self.external_entity_type,
				self.erp_doctype,
				self.erp_document,
				self.external_variant_id,
			)
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
			var_desc = f" (Variant: '{self.external_variant_id}')" if self.external_variant_id else ""
			frappe.throw(
				_(
					"An active mapping already exists for Channel '{0}', Type '{1}', and External ID '{2}'{3} "
					"(mapped to {4} '{5}')."
				).format(
					self.sales_channel,
					self.external_entity_type,
					self.external_id,
					var_desc,
					existing.erp_doctype,
					existing.erp_document,
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
			var_desc = f" (Variant: '{self.external_variant_id}')" if self.external_variant_id else ""
			frappe.throw(
				_(
					"An active mapping already exists for {0} '{1}' in Channel '{2}'{3} "
					"(External ID: '{4}'). Cannot create duplicate active mapping."
				).format(
					self.erp_doctype,
					self.erp_document,
					self.sales_channel,
					var_desc,
					existing.external_id,
				)
			)
