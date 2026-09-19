# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from bop_erp.migration.opening_inventory.location_mapping import LocationMappingRegistry
from bop_erp.migration.opening_inventory.models import (
	CanonicalOpeningInventoryRow,
	CutoverPolicy,
	ItemReadinessStatus,
	LocationMappingStatus,
	PhysicalStockQuantityPolicy,
	SnapshotProvenancePolicy,
	ValuationPolicyStatus,
)


def get_physical_stock_quantity_policy_table() -> List[PhysicalStockQuantityPolicy]:
	"""
	Returns the authoritative policy table defining physical stock and demand separation.
	"""
	return [
		PhysicalStockQuantityPolicy(
			field_name="Quantity On Hand",
			meaning="Physical stock physically present in the warehouse location.",
			opening_stock_contribution="Direct physical stock basis (1:1).",
			is_included=True,
			is_excluded=False,
			reason="Represents verifiable on-hand stock. Default candidate for opening physical stock.",
		),
		PhysicalStockQuantityPolicy(
			field_name="Quantity Allocated",
			meaning="Stock reserved/allocated for open sales orders, quotes, or pick tickets.",
			opening_stock_contribution="Zero contribution to physical opening stock.",
			is_included=False,
			is_excluded=True,
			reason="Demand allocation. Folding into physical stock would double-count stock when sales orders are imported.",
		),
		PhysicalStockQuantityPolicy(
			field_name="Quantity Backordered",
			meaning="Unfulfilled customer demand awaiting replenishment.",
			opening_stock_contribution="Zero contribution to physical opening stock.",
			is_included=False,
			is_excluded=True,
			reason="Represents unmet demand, not physical stock on shelf.",
		),
		PhysicalStockQuantityPolicy(
			field_name="Quantity In Transit",
			meaning="Inventory currently in transit between warehouses or inbound from purchase orders.",
			opening_stock_contribution="Zero contribution to physical opening stock.",
			is_included=False,
			is_excluded=True,
			reason="Not physically present at destination. Managed via Material Transfers or Inward Purchase Receipts.",
		),
		PhysicalStockQuantityPolicy(
			field_name="Quantity in Process",
			meaning="Stock currently issued to or being converted in manufacturing, assembly, or kitting.",
			opening_stock_contribution="Zero contribution to physical opening stock.",
			is_included=False,
			is_excluded=True,
			reason="Work-in-progress stock managed via Work Orders / Job Cards, not available on-hand stock.",
		),
	]


