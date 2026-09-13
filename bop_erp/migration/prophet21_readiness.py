# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from bop_erp.migration.exceptions import (
	ProductionSourceBlockedError,
	SourceSafetyViolationError,
)
from bop_erp.migration.namespaces import (
	canonical_source_instance_id,
	canonical_source_system,
)
from bop_erp.safety import is_forbidden_production_host

# -------------------------------------------------------------------------
# B — Supported Real Access Modes
# -------------------------------------------------------------------------
class Prophet21AccessMode:
	SQL_SERVER_READONLY = "SQL_SERVER_READONLY"
	ODBC_READONLY = "ODBC_READONLY"
	RESTORED_DATABASE_SNAPSHOT = "RESTORED_DATABASE_SNAPSHOT"
	DATABASE_BACKUP_RESTORE = "DATABASE_BACKUP_RESTORE"
	SANITIZED_DATABASE_COPY = "SANITIZED_DATABASE_COPY"
	CSV_EXPORT = "CSV_EXPORT"
	API_READONLY = "API_READONLY"
	OTHER_READONLY = "OTHER_READONLY"

	ALL = {
		SQL_SERVER_READONLY,
		ODBC_READONLY,
		RESTORED_DATABASE_SNAPSHOT,
		DATABASE_BACKUP_RESTORE,
		SANITIZED_DATABASE_COPY,
		CSV_EXPORT,
		API_READONLY,
		OTHER_READONLY,
	}

	# C — Preferred access order (highest to lowest safety/recommendation)
	PREFERRED_ORDER = [
		RESTORED_DATABASE_SNAPSHOT,
		SANITIZED_DATABASE_COPY,
		DATABASE_BACKUP_RESTORE,
		SQL_SERVER_READONLY,
		ODBC_READONLY,
		API_READONLY,
		CSV_EXPORT,
		OTHER_READONLY,
	]


ACCESS_MODE_EVALUATIONS: Dict[str, Dict[str, Any]] = {
	Prophet21AccessMode.RESTORED_DATABASE_SNAPSHOT: {
		"safety": "MAXIMUM",
		"fidelity": "EXACT",
		"repeatability": "PERFECT",
		"performance": "HIGH",
		"schema_visibility": "COMPLETE",
		"cutover_suitability": "EXCELLENT",
		"delta_extraction_suitability": "MODERATE",
		"operational_risk": "ZERO_PRODUCTION_RISK",
		"description": "Read-only snapshot restored in isolated environment. Safest and highest fidelity.",
	},
	Prophet21AccessMode.SANITIZED_DATABASE_COPY: {
		"safety": "MAXIMUM",
		"fidelity": "HIGH",
		"repeatability": "HIGH",
		"performance": "HIGH",
		"schema_visibility": "COMPLETE",
		"cutover_suitability": "EXCELLENT",
		"delta_extraction_suitability": "MODERATE",
		"operational_risk": "ZERO_PRODUCTION_RISK",
		"description": "Full copy with PII/secrets redacted/sanitized by IT. Isolated infrastructure.",
	},
	Prophet21AccessMode.DATABASE_BACKUP_RESTORE: {
		"safety": "MAXIMUM",
		"fidelity": "EXACT",
		"repeatability": "PERFECT",
		"performance": "HIGH",
		"schema_visibility": "COMPLETE",
		"cutover_suitability": "HIGH",
		"delta_extraction_suitability": "MODERATE",
		"operational_risk": "ZERO_PRODUCTION_RISK",
		"description": "Standard .bak restore into isolated secondary SQL instance.",
	},
	Prophet21AccessMode.SQL_SERVER_READONLY: {
		"safety": "HIGH",
		"fidelity": "EXACT",
		"repeatability": "HIGH",
		"performance": "HIGH",
		"schema_visibility": "COMPLETE",
		"cutover_suitability": "HIGH",
		"delta_extraction_suitability": "HIGH",
		"operational_risk": "LOW_TO_MODERATE",
		"description": "Dedicated read-only SQL Server account. Requires strict IT privilege gating and replica/reporting tier.",
	},
	Prophet21AccessMode.ODBC_READONLY: {
		"safety": "HIGH",
		"fidelity": "EXACT",
		"repeatability": "HIGH",
		"performance": "MODERATE",
		"schema_visibility": "COMPLETE",
		"cutover_suitability": "HIGH",
		"delta_extraction_suitability": "HIGH",
		"operational_risk": "LOW_TO_MODERATE",
		"description": "Standard ODBC DSN with read-only credentials. Suitable across container/Linux bridge.",
	},
	Prophet21AccessMode.API_READONLY: {
		"safety": "HIGH",
		"fidelity": "MODERATE",
		"repeatability": "MODERATE",
		"performance": "MODERATE",
		"schema_visibility": "RESTRICTED",
		"cutover_suitability": "MODERATE",
		"delta_extraction_suitability": "HIGH",
		"operational_risk": "LOW",
		"description": "Prophet 21 middleware/Web Services API. Schema obscured behind API contracts.",
	},
	Prophet21AccessMode.CSV_EXPORT: {
		"safety": "MAXIMUM",
		"fidelity": "MODERATE",
		"repeatability": "LOW",
		"performance": "HIGH",
		"schema_visibility": "PARTIAL",
		"cutover_suitability": "LOW",
		"delta_extraction_suitability": "POOR",
		"operational_risk": "ZERO_PRODUCTION_RISK",
		"description": "Tabular export. Lacks relational integrity guarantees and requires manual export cycles.",
	},
	Prophet21AccessMode.OTHER_READONLY: {
		"safety": "UNKNOWN",
		"fidelity": "UNKNOWN",
		"repeatability": "UNKNOWN",
		"performance": "UNKNOWN",
		"schema_visibility": "UNKNOWN",
		"cutover_suitability": "LOW",
		"delta_extraction_suitability": "LOW",
		"operational_risk": "REVIEW_REQUIRED",
		"description": "Unclassified access mode requiring security and architectural review.",
	},
}


