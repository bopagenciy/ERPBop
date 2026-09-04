# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import os
from dataclasses import dataclass
import frappe
from frappe import _

from bop_erp.safety import (
	IntegrationEnvironment,
	assert_safe_connector_target,
)
from bop_erp.integrations.prestashop.exceptions import PrestaShopAuthError


@dataclass
class PrestaShopConfig:
	sales_channel: str
	environment: str
	base_url: str
	credential_reference: str
	timeout_seconds: int = 30
	verify_tls: bool = True
	read_enabled: bool = True
	write_enabled: bool = False

	def assert_safe(self):
		"""Validates safety constraints against target base URL and environment."""
		assert_safe_connector_target(environment=self.environment, base_url=self.base_url)

	def resolve_api_key(self) -> str:
		"""
		Securely resolves the PrestaShop API key using the credential_reference.
		Checked in order:
		1. frappe.conf (site_config.json / bench config)
		2. os.environ
		3. Local test fallback for development/testing environments
		"""
		ref = self.credential_reference.strip()

		# 1. Check frappe.conf
		key = frappe.conf.get(ref)
		if key:
			return str(key).strip()

		# 2. Check environment variable
		key = os.environ.get(ref)
		if key:
			return str(key).strip()

		# 3. Known local development test key fallback
		if self.environment in (IntegrationEnvironment.DEVELOPMENT, "DEVELOPMENT"):
			if ref in ("TEST_PRESTASHOP_KEY", "PRESTASHOP_TEST_KEY", "LOCAL_PRESTASHOP_KEY"):
				return "BOPTESTKEY1234567890123456789012"

		raise PrestaShopAuthError(
			_("Could not resolve API credential for reference '{0}'. "
			  "Ensure it is configured in site_config.json or environment variables.").format(ref)
		)

	@classmethod
	def from_connector_doc(cls, doc):
		return cls(
			sales_channel=doc.sales_channel,
			environment=doc.environment or IntegrationEnvironment.DEVELOPMENT,
			base_url=(doc.base_url or "").strip().rstrip("/"),
			credential_reference=(doc.credential_reference or "").strip(),
			timeout_seconds=int(doc.timeout_seconds or 30),
			verify_tls=bool(doc.verify_tls),
			read_enabled=bool(doc.read_enabled),
			write_enabled=bool(doc.write_enabled),
		)
