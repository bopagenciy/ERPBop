# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from collections import Counter
from typing import Any, Dict, List, Optional, Set

from bop_erp.migration.datasets.aggregation import CanonicalSourceItem
from bop_erp.migration.datasets.profiles import SourceDatasetProfile


def generate_quality_reconciliation_report(
	received_profiles: List[SourceDatasetProfile],
	expected_profiles: List[SourceDatasetProfile],
	canonical_items: Dict[str, CanonicalSourceItem],
	deferred_dependencies: Optional[List[str]] = None,
	duplicate_identities: Optional[List[str]] = None,
	invalid_row_count: int = 0,
) -> Dict[str, Any]:
	"""
	Generates an offline quality & reconciliation report summarizing ingestion findings,
	entity completeness, gaps, and readiness.
	"""
	received_ids = {p.profile_id for p in received_profiles}
	expected_ids = {p.profile_id for p in expected_profiles}
	missing_ids = expected_ids - received_ids

	total_items = len(canonical_items)
	complete_count = 0
	partial_count = 0
	not_stocked_count = 0
	missing_location_count = 0
	missing_uom_count = 0
	missing_supplier_count = 0
	missing_description_count = 0
	invalid_item_count = 0

	drilldown_partial: List[Dict[str, Any]] = []

	for itm in canonical_items.values():
		st = itm.completeness_status
		if st == "COMPLETE":
			complete_count += 1
		elif st == "NOT_STOCKED":
			not_stocked_count += 1
			partial_count += 1
			drilldown_partial.append({"item_id": itm.item_id, "status": st, "reason": "No location records"})
		elif st == "MISSING_LOCATION":
			missing_location_count += 1
			partial_count += 1
			drilldown_partial.append({"item_id": itm.item_id, "status": st, "reason": "No location records"})
		elif st == "MISSING_UOM":
			missing_uom_count += 1
			partial_count += 1
			drilldown_partial.append({"item_id": itm.item_id, "status": st, "reason": "No UOM records"})
		elif st == "MISSING_SUPPLIER":
			missing_supplier_count += 1
			partial_count += 1
			drilldown_partial.append({"item_id": itm.item_id, "status": st, "reason": "No supplier records"})
		elif st == "MISSING_DESCRIPTION":
			missing_description_count += 1
			partial_count += 1
			drilldown_partial.append({"item_id": itm.item_id, "status": st, "reason": "No extended description"})
		elif st == "PARTIAL":
			partial_count += 1
			reasons = []
			if not itm.locations:
				reasons.append("locations")
			if not itm.uoms:
				reasons.append("uoms")
			if not itm.suppliers:
				reasons.append("suppliers")
			if not itm.descriptions:
				reasons.append("descriptions")
			drilldown_partial.append({"item_id": itm.item_id, "status": st, "missing": reasons})
		elif st == "INVALID":
			invalid_item_count += 1

	ready_rows = complete_count
	review_required_rows = partial_count + invalid_item_count

	report: Dict[str, Any] = {
		"datasets_received": sorted(list(received_ids)),
		"datasets_missing": sorted(list(missing_ids)),
		"total_items": total_items,
		"complete_items": complete_count,
		"partial_items": partial_count,
		"items_not_stocked": not_stocked_count,
		"items_without_location": missing_location_count + not_stocked_count,
		"items_without_uom": missing_uom_count,
		"items_without_supplier": missing_supplier_count,
		"items_without_description": missing_description_count,
		"invalid_items": invalid_item_count,
		"invalid_rows": invalid_row_count,
		"duplicate_source_identities": duplicate_identities or [],
		"deferred_dependencies": deferred_dependencies or [],
		"ready_items": ready_rows,
		"review_required_items": review_required_rows,
		"drilldown_findings": drilldown_partial[:50],  # Bounded sample for review
	}
	return report