# -------------------------------------------------------------------------
# P — Read-Only Connection Config Model
# -------------------------------------------------------------------------
@dataclass
class Prophet21ConnectionConfig:
	"""
	Configuration container for future Prophet 21 source connections.
	Enforces zero storage or serialization of raw passwords/secrets.
	Guarantees safe string/logging representation.
	"""

	source_instance_id: str
	environment: str  # SYNTHETIC, SNAPSHOT, REPLICA, PRODUCTION
	access_mode: str  # Member of Prophet21AccessMode.ALL
	host: Optional[str] = None
	port: int = 1433
	database: Optional[str] = None
	driver: str = "ODBC Driver 18 for SQL Server"
	username_reference: Optional[str] = None  # Reference to config/env var, e.g. "P21_RO_USER"
	password_reference: Optional[str] = None  # Reference to Frappe Password / secret manager, e.g. "P21_RO_PWD"
	encrypt: bool = True
	trust_server_certificate: bool = False
	connect_timeout: int = 30
	query_timeout: int = 60
	vpn_required: bool = True
	metadata_notes: Optional[str] = None

	def __post_init__(self):
		self.source_instance_id = canonical_source_instance_id(self.source_instance_id)
		self.environment = str(self.environment).strip().upper()
		self.access_mode = str(self.access_mode).strip().upper()

		# Guard against production host denylist
		if self.host and is_forbidden_production_host(self.host):
			raise SourceSafetyViolationError(
				f"Configured host '{self.host}' matches protected production domain. Connection forbidden."
			)

	def __repr__(self) -> str:
		"""Safe string representation without secrets."""
		return (
			f"<Prophet21ConnectionConfig instance='{self.source_instance_id}' "
			f"mode='{self.access_mode}' env='{self.environment}' "
			f"host='{self.host or '[UNSET]'}' db='{self.database or '[UNSET]'}'>"
		)

	def to_dict(self) -> Dict[str, Any]:
		"""Returns safe dictionary representation. Secret values are never exposed."""
		return {
			"source_instance_id": self.source_instance_id,
			"environment": self.environment,
			"access_mode": self.access_mode,
			"host": self.host,
			"port": self.port,
			"database": self.database,
			"driver": self.driver,
			"username_reference": self.username_reference,
			"password_reference_set": bool(self.password_reference),
			"encrypt": self.encrypt,
			"trust_server_certificate": self.trust_server_certificate,
			"connect_timeout": self.connect_timeout,
			"query_timeout": self.query_timeout,
			"vpn_required": self.vpn_required,
			"metadata_notes": self.metadata_notes,
		}


