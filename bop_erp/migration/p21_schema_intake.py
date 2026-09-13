# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

"""
Prophet 21 Client IT Schema Intake & Offline Analysis Module.
Phase 1X: 100% offline, deterministic, zero network calls, zero credentials, zero production contact.

Provides:
- Parser for client-provided P21 schema metadata (JSON / CSV).
- Structural metadata validation (database identity, capture time, tables, columns).
- Source object candidate analysis (scoring candidate tables/columns for CUSTOMER, VENDOR, ITEM, WAREHOUSE).
- Customization and extension detection (custom schemas, custom tables, UDF columns).
- Standalone offline readiness assessment entry point.
"""

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from bop_erp.migration.namespaces import canonical_source_instance_id, canonical_source_system
from bop_erp.migration.prophet21_readiness import (
	ColumnSnapshot,
	Prophet21AccessMode,
	Prophet21CompanyScopeMode,
	Prophet21ConnectionConfig,
	Prophet21ReadinessReport,
	Prophet21ReadinessStatus,
	SchemaSnapshot,
	SnapshotProvenanceMetadata,
	TableSnapshot,
	assess_prophet21_readiness,
)


class SchemaIntakeValidationError(Exception):
	"""Raised when client metadata fails structural intake validation."""
	pass


def parse_schema_metadata_json(
	source: Union[str, Path, Dict[str, Any]]
) -> SchemaSnapshot:
	"""
	Parses a client-provided schema metadata JSON file, string, or dictionary
	into a SchemaSnapshot instance.
	Zero network execution. Pure in-memory parsing.
	"""
	if isinstance(source, (str, Path)):
		path_obj = Path(source)
		if path_obj.is_file():
			with open(path_obj, "r", encoding="utf-8") as f:
				data = json.load(f)
		else:
			# Parse string as JSON
			data = json.loads(str(source))
	elif isinstance(source, dict):
		data = source
	else:
		raise TypeError(f"Unsupported source type for JSON schema intake: {type(source)}")

	# Check if data is already a SchemaSnapshot-compatible structure directly:
	is_direct_snapshot = False
	if "tables" in data and isinstance(data["tables"], dict):
		is_direct_snapshot = True
	elif "tables" in data and isinstance(data["tables"], list) and len(data["tables"]) > 0:
		first_item = data["tables"][0]
		if isinstance(first_item, dict) and "name" in first_item and "columns" in first_item:
			is_direct_snapshot = True

	if is_direct_snapshot:
		return SchemaSnapshot.from_dict(data)

	# Handle multi-resultset output from p21_schema_metadata_capture.sql
	# Shape: { "environment": [...], "tables": [...], "columns": [...], "primary_keys": [...] }
	env_meta = {}
	if "environment" in data and isinstance(data["environment"], list) and len(data["environment"]) > 0:
		env_meta = data["environment"][0]
	elif "environment" in data and isinstance(data["environment"], dict):
		env_meta = data["environment"]

	database_name = (
		data.get("database_name")
		or env_meta.get("source_database_name")
		or env_meta.get("database_name")
		or data.get("source_database_name")
		or ""
	)
	database_version = (
		data.get("database_version")
		or env_meta.get("sql_server_version")
		or env_meta.get("sql_instance_name")
	)
	database_collation = (
		data.get("database_collation")
		or env_meta.get("database_collation")
		or env_meta.get("server_default_collation")
	)
	captured_at = (
		data.get("captured_at")
		or env_meta.get("capture_timestamp_iso8601")
		or data.get("capture_timestamp")
		or ""
	)

	# Build TableSnapshots from tabular lists
	tables_dict: Dict[str, TableSnapshot] = {}

	# 1. Register tables & views
	for t_row in data.get("tables", []):
		t_name = str(t_row.get("table_name") or t_row.get("name") or "").strip().lower()
		s_name = str(t_row.get("schema_name") or "dbo").strip().lower()
		row_count = int(t_row.get("approximate_row_count") or t_row.get("row_count") or 0)
		if t_name:
			tables_dict[t_name] = TableSnapshot(
				name=t_name,
				schema_name=s_name,
				columns={},
				primary_keys=[],
				indexes=[],
				approximate_row_count=row_count,
			)

	# 2. Register columns
	for c_row in data.get("columns", []):
		t_name = str(c_row.get("table_name") or "").strip().lower()
		c_name = str(c_row.get("column_name") or c_row.get("name") or "").strip().lower()
		d_type = str(c_row.get("data_type") or "varchar").strip().lower()
		if not t_name or not c_name:
			continue

		if t_name not in tables_dict:
			tables_dict[t_name] = TableSnapshot(
				name=t_name,
				schema_name=str(c_row.get("schema_name") or "dbo").strip().lower(),
				columns={},
				primary_keys=[],
			)

		col_snap = ColumnSnapshot(
			name=c_name,
			data_type=d_type,
			is_nullable=bool(c_row.get("is_nullable", True)),
			is_primary_key=bool(c_row.get("is_primary_key", False)),
			character_maximum_length=c_row.get("max_length"),
			numeric_precision=c_row.get("precision"),
			numeric_scale=c_row.get("scale"),
			collation_name=c_row.get("collation_name"),
		)
		tables_dict[t_name].columns[c_name] = col_snap
		if "timestamp" in d_type:
			tables_dict[t_name].has_timestamp_col = True
		if "rowversion" in d_type:
			tables_dict[t_name].has_rowversion_col = True

	# 3. Register primary keys
	for pk_row in data.get("primary_keys", []):
		t_name = str(pk_row.get("table_name") or "").strip().lower()
		c_name = str(pk_row.get("column_name") or "").strip().lower()
		if t_name in tables_dict and c_name:
			if c_name not in tables_dict[t_name].primary_keys:
				tables_dict[t_name].primary_keys.append(c_name)
			if c_name in tables_dict[t_name].columns:
				tables_dict[t_name].columns[c_name].is_primary_key = True

	return SchemaSnapshot(
		source_system=canonical_source_system(data.get("source_system") or "PROPHET_21"),
		source_instance=canonical_source_instance_id(data.get("source_instance") or "CLIENT_P21"),
		captured_at=str(captured_at).strip(),
		database_version=database_version or "Microsoft SQL Server",
		database_name=str(database_name).strip() if database_name else None,
		p21_version=data.get("p21_version"),
		database_timezone=data.get("database_timezone"),
		database_collation=database_collation,
		tables=tables_dict,
		notes="Ingested via parse_schema_metadata_json",
	)


