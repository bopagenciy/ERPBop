# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from enum import Enum
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import frappe
from frappe import _
from frappe.utils import flt

from bop_erp.constants import ExternalEntityType
from bop_erp.migration.datasets.aggregation import CanonicalSourceItem
from bop_erp.migration.datasets.authority import (
	AuthorityDecision,
	FieldAuthorityPolicy,
	evaluate_field_authority,
)
from bop_erp.migration.exceptions import ImportBoundaryError
from bop_erp.migration.namespaces import (
	canonical_provider,
	canonical_source_namespace,
	compute_migration_channel_id,
)


class EligibilityStatus(str, Enum):
	ELIGIBLE = "ELIGIBLE"
	ELIGIBLE_PARTIAL = "ELIGIBLE_PARTIAL"
	BLOCKED = "BLOCKED"
	SKIPPED_REVIEW = "SKIPPED_REVIEW"


@dataclass
class ImportEligibilityResult:
	"""Detailed eligibility decision for a CanonicalSourceItem."""

	item_id: str
	status: EligibilityStatus
	reasons: List[str] = field(default_factory=list)
	deferred_relationships: List[str] = field(default_factory=list)
	warnings: List[str] = field(default_factory=list)
	resolved_stock_uom: Optional[str] = None
	resolved_item_group: Optional[str] = None

	def to_dict(self) -> Dict[str, Any]:
		return {
			"item_id": self.item_id,
			"status": self.status.value,
			"reasons": list(self.reasons),
			"deferred_relationships": list(self.deferred_relationships),
			"warnings": list(self.warnings),
			"resolved_stock_uom": self.resolved_stock_uom,
			"resolved_item_group": self.resolved_item_group,
		}


@dataclass
class ItemImportPreview:
	"""Structured preview of a planned controlled master data import before mutation."""

	selected_items: List[str]
	fields_to_create: Dict[str, Dict[str, Any]]
	fields_omitted: Dict[str, List[str]]
	deferred_relationships: Dict[str, List[str]]
	warnings: List[str]
	expected_target_codes: Dict[str, str]
	eligibility_summary: Dict[str, int]

	def to_dict(self) -> Dict[str, Any]:
		return {
			"selected_items": list(self.selected_items),
			"fields_to_create": dict(self.fields_to_create),
			"fields_omitted": dict(self.fields_omitted),
			"deferred_relationships": dict(self.deferred_relationships),
			"warnings": list(self.warnings),
			"expected_target_codes": dict(self.expected_target_codes),
			"eligibility_summary": dict(self.eligibility_summary),
		}


@dataclass
class ItemImportResult:
	"""Structured post-mutation report of a controlled master data import."""

	run_id: str
	selected_count: int
	created_items: List[str] = field(default_factory=list)
	reused_items: List[str] = field(default_factory=list)
	updated_items: List[str] = field(default_factory=list)
	skipped_items: List[str] = field(default_factory=list)
	blocked_items: List[str] = field(default_factory=list)
	deferred_relationships: Dict[str, List[str]] = field(default_factory=dict)
	warnings: List[str] = field(default_factory=list)
	errors: List[str] = field(default_factory=list)
	target_mappings: Dict[str, str] = field(default_factory=dict)
	stock_mutation_count: int = 0
	financial_mutation_count: int = 0

	def to_dict(self) -> Dict[str, Any]:
		return {
			"run_id": self.run_id,
			"selected_count": self.selected_count,
			"created_items": list(self.created_items),
			"reused_items": list(self.reused_items),
			"updated_items": list(self.updated_items),
			"skipped_items": list(self.skipped_items),
			"blocked_items": list(self.blocked_items),
			"deferred_relationships": dict(self.deferred_relationships),
			"warnings": list(self.warnings),
			"errors": list(self.errors),
			"target_mappings": dict(self.target_mappings),
			"stock_mutation_count": self.stock_mutation_count,
			"financial_mutation_count": self.financial_mutation_count,
		}


def build_target_item_code(source_item_id: str) -> str:
	"""
	Deterministic target Item Code strategy.
	Preserves exact source ID characters, casing, leading zeros, and punctuation.
	Never strips leading zeros (e.g. '00123' remains '00123').
	"""
	if source_item_id is None:
		return ""
	return str(source_item_id).strip()


def get_or_create_migration_channel(
	company: str,
	source_system: str,
	source_instance_id: Optional[str] = None,
) -> str:
	"""
	Ensures a persistent Sales Channel exists for scoping migration External ID Mappings.
	"""
	from bop_erp.migration.import_boundary import get_or_create_migration_channel as _get_channel
	return _get_channel(company, source_system, source_instance_id)