# -------------------------------------------------------------------------
# F — Real Schema Intake Data Model (p21_schema_snapshot.json schema)
# -------------------------------------------------------------------------
@dataclass
class ColumnSnapshot:
	name: str
	data_type: str
	is_nullable: bool = True
	is_primary_key: bool = False
	character_maximum_length: Optional[int] = None
	numeric_precision: Optional[int] = None
	numeric_scale: Optional[int] = None
	collation_name: Optional[str] = None


@dataclass
class TableSnapshot:
	name: str
	schema_name: str = "dbo"
	columns: Dict[str, ColumnSnapshot] = field(default_factory=dict)
	primary_keys: List[str] = field(default_factory=list)
	indexes: List[str] = field(default_factory=list)
	approximate_row_count: int = 0
	has_timestamp_col: bool = False
	has_rowversion_col: bool = False


@dataclass
class SchemaSnapshot:
	"""
	Normalized, provider-neutral representation of an offline schema intake file
	(e.g., captured from real Prophet 21 via read-only sys/information_schema script).
	Contains ZERO passwords, secrets, or row data.
	"""

	source_system: str
	source_instance: str
	captured_at: str
	database_version: str
	p21_version: Optional[str] = None
	database_timezone: Optional[str] = None
	database_collation: Optional[str] = None
	tables: Dict[str, TableSnapshot] = field(default_factory=dict)
	notes: Optional[str] = None

	@classmethod
	def from_dict(cls, data: Dict[str, Any]) -> "SchemaSnapshot":
		tables = {}
		for tbl_data in data.get("tables", []):
			cols = {}
			for c_data in tbl_data.get("columns", []):
				col_obj = ColumnSnapshot(
					name=c_data["name"],
					data_type=c_data.get("data_type", "varchar"),
					is_nullable=c_data.get("is_nullable", True),
					is_primary_key=c_data.get("is_primary_key", False),
					character_maximum_length=c_data.get("character_maximum_length"),
					numeric_precision=c_data.get("numeric_precision"),
					numeric_scale=c_data.get("numeric_scale"),
					collation_name=c_data.get("collation_name"),
				)
				cols[col_obj.name] = col_obj

			t_name = tbl_data["name"]
			tables[t_name] = TableSnapshot(
				name=t_name,
				schema_name=tbl_data.get("schema_name", "dbo"),
				columns=cols,
				primary_keys=tbl_data.get("primary_keys", []),
				indexes=tbl_data.get("indexes", []),
				approximate_row_count=tbl_data.get("approximate_row_count", 0),
				has_timestamp_col=tbl_data.get("has_timestamp_col", False),
				has_rowversion_col=tbl_data.get("has_rowversion_col", False),
			)

		return cls(
			source_system=canonical_source_system(data.get("source_system", "PROPHET_21")),
			source_instance=canonical_source_instance_id(data.get("source_instance", "MAIN")),
			captured_at=data.get("captured_at", datetime.now(timezone.utc).isoformat()),
			database_version=data.get("database_version", "Unknown"),
			p21_version=data.get("p21_version"),
			database_timezone=data.get("database_timezone"),
			database_collation=data.get("database_collation"),
			tables=tables,
			notes=data.get("notes"),
		)


# -------------------------------------------------------------------------
# G — Logical Entity Requirement Definitions
# -------------------------------------------------------------------------
class FieldRequirementLevel:
	REQUIRED = "REQUIRED"
	OPTIONAL = "OPTIONAL"
	DERIVED = "DERIVED"
	DEFAULTABLE = "DEFAULTABLE"
	REVIEW_REQUIRED_IF_MISSING = "REVIEW_REQUIRED_IF_MISSING"


