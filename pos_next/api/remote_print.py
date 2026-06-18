# -*- coding: utf-8 -*-
# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""
Remote Printing API.

Lets a POS device share its local QZ Tray printers as "remote printers" so
other POS users can send print jobs to them over the existing Frappe
Socket.IO channel.

Flow:
  1. Sharing device calls ``register_remote_printer`` + periodic
     ``heartbeat_remote_printers``.
  2. Other users list printers via ``list_remote_printers`` and pick one.
  3. A print request calls ``create_remote_print_job`` — the backend
     persists a ``POS Remote Print Job`` and emits ``pos_remote_print_job``.
  4. The sharing device receives the realtime event, calls
     ``claim_remote_print_job`` (atomic), fetches the payload via
     ``get_remote_print_job_payload``, prints locally with QZ Tray, then
     reports ``complete_remote_print_job`` or ``fail_remote_print_job``.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _

# A printer is considered online if it sent a heartbeat within this window.
HEARTBEAT_TIMEOUT_SECONDS = 60

DOCTYPE_REMOTE_PRINTER = "POS Remote Printer"
DOCTYPE_REMOTE_PRINT_JOB = "POS Remote Print Job"

VALID_PRINTER_TYPES = ("Receipt", "Closing Report", "General")
JOB_TO_PRINTER_TYPE = {
	"Invoice": "Receipt",
	"Closing Report": "Closing Report",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_datetime():
	return frappe.utils.now_datetime()


def _is_online(last_seen) -> bool:
	if not last_seen:
		return False
	age = (frappe.utils.now_datetime() - frappe.utils.get_datetime(last_seen)).total_seconds()
	return age <= HEARTBEAT_TIMEOUT_SECONDS


def _normalize_allowed_types(
	allowed_types: Any = None,
	printer_type: str | None = None,
) -> list[str]:
	"""Normalize allowed printer types from new or legacy API input."""
	if isinstance(allowed_types, str):
		value = allowed_types.strip()
		if value.startswith("["):
			allowed_types = json.loads(value)
		elif value:
			allowed_types = [item.strip() for item in value.split(",")]
		else:
			allowed_types = []

	if not allowed_types and printer_type:
		allowed_types = [printer_type]
	if not allowed_types:
		allowed_types = ["General"]

	normalized = []
	for value in allowed_types:
		if isinstance(value, dict):
			value = value.get("allowed_type")
		if value not in VALID_PRINTER_TYPES:
			frappe.throw(_("Invalid printer type: {0}").format(value))
		if value not in normalized:
			normalized.append(value)

	return normalized or ["General"]


def _set_allowed_types(doc, allowed_types: list[str]) -> None:
	doc.set("allowed_types", [])
	for allowed_type in allowed_types:
		doc.append("allowed_types", {"allowed_type": allowed_type})
	doc.printer_type = allowed_types[0] if allowed_types else "General"


def _batch_allowed_types(printer_names: list[str]) -> dict[str, list[str]]:
	if not printer_names:
		return {}

	rows = frappe.get_all(
		"POS Remote Printer Allowed Type",
		filters={"parent": ["in", printer_names], "parenttype": DOCTYPE_REMOTE_PRINTER},
		fields=["parent", "allowed_type", "idx"],
		order_by="parent asc, idx asc",
	)

	allowed_by_parent: dict[str, list[str]] = {name: [] for name in printer_names}
	for row in rows:
		if row.allowed_type and row.allowed_type not in allowed_by_parent[row.parent]:
			allowed_by_parent[row.parent].append(row.allowed_type)

	return allowed_by_parent


def _printer_allows_type(allowed_types: list[str], requested_type: str | None) -> bool:
	if not requested_type:
		return True
	return "General" in allowed_types or requested_type in allowed_types


def _validate_pos_profile_access(pos_profile: str | None) -> None:
	"""Ensure the current user can use the given POS Profile (or has POS Settings read)."""
	if not pos_profile:
		return
	if frappe.has_permission("POS Settings", "read"):
		return
	has_access = frappe.db.exists(
		"POS Profile User",
		{"parent": pos_profile, "user": frappe.session.user},
	)
	if not has_access:
		frappe.throw(_("You don't have access to POS Profile {0}").format(pos_profile))


def _resolve_invoice_print_format(pos_profile: str | None) -> str:
	"""Resolve the print format for a Sales Invoice from its POS Profile."""
	default = "POS Next Receipt"
	if not pos_profile:
		return default
	fmt = frappe.db.get_value("POS Profile", pos_profile, "print_format")
	return fmt or default


def _is_raw_print_format(print_format: str) -> bool:
	if not print_format:
		return False
	if "esc" in print_format.lower() and "pos" in print_format.lower():
		return True
	return bool(frappe.db.get_value("Print Format", print_format, "raw_printing"))


# ---------------------------------------------------------------------------
# Printer registration & heartbeat
# ---------------------------------------------------------------------------


@frappe.whitelist()
def register_remote_printer(
	printer_name: str,
	qz_printer_name: str,
	hub_id: str,
	printer_type: str = "General",
	allowed_types: Any = None,
	pos_profile: str | None = None,
):
	"""Create or update a shared remote printer owned by ``hub_id``.

	Deduplication is keyed on ``hub_id`` + ``qz_printer_name`` so a device
	re-registering the same local printer updates the existing record
	instead of creating a duplicate.
	"""
	if not printer_name or not qz_printer_name or not hub_id:
		frappe.throw(_("printer_name, qz_printer_name and hub_id are required"))

	normalized_allowed_types = _normalize_allowed_types(allowed_types, printer_type)

	# Remote printers are device resources, not POS Profile resources. Keep the
	# field empty so newly-created POS Profiles can discover existing printers.
	pos_profile = None

	existing_name = frappe.db.get_value(
		DOCTYPE_REMOTE_PRINTER,
		{"hub_id": hub_id, "qz_printer_name": qz_printer_name},
	)

	now = _now_datetime()

	if existing_name:
		doc = frappe.get_doc(DOCTYPE_REMOTE_PRINTER, existing_name)
		doc.printer_name = printer_name
		_set_allowed_types(doc, normalized_allowed_types)
		doc.pos_profile = pos_profile
		doc.enabled = 1
		doc.last_seen = now
		doc.flags.ignore_permissions = True
		doc.save()
	else:
		doc = frappe.get_doc(
			{
				"doctype": DOCTYPE_REMOTE_PRINTER,
				"printer_name": printer_name,
				"hub_id": hub_id,
				"qz_printer_name": qz_printer_name,
				"printer_type": normalized_allowed_types[0],
				"allowed_types": [
					{"allowed_type": allowed_type} for allowed_type in normalized_allowed_types
				],
				"pos_profile": pos_profile,
				"enabled": 1,
				"last_seen": now,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()

	frappe.db.commit()
	return {
		"name": doc.name,
		"printer_name": doc.printer_name,
		"hub_id": doc.hub_id,
		"printer_type": doc.printer_type,
		"allowed_types": normalized_allowed_types,
	}


@frappe.whitelist()
def heartbeat_remote_printers(hub_id: str):
	"""Refresh ``last_seen`` on every enabled printer owned by ``hub_id``.

	Returns the list of printer names owned by this hub so the frontend can
	confirm its registrations are still recognised server-side.
	"""
	if not hub_id:
		frappe.throw(_("hub_id is required"))

	now = _now_datetime()
	printers = frappe.get_all(
		DOCTYPE_REMOTE_PRINTER,
		filters={"hub_id": hub_id, "enabled": 1},
		fields=["name"],
	)

	for p in printers:
		frappe.db.set_value(
			DOCTYPE_REMOTE_PRINTER,
			p.name,
			"last_seen",
			now,
			update_modified=False,
		)

	frappe.db.commit()

	return {
		"hub_id": hub_id,
		"printers": [p.name for p in printers],
		"last_seen": str(now),
	}


@frappe.whitelist()
def unregister_remote_printer(hub_id: str, qz_printer_name: str | None = None):
	"""Disable (or delete) printer(s) owned by ``hub_id``.

	If ``qz_printer_name`` is given, only that printer is disabled. Otherwise
	all printers owned by the hub are disabled.
	"""
	if not hub_id:
		frappe.throw(_("hub_id is required"))

	filters = {"hub_id": hub_id}
	if qz_printer_name:
		filters["qz_printer_name"] = qz_printer_name

	printers = frappe.get_all(DOCTYPE_REMOTE_PRINTER, filters=filters, pluck="name")
	for name in printers:
		frappe.db.set_value(DOCTYPE_REMOTE_PRINTER, name, "enabled", 0, update_modified=False)

	frappe.db.commit()
	return {"disabled": printers}


# ---------------------------------------------------------------------------
# Printer discovery
# ---------------------------------------------------------------------------


@frappe.whitelist()
def list_remote_printers(
	pos_profile: str | None = None,
	printer_type: str | None = None,
	include_offline: int = 0,
):
	"""Return remote printers available for the current user.

	Printers are visible when:
      - ``enabled`` is set
      - ``last_seen`` is within the heartbeat window (unless include_offline)

	``pos_profile`` is accepted for API compatibility but remote printers are
	listed globally. The selected defaults remain stored per POS Profile in
	``POS Settings``.
	"""
	requested_type = printer_type or None
	if requested_type not in (None, *VALID_PRINTER_TYPES):
		frappe.throw(_("Invalid printer_type: {0}").format(requested_type))

	filters = {"enabled": 1}

	printers = frappe.get_all(
		DOCTYPE_REMOTE_PRINTER,
		filters=filters,
		fields=[
			"name",
			"printer_name",
			"hub_id",
			"qz_printer_name",
			"printer_type",
			"pos_profile",
			"enabled",
			"last_seen",
		],
		order_by="modified desc",
	)

	result = []
	allowed_by_parent = _batch_allowed_types([p.name for p in printers])
	for p in printers:
		allowed_types = allowed_by_parent.get(p.name) or _normalize_allowed_types(printer_type=p.get("printer_type"))
		if not _printer_allows_type(allowed_types, requested_type):
			continue
		online = _is_online(p.get("last_seen"))
		if not include_offline and not online:
			continue
		p["online"] = online
		p["allowed_types"] = allowed_types
		p["printer_type"] = allowed_types[0] if allowed_types else p.get("printer_type")
		result.append(p)

	return result


# ---------------------------------------------------------------------------
# Print job lifecycle
# ---------------------------------------------------------------------------


@frappe.whitelist()
def create_remote_print_job(
	remote_printer: str,
	job_type: str,
	reference_doctype: str,
	reference_name: str,
):
	"""Create a queued print job and emit a realtime event to the owning hub.

	The request returns immediately after persistence + emit, so the cashier
	is never blocked on the remote printer actually finishing.
	"""
	if not remote_printer or not reference_doctype or not reference_name:
		frappe.throw(_("remote_printer, reference_doctype and reference_name are required"))

	if job_type not in ("Invoice", "Closing Report"):
		frappe.throw(_("Invalid job_type: {0}").format(job_type))

	# Verify the printer exists and is enabled.
	printer = frappe.db.get_value(
		DOCTYPE_REMOTE_PRINTER,
		remote_printer,
		["name", "hub_id", "enabled", "printer_type", "pos_profile", "last_seen"],
		as_dict=True,
	)
	if not printer:
		frappe.throw(_("Remote printer {0} not found").format(remote_printer))
	if not printer.enabled:
		frappe.throw(_("Remote printer {0} is disabled").format(remote_printer))
	if not _is_online(printer.last_seen):
		frappe.throw(_("Remote printer {0} is offline").format(remote_printer))

	allowed_types = _batch_allowed_types([remote_printer]).get(remote_printer) or _normalize_allowed_types(
		printer_type=printer.printer_type
	)
	requested_type = JOB_TO_PRINTER_TYPE.get(job_type)
	if not _printer_allows_type(allowed_types, requested_type):
		frappe.throw(_("Remote printer {0} does not allow {1} jobs").format(remote_printer, job_type))

	# Validate the caller can read the referenced document.
	if not frappe.has_permission(reference_doctype, "read", reference_name):
		frappe.throw(_("You do not have access to {0} {1}").format(reference_doctype, reference_name))

	job = frappe.get_doc(
		{
			"doctype": DOCTYPE_REMOTE_PRINT_JOB,
			"remote_printer": remote_printer,
			"job_type": job_type,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"status": "Queued",
		}
	)
	job.flags.ignore_permissions = True
	job.insert()
	frappe.db.commit()

	# Emit after commit so we never broadcast a job that can still roll back.
	frappe.publish_realtime(
		event="pos_remote_print_job",
		message={
			"job": job.name,
			"job_type": job.job_type,
			"reference_doctype": job.reference_doctype,
			"reference_name": job.reference_name,
			"remote_printer": remote_printer,
			"hub_id": printer.hub_id,
			"printer_type": allowed_types[0] if allowed_types else printer.printer_type,
			"allowed_types": allowed_types,
			"requested_by": frappe.session.user,
			"timestamp": frappe.utils.now(),
		},
		user=None,
		after_commit=True,
	)

	return {
		"job": job.name,
		"status": job.status,
		"remote_printer": remote_printer,
		"hub_id": printer.hub_id,
	}


@frappe.whitelist()
def claim_remote_print_job(job_name: str, hub_id: str):
	"""Atomically claim a queued job for a hub.

	Prevents duplicate printing when multiple hubs could theoretically receive
	the same broadcast. Uses a row-level lock + status check.
	"""
	if not job_name or not hub_id:
		frappe.throw(_("job_name and hub_id are required"))

	# Row-level lock to prevent race conditions between competing hubs.
	existing = frappe.db.get_value(
		DOCTYPE_REMOTE_PRINT_JOB,
		job_name,
		["name", "status", "remote_printer"],
		as_dict=True,
		for_update=True,
	)

	if not existing:
		frappe.throw(_("Print job {0} not found").format(job_name))

	if existing.status != "Queued":
		# Already claimed/printed/failed — tell the caller it lost the race.
		return {"claimed": False, "status": existing.status, "job": job_name}

	# Confirm this hub actually owns the printer.
	printer_hub = frappe.db.get_value(DOCTYPE_REMOTE_PRINTER, existing.remote_printer, "hub_id")
	if printer_hub != hub_id:
		return {"claimed": False, "status": "unauthorized", "job": job_name}

	frappe.db.set_value(
		DOCTYPE_REMOTE_PRINT_JOB,
		job_name,
		{"status": "Claimed", "claimed_by_hub": hub_id},
	)
	frappe.db.commit()

	return {"claimed": True, "status": "Claimed", "job": job_name}


@frappe.whitelist()
def get_remote_print_job_payload(job_name: str):
	"""Return job routing metadata for the hub-side local print path."""
	if not job_name:
		frappe.throw(_("job_name is required"))

	job = frappe.db.get_value(
		DOCTYPE_REMOTE_PRINT_JOB,
		job_name,
		["name", "job_type", "reference_doctype", "reference_name", "remote_printer", "status"],
		as_dict=True,
	)
	if not job:
		frappe.throw(_("Print job {0} not found").format(job_name))

	printer = frappe.db.get_value(
		DOCTYPE_REMOTE_PRINTER,
		job.remote_printer,
		["qz_printer_name", "pos_profile"],
		as_dict=True,
	)

	payload: dict = {
		"job": job.name,
		"job_type": job.job_type,
		"reference_doctype": job.reference_doctype,
		"reference_name": job.reference_name,
		"qz_printer_name": printer.qz_printer_name if printer else None,
	}

	return payload


def _render_invoice_payload(invoice_name: str, pos_profile: str | None) -> dict:
	"""Render a Sales Invoice receipt using the POS Profile's print format."""
	print_format = _resolve_invoice_print_format(pos_profile)

	if _is_raw_print_format(print_format):
		raw = frappe.get_attr("frappe.www.printview.get_rendered_raw_commands")(
			doc="Sales Invoice",
			name=invoice_name,
			print_format=print_format,
		)
		raw_commands = raw.get("raw_commands") if isinstance(raw, dict) else None
		if not raw_commands:
			frappe.throw(_("Failed to render raw print commands for {0}").format(invoice_name))
		return {"type": "raw", "raw_commands": raw_commands}

	result = frappe.get_attr("frappe.www.printview.get_html_and_style")(
		doc="Sales Invoice",
		name=invoice_name,
		print_format=print_format,
		no_letterhead=1,
	)
	html = result.get("html") if isinstance(result, dict) else None
	style = result.get("style") if isinstance(result, dict) else ""
	if not html:
		frappe.throw(_("Failed to render invoice print HTML for {0}").format(invoice_name))

	full_html = (
		f"<!DOCTYPE html><html><head><meta charset='UTF-8'>"
		f"<style>{style or ''}</style></head><body>{html}</body></html>"
	)
	return {"type": "html", "html": full_html}


def _render_closing_report_payload(closing_shift_name: str) -> dict:
	"""Render a POS Closing Shift report, reusing the existing shifts API."""
	from pos_next.api.shifts import get_closing_shift_print_html

	result = get_closing_shift_print_html(closing_shift_name)
	if isinstance(result, str):
		return {"type": "html", "html": result}

	if result.get("type") == "raw":
		return {
			"type": "raw",
			"raw_commands": result.get("raw_commands"),
			"html": result.get("fallback_html"),
		}

	return {"type": "html", "html": result.get("html")}

@frappe.whitelist()
def complete_remote_print_job(job_name: str):
	"""Mark a print job as successfully printed."""
	if not job_name:
		frappe.throw(_("job_name is required"))

	frappe.db.set_value(
		DOCTYPE_REMOTE_PRINT_JOB,
		job_name,
		{"status": "Printed", "error_message": None},
	)
	frappe.db.commit()
	return {"job": job_name, "status": "Printed"}


@frappe.whitelist()
def fail_remote_print_job(job_name: str, error_message: str | None = None):
	"""Mark a print job as failed and record the error."""
	if not job_name:
		frappe.throw(_("job_name is required"))

	# Increment attempts atomically.
	job = frappe.get_doc(DOCTYPE_REMOTE_PRINT_JOB, job_name)
	job.status = "Failed"
	job.error_message = (error_message or "")[:1000]
	job.attempts = (job.attempts or 0) + 1
	job.flags.ignore_permissions = True
	job.save()
	frappe.db.commit()
	return {"job": job_name, "status": "Failed", "attempts": job.attempts}


# ---------------------------------------------------------------------------
# Scheduled maintenance
# ---------------------------------------------------------------------------


def mark_stale_printers_offline():
	"""Disable printers that haven't sent a heartbeat in 5 minutes.

	Register as a scheduled task so the printer list stays clean even when
	devices disconnect without calling ``unregister_remote_printers``.
	"""
	threshold = frappe.utils.add_to_date(None, minutes=-5)
	stale = frappe.get_all(
		DOCTYPE_REMOTE_PRINTER,
		filters={"enabled": 1, "last_seen": ["<", threshold]},
		pluck="name",
	)
	for name in stale:
		frappe.db.set_value(DOCTYPE_REMOTE_PRINTER, name, "enabled", 0, update_modified=False)
	if stale:
		frappe.db.commit()
	return len(stale)
