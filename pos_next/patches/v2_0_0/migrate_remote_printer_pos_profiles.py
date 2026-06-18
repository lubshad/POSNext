from __future__ import annotations

import frappe


def execute() -> None:
	"""Move legacy POS Remote Printer.pos_profile values into child table rows.

	Why: POS Remote Printer now supports multiple POS Profiles through the
	POS Remote Printer POS Profile child table. Existing single-link values must
	be preserved; empty legacy values intentionally remain empty to mean global.
	"""
	if not frappe.db.exists("DocType", "POS Remote Printer"):
		return
	if not frappe.db.exists("DocType", "POS Remote Printer POS Profile"):
		return
	if not frappe.db.has_column("POS Remote Printer", "pos_profile"):
		return

	printers = frappe.get_all("POS Remote Printer", fields=["name", "pos_profile"])
	for printer in printers:
		if not printer.pos_profile:
			continue
		if frappe.db.exists(
			"POS Remote Printer POS Profile",
			{
				"parent": printer.name,
				"parenttype": "POS Remote Printer",
				"pos_profile": printer.pos_profile,
			},
		):
			continue
		doc = frappe.get_doc("POS Remote Printer", printer.name)
		doc.append("pos_profiles", {"pos_profile": printer.pos_profile})
		doc.flags.ignore_permissions = True
		doc.save()
