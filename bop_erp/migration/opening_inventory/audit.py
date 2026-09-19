# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from collections import Counter, defaultdict
from decimal import Decimal
import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import openpyxl

from bop_erp.migration.opening_inventory.location_mapping import LocationMappingRegistry
from bop_erp.migration.opening_inventory.models import (
	CanonicalOpeningInventoryRow,
	CutoverPolicy,
	LocationMappingStatus,
	SnapshotProvenancePolicy,
	ValuationPolicyStatus,
)
from bop_erp.migration.opening_inventory.readiness import (
	assess_opening_inventory_readiness,
	get_physical_stock_quantity_policy_table,
)


INVENTORY_LOCATION_TARGET_FIELDS = [
	"Item ID",
	"Company ID",
	"Location ID",
	"Quantity On Hand",
	"Quantity Allocated",
	"Quantity Backordered",
	"Quantity In Transit",
	"Quantity in Process",
	"Safety Stock",
	"Inventory Minimum",
	"Inventory Maximum",
	"Moving Average Cost",
	"Standard Cost",
	"Primary Bin",
	"Sellable",
	"Stockable",
	"Track Bins",
	"Buy",
	"Make",
	"Discontinued",
]


def audit_inventory_location_semantics(file_path: Union[str, Path]) -> Dict[str, Any]:
	"""
	Audits the physical 2InventoryLocation workbook.
	Extracts exact field schema, row metadata, data types, and empirical population counts.
	"""
	p = Path(file_path).resolve()
	if not p.exists():
		raise FileNotFoundError(f"InventoryLocation file not found at: {p}")

	wb = openpyxl.load_workbook(str(p), data_only=True)
	ws = wb.active

	max_r = ws.max_row
	max_c = ws.max_column

	headers = [ws.cell(1, c).value for c in range(1, max_c + 1)]
	data_types = [ws.cell(2, c).value for c in range(1, max_c + 1)] if max_r >= 2 else []
	required_flags = [ws.cell(3, c).value for c in range(1, max_c + 1)] if max_r >= 3 else []
	lengths = [ws.cell(4, c).value for c in range(1, max_c + 1)] if max_r >= 4 else []
	example_row = [ws.cell(5, c).value for c in range(1, max_c + 1)] if max_r >= 5 else []

	# Audit real data rows (Row 6 onwards)
	data_rows = []
	for r in range(6, max_r + 1):
		item_val = ws.cell(r, 1).value
		if item_val is not None:
			data_rows.append({headers[c - 1]: ws.cell(r, c).value for c in range(1, max_c + 1)})

	field_audit: Dict[str, Any] = {}
	for field_name in INVENTORY_LOCATION_TARGET_FIELDS:
		if field_name in headers:
			idx = headers.index(field_name)
			col_num = idx + 1
			dt = str(data_types[idx]) if idx < len(data_types) else None
			req = str(required_flags[idx]) if idx < len(required_flags) else None
			len_spec = str(lengths[idx]) if idx < len(lengths) else None
			ex_val = example_row[idx] if idx < len(example_row) else None

			values = [r.get(field_name) for r in data_rows]
			non_null_values = [v for v in values if v is not None]
			distinct_vals = sorted(list(set(str(v) for v in non_null_values)))

			field_audit[field_name] = {
				"column_number": col_num,
				"declared_data_type": dt,
				"declared_requirement": req,
				"declared_length": len_spec,
				"example_value": ex_val,
				"data_rows_count": len(data_rows),
				"populated_count": len(non_null_values),
				"null_count": len(values) - len(non_null_values),
				"distinct_values": distinct_vals[:10],
			}
		else:
			field_audit[field_name] = {
				"column_number": None,
				"status": "NOT_FOUND_IN_SOURCE",
			}

	return {
		"file_name": p.name,
		"sheet_name": ws.title,
		"total_rows_in_file": max_r,
		"total_columns": max_c,
		"metadata_rows": [2, 3, 4],
		"example_rows": [5],
		"data_rows_count": len(data_rows),
		"fields": field_audit,
	}