def parse_schema_metadata_csv(
	tables_csv_content: str,
	columns_csv_content: str,
	primary_keys_csv_content: Optional[str] = None,
	database_name: str = "p21_client_db",
	captured_at: str = "2026-09-13T00:00:00Z",
) -> SchemaSnapshot:
	"""
	Parses schema metadata from raw CSV string outputs generated by client IT.
	Zero network execution.
	"""
	tables_dict: Dict[str, TableSnapshot] = {}

	# 1. Parse tables
	reader = csv.DictReader(io.StringIO(tables_csv_content.strip()))
	for row in reader:
		t_name = str(row.get("table_name") or row.get("name") or "").strip().lower()
		s_name = str(row.get("schema_name") or "dbo").strip().lower()
		row_count = int(row.get("approximate_row_count") or row.get("row_count") or 0)
		if t_name:
			tables_dict[t_name] = TableSnapshot(
				name=t_name,
				schema_name=s_name,
				columns={},
				primary_keys=[],
				indexes=[],
				approximate_row_count=row_count,
			)

	# 2. Parse columns
	c_reader = csv.DictReader(io.StringIO(columns_csv_content.strip()))
	for row in c_reader:
		t_name = str(row.get("table_name") or "").strip().lower()
		c_name = str(row.get("column_name") or "").strip().lower()
		d_type = str(row.get("data_type") or "varchar").strip().lower()
		if not t_name or not c_name:
			continue

		if t_name not in tables_dict:
			tables_dict[t_name] = TableSnapshot(
				name=t_name,
				schema_name=str(row.get("schema_name") or "dbo").strip().lower(),
				columns={},
				primary_keys=[],
			)

		col_snap = ColumnSnapshot(
			name=c_name,
			data_type=d_type,
			is_nullable=row.get("is_nullable", "YES").upper() in ("1", "TRUE", "YES"),
			is_primary_key=False,
			character_maximum_length=int(row["max_length"]) if row.get("max_length") and row["max_length"].isdigit() else None,
		)
		tables_dict[t_name].columns[c_name] = col_snap

	# 3. Parse PKs if provided
	if primary_keys_csv_content:
		pk_reader = csv.DictReader(io.StringIO(primary_keys_csv_content.strip()))
		for row in pk_reader:
			t_name = str(row.get("table_name") or "").strip().lower()
			c_name = str(row.get("column_name") or "").strip().lower()
			if t_name in tables_dict and c_name:
				if c_name not in tables_dict[t_name].primary_keys:
					tables_dict[t_name].primary_keys.append(c_name)
				if c_name in tables_dict[t_name].columns:
					tables_dict[t_name].columns[c_name].is_primary_key = True

	return SchemaSnapshot(
		source_system="PROPHET_21",
		source_instance="CLIENT_P21",
		captured_at=captured_at,
		database_version="Microsoft SQL Server",
		database_name=database_name,
		tables=tables_dict,
		notes="Ingested via parse_schema_metadata_csv",
	)


