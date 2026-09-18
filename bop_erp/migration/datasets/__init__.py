# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from bop_erp.migration.datasets.aggregation import (
	CanonicalSourceItem,
	build_canonical_items_from_staging,
	classify_item_completeness,
)
from bop_erp.migration.datasets.authority import (
	AuthorityDecision,
	FieldAuthorityPolicy,
	evaluate_field_authority,
)
from bop_erp.migration.datasets.dependencies import build_dependency_order
from bop_erp.migration.datasets.exceptions import (
	AmbiguousProfileError,
	DatasetIngestionError,
	DatasetParsingError,
	DatasetProfileError,
	DatasetValidationError,
	DependencyCycleError,
	FileSecurityError,
	FieldAuthorityConflictError,
	MissingDependencyError,
	UnknownProfileError,
)
from bop_erp.migration.datasets.parser import (
	DEFAULT_MAX_FILE_SIZE_BYTES,
	check_file_safety,
	inspect_file_headers,
	parse_csv_stream,
	parse_xlsx_stream,
	sanitize_cell_value,
	stream_dataset_file,
)
from bop_erp.migration.datasets.profiles import (
	SourceDatasetProfile,
	get_initial_p21_profiles,
)
from bop_erp.migration.datasets.quality import generate_quality_reconciliation_report
from bop_erp.migration.datasets.registry import (
	DetectionResult,
	ProfileRegistry,
	default_registry,
)
from bop_erp.migration.datasets.staging import (
	compute_source_record_key_hash,
	extract_composite_source_identity,
	extract_item_id_from_record_id,
	extract_source_key_components,
	normalize_dataset_payload,
	parse_source_record_id,
	serialize_canonical_key,
	stage_dataset_file,
	stage_dataset_row,
)
from bop_erp.migration.datasets.importers import (
	ControlledItemImporter,
	EligibilityStatus,
	ImportEligibilityResult,
	ItemImportPreview,
	ItemImportResult,
	build_target_item_code,
	evaluate_item_eligibility,
	generate_import_preview,
)
from bop_erp.migration.datasets.validation import (
	validate_cross_dataset_relationship,
	validate_row_structure,
)

__all__ = [
	"CanonicalSourceItem",
	"build_canonical_items_from_staging",
	"classify_item_completeness",
	"AuthorityDecision",
	"FieldAuthorityPolicy",
	"evaluate_field_authority",
	"build_dependency_order",
	"DatasetIngestionError",
	"DatasetProfileError",
	"DatasetParsingError",
	"FileSecurityError",
	"DatasetValidationError",
	"DependencyCycleError",
	"MissingDependencyError",
	"AmbiguousProfileError",
	"UnknownProfileError",
	"FieldAuthorityConflictError",
	"DEFAULT_MAX_FILE_SIZE_BYTES",
	"check_file_safety",
	"inspect_file_headers",
	"parse_csv_stream",
	"parse_xlsx_stream",
	"sanitize_cell_value",
	"stream_dataset_file",
	"SourceDatasetProfile",
	"get_initial_p21_profiles",
	"generate_quality_reconciliation_report",
	"DetectionResult",
	"ProfileRegistry",
	"default_registry",
	"extract_composite_source_identity",
	"extract_source_key_components",
	"serialize_canonical_key",
	"compute_source_record_key_hash",
	"parse_source_record_id",
	"extract_item_id_from_record_id",
	"normalize_dataset_payload",
	"stage_dataset_file",
	"stage_dataset_row",
	"validate_cross_dataset_relationship",
	"validate_row_structure",
	"ControlledItemImporter",
	"EligibilityStatus",
	"ImportEligibilityResult",
	"ItemImportPreview",
	"ItemImportResult",
	"build_target_item_code",
	"evaluate_item_eligibility",
	"generate_import_preview",
]
