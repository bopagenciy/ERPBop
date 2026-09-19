# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import json
from typing import Any, Dict, List, Optional, Set, Tuple

import frappe
from frappe.utils import flt

from bop_erp.constants import ExternalEntityType
from bop_erp.migration.datasets.aggregation import CanonicalSourceItem
from bop_erp.migration.datasets.authority import (
	AuthorityDecision,
	FieldAuthorityPolicy,
	evaluate_field_authority,
)
from bop_erp.migration.datasets.importers.item_master import (
	EligibilityStatus,
	build_target_item_code,
	get_or_create_migration_channel,
)
from bop_erp.migration.namespaces import canonical_provider, compute_migration_channel_id


class DescriptionAction(str, Enum):
	CREATE = "CREATE"
	UPDATE = "UPDATE"
	NOOP = "NOOP"
	PRESERVE_BOP = "PRESERVE_BOP"
	MISSING = "MISSING"
	CONFLICT = "CONFLICT"


class UOMAction(str, Enum):
	ADD = "ADD"
	NOOP = "NOOP"
	CONFLICT = "CONFLICT"
	MISSING = "MISSING"
	INVALID = "INVALID"


@dataclass
class UOMMappingConfig:
	"""
	Configurable mapping foundation for translating source dataset UOM values
	to native ERPNext UOM records.
	"""

	mappings: Dict[str, str] = field(default_factory=lambda: {"EA": "EA", "Nos": "Nos"})
	allow_exact_match: bool = True
	allow_case_insensitive: bool = True

	def resolve_erp_uom(self, source_uom: Optional[str]) -> Optional[str]:
		"""
		Resolves a source UOM string to a valid ERPNext UOM document name.
		1. Checks explicit mappings (e.g. {"EA": "Nos"} or {"EA": "EA"}).
		2. Checks exact match against ERPNext tabUOM if allowed.
		3. Checks case-insensitive match if allowed.
		"""
		if not source_uom or not str(source_uom).strip():
			return None

		cleaned = str(source_uom).strip()

		# 1. Explicit mappings
		if cleaned in self.mappings:
			return self.mappings[cleaned]
		if cleaned.upper() in self.mappings:
			return self.mappings[cleaned.upper()]

		# 2. ERPNext database lookup
		if hasattr(frappe, "db") and frappe.db:
			if self.allow_exact_match and frappe.db.exists("UOM", cleaned):
				return cleaned

			if self.allow_case_insensitive:
				matched = frappe.db.get_value("UOM", {"uom_name": cleaned}, "name")
				if matched:
					return matched
				# Try uppercase match
				matched_upper = frappe.db.get_value("UOM", {"name": cleaned.upper()}, "name")
				if matched_upper:
					return matched_upper

		return None


class CompletenessGatePolicy(str, Enum):
	ALLOW_PARTIAL = "ALLOW_PARTIAL"
	STRICT = "STRICT"
	REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass
class DatasetCompletenessReport:
	"""
	Reconciles root source items against a secondary dataset to track
	matched, missing, and unexpected secondary-only records prior to target mutation.
	"""

	dataset_name: str
	selected_root_ids: List[str]
	secondary_ids: List[str]
	matched_ids: List[str]
	missing_ids: List[str]
	unexpected_secondary_only_ids: List[str]
	status: str = "COMPLETE"
	policy: CompletenessGatePolicy = CompletenessGatePolicy.ALLOW_PARTIAL
	warnings: List[str] = field(default_factory=list)

	def to_dict(self) -> Dict[str, Any]:
		return {
			"dataset_name": self.dataset_name,
			"selected_root_ids": list(self.selected_root_ids),
			"secondary_ids": list(self.secondary_ids),
			"matched_ids": list(self.matched_ids),
			"missing_ids": list(self.missing_ids),
			"unexpected_secondary_only_ids": list(self.unexpected_secondary_only_ids),
			"status": self.status,
			"policy": self.policy.value if hasattr(self.policy, "value") else str(self.policy),
			"warnings": list(self.warnings),
		}


