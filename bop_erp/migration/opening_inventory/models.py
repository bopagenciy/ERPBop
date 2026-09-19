# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Any, Dict, List, Optional


class LocationMappingStatus(str, Enum):
	MAPPED = "MAPPED"
	UNMAPPED = "UNMAPPED"
	AMBIGUOUS = "AMBIGUOUS"
	INVALID = "INVALID"


class ValuationPolicyStatus(str, Enum):
	CONFIRMED = "CONFIRMED"
	PROVISIONAL = "PROVISIONAL"
	REVIEW_REQUIRED = "REVIEW_REQUIRED"
	BLOCKED = "BLOCKED"


class ItemReadinessStatus(str, Enum):
	READY = "READY"
	MISSING_ITEM = "MISSING_ITEM"
	AMBIGUOUS_ITEM = "AMBIGUOUS_ITEM"
	DISABLED_ITEM = "DISABLED_ITEM"
	REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class SourceInventoryLocationIdentity:
	"""
	Provider-neutral identity identifying a source inventory location.
	Never invents target Warehouse names.
	"""

	source_system: str
	source_instance: str
	company_id: str
	location_id: str

	@property
	def composite_key(self) -> str:
		return f"{self.source_system}:{self.source_instance}:{self.company_id}:{self.location_id}"

	@property
	def identity_hash(self) -> str:
		return hashlib.sha256(self.composite_key.encode("utf-8")).hexdigest()

	def to_dict(self) -> Dict[str, Any]:
		return {
			"source_system": self.source_system,
			"source_instance": self.source_instance,
			"company_id": self.company_id,
			"location_id": self.location_id,
			"composite_key": self.composite_key,
			"identity_hash": self.identity_hash,
		}


@dataclass
class TargetLocationMapping:
	"""
	Represents the authoritative resolution from a source location identity
	to an ERPNext Company and Warehouse.
	"""

	source_identity: SourceInventoryLocationIdentity
	status: LocationMappingStatus = LocationMappingStatus.UNMAPPED
	target_company: Optional[str] = None
	target_warehouse: Optional[str] = None
	notes: Optional[str] = None

	def to_dict(self) -> Dict[str, Any]:
		return {
			"source_identity": self.source_identity.to_dict(),
			"status": self.status.value,
			"target_company": self.target_company,
			"target_warehouse": self.target_warehouse,
			"notes": self.notes,
		}


@dataclass(frozen=True)
class PhysicalStockQuantityPolicy:
	"""
	Policy table entry defining the semantics and opening-stock contribution
	of an inventory quantity field.
	"""

	field_name: str
	meaning: str
	opening_stock_contribution: str
	is_included: bool
	is_excluded: bool
	reason: str

	def to_dict(self) -> Dict[str, Any]:
		return {
			"field_name": self.field_name,
			"meaning": self.meaning,
			"opening_stock_contribution": self.opening_stock_contribution,
			"is_included": self.is_included,
			"is_excluded": self.is_excluded,
			"reason": self.reason,
		}


@dataclass
class CutoverPolicy:
	"""
	Policy structure defining authoritative cutover timestamps.
	No default 'today' behavior allowed.
	"""

	cutoff_date: Optional[str] = None
	cutoff_time: Optional[str] = None
	timezone: Optional[str] = None
	source_snapshot_time: Optional[str] = None
	target_posting_date: Optional[str] = None
	target_posting_time: Optional[str] = None

	def is_configured(self) -> bool:
		"""
		Returns True only if all required cutover parameters are explicitly configured.
		"""
		required = [
			self.cutoff_date,
			self.cutoff_time,
			self.timezone,
			self.target_posting_date,
			self.target_posting_time,
		]
		return all(bool(r and str(r).strip()) for r in required)

	def to_dict(self) -> Dict[str, Any]:
		return {
			"cutoff_date": self.cutoff_date,
			"cutoff_time": self.cutoff_time,
			"timezone": self.timezone,
			"source_snapshot_time": self.source_snapshot_time,
			"target_posting_date": self.target_posting_date,
			"target_posting_time": self.target_posting_time,
			"is_configured": self.is_configured(),
		}


