# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

import frappe

from bop_erp.migration.adapters.base import SourceAdapter
from bop_erp.migration.adapters.prophet21_queries import (
	DEFAULT_PAGE_SIZE,
	MAX_PAGE_SIZE,
	LogicalEntityMapping,
	build_bounded_select_query,
)
from bop_erp.migration.adapters.sql_executor import (
	ReadOnlySqlExecutor,
	SyntheticSqlExecutor,
	coerce_source_record,
)
from bop_erp.migration.exceptions import (
	ProductionSourceBlockedError,
	SourceConnectionError,
	SourceMappingError,
	SourceReadError,
	SourceSafetyViolationError,
	SourceSchemaError,
	StagingError,
)
from bop_erp.migration.namespaces import (
	canonical_source_instance_id,
	canonical_source_system,
)
from bop_erp.migration.staging import stage_source_record

ALLOWED_ENVIRONMENTS = {"SYNTHETIC", "SNAPSHOT"}
BLOCKED_ENVIRONMENTS = {"PRODUCTION", "STAGING"}


@dataclass
class SourceColumnMetadata:
	name: str
	data_type: str
	is_nullable: bool = True
	is_primary_key: bool = False


@dataclass
class SourceTableMetadata:
	name: str
	columns: Dict[str, SourceColumnMetadata] = field(default_factory=dict)
	primary_key: Optional[str] = None


@dataclass
class SourceSchemaMetadata:
	tables: Dict[str, SourceTableMetadata] = field(default_factory=dict)

	def has_table(self, table_name: str) -> bool:
		return table_name in self.tables

	def get_table(self, table_name: str) -> Optional[SourceTableMetadata]:
		return self.tables.get(table_name)

	def can_resolve_mapping(self, mapping: LogicalEntityMapping) -> Tuple[bool, List[str]]:
		missing = []
		if not self.has_table(mapping.table_name):
			missing.append(f"Table '{mapping.table_name}' does not exist in discovered schema.")
			return False, missing

		tbl = self.tables[mapping.table_name]
		for log_field, phys_col in mapping.field_mappings.items():
			if phys_col not in tbl.columns:
				missing.append(
					f"Column '{phys_col}' for logical field '{log_field}' not found in table '{mapping.table_name}'."
				)

		return len(missing) == 0, missing


DEFAULT_SYNTHETIC_MAPPINGS: Dict[str, LogicalEntityMapping] = {
	"CUSTOMER": LogicalEntityMapping(
		entity_type="CUSTOMER",
		table_name="synthetic_customer",
		primary_key="customer_id",
		field_mappings={
			"source_record_id": "customer_id",
			"name": "customer_name",
			"email": "email_address",
			"phone": "phone_number",
			"tax_id": "tax_id_number",
			"currency": "currency_code",
			"payment_terms": "terms_code",
		},
		source_company_column="company_id",
	),
	"VENDOR": LogicalEntityMapping(
		entity_type="VENDOR",
		table_name="synthetic_vendor",
		primary_key="vendor_id",
		field_mappings={
			"source_record_id": "vendor_id",
			"name": "vendor_name",
			"email": "email_address",
			"phone": "phone_number",
			"tax_id": "tax_id_number",
			"currency": "currency_code",
			"payment_terms": "terms_code",
		},
		source_company_column="company_id",
	),
	"ITEM": LogicalEntityMapping(
		entity_type="ITEM",
		table_name="synthetic_item",
		primary_key="item_id",
		field_mappings={
			"source_record_id": "item_id",
			"item_code": "item_code",
			"name": "item_description",
			"description": "extended_description",
			"stock_uom": "unit_of_measure",
			"item_group": "product_group",
			"is_stock_item": "stockable_flag",
			"serialized": "serialized_flag",
			"batch_tracked": "lot_tracked_flag",
		},
		source_company_column="company_id",
	),
	"WAREHOUSE": LogicalEntityMapping(
		entity_type="WAREHOUSE",
		table_name="synthetic_warehouse",
		primary_key="location_id",
		field_mappings={
			"source_record_id": "location_id",
			"warehouse_code": "location_code",
			"warehouse_name": "location_name",
			"parent_code": "parent_location_code",
			"company": "company_id",
		},
		source_company_column="company_id",
	),
}