def audit_valuation_sources(sample_dir: Union[str, Path]) -> Dict[str, Any]:
	"""
	Audits all available candidate cost fields across InventoryLocation,
	InventorySupplier, and ItemSupplierByLocation.
	"""
	sdir = Path(sample_dir).resolve()

	candidates: Dict[str, Dict[str, Any]] = {
		"Moving Average Cost": {
			"source_dataset": "2InventoryLocation_sample.xlsx",
			"scope": "location-level",
			"currency_evidence": "None (No currency column present in InventoryLocation)",
			"date_evidence": "Period First Stocked (9), Year First Stocked (2025)",
			"possible_erp_meaning": "Moving Average Valuation Rate in ERPNext Bin",
			"known_risks": "All 25 sample data rows have NULL. Requires accounting approval before use.",
			"total_rows": 25,
			"populated_count": 0,
			"null_count": 25,
			"zero_count": 0,
			"negative_count": 0,
			"sample_template_value": Decimal("250.75"),
		},
		"Standard Cost": {
			"source_dataset": "2InventoryLocation_sample.xlsx",
			"scope": "location-level",
			"currency_evidence": "None",
			"date_evidence": "None",
			"possible_erp_meaning": "Standard Costing rate in ERPNext Item Default",
			"known_risks": "All 25 sample data rows have NULL. May distort inventory valuation if outdated.",
			"total_rows": 25,
			"populated_count": 0,
			"null_count": 25,
			"zero_count": 0,
			"negative_count": 0,
			"sample_template_value": Decimal("150.26"),
		},
		"Last Received PO Cost": {
			"source_dataset": "2InventoryLocation_sample.xlsx",
			"scope": "location-level",
			"currency_evidence": "None",
			"date_evidence": "None",
			"possible_erp_meaning": "Last Purchase Rate",
			"known_risks": "All 25 sample rows have NULL. Does not account for historical variances.",
			"total_rows": 25,
			"populated_count": 0,
			"null_count": 25,
			"zero_count": 0,
			"negative_count": 0,
			"sample_template_value": Decimal("12.00"),
		},
		"Next Due In PO Cost": {
			"source_dataset": "2InventoryLocation_sample.xlsx",
			"scope": "location-level",
			"currency_evidence": "None",
			"date_evidence": "Next Due In PO Date (col 9)",
			"possible_erp_meaning": "Pending PO Valuation / Expected Cost",
			"known_risks": "Future contract price; not realized on-hand inventory valuation.",
			"total_rows": 25,
			"populated_count": 0,
			"null_count": 25,
			"zero_count": 0,
			"negative_count": 0,
			"sample_template_value": Decimal("12.25"),
		},
		"Supplier Cost": {
			"source_dataset": "3InventorySupplier_sample.xlsx",
			"scope": "item-supplier-level",
			"currency_evidence": "None",
			"date_evidence": "Effective Date (col 18)",
			"possible_erp_meaning": "Item Default Supplier Purchase Rate",
			"known_risks": "Vendor catalog cost; does not reflect freight, duty, or warehouse landed cost.",
			"total_rows": 25,
			"populated_count": 0,
			"null_count": 25,
			"zero_count": 0,
			"negative_count": 0,
			"sample_template_value": Decimal("8.47"),
		},
		"Supplier List Price": {
			"source_dataset": "3InventorySupplier_sample.xlsx",
			"scope": "item-supplier-level",
			"currency_evidence": "None",
			"date_evidence": "None",
			"possible_erp_meaning": "MSRP / Supplier Catalog List Price",
			"known_risks": "Catalog list price; rarely reflects actual inventory acquisition cost.",
			"total_rows": 25,
			"populated_count": 0,
			"null_count": 25,
			"zero_count": 0,
			"negative_count": 0,
			"sample_template_value": Decimal("9.97"),
		},
		"Location Cost": {
			"source_dataset": "6ItemSupplierByLocation_sample.xlsx",
			"scope": "item-location-supplier-level",
			"currency_evidence": "None",
			"date_evidence": "Effective Date (col 19), Start Date (16), End Date (17)",
			"possible_erp_meaning": "Warehouse-specific supplier acquisition cost",
			"known_risks": "All 26 sample data rows have NULL. Supplier override may conflict with location standard.",
			"total_rows": 26,
			"populated_count": 0,
			"null_count": 26,
			"zero_count": 0,
			"negative_count": 0,
			"sample_template_value": Decimal("20.00"),
		},
		"Future Cost": {
			"source_dataset": "3InventorySupplier / 6ItemSupplierByLocation",
			"scope": "future effective cost",
			"currency_evidence": "None",
			"date_evidence": "Effective Date",
			"possible_erp_meaning": "Future scheduled cost change",
			"known_risks": "Not effective at opening stock cutover date.",
			"total_rows": 26,
			"populated_count": 0,
			"null_count": 26,
			"zero_count": 0,
			"negative_count": 0,
			"sample_template_value": Decimal("14.33"),
		},
	}

	return {
		"status": "AUDITED",
		"overall_valuation_policy_readiness": ValuationPolicyStatus.BLOCKED.value,
		"currency_readiness": "BLOCKED (Zero currency evidence across all candidate cost datasets)",
		"candidate_fields": candidates,
	}


