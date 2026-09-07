# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class WarehouseInventorySnapshot:
	"""
	Read-only point-in-time snapshot of native ERPNext Bin quantities for an Item in a specific Warehouse.
	Maintains exact separation of native buckets without fabricating an unapproved ATS formula.
	"""
	item_code: str
	warehouse: str
	company: str
	stock_uom: str
	actual_qty: float = 0.0
	reserved_qty: float = 0.0
	ordered_qty: float = 0.0
	indented_qty: float = 0.0
	planned_qty: float = 0.0
	projected_qty: float = 0.0
	allow_sellable_stock: bool = True
	allow_fulfillment: bool = True
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
	source: str = "SOURCE ERP"


@dataclass
class ChannelInventorySnapshot:
	"""
	Read-only aggregated snapshot across enabled Channel Inventory Source warehouses for a Sales Channel.
	Strictly preserves discrete native quantities and lists contributing warehouse details.
	"""
	item_code: str
	sales_channel: str
	company: str
	stock_uom: str
	warehouses: List[WarehouseInventorySnapshot] = field(default_factory=list)
	aggregate_actual_qty: float = 0.0
	aggregate_reserved_qty: float = 0.0
	aggregate_ordered_qty: float = 0.0
	aggregate_indented_qty: float = 0.0
	aggregate_planned_qty: float = 0.0
	aggregate_projected_qty: float = 0.0
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
	source: str = "SOURCE ERP"


@dataclass
class InventoryComparisonResult:
	"""
	Diagnostic comparison between external reported stock and ERP native raw stock snapshot.
	Does NOT reconcile or mutate either side.
	"""
	item_code: str
	sales_channel: str
	external_qty: float
	erp_aggregate_actual_qty: float
	erp_aggregate_projected_qty: float
	delta_actual: float
	delta_projected: float
	external_source: str = "SOURCE EXTERNAL"
	erp_source: str = "SOURCE ERP"
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class EffectiveReservedBreakdown:
	"""
	Auditable breakdown of effective reserved demand components for an item in a warehouse.
	All demand components are mutually exclusive and partition all native ERPNext reservation sources.
	Their sum equals total_effective_reserved.
	"""
	item_code: str
	warehouse: str
	sales_order_demand: float = 0.0
	standalone_sre_demand: float = 0.0
	production_demand: float = 0.0
	subcontract_demand: float = 0.0
	production_plan_demand: float = 0.0
	total_effective_reserved: float = 0.0
	native_reserved_stock: float = 0.0
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class WarehouseATP:
	"""
	Typed Available-To-Promise (ATP) snapshot for an Item in a specific Warehouse.
	INFORMATIONAL ONLY: Non-locking point-in-time informational read without holding locks.
	Formula:
	    candidate_atp_qty = max(0, actual_qty - effective_reserved_qty - safety_stock_qty)
	Only sellable warehouses contribute positive ATP.
	"""
	item_code: str
	warehouse: str
	company: str
	actual_qty: float
	native_reserved_qty: float
	effective_reserved_qty: float
	safety_stock_qty: float
	candidate_atp_qty: float
	stock_uom: str
	allow_sellable_stock: bool = True
	allow_fulfillment: bool = True
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ChannelATP:
	"""
	Typed Available-To-Promise (ATP) aggregate for an Item across enabled sellable warehouses for a Sales Channel.
	INFORMATIONAL ONLY: Non-locking point-in-time informational read without holding locks.
	Formula:
	    channel_atp = max(0, base_pool_capacity - uncovered_sales_order_demand)
	"""
	item_code: str
	sales_channel: str
	company: str
	warehouses: List[WarehouseATP] = field(default_factory=list)
	aggregate_actual_qty: float = 0.0
	aggregate_reserved_qty: float = 0.0
	aggregate_safety_stock_qty: float = 0.0
	aggregate_atp_qty: float = 0.0
	base_physical_capacity: float = 0.0
	uncovered_sales_order_demand: float = 0.0
	cross_warehouse_adjustments: Dict[str, float] = field(default_factory=dict)
	demand_breakdown: Optional[Any] = None
	stock_uom: str = "Nos"
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())



@dataclass
class ReservationSnapshot:
	"""
	Detailed audit snapshot of active Stock Reservation Entries for an item and warehouse.
	"""
	item_code: str
	warehouse: str
	company: str
	total_reserved_qty: float
	total_delivered_qty: float
	net_reserved_qty: float
	stock_uom: str
	active_reservations: List[Dict[str, Any]] = field(default_factory=list)
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ReservationAllocation:
	"""
	Individual warehouse reservation allocation record.
	"""
	warehouse: str
	allocated_qty: float
	stock_reservation_entry: str
	idempotency_key: Optional[str] = None


@dataclass
class ReservationResult:
	"""
	Result of a stock reservation operation.
	TRANSACTIONAL GUARANTEE: Produced exclusively under exclusive row-level locking
	on tabBin and validated against active in-transaction ATP.
	"""
	success: bool
	item_code: str
	requested_qty: float
	reserved_qty: float
	unfulfilled_qty: float = 0.0
	allocations: List[ReservationAllocation] = field(default_factory=list)
	idempotency_key: Optional[str] = None
	is_idempotent_replay: bool = False
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ATPBreakdownWarehouse:
	"""
	Individual warehouse breakdown line for explainability.
	"""
	warehouse: str
	actual_qty: float
	reserved_qty: float
	safety_stock_qty: float
	atp_qty: float
	allow_sellable_stock: bool
	priority: int


@dataclass(frozen=True)
class SalesOrderDemandDetail:
	"""
	Audit detail for an individual Sales Order Item demand and linked SREs.
	"""
	sales_order: str
	sales_order_item: str
	target_warehouse: str
	pending_qty: float
	linked_sre_qty: float
	uncovered_qty: float


@dataclass
class ChannelDemandBreakdown:
	"""
	Auditable channel-level model with strict single-ownership demand partitioning.
	Separates physical warehouse capacities from logical uncovered Sales Order demand.
	"""
	base_physical_capacity: float = 0.0
	physical_sre_demand: float = 0.0
	local_non_so_commitments: float = 0.0
	safety_stock: float = 0.0
	uncovered_sales_order_demand: float = 0.0
	channel_atp: float = 0.0
	sales_order_demand: float = 0.0
	cross_warehouse_sales_order_demand: float = 0.0
	standalone_sre_demand: float = 0.0
	production_demand: float = 0.0
	subcontract_demand: float = 0.0
	production_plan_demand: float = 0.0
	warehouse_local_commitments: float = 0.0
	total_demand: float = 0.0
	so_demand_details: List[SalesOrderDemandDetail] = field(default_factory=list)
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ChannelATPBreakdown:
	"""
	Auditable explanation breakdown of Channel ATP calculation.
	"""
	item_code: str
	sales_channel: str
	company: str
	lines: List[ATPBreakdownWarehouse] = field(default_factory=list)
	channel_atp: float = 0.0
	base_physical_capacity: float = 0.0
	uncovered_sales_order_demand: float = 0.0
	cross_warehouse_adjustments: Dict[str, float] = field(default_factory=dict)
	demand_breakdown: Optional[ChannelDemandBreakdown] = None
	stock_uom: str = "Nos"
	timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
