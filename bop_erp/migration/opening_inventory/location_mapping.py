# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Dict, List, Optional, Set, Tuple

from bop_erp.migration.opening_inventory.models import (
	LocationMappingStatus,
	SourceInventoryLocationIdentity,
	TargetLocationMapping,
)


class LocationMappingRegistry:
	"""
	Provider-neutral repository and resolver for mapping source ERP inventory locations
	to target Bop ERP companies and warehouses.

	Safety Invariants:
	- NEVER creates Warehouse DocTypes in ERPNext.
	- NEVER invents target Warehouse names.
	- Any unconfigured location resolves strictly as UNMAPPED.
	- Company scope must be explicitly mapped; never inferred from location_id alone.
	- P21 Primary Bin is NEVER mapped to ERPNext Bin DocType.
	"""

	def __init__(self):
		self._mappings: Dict[str, TargetLocationMapping] = {}
		self._company_mappings: Dict[str, str] = {}

	def register_company_mapping(self, source_company_id: str, target_company: str):
		"""Registers an authoritative source company to target ERP company mapping."""
		if not source_company_id or not target_company:
			return
		self._company_mappings[str(source_company_id).strip()] = str(target_company).strip()

	def get_target_company(self, source_company_id: str) -> Optional[str]:
		"""Resolves target ERP company deterministically from source company ID."""
		if not source_company_id:
			return None
		return self._company_mappings.get(str(source_company_id).strip())

	def register_location_mapping(
		self,
		identity: SourceInventoryLocationIdentity,
		target_company: Optional[str] = None,
		target_warehouse: Optional[str] = None,
		status: LocationMappingStatus = LocationMappingStatus.MAPPED,
		notes: Optional[str] = None,
	):
		"""
		Registers an explicit mapping for a source location.
		If target_warehouse is empty or not provided, status becomes UNMAPPED.
		"""
		clean_company = str(target_company).strip() if target_company else None
		clean_warehouse = str(target_warehouse).strip() if target_warehouse else None

		if not clean_warehouse:
			status = LocationMappingStatus.UNMAPPED
		elif not clean_company:
			status = LocationMappingStatus.AMBIGUOUS

		self._mappings[identity.composite_key] = TargetLocationMapping(
			source_identity=identity,
			status=status,
			target_company=clean_company,
			target_warehouse=clean_warehouse,
			notes=notes,
		)

	def resolve_location(
		self,
		source_system: str,
		source_instance: str,
		company_id: str,
		location_id: str,
	) -> TargetLocationMapping:
		"""
		Resolves a source location identity against registered mappings.
		Returns TargetLocationMapping with status MAPPED, UNMAPPED, AMBIGUOUS, or INVALID.
		"""
		clean_co = str(company_id).strip() if company_id is not None else ""
		clean_loc = str(location_id).strip() if location_id is not None else ""

		identity = SourceInventoryLocationIdentity(
			source_system=str(source_system).strip(),
			source_instance=str(source_instance).strip(),
			company_id=clean_co,
			location_id=clean_loc,
		)

		if not clean_co or not clean_loc:
			return TargetLocationMapping(
				source_identity=identity,
				status=LocationMappingStatus.INVALID,
				notes="Missing source company_id or location_id.",
			)

		# Look up explicit mapping
		if identity.composite_key in self._mappings:
			return self._mappings[identity.composite_key]

		# Check if company is mapped even if warehouse is not
		target_co = self.get_target_company(clean_co)

		return TargetLocationMapping(
			source_identity=identity,
			status=LocationMappingStatus.UNMAPPED,
			target_company=target_co,
			target_warehouse=None,
			notes="No authoritative warehouse mapping configured for source location.",
		)

	def audit_unique_locations(
		self,
		locations: List[Tuple[str, str]],
		source_system: str = "PROPHET_21",
		source_instance: str = "DEFAULT",
	) -> List[Dict[str, Any]]:
		"""
		Builds a readiness classification list for unique (company_id, location_id) pairs.
		"""
		results = []
		unique_pairs: Set[Tuple[str, str]] = set()
		for co, loc in locations:
			unique_pairs.add((str(co).strip(), str(loc).strip()))

		for co, loc in sorted(unique_pairs):
			mapping = self.resolve_location(
				source_system=source_system,
				source_instance=source_instance,
				company_id=co,
				location_id=loc,
			)
			results.append({
				"source_company_id": co,
				"source_location_id": loc,
				"status": mapping.status.value,
				"target_company": mapping.target_company,
				"target_warehouse": mapping.target_warehouse,
				"notes": mapping.notes,
			})
		return results
