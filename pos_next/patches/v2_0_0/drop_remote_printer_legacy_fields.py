from __future__ import annotations

import frappe


def execute() -> None:
	"""Drop legacy POS Remote Printer columns after child-table migrations.

	Why: the DocType no longer exposes printer_type or the single pos_profile
	Link. Frappe does not always drop removed DocField columns during sync, so
	this patch physically removes the stale columns after their data has been
	migrated into allowed_types and pos_profiles child tables.
	"""
	if not frappe.db.exists("DocType", "POS Remote Printer"):
		return

	for fieldname in ("printer_type", "pos_profile"):
		if frappe.db.has_column("POS Remote Printer", fieldname):
			frappe.db.sql_ddl(f"ALTER TABLE `tabPOS Remote Printer` DROP COLUMN `{fieldname}`")