LOGICAL_ENTITY_REQUIREMENTS: Dict[str, Dict[str, str]] = {
	"CUSTOMER": {
		"source_record_id": FieldRequirementLevel.REQUIRED,
		"name": FieldRequirementLevel.REQUIRED,
		"email": FieldRequirementLevel.OPTIONAL,
		"phone": FieldRequirementLevel.OPTIONAL,
		"tax_id": FieldRequirementLevel.OPTIONAL,
		"currency": FieldRequirementLevel.DEFAULTABLE,
		"payment_terms": FieldRequirementLevel.REVIEW_REQUIRED_IF_MISSING,
		"source_company_id": FieldRequirementLevel.REQUIRED,
	},
	"VENDOR": {
		"source_record_id": FieldRequirementLevel.REQUIRED,
		"name": FieldRequirementLevel.REQUIRED,
		"email": FieldRequirementLevel.OPTIONAL,
		"phone": FieldRequirementLevel.OPTIONAL,
		"tax_id": FieldRequirementLevel.OPTIONAL,
		"currency": FieldRequirementLevel.DEFAULTABLE,
		"payment_terms": FieldRequirementLevel.REVIEW_REQUIRED_IF_MISSING,
		"source_company_id": FieldRequirementLevel.REQUIRED,
	},
	"ITEM": {
		"source_record_id": FieldRequirementLevel.REQUIRED,
		"item_code": FieldRequirementLevel.REQUIRED,
		"name": FieldRequirementLevel.REQUIRED,
		"description": FieldRequirementLevel.OPTIONAL,
		"stock_uom": FieldRequirementLevel.REQUIRED,
		"item_group": FieldRequirementLevel.DEFAULTABLE,
		"is_stock_item": FieldRequirementLevel.DEFAULTABLE,
		"serialized": FieldRequirementLevel.OPTIONAL,
		"batch_tracked": FieldRequirementLevel.OPTIONAL,
		"source_company_id": FieldRequirementLevel.REQUIRED,
	},
	"WAREHOUSE": {
		"source_record_id": FieldRequirementLevel.REQUIRED,
		"warehouse_code": FieldRequirementLevel.REQUIRED,
		"warehouse_name": FieldRequirementLevel.REQUIRED,
		"parent_code": FieldRequirementLevel.OPTIONAL,
		"company": FieldRequirementLevel.REQUIRED,
		"source_company_id": FieldRequirementLevel.REQUIRED,
	},
}


# -------------------------------------------------------------------------
# E — Database Privilege Verification Rules
# -------------------------------------------------------------------------
FORBIDDEN_DB_PRIVILEGES = {
	"INSERT",
	"UPDATE",
	"DELETE",
	"EXECUTE",
	"ALTER",
	"CONTROL",
	"DROP",
	"CREATE",
	"TAKE OWNERSHIP",
	"DB_OWNER",
	"DB_DATAWRITER",
	"SYSADMIN",
	"SECURITYADMIN",
	"SERVERADMIN",
}

PERMITTED_DB_PRIVILEGES = {
	"CONNECT",
	"SELECT",
	"VIEW DEFINITION",
}


def audit_sql_account_privileges(granted_privileges: List[str]) -> Tuple[bool, List[str], List[str]]:
	"""
	Audits a list of effective database privileges for the prospective read-only account.
	Returns (is_compliant, violations, allowed_found).
	"""
	violations = []
	allowed_found = []
	for priv in granted_privileges:
		clean = str(priv).strip().upper()
		if clean in FORBIDDEN_DB_PRIVILEGES:
			violations.append(f"Forbidden write or administrative privilege granted: '{clean}'")
		elif any(f in clean for f in ("WRITE", "ADMIN", "OWNER", "EXEC", "ALTER")):
			violations.append(f"Suspicious privilege pattern granted: '{clean}'")
		else:
			allowed_found.append(clean)

	compliant = len(violations) == 0 and ("SELECT" in allowed_found or "CONNECT" in allowed_found)
	if not ("SELECT" in allowed_found or any("SELECT" in p for p in allowed_found)):
		violations.append("Missing mandatory SELECT permission on source tables/views.")

	return (compliant and len(violations) == 0), violations, allowed_found


# -------------------------------------------------------------------------
# Q & R — Readiness Validation Service & Report
# -------------------------------------------------------------------------
class Prophet21ReadinessStatus:
	READY = "READY"
	PARTIAL = "PARTIAL"
	BLOCKED = "BLOCKED"


