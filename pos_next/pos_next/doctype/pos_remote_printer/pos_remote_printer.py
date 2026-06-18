# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document


class POSRemotePrinter(Document):
	# begin: auto-generated types
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from pos_next.pos_next.doctype.pos_remote_printer_allowed_type.pos_remote_printer_allowed_type import POSRemotePrinterAllowedType
		from frappe.types import DF

		allowed_types: DF.Table[POSRemotePrinterAllowedType]
		printer_name: DF.Data | None
		hub_id: DF.Data | None
		qz_printer_name: DF.Data | None
		printer_type: DF.Literal["Receipt", "Closing Report", "General"]
		pos_profile: DF.Link | None
		enabled: DF.Check
		last_seen: DF.Datetime | None
	# end: auto-generated types
	pass
