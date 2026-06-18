from __future__ import annotations

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "POS Remote Printer"):
		return

	if not frappe.db.exists("DocType", "POS Remote Printer Allowed Type"):
		return

	printers = frappe.get_all(
		"POS Remote Printer",
		fields=["name", "printer_type"],
	)

	for printer in printers:
		if frappe.db.exists(
			"POS Remote Printer Allowed Type",
			{"parent": printer.name, "parenttype": "POS Remote Printer"},
		):
			continue

		allowed_type = printer.printer_type or "General"
		if allowed_type not in ("Receipt", "Closing Report", "General"):
			allowed_type = "General"

		doc = frappe.get_doc("POS Remote Printer", printer.name)
		doc.append("allowed_types", {"allowed_type": allowed_type})
		doc.flags.ignore_permissions = True
		doc.save()
