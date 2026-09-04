# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import re
from bop_erp.safety import sanitize_url_for_logging


def mask_sensitive_strings(text, sensitive_tokens=None):
	"""Masks 32-char hex/alnum strings and explicit tokens from text."""
	if not text:
		return ""
	s = str(text)
	if sensitive_tokens:
		for token in sensitive_tokens:
			if token and len(str(token)) > 4:
				s = s.replace(str(token), "***")
	# Mask standard 32-char PrestaShop keys (e.g. Basic auth headers or URL fragments)
	s = re.sub(r"\b[A-Za-z0-9]{32}\b", "***", s)
	return sanitize_url_for_logging(s)


class PrestaShopError(Exception):
	"""Base exception for all PrestaShop integration errors."""

	def __init__(self, message, status_code=None, response_body=None, sensitive_token=None):
		self.raw_message = message
		self.status_code = status_code
		self.sensitive_token = sensitive_token
		sanitized_msg = mask_sensitive_strings(message, [sensitive_token] if sensitive_token else None)
		super().__init__(sanitized_msg)

	def __str__(self):
		return mask_sensitive_strings(super().__str__(), [self.sensitive_token] if self.sensitive_token else None)

	def __repr__(self):
		return f"{self.__class__.__name__}({str(self)})"


class PrestaShopAuthError(PrestaShopError):
	"""Authentication or permission failure (401 / 403)."""
	pass


class PrestaShopNotFoundError(PrestaShopError):
	"""Resource not found (404)."""
	pass


class PrestaShopRateLimitError(PrestaShopError):
	"""Rate limit exceeded (429)."""
	pass


class PrestaShopValidationError(PrestaShopError):
	"""Invalid request or schema validation failure (400 / 422)."""
	pass


class PrestaShopServerError(PrestaShopError):
	"""PrestaShop internal server error (5xx)."""
	pass


class PrestaShopTransientError(PrestaShopError):
	"""Network connectivity, socket, or timeout failure."""
	pass


class PrestaShopMalformedResponseError(PrestaShopError):
	"""Response could not be parsed as valid JSON or expected structure."""
	pass
