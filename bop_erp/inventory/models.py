# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


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
	timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
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
	timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
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
	timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