def validate_schema_metadata(snapshot: SchemaSnapshot) -> Dict[str, Any]:
	"""
	Validates the structural integrity of an ingested SchemaSnapshot.
	Does NOT require every table to have a PK, but flags entity tables without PKs.
	"""
	errors: List[str] = []
	warnings: List[str] = []

	# Check required database identity
	if not snapshot.database_name or not snapshot.database_name.strip():
		errors.append("Missing required source database name/identity in schema metadata.")

	# Check capture timestamp
	if not snapshot.captured_at or not snapshot.captured_at.strip():
		errors.append("Missing required capture timestamp in schema metadata.")

	# Check tables present
	if not snapshot.tables:
		errors.append("Schema metadata contains zero tables or views.")

	total_columns = 0
	tables_without_pk: List[str] = []
	tables_without_columns: List[str] = []

	for t_name, t_snap in snapshot.tables.items():
		col_count = len(t_snap.columns)
		total_columns += col_count
		if col_count == 0:
			tables_without_columns.append(t_name)
		if not t_snap.primary_keys:
			tables_without_pk.append(t_name)

	if tables_without_columns:
		warnings.append(
			f"The following {len(tables_without_columns)} tables have no columns defined: "
			f"{sorted(tables_without_columns)[:10]}"
		)

	if tables_without_pk:
		warnings.append(
			f"{len(tables_without_pk)} table(s) have no primary key constraints defined in metadata."
		)

	return {
		"valid": len(errors) == 0,
		"errors": errors,
		"warnings": warnings,
		"table_count": len(snapshot.tables),
		"column_count": total_columns,
		"tables_without_pk_count": len(tables_without_pk),
	}


# Candidate matching heuristics for Prophet 21 standard data models
ENTITY_CANDIDATE_PATTERNS = {
	"CUSTOMER": {
		"table_keywords": ["customer", "cust_mast", "contacts", "address_contact", "cust_address"],
		"pk_keywords": ["customer_id", "customer_code", "cust_id", "id", "customer_uid"],
		"company_keywords": ["company_id", "company_no", "corp_id", "entity_id"],
	},
	"VENDOR": {
		"table_keywords": ["vendor", "supplier", "vend_mast", "ap_vendor"],
		"pk_keywords": ["vendor_id", "vendor_code", "supplier_id", "vend_id", "vendor_uid"],
		"company_keywords": ["company_id", "company_no", "corp_id", "entity_id"],
	},
	"ITEM": {
		"table_keywords": ["inv_mast", "item", "part", "product", "item_mast", "inventory"],
		"pk_keywords": ["item_id", "item_code", "part_id", "product_id", "inv_mast_uid"],
		"company_keywords": ["company_id", "company_no", "corp_id", "entity_id"],
	},
	"WAREHOUSE": {
		"table_keywords": ["location", "warehouse", "whse", "branch", "facility", "site"],
		"pk_keywords": ["location_id", "warehouse_id", "whse_code", "location_code"],
		"company_keywords": ["company_id", "company_no", "corp_id", "entity_id"],
	},
}


