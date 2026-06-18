# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class POSRemotePrinterAllowedType(Document):
	# begin: auto-generated types
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		allowed_type: DF.Literal["Receipt", "Closing Report", "General"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
	# end: auto-generated types
	pass
