# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from urllib.parse import urlparse
import frappe
from frappe import _

class IntegrationEnvironment:
	DEVELOPMENT = "DEVELOPMENT"
	STAGING = "STAGING"
	PRODUCTION = "PRODUCTION"

	DEFAULT = DEVELOPMENT
	ALL = {DEVELOPMENT, STAGING, PRODUCTION}

FORBIDDEN_PRODUCTION_DOMAINS = {
	"theindustrialdepot.com",
}

def canonicalize_hostname(target_url_or_host):
	"""
	Extracts and normalizes the canonical hostname from a URL, hostname, or address.
	- Lowercases the hostname.
	- Strips URI scheme (http://, https://).
	- Strips port numbers (:8080, :443, etc.).
	- Strips path components, query strings, and fragments.
	- Strips trailing dots.
	Returns: normalized hostname string, or empty string.
	"""
	if not target_url_or_host:
		return ""

	raw = str(target_url_or_host).strip().lower()

	if "://" not in raw:
		parsed = urlparse(f"http://{raw}")
	else:
		parsed = urlparse(raw)

	netloc = parsed.netloc or parsed.path.split("/")[0]

	if ":" in netloc:
		netloc = netloc.split(":")[0]

	hostname = netloc.strip().strip(".")
	return hostname

def is_forbidden_production_host(target_url_or_host):
	"""
	Checks if target_url_or_host resolves to a forbidden production host or subdomain.
	Uses exact domain and subdomain boundary matching, NOT naive substring matching.
	"""
	hostname = canonicalize_hostname(target_url_or_host)
	if not hostname:
		return False

	for forbidden in FORBIDDEN_PRODUCTION_DOMAINS:
		if hostname == forbidden:
			return True
		if hostname.endswith("." + forbidden):
			return True

	return False

def assert_safe_connector_target(environment=None, base_url=None):
	"""
	Validates that an external connector target is safe for the specified environment.
	- In DEVELOPMENT or STAGING: targeting a forbidden production host results in a HARD FAILURE.
	- In PRODUCTION: production operations are strictly forbidden in this phase.
	- If environment is invalid: hard failure.
	Throws frappe.ValidationError on any violation.
	Returns True if safe.
	"""
	env = (environment or IntegrationEnvironment.DEFAULT).strip().upper()

	if env not in IntegrationEnvironment.ALL:
		frappe.throw(
			_("Invalid integration environment '{0}'. Allowed: {1}").format(
				env, ", ".join(sorted(IntegrationEnvironment.ALL))
			),
			frappe.ValidationError,
		)

	if not base_url:
		frappe.throw(_("Base URL is required to validate connector target."), frappe.ValidationError)

	canonical_host = canonicalize_hostname(base_url)

	if is_forbidden_production_host(base_url):
		frappe.throw(
			_(
				"CRITICAL SAFETY VIOLATION: Target host '{0}' resolves to a forbidden production domain "
				"and is strictly blocked in environment '{1}'."
			).format(canonical_host, env),
			frappe.ValidationError,
		)

	if env == IntegrationEnvironment.PRODUCTION:
		frappe.throw(
			_(
				"CRITICAL SAFETY VIOLATION: Production operations are permanently disabled in Phase 1D."
			),
			frappe.ValidationError,
		)

	return True

def sanitize_url_for_logging(url):
	"""
	Sanitizes a URL for safe logging by masking any embedded username or password.
	e.g. http://KEY:@127.0.0.1:8082/api -> http://***@127.0.0.1:8082/api
	"""
	if not url:
		return ""
	try:
		parsed = urlparse(str(url))
		if parsed.username or parsed.password:
			netloc = parsed.netloc
			if "@" in netloc:
				auth_part, host_part = netloc.split("@", 1)
				sanitized_netloc = f"***@{host_part}"
				return parsed._replace(netloc=sanitized_netloc).geturl()
		return str(url)
	except Exception:
		return "[SANITIZED_URL]"

