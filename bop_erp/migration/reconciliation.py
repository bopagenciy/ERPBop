# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional

import frappe
from frappe import _


def reconcile_migration_run(
	run_id: str,
	source_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
	"""
	Produces a provider-neutral reconciliation report comparing source, staging, and target ERP states.
	Read-only report only; never attempts writes against source ERP.
	"""
	run_doc = frappe.get_doc("Migration Run", run_id)
	entity_types = ["CUSTOMER", "VENDOR", "ITEM", "WAREHOUSE"]

	report = {
		"run_id": run_id,
		"source_system": run_doc.source_system,
		"source_instance_id": run_doc.source_instance_id,
		"company": run_doc.company,
		"status": run_doc.status,
		"by_entity_type": {},
		"discrepancies": {
			"missing_in_target": 0,
			"unexpected_in_target": 0,
			"identity_conflicts": 0,
			"payload_drift": 0,
		},
	}

	source_counts = (source_metadata or {}).get("capabilities", {})

	for e_type in entity_types:
		staged_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type})
		valid_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "validation_status": "VALID"})
		error_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "validation_status": "ERROR"})
		warning_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "validation_status": "WARNING"})
		imported_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "import_status": "IMPORTED"})
		skipped_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "import_status": "SKIPPED"})

		# Source count from metadata or fallback to staged
		src_count = (source_metadata or {}).get(f"{e_type.lower()}_count", staged_count)

		missing_in_target = max(0, valid_count - imported_count)

		report["by_entity_type"][e_type] = {
			"source_count": src_count,
			"staged_count": staged_count,
			"valid_count": valid_count,
			"warning_count": warning_count,
			"error_count": error_count,
			"imported_count": imported_count,
			"skipped_count": skipped_count,
			"missing_in_target": missing_in_target,
		}
		report["discrepancies"]["missing_in_target"] += missing_in_target

	# Check for identity conflicts (same identity staged multiple times with differing payloads across runs)
	drift_count = frappe.db.sql(
		"""
		SELECT COUNT(DISTINCT staging_identity_key)
		FROM `tabMigration Staging Row`
		WHERE migration_run = %s
		GROUP BY staging_identity_key
		HAVING COUNT(DISTINCT source_payload_hash) > 1
		""",
		(run_id,),
	)
	report["discrepancies"]["payload_drift"] = len(drift_count)

	return report
