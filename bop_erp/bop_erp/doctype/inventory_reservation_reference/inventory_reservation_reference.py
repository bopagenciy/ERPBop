# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class InventoryReservationReference(Document):
	def validate(self):
		if not self.idempotency_key:
			frappe.throw(_("Idempotency Key is required."))

		if not self.item_code:
			frappe.throw(_("Item Code is required."))

		if not self.warehouse:
			frappe.throw(_("Warehouse is required."))

		if flt(self.reserved_qty) <= 0:
			frappe.throw(_("Reserved Qty must be greater than 0."))