@dataclass
class Prophet21ReadinessReport:
	"""
	Structured audit report produced by assess_prophet21_readiness.
	Exportable to JSON without exposing secrets or PII.
	"""

	status: str  # READY, PARTIAL, BLOCKED
	evaluated_at: str
	source_system: str
	source_instance_id: str
	access_mode: str
	access_evaluation: Dict[str, Any]
	blockers: List[str]
	warnings: List[str]
	entity_readiness: Dict[str, Dict[str, Any]]
	company_discriminator_finding: Dict[str, Any]
	data_volume_classification: Dict[str, Any]
	delta_capability_finding: Dict[str, Any]
	timezone_finding: Dict[str, Any]
	numeric_precision_finding: Dict[str, Any]
	text_encoding_finding: Dict[str, Any]
	null_sentinel_policy: Dict[str, Any]
	security_audit: Dict[str, Any]
	next_actions: List[str]

	def to_dict(self) -> Dict[str, Any]:
		return {
			"status": self.status,
			"evaluated_at": self.evaluated_at,
			"source_system": self.source_system,
			"source_instance_id": self.source_instance_id,
			"access_mode": self.access_mode,
			"access_evaluation": self.access_evaluation,
			"blockers": self.blockers,
			"warnings": self.warnings,
			"entity_readiness": self.entity_readiness,
			"company_discriminator_finding": self.company_discriminator_finding,
			"data_volume_classification": self.data_volume_classification,
			"delta_capability_finding": self.delta_capability_finding,
			"timezone_finding": self.timezone_finding,
			"numeric_precision_finding": self.numeric_precision_finding,
			"text_encoding_finding": self.text_encoding_finding,
			"null_sentinel_policy": self.null_sentinel_policy,
			"security_audit": self.security_audit,
			"next_actions": self.next_actions,
		}


def classify_data_volume(total_records: int) -> Dict[str, Any]:
	"""Classifies entity data volume into small/medium/large extraction tiers."""
	if total_records < 10000:
		tier = "SMALL"
		strategy = "Single-batch or standard pagination (page_size=500). Fast baseline snapshot."
	elif total_records < 100000:
		tier = "MEDIUM"
		strategy = "Keyset cursor pagination (page_size=1000). Checkpointed chunk extraction."
	else:
		tier = "LARGE"
		strategy = "Parallel keyset partition extraction with bounded chunk commit."
	return {
		"total_records": total_records,
		"tier": tier,
		"recommended_strategy": strategy,
	}


