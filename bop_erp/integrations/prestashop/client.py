# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import json
from typing import Any, Dict, List, Optional
import requests
from requests.auth import HTTPBasicAuth

from bop_erp.safety import sanitize_url_for_logging
from bop_erp.integrations.prestashop.config import PrestaShopConfig
from bop_erp.integrations.prestashop.exceptions import (
	PrestaShopError,
	PrestaShopAuthError,
	PrestaShopNotFoundError,
	PrestaShopRateLimitError,
	PrestaShopValidationError,
	PrestaShopServerError,
	PrestaShopTransientError,
	PrestaShopMalformedResponseError,
)

MAX_PAGE_SIZE = 100


class PrestaShopClient:
	"""
	Read-only HTTP client for the PrestaShop Webservice API.
	Enforces safety denylists, URL sanitization, bounded pagination, and robust error classification.
	"""

	def __init__(self, config: PrestaShopConfig, session: Optional[requests.Session] = None):
		self.config = config
		self.config.assert_safe()
		self._api_key = self.config.resolve_api_key()
		self.session = session or requests.Session()
		self.session.auth = HTTPBasicAuth(self._api_key, "")

	def _build_url(self, endpoint: str) -> str:
		self.config.assert_safe()
		endpoint = endpoint.lstrip("/")
		return f"{self.config.base_url}/api/{endpoint}"

	def _request(
		self,
		method: str,
		endpoint: str,
		params: Optional[Dict[str, Any]] = None,
		expected_json: bool = True,
	) -> Any:
		self.config.assert_safe()
		url = self._build_url(endpoint)

		query_params = dict(params or {})
		if expected_json and "output_format" not in query_params:
			query_params["output_format"] = "JSON"

		headers = {
			"Accept": "application/json" if expected_json else "*/*",
			"User-Agent": "Bop-ERP-Connector/1.0",
		}

		try:
			resp = self.session.request(
				method=method,
				url=url,
				params=query_params,
				headers=headers,
				timeout=self.config.timeout_seconds,
				verify=self.config.verify_tls,
			)
		except (requests.ConnectionError, requests.Timeout) as conn_err:
			safe_url = sanitize_url_for_logging(url)
			raise PrestaShopTransientError(
				f"Network connectivity/timeout failure calling {safe_url}: {conn_err}",
				sensitive_token=self._api_key,
			) from conn_err
		except requests.RequestException as req_err:
			safe_url = sanitize_url_for_logging(url)
			raise PrestaShopError(
				f"Unexpected HTTP client request failure calling {safe_url}: {req_err}",
				sensitive_token=self._api_key,
			) from req_err

		# Translate HTTP Status Codes
		status = resp.status_code
		safe_url = sanitize_url_for_logging(url)

		if status in (401, 403):
			raise PrestaShopAuthError(
				f"Authentication/permission rejected ({status}) for PrestaShop endpoint {safe_url}: {resp.text[:200]}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			)
		if status == 404:
			raise PrestaShopNotFoundError(
				f"Resource not found (404) at PrestaShop endpoint {safe_url}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			)
		if status == 429:
			raise PrestaShopRateLimitError(
				f"Rate limit exceeded (429) at PrestaShop endpoint {safe_url}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			)
		if status in (400, 422):
			raise PrestaShopValidationError(
				f"Validation/client error ({status}) at PrestaShop endpoint {safe_url}: {resp.text[:300]}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			)
		if status >= 500:
			raise PrestaShopServerError(
				f"PrestaShop server error ({status}) at PrestaShop endpoint {safe_url}: {resp.text[:300]}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			)

		if not expected_json:
			return resp.text

		try:
			return resp.json()
		except Exception as json_err:
			raise PrestaShopMalformedResponseError(
				f"Malformed JSON response from PrestaShop endpoint {safe_url}: {json_err}. Raw body: {resp.text[:200]}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			) from json_err

	def health_check(self) -> bool:
		"""
		Verifies reachability and authorization of the PrestaShop API.
		Uses /api (HEAD or GET) or /api/categories?limit=1 for compatibility.
		"""
		self.config.assert_safe()
		try:
			# Checking /api/categories with limit 1 is the most robust JSON check
			data = self._request("GET", "categories", params={"limit": 1})
			return "categories" in data or "category" in data
		except PrestaShopError:
			raise
		except Exception as e:
			raise PrestaShopTransientError(
				f"Health check failed: {e}",
				sensitive_token=self._api_key,
			) from e

	def _list_resource(
		self,
		resource_name: str,
		limit: int = 50,
		offset: int = 0,
		filters: Optional[Dict[str, Any]] = None,
		display: Optional[str] = None,
	) -> List[Dict[str, Any]]:
		"""Generic bounded pagination query for PrestaShop list resources."""
		bounded_limit = max(1, min(int(limit), MAX_PAGE_SIZE))
		bounded_offset = max(0, int(offset))

		params = {
			"limit": f"{bounded_offset},{bounded_limit}",
		}
		if display:
			params["display"] = display

		if filters:
			for k, v in filters.items():
				params[f"filter[{k}]"] = f"[{v}]"

		data = self._request("GET", resource_name, params=params)
		items = data.get(resource_name, [])
		if isinstance(items, list):
			return items
		if isinstance(items, dict):
			return [items]
		return []

	def _get_resource(self, resource_name: str, resource_id: Any) -> Dict[str, Any]:
		"""Generic fetch for a single PrestaShop resource by ID."""
		res_id = str(resource_id).strip()
		if not res_id:
			raise PrestaShopValidationError("Resource ID is required.")

		data = self._request("GET", f"{resource_name}/{res_id}")
		if isinstance(data, dict):
			# PrestaShop returns singular object e.g. {"product": {...}} or {"address": {...}}
			for k, v in data.items():
				if isinstance(v, dict) and k != "errors":
					return v
		return data

	# --- Category Methods ---
	def list_categories(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("categories", limit, offset, filters, display)

	def get_category(self, category_id: Any) -> Dict[str, Any]:
		return self._get_resource("categories", category_id)

	# --- Product Methods ---
	def list_products(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("products", limit, offset, filters, display)

	def get_product(self, product_id: Any) -> Dict[str, Any]:
		return self._get_resource("products", product_id)

	# --- Combination Methods ---
	def list_combinations(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("combinations", limit, offset, filters, display)

	def get_combination(self, combination_id: Any) -> Dict[str, Any]:
		return self._get_resource("combinations", combination_id)

	# --- Stock Methods ---
	def list_stock_availables(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("stock_availables", limit, offset, filters, display)

	def get_stock_available(self, stock_id: Any) -> Dict[str, Any]:
		return self._get_resource("stock_availables", stock_id)

	# --- Customer Methods ---
	def list_customers(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("customers", limit, offset, filters, display)

	def get_customer(self, customer_id: Any) -> Dict[str, Any]:
		return self._get_resource("customers", customer_id)

	# --- Address Methods ---
	def list_addresses(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("addresses", limit, offset, filters, display)

	def get_address(self, address_id: Any) -> Dict[str, Any]:
		return self._get_resource("addresses", address_id)

	# --- Order Methods ---
	def list_orders(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("orders", limit, offset, filters, display)

	def get_order(self, order_id: Any) -> Dict[str, Any]:
		return self._get_resource("orders", order_id)

	def get_order_details(self, order_id: Any) -> List[Dict[str, Any]]:
		"""Fetches order_detail records belonging to a given order ID."""
		res = self._list_resource("order_details", limit=100, filters={"id_order": order_id}, display="full")
		return res

	def get_order_state(self, state_id: Any) -> Dict[str, Any]:
		return self._get_resource("order_states", state_id)
