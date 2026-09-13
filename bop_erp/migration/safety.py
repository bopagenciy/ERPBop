# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import copy
import re
from typing import Any, Dict, List, Set, Union
from urllib.parse import urlparse

from bop_erp.migration.exceptions import (
	SourceSafetyViolationError,
	SourceWriteBlockedError,
)
from bop_erp.safety import is_forbidden_production_host

ALLOWED_HTTP_METHODS: Set[str] = {"GET", "HEAD", "OPTIONS"}
BLOCKED_HTTP_METHODS: Set[str] = {"POST", "PUT", "PATCH", "DELETE"}

BLOCKED_SQL_COMMANDS: Set[str] = {
	"INSERT",
	"UPDATE",
	"DELETE",
	"MERGE",
	"UPSERT",
	"REPLACE",
	"ALTER",
	"DROP",
	"CREATE",
	"TRUNCATE",
	"EXECUTE",
	"EXEC",
	"CALL",
	"GRANT",
	"REVOKE",
}

DANGEROUS_SQL_PATTERNS = [
	re.compile(r"\bINTO\s+OUTFILE\b", re.IGNORECASE),
	re.compile(r"\bINTO\s+DUMPFILE\b", re.IGNORECASE),
	re.compile(r"\bSELECT\s+.*?\bINTO\s+\b(?!(?:OUTFILE|DUMPFILE)\b)", re.IGNORECASE),
]


def strip_sql_comments(sql: str) -> str:
	"""Removes line (-- and #) and block (/* ... */) comments from SQL."""
	# Remove block comments
	sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
	# Remove line comments
	sql = re.sub(r"--.*?$", " ", sql, flags=re.MULTILINE)
	sql = re.sub(r"#.*?$", " ", sql, flags=re.MULTILINE)
	return sql.strip()


def assert_read_only_sql(sql_query: str) -> None:
	"""
	Classifies SQL statements before execution to guarantee read-only semantics.
	Only SELECT and CTEs (WITH ... SELECT) are permitted.
	Fail-closed on any mutation, DDL, DCL, or unparseable query structure.
	"""
	if not sql_query or not isinstance(sql_query, str):
		raise SourceWriteBlockedError("Empty or invalid SQL query passed to source execution safety gate.")

	cleaned = strip_sql_comments(sql_query).strip()
	if not cleaned:
		raise SourceWriteBlockedError("SQL query contains no executable statements after comment stripping.")

	# Disallow forbidden write clauses
	for pat in DANGEROUS_SQL_PATTERNS:
		if pat.search(cleaned):
			raise SourceWriteBlockedError(
				f"SQL query contains forbidden data export/mutation pattern: {pat.pattern}"
			)

	# Split on semicolons to check all chained statements
	statements = [s.strip() for s in cleaned.split(";") if s.strip()]
	if not statements:
		raise SourceWriteBlockedError("No executable SQL statements found.")

	for statement in statements:
		# Tokenize keywords
		tokens = re.findall(r"[A-Za-z_]+", statement)
		if not tokens:
			raise SourceWriteBlockedError("Statement contains no valid tokens.")

		first_token = tokens[0].upper()

		# Must begin with SELECT or WITH
		if first_token not in {"SELECT", "WITH"}:
			raise SourceWriteBlockedError(
				f"Source ERP statement must start with SELECT or WITH. Attempted '{first_token}' is strictly blocked."
			)

		# Check for blocked command tokens anywhere in statement
		for token in tokens:
			t_upper = token.upper()
			if t_upper in BLOCKED_SQL_COMMANDS:
				# Distinguish legitimate WITH clauses from CTE mutation statements (e.g. WITH ... UPDATE)
				raise SourceWriteBlockedError(
					f"Forbidden SQL keyword '{t_upper}' detected in source ERP query. "
					"Source ERP is strictly read-only."
				)

		# If statement starts with WITH, ensure final action is a SELECT
		if first_token == "WITH":
			upper_tokens = [t.upper() for t in tokens]
			if "SELECT" not in upper_tokens:
				raise SourceWriteBlockedError(
					"Common Table Expression (WITH) does not resolve to a final SELECT query."
				)


def assert_read_only_http_method(method: str) -> None:
	"""
	Verifies that HTTP requests targeting source systems use strictly read-only verbs.
	Blocks POST, PUT, PATCH, DELETE before the request is dispatched over network.
	"""
	if not method or not isinstance(method, str):
		raise SourceWriteBlockedError("HTTP method must be a non-empty string.")

	clean_method = method.strip().upper()

	if clean_method in BLOCKED_HTTP_METHODS:
		raise SourceWriteBlockedError(
			f"HTTP verb '{clean_method}' is a mutation operation. "
			"Source ERP adapters are strictly read-only; mutations are blocked before dispatch."
		)

	if clean_method not in ALLOWED_HTTP_METHODS:
		raise SourceWriteBlockedError(
			f"HTTP verb '{clean_method}' is not in the allowed read-only whitelist ({', '.join(sorted(ALLOWED_HTTP_METHODS))})."
		)


def assert_safe_source_target(url_or_host: str) -> None:
	"""
	Validates that an adapter does not contact forbidden production domains.
	"""
	if not url_or_host:
		return

	if is_forbidden_production_host(url_or_host):
		raise SourceSafetyViolationError(
			f"CRITICAL SAFETY VIOLATION: Source target '{url_or_host}' resolves to a forbidden production host."
		)


def redact_sensitive_payload(data: Any) -> Any:
	"""
	Recursively redacts PII and confidential credentials in migration logs and audit trails.
	"""
	if isinstance(data, dict):
		redacted = {}
		for k, v in data.items():
			lower_key = str(k).lower()
			if any(secret_term in lower_key for secret_term in ["password", "secret", "token", "api_key", "apikey", "card", "cvv", "auth"]):
				redacted[k] = "[REDACTED_SECRET]"
			elif any(tax_term in lower_key for tax_term in ["tax_id", "rfc", "nit", "rut", "ssn"]):
				redacted[k] = _mask_text(str(v), keep_end=4) if v else v
			elif "email" in lower_key:
				redacted[k] = _mask_email(str(v)) if v else v
			elif "phone" in lower_key:
				redacted[k] = _mask_phone(str(v)) if v else v
			elif isinstance(v, (dict, list)):
				redacted[k] = redact_sensitive_payload(v)
			else:
				redacted[k] = v
		return redacted
	elif isinstance(data, list):
		return [redact_sensitive_payload(item) for item in data]
	return data


def _mask_text(val: str, keep_end: int = 4) -> str:
	s = str(val).strip()
	if len(s) <= keep_end:
		return "***"
	return "*" * (len(s) - keep_end) + s[-keep_end:]


def _mask_email(email: str) -> str:
	if "@" not in email:
		return _mask_text(email)
	user, domain = email.split("@", 1)
	if len(user) <= 1:
		masked_user = "*"
	else:
		masked_user = user[0] + "*" * (len(user) - 1)
	return f"{masked_user}@{domain}"


def _mask_phone(phone: str) -> str:
	s = str(phone).strip()
	if len(s) <= 4:
		return "***"
	return "*" * (len(s) - 4) + s[-4:]