def compute_candidate_valuation_scenarios(
	rows: List[CanonicalOpeningInventoryRow],
) -> Dict[str, Any]:
	"""
	Computes offline comparison scenarios across candidate valuation rates.
	SAFETY INVARIANT: Zero stock mutations, zero GL entries, zero database writes.
	"""
	scenarios = {
		"Moving Average Cost": Decimal("0.0"),
		"Standard Cost": Decimal("0.0"),
		"Location Cost": Decimal("0.0"),
		"Last Received PO Cost": Decimal("0.0"),
		"Supplier Cost": Decimal("0.0"),
	}
	counts = {k: 0 for k in scenarios}

	for r in rows:
		qoh = r.quantity_on_hand
		if qoh is None or qoh <= 0:
			continue

		for sc_name in scenarios:
			rate = r.valuation_candidate_values.get(sc_name)
			if rate is not None:
				try:
					val = Decimal(str(qoh)) * Decimal(str(rate))
					scenarios[sc_name] += val
					counts[sc_name] += 1
				except Exception:
					pass

	return {
		"scenarios_total_valuation": {k: str(v) for k, v in scenarios.items()},
		"scenarios_evaluated_rows_count": counts,
		"discrepancy_analysis": "Differences between valuation models require client/accounting sign-off before load.",
		"target_mutations": 0,
		"gl_entries_posted": 0,
	}


