# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document


class POSRemotePrintJob(Document):
	# begin: auto-generated types
	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		remote_printer: DF.Link | None
		job_type: DF.Literal["Invoice", "Closing Report"]
		reference_doctype: DF.Data | None
		reference_name: DF.Data | None
		status: DF.Literal["Queued", "Claimed", "Printed", "Failed"]
		claimed_by_hub: DF.Data | None
		attempts: DF.Int
		error_message: DF.SmallText | None
	# end: auto-generated types
	pass
