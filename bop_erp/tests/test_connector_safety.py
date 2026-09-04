# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from bop_erp.safety import (
	IntegrationEnvironment,
	canonicalize_hostname,
	is_forbidden_production_host,
	assert_safe_connector_target,
)

class TestConnectorSafety(FrappeTestCase):
	def test_default_environment_is_development(self):
		self.assertEqual(IntegrationEnvironment.DEFAULT, "DEVELOPMENT")

	def test_canonicalize_hostname(self):
		self.assertEqual(canonicalize_hostname("https://theindustrialdepot.com/api/products"), "theindustrialdepot.com")
		self.assertEqual(canonicalize_hostname("http://www.theindustrialdepot.com:8080/"), "www.theindustrialdepot.com")
		self.assertEqual(canonicalize_hostname("theindustrialdepot.com"), "theindustrialdepot.com")
		self.assertEqual(canonicalize_hostname("http://localhost:8082"), "localhost")
		self.assertEqual(canonicalize_hostname("127.0.0.1:8082"), "127.0.0.1")

	def test_forbidden_production_host_detection(self):
		self.assertTrue(is_forbidden_production_host("theindustrialdepot.com"))
		self.assertTrue(is_forbidden_production_host("https://theindustrialdepot.com"))
		self.assertTrue(is_forbidden_production_host("http://www.theindustrialdepot.com"))
		self.assertTrue(is_forbidden_production_host("https://api.theindustrialdepot.com/api"))
		self.assertTrue(is_forbidden_production_host("theindustrialdepot.com:443"))

	def test_unrelated_hosts_not_accidentally_matched(self):
		# Naive substring matching must NOT occur
		self.assertFalse(is_forbidden_production_host("https://nottheindustrialdepot.com"))
		self.assertFalse(is_forbidden_production_host("https://theindustrialdepot.com.attacker.org"))
		self.assertFalse(is_forbidden_production_host("https://theindustrialdepot.net"))
		self.assertFalse(is_forbidden_production_host("http://localhost:8082"))
		self.assertFalse(is_forbidden_production_host("http://127.0.0.1:8082"))

	def test_development_rejects_production_hosts(self):
		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("DEVELOPMENT", "https://theindustrialdepot.com")

		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("DEVELOPMENT", "https://www.theindustrialdepot.com")

		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("DEVELOPMENT", "https://api.theindustrialdepot.com")

	def test_staging_rejects_production_hosts(self):
		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("STAGING", "https://theindustrialdepot.com")

	def test_production_environment_disabled_in_phase_1(self):
		# Production operations must hard-fail in this phase
		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("PRODUCTION", "http://any-target.com")

	def test_local_test_endpoints_accepted_in_development(self):
		self.assertTrue(assert_safe_connector_target("DEVELOPMENT", "http://localhost:8082"))
		self.assertTrue(assert_safe_connector_target("DEVELOPMENT", "http://127.0.0.1:8082"))
		self.assertTrue(assert_safe_connector_target("DEVELOPMENT", "http://prestashop-test:80"))
		self.assertTrue(assert_safe_connector_target(None, "http://localhost:8082")) # Uses default DEVELOPMENT

	def test_invalid_environment_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			assert_safe_connector_target("INVALID_ENV", "http://localhost:8082")
