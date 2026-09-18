# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from bop_erp.migration.exceptions import MigrationError


class DatasetIngestionError(MigrationError):
	"""Base exception for all dataset ingestion operations."""
	pass


class DatasetProfileError(DatasetIngestionError):
	"""Raised when a dataset profile definition is missing, invalid, or corrupted."""
	pass


class DatasetParsingError(DatasetIngestionError):
	"""Raised when parsing a source file (CSV/XLSX) fails or encounters corrupt data."""
	pass


class FileSecurityError(DatasetParsingError):
	"""Raised when a file fails security checks (oversized, macro, unsafe path)."""
	pass


class DatasetValidationError(DatasetIngestionError):
	"""Raised when dataset rows fail structural or business validation rules."""
	pass


class DependencyCycleError(DatasetIngestionError):
	"""Raised when a circular dependency is detected among dataset profiles."""
	pass


class MissingDependencyError(DatasetIngestionError):
	"""Raised when a required profile dependency is missing or disabled."""
	pass


class AmbiguousProfileError(DatasetIngestionError):
	"""Raised when profile detection encounters multiple conflicting candidates."""
	pass


class UnknownProfileError(DatasetIngestionError):
	"""Raised when no matching profile can be detected for an input file."""
	pass


class FieldAuthorityConflictError(DatasetIngestionError):
	"""Raised when a field collision occurs under REVIEW_ON_CONFLICT policy."""
	pass
