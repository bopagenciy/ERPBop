# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class MigrationRunStatus:
	DRAFT = "DRAFT"
	EXTRACTING = "EXTRACTING"
	STAGED = "STAGED"
	VALIDATING = "VALIDATING"
	READY = "READY"
	IMPORTING = "IMPORTING"
	RECONCILING = "RECONCILING"
	COMPLETED = "COMPLETED"
	FAILED = "FAILED"
	REVIEW_REQUIRED = "REVIEW_REQUIRED"
	CANCELLED = "CANCELLED"

	ALL = (
		DRAFT,
		EXTRACTING,
		STAGED,
		VALIDATING,
		READY,
		IMPORTING,
		RECONCILING,
		COMPLETED,
		FAILED,
		REVIEW_REQUIRED,
		CANCELLED,
	)

	ALLOWED_TRANSITIONS = {
		DRAFT: {EXTRACTING, CANCELLED, FAILED},
		EXTRACTING: {STAGED, FAILED, CANCELLED},
		STAGED: {VALIDATING, FAILED, CANCELLED},
		VALIDATING: {READY, REVIEW_REQUIRED, FAILED, CANCELLED},
		READY: {IMPORTING, RECONCILING, REVIEW_REQUIRED, CANCELLED},
		IMPORTING: {RECONCILING, COMPLETED, REVIEW_REQUIRED, FAILED},
		RECONCILING: {COMPLETED, REVIEW_REQUIRED, FAILED},
		REVIEW_REQUIRED: {VALIDATING, CANCELLED, FAILED},
		COMPLETED: set(),
		FAILED: set(),
		CANCELLED: set(),
	}


class MigrationRun(Document):
	def validate(self):
		self.validate_status_transition()
		self.validate_company()

	def validate_company(self):
		if self.company and not frappe.db.exists("Company", self.company):
			frappe.throw(_("Company '{0}' does not exist.").format(self.company), frappe.ValidationError)

	def validate_status_transition(self):
		if not self.is_new():
			old_status = frappe.db.get_value("Migration Run", self.name, "status")
			if old_status and old_status != self.status:
				allowed = MigrationRunStatus.ALLOWED_TRANSITIONS.get(old_status, set())
				if self.status not in allowed:
					frappe.throw(
						_("Invalid status transition for Migration Run from '{0}' to '{1}'.").format(
							old_status, self.status
						),
						frappe.ValidationError,
					)
