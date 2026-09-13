# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from bop_erp.migration.adapters.base import SourceAdapter
from bop_erp.migration.adapters.synthetic import SyntheticSourceAdapter
from bop_erp.migration.dry_run import execute_dry_run
from bop_erp.migration.exceptions import (
	DryRunError,
	ImportBoundaryError,
	MigrationError,
	MigrationRunStateError,
	MigrationValidationError,
	NormalizationError,
	ProductionSourceBlockedError,
	SourceConnectionError,
	SourceMappingError,
	SourcePayloadDriftError,
	SourceReadError,
	SourceSafetyViolationError,
	SourceSchemaError,
	SourceWriteBlockedError,
	StagingError,
)
from bop_erp.migration.adapters.prophet21 import (
	Prophet21SourceAdapter,
	SourceColumnMetadata,
	SourceSchemaMetadata,
	SourceTableMetadata,
	extract_entity_to_staging,
)
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
	coerce_source_value,
)
from bop_erp.migration.import_boundary import import_validated_entity
from bop_erp.migration.normalization import (
	normalize_customer,
	normalize_item,
	normalize_migration_run,
	normalize_record,
	normalize_staging_row,
	normalize_vendor,
	normalize_warehouse,
)
from bop_erp.migration.reconciliation import reconcile_migration_run
from bop_erp.migration.safety import (
	assert_read_only_http_method,
	assert_read_only_sql,
	assert_safe_source_target,
	redact_sensitive_payload,
)
from bop_erp.migration.staging import (
	compute_payload_hash,
	compute_staging_identity,
	extract_and_stage_from_adapter,
	stage_source_record,
)
from bop_erp.migration.namespaces import (
	canonical_company_tag,
	canonical_provider,
	canonical_source_instance_id,
	canonical_source_namespace,
	canonical_source_system,
	compute_migration_channel_id,
)
from bop_erp.migration.validation import validate_migration_run

__all__ = [
	"canonical_source_system",
	"canonical_source_instance_id",
	"canonical_source_namespace",
	"canonical_provider",
	"canonical_company_tag",
	"compute_migration_channel_id",
	"SourceAdapter",
	"SyntheticSourceAdapter",
	"execute_dry_run",
	"reconcile_migration_run",
	"import_validated_entity",
	"normalize_record",
	"normalize_customer",
	"normalize_vendor",
	"normalize_item",
	"normalize_warehouse",
	"normalize_staging_row",
	"normalize_migration_run",
	"validate_migration_run",
	"stage_source_record",
	"extract_and_stage_from_adapter",
	"compute_staging_identity",
	"compute_payload_hash",
	"assert_read_only_sql",
	"assert_read_only_http_method",
	"assert_safe_source_target",
	"redact_sensitive_payload",
	"MigrationError",
	"SourceWriteBlockedError",
	"SourceSafetyViolationError",
	"SourceConnectionError",
	"MigrationRunStateError",
	"SourcePayloadDriftError",
	"StagingError",
	"NormalizationError",
	"MigrationValidationError",
	"DryRunError",
	"ImportBoundaryError",
	"ProductionSourceBlockedError",
	"SourceSchemaError",
	"SourceMappingError",
	"SourceReadError",
	"Prophet21SourceAdapter",
	"SourceColumnMetadata",
	"SourceTableMetadata",
	"SourceSchemaMetadata",
	"extract_entity_to_staging",
	"ReadOnlySqlExecutor",
	"SyntheticSqlExecutor",
	"LogicalEntityMapping",
	"build_bounded_select_query",
	"coerce_source_value",
	"coerce_source_record",
	"DEFAULT_PAGE_SIZE",
	"MAX_PAGE_SIZE",
]