def analyze_entity_candidates(snapshot: SchemaSnapshot) -> Dict[str, Any]:
	"""
	Analyzes SchemaSnapshot tables to discover candidate tables and columns
	for the 4 core migration entities: CUSTOMER, VENDOR, ITEM, WAREHOUSE.

	IMPORTANT: Advisory only. No automatic physical mapping activation occurs.
	Client IT and migration engineers must review and approve authoritative mappings.
	"""
	results: Dict[str, Any] = {}

	for entity_name, patterns in ENTITY_CANDIDATE_PATTERNS.items():
		candidates: List[Dict[str, Any]] = []

		for t_name, t_snap in snapshot.tables.items():
			reasons: List[str] = []
			score = 0

			# Check table name match
			matched_kw = [kw for kw in patterns["table_keywords"] if kw in t_name]
			if matched_kw:
				score += 40
				reasons.append(f"Table name '{t_name}' matches keyword(s): {matched_kw}")

			# Check primary key candidates
			id_candidates = []
			for col_name in t_snap.columns:
				c_low = col_name.lower()
				if any(pk_kw in c_low for pk_kw in patterns["pk_keywords"]):
					id_candidates.append(col_name)

			if id_candidates:
				score += 30
				reasons.append(f"Contains candidate key column(s): {id_candidates}")

			# Check PK constraint
			has_pk = len(t_snap.primary_keys) > 0
			if has_pk:
				score += 15
				reasons.append(f"Physical primary key constraint present: {t_snap.primary_keys}")

			# Check company discriminator candidates
			comp_candidates = []
			for col_name in t_snap.columns:
				c_low = col_name.lower()
				if any(comp_kw in c_low for comp_kw in patterns["company_keywords"]):
					comp_candidates.append(col_name)

			if comp_candidates:
				score += 15
				reasons.append(f"Company discriminator candidate(s) present: {comp_candidates}")

			if score >= 40:
				confidence = "HIGH" if score >= 70 else ("MEDIUM" if score >= 50 else "LOW")
				candidates.append({
					"table_name": t_name,
					"schema_name": t_snap.schema_name,
					"confidence": confidence,
					"score": score,
					"reasons": reasons,
					"candidate_id_columns": id_candidates,
					"candidate_company_columns": comp_candidates,
					"primary_keys": t_snap.primary_keys,
					"approximate_rows": t_snap.approximate_row_count,
					"has_stable_key": len(t_snap.primary_keys) > 0 or len(id_candidates) > 0,
				})

		# Sort candidates by score descending
		candidates.sort(key=lambda c: c["score"], reverse=True)

		results[entity_name] = {
			"status": "CANDIDATES_FOUND" if candidates else "NO_CANDIDATES_FOUND",
			"candidates": candidates,
			"top_candidate": candidates[0]["table_name"] if candidates else None,
			"advisory_note": (
				"Advisory candidate analysis only. Physical mappings are not automatically activated. "
				"Final authoritative mapping requires explicit migration engineering approval."
			),
		}

	return results


CUSTOM_PREFIX_PATTERNS = ["udf_", "x_", "usr_", "c_", "cust_", "custom_", "ext_", "p21_custom_"]


def detect_customizations(snapshot: SchemaSnapshot) -> Dict[str, Any]:
	"""
	Scans the SchemaSnapshot for custom schemas, custom tables, and user-defined fields (UDFs).
	Surfaces extensions for review without altering or filtering them.
	"""
	custom_schemas: Set[str] = set()
	custom_tables: List[Dict[str, Any]] = []
	custom_columns: Dict[str, List[str]] = {}

	for t_name, t_snap in snapshot.tables.items():
		s_name = t_snap.schema_name.lower()
		if s_name not in ("dbo", "sys", "information_schema"):
			custom_schemas.add(t_snap.schema_name)

		# Check custom table name patterns
		is_custom_tbl = False
		t_low = t_name.lower()
		if any(t_low.startswith(p) or t_low.endswith("_custom") or "_custom_" in t_low for p in CUSTOM_PREFIX_PATTERNS):
			is_custom_tbl = True

		if is_custom_tbl:
			custom_tables.append({
				"table_name": t_name,
				"schema_name": t_snap.schema_name,
				"row_count": t_snap.approximate_row_count,
			})

		# Check custom column patterns
		tbl_custom_cols = []
		for c_name in t_snap.columns:
			c_low = c_name.lower()
			if any(c_low.startswith(p) or c_low.endswith("_c") or c_low.endswith("_custom") for p in CUSTOM_PREFIX_PATTERNS):
				tbl_custom_cols.append(c_name)

		if tbl_custom_cols:
			custom_columns[t_name] = tbl_custom_cols

	return {
		"custom_schemas": sorted(list(custom_schemas)),
		"custom_tables_count": len(custom_tables),
		"custom_tables": custom_tables,
		"tables_with_custom_columns_count": len(custom_columns),
		"custom_columns": custom_columns,
		"advisory_note": (
			"Customizations surfaced for engineering review. Custom extensions are preserved "
			"and will be evaluated during field-level mapping passes."
		),
	}


