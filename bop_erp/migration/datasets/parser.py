# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import csv
from datetime import date, datetime
from decimal import Decimal
import io
import os
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple, Union

from bop_erp.migration.datasets.exceptions import (
	DatasetParsingError,
	FileSecurityError,
)
from bop_erp.migration.datasets.profiles import SourceDatasetProfile

# 50MB maximum bound for file safety
DEFAULT_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".xlsx", ".csv"}
FORBIDDEN_EXTENSIONS = {".xlsm", ".xltm", ".xla", ".exe", ".dll", ".sh", ".bat", ".ps1"}


def check_file_safety(file_path: Union[str, Path], max_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES) -> Path:
	"""
	Validates that the file path exists, is a regular file within allowed extension list,
	does not exceed max size bounds, and does not exhibit directory traversal tricks.
	"""
	path = Path(file_path).resolve()
	if not path.exists() or not path.is_file():
		raise FileSecurityError(f"File '{file_path}' does not exist or is not a file.")

	ext = path.suffix.lower()
	if ext in FORBIDDEN_EXTENSIONS:
		raise FileSecurityError(f"File extension '{ext}' is forbidden for security reasons.")
	if ext not in ALLOWED_EXTENSIONS:
		raise FileSecurityError(f"File extension '{ext}' is not supported. Allowed: {ALLOWED_EXTENSIONS}")

	size = path.stat().st_size
	if size > max_bytes:
		raise FileSecurityError(
			f"File size ({size} bytes) exceeds maximum allowable limit of {max_bytes} bytes."
		)
	return path


def sanitize_cell_value(val: Any) -> Any:
	"""
	Preserves explicit None, empty string, boolean False, 0, and Decimal precision.
	Avoids lossy floating point coercion.
	Preserves leading-zero string identities.
	"""
	if val is None:
		return None
	if isinstance(val, bool):
		return val
	if isinstance(val, (int, Decimal)):
		return val
	if isinstance(val, float):
		# If float represents an exact integer (e.g. 101341.0 from excel), preserve as int
		if val.is_integer():
			return int(val)
		return Decimal(str(val))
	if isinstance(val, (datetime, date)):
		return val.isoformat()

	val_str = str(val).strip()
	return val_str


def parse_csv_stream(
	file_path: Union[str, Path],
	profile: SourceDatasetProfile,
	encoding: str = "utf-8-sig",
	max_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
) -> Generator[Tuple[int, Dict[str, Any]], None, None]:
	"""
	Streams rows from a CSV file following profile rules.
	"""
	safe_path = check_file_safety(file_path, max_bytes=max_bytes)

	try:
		with open(safe_path, mode="r", encoding=encoding, newline="") as f:
			reader = csv.reader(f)
			headers: List[str] = []
			header_row_num = profile.header_row
			metadata_rows = set(profile.metadata_rows)
			example_rows = set(profile.example_rows_to_ignore)
			data_start_row = profile.data_start_row

			for row_idx, raw_row in enumerate(reader, start=1):
				if row_idx == header_row_num:
					headers = [str(c).strip() for c in raw_row]
					continue

				if row_idx in metadata_rows or row_idx in example_rows:
					continue

				if row_idx < data_start_row:
					continue

				# Check for completely empty trailing rows
				if not any(raw_row):
					continue

				row_dict: Dict[str, Any] = {}
				for col_idx, col_name in enumerate(headers):
					if not col_name:
						continue
					val = raw_row[col_idx] if col_idx < len(raw_row) else None
					row_dict[col_name] = sanitize_cell_value(val)

				# Yield 1-based source row index and payload
				yield row_idx, row_dict
	except Exception as e:
		if isinstance(e, FileSecurityError):
			raise
		raise DatasetParsingError(f"Failed to parse CSV file '{safe_path.name}': {str(e)}") from e


