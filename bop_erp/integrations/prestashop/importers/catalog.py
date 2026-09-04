# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Dict, Any, Optional
import frappe
from frappe import _

from bop_erp.integrations.prestashop.importers.base import BaseImporter, ImportResult
from bop_erp.integrations.prestashop.importers.categories import CategoryImporter
from bop_erp.integrations.prestashop.importers.attributes import AttributeImporter
from bop_erp.integrations.prestashop.importers.products import ProductImporter


class CatalogImporter:
	"""
	Authoritative PrestaShop Inbound Catalog Orchestrator.
	Coordinates:
	1. Category Synchronization (PrestaShop Category -> native ERPNext Item Group)
	2. Attribute Synchronization (PrestaShop Product Options -> native ERPNext Item Attribute)
	3. Product Synchronization (PrestaShop Products -> native simple/variant ERPNext Items)

	Guarantees:
	- Respects dry_run flag: when True, zero DB writes are executed.
	- Failure isolation: errors on individual records do not abort the entire run.
	- Aggregates structured execution counters and errors across all phases.
	"""

	def __init__(self, client, sales_channel: str, dry_run: bool = False):
		self.client = client
		self.sales_channel = sales_channel
		self.dry_run = dry_run
		self.category_importer = CategoryImporter(client, sales_channel, dry_run=dry_run)
		self.attribute_importer = AttributeImporter(client, sales_channel, dry_run=dry_run)
		self.product_importer = ProductImporter(client, sales_channel, dry_run=dry_run)

	def run(self) -> Dict[str, Any]:
		"""Executes the complete catalog import pipeline."""
		report = {
			"sales_channel": self.sales_channel,
			"dry_run": self.dry_run,
			"categories": None,
			"attributes": None,
			"products": None,
			"success": True,
		}

		# Phase 1: Categories
		cat_res = self.category_importer.import_categories()
		report["categories"] = cat_res.to_dict()

		# Phase 2: Attributes
		attr_res = self.attribute_importer.import_attributes()
		report["attributes"] = attr_res.to_dict()

		# Phase 3: Products (Simple & Variants)
		prod_res = self.product_importer.import_products()
		report["products"] = prod_res.to_dict()

		total_failed = cat_res.failed + attr_res.failed + prod_res.failed
		report["success"] = (total_failed == 0)
		report["total_seen"] = cat_res.seen + attr_res.seen + prod_res.seen
		report["total_created"] = cat_res.created + attr_res.created + prod_res.created
		report["total_updated"] = cat_res.updated + attr_res.updated + prod_res.updated
		report["total_unchanged"] = cat_res.unchanged + attr_res.unchanged + prod_res.unchanged
		report["total_failed"] = total_failed

		return report