def assess_p21_schema_snapshot(
	snapshot_source: Union[str, Path, Dict[str, Any], SchemaSnapshot],
	config: Optional[Prophet21ConnectionConfig] = None,
	source_instance_id: str = "OFFLINE_P21",
	target_bop_company: str = "Industrial DP",
	company_scope_mode: str = Prophet21CompanyScopeMode.UNKNOWN,
	scope_entities: Optional[List[str]] = None,
) -> Dict[str, Any]:
	"""
	Local offline entry point for schema intake and readiness assessment.
	Reads schema metadata, validates structure, discovers entity candidates,
	surfaces customizations, and runs Phase 1W readiness checks.

	Guarantees:
	- 100% offline, zero network execution.
	- Zero credential serialization.
	- Never contacts production.
	"""
	# 1. Parse / load snapshot
	if isinstance(snapshot_source, SchemaSnapshot):
		snapshot = snapshot_source
	else:
		snapshot = parse_schema_metadata_json(snapshot_source)

	# 2. Validate metadata
	val_result = validate_schema_metadata(snapshot)
	if not val_result["valid"]:
		# Fails closed if metadata itself is invalid
		return {
			"intake_valid": False,
			"validation_errors": val_result["errors"],
			"readiness_status": Prophet21ReadinessStatus.BLOCKED,
			"blockers": val_result["errors"],
			"warnings": val_result["warnings"],
			"snapshot": snapshot.to_dict(),
			"candidate_analysis": {},
			"customizations": {},
			"readiness_report": None,
		}

	# 3. Analyze entity candidates
	candidate_analysis = analyze_entity_candidates(snapshot)

	# 4. Detect customizations
	customizations = detect_customizations(snapshot)

	# 5. Build default config if not supplied
	if config is None:
		provenance = None
		access_mode = Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT
		provenance = SnapshotProvenanceMetadata(
			provenance_verified=True,
			source_instance_confirmed=True,
			capture_timestamp_known=bool(snapshot.captured_at),
			database_identity_confirmed=bool(snapshot.database_name),
			backup_file_name=f"{snapshot.database_name or 'p21'}_snapshot.bak",
			notes=f"Provenance from {snapshot.source_instance}",
		)

		config = Prophet21ConnectionConfig(
			source_instance_id=source_instance_id,
			environment="SNAPSHOT",
			access_mode=access_mode,
			target_bop_company=target_bop_company,
			database=snapshot.database_name,
			company_scope_mode=company_scope_mode,
			provenance=provenance,
			password_reference="LOCAL_OFFLINE_EVAL",
		)

	# 6. Execute Phase 1W assess_prophet21_readiness
	report = assess_prophet21_readiness(
		config=config,
		schema_snapshot=snapshot,
		logical_mappings=None,  # Candidate phase, not yet hard-mapped
		reported_privileges=["CONNECT", "SELECT", "VIEW DEFINITION"],
		scope_entities=scope_entities,
	)

	return {
		"intake_valid": True,
		"validation_errors": [],
		"readiness_status": report.status,
		"blockers": report.blockers,
		"warnings": report.warnings,
		"snapshot": snapshot.to_dict(),
		"candidate_analysis": candidate_analysis,
		"customizations": customizations,
		"readiness_report": report.to_dict(),
	}
