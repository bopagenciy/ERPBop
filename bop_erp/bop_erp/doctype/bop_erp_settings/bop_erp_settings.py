# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

class BopERPSettings(Document):
	def validate(self):
		if (self.integration_max_attempts or 0) < 1:
			self.integration_max_attempts = 5
		if (self.integration_processing_timeout_minutes or 0) < 1:
			self.integration_processing_timeout_minutes = 15
		if (self.integration_metadata_max_length or 0) < 100:
			self.integration_metadata_max_length = 5000

		# Validate retry schedule format
		schedule = self.get_retry_schedule()
		if not schedule:
			self.integration_retry_schedule = "60, 300, 900, 3600, 14400"

	def get_retry_schedule(self):
		raw = self.integration_retry_schedule or "60, 300, 900, 3600, 14400"
		delays = []
		for part in raw.split(","):
			part = part.strip()
			if part.isdigit():
				delays.append(int(part))
		return delays or [60, 300, 900, 3600, 14400]

	def get_backoff_delay(self, attempt_count):
		schedule = self.get_retry_schedule()
		idx = min(max(0, attempt_count - 1), len(schedule) - 1)
		return schedule[idx]

def get_settings():
	return frappe.get_cached_doc("Bop ERP Settings")
