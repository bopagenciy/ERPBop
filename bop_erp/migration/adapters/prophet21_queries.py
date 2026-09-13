# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from bop_erp.migration.exceptions import (
	SourceMappingError,
	SourceReadError,
)
from bop_erp.migration.safety import assert_read_only_sql

IDENTIFIER_REGEX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 5000

REQUIRED_LOGICAL_FIELDS: Dict[str, List[str]] = {
	"CUSTOMER": ["source_record_id", "name"],
	"VENDOR": ["source_record_id", "name"],
	"ITEM": ["source_record_id", "item_code", "name"],
	"WAREHOUSE": ["source_record_id", "warehouse_code", "warehouse_name"],
}

ALL_LOGICAL_FIELDS: Dict[str, List[str]] = {
	"CUSTOMER": ["source_record_id", "name", "email", "phone", "tax_id", "currency", "payment_terms"],
	"VENDOR": ["source_record_id", "name", "email", "phone", "tax_id", "currency", "payment_terms"],
	"ITEM": [
		"source_record_id",
		"item_code",
		"name",
		"description",
		"stock_uom",
		"item_group",
		"is_stock_item",
		"serialized",
		"batch_tracked",
	],
	"WAREHOUSE": ["source_record_id", "warehouse_code", "warehouse_name", "parent_code", "company"],
}


@dataclass
class LogicalEntityMapping:
	"""
	Defines the physical Prophet 21 table and column bindings for a logical entity.
	Decouples logical ERP migration entities from customer-specific physical P21 schemas.
	"""

	entity_type: str
	table_name: str
	primary_key: str
	field_mappings: Dict[str, str]  # logical_field -> physical_column
	source_company_column: Optional[str] = None

	def validate(self) -> None:
		"""Validates identifier safety and required logical field coverage."""
		e_type = str(self.entity_type).strip().upper()
		if e_type not in REQUIRED_LOGICAL_FIELDS:
			raise SourceMappingError(f"Unsupported entity type '{self.entity_type}' in Prophet 21 mapping.")

		if not IDENTIFIER_REGEX.match(self.table_name):
			raise SourceMappingError(f"Invalid physical table identifier: '{self.table_name}'")

		if not IDENTIFIER_REGEX.match(self.primary_key):
			raise SourceMappingError(f"Invalid primary key identifier: '{self.primary_key}'")

		if self.source_company_column and not IDENTIFIER_REGEX.match(self.source_company_column):
			raise SourceMappingError(f"Invalid source company column identifier: '{self.source_company_column}'")

		# Check that required logical fields are mapped
		req_fields = REQUIRED_LOGICAL_FIELDS[e_type]
		for rf in req_fields:
			if rf not in self.field_mappings or not self.field_mappings[rf]:
				raise SourceMappingError(
					f"Entity '{e_type}' mapping is missing required logical field '{rf}'. "
					f"Mapped fields: {list(self.field_mappings.keys())}"
				)

		# Validate physical column identifiers
		for log_field, phys_col in self.field_mappings.items():
			if not IDENTIFIER_REGEX.match(phys_col):
				raise SourceMappingError(
					f"Invalid physical column identifier '{phys_col}' for logical field '{log_field}'."
				)


def build_bounded_select_query(
	mapping: LogicalEntityMapping,
	source_company_id: Optional[str] = None,
	cursor_val: Optional[Any] = None,
	limit: int = DEFAULT_PAGE_SIZE,
	offset: Optional[int] = None,
) -> Tuple[str, List[Any]]:
	"""
	Constructs a strictly read-only, deterministic SELECT query for bounded extraction.
	- Columns are explicitly enumerated as 'physical_col AS logical_field' (no SELECT *).
	- Identifiers are pre-validated against injection.
	- Values are passed via bound parameters.
	- Deterministic ORDER BY primary_key is mandatory.
	- Extraction bounds (page size) are enforced.
	"""
	mapping.validate()

	# Enforce page size bounds
	if limit is None or limit <= 0:
		limit = DEFAULT_PAGE_SIZE
	if limit > MAX_PAGE_SIZE:
		limit = MAX_PAGE_SIZE

	# Explicit column projections
	projections = []
	for log_col, phys_col in mapping.field_mappings.items():
		projections.append(f"{phys_col} AS {log_col}")
	proj_str = ", ".join(projections)

	where_clauses = []
	params: List[Any] = []

	# Source company scoping
	if mapping.source_company_column and source_company_id is not None:
		where_clauses.append(f"{mapping.source_company_column} = ?")
		params.append(str(source_company_id))

	# Keyset cursor pagination (preferred)
	if cursor_val is not None and str(cursor_val).strip():
		where_clauses.append(f"{mapping.primary_key} > ?")
		params.append(cursor_val)

	where_str = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
	order_by_str = f" ORDER BY {mapping.primary_key} ASC"

	# Pagination
	if offset is not None and offset > 0:
		limit_str = f" LIMIT ? OFFSET ?"
		params.extend([limit, offset])
	else:
		limit_str = f" LIMIT ?"
		params.append(limit)

	sql = f"SELECT {proj_str} FROM {mapping.table_name}{where_str}{order_by_str}{limit_str}"

	# Assert read-only safety
	assert_read_only_sql(sql)

	return sql, params