@dataclass
class SnapshotProvenancePolicy:
	"""
	Evidence requirements to prove source inventory rows belong to the same logical snapshot.
	"""

	export_timestamp: Optional[str] = None
	backup_timestamp: Optional[str] = None
	report_timestamp: Optional[str] = None
	file_manifest: Optional[Dict[str, Any]] = None
	source_snapshot_id: Optional[str] = None

	def is_acceptable(self) -> bool:
		"""
		Returns True if at least one cryptographic or authoritative timestamp/snapshot identifier exists.
		"""
		has_id = bool(self.source_snapshot_id and str(self.source_snapshot_id).strip())
		has_manifest = bool(self.file_manifest and isinstance(self.file_manifest, dict) and len(self.file_manifest) > 0)
		has_authoritative_time = bool(
			(self.export_timestamp and str(self.export_timestamp).strip())
			or (self.backup_timestamp and str(self.backup_timestamp).strip())
			or (self.report_timestamp and str(self.report_timestamp).strip())
		)
		return (has_id and has_manifest) or (has_id and has_authoritative_time) or (has_manifest and has_authoritative_time)

	def risk_classification(self) -> str:
		if self.is_acceptable():
			return "LOW"
		if self.source_snapshot_id or self.export_timestamp or self.file_manifest:
			return "MODERATE"
		return "HIGH"

	def to_dict(self) -> Dict[str, Any]:
		return {
			"export_timestamp": self.export_timestamp,
			"backup_timestamp": self.backup_timestamp,
			"report_timestamp": self.report_timestamp,
			"file_manifest": self.file_manifest,
			"source_snapshot_id": self.source_snapshot_id,
			"is_acceptable": self.is_acceptable(),
			"risk_classification": self.risk_classification(),
		}


@dataclass
class CanonicalOpeningInventoryRow:
	"""
	Provider-neutral offline aggregate representing a single source inventory location row.
	Used for readiness gating, valuation auditing, and reconciliation.
	Never mutates stock, bins, or ledgers.
	"""

	source_item_id: str
	source_company_id: str
	source_location_id: str
	target_item: Optional[str] = None
	target_company: Optional[str] = None
	target_warehouse: Optional[str] = None
	quantity_on_hand: Optional[Decimal] = None
	allocated_qty: Optional[Decimal] = None
	backordered_qty: Optional[Decimal] = None
	in_transit_qty: Optional[Decimal] = None
	in_process_qty: Optional[Decimal] = None
	primary_bin: Optional[str] = None
	sellable: Optional[str] = None
	stockable: Optional[str] = None
	track_bins: Optional[str] = None
	buy: Optional[str] = None
	make: Optional[str] = None
	discontinued: Optional[str] = None
	valuation_candidate_values: Dict[str, Any] = field(default_factory=dict)
	selected_valuation_rate: Optional[Decimal] = None
	valuation_policy_status: str = ValuationPolicyStatus.BLOCKED.value
	currency: Optional[str] = None
	serialized: bool = False
	batch_tracked: bool = False
	has_serial_detail: bool = False
	has_batch_detail: bool = False
	source_snapshot_id: Optional[str] = None
	cutoff_timestamp: Optional[str] = None
	readiness_status: str = "BLOCKED"
	blocking_reasons: List[str] = field(default_factory=list)
	source_provenance: Dict[str, Any] = field(default_factory=dict)

	@property
	def identity_key(self) -> str:
		return f"{self.source_item_id}:{self.source_company_id}:{self.source_location_id}"

	def to_dict(self) -> Dict[str, Any]:
		return {
			"source_item_id": self.source_item_id,
			"source_company_id": self.source_company_id,
			"source_location_id": self.source_location_id,
			"target_item": self.target_item,
			"target_company": self.target_company,
			"target_warehouse": self.target_warehouse,
			"quantity_on_hand": str(self.quantity_on_hand) if self.quantity_on_hand is not None else None,
			"allocated_qty": str(self.allocated_qty) if self.allocated_qty is not None else None,
			"backordered_qty": str(self.backordered_qty) if self.backordered_qty is not None else None,
			"in_transit_qty": str(self.in_transit_qty) if self.in_transit_qty is not None else None,
			"in_process_qty": str(self.in_process_qty) if self.in_process_qty is not None else None,
			"primary_bin": self.primary_bin,
			"sellable": self.sellable,
			"stockable": self.stockable,
			"track_bins": self.track_bins,
			"buy": self.buy,
			"make": self.make,
			"discontinued": self.discontinued,
			"valuation_candidate_values": {
				k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.valuation_candidate_values.items()
			},
			"selected_valuation_rate": str(self.selected_valuation_rate) if self.selected_valuation_rate is not None else None,
			"valuation_policy_status": self.valuation_policy_status,
			"currency": self.currency,
			"serialized": self.serialized,
			"batch_tracked": self.batch_tracked,
			"has_serial_detail": self.has_serial_detail,
			"has_batch_detail": self.has_batch_detail,
			"source_snapshot_id": self.source_snapshot_id,
			"cutoff_timestamp": self.cutoff_timestamp,
			"readiness_status": self.readiness_status,
			"blocking_reasons": list(self.blocking_reasons),
			"source_provenance": dict(self.source_provenance),
		}