def reconcile_dataset_completeness(
	selected_root_ids: List[str],
	dataset_name: str,
	secondary_ids: List[str],
	policy: CompletenessGatePolicy = CompletenessGatePolicy.ALLOW_PARTIAL,
	expected_missing_ids: Optional[Set[str]] = None,
) -> DatasetCompletenessReport:
	"""
	Produces a deterministic completeness report comparing root IDs with secondary dataset IDs.
	"""
	root_set = set(str(i).strip() for i in selected_root_ids if i)
	sec_set = set(str(i).strip() for i in secondary_ids if i)
	expected_missing = expected_missing_ids or set()

	matched = sorted(list(root_set & sec_set))
	missing = sorted(list(root_set - sec_set))
	unexpected_sec = sorted(list(sec_set - root_set))

	warnings: List[str] = []
	if missing:
		unexp_missing = set(missing) - expected_missing
		if unexp_missing:
			warnings.append(f"Unexpected missing IDs in {dataset_name}: {sorted(list(unexp_missing))}")
		else:
			warnings.append(f"Expected missing IDs in {dataset_name}: {missing}")

	if unexpected_sec:
		warnings.append(f"Secondary dataset {dataset_name} has {len(unexpected_sec)} IDs not in selected root IDs.")

	if not missing:
		status = "COMPLETE"
	elif all(m in expected_missing for m in missing) or policy == CompletenessGatePolicy.ALLOW_PARTIAL:
		status = "PARTIAL_EXPECTED"
	elif policy == CompletenessGatePolicy.STRICT:
		status = "BLOCKED"
	else:
		status = "REVIEW_REQUIRED"

	return DatasetCompletenessReport(
		dataset_name=dataset_name,
		selected_root_ids=list(selected_root_ids),
		secondary_ids=list(secondary_ids),
		matched_ids=matched,
		missing_ids=missing,
		unexpected_secondary_only_ids=unexpected_sec,
		status=status,
		policy=policy,
		warnings=warnings,
	)


@dataclass
class ItemEnrichmentPreview:
	"""
	Dry-run projection of planned master data enrichment without target mutations.
	"""

	selected_items: List[str]
	items: Dict[str, Dict[str, Any]]
	summary: Dict[str, int]
	warnings: List[str]
	deferred_relationships: Dict[str, List[str]]
	completeness_reports: Dict[str, Dict[str, Any]] = field(default_factory=dict)

	@property
	def is_blocked(self) -> bool:
		return any(
			rep.get("status") in ("BLOCKED", "REVIEW_REQUIRED")
			for rep in self.completeness_reports.values()
		) or self.summary.get("blocked", 0) > 0

	@property
	def blocking_reasons(self) -> List[str]:
		reasons = []
		for name, rep in self.completeness_reports.items():
			if rep.get("status") in ("BLOCKED", "REVIEW_REQUIRED"):
				reasons.extend(rep.get("warnings", []))
		for item_id, item_info in self.items.items():
			if item_info.get("status") == "BLOCKED":
				reasons.append(f"{item_id}: {item_info.get('reason')}")
		return reasons

	def to_dict(self) -> Dict[str, Any]:
		return {
			"selected_items": list(self.selected_items),
			"items": dict(self.items),
			"summary": dict(self.summary),
			"warnings": list(self.warnings),
			"deferred_relationships": dict(self.deferred_relationships),
			"completeness_reports": dict(self.completeness_reports),
			"is_blocked": self.is_blocked,
			"blocking_reasons": self.blocking_reasons,
		}


