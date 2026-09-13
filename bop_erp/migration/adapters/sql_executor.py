# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import re
import sqlite3
from abc import ABC, abstractmethod
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

from bop_erp.migration.exceptions import (
	SourceReadError,
	SourceWriteBlockedError,
)
from bop_erp.migration.safety import assert_read_only_sql


def coerce_source_value(val: Any) -> Any:
	"""
	Strictly controls type coercion of raw source ERP database values.
	Preserves numeric precision, NULL, empty string, zero, and boolean false.
	Never collapses distinct falsy semantics.
	"""
	if val is None:
		return None
	if isinstance(val, bool):
		return val
	if isinstance(val, int):
		return val
	if isinstance(val, float):
		return val
	if isinstance(val, Decimal):
		# Preserve exact precision without IEEE-754 floating point distortion
		return str(val)
	if isinstance(val, (datetime, date)):
		return val.isoformat()
	if isinstance(val, bytes):
		try:
			return val.decode("utf-8")
		except UnicodeDecodeError:
			return val.hex()
	if isinstance(val, str):
		return val
	return str(val)


def coerce_source_record(row: Dict[str, Any]) -> Dict[str, Any]:
	"""Coerces all field values in a source record dictionary."""
	return {k: coerce_source_value(v) for k, v in row.items()}


class ReadOnlySqlExecutor(ABC):
	"""
	Abstract base class for strictly read-only SQL execution against source ERPs.
	Guarantees all queries pass through assert_read_only_sql prior to execution.
	Never exposes DML, DDL, stored procedure execution, or transaction mutation methods.
	"""

	def execute_select(
		self,
		sql: str,
		params: Optional[Union[Tuple[Any, ...], List[Any], Dict[str, Any]]] = None,
		*,
		max_rows: Optional[int] = None,
	) -> List[Dict[str, Any]]:
		"""
		Validates and executes a SELECT statement.
		Central safety gate: any non-SELECT or mutation statement raises SourceWriteBlockedError.
		"""
		assert_read_only_sql(sql)
		return self._execute_select_internal(sql, params, max_rows=max_rows)

	@abstractmethod
	def _execute_select_internal(
		self,
		sql: str,
		params: Optional[Union[Tuple[Any, ...], List[Any], Dict[str, Any]]] = None,
		*,
		max_rows: Optional[int] = None,
	) -> List[Dict[str, Any]]:
		"""Provider-specific internal select execution."""
		pass


class SyntheticSqlExecutor(ReadOnlySqlExecutor):
	"""
	In-memory SQLite-backed read-only executor for synthetic Prophet 21 testing.
	Populated purely with offline test fixtures without external network or credentials.
	"""

	def __init__(
		self,
		initial_tables: Optional[Dict[str, List[Dict[str, Any]]]] = None,
		initial_data: Optional[Dict[str, List[Dict[str, Any]]]] = None,
	):
		self._conn = sqlite3.connect(":memory:", check_same_thread=False)
		self._conn.row_factory = sqlite3.Row
		tables = initial_tables or initial_data
		if tables:
			for table_name, rows in tables.items():
				self.load_table(table_name, rows)

	def load_table(self, table_name: str, rows: List[Dict[str, Any]]) -> None:
		"""Loads synthetic table schema and rows into memory."""
		if not re.match(r"^[A-Za-z0-9_]+$", table_name):
			raise ValueError(f"Invalid synthetic table name: '{table_name}'")

		if not rows:
			# Create empty table
			self._conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} (id TEXT PRIMARY KEY)")
			self._conn.commit()
			return

		sample = rows[0]
		cols = []
		for col, val in sample.items():
			col_type = "TEXT"
			if isinstance(val, bool):
				col_type = "INTEGER"
			elif isinstance(val, int):
				col_type = "INTEGER"
			elif isinstance(val, (float, Decimal)):
				col_type = "REAL"
			cols.append(f"{col} {col_type}")

		create_stmt = f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(cols)})"
		self._conn.execute(create_stmt)

		col_names = list(sample.keys())
		placeholders = ", ".join(["?" for _ in col_names])
		insert_stmt = f"INSERT INTO {table_name} ({', '.join(col_names)}) VALUES ({placeholders})"

		for r in rows:
			row_vals = [r.get(c) for c in col_names]
			# Coerce boolean to int for sqlite
			coerced = [int(v) if isinstance(v, bool) else (str(v) if isinstance(v, Decimal) else v) for v in row_vals]
			self._conn.execute(insert_stmt, coerced)

		self._conn.commit()

	def _execute_select_internal(
		self,
		sql: str,
		params: Optional[Union[Tuple[Any, ...], List[Any], Dict[str, Any]]] = None,
		*,
		max_rows: Optional[int] = None,
	) -> List[Dict[str, Any]]:
		try:
			cursor = self._conn.cursor()
			if params:
				cursor.execute(sql, params)
			else:
				cursor.execute(sql)

			if max_rows and max_rows > 0:
				raw_rows = cursor.fetchmany(max_rows)
			else:
				raw_rows = cursor.fetchall()

			results = []
			for row in raw_rows:
				d = dict(row)
				results.append(coerce_source_record(d))
			return results
		except sqlite3.Error as e:
			raise SourceReadError(f"Synthetic SQL execution error: {e}") from e

	def close(self) -> None:
		try:
			self._conn.close()
		except Exception:
			pass