def assess_opening_inventory_readiness(
	row: CanonicalOpeningInventoryRow,
	mapping_registry: Optional[LocationMappingRegistry] = None,
	cutover_policy: Optional[CutoverPolicy] = None,
	snapshot_policy: Optional[SnapshotProvenancePolicy] = None,
	currency_confirmed: bool = False,
	approved_valuation_policy: Optional[ValuationPolicyStatus] = None,
	known_identities: Optional[Set[str]] = None,
	existing_rows_by_identity: Optional[Dict[str, CanonicalOpeningInventoryRow]] = None,
) -> Tuple[bool, List[str]]:
	"""
	Deterministic readiness gate for an opening inventory row.
	Evaluates all safety constraints in strict order.

	Returns:
	    (is_ready: bool, blocking_reasons: List[str])

	Side effects on row:
	    - Updates row.readiness_status
	    - Updates row.blocking_reasons
	    - Enforces selected_valuation_rate remains None if unconfirmed.
	"""
	blockers: List[str] = []

	# 1. Target Item Resolution Gate
	if not row.target_item or row.target_item == ItemReadinessStatus.MISSING_ITEM.value:
		blockers.append("MISSING_ITEM: Target Item could not be resolved from CanonicalSourceItem / External ID Mapping.")
	elif row.target_item == ItemReadinessStatus.AMBIGUOUS_ITEM.value:
		blockers.append("AMBIGUOUS_ITEM: Source Item ID maps to multiple conflicting target items.")
	elif row.target_item == ItemReadinessStatus.DISABLED_ITEM.value:
		blockers.append("DISABLED_ITEM: Target Item exists in ERP but is marked disabled.")
	elif row.target_item == ItemReadinessStatus.REVIEW_REQUIRED.value:
		blockers.append("REVIEW_REQUIRED: Item mapping requires explicit manual review.")

	# 2. Company Scope Gate
	if not row.source_company_id or not str(row.source_company_id).strip():
		blockers.append("BLOCKED_AMBIGUOUS_COMPANY: Source Company ID is missing.")
	elif not row.target_company or not str(row.target_company).strip():
		blockers.append(f"BLOCKED_AMBIGUOUS_COMPANY: Target ERP Company unresolved for source company '{row.source_company_id}'.")

	# 3. Location to Warehouse Mapping Gate
	if mapping_registry:
		loc_mapping = mapping_registry.resolve_location(
			source_system="PROPHET_21",
			source_instance="DEFAULT",
			company_id=row.source_company_id,
			location_id=row.source_location_id,
		)
		if loc_mapping.status != LocationMappingStatus.MAPPED:
			blockers.append(
				f"UNMAPPED_WAREHOUSE: Location '{row.source_location_id}' (Company: '{row.source_company_id}') "
				f"status is {loc_mapping.status.value}. Target Warehouse is unmapped."
			)
		elif not row.target_warehouse:
			row.target_warehouse = loc_mapping.target_warehouse
	elif not row.target_warehouse or not str(row.target_warehouse).strip():
		blockers.append(
			f"UNMAPPED_WAREHOUSE: Location '{row.source_location_id}' has no mapped target warehouse."
		)

	# 4. Physical Quantity Validation Gate
	if row.quantity_on_hand is None:
		blockers.append("BLOCKED_NULL_QUANTITY: Quantity On Hand is null. Data quality review required.")
	else:
		try:
			q_val = Decimal(str(row.quantity_on_hand))
			if q_val < Decimal("0"):
				blockers.append(f"BLOCKED_NEGATIVE_QUANTITY: Negative Quantity On Hand ({q_val}) is forbidden.")
			elif q_val == Decimal("0"):
				# Zero QOH: valid, but no opening stock ledger row required
				blockers.append("ZERO_QUANTITY: Zero Quantity On Hand; no opening stock entry required.")
		except Exception:
			blockers.append(f"BLOCKED_INVALID_QUANTITY: Quantity On Hand '{row.quantity_on_hand}' is not a valid number.")

	# 5. Serialized / Lot-Tracked Gate
	if row.serialized and not row.has_serial_detail:
		blockers.append("BLOCKED_PENDING_SERIAL_DETAIL: Item requires serial numbers but no serial source data exists.")
	if row.batch_tracked and not row.has_batch_detail:
		blockers.append("BLOCKED_PENDING_BATCH_DETAIL: Item requires lot/batch tracking but no batch source data exists.")

	# 6. Valuation Policy Gate
	# Selected valuation rate MUST remain unset until policy is confirmed
	if approved_valuation_policy != ValuationPolicyStatus.CONFIRMED:
		row.selected_valuation_rate = None
		row.valuation_policy_status = ValuationPolicyStatus.BLOCKED.value
		blockers.append(
			"BLOCKED_VALUATION_UNCONFIRMED: Opening valuation policy is unconfirmed. "
			"Selecting a valuation source requires explicit client / accounting confirmation."
		)
	else:
		row.valuation_policy_status = ValuationPolicyStatus.CONFIRMED.value

	# 7. Currency Readiness Gate
	if not currency_confirmed or not row.currency or not str(row.currency).strip():
		blockers.append(
			"BLOCKED_CURRENCY_UNKNOWN: Currency cannot be proven from provided datasets. "
			"Assuming USD without explicit proof is forbidden."
		)

	# 8. Cutover Date / Time Authority Gate
	if not cutover_policy or not cutover_policy.is_configured():
		blockers.append(
			"BLOCKED_CUTOFF_MISSING: Authoritative cutover timestamp is not configured. "
			"Default 'today' behavior is forbidden."
		)
	else:
		row.cutoff_timestamp = f"{cutover_policy.cutoff_date} {cutover_policy.cutoff_time} {cutover_policy.timezone}"

	# 9. Source Snapshot Consistency Gate
	if not snapshot_policy or not snapshot_policy.is_acceptable():
		blockers.append(
			"BLOCKED_SNAPSHOT_PROVENANCE: Source snapshot consistency cannot be verified across datasets."
		)

	# 10. Duplicate Location Inventory Gate
	ident = row.identity_key
	if known_identities is not None:
		if ident in known_identities:
			# If existing row has conflicting quantity or values, block
			if existing_rows_by_identity and ident in existing_rows_by_identity:
				prior = existing_rows_by_identity[ident]
				if prior.quantity_on_hand != row.quantity_on_hand or prior.valuation_candidate_values != row.valuation_candidate_values:
					blockers.append(f"BLOCKED_DUPLICATE_IDENTITY_CONFLICT: Conflicting duplicate row for identity '{ident}'.")
				else:
					blockers.append(f"DUPLICATE_IDENTICAL_ROW: Identical duplicate row for identity '{ident}'. Staged once.")
			else:
				blockers.append(f"BLOCKED_DUPLICATE_IDENTITY: Duplicate row for identity '{ident}'.")
		known_identities.add(ident)

	# Final readiness determination
	is_ready = (len(blockers) == 0)
	row.readiness_status = "READY" if is_ready else "BLOCKED"
	row.blocking_reasons = blockers

	return is_ready, blockers
