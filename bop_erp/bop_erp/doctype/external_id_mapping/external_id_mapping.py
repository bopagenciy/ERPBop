# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import frappe
from frappe import _
from frappe.model.document import Document
from bop_erp.constants import ExternalEntityType

MAX_EXTERNAL_ID_LENGTH = 1000

def compute_active_external_key(sales_channel, external_entity_type, external_id, external_variant_id=None, provider=None):
	"""
	External active unique key:
	For PRODUCT_VARIANT:
		[sales_channel, (opt) provider, external_entity_type, external_id, external_variant_id]
	For all non-variant types:
		[sales_channel, (opt) provider, external_entity_type, external_id]

	Canonical SHA-256 hash over deterministic JSON tuple.
	External IDs are strictly opaque: casing, leading/trailing whitespace, and separators are preserved.
	If provider is not specified, backward-compatible tuple is preserved without breaking existing keys.
	"""
	clean_prov = str(provider).strip().upper() if provider and str(provider).strip() else None

	if str(external_entity_type) == ExternalEntityType.PRODUCT_VARIANT:
		if clean_prov:
			identity_tuple = [
				str(sales_channel),
				clean_prov,
				str(external_entity_type),
				str(external_id),
				str(external_variant_id) if external_variant_id is not None else None,
			]
		else:
			identity_tuple = [
				str(sales_channel),
				str(external_entity_type),
				str(external_id),
				str(external_variant_id) if external_variant_id is not None else None,
			]
	else:
		if clean_prov:
			identity_tuple = [
				str(sales_channel),
				clean_prov,
				str(external_entity_type),
				str(external_id),
			]
		else:
			identity_tuple = [
				str(sales_channel),
				str(external_entity_type),
				str(external_id),
			]
	canonical_json = json.dumps(identity_tuple, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

def compute_active_erp_key(sales_channel, external_entity_type, erp_doctype, erp_document):
	"""
	ERP inverse active unique key MUST represent the ERP object identity:
	[sales_channel, external_entity_type, erp_doctype, erp_document]

	Canonical SHA-256 hash over deterministic JSON tuple.
	Does NOT include external_variant_id, preventing multiple external variants from
	mapping simultaneously to the same ERP document inside the same Sales Channel.
	"""
	identity_tuple = [
		str(sales_channel),
		str(external_entity_type),
		str(erp_doctype),
		str(erp_document),
	]
	canonical_json = json.dumps(identity_tuple, ensure_ascii=False, separators=(",", ":"))
	return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

class ExternalIDMapping(Document):
	def validate(self):
		self.clean_fields()
		self.validate_field_lengths()
		self.validate_variant_semantics()
		self.set_uniqueness_keys()
		self.validate_linked_document()
		self.validate_external_uniqueness()
		self.validate_erp_document_uniqueness()

	def clean_fields(self):
		# External IDs are opaque: DO NOT strip, lowercase, uppercase, or collapse whitespace
		if self.external_id is not None:
			self.external_id = str(self.external_id)
		if self.external_variant_id is not None:
			self.external_variant_id = str(self.external_variant_id)
		if getattr(self, "provider", None):
			self.provider = str(self.provider).strip().upper()

	def validate_field_lengths(self):
		if self.external_id and len(self.external_id) > MAX_EXTERNAL_ID_LENGTH:
			frappe.throw(
				_("External ID exceeds maximum allowed length of {0} characters (received {1}).").format(
					MAX_EXTERNAL_ID_LENGTH, len(self.external_id)
				)
			)
		if self.external_variant_id and len(self.external_variant_id) > MAX_EXTERNAL_ID_LENGTH:
			frappe.throw(
				_("External Variant ID exceeds maximum allowed length of {0} characters (received {1}).").format(
					MAX_EXTERNAL_ID_LENGTH, len(self.external_variant_id)
				)
			)

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
				provider=getattr(self, "provider", None),
			)
			self.active_erp_key = compute_active_erp_key(
				self.sales_channel,
				self.external_entity_type,
				self.erp_doctype,
				self.erp_document,
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
			["name", "external_id", "external_variant_id"],
			as_dict=True,
		)
		if existing and existing.name != self.name:
			var_desc = f" (Variant: '{existing.external_variant_id}')" if existing.external_variant_id else ""
			frappe.throw(
				_(
					"An active mapping already exists for {0} '{1}' in Channel '{2}' "
					"(External ID: '{3}'{4}). Cannot create duplicate active mapping."
				).format(
					self.erp_doctype,
					self.erp_document,
					self.sales_channel,
					existing.external_id,
					var_desc,
				)
			)
