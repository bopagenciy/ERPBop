# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Iterator, Optional

from bop_erp.migration.safety import (
	assert_read_only_http_method,
	assert_read_only_sql,
	assert_safe_source_target,
)


class SourceAdapter(ABC):
	"""
	Provider-neutral, read-only adapter contract for Source ERP extraction.
	Enforces read-only safety before any query or request execution.
	"""

	def __init__(
		self,
		source_system: str,
		source_instance_id: str = "DEFAULT",
		source_schema_version: Optional[str] = None,
	):
		self.source_system = str(source_system).strip().upper()
		self.source_instance_id = str(source_instance_id).strip()
		self.source_schema_version = source_schema_version
		self.snapshot_started_at: Optional[datetime] = None
		self.snapshot_completed_at: Optional[datetime] = None

	@abstractmethod
	def health_check(self) -> Dict[str, Any]:
		"""Verifies read connectivity to the source system without mutating state."""
		pass

	@abstractmethod
	def get_source_metadata(self) -> Dict[str, Any]:
		"""Returns descriptive metadata (system name, instance, schema version, capabilities)."""
		pass

	@abstractmethod
	def stream_customers(
		self,
		limit: Optional[int] = None,
		offset: int = 0,
		filters: Optional[Dict[str, Any]] = None,
	) -> Iterator[Dict[str, Any]]:
		"""Streams raw customer records from the source system."""
		pass

	@abstractmethod
	def stream_vendors(
		self,
		limit: Optional[int] = None,
		offset: int = 0,
		filters: Optional[Dict[str, Any]] = None,
	) -> Iterator[Dict[str, Any]]:
		"""Streams raw vendor / supplier records from the source system."""
		pass

	@abstractmethod
	def stream_items(
		self,
		limit: Optional[int] = None,
		offset: int = 0,
		filters: Optional[Dict[str, Any]] = None,
	) -> Iterator[Dict[str, Any]]:
		"""Streams raw item / product catalog records from the source system."""
		pass

	@abstractmethod
	def stream_warehouses(
		self,
		limit: Optional[int] = None,
		offset: int = 0,
		filters: Optional[Dict[str, Any]] = None,
	) -> Iterator[Dict[str, Any]]:
		"""Streams raw warehouse / location records from the source system."""
		pass

	def execute_read_query(self, sql_query: str, params: Optional[Any] = None) -> Any:
		"""
		Safe SQL execution gate: validates query via assert_read_only_sql before delegating.
		Concrete database adapters must call this or override while honoring assert_read_only_sql.
		"""
		assert_read_only_sql(sql_query)
		return self._run_safe_sql(sql_query, params)

	def execute_http_get(
		self,
		url: str,
		params: Optional[Dict[str, Any]] = None,
		headers: Optional[Dict[str, Any]] = None,
	) -> Any:
		"""
		Safe HTTP execution gate: validates GET verb and target domain safety before delegating.
		"""
		assert_read_only_http_method("GET")
		assert_safe_source_target(url)
		return self._run_safe_get(url, params=params, headers=headers)

	def _run_safe_sql(self, sql_query: str, params: Optional[Any] = None) -> Any:
		"""Subclass hook to execute a validated read-only SQL query."""
		raise NotImplementedError("Subclass must implement _run_safe_sql")

	def _run_safe_get(
		self,
		url: str,
		params: Optional[Dict[str, Any]] = None,
		headers: Optional[Dict[str, Any]] = None,
	) -> Any:
		"""Subclass hook to execute a validated read-only HTTP GET request."""
		raise NotImplementedError("Subclass must implement _run_safe_get")