@dataclass
class ItemEnrichmentResult:
	"""
	Audit report of a completed multi-dataset enrichment run.
	"""

	run_id: str
	items_processed: int
	items_updated: List[str] = field(default_factory=list)
	items_noop: List[str] = field(default_factory=list)
	items_conflict: List[str] = field(default_factory=list)
	items_blocked: List[str] = field(default_factory=list)
	uom_rows_added: int = 0
	uom_rows_noop: int = 0
	uom_conflicts: List[str] = field(default_factory=list)
	descriptions_updated: int = 0
	descriptions_preserved_bop: int = 0
	descriptions_noop: int = 0
	descriptions_missing: int = 0
	deferred_relationships: Dict[str, List[str]] = field(default_factory=dict)
	warnings: List[str] = field(default_factory=list)
	errors: List[str] = field(default_factory=list)
	completeness_reports: Dict[str, Dict[str, Any]] = field(default_factory=dict)

	def to_dict(self) -> Dict[str, Any]:
		return {
			"run_id": self.run_id,
			"items_processed": self.items_processed,
			"items_updated": list(self.items_updated),
			"items_noop": list(self.items_noop),
			"items_conflict": list(self.items_conflict),
			"items_blocked": list(self.items_blocked),
			"uom_rows_added": self.uom_rows_added,
			"uom_rows_noop": self.uom_rows_noop,
			"uom_conflicts": list(self.uom_conflicts),
			"descriptions_updated": self.descriptions_updated,
			"descriptions_preserved_bop": self.descriptions_preserved_bop,
			"descriptions_noop": self.descriptions_noop,
			"descriptions_missing": self.descriptions_missing,
			"deferred_relationships": dict(self.deferred_relationships),
			"warnings": list(self.warnings),
			"errors": list(self.errors),
			"completeness_reports": dict(self.completeness_reports),
		}


def generate_enrichment_preview(
	canonical_items: List[CanonicalSourceItem],
	company: str,
	source_system: str = "PROPHET_21",
	source_instance_id: Optional[str] = None,
	uom_config: Optional[UOMMappingConfig] = None,
	description_policy: FieldAuthorityPolicy = FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
	uom_policy: FieldAuthorityPolicy = FieldAuthorityPolicy.REVIEW_ON_CONFLICT,
) -> ItemEnrichmentPreview:
	"""
	Produces a dry-run preview of planned multi-dataset enrichments.
	Strictly non-mutating: zero target writes to MariaDB.
	"""
	enricher = ControlledItemEnricher(
		company=company,
		source_system=source_system,
		source_instance_id=source_instance_id,
		uom_config=uom_config,
		description_policy=description_policy,
		uom_policy=uom_policy,
	)
	return enricher.preview(canonical_items)


