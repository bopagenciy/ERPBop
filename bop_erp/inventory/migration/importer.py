# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

import csv
import hashlib
import json
from io import StringIO
from typing import Dict, List, Any, Optional

import frappe
from frappe.utils import flt, nowdate, nowtime


class MigrationImporter:
	"""
	Parses and stages normalized opening inventory records from CSV or dict data.
	Computes deterministic input hashes and ensures batch/row idempotency.
	"""

	REQUIRED_COLUMNS = ["source_record_id", "item_code", "warehouse", "quantity", "valuation_rate"]

	@classmethod
	def compute_payload_hash(cls, records: List[Dict[str, Any]]) -> str:
		"""Computes deterministic SHA-256 hash across sorted normalized records."""
		canonical = []
		for r in records:
			norm = [
				str(r.get("source_record_id", "")).strip(),
				str(r.get("item_code", "")).strip(),
				str(r.get("warehouse", "")).strip(),
				flt(r.get("quantity", 0)),
				flt(r.get("valuation_rate", 0)),
				str(r.get("stock_uom", "")).strip(),
				str(r.get("batch_no", "")).strip(),
				str(r.get("serial_no", "")).strip(),
			]
			canonical.append(norm)
		canonical.sort(key=lambda x: (x[0], x[1], x[2]))
		canonical_json = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
		return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

	@classmethod
	def compute_batch_rows_hash(cls, batch_name: str) -> str:
		"""
		Recomputes deterministic SHA-256 payload hash directly from the current staged
		Inventory Migration Row records in the database.
		"""
		rows = frappe.get_all(
			"Inventory Migration Row",
			filters={"batch": batch_name},
			fields=[
				"source_record_id",
				"item_code",
				"warehouse",
				"quantity",
				"valuation_rate",
				"stock_uom",
				"batch_no",
				"serial_no",
			],
		)
		return cls.compute_payload_hash(rows)

	@classmethod
	def parse_csv(cls, csv_text: str) -> List[Dict[str, Any]]:
		"""Parses CSV string into a list of record dicts."""
		reader = csv.DictReader(StringIO(csv_text.strip()))
		if not reader.fieldnames:
			raise frappe.ValidationError("CSV file is empty or missing headers.")

		missing = [col for col in cls.REQUIRED_COLUMNS if col not in reader.fieldnames]
		if missing:
			raise frappe.ValidationError(f"CSV is missing required column(s): {', '.join(missing)}")

		records = []
		for idx, row in enumerate(reader, start=2):
			rec = {k.strip(): (v.strip() if v else "") for k, v in row.items()}
			rec["_row_num"] = idx
			records.append(rec)
		return records

	@classmethod
	def stage_batch(
		cls,
		batch_id: str,
		company: str,
		records: List[Dict[str, Any]],
		source_system: str = "GENERIC_CSV",
		posting_date: Optional[str] = None,
		posting_time: Optional[str] = None,
		notes: Optional[str] = None,
		opening_difference_account: Optional[str] = None,
	) -> str:
		"""
		Creates an Inventory Migration Batch and stages all Inventory Migration Row records.
		If batch already exists and is not APPLIED, updates rows idempotently.
		"""
		if not records:
			raise frappe.ValidationError("No inventory migration records provided.")

		input_hash = cls.compute_payload_hash(records)
		post_date = posting_date or nowdate()
		post_time = posting_time or nowtime()

		if frappe.db.exists("Inventory Migration Batch", {"batch_id": batch_id}):
			batch = frappe.get_doc("Inventory Migration Batch", {"batch_id": batch_id})
			if batch.status == "APPLIED":
				raise frappe.ValidationError(f"Migration batch '{batch_id}' has already been APPLIED and cannot be restaged.")
			batch.company = company
			batch.source_system = source_system
			batch.posting_date = post_date
			batch.posting_time = post_time
			batch.input_hash = input_hash
			batch.status = "DRAFT"
			batch.notes = notes
			if opening_difference_account:
				batch.opening_difference_account = opening_difference_account
			batch.save(ignore_permissions=True)
			# Delete old unapplied rows
			frappe.db.delete("Inventory Migration Row", {"batch": batch.name})
		else:
			batch = frappe.get_doc({
				"doctype": "Inventory Migration Batch",
				"batch_id": batch_id,
				"source_system": source_system,
				"company": company,
				"posting_date": post_date,
				"posting_time": post_time,
				"input_hash": input_hash,
				"opening_difference_account": opening_difference_account,
				"status": "DRAFT",
				"notes": notes,
			}).insert(ignore_permissions=True)

		# Stage rows
		total = len(records)
		for r in records:
			qty_raw = r.get("quantity", 0)
			rate_raw = r.get("valuation_rate", 0)
			try:
				qty = flt(qty_raw)
			except Exception:
				qty = 0.0

			try:
				rate = flt(rate_raw)
			except Exception:
				rate = 0.0

			frappe.get_doc({
				"doctype": "Inventory Migration Row",
				"batch": batch.name,
				"source_record_id": str(r.get("source_record_id", "")).strip(),
				"item_code": str(r.get("item_code", "")).strip(),
				"warehouse": str(r.get("warehouse", "")).strip(),
				"quantity": qty,
				"valuation_rate": rate,
				"stock_uom": r.get("stock_uom"),
				"batch_no": r.get("batch_no"),
				"serial_no": r.get("serial_no"),
				"status": "DRAFT",
			}).insert(ignore_permissions=True)

		batch.total_rows = total
		batch.valid_rows = 0
		batch.error_rows = 0
		batch.save(ignore_permissions=True)
		frappe.db.commit()
		return batch.name
