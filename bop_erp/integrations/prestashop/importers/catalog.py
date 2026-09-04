# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Dict, Any, Optional, Set, List
import frappe
from frappe import _

from bop_erp.integrations.prestashop.importers.base import BaseImporter, ImportResult
from bop_erp.integrations.prestashop.importers.categories import CategoryImporter
from bop_erp.integrations.prestashop.importers.attributes import AttributeImporter
from bop_erp.integrations.prestashop.importers.products import ProductImporter
from bop_erp.integrations.prestashop.importers.presentation import PresentationImporter


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
	- Explicit 4-tier structured execution accounting (categories, attributes, products, variants).
	- Explicit mapping metrics (created, reused, updated).
	"""

	def __init__(
		self,
		client,
		sales_channel: str,
		dry_run: bool = False,
		allow_trusted_sku_reuse: bool = True,
		sync_presentation: bool = True,
	):
		self.client = client
		self.sales_channel = sales_channel
		self.dry_run = dry_run
		self.sync_presentation = sync_presentation
		self.category_importer = CategoryImporter(client, sales_channel, dry_run=dry_run)
		self.attribute_importer = AttributeImporter(client, sales_channel, dry_run=dry_run)
		self.product_importer = ProductImporter(
			client, sales_channel, dry_run=dry_run, allow_trusted_sku_reuse=allow_trusted_sku_reuse
		)
		self.presentation_importer = PresentationImporter(client, sales_channel, dry_run=dry_run)

	def run(
		self,
		category_ids: Optional[Set[str]] = None,
		product_ids: Optional[Set[str]] = None,
		sku_prefix_filter: Optional[str] = None,
	) -> Dict[str, Any]:
		"""Executes the complete catalog import pipeline."""
		report = {
			"sales_channel": self.sales_channel,
			"dry_run": self.dry_run,
			"categories": None,
			"attributes": None,
			"products": None,
			"variants": None,
			"channel_categories": None,
			"presentations": None,
			"mappings": None,
			"success": True,
		}

		# Phase 1: Categories
		cat_res = self.category_importer.import_categories(category_ids=category_ids)
		report["categories"] = cat_res.to_dict()

		# Phase 2: Attributes
		attr_res = self.attribute_importer.import_attributes()
		report["attributes"] = attr_res.to_dict()

		# Phase 3: Products (Simple & Variants)
		prod_res, var_res = self.product_importer.import_products(
			product_ids=product_ids, sku_prefix_filter=sku_prefix_filter
		)
		report["products"] = prod_res.to_dict()
		report["variants"] = var_res.to_dict()

		# Phase 4: Presentations & Media (Phase 1F)
		total_pres_failed = 0
		if self.sync_presentation:
			channel_cat_res = self.presentation_importer.sync_channel_categories()
			report["channel_categories"] = channel_cat_res.to_dict()

			pres_res = self.presentation_importer.sync_product_presentation_and_media()
			report["presentations"] = pres_res.to_dict()
			total_pres_failed = channel_cat_res.failed + pres_res.failed

		# Mapping metrics across all importers
		total_map_created = (
			self.category_importer.mappings_created + self.product_importer.mappings_created
		)
		total_map_reused = (
			self.category_importer.mappings_reused + self.product_importer.mappings_reused
		)
		total_map_updated = (
			self.category_importer.mappings_updated + self.product_importer.mappings_updated
		)
		report["mappings"] = {
			"created": total_map_created,
			"reused": total_map_reused,
			"updated": total_map_updated,
		}

		total_failed = cat_res.failed + attr_res.failed + prod_res.failed + var_res.failed + total_pres_failed
		report["success"] = (total_failed == 0)
		report["total_failed"] = total_failed

		return report
