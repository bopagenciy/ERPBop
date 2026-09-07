# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import hashlib
import json
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


def compute_policy_uniqueness_key(warehouse: str, item_code: str = None, item_group: str = None) -> str:
	"""
	Computes a deterministic hash for (warehouse, item_code, item_group) scope.
	"""
	t = [
		str(warehouse or "").strip(),
		str(item_code or "").strip(),
		str(item_group or "").strip(),
	]
	return hashlib.sha256(json.dumps(t, separators=(",", ":")).encode("utf-8")).hexdigest()


class InventoryAvailabilityPolicy(Document):
	def before_validate(self):
		self.populate_and_validate_warehouse()
		self.validate_safety_stock()
		self.compute_uniqueness_key()

	def autoname(self):
		self.populate_and_validate_warehouse()
		self.compute_uniqueness_key()
		if self.item_code:
			self.name = f"IAP-{self.warehouse}-{self.item_code}"
		elif self.item_group:
			self.name = f"IAP-{self.warehouse}-GRP-{self.item_group}"
		else:
			self.name = f"IAP-{self.warehouse}-DEFAULT"

	def validate(self):
		self.populate_and_validate_warehouse()
		self.validate_safety_stock()
		self.validate_item_and_group()
		self.compute_uniqueness_key()

	def populate_and_validate_warehouse(self):
		if not self.warehouse:
			frappe.throw(_("Warehouse is required."))

		wh_data = frappe.db.get_value("Warehouse", self.warehouse, ["name", "company", "is_group"], as_dict=True)
		if not wh_data:
			frappe.throw(_("Warehouse {0} does not exist.").format(self.warehouse))

		if wh_data.is_group:
			frappe.throw(_("Safety stock policy cannot be applied to group warehouse {0}.").format(self.warehouse))

		self.company = wh_data.company

	def validate_safety_stock(self):
		if flt(self.safety_stock_qty) < 0:
			frappe.throw(_("Safety Stock Qty cannot be negative."))

	def validate_item_and_group(self):
		if self.item_code and not frappe.db.exists("Item", self.item_code):
			frappe.throw(_("Item {0} does not exist.").format(self.item_code))

		if self.item_group and not frappe.db.exists("Item Group", self.item_group):
			frappe.throw(_("Item Group {0} does not exist.").format(self.item_group))

	def compute_uniqueness_key(self):
		if self.warehouse:
			self.unique_policy_key = compute_policy_uniqueness_key(
				self.warehouse, self.item_code, self.item_group
			)