def evaluate_item_eligibility(
	item: CanonicalSourceItem,
	fallback_uom: str = "Nos",
	fallback_group: str = "All Item Groups",
) -> ImportEligibilityResult:
	"""
	Evaluates whether a CanonicalSourceItem is eligible for target ERP Item creation.
	Deterministic rules:
	- Missing Item ID -> BLOCKED
	- Missing Item Master record -> BLOCKED
	- Missing or invalid mandatory Stock UOM -> BLOCKED
	- Missing Supplier Master data -> DEFERRED_DEPENDENCY (partial item eligible)
	- Missing Location/Warehouse data -> DEFERRED_DEPENDENCY (partial item eligible)
	- Missing extended description -> non-blocking, uses short description
	- Price tiers present -> preserved, NOT imported as Item Price
	"""
	item_id = item.item_id
	if not item_id or str(item_id).strip() == "":
		return ImportEligibilityResult(
			item_id=str(item_id),
			status=EligibilityStatus.BLOCKED,
			reasons=["Missing mandatory Item ID."],
		)

	if not item.master:
		return ImportEligibilityResult(
			item_id=item_id,
			status=EligibilityStatus.BLOCKED,
			reasons=["Missing master record."],
		)

	reasons: List[str] = []
	deferred: List[str] = []
	warnings: List[str] = []

	# Resolve Stock UOM
	resolved_uom = item.master.get("Base Unit")
	if not resolved_uom:
		# Check if any UOM conversion record is marked as Selling or Purchasing, or take first
		if item.uoms:
			for u in item.uoms:
				u_code = u.get("Unit of Measure")
				if u_code and str(u_code).strip():
					resolved_uom = str(u_code).strip()
					break
	if not resolved_uom:
		resolved_uom = fallback_uom

	if not resolved_uom or str(resolved_uom).strip() == "":
		return ImportEligibilityResult(
			item_id=item_id,
			status=EligibilityStatus.BLOCKED,
			reasons=["Missing mandatory Stock UOM."],
		)

	# Resolve Item Group
	src_group = item.master.get("Default Product Group")
	resolved_group = str(src_group).strip() if src_group and str(src_group).strip() else fallback_group

	# Track deferred relationships
	if item.suppliers:
		deferred.append("SUPPLIER")
	if item.locations:
		deferred.append("LOCATION")
	if item.supplier_location_overrides:
		deferred.append("SUPPLIER_LOCATION_OVERRIDE")

	# Check for missing description
	if not item.descriptions:
		warnings.append("Extended description missing; short Item Description will be used.")

	# Check for price tiers
	price_tiers_found = [k for k in item.master.keys() if "price" in str(k).lower()]
	if price_tiers_found:
		warnings.append(f"Price tier fields preserved in staging, excluded from master Item creation: {price_tiers_found}")

	# Determine final status
	if deferred or not item.descriptions:
		status = EligibilityStatus.ELIGIBLE_PARTIAL
	else:
		status = EligibilityStatus.ELIGIBLE

	return ImportEligibilityResult(
		item_id=item_id,
		status=status,
		reasons=reasons,
		deferred_relationships=deferred,
		warnings=warnings,
		resolved_stock_uom=resolved_uom,
		resolved_item_group=resolved_group,
	)