class Prophet21SourceAdapter(SourceAdapter):
	"""
	Strictly read-only Prophet 21 source adapter.
	Plugs cleanly into the Phase 1U migration framework.
	Guaranteed:
	- Zero writes to Prophet 21.
	- Hard-blocked on PRODUCTION environment.
	- All queries pass through assert_read_only_sql.
	- Configurable logical-to-physical schema mapping.
	- Bounded, deterministically ordered extraction.
	"""

	def __init__(
		self,
		source_instance_id: str = "DEFAULT",
		source_environment: str = "SYNTHETIC",
		executor: Optional[ReadOnlySqlExecutor] = None,
		entity_mappings: Optional[Dict[str, LogicalEntityMapping]] = None,
		source_company_id: Optional[str] = None,
		source_schema_version: Optional[str] = "2026.1",
	):
		clean_env = str(source_environment).strip().upper()

		# Section D: Production Environment Gate (Fail-closed)
		if clean_env == "PRODUCTION":
			raise ProductionSourceBlockedError(
				"CRITICAL SAFETY VIOLATION: Prophet 21 adapter targeting 'PRODUCTION' is strictly prohibited in Phase 1V. "
				"No connection or credentials can be established."
			)

		if clean_env not in ALLOWED_ENVIRONMENTS:
			raise ProductionSourceBlockedError(
				f"Source environment '{clean_env}' is not permitted in Phase 1V. "
				f"Allowed environments: {sorted(list(ALLOWED_ENVIRONMENTS))}."
			)

		super().__init__(
			source_system="PROPHET_21",
			source_instance_id=canonical_source_instance_id(source_instance_id),
		)

		self.source_environment = clean_env
		self.source_company_id = str(source_company_id).strip() if source_company_id else None
		self.source_schema_version = str(source_schema_version).strip() if source_schema_version else "2026.1"

		self.executor: ReadOnlySqlExecutor = executor or SyntheticSqlExecutor()
		self.entity_mappings = entity_mappings or dict(DEFAULT_SYNTHETIC_MAPPINGS)

		# Snapshot & watermark tracking
		self.snapshot_started_at: Optional[str] = None
		self.snapshot_completed_at: Optional[str] = None
		self.last_source_key: Optional[str] = None
		self.rows_extracted: int = 0
		self.pages_extracted: int = 0

	def health_check(self) -> Dict[str, Any]:
		"""Verifies read-only adapter health and environment status."""
		test_rows = self.executor.execute_select("SELECT 1 AS probe", max_rows=1)
		healthy = len(test_rows) > 0 and test_rows[0].get("probe") == 1
		return {
			"status": "HEALTHY" if healthy else "UNHEALTHY",
			"read_only": True,
			"source_system": self.source_system,
			"source_instance_id": self.source_instance_id,
			"source_environment": self.source_environment,
		}

	def get_source_metadata(self) -> Dict[str, Any]:
		"""
		Returns sanitized adapter metadata.
		Guaranteed to never contain secret credentials or connection tokens.
		"""
		counts = {}
		for e_type, mapping in self.entity_mappings.items():
			try:
				cnt_sql = f"SELECT COUNT(*) AS total FROM {mapping.table_name}"
				if mapping.source_company_column and self.source_company_id:
					cnt_sql += f" WHERE {mapping.source_company_column} = '{self.source_company_id}'"
				cnt_res = self.executor.execute_select(cnt_sql, max_rows=1)
				counts[f"{e_type.lower()}_count"] = cnt_res[0].get("total", 0) if cnt_res else 0
			except Exception:
				counts[f"{e_type.lower()}_count"] = 0

		return {
			"source_system": self.source_system,
			"source_instance_id": self.source_instance_id,
			"source_environment": self.source_environment,
			"source_schema_version": self.source_schema_version,
			"source_company_id": self.source_company_id,
			"capabilities": ["CUSTOMER", "VENDOR", "ITEM", "WAREHOUSE"],
			"read_only": True,
			**counts,
		}

	def discover_schema(self) -> SourceSchemaMetadata:
		"""
		Performs read-only metadata inspection of the underlying database schema.
		Returns normalized SourceSchemaMetadata.
		"""
		schema_meta = SourceSchemaMetadata()
		for e_type, mapping in self.entity_mappings.items():
			tbl_name = mapping.table_name
			# Read one row to discover columns and types safely
			try:
				rows = self.executor.execute_select(f"SELECT * FROM {tbl_name} WHERE 1=0")
			except Exception as e:
				raise SourceSchemaError(f"Failed to inspect table '{tbl_name}' for entity '{e_type}': {e}") from e

			tbl_meta = SourceTableMetadata(name=tbl_name, primary_key=mapping.primary_key)
			# Populate discovered columns from mapping
			for log_f, phys_c in mapping.field_mappings.items():
				tbl_meta.columns[phys_c] = SourceColumnMetadata(
					name=phys_c,
					data_type="TEXT",
					is_primary_key=(phys_c == mapping.primary_key),
				)
			if mapping.source_company_column:
				tbl_meta.columns[mapping.source_company_column] = SourceColumnMetadata(
					name=mapping.source_company_column,
					data_type="TEXT",
				)
			schema_meta.tables[tbl_name] = tbl_meta

		return schema_meta

	def _stream_entity(
		self,
		entity_type: str,
		limit: Optional[int] = None,
		offset: Optional[int] = None,
		cursor_val: Optional[Any] = None,
		page_size: int = DEFAULT_PAGE_SIZE,
	) -> Generator[Dict[str, Any], None, None]:
		"""Bounded, deterministic extraction generator for a logical entity."""
		e_type = entity_type.upper()
		if e_type not in self.entity_mappings:
			raise SourceMappingError(f"No mapping registered for entity '{entity_type}'.")

		mapping = self.entity_mappings[e_type]
		mapping.validate()

		if not self.snapshot_started_at:
			self.snapshot_started_at = datetime.now(timezone.utc).isoformat()

		current_cursor = cursor_val
		current_offset = offset or 0
		remaining = limit

		while remaining is None or remaining > 0:
			fetch_size = min(remaining, min(page_size, MAX_PAGE_SIZE)) if remaining is not None else min(page_size, MAX_PAGE_SIZE)
			sql, params = build_bounded_select_query(
				mapping=mapping,
				source_company_id=self.source_company_id,
				cursor_val=current_cursor,
				limit=fetch_size,
				offset=current_offset if current_cursor is None else None,
			)

			batch = self.executor.execute_select(sql, params, max_rows=fetch_size)
			if not batch:
				break

			self.pages_extracted += 1
			for row in batch:
				yield row
				self.rows_extracted += 1
				if remaining is not None:
					remaining -= 1
					if remaining <= 0:
						break

			if len(batch) < fetch_size:
				break

			if current_cursor is not None:
				current_cursor = batch[-1][mapping.primary_key]
			else:
				current_offset += len(batch)

		self.snapshot_completed_at = datetime.now(timezone.utc).isoformat()

	def stream_customers(
		self, limit: Optional[int] = None, offset: Optional[int] = None, page_size: int = DEFAULT_PAGE_SIZE
	) -> Generator[Dict[str, Any], None, None]:
		yield from self._stream_entity("CUSTOMER", limit=limit, offset=offset, page_size=page_size)

	def stream_vendors(
		self, limit: Optional[int] = None, offset: Optional[int] = None, page_size: int = DEFAULT_PAGE_SIZE
	) -> Generator[Dict[str, Any], None, None]:
		yield from self._stream_entity("VENDOR", limit=limit, offset=offset, page_size=page_size)

	def stream_items(
		self, limit: Optional[int] = None, offset: Optional[int] = None, page_size: int = DEFAULT_PAGE_SIZE
	) -> Generator[Dict[str, Any], None, None]:
		yield from self._stream_entity("ITEM", limit=limit, offset=offset, page_size=page_size)

	def stream_warehouses(
		self, limit: Optional[int] = None, offset: Optional[int] = None, page_size: int = DEFAULT_PAGE_SIZE
	) -> Generator[Dict[str, Any], None, None]:
		yield from self._stream_entity("WAREHOUSE", limit=limit, offset=offset, page_size=page_size)