def assess_prophet21_readiness(
	config: Prophet21ConnectionConfig,
	schema_snapshot: Optional[SchemaSnapshot] = None,
	logical_mappings: Optional[Dict[str, Dict[str, str]]] = None,
	reported_privileges: Optional[List[str]] = None,
) -> Prophet21ReadinessReport:
	"""
	Provider-specific readiness audit service for Prophet 21 migration intake.
	Performs pure offline, deterministic analysis.
	ZERO network calls. ZERO production contact. ZERO credentials evaluated.
	"""
	blockers: List[str] = []
	warnings: List[str] = []
	next_actions: List[str] = []

	# 1. Access Mode Validation
	if config.access_mode not in Prophet21AccessMode.ALL:
		blockers.append(f"Unsupported access mode: '{config.access_mode}'")
	access_eval = ACCESS_MODE_EVALUATIONS.get(
		config.access_mode, {"safety": "UNKNOWN", "operational_risk": "UNSUPPORTED"}
	)

	# 2. Environment Validation
	if config.environment == "PRODUCTION":
		blockers.append(
			"Environment is set to PRODUCTION. Direct connection to production Prophet 21 is strictly forbidden."
		)

	# 3. Security & Privilege Validation
	sec_audit = {"compliant": True, "violations": []}
	if reported_privileges is not None:
		is_comp, priv_violations, allowed = audit_sql_account_privileges(reported_privileges)
		sec_audit["compliant"] = is_comp
		sec_audit["violations"] = priv_violations
		sec_audit["allowed"] = allowed
		if not is_comp:
			for v in priv_violations:
				blockers.append(f"Security privilege blocker: {v}")

	if not config.password_reference and config.access_mode in (
		Prophet21AccessMode.SQL_SERVER_READONLY,
		Prophet21AccessMode.ODBC_READONLY,
	):
		warnings.append("No secure password reference configured (password_reference is unset).")

	# 4. Schema Snapshot Assessment
	entity_readiness = {}
	company_discriminator = {
		"status": "UNVERIFIED",
		"discriminator_column": None,
		"is_company_scoped": False,
	}
	delta_capability = {
		"status": "NONE_DETECTED",
		"candidates": [],
		"fallback_strategy": "Bounded full snapshot comparison with payload SHA256 hashing",
	}
	vol_classification = {"tier": "UNKNOWN", "total_records": 0}
	tz_finding = {"status": "UNKNOWN", "database_timezone": None}
	num_finding = {"decimal_scale_enforced": True, "floating_point_allowed": False}
	text_finding = {"collation": "UNKNOWN", "unicode_preserved": True}

	if not schema_snapshot:
		blockers.append("Schema snapshot not captured or provided. Run read-only schema discovery script.")
		next_actions.append("Capture p21_schema_snapshot.json from read-only replica or staging restore.")
	else:
		# Timezone check
		if schema_snapshot.database_timezone:
			tz_finding["status"] = "SPECIFIED"
			tz_finding["database_timezone"] = schema_snapshot.database_timezone
		else:
			warnings.append("Database timezone not specified in schema snapshot. Timestamps must be treated with caution.")
			tz_finding["status"] = "UNSPECIFIED"

		if schema_snapshot.database_collation:
			text_finding["collation"] = schema_snapshot.database_collation

		# Check for delta candidates (timestamp, rowversion, etc.)
		all_delta_cols = []
		total_rows = 0
		for tbl_name, tbl_meta in schema_snapshot.tables.items():
			total_rows += tbl_meta.approximate_row_count
			if tbl_meta.has_timestamp_col or tbl_meta.has_rowversion_col:
				all_delta_cols.append(tbl_name)
			for col_name, col_meta in tbl_meta.columns.items():
				c_lower = col_name.lower()
				if any(k in c_lower for k in ("last_modified", "date_modified", "rowversion", "change_tracking")):
					delta_capability["candidates"].append(f"{tbl_name}.{col_name}")

		if delta_capability["candidates"]:
			delta_capability["status"] = "CANDIDATES_FOUND"
		else:
			warnings.append(
				"No explicit change tracking or last_modified columns detected. Incremental sync will require snapshot hashing."
			)

		vol_classification = classify_data_volume(total_rows)

		# Evaluate entity mappings against schema snapshot
		mappings = logical_mappings or {}
		for e_type, req_fields in LOGICAL_ENTITY_REQUIREMENTS.items():
			ent_report = {
				"mapped": False,
				"table": None,
				"primary_key": None,
				"missing_required_fields": [],
				"missing_optional_fields": [],
				"status": "UNMAPPED",
			}
			tbl_mapping = mappings.get(e_type, {})
			tbl_name = tbl_mapping.get("table_name")
			if not tbl_name:
				ent_report["status"] = "UNMAPPED"
				warnings.append(f"Entity '{e_type}' has no table mapping configured.")
			elif tbl_name not in schema_snapshot.tables:
				ent_report["status"] = "TABLE_MISSING"
				blockers.append(f"Entity '{e_type}' mapped to table '{tbl_name}' which does not exist in schema.")
			else:
				ent_report["mapped"] = True
				ent_report["table"] = tbl_name
				tbl_snap = schema_snapshot.tables[tbl_name]
				ent_report["primary_key"] = tbl_mapping.get("primary_key")

				# Stable key verification
				pk = ent_report["primary_key"]
				if not pk:
					blockers.append(f"Entity '{e_type}' lacks a designated primary_key.")
				elif pk not in tbl_snap.columns:
					blockers.append(f"Primary key '{pk}' for entity '{e_type}' not found in table '{tbl_name}'.")

				# Check logical fields
				field_maps = tbl_mapping.get("field_mappings", {})
				for l_field, req_lvl in req_fields.items():
					if l_field == "source_company_id":
						continue  # Evaluated separately below
					phys_col = field_maps.get(l_field)
					if not phys_col:
						if req_lvl == FieldRequirementLevel.REQUIRED:
							ent_report["missing_required_fields"].append(l_field)
							blockers.append(f"Entity '{e_type}' missing required logical field mapping: '{l_field}'.")
						elif req_lvl == FieldRequirementLevel.REVIEW_REQUIRED_IF_MISSING:
							ent_report["missing_optional_fields"].append(l_field)
							warnings.append(
								f"Entity '{e_type}' missing '{l_field}'. Requires explicit business review/default."
							)
						else:
							ent_report["missing_optional_fields"].append(l_field)
					elif phys_col not in tbl_snap.columns:
						blockers.append(
							f"Physical column '{phys_col}' for entity '{e_type}.{l_field}' does not exist in '{tbl_name}'."
						)

				ent_report["status"] = "READY" if not ent_report["missing_required_fields"] else "INCOMPLETE"

			entity_readiness[e_type] = ent_report

		# Company Discriminator Assessment
		# Look for company discriminator candidate in tables
		comp_col_candidates = set()
		for tbl in schema_snapshot.tables.values():
			for col in tbl.columns:
				c_low = col.lower()
				if any(k in c_low for k in ("company_id", "company_no", "corp_id", "entity_id", "business_unit")):
					comp_col_candidates.add(col)

		if comp_col_candidates:
			company_discriminator["status"] = "CANDIDATE_DISCRIMINATOR_FOUND"
			company_discriminator["discriminator_column"] = sorted(list(comp_col_candidates))[0]
			company_discriminator["candidates"] = sorted(list(comp_col_candidates))
			company_discriminator["is_company_scoped"] = True
		else:
			warnings.append(
				"No explicit company discriminator column identified in schema. "
				"Must confirm if database is single-company or multi-company via location/branch hierarchy."
			)
			company_discriminator["status"] = "NO_EXPLICIT_COLUMN_FOUND"

	# Null Sentinel Value Policy (evidence based)
	null_sentinel_policy = {
		"NULL": "Preserved strictly as None",
		"empty_string": "Preserved strictly as empty string, never coerced to NULL",
		"zero_numeric": "Preserved strictly as 0 / 0.0, never coerced to None or False",
		"boolean_false": "Preserved strictly as False, never coerced to 0 or None",
		"sentinel_literals": {
			"N/A": "Retained as raw evidence until client mapping specifies otherwise",
			"UNKNOWN": "Retained as raw evidence",
			"1900-01-01": "Retained as raw date evidence; flagged if assigned to active business date",
			"9999-12-31": "Retained as raw date evidence; flagged if assigned to active business date",
		},
	}

	# Compute Final Status
	if blockers:
		status = Prophet21ReadinessStatus.BLOCKED
	elif warnings or any(e.get("status") != "READY" for e in entity_readiness.values()):
		status = Prophet21ReadinessStatus.PARTIAL
	else:
		status = Prophet21ReadinessStatus.READY

	# Next Actions recommendations
	if status == Prophet21ReadinessStatus.BLOCKED:
		next_actions.append("Resolve all critical security and schema blockers before attempting connection.")
	elif status == Prophet21ReadinessStatus.PARTIAL:
		next_actions.append("Complete entity field mappings and obtain client IT company structure clarification.")
	else:
		next_actions.append("Proceed to read-only replica connection testing and baseline snapshot extraction.")

	return Prophet21ReadinessReport(
		status=status,
		evaluated_at=datetime.now(timezone.utc).isoformat(),
		source_system=config.source_instance_id,
		source_instance_id=config.source_instance_id,
		access_mode=config.access_mode,
		access_evaluation=access_eval,
		blockers=blockers,
		warnings=warnings,
		entity_readiness=entity_readiness,
		company_discriminator_finding=company_discriminator,
		data_volume_classification=vol_classification,
		delta_capability_finding=delta_capability,
		timezone_finding=tz_finding,
		numeric_precision_finding=num_finding,
		text_encoding_finding=text_finding,
		null_sentinel_policy=null_sentinel_policy,
		security_audit=sec_audit,
		next_actions=next_actions,
	)
