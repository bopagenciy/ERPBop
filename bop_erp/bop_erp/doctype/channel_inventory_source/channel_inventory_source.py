# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import frappe
from frappe import _
from frappe.model.document import Document
from bop_erp.inventory.exceptions import ChannelCompanyMismatchError


def compute_channel_inventory_source_key(sales_channel: str, warehouse: str) -> str:
	"""
	Computes a deterministic unique hash for active [sales_channel, warehouse] pairs.
	Uses canonical JSON tuple serialization.
	"""
	t = [str(sales_channel).strip(), str(warehouse).strip()]
	return hashlib.sha256(json.dumps(t, separators=(",", ":")).encode("utf-8")).hexdigest()


class ChannelInventorySource(Document):
	def before_validate(self):
		self.populate_and_validate_company()
		self.compute_uniqueness_key()

	def autoname(self):
		self.populate_and_validate_company()
		self.compute_uniqueness_key()
		base_name = f"CIS-{self.sales_channel}-{self.warehouse}"
		self.name = base_name

	def validate(self):
		self.populate_and_validate_company()
		self.compute_uniqueness_key()

	def populate_and_validate_company(self):
		if not self.warehouse:
			frappe.throw(_("Warehouse is required."))
		if not self.sales_channel:
			frappe.throw(_("Sales Channel is required."))

		wh_company = frappe.db.get_value("Warehouse", self.warehouse, "company")
		if not wh_company:
			frappe.throw(_("Warehouse {0} does not belong to any Company.").format(self.warehouse))

		ch_company = frappe.db.get_value("Sales Channel", self.sales_channel, "company")
		if not ch_company:
			frappe.throw(_("Sales Channel {0} does not belong to any Company.").format(self.sales_channel))

		if wh_company != ch_company:
			raise ChannelCompanyMismatchError(
				_("Company mismatch: Sales Channel '{0}' belongs to '{1}', but Warehouse '{2}' belongs to '{3}'. Cross-company inventory sourcing is not permitted.").format(
					self.sales_channel, ch_company, self.warehouse, wh_company
				)
			)

		self.company = wh_company

	def compute_uniqueness_key(self):
		if self.sales_channel and self.warehouse:
			self.unique_source_key = compute_channel_inventory_source_key(self.sales_channel, self.warehouse)