def parse_xlsx_stream(
	file_path: Union[str, Path],
	profile: SourceDatasetProfile,
	max_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
) -> Generator[Tuple[int, Dict[str, Any]], None, None]:
	"""
	Streams rows from an XLSX workbook in read-only, data-only mode to prevent macro
	or arbitrary formula execution.
	"""
	safe_path = check_file_safety(file_path, max_bytes=max_bytes)

	try:
		import openpyxl
	except ImportError:
		raise DatasetParsingError("openpyxl is required to parse Excel workbooks.")

	try:
		wb = openpyxl.load_workbook(safe_path, read_only=True, data_only=True)
		sheet_name = profile.sheet_name or wb.sheetnames[0]
		if sheet_name not in wb.sheetnames:
			if len(wb.sheetnames) == 1:
				sheet_name = wb.sheetnames[0]
			else:
				wb.close()
				raise DatasetParsingError(
					f"Sheet '{sheet_name}' not found in workbook '{safe_path.name}'. Available: {wb.sheetnames}"
				)

		sheet = wb[sheet_name]
		headers: List[str] = []
		header_row_num = profile.header_row
		metadata_rows = set(profile.metadata_rows)
		example_rows = set(profile.example_rows_to_ignore)
		data_start_row = profile.data_start_row

		for row_idx, row_cells in enumerate(sheet.iter_rows(values_only=True), start=1):
			if row_idx == header_row_num:
				# Capture headers, strip trailing Nones
				raw_headers = [str(c).strip() if c is not None else "" for c in row_cells]
				# Remove trailing empty column headers
				last_valid = len(raw_headers)
				while last_valid > 0 and not raw_headers[last_valid - 1]:
					last_valid -= 1
				headers = raw_headers[:last_valid]
				continue

			if row_idx in metadata_rows or row_idx in example_rows:
				continue

			if row_idx < data_start_row:
				continue

			# Check if entire row is empty
			if not any(c is not None and str(c).strip() != "" for c in row_cells):
				continue

			row_dict: Dict[str, Any] = {}
			for col_idx, col_name in enumerate(headers):
				if not col_name:
					continue
				raw_val = row_cells[col_idx] if col_idx < len(row_cells) else None
				row_dict[col_name] = sanitize_cell_value(raw_val)

			yield row_idx, row_dict

		wb.close()
	except Exception as e:
		if isinstance(e, (FileSecurityError, DatasetParsingError)):
			raise
		raise DatasetParsingError(f"Failed to parse XLSX file '{safe_path.name}': {str(e)}") from e


def stream_dataset_file(
	file_path: Union[str, Path],
	profile: SourceDatasetProfile,
	max_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
) -> Generator[Tuple[int, Dict[str, Any]], None, None]:
	"""
	Generic file streaming entrypoint. Dispatches to CSV or XLSX parser based on file suffix.
	"""
	safe_path = check_file_safety(file_path, max_bytes=max_bytes)
	ext = safe_path.suffix.lower()
	if ext == ".xlsx":
		return parse_xlsx_stream(safe_path, profile, max_bytes=max_bytes)
	elif ext == ".csv":
		return parse_csv_stream(safe_path, profile, max_bytes=max_bytes)
	else:
		raise FileSecurityError(f"Unsupported file format '{ext}'. Allowed: {ALLOWED_EXTENSIONS}")


def inspect_file_headers(
	file_path: Union[str, Path],
	header_row: int = 1,
	sheet_name: Optional[str] = None,
	max_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
) -> List[str]:
	"""
	Reads only the header row from a file to support auto-detection without loading all rows.
	"""
	safe_path = check_file_safety(file_path, max_bytes=max_bytes)
	ext = safe_path.suffix.lower()

	if ext == ".csv":
		with open(safe_path, mode="r", encoding="utf-8-sig", newline="") as f:
			reader = csv.reader(f)
			for idx, row in enumerate(reader, start=1):
				if idx == header_row:
					return [str(c).strip() for c in row if c is not None and str(c).strip()]
		return []

	elif ext == ".xlsx":
		import openpyxl
		wb = openpyxl.load_workbook(safe_path, read_only=True, data_only=True)
		target_sheet = sheet_name or wb.sheetnames[0]
		sheet = wb[target_sheet]
		for idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
			if idx == header_row:
				headers = [str(c).strip() for c in row if c is not None and str(c).strip()]
				wb.close()
				return headers
		wb.close()
		return []

	return []