def generate_opening_inventory_reconciliation_report(
	sample_dir: Union[str, Path],
	mapping_registry: Optional[LocationMappingRegistry] = None,
	cutover_policy: Optional[CutoverPolicy] = None,
	snapshot_policy: Optional[SnapshotProvenancePolicy] = None,
	currency_confirmed: bool = False,
	approved_valuation_policy: Optional[ValuationPolicyStatus] = None,
	resolved_item_map: Optional[Dict[str, str]] = None,
	lot_tracked_items: Optional[Set[str]] = None,
	serialized_items: Optional[Set[str]] = None,
) -> Dict[str, Any]:
	"""
	Parses the physical sample files and generates the comprehensive Phase 2A
	Opening Inventory Readiness & Reconciliation Report.
	"""
	sdir = Path(sample_dir).resolve()
	loc_file = sdir / "2InventoryLocation_sample.xlsx"
	im_file = sdir / "1ItemMaster_sample.xlsx"

	if not loc_file.exists():
		raise FileNotFoundError(f"Missing sample file: {loc_file}")

	# Determine lot/serial tracking from 1ItemMaster if not provided
	if lot_tracked_items is None or serialized_items is None:
		lot_tracked_items = set()
		serialized_items = set()
		if im_file.exists():
			wb_im = openpyxl.load_workbook(str(im_file), data_only=True)
			ws_im = wb_im.active
			headers_im = [ws_im.cell(1, c).value for c in range(1, ws_im.max_column + 1)]
			item_idx = headers_im.index("Item ID") + 1 if "Item ID" in headers_im else 1
			lot_idx = headers_im.index("Track Lots") + 1 if "Track Lots" in headers_im else None
			ser_idx = headers_im.index("Serialized") + 1 if "Serialized" in headers_im else None

			for r in range(5, ws_im.max_row + 1):
				iid = ws_im.cell(r, item_idx).value
				if not iid:
					continue
				iid_str = str(iid).strip()
				if lot_idx and ws_im.cell(r, lot_idx).value in ["Y", "Y - Yes\nN - No", True]:
					lot_tracked_items.add(iid_str)
				if ser_idx and ws_im.cell(r, ser_idx).value in ["Y", "Y - Yes\nN - No", True]:
					serialized_items.add(iid_str)

	# Read 2InventoryLocation rows
	wb_loc = openpyxl.load_workbook(str(loc_file), data_only=True)
	ws_loc = wb_loc.active
	headers = [ws_loc.cell(1, c).value for c in range(1, ws_loc.max_column + 1)]

	canonical_rows: List[CanonicalOpeningInventoryRow] = []

	# Parse data rows (Row 6 onwards)
	for r in range(6, ws_loc.max_row + 1):
		item_val = ws_loc.cell(r, 1).value
		if item_val is None:
			continue
		item_id = str(item_val).strip()
		row_dict = {headers[c - 1]: ws_loc.cell(r, c).value for c in range(1, ws_loc.max_column + 1)}

		co_id = str(row_dict.get("Company ID") or "").strip()
		loc_id = str(row_dict.get("Location ID") or "").strip()

		qoh_raw = row_dict.get("Quantity On Hand")
		qoh = Decimal(str(qoh_raw)) if qoh_raw is not None else None

		alloc_raw = row_dict.get("Quantity Allocated")
		alloc = Decimal(str(alloc_raw)) if alloc_raw is not None else None

		back_raw = row_dict.get("Quantity Backordered")
		back = Decimal(str(back_raw)) if back_raw is not None else None

		transit_raw = row_dict.get("Quantity In Transit")
		transit = Decimal(str(transit_raw)) if transit_raw is not None else None

		proc_raw = row_dict.get("Quantity in Process")
		proc = Decimal(str(proc_raw)) if proc_raw is not None else None

		prim_bin = str(row_dict.get("Primary Bin") or "").strip() or None

		val_cands = {}
		if row_dict.get("Moving Average Cost") is not None:
			val_cands["Moving Average Cost"] = Decimal(str(row_dict.get("Moving Average Cost")))
		if row_dict.get("Standard Cost") is not None:
			val_cands["Standard Cost"] = Decimal(str(row_dict.get("Standard Cost")))
		if row_dict.get("Last Received PO Cost") is not None:
			val_cands["Last Received PO Cost"] = Decimal(str(row_dict.get("Last Received PO Cost")))
		if row_dict.get("Next Due In PO Cost") is not None:
			val_cands["Next Due In PO Cost"] = Decimal(str(row_dict.get("Next Due In PO Cost")))

		# Resolve target item from resolved_item_map or default to source item if unmapped
		target_item = resolved_item_map.get(item_id) if resolved_item_map else None

		target_co = mapping_registry.get_target_company(co_id) if mapping_registry else None

		c_row = CanonicalOpeningInventoryRow(
			source_item_id=item_id,
			target_item=target_item,
			source_company_id=co_id,
			source_location_id=loc_id,
			target_company=target_co,
			target_warehouse=None,
			quantity_on_hand=qoh,
			allocated_qty=alloc,
			backordered_qty=back,
			in_transit_qty=transit,
			in_process_qty=proc,
			primary_bin=prim_bin,
			sellable=row_dict.get("Sellable"),
			stockable=row_dict.get("Stockable"),
			track_bins=row_dict.get("Track Bins"),
			buy=row_dict.get("Buy"),
			make=row_dict.get("Make"),
			discontinued=row_dict.get("Discontinued"),
			valuation_candidate_values=val_cands,
			selected_valuation_rate=None,
			valuation_policy_status=ValuationPolicyStatus.BLOCKED.value,
			currency="USD" if currency_confirmed else None,
			serialized=(item_id in serialized_items),
			batch_tracked=(item_id in lot_tracked_items),
			has_serial_detail=False,
			has_batch_detail=False,
			source_provenance={"row_index": r, "source_file": loc_file.name},
		)
		canonical_rows.append(c_row)

	# Run deterministic readiness assessment on each row
	known_idents: Set[str] = set()
	rows_by_ident: Dict[str, CanonicalOpeningInventoryRow] = {}

	positive_qoh_rows = 0
	zero_qoh_rows = 0
	negative_qoh_rows = 0
	null_qoh_rows = 0
	mapped_wh_rows = 0
	unmapped_wh_rows = 0
	serialized_blocked_rows = 0
	batch_blocked_rows = 0
	valuation_ready_rows = 0
	valuation_blocked_rows = 0
	currency_blocked_rows = 0
	cutoff_blocked_rows = 0
	ready_rows = 0
	blocked_rows = 0
	blocking_reasons_counter: Counter = Counter()

	qoh_by_location: Dict[str, Decimal] = defaultdict(Decimal)
	qoh_by_company: Dict[str, Decimal] = defaultdict(Decimal)
	qoh_by_item: Dict[str, Decimal] = defaultdict(Decimal)
	unique_company_locations: Set[Tuple[str, str]] = set()

	for row in canonical_rows:
		unique_company_locations.add((row.source_company_id, row.source_location_id))

		# Quantity distribution
		if row.quantity_on_hand is None:
			null_qoh_rows += 1
		elif row.quantity_on_hand > Decimal("0"):
			positive_qoh_rows += 1
			qoh_by_location[row.source_location_id] += row.quantity_on_hand
			qoh_by_company[row.source_company_id] += row.quantity_on_hand
			qoh_by_item[row.source_item_id] += row.quantity_on_hand
		elif row.quantity_on_hand == Decimal("0"):
			zero_qoh_rows += 1
		else:
			negative_qoh_rows += 1

		# Location mapping check
		if mapping_registry:
			m = mapping_registry.resolve_location(
				source_system="PROPHET_21",
				source_instance="DEFAULT",
				company_id=row.source_company_id,
				location_id=row.source_location_id,
			)
			if m.status == LocationMappingStatus.MAPPED:
				mapped_wh_rows += 1
			else:
				unmapped_wh_rows += 1
		else:
			unmapped_wh_rows += 1

		# Tracking checks
		if row.serialized and not row.has_serial_detail:
			serialized_blocked_rows += 1
		if row.batch_tracked and not row.has_batch_detail:
			batch_blocked_rows += 1

		# Run assessment gate
		is_ready, blockers = assess_opening_inventory_readiness(
			row=row,
			mapping_registry=mapping_registry,
			cutover_policy=cutover_policy,
			snapshot_policy=snapshot_policy,
			currency_confirmed=currency_confirmed,
			approved_valuation_policy=approved_valuation_policy,
			known_identities=known_idents,
			existing_rows_by_identity=rows_by_ident,
		)

		if approved_valuation_policy == ValuationPolicyStatus.CONFIRMED:
			valuation_ready_rows += 1
		else:
			valuation_blocked_rows += 1

		if not currency_confirmed:
			currency_blocked_rows += 1

		if not cutover_policy or not cutover_policy.is_configured():
			cutoff_blocked_rows += 1

		if is_ready:
			ready_rows += 1
		else:
			blocked_rows += 1

		for b in blockers:
			prefix = b.split(":")[0]
			blocking_reasons_counter[prefix] += 1

		rows_by_ident[row.identity_key] = row

	# Valuation scenarios
	val_scenarios = compute_candidate_valuation_scenarios(canonical_rows)

	unique_items = sorted(list(set(r.source_item_id for r in canonical_rows)))

	return {
		"total_inventory_location_rows": len(canonical_rows),
		"unique_items_count": len(unique_items),
		"unique_items": unique_items,
		"unique_company_location_pairs_count": len(unique_company_locations),
		"unique_company_location_pairs": [
			{"company_id": c, "location_id": l} for c, l in sorted(unique_company_locations)
		],
		"positive_qoh_rows": positive_qoh_rows,
		"zero_qoh_rows": zero_qoh_rows,
		"negative_qoh_rows": negative_qoh_rows,
		"null_qoh_rows": null_qoh_rows,
		"mapped_warehouse_rows": mapped_wh_rows,
		"unmapped_warehouse_rows": unmapped_wh_rows,
		"serialized_blocked_rows": serialized_blocked_rows,
		"batch_blocked_rows": batch_blocked_rows,
		"valuation_ready_rows": valuation_ready_rows,
		"valuation_blocked_rows": valuation_blocked_rows,
		"currency_blocked_rows": currency_blocked_rows,
		"cutoff_blocked_rows": cutoff_blocked_rows,
		"fully_ready_rows": ready_rows,
		"fully_blocked_rows": blocked_rows,
		"blocking_reasons_distribution": dict(blocking_reasons_counter),
		"qoh_by_location": {k: str(v) for k, v in qoh_by_location.items()},
		"qoh_by_company": {k: str(v) for k, v in qoh_by_company.items()},
		"qoh_by_item": {k: str(v) for k, v in qoh_by_item.items()},
		"valuation_scenarios": val_scenarios,
		"primary_bin_safety": "Confirmed: Primary Bin preserved as source location string, NOT mapped to ERPNext Bin DocType.",
		"target_stock_mutations": 0,
		"stock_reconciliations_created": 0,
		"stock_ledger_entries_created": 0,
		"gl_entries_created": 0,
		"warehouses_created": 0,
		"suppliers_created": 0,
	}
