# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt


class MigrationError(Exception):
	"""Base exception for all source ERP migration errors."""
	pass


class SourceWriteBlockedError(MigrationError):
	"""Raised when any mutation, DML write, or non-read HTTP verb is attempted against a source ERP."""
	pass


class SourceSafetyViolationError(MigrationError):
	"""Raised when a forbidden production host, credential leak, or unsafe connection target is detected."""
	pass


class SourceConnectionError(MigrationError):
	"""Raised when connection to a source ERP fails."""
	pass


class ProductionSourceBlockedError(SourceSafetyViolationError):
	"""Raised when any attempt is made to connect to or extract from a PRODUCTION source environment."""
	pass


class SourceSchemaError(MigrationError):
	"""Raised when source schema inspection or resolution fails."""
	pass


class SourceMappingError(MigrationError):
	"""Raised when logical to physical schema mapping cannot be resolved or is invalid."""
	pass


class SourceReadError(MigrationError):
	"""Raised when an error occurs during source ERP query extraction."""
	pass


class MigrationRunStateError(MigrationError):
	"""Raised when an invalid state transition is attempted on a Migration Run."""
	pass


class SourcePayloadDriftError(MigrationError):
	"""Raised when a source record's payload has drifted from a previously captured payload."""
	pass


class StagingError(MigrationError):
	"""Raised when error occurs during staging."""
	pass


class NormalizationError(MigrationError):
	"""Raised when candidate normalization fails."""
	pass


class MigrationValidationError(MigrationError):
	"""Raised when validation fails or blocks migration readiness."""
	pass


class DryRunError(MigrationError):
	"""Raised when an error occurs during dry run preview."""
	pass


class ImportBoundaryError(MigrationError):
	"""Raised when an import is attempted on unready or invalid migration records."""
	pass
