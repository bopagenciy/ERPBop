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
			retry_after = None
			ra_hdr = resp.headers.get("Retry-After")
			if ra_hdr:
				try:
					retry_after = int(ra_hdr)
				except (ValueError, TypeError):
					pass
			raise PrestaShopRateLimitError(
				f"Rate limit exceeded (429) at PrestaShop endpoint {safe_url}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
				retry_after=retry_after,
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
			return resp.content

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
		if isinstance(data, list):
			return data
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

	def get_product_image_binary(self, product_id: Any, image_id: Any) -> bytes:
		"""Fetches raw image binary content from /api/images/products/{product_id}/{image_id}."""
		return self._request("GET", f"images/products/{product_id}/{image_id}", expected_json=False)

	# --- Combination Methods ---
	def list_combinations(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("combinations", limit, offset, filters, display)

	def get_combination(self, combination_id: Any) -> Dict[str, Any]:
		return self._get_resource("combinations", combination_id)

	# --- Product Options (Attribute Groups) & Option Values ---
	def list_product_options(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("product_options", limit, offset, filters, display)

	def get_product_option(self, option_id: Any) -> Dict[str, Any]:
		return self._get_resource("product_options", option_id)

	def list_product_option_values(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("product_option_values", limit, offset, filters, display)

	def get_product_option_value(self, value_id: Any) -> Dict[str, Any]:
		return self._get_resource("product_option_values", value_id)

	# --- Stock Methods ---
	def list_stock_availables(
		self, limit: int = 50, offset: int = 0, filters: Optional[Dict[str, Any]] = None, display: Optional[str] = None
	) -> List[Dict[str, Any]]:
		return self._list_resource("stock_availables", limit, offset, filters, display)

	def get_stock_available(self, stock_id: Any) -> Dict[str, Any]:
		return self._get_resource("stock_availables", stock_id)

	def resolve_stock_available_id(self, product_id: int, variant_id: Optional[int] = None) -> int:
		"""
		Resolves the exact stock_available resource ID from PrestaShop.
		- For simple products: id_product = product_id, id_product_attribute = 0.
		- For combination products: id_product = product_id, id_product_attribute = variant_id.
		Raises PrestaShopNotFoundError if no matching row is found.
		"""
		attr_id = int(variant_id) if variant_id else 0
		filters = {
			"id_product": int(product_id),
			"id_product_attribute": attr_id,
		}
		rows = self._list_resource("stock_availables", limit=10, filters=filters)
		if not rows:
			raise PrestaShopNotFoundError(
				f"No stock_available resource found for product {product_id} with attribute {attr_id}"
			)
		first = rows[0]
		sa_id = first.get("id") if isinstance(first, dict) else getattr(first, "id", None)
		if not sa_id:
			raise PrestaShopNotFoundError(
				f"Invalid stock_available row returned for product {product_id} with attribute {attr_id}: {first}"
			)
		return int(sa_id)

	def get_stock_available_xml(self, stock_id: Any) -> str:
		"""Fetches the raw XML representation of stock_available for Read-Modify-Write."""
		self.config.assert_safe()
		res_id = str(stock_id).strip()
		content = self._request("GET", f"stock_availables/{res_id}", expected_json=False)
		return content.decode("utf-8") if isinstance(content, bytes) else str(content)

	def update_stock_available_quantity(
		self,
		stock_available_id: int,
		quantity: int,
		expected_product_id: int,
		expected_variant_id: Optional[int] = None,
		pre_put_hook: Optional[Any] = None,
	) -> Dict[str, Any]:
		"""
		Safely updates the quantity of a stock_available record in PrestaShop.
		Enforces:
		1. Hard Host Safety Guard: assert_safe_write_target(environment, base_url).
		2. Connector write_enabled flag.
		3. Read-before-write XML fetch.
		4. Identity Mismatch verification: id_product and id_product_attribute MUST match expected.
		5. Delta detection (NO-OP if remote quantity already equals desired quantity).
		6. Read-Modify-Write XML: preserves id_product, id_product_attribute, id_shop, out_of_stock,
		   depends_on_stock, location, and mutates ONLY the <quantity> element.
		7. Executes optional pre_put_hook immediately before outbound mutation.
		8. Executes PUT with Content-Type: text/xml.
		9. Verifies update response.
		"""
		import xml.etree.ElementTree as ET
		from bop_erp.safety import assert_safe_write_target

		# 1. Hard Host Safety Guard
		assert_safe_write_target(self.config.environment, self.config.base_url)

		# 2. Config write permission check
		if not self.config.write_enabled:
			raise PrestaShopValidationError(
				"PrestaShop connector write operations are disabled (write_enabled=0)."
			)

		target_qty = int(quantity)
		exp_prod = int(expected_product_id)
		exp_attr = int(expected_variant_id) if expected_variant_id else 0

		# 3. Read current XML
		xml_text = self.get_stock_available_xml(stock_available_id)
		try:
			root = ET.fromstring(xml_text)
		except Exception as parse_err:
			raise PrestaShopMalformedResponseError(
				f"Failed to parse XML from stock_availables/{stock_available_id}: {parse_err}"
			) from parse_err

		sa_elem = root.find("stock_available")
		if sa_elem is None:
			raise PrestaShopMalformedResponseError(
				f"Expected <stock_available> root child in XML for stock_availables/{stock_available_id}"
			)

		# 4. Identity Mismatch verification
		actual_prod_elem = sa_elem.find("id_product")
		actual_attr_elem = sa_elem.find("id_product_attribute")
		actual_prod = int(actual_prod_elem.text.strip()) if (actual_prod_elem is not None and actual_prod_elem.text) else 0
		actual_attr = int(actual_attr_elem.text.strip()) if (actual_attr_elem is not None and actual_attr_elem.text) else 0

		if actual_prod != exp_prod or actual_attr != exp_attr:
			raise PrestaShopValidationError(
				f"CRITICAL IDENTITY MISMATCH for stock_available {stock_available_id}: "
				f"Expected (product={exp_prod}, attribute={exp_attr}), but remote record has "
				f"(product={actual_prod}, attribute={actual_attr}). Write strictly blocked!"
			)

		# 5. Delta detection / No-Op
		qty_elem = sa_elem.find("quantity")
		if qty_elem is None:
			raise PrestaShopMalformedResponseError(
				f"Missing <quantity> element in stock_availables/{stock_available_id}"
			)
		current_remote_qty = int(qty_elem.text.strip()) if qty_elem.text else 0

		if current_remote_qty == target_qty:
			return {
				"stock_available_id": int(stock_available_id),
				"previous_qty": current_remote_qty,
				"resulting_qty": current_remote_qty,
				"changed": False,
				"reason": "NO_OP_IDENTICAL_QUANTITY",
			}

		# 6. Mutate ONLY <quantity>
		qty_elem.text = str(target_qty)
		put_body = ET.tostring(root, encoding="utf-8")

		# 7. Immediate pre-PUT freshness check hook
		if pre_put_hook is not None:
			pre_put_hook()

		# 8. Execute PUT
		url = self._build_url(f"stock_availables/{stock_available_id}")
		headers = {
			"Content-Type": "text/xml",
			"User-Agent": "Bop-ERP-Connector/1.0",
		}

		try:
			resp = self.session.put(
				url=url,
				data=put_body,
				headers=headers,
				timeout=self.config.timeout_seconds,
				verify=self.config.verify_tls,
			)
		except (requests.ConnectionError, requests.Timeout) as conn_err:
			safe_url = sanitize_url_for_logging(url)
			raise PrestaShopTransientError(
				f"Network connectivity/timeout failure during PUT {safe_url}: {conn_err}",
				sensitive_token=self._api_key,
			) from conn_err
		except requests.RequestException as req_err:
			safe_url = sanitize_url_for_logging(url)
			raise PrestaShopError(
				f"Unexpected HTTP error during PUT {safe_url}: {req_err}",
				sensitive_token=self._api_key,
			) from req_err

		status = resp.status_code
		safe_url = sanitize_url_for_logging(url)

		if status in (401, 403):
			raise PrestaShopAuthError(
				f"Authentication/permission rejected ({status}) for PUT {safe_url}: {resp.text[:200]}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			)
		if status == 404:
			raise PrestaShopNotFoundError(
				f"Resource not found (404) at PUT {safe_url}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			)
		if status == 429:
			retry_after = None
			ra_hdr = resp.headers.get("Retry-After")
			if ra_hdr:
				try:
					retry_after = int(ra_hdr)
				except (ValueError, TypeError):
					pass
			raise PrestaShopRateLimitError(
				f"Rate limit exceeded (429) at PUT {safe_url}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
				retry_after=retry_after,
			)
		if status in (400, 422):
			raise PrestaShopValidationError(
				f"Validation error ({status}) at PUT {safe_url}: {resp.text[:300]}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			)
		if status >= 500:
			raise PrestaShopServerError(
				f"PrestaShop server error ({status}) at PUT {safe_url}: {resp.text[:300]}",
				status_code=status,
				response_body=resp.text,
				sensitive_token=self._api_key,
			)

		# 8. Confirm resulting quantity from PUT response
		try:
			res_root = ET.fromstring(resp.text)
			res_qty_elem = res_root.find("stock_available").find("quantity")
			resulting_qty = int(res_qty_elem.text.strip()) if (res_qty_elem is not None and res_qty_elem.text) else target_qty
		except Exception:
			resulting_qty = target_qty

		return {
			"stock_available_id": int(stock_available_id),
			"previous_qty": current_remote_qty,
			"resulting_qty": resulting_qty,
			"changed": True,
			"reason": "QUANTITY_UPDATED",
		}

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