class ControlledItemEnricher:
	"""
	Engine for safely enriching existing target Items using secondary datasets
	(ItemDescription, ItemUnitofMeasure) through canonical External ID Mapping.
	Strictly enforces:
	- Zero Stock / Zero Inventory mutations (no Bin, SLE, Stock Reconciliation, GL Entry).
	- Zero Item Price mutations.
	- Supplier and Location relations remain DEFERRED_DEPENDENCY.
	- Field authority policy on description and UOM conflicts.
	- Idempotency & write minimization.
	- Transactional atomicity per item.
	"""

	def __init__(
		self,
		company: Optional[str] = None,
		source_system: str = "PROPHET_21",
		source_instance_id: Optional[str] = None,
		uom_config: Optional[UOMMappingConfig] = None,
		description_policy: FieldAuthorityPolicy = FieldAuthorityPolicy.SOURCE_AUTHORITATIVE,
		uom_policy: FieldAuthorityPolicy = FieldAuthorityPolicy.REVIEW_ON_CONFLICT,
		completeness_policy: Optional[CompletenessGatePolicy] = None,
		expected_missing_ids: Optional[Dict[str, Set[str]]] = None,
		completeness_gate_policy: Optional[CompletenessGatePolicy] = None,
		expected_missing_secondary_ids: Optional[Dict[str, Set[str]]] = None,
	):
		self.company = company or (
			frappe.defaults.get_user_default("Company")
			if hasattr(frappe, "defaults") and frappe.defaults
			else "_Test Company"
		) or "_Test Company"
		self.source_system = source_system
		self.source_instance_id = source_instance_id
		self.provider = canonical_provider(source_system, source_instance_id)
		self.uom_config = uom_config or UOMMappingConfig()
		self.description_policy = description_policy
		self.uom_policy = uom_policy
		self.completeness_policy = (
			completeness_policy
			or completeness_gate_policy
			or CompletenessGatePolicy.ALLOW_PARTIAL
		)
		self.expected_missing_ids = (
			expected_missing_ids
			or expected_missing_secondary_ids
			or {}
		)
		self.channel_id = compute_migration_channel_id(self.company, source_system, source_instance_id)

	def resolve_target_item(self, item_id: str) -> Tuple[Optional[Any], Optional[str]]:
		"""
		Resolves an existing ERPNext Item document and mapping ID for a given source Item ID
		using canonical External ID Mapping.
		Returns (item_doc, mapping_name) or (None, None).
		"""
		if not (hasattr(frappe, "db") and frappe.db):
			return None, None

		filters = {
			"external_id": item_id,
			"external_entity_type": ExternalEntityType.PRODUCT,
			"erp_doctype": "Item",
			"active": 1,
		}
		mapping = frappe.db.get_value(
			"External ID Mapping",
			filters,
			["name", "erp_document"],
			as_dict=True,
		)

		if not mapping:
			# Fallback query without provider if single-channel
			mapping = frappe.db.get_value(
				"External ID Mapping",
				{"external_id": item_id, "erp_doctype": "Item", "active": 1},
				["name", "erp_document"],
				as_dict=True,
			)

		if not mapping:
			return None, None

		target_code = mapping.get("erp_document") if isinstance(mapping, dict) else mapping.erp_document
		mapping_name = mapping.get("name") if isinstance(mapping, dict) else mapping.name

		if not frappe.db.exists("Item", target_code):
			return None, mapping_name

		item_doc = frappe.get_doc("Item", target_code)
		return item_doc, mapping_name

	def preview(self, canonical_items: List[CanonicalSourceItem]) -> ItemEnrichmentPreview:
		"""
		Generates a non-mutating preview of the enrichment actions.
		"""
		items_preview: Dict[str, Dict[str, Any]] = {}
		summary = {
			"to_update": 0,
			"noop": 0,
			"conflict": 0,
			"blocked": 0,
			"missing_description": 0,
			"missing_uom": 0,
		}
		warnings: List[str] = []
		deferred_relations: Dict[str, List[str]] = {}

		for item in canonical_items:
			item_id = item.item_id
			item_doc, mapping_name = self.resolve_target_item(item_id)

			deferred = []
			if item.suppliers:
				deferred.append("INVENTORY_SUPPLIER")
			if item.locations:
				deferred.append("INVENTORY_LOCATION")
			if item.supplier_location_overrides:
				deferred.append("ITEM_SUPPLIER_BY_LOCATION")
			if deferred:
				deferred_relations[item_id] = deferred

			if not item_doc:
				summary["blocked"] += 1
				items_preview[item_id] = {
					"status": "BLOCKED",
					"reason": f"No active External ID Mapping found for source item {item_id}.",
					"description_action": DescriptionAction.MISSING.value,
					"uom_actions": [],
				}
				warnings.append(f"Item {item_id} blocked: missing External ID Mapping.")
				continue

			# Description preview
			desc_action = DescriptionAction.MISSING
			incoming_desc = None
			if item.descriptions:
				incoming_desc = item.descriptions[0].get("Extended Description") or item.descriptions[0].get("extended_description")

			if incoming_desc:
				if item_doc.description == incoming_desc:
					desc_action = DescriptionAction.NOOP
				else:
					decision, _, _ = evaluate_field_authority(
						"description",
						item_doc.description,
						incoming_desc,
						self.description_policy,
					)
					if decision == AuthorityDecision.APPLY_SOURCE:
						desc_action = DescriptionAction.UPDATE if item_doc.description else DescriptionAction.CREATE
					elif decision == AuthorityDecision.KEEP_BOP:
						desc_action = DescriptionAction.PRESERVE_BOP
					else:
						desc_action = DescriptionAction.CONFLICT
			else:
				summary["missing_description"] += 1

			# UOM preview
			uom_actions = []
			has_uom_conflict = False
			has_uom_add = False

			if not item.uoms:
				summary["missing_uom"] += 1
			else:
				for u_row in item.uoms:
					src_uom = u_row.get("Unit of Measure") or u_row.get("uom")
					src_size = u_row.get("Unit Size") or u_row.get("conversion_factor")

					try:
						factor = flt(src_size)
					except Exception:
						factor = 0.0

					if factor <= 0:
						uom_actions.append({
							"uom": src_uom,
							"action": UOMAction.INVALID.value,
							"reason": f"Conversion factor {src_size} <= 0.",
						})
						has_uom_conflict = True
						continue

					erp_uom = self.uom_config.resolve_erp_uom(src_uom)
					if not erp_uom:
						uom_actions.append({
							"uom": src_uom,
							"action": UOMAction.CONFLICT.value,
							"reason": f"Source UOM '{src_uom}' cannot be resolved to ERPNext UOM.",
						})
						has_uom_conflict = True
						continue

					if erp_uom == item_doc.stock_uom:
						if factor != 1.0:
							uom_actions.append({
								"uom": erp_uom,
								"factor": factor,
								"action": UOMAction.CONFLICT.value,
								"reason": f"Stock UOM '{erp_uom}' factor is {factor} (expected 1.0).",
							})
							has_uom_conflict = True
						else:
							uom_actions.append({
								"uom": erp_uom,
								"factor": 1.0,
								"action": UOMAction.NOOP.value,
							})
					else:
						# Child conversion row
						existing_child = None
						for c in (item_doc.uoms or []):
							if c.uom == erp_uom:
								existing_child = c
								break

						if existing_child:
							if flt(existing_child.conversion_factor) == factor:
								uom_actions.append({
									"uom": erp_uom,
									"factor": factor,
									"action": UOMAction.NOOP.value,
								})
							else:
								if self.uom_policy == FieldAuthorityPolicy.SOURCE_AUTHORITATIVE:
									uom_actions.append({
										"uom": erp_uom,
										"factor": factor,
										"action": UOMAction.ADD.value,
									})
									has_uom_add = True
								else:
									uom_actions.append({
										"uom": erp_uom,
										"factor": factor,
										"action": UOMAction.CONFLICT.value,
										"reason": f"Existing factor {existing_child.conversion_factor} vs incoming {factor}.",
									})
									has_uom_conflict = True
						else:
							uom_actions.append({
								"uom": erp_uom,
								"factor": factor,
								"action": UOMAction.ADD.value,
							})
							has_uom_add = True

			# Overall item status
			if desc_action == DescriptionAction.CONFLICT or has_uom_conflict:
				status = "CONFLICT"
				summary["conflict"] += 1
			elif desc_action in (DescriptionAction.UPDATE, DescriptionAction.CREATE) or has_uom_add:
				status = "TO_UPDATE"
				summary["to_update"] += 1
			else:
				status = "NOOP"
				summary["noop"] += 1

			items_preview[item_id] = {
				"status": status,
				"target_code": item_doc.name,
				"description_action": desc_action.value,
				"uom_actions": uom_actions,
				"stock_uom": item_doc.stock_uom,
			}

		# Run completeness pre-mutation gates
		selected_ids = [i.item_id for i in canonical_items]
		desc_ids = [i.item_id for i in canonical_items if i.descriptions]
		uom_ids = [i.item_id for i in canonical_items if i.uoms]

		desc_report = reconcile_dataset_completeness(
			selected_root_ids=selected_ids,
			dataset_name="ItemDescription",
			secondary_ids=desc_ids,
			policy=self.completeness_policy,
			expected_missing_ids=self.expected_missing_ids.get("ItemDescription"),
		)
		uom_report = reconcile_dataset_completeness(
			selected_root_ids=selected_ids,
			dataset_name="ItemUnitofMeasure",
			secondary_ids=uom_ids,
			policy=self.completeness_policy,
			expected_missing_ids=self.expected_missing_ids.get("ItemUnitofMeasure"),
		)
		completeness_reports = {
			"ItemDescription": desc_report.to_dict(),
			"ItemUnitofMeasure": uom_report.to_dict(),
		}
		warnings.extend(desc_report.warnings)
		warnings.extend(uom_report.warnings)

		return ItemEnrichmentPreview(
			selected_items=[i.item_id for i in canonical_items],
			items=items_preview,
			summary=summary,
			warnings=warnings,
			deferred_relationships=deferred_relations,
			completeness_reports=completeness_reports,
		)

	def enrich_item(
		self,
		canonical_item: CanonicalSourceItem,
		run_id: Optional[str] = None,
	) -> Dict[str, Any]:
		"""
		Enriches an existing target Item document within a transactional savepoint.
		"""
		item_id = canonical_item.item_id
		item_doc, mapping_name = self.resolve_target_item(item_id)

		if not item_doc:
			return {
				"status": "BLOCKED",
				"item_id": item_id,
				"reason": f"No active External ID Mapping for item {item_id}.",
				"modified_fields": [],
			}

		# Establish transactional savepoint (MariaDB requires valid identifier without hyphens)
		clean_id = "".join(c if c.isalnum() else "_" for c in str(item_id))
		sp_name = f"sp_enrich_{clean_id}_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(sp_name)

		try:
			modified_fields = []
			desc_action = DescriptionAction.NOOP
			uom_actions = []

			# 1. Description Enrichment
			if canonical_item.descriptions:
				incoming_desc = canonical_item.descriptions[0].get("Extended Description") or canonical_item.descriptions[0].get("extended_description")
				if incoming_desc:
					if item_doc.description != incoming_desc:
						decision, resolved_val, reason = evaluate_field_authority(
							"description",
							item_doc.description,
							incoming_desc,
							self.description_policy,
						)
						if decision == AuthorityDecision.APPLY_SOURCE:
							item_doc.description = resolved_val
							modified_fields.append("description")
							desc_action = DescriptionAction.UPDATE
						elif decision == AuthorityDecision.KEEP_BOP:
							desc_action = DescriptionAction.PRESERVE_BOP
						else:
							desc_action = DescriptionAction.CONFLICT
					else:
						desc_action = DescriptionAction.NOOP
			else:
				desc_action = DescriptionAction.MISSING

			# 2. UOM Conversions Enrichment
			if canonical_item.uoms:
				for u_row in canonical_item.uoms:
					src_uom = u_row.get("Unit of Measure") or u_row.get("uom")
					src_size = u_row.get("Unit Size") or u_row.get("conversion_factor")

					try:
						factor = flt(src_size)
					except Exception:
						factor = 0.0

					if factor <= 0:
						uom_actions.append({"uom": src_uom, "action": UOMAction.INVALID.value})
						continue

					erp_uom = self.uom_config.resolve_erp_uom(src_uom)
					if not erp_uom:
						uom_actions.append({"uom": src_uom, "action": UOMAction.CONFLICT.value})
						continue

					if erp_uom == item_doc.stock_uom:
						if factor != 1.0:
							uom_actions.append({"uom": erp_uom, "action": UOMAction.CONFLICT.value})
						else:
							uom_actions.append({"uom": erp_uom, "action": UOMAction.NOOP.value})
					else:
						# Alternate conversion row
						existing_child = None
						for c in (item_doc.uoms or []):
							if c.uom == erp_uom:
								existing_child = c
								break

						if existing_child:
							if flt(existing_child.conversion_factor) == factor:
								uom_actions.append({"uom": erp_uom, "action": UOMAction.NOOP.value})
							else:
								if self.uom_policy == FieldAuthorityPolicy.SOURCE_AUTHORITATIVE:
									existing_child.conversion_factor = factor
									modified_fields.append(f"uom_{erp_uom}")
									uom_actions.append({"uom": erp_uom, "action": UOMAction.ADD.value})
								else:
									uom_actions.append({"uom": erp_uom, "action": UOMAction.CONFLICT.value})
						else:
							# Add new conversion child row without touching existing rows
							item_doc.append("uoms", {"uom": erp_uom, "conversion_factor": factor})
							modified_fields.append(f"uom_{erp_uom}")
							uom_actions.append({"uom": erp_uom, "action": UOMAction.ADD.value})

					# Safe Sales/Purchase UOM mapping
					is_selling = str(u_row.get("Selling Unit", "")).strip().upper() in ("Y", "1", "TRUE")
					is_purchasing = str(u_row.get("Purchasing Unit", "")).strip().upper() in ("Y", "1", "TRUE")

					if is_selling and not item_doc.sales_uom:
						item_doc.sales_uom = erp_uom
						modified_fields.append("sales_uom")

					if is_purchasing and not item_doc.purchase_uom:
						item_doc.purchase_uom = erp_uom
						modified_fields.append("purchase_uom")

			# 3. Write Minimization
			if modified_fields:
				item_doc.save(ignore_permissions=True)
				status = "UPDATED"
			else:
				status = "NOOP"

			# Deferred dependencies
			deferred = []
			if canonical_item.suppliers:
				deferred.append("INVENTORY_SUPPLIER")
			if canonical_item.locations:
				deferred.append("INVENTORY_LOCATION")
			if canonical_item.supplier_location_overrides:
				deferred.append("ITEM_SUPPLIER_BY_LOCATION")

			return {
				"status": status,
				"item_id": item_id,
				"target_code": item_doc.name,
				"modified_fields": modified_fields,
				"description_action": desc_action.value,
				"uom_actions": uom_actions,
				"deferred_relationships": deferred,
			}

		except Exception as e:
			frappe.db.rollback(save_point=sp_name)
			raise

	def enrich_batch(
		self,
		canonical_items: List[CanonicalSourceItem],
		run_id: Optional[str] = None,
	) -> ItemEnrichmentResult:
		"""
		Applies multi-dataset enrichment across a batch of CanonicalSourceItems.
		"""
		actual_run_id = run_id or f"RUN-ENRICH-{frappe.generate_hash(length=8)}"
		result = ItemEnrichmentResult(
			run_id=actual_run_id,
			items_processed=len(canonical_items),
		)

		# Completeness gate evaluation before mutations
		selected_ids = [i.item_id for i in canonical_items]
		desc_ids = [i.item_id for i in canonical_items if i.descriptions]
		uom_ids = [i.item_id for i in canonical_items if i.uoms]

		desc_report = reconcile_dataset_completeness(
			selected_root_ids=selected_ids,
			dataset_name="ItemDescription",
			secondary_ids=desc_ids,
			policy=self.completeness_policy,
			expected_missing_ids=self.expected_missing_ids.get("ItemDescription"),
		)
		uom_report = reconcile_dataset_completeness(
			selected_root_ids=selected_ids,
			dataset_name="ItemUnitofMeasure",
			secondary_ids=uom_ids,
			policy=self.completeness_policy,
			expected_missing_ids=self.expected_missing_ids.get("ItemUnitofMeasure"),
		)
		result.completeness_reports = {
			"ItemDescription": desc_report.to_dict(),
			"ItemUnitofMeasure": uom_report.to_dict(),
		}
		result.warnings.extend(desc_report.warnings)
		result.warnings.extend(uom_report.warnings)

		if self.completeness_policy == CompletenessGatePolicy.STRICT:
			if desc_report.status == "BLOCKED" or uom_report.status == "BLOCKED":
				result.errors.append("Completeness gate violation: unexpected missing relationships under STRICT policy.")
				return result

		for item in canonical_items:
			try:
				res = self.enrich_item(item, run_id=actual_run_id)
				status = res.get("status")

				if status == "UPDATED":
					result.items_updated.append(item.item_id)
				elif status == "NOOP":
					result.items_noop.append(item.item_id)
				elif status == "CONFLICT":
					result.items_conflict.append(item.item_id)
				elif status == "BLOCKED":
					result.items_blocked.append(item.item_id)

				desc_act = res.get("description_action")
				if desc_act == DescriptionAction.UPDATE.value or desc_act == DescriptionAction.CREATE.value:
					result.descriptions_updated += 1
				elif desc_act == DescriptionAction.PRESERVE_BOP.value:
					result.descriptions_preserved_bop += 1
				elif desc_act == DescriptionAction.NOOP.value:
					result.descriptions_noop += 1
				elif desc_act == DescriptionAction.MISSING.value:
					result.descriptions_missing += 1

				for u_act in res.get("uom_actions", []):
					act = u_act.get("action")
					if act == UOMAction.ADD.value:
						result.uom_rows_added += 1
					elif act == UOMAction.NOOP.value:
						result.uom_rows_noop += 1
					elif act in (UOMAction.CONFLICT.value, UOMAction.INVALID.value):
						result.uom_conflicts.append(f"{item.item_id}:{u_act.get('uom')}")

				if res.get("deferred_relationships"):
					result.deferred_relationships[item.item_id] = res["deferred_relationships"]

			except Exception as e:
				result.errors.append(f"Error enriching item {item.item_id}: {str(e)}")

		return result