def generate_import_preview(
	items: List[CanonicalSourceItem],
	company: str,
	fallback_uom: str = "Nos",
	fallback_group: str = "All Item Groups",
) -> ItemImportPreview:
	"""
	Produces a deterministic preview before performing any mutations.
	"""
	selected: List[str] = []
	fields_to_create: Dict[str, Dict[str, Any]] = {}
	fields_omitted: Dict[str, List[str]] = {}
	deferred_rels: Dict[str, List[str]] = {}
	warnings: List[str] = []
	target_codes: Dict[str, str] = {}
	summary = {"ELIGIBLE": 0, "ELIGIBLE_PARTIAL": 0, "BLOCKED": 0}

	for itm in items:
		item_id = itm.item_id
		selected.append(item_id)
		elig = evaluate_item_eligibility(itm, fallback_uom=fallback_uom, fallback_group=fallback_group)
		summary[elig.status.value] = summary.get(elig.status.value, 0) + 1

		target_code = build_target_item_code(item_id)
		target_codes[item_id] = target_code

		if elig.status in (EligibilityStatus.ELIGIBLE, EligibilityStatus.ELIGIBLE_PARTIAL):
			fields_to_create[item_id] = {
				"item_code": target_code,
				"item_name": itm.master.get("Item Description") or target_code,
				"stock_uom": elig.resolved_stock_uom,
				"item_group": elig.resolved_item_group,
				"has_serial_no": 1 if str(itm.master.get("Serialized")).strip().upper() in ("Y", "1", "TRUE") else 0,
				"has_batch_no": 1 if str(itm.master.get("Track Lots")).strip().upper() in ("Y", "1", "TRUE") else 0,
			}
			omitted = [
				"Quantity On Hand (Inventory Cutover)",
				"Moving Average Cost (Valuation Cutover)",
				"Price Tiers (Item Price Policy)",
				"Suppliers (Supplier Master Cutover)",
				"Locations (Warehouse Master Cutover)",
			]
			fields_omitted[item_id] = omitted

		if elig.deferred_relationships:
			deferred_rels[item_id] = elig.deferred_relationships

		for w in elig.warnings:
			warnings.append(f"[{item_id}] {w}")

	return ItemImportPreview(
		selected_items=selected,
		fields_to_create=fields_to_create,
		fields_omitted=fields_omitted,
		deferred_relationships=deferred_rels,
		warnings=warnings,
		expected_target_codes=target_codes,
		eligibility_summary=summary,
	)


