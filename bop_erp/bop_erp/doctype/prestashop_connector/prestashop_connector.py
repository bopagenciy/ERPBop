# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from bop_erp.safety import (
	IntegrationEnvironment,
	assert_safe_connector_target,
	is_forbidden_production_host,
)


class PrestaShopConnector(Document):
	def validate(self):
		self.validate_safety()
		self.validate_read_only_phase()
		self.validate_credential_reference()
		self.validate_uniqueness()
		self.clean_base_url()

	def validate_uniqueness(self):
		"""
		Enforces:
		1. Unique composite key of (sales_channel, environment).
		2. At most one enabled connector per sales_channel.
		"""
		existing_env = frappe.db.get_value(
			"PrestaShop Connector",
			{"sales_channel": self.sales_channel, "environment": self.environment},
			"name",
		)
		if existing_env and existing_env != self.name:
			frappe.throw(
				_("A PrestaShop Connector already exists for Sales Channel '{0}' and Environment '{1}' ({2}).").format(
					self.sales_channel, self.environment, existing_env
				),
				frappe.DuplicateEntryError,
			)

		if self.enabled:
			existing_enabled = frappe.db.get_value(
				"PrestaShop Connector",
				{"sales_channel": self.sales_channel, "enabled": 1},
				"name",
			)
			if existing_enabled and existing_enabled != self.name:
				frappe.throw(
					_(
						"Only one active PrestaShop Connector is permitted for Sales Channel '{0}'. "
						"Connector '{1}' is already active."
					).format(self.sales_channel, existing_enabled),
					frappe.ValidationError,
				)

	def validate_safety(self):
		"""Enforces strict safety denylist and environment rules before saving."""
		assert_safe_connector_target(environment=self.environment, base_url=self.base_url)

	def validate_read_only_phase(self):
		"""Phase 1D is strictly read-only; write_enabled must be False."""
		if self.write_enabled:
			frappe.throw(
				_("Write operations are strictly disabled in Phase 1D (Read-Only connector core)."),
				frappe.ValidationError,
			)

	def validate_credential_reference(self):
		"""Ensures credential_reference is a reference key and not a raw API secret."""
		ref = (self.credential_reference or "").strip()
		if not ref:
			frappe.throw(_("Credential Reference is required."), frappe.ValidationError)
		# Raw PrestaShop webservice keys are 32 hexadecimal/alphanumeric chars
		if len(ref) >= 32 and ref.isalnum() and not any(c in ref for c in "_-"):
			frappe.throw(
				_(
					"Credential Reference appears to be a raw API key. "
					"Do NOT store raw credentials in PrestaShop Connector. "
					"Provide a reference key name (e.g. TEST_PRESTASHOP_KEY) to resolve from secure configuration."
				),
				frappe.ValidationError,
			)

	def clean_base_url(self):
		if self.base_url:
			self.base_url = self.base_url.strip().rstrip("/")

	def get_client(self):
		"""Returns an authenticated PrestaShopClient instance configured from this connector."""
		from bop_erp.integrations.prestashop.client import PrestaShopClient
		from bop_erp.integrations.prestashop.config import PrestaShopConfig

		config = PrestaShopConfig.from_connector_doc(self)
		return PrestaShopClient(config=config)

	@frappe.whitelist()
	def run_health_check(self):
		"""Executes a live health check against the configured PrestaShop endpoint."""
		self.validate_safety()
		client = self.get_client()
		try:
			is_healthy = client.health_check()
			status = "HEALTHY" if is_healthy else "UNHEALTHY"
			self.db_set("last_health_check_at", now_datetime())
			self.db_set("last_health_check_status", status)
			return {"status": status, "success": is_healthy}
		except Exception as e:
			status = f"FAILED: {str(e)[:140]}"
			self.db_set("last_health_check_at", now_datetime())
			self.db_set("last_health_check_status", status)
			return {"status": status, "success": False, "error": str(e)}
