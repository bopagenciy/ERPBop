# Copyright (c) 2026, Bop Agency and Contributors
# See license.txt

from typing import Any, Dict, List, Optional
from datetime import timedelta
import frappe
from frappe import _
from frappe.utils import now_datetime, get_datetime

from bop_erp.constants import IntegrationStatus, IntegrationReadinessStatus


class ThresholdSeverity:
	OK = "OK"
	WARNING = "WARNING"
	CRITICAL = "CRITICAL"


DEFAULT_THRESHOLDS = {
	"pending_event_max_count": 50,
	"retry_pending_max_count": 20,
	"dead_letter_max_count": 0,       # Any dead letter triggers warning/critical
	"failed_review_order_max_count": 0,
	"stale_processing_max_count": 0,
	"max_pending_age_minutes": 30,
}


def get_system_health() -> Dict[str, Any]:
	"""
	Lightweight system health check.
	Reports connectivity and liveness of fundamental dependencies:
	- MariaDB database
	- Redis cache
	- Redis queue
	Returns status: 'UP' or 'DOWN', with boolean flags and error messages.
	Exposes ZERO credentials or secrets.
	"""
	health = {
		"status": "UP",
		"database": {"alive": False, "error": None},
		"redis_cache": {"alive": False, "error": None},
		"redis_queue": {"alive": False, "error": None},
		"timestamp": str(now_datetime()),
	}

	# 1. MariaDB Database Check
	try:
		frappe.db.sql("SELECT 1")
		health["database"]["alive"] = True
	except Exception as e:
		health["database"]["alive"] = False
		health["database"]["error"] = str(e)
		health["status"] = "DOWN"

	# 2. Redis Cache Check
	try:
		frappe.cache().ping()
		health["redis_cache"]["alive"] = True
	except Exception as e:
		health["redis_cache"]["alive"] = False
		health["redis_cache"]["error"] = str(e)
		health["status"] = "DOWN"

	# 3. Redis Queue Check
	try:
		import redis
		conf = frappe.get_site_config()
		q_url = conf.get("redis_queue") or "redis://redis-queue:6379"
		r_q = redis.from_url(q_url, socket_timeout=2)
		r_q.ping()
		health["redis_queue"]["alive"] = True
	except Exception as e:
		health["redis_queue"]["alive"] = False
		health["redis_queue"]["error"] = str(e)
		health["status"] = "DOWN"

	return health


def get_system_readiness(thresholds: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
	"""
	Authoritative system readiness evaluation.
	Separates simple process liveness (health) from whether the system is
	ready to safely process orders and publications without accumulating backlog or running stale.

	Evaluates:
	1. Underlying dependencies health (DB, Redis)
	2. Integration Event queues (PENDING, RETRY_PENDING, DEAD_LETTER, stale PROCESSING)
	3. Sales Orders requiring operational review (FAILED_REVIEW)
	4. Threshold severity classification (OK, WARNING, CRITICAL)
	"""
	th = dict(DEFAULT_THRESHOLDS)
	if thresholds:
		th.update(thresholds)

	health = get_system_health()
	now = now_datetime()

	readiness = {
		"ready": False,
		"severity": ThresholdSeverity.OK,
		"health": health,
		"metrics": {
			"pending_events": 0,
			"retry_pending_events": 0,
			"dead_letter_events": 0,
			"stale_processing_events": 0,
			"failed_review_orders": 0,
			"oldest_pending_age_minutes": 0,
		},
		"violations": [],
		"timestamp": str(now),
	}

	if health["status"] != "UP":
		readiness["ready"] = False
		readiness["severity"] = ThresholdSeverity.CRITICAL
		readiness["violations"].append("Core dependencies DOWN")
		return readiness

	# Query queue counts
	counts = frappe.db.sql(
		"""
		SELECT status, count(*) as cnt
		FROM `tabIntegration Event`
		GROUP BY status
		""",
		as_dict=True,
	)
	status_map = {row["status"]: row["cnt"] for row in counts}

	readiness["metrics"]["pending_events"] = status_map.get(IntegrationStatus.PENDING, 0)
	readiness["metrics"]["retry_pending_events"] = status_map.get(IntegrationStatus.RETRY_PENDING, 0)
	readiness["metrics"]["dead_letter_events"] = status_map.get(IntegrationStatus.DEAD_LETTER, 0)

	# Query stale processing events
	stale_cnt = frappe.db.sql(
		"""
		SELECT count(*)
		FROM `tabIntegration Event`
		WHERE status = %s
		  AND lease_expires_at IS NOT NULL
		  AND lease_expires_at < %s
		""",
		(IntegrationStatus.PROCESSING, now),
	)[0][0]
	readiness["metrics"]["stale_processing_events"] = stale_cnt

	# Query failed review orders
	failed_review_cnt = frappe.db.count(
		"Sales Order",
		{"integration_status": IntegrationReadinessStatus.FAILED_REVIEW},
	)
	readiness["metrics"]["failed_review_orders"] = failed_review_cnt

	# Oldest pending event age
	oldest_pending = frappe.db.sql(
		"""
		SELECT creation
		FROM `tabIntegration Event`
		WHERE status = %s
		ORDER BY creation ASC
		LIMIT 1
		""",
		(IntegrationStatus.PENDING,),
	)
	if oldest_pending and oldest_pending[0][0]:
		age_min = (now - get_datetime(oldest_pending[0][0])).total_seconds() / 60.0
		readiness["metrics"]["oldest_pending_age_minutes"] = round(age_min, 1)

	# Evaluate threshold violations
	violations = []
	severity = ThresholdSeverity.OK

	if readiness["metrics"]["dead_letter_events"] > th["dead_letter_max_count"]:
		violations.append(f"Dead letter backlog present: {readiness['metrics']['dead_letter_events']}")
		severity = ThresholdSeverity.CRITICAL

	if readiness["metrics"]["failed_review_orders"] > th["failed_review_order_max_count"]:
		violations.append(f"Orders requiring manual review: {readiness['metrics']['failed_review_orders']}")
		severity = ThresholdSeverity.CRITICAL

	if readiness["metrics"]["stale_processing_events"] > th["stale_processing_max_count"]:
		violations.append(f"Stale processing events detected: {readiness['metrics']['stale_processing_events']}")
		if severity != ThresholdSeverity.CRITICAL:
			severity = ThresholdSeverity.WARNING

	if readiness["metrics"]["pending_events"] > th["pending_event_max_count"]:
		violations.append(f"Pending event backlog high: {readiness['metrics']['pending_events']}")
		if severity != ThresholdSeverity.CRITICAL:
			severity = ThresholdSeverity.WARNING

	if readiness["metrics"]["oldest_pending_age_minutes"] > th["max_pending_age_minutes"]:
		violations.append(f"Pending event age high: {readiness['metrics']['oldest_pending_age_minutes']}m")
		if severity != ThresholdSeverity.CRITICAL:
			severity = ThresholdSeverity.WARNING

	readiness["violations"] = violations
	readiness["severity"] = severity
	readiness["ready"] = (severity == ThresholdSeverity.OK)

	return readiness