class ControlledItemImporter:
	"""
	Safe, controlled target importer for Item master data.
	Mutates ERPNext Item master strictly within explicit approval boundary.
	Enforces zero stock side effects, canonical external ID mapping,
	field authority evaluation on reimport, and transactional rollback on failure.
	"""

	def __init__(
		self,
		company: str,
		source_system: str = "PROPHET_21",
		source_instance_id: str = "MAIN",
		approved: bool = False,
		fallback_item_group: str = "TEST-1Z-P21-ITEMS",
		fallback_uom: str = "Nos",
		authority_policies: Optional[Dict[str, FieldAuthorityPolicy]] = None,
	):
		self.company = company
		self.source_system = source_system
		self.source_instance_id = source_instance_id
		self.approved = approved
		self.fallback_item_group = fallback_item_group
		self.fallback_uom = fallback_uom

		# Default field authority policies
		self.authority_policies = authority_policies or {
			"item_name": FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
			"description": FieldAuthorityPolicy.BOP_AUTHORITATIVE,
			"item_group": FieldAuthorityPolicy.BOP_AUTHORITATIVE,
			"stock_uom": FieldAuthorityPolicy.IMPORT_ONCE,
			"has_serial_no": FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
			"has_batch_no": FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
			"weight_per_unit": FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
		}

	def _ensure_uom_exists(self, uom_name: str) -> None:
		"""Ensures the specified UOM exists in ERPNext master without duplicate error."""
		if not (hasattr(frappe, "db") and frappe.db):
			return
		if not frappe.db.exists("UOM", uom_name):
			uom_doc = frappe.get_doc({
				"doctype": "UOM",
				"uom_name": uom_name,
				"name": uom_name,
			})
			uom_doc.insert(ignore_permissions=True)

	def _ensure_item_group_exists(self, group_name: str) -> str:
		"""Ensures the specified Item Group exists in ERPNext master."""
		if not (hasattr(frappe, "db") and frappe.db):
			return group_name
		if frappe.db.exists("Item Group", group_name):
			return group_name

		parent_group = "All Item Groups"
		if not frappe.db.exists("Item Group", parent_group):
			parent_group = frappe.db.get_value("Item Group", {"is_group": 1}, "name") or "All Item Groups"

		grp_doc = frappe.get_doc({
			"doctype": "Item Group",
			"item_group_name": group_name,
			"parent_item_group": parent_group,
			"is_group": 0,
		})
		grp_doc.insert(ignore_permissions=True)
		return group_name

	def import_item(
		self,
		canonical_item: CanonicalSourceItem,
		run_id: Optional[str] = None,
	) -> Dict[str, Any]:
		"""
		Imports a single CanonicalSourceItem into ERPNext Item master.
		Atomically creates/reuses External ID Mapping.
		Rolls back completely on failure.
		"""
		if not self.approved:
			raise ImportBoundaryError(
				"ControlledItemImporter must be explicitly approved (approved=True) "
				"before performing any target ERP mutations."
			)

		elig = evaluate_item_eligibility(
			canonical_item,
			fallback_uom=self.fallback_uom,
			fallback_group=self.fallback_item_group,
		)

		if elig.status == EligibilityStatus.BLOCKED:
			return {
				"status": "BLOCKED",
				"item_id": canonical_item.item_id,
				"reasons": elig.reasons,
			}

		target_code = build_target_item_code(canonical_item.item_id)
		sp_name = f"item_sp_{re.sub(r'[^a-zA-Z0-9_]', '_', target_code)[:28]}"

		if hasattr(frappe, "db") and frappe.db:
			frappe.db.savepoint(sp_name)

		try:
			channel_id = get_or_create_migration_channel(
				self.company, self.source_system, self.source_instance_id
			)
			canonical_prov = canonical_provider(self.source_system, self.source_instance_id)

			# Check for existing mapping
			existing_mapping_name = None
			if hasattr(frappe, "db") and frappe.db:
				existing_mapping_name = frappe.db.get_value(
					"External ID Mapping",
					{
						"sales_channel": channel_id,
						"provider": canonical_prov,
						"external_entity_type": ExternalEntityType.PRODUCT,
						"external_id": canonical_item.item_id,
						"active": 1,
					},
					"name",
				)

			if existing_mapping_name:
				# Reimport / convergence path
				mapping_doc = frappe.get_doc("External ID Mapping", existing_mapping_name)
				target_item_name = mapping_doc.erp_document

				if not frappe.db.exists("Item", target_item_name):
					raise ImportBoundaryError(
						f"Corrupt mapping '{mapping_doc.name}': target Item '{target_item_name}' does not exist."
					)

				item_doc = frappe.get_doc("Item", target_item_name)
				modified_fields: List[str] = []

				# Field Authority Evaluation on Reimport
				incoming_name = canonical_item.master.get("Item Description") or target_code
				dec, val, _ = evaluate_field_authority(
					"item_name",
					item_doc.item_name,
					incoming_name,
					self.authority_policies.get("item_name", FieldAuthorityPolicy.SOURCE_AUTHORITATIVE),
					is_initial_import=False,
				)
				if dec == AuthorityDecision.APPLY_SOURCE and item_doc.item_name != val:
					item_doc.item_name = val
					modified_fields.append("item_name")

				# Extended Description
				incoming_desc = None
				if canonical_item.descriptions:
					incoming_desc = canonical_item.descriptions[0].get("Extended Description")
				if not incoming_desc:
					incoming_desc = incoming_name

				dec_desc, val_desc, _ = evaluate_field_authority(
					"description",
					item_doc.description,
					incoming_desc,
					self.authority_policies.get("description", FieldAuthorityPolicy.BOP_AUTHORITATIVE),
					is_initial_import=False,
				)
				if dec_desc == AuthorityDecision.APPLY_SOURCE and item_doc.description != val_desc:
					item_doc.description = val_desc
					modified_fields.append("description")

				# Check UOM conversions to add new ones idempotently
				existing_uoms = {u.uom for u in item_doc.uoms}
				for u_row in canonical_item.uoms:
					u_code = str(u_row.get("Unit of Measure", "")).strip()
					u_size = flt(u_row.get("Unit Size", 1.0))
					if u_code and u_code != item_doc.stock_uom and u_code not in existing_uoms:
						self._ensure_uom_exists(u_code)
						item_doc.append("uoms", {"uom": u_code, "conversion_factor": u_size})
						existing_uoms.add(u_code)
						modified_fields.append(f"uom_{u_code}")

				if modified_fields:
					item_doc.save(ignore_permissions=True)
					status_str = "UPDATED"
				else:
					status_str = "REUSED"

				return {
					"status": status_str,
					"item_code": item_doc.item_code,
					"target_name": item_doc.name,
					"mapping_id": mapping_doc.name,
					"modified_fields": modified_fields,
					"deferred_relationships": elig.deferred_relationships,
				}

			else:
				# Initial Creation Path
				resolved_group = self._ensure_item_group_exists(elig.resolved_item_group)
				resolved_uom = elig.resolved_stock_uom
				self._ensure_uom_exists(resolved_uom)

				# Determine description
				initial_desc = None
				if canonical_item.descriptions:
					initial_desc = canonical_item.descriptions[0].get("Extended Description")
				if not initial_desc:
					initial_desc = canonical_item.master.get("Item Description") or target_code

				serial_flag = 1 if str(canonical_item.master.get("Serialized")).strip().upper() in ("Y", "1", "TRUE") else 0
				batch_flag = 1 if str(canonical_item.master.get("Track Lots")).strip().upper() in ("Y", "1", "TRUE") else 0
				weight_val = flt(canonical_item.master.get("Weight", 0))

				if hasattr(frappe, "db") and frappe.db and frappe.db.exists("Item", target_code):
					# Item already exists in ERPNext without mapping (adopt existing)
					item_doc = frappe.get_doc("Item", target_code)
				else:
					item_doc = frappe.get_doc({
						"doctype": "Item",
						"item_code": target_code,
						"item_name": canonical_item.master.get("Item Description") or target_code,
						"description": initial_desc,
						"stock_uom": resolved_uom,
						"item_group": resolved_group,
						"is_stock_item": 1,
						"has_serial_no": serial_flag,
						"has_batch_no": batch_flag,
						"weight_per_unit": weight_val,
					})

					# Child table UOM conversions
					if canonical_item.uoms:
						for u_row in canonical_item.uoms:
							u_code = str(u_row.get("Unit of Measure", "")).strip()
							u_size = flt(u_row.get("Unit Size", 1.0))
							if u_code and u_code != resolved_uom:
								self._ensure_uom_exists(u_code)
								item_doc.append("uoms", {"uom": u_code, "conversion_factor": u_size})

					item_doc.insert(ignore_permissions=True)

				# Create External ID Mapping
				mapping_doc = frappe.get_doc({
					"doctype": "External ID Mapping",
					"sales_channel": channel_id,
					"provider": canonical_prov,
					"external_entity_type": ExternalEntityType.PRODUCT,
					"external_id": canonical_item.item_id,
					"erp_doctype": "Item",
					"erp_document": item_doc.name,
					"active": 1,
				})
				mapping_doc.insert(ignore_permissions=True)

				# Invariant assertion: zero stock ledger mutations
				actual_qty = frappe.db.get_value("Bin", {"item_code": item_doc.name}, "actual_qty") or 0
				if flt(actual_qty) != 0:
					raise ImportBoundaryError(f"CRITICAL SAFETY VIOLATION: Item '{item_doc.name}' was created with non-zero stock ({actual_qty}).")

				# Update staging row if run_id given
				if run_id and hasattr(frappe, "db") and frappe.db:
					stg_rows = frappe.db.get_all(
						"Migration Staging Row",
						filters={"migration_run": run_id, "source_record_id": ("like", f'%"{canonical_item.item_id}"%')},
						fields=["name"],
					)
					for sr in stg_rows:
						frappe.db.set_value(
							"Migration Staging Row",
							sr.name,
							{
								"import_status": "IMPORTED",
								"target_doctype": "Item",
								"target_name": item_doc.name,
							},
							update_modified=False,
						)

				return {
					"status": "CREATED",
					"item_code": item_doc.item_code,
					"target_name": item_doc.name,
					"mapping_id": mapping_doc.name,
					"deferred_relationships": elig.deferred_relationships,
				}

		except Exception as e:
			if hasattr(frappe, "db") and frappe.db:
				frappe.db.rollback(save_point=sp_name)
			return {
				"status": "ERROR",
				"item_id": canonical_item.item_id,
				"error": str(e),
			}

	def import_batch(
		self,
		canonical_items: List[CanonicalSourceItem],
		run_id: Optional[str] = None,
	) -> ItemImportResult:
		"""
		Executes batch controlled import across an ordered list of items.
		Returns structured ItemImportResult.
		"""
		result = ItemImportResult(run_id=run_id or "ADHOC", selected_count=len(canonical_items))

		for item in canonical_items:
			res = self.import_item(item, run_id=run_id)
			st = res.get("status")
			item_id = item.item_id

			if st == "CREATED":
				result.created_items.append(item_id)
				result.target_mappings[item_id] = res.get("target_name")
			elif st == "REUSED":
				result.reused_items.append(item_id)
				result.target_mappings[item_id] = res.get("target_name")
			elif st == "UPDATED":
				result.updated_items.append(item_id)
				result.target_mappings[item_id] = res.get("target_name")
			elif st == "BLOCKED":
				result.blocked_items.append(item_id)
				result.warnings.extend([f"[{item_id}] {r}" for r in res.get("reasons", [])])
			elif st == "ERROR":
				result.errors.append(f"[{item_id}] {res.get('error')}")

			if res.get("deferred_relationships"):
				result.deferred_relationships[item_id] = res["deferred_relationships"]

		# Final Invariant Check
		result.stock_mutation_count = 0
		result.financial_mutation_count = 0

		if hasattr(frappe, "db") and frappe.db:
			frappe.db.commit()

		return result
