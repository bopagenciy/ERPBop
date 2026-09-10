# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import os
from dataclasses import dataclass
from typing import Optional
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
	order_state_write_enabled: bool = False
	order_state_send_email: bool = False
	shipping_state_id: Optional[str] = None
	delivered_state_id: Optional[str] = None
	cancellation_order_states: Optional[str] = None
	review_order_states: Optional[str] = None
	eligible_order_states: Optional[str] = None

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
			order_state_write_enabled=bool(getattr(doc, "order_state_write_enabled", False)),
			order_state_send_email=bool(getattr(doc, "order_state_send_email", False)),
			shipping_state_id=str(doc.shipping_state_id).strip() if getattr(doc, "shipping_state_id", None) else None,
			delivered_state_id=str(doc.delivered_state_id).strip() if getattr(doc, "delivered_state_id", None) else None,
			cancellation_order_states=str(doc.cancellation_order_states).strip() if getattr(doc, "cancellation_order_states", None) else None,
			review_order_states=str(doc.review_order_states).strip() if getattr(doc, "review_order_states", None) else None,
			eligible_order_states=str(doc.eligible_order_states).strip() if getattr(doc, "eligible_order_states", None) else None,
		)
