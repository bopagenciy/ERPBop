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
	Surfaces snapshot classifications: new, unchanged, changed, and missing entities.
	Read-only report only; never attempts writes against source ERP.
	"""
	run_doc = frappe.get_doc("Migration Run", run_id)
	entity_types = ["CUSTOMER", "VENDOR", "ITEM", "WAREHOUSE"]

	# Find most recent prior run for the same source system & instance
	prior_run = frappe.db.sql(
		"""
		SELECT name FROM `tabMigration Run`
		WHERE source_system = %s AND source_instance_id = %s
		  AND name != %s AND status IN ('STAGED', 'READY', 'COMPLETED', 'RECONCILING', 'IMPORTING')
		ORDER BY creation DESC LIMIT 1
		""",
		(run_doc.source_system, run_doc.source_instance_id, run_id),
		as_dict=True,
	)
	prior_run_id = prior_run[0].name if prior_run else None

	report = {
		"run_id": run_id,
		"source_system": run_doc.source_system,
		"source_instance_id": run_doc.source_instance_id,
		"company": run_doc.company,
		"status": run_doc.status,
		"prior_run_id": prior_run_id,
		"by_entity_type": {},
		"discrepancies": {
			"missing_in_target": 0,
			"missing_from_prior_run": 0,
			"unexpected_in_target": 0,
			"identity_conflicts": 0,
			"payload_drift": 0,
			"new_entities": 0,
			"unchanged_entities": 0,
			"changed_entities": 0,
		},
	}

	for e_type in entity_types:
		staged_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type})
		new_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "snapshot_state": "NEW"})
		unchanged_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "snapshot_state": "UNCHANGED"})
		changed_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "snapshot_state": "CHANGED"})
		valid_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "validation_status": "VALID"})
		error_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "validation_status": "ERROR"})
		warning_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "validation_status": "WARNING"})
		imported_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "import_status": "IMPORTED"})
		skipped_count = frappe.db.count("Migration Staging Row", {"migration_run": run_id, "entity_type": e_type, "import_status": "SKIPPED"})

		# Source count from metadata or fallback to staged
		src_count = (source_metadata or {}).get(f"{e_type.lower()}_count", staged_count)

		missing_in_target = max(0, valid_count - imported_count)

		# Check for entities present in prior run but missing in this run
		missing_from_prior = 0
		if prior_run_id:
			missing_res = frappe.db.sql(
				"""
				SELECT COUNT(DISTINCT staging_identity_key) as cnt
				FROM `tabMigration Staging Row`
				WHERE migration_run = %s AND entity_type = %s
				  AND staging_identity_key NOT IN (
					SELECT staging_identity_key FROM `tabMigration Staging Row` WHERE migration_run = %s AND entity_type = %s
				  )
				""",
				(prior_run_id, e_type, run_id, e_type),
				as_dict=True,
			)
			missing_from_prior = missing_res[0].cnt if missing_res else 0

		report["by_entity_type"][e_type] = {
			"source_count": src_count,
			"staged_count": staged_count,
			"new_count": new_count,
			"unchanged_count": unchanged_count,
			"changed_count": changed_count,
			"valid_count": valid_count,
			"warning_count": warning_count,
			"error_count": error_count,
			"imported_count": imported_count,
			"skipped_count": skipped_count,
			"missing_in_target": missing_in_target,
			"missing_from_prior_run": missing_from_prior,
		}

	# Check for identity conflicts (same identity staged multiple times with differing payloads within this run)
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

	report["summary"] = {
		"total_staged": sum(d["staged_count"] for d in report["by_entity_type"].values()),
		"total_new": sum(d["new_count"] for d in report["by_entity_type"].values()),
		"total_unchanged": sum(d["unchanged_count"] for d in report["by_entity_type"].values()),
		"total_changed": sum(d["changed_count"] for d in report["by_entity_type"].values()),
		"total_valid": sum(d["valid_count"] for d in report["by_entity_type"].values()),
		"total_warning": sum(d["warning_count"] for d in report["by_entity_type"].values()),
		"total_error": sum(d["error_count"] for d in report["by_entity_type"].values()),
		"total_imported": sum(d["imported_count"] for d in report["by_entity_type"].values()),
		"total_missing_in_target": sum(d["missing_in_target"] for d in report["by_entity_type"].values()),
		"total_missing_from_prior_run": sum(d["missing_from_prior_run"] for d in report["by_entity_type"].values()),
	}

	report["discrepancies"] = {
		"missing_in_target": report["summary"]["total_missing_in_target"],
		"missing_from_prior_run": report["summary"]["total_missing_from_prior_run"],
		"new_entities": report["summary"]["total_new"],
		"unchanged_entities": report["summary"]["total_unchanged"],
		"changed_entities": report["summary"]["total_changed"],
		"identity_conflicts": 0,
		"payload_drift": len(drift_count),
	}

	return report
