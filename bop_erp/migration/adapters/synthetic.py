# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import copy
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from bop_erp.migration.adapters.base import SourceAdapter
from bop_erp.migration.safety import (
	assert_read_only_http_method,
	assert_read_only_sql,
	assert_safe_source_target,
)


class SyntheticSourceAdapter(SourceAdapter):
	"""
	Deterministic, in-memory SourceAdapter implementation for isolated unit and live testing.
	Exposes predefined or custom synthetic datasets without contacting external infrastructure.
	"""

	def __init__(
		self,
		source_system: str = "SYNTHETIC",
		source_instance_id: str = "DEFAULT",
		source_schema_version: Optional[str] = "1.0.0-mock",
	):
		super().__init__(
			source_system=source_system,
			source_instance_id=source_instance_id,
			source_schema_version=source_schema_version,
		)
		self.customers: List[Dict[str, Any]] = []
		self.vendors: List[Dict[str, Any]] = []
		self.items: List[Dict[str, Any]] = []
		self.warehouses: List[Dict[str, Any]] = []
		self._populate_default_fixtures()

	def _populate_default_fixtures(self) -> None:
		self.customers = [
			{
				"id": "CUST-SYN-001",
				"name": "Acme Industrial Supplies",
				"type": "Company",
				"email": "purchasing@acme-industrial.com",
				"phone": "+1-555-0199",
				"tax_id": "12-3456789",
				"currency": "COP",
				"payment_terms": "Net 30",
				"billing_address": {
					"address_line1": "100 Industrial Parkway",
					"city": "Miami",
					"state": "FL",
					"country": "Colombia",
					"pincode": "33101",
				},
				"shipping_address": {
					"address_line1": "100 Industrial Parkway Dock B",
					"city": "Miami",
					"state": "FL",
					"country": "Colombia",
					"pincode": "33101",
				},
			},
			{
				"id": "CUST-SYN-002",
				"name": "John Doe Contractors",
				"type": "Individual",
				"email": "johndoe@contractors.net",
				"phone": "+1-555-0200",
				"tax_id": "98-7654321",
				"currency": "COP",
				"payment_terms": "Due on Receipt",
			},
		]

		self.vendors = [
			{
				"id": "VEND-SYN-001",
				"name": "Global Fasteners Mfg",
				"tax_id": "88-1234567",
				"email": "sales@globalfasteners.com",
				"phone": "+1-555-0301",
				"currency": "COP",
				"payment_terms": "Net 60",
			},
			{
				"id": "VEND-SYN-002",
				"name": "Apex Tool & Die",
				"tax_id": "99-7654321",
				"email": "orders@apextool.com",
				"phone": "+1-555-0302",
				"currency": "COP",
				"payment_terms": "Net 30",
			},
		]

		self.items = [
			{
				"id": "ITEM-SYN-001",
				"sku": "SKU-SYN-BOLT-100",
				"name": "Heavy Duty Steel Hex Bolt 1/2x3",
				"description": "Grade 8 Zinc Plated Steel Hex Bolt",
				"uom": "Nos",
				"item_group": "All Item Groups",
				"is_stock": True,
				"is_serial": False,
				"is_batch": True,
			},
			{
				"id": "ITEM-SYN-002",
				"sku": "SKU-SYN-DRILL-900",
				"name": "Cordless Industrial Rotary Hammer Drill",
				"description": "Brushless 20V Rotary Hammer Drill with Hard Case",
				"uom": "Nos",
				"item_group": "All Item Groups",
				"is_stock": True,
				"is_serial": True,
				"is_batch": False,
			},
		]

		self.warehouses = [
			{
				"id": "WH-SYN-01",
				"name": "Central Distribution Hub",
				"parent_id": None,
				"is_group": False,
			},
			{
				"id": "WH-SYN-02",
				"name": "Secondary Yard Annex",
				"parent_id": "WH-SYN-01",
				"is_group": False,
			},
		]

	def health_check(self) -> Dict[str, Any]:
		return {
			"status": "HEALTHY",
			"source_system": self.source_system,
			"source_instance_id": self.source_instance_id,
			"timestamp": datetime.now(timezone.utc).isoformat(),
			"read_only": True,
		}

	def get_source_metadata(self) -> Dict[str, Any]:
		return {
			"source_system": self.source_system,
			"source_instance_id": self.source_instance_id,
			"source_schema_version": self.source_schema_version,
			"capabilities": ["CUSTOMERS", "VENDORS", "ITEMS", "WAREHOUSES"],
			"customer_count": len(self.customers),
			"vendor_count": len(self.vendors),
			"item_count": len(self.items),
			"warehouse_count": len(self.warehouses),
		}

	def _paginate(self, records: List[Dict[str, Any]], limit: Optional[int], offset: int) -> Iterator[Dict[str, Any]]:
		slice_ = records[offset:]
		if limit is not None:
			slice_ = slice_[:limit]
		for record in slice_:
			yield copy.deepcopy(record)

	def stream_customers(
		self,
		limit: Optional[int] = None,
		offset: int = 0,
		filters: Optional[Dict[str, Any]] = None,
	) -> Iterator[Dict[str, Any]]:
		self.snapshot_started_at = datetime.now(timezone.utc)
		yield from self._paginate(self.customers, limit, offset)
		self.snapshot_completed_at = datetime.now(timezone.utc)

	def stream_vendors(
		self,
		limit: Optional[int] = None,
		offset: int = 0,
		filters: Optional[Dict[str, Any]] = None,
	) -> Iterator[Dict[str, Any]]:
		self.snapshot_started_at = datetime.now(timezone.utc)
		yield from self._paginate(self.vendors, limit, offset)
		self.snapshot_completed_at = datetime.now(timezone.utc)

	def stream_items(
		self,
		limit: Optional[int] = None,
		offset: int = 0,
		filters: Optional[Dict[str, Any]] = None,
	) -> Iterator[Dict[str, Any]]:
		self.snapshot_started_at = datetime.now(timezone.utc)
		yield from self._paginate(self.items, limit, offset)
		self.snapshot_completed_at = datetime.now(timezone.utc)

	def stream_warehouses(
		self,
		limit: Optional[int] = None,
		offset: int = 0,
		filters: Optional[Dict[str, Any]] = None,
	) -> Iterator[Dict[str, Any]]:
		self.snapshot_started_at = datetime.now(timezone.utc)
		yield from self._paginate(self.warehouses, limit, offset)
		self.snapshot_completed_at = datetime.now(timezone.utc)

	def attempt_write_sql(self, sql_query: str) -> None:
		"""Attempts to execute a SQL query, triggering the read-only safety classifier."""
		assert_read_only_sql(sql_query)

	def attempt_write_http(self, method: str, url: str) -> None:
		"""Attempts to issue an HTTP call, triggering the read-only verb and target safety gates."""
		assert_read_only_http_method(method)
		assert_safe_source_target(url)

	def _run_safe_sql(self, sql_query: str, params: Optional[Any] = None) -> Any:
		return [{"result": "safe_mock_execution", "query": sql_query}]

	def _run_safe_get(
		self,
		url: str,
		params: Optional[Dict[str, Any]] = None,
		headers: Optional[Dict[str, Any]] = None,
	) -> Any:
		return {"status": 200, "url": url, "mock": True}