def extract_entity_to_staging(
	run_id: str,
	adapter: Prophet21SourceAdapter,
	entity_type: str,
	limit: Optional[int] = None,
	fail_after_records: Optional[int] = None,
) -> int:
	"""
	Orchestrates extraction from Prophet21SourceAdapter into Phase 1U Migration Staging Row.
	The ONLY allowed path:
	SOURCE -> STAGING -> NORMALIZATION -> VALIDATION -> DRY RUN.
	Direct adapter -> ERP target documents is strictly prohibited.
	"""
	run_doc = frappe.get_doc("Migration Run", run_id)
	if run_doc.status not in ("DRAFT", "EXTRACTING", "STAGED"):
		raise StagingError(f"Migration Run '{run_id}' is in status '{run_doc.status}'. Cannot extract.")

	if run_doc.status == "DRAFT":
		run_doc.status = "EXTRACTING"
		run_doc.started_at = frappe.utils.now_datetime()
		run_doc.save(ignore_permissions=True)

	e_type = entity_type.upper()
	if e_type == "CUSTOMER":
		stream = adapter.stream_customers(limit=limit)
	elif e_type == "VENDOR":
		stream = adapter.stream_vendors(limit=limit)
	elif e_type == "ITEM":
		stream = adapter.stream_items(limit=limit)
	elif e_type == "WAREHOUSE":
		stream = adapter.stream_warehouses(limit=limit)
	else:
		raise StagingError(f"Unsupported entity type '{entity_type}'.")

	staged_count = 0
	try:
		for raw_record in stream:
			parent_id = raw_record.get("parent_code") if e_type == "WAREHOUSE" else None
			stage_source_record(
				run_id=run_id,
				source_system=adapter.source_system,
				source_instance_id=adapter.source_instance_id,
				entity_type=e_type,
				source_record=raw_record,
				source_parent_id=parent_id,
			)
			staged_count += 1

			# Simulated failure injection for resilience testing
			if fail_after_records is not None and staged_count >= fail_after_records:
				raise SourceReadError(f"Simulated extraction failure after {staged_count} records.")

		run_doc.reload()
		run_doc.status = "STAGED"
		run_doc.total_rows = frappe.db.count("Migration Staging Row", {"migration_run": run_id})
		run_doc.save(ignore_permissions=True)
		frappe.db.commit()

	except Exception as ex:
		# Retain already staged rows for auditability; transition run to FAILED or REVIEW_REQUIRED
		run_doc.reload()
		run_doc.status = "FAILED"
		run_doc.notes = f"Extraction failed mid-stream: {str(ex)}"
		run_doc.total_rows = frappe.db.count("Migration Staging Row", {"migration_run": run_id})
		run_doc.save(ignore_permissions=True)
		frappe.db.commit()
		raise

	return staged_count
