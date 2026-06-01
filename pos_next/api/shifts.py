# -*- coding: utf-8 -*-
# Copyright (c) 2024, POS Next and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import json
import frappe
from frappe import _
from frappe.utils import format_datetime, nowdate, nowtime, get_datetime
from pos_next.api.utilities import get_wallet_payment_modes


@frappe.whitelist()
def get_opening_dialog_data():
	"""Get data required for opening shift dialog"""
	data = {}

	# Get POS Profiles where current user is defined in POS Profile User table
	pos_profiles_data = frappe.db.sql(
		"""
		SELECT DISTINCT p.name, p.company, p.currency, p.warehouse, p.selling_price_list
		FROM `tabPOS Profile` p
		INNER JOIN `tabPOS Profile User` u ON u.parent = p.name
		WHERE p.disabled = 0 AND u.user = %s
		ORDER BY p.name
		""",
		frappe.session.user,
		as_dict=1,
	)

	data["pos_profiles_data"] = pos_profiles_data

	# Derive companies from accessible POS Profiles
	company_names = []
	for profile in pos_profiles_data:
		if profile.company and profile.company not in company_names:
			company_names.append(profile.company)
	data["companies"] = [{"name": c} for c in company_names]

	# Get payment methods for POS profiles (exclude wallet payment methods)
	pos_profiles_list = [p.name for p in pos_profiles_data]

	if pos_profiles_list:
		# Exclude wallet payment modes from opening balance
		wallet_modes = get_wallet_payment_modes()

		payment_filters = {"parent": ["in", pos_profiles_list]}
		if wallet_modes:
			payment_filters["mode_of_payment"] = ["not in", wallet_modes]

		data["payments_method"] = frappe.get_list(
			"POS Payment Method",
			filters=payment_filters,
			fields=["*"],
			limit_page_length=0,
			order_by="parent",
			ignore_permissions=True,
		)

		# Set currency from pos profile
		for mode in data["payments_method"]:
			mode["currency"] = frappe.get_cached_value("POS Profile", mode["parent"], "currency")
	else:
		data["payments_method"] = []

	return data


@frappe.whitelist()
def check_opening_shift(user=None):
	"""Check if user has an open shift"""
	if not user:
		user = frappe.session.user

	open_shifts = frappe.db.get_all(
		"POS Opening Shift",
		filters={
			"user": user,
			"pos_closing_shift": ["is", "not set"],
			"docstatus": 1,
			"status": "Open",
		},
		fields=["name", "pos_profile", "period_start_date"],
		order_by="period_start_date desc",
	)

	if not open_shifts:
		return None

	# Get the latest open shift
	shift_data = open_shifts[0]
	data = {}
	data["pos_opening_shift"] = frappe.get_doc("POS Opening Shift", shift_data["name"])
	data["pos_profile"] = frappe.get_doc("POS Profile", shift_data["pos_profile"])
	data["company"] = frappe.get_doc("Company", data["pos_profile"].company)
	# Include server timestamp so frontend can compute shift duration
	# without timezone mismatch (period_start_date is in server timezone)
	data["server_now"] = str(get_datetime())

	return data


@frappe.whitelist()
def create_opening_shift(pos_profile, company, balance_details):
	"""Create a new POS Opening Shift"""
	balance_details = json.loads(balance_details) if isinstance(balance_details, str) else balance_details

	# Check if user already has an open shift
	existing_shift = check_opening_shift(frappe.session.user)
	if existing_shift:
		frappe.throw(_("You already have an open shift: {0}").format(existing_shift["pos_opening_shift"].name))

	new_pos_opening = frappe.get_doc(
		{
			"doctype": "POS Opening Shift",
			"period_start_date": get_datetime(),
			"posting_date": nowdate(),
			"posting_time": nowtime(),
			"user": frappe.session.user,
			"pos_profile": pos_profile,
			"company": company,
			"status": "Open",
		}
	)

	# Add balance details - map opening_amount to amount
	formatted_balance_details = []
	for detail in balance_details:
		formatted_balance_details.append({
			"mode_of_payment": detail.get("mode_of_payment"),
			"amount": detail.get("opening_amount", 0)
		})

	new_pos_opening.set("balance_details", formatted_balance_details)
	new_pos_opening.insert(ignore_permissions=True)
	new_pos_opening.submit()

	data = {}
	data["pos_opening_shift"] = new_pos_opening.as_dict()
	data["pos_profile"] = frappe.get_doc("POS Profile", pos_profile)
	data["company"] = frappe.get_doc("Company", company)

	return data


@frappe.whitelist()
def get_closing_shift_data(opening_shift):
	"""Get data for closing shift"""
	from pos_next.pos_next.doctype.pos_closing_shift.pos_closing_shift import make_closing_shift_from_opening

	try:
		# Get the opening shift document
		opening_shift_doc = frappe.get_doc("POS Opening Shift", opening_shift)

		# Convert to dict with proper datetime serialization
		opening_shift_dict = opening_shift_doc.as_dict()
		opening_shift_json = json.dumps(opening_shift_dict, default=str)

		# Create closing shift from opening shift (returns a dict)
		closing_data = make_closing_shift_from_opening(opening_shift_json)

		# Ensure datetime values are JSON serializable
		return json.loads(json.dumps(closing_data, default=str))
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Get Closing Shift Data Error")
		frappe.throw(_("Error getting closing shift data: {0}").format(str(e)))


@frappe.whitelist()
def submit_closing_shift(closing_shift):
	"""Submit closing shift"""
	from pos_next.pos_next.doctype.pos_closing_shift.pos_closing_shift import submit_closing_shift as submit_shift

	try:
		# closing_shift is already a JSON string from frontend
		# If it's a dict, convert to JSON string
		if isinstance(closing_shift, dict):
			closing_shift = json.dumps(closing_shift)

		result = submit_shift(closing_shift)
		return {"name": result, "status": "success"}
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Submit Closing Shift Error")
		frappe.throw(_("Error submitting closing shift: {0}").format(str(e)))


@frappe.whitelist()
def get_closing_shift_print_html(closing_shift: str) -> dict:
	"""Return printable payload for a submitted POS Closing Shift."""
	if not closing_shift:
		frappe.throw(_("Closing Shift is required"))

	try:
		doc = frappe.get_doc("POS Closing Shift", closing_shift)
		if doc.docstatus != 1:
			frappe.throw(_("Only submitted closing shifts can be printed"))

		doc.check_permission("read")
		fallback_html = _get_builtin_closing_shift_print_html(doc)
		print_format = _get_closing_report_print_format(doc.pos_profile)

		if not print_format:
			return {"type": "html", "html": fallback_html}

		if print_format.raw_printing:
			raw_result = frappe.get_attr("frappe.www.printview.get_rendered_raw_commands")(
				doc="POS Closing Shift",
				name=doc.name,
				print_format=print_format.name,
			)
			raw_commands = raw_result.get("raw_commands")
			if not raw_commands:
				frappe.throw(_("Failed to render raw closing report print format"))
			return {
				"type": "raw",
				"raw_commands": raw_commands,
				"fallback_html": fallback_html,
			}

		result = frappe.get_attr("frappe.www.printview.get_html_and_style")(
			doc="POS Closing Shift",
			name=doc.name,
			print_format=print_format.name,
			no_letterhead=1,
		)
		html = result.get("html")
		style = result.get("style") or ""
		if not html:
			frappe.throw(_("Failed to render closing report print format"))

		return {
			"type": "html",
			"html": _build_print_format_document_html(doc, html, style),
			"fallback_html": fallback_html,
		}
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Get Closing Shift Print HTML Error")
		frappe.throw(_("Error preparing closing shift report: {0}").format(str(e)))


def _get_closing_report_print_format(pos_profile: str):
	print_format_name = frappe.db.get_value(
		"POS Settings",
		{"pos_profile": pos_profile},
		"closing_report_print_format",
	)
	if not print_format_name:
		return None

	print_format = frappe.get_doc("Print Format", print_format_name)
	if print_format.disabled:
		frappe.throw(_("Closing Report Print Format {0} is disabled").format(print_format_name))
	if print_format.doc_type != "POS Closing Shift":
		frappe.throw(_("Closing Report Print Format must be for POS Closing Shift"))

	return print_format


def _get_builtin_closing_shift_print_html(doc) -> str:
	report_html = doc.get_payment_reconciliation_details()
	return _build_closing_shift_print_html(doc, report_html)


def _build_print_format_document_html(doc, html: str, style: str) -> str:
	return f"""<!DOCTYPE html>
<html>
<head>
	<meta charset="UTF-8">
	<title>{frappe.utils.escape_html(_('POS Closing Report - {0}').format(doc.name))}</title>
	<style>{style}</style>
</head>
<body>{html}</body>
</html>"""


def _build_closing_shift_print_html(doc, report_html: str) -> str:
	period_start = format_datetime(doc.period_start_date) if doc.period_start_date else ""
	period_end = format_datetime(doc.period_end_date) if doc.period_end_date else ""

	return f"""<!DOCTYPE html>
<html>
<head>
	<meta charset="UTF-8">
	<title>{frappe.utils.escape_html(_('POS Closing Report - {0}').format(doc.name))}</title>
	<style>
		@page {{ size: 80mm auto; margin: 4mm; }}
		* {{ box-sizing: border-box; }}
		body {{
			color: #111;
			font-family: Arial, sans-serif;
			font-size: 11px;
			line-height: 1.35;
			margin: 0 auto;
			max-width: 80mm;
			padding: 4mm;
			width: 80mm;
		}}
		.header {{
			border-bottom: 1px dashed #111;
			margin-bottom: 10px;
			padding-bottom: 8px;
			text-align: center;
		}}
		.company {{ font-size: 15px; font-weight: 700; }}
		.title {{ font-size: 12px; font-weight: 700; margin-top: 2px; }}
		.meta {{
			border-bottom: 1px dashed #111;
			margin-bottom: 10px;
			padding-bottom: 8px;
		}}
		.meta-row {{
			display: flex;
			justify-content: space-between;
			gap: 8px;
			margin: 2px 0;
		}}
		.meta-row span:first-child {{ font-weight: 700; }}
		h6 {{
			color: #111 !important;
			font-size: 11px;
			font-weight: 700;
			margin: 10px 0 6px;
			text-transform: uppercase;
		}}
		table {{
			border-collapse: collapse;
			margin-bottom: 8px;
			width: 100%;
		}}
		th, td {{
			border-bottom: 1px solid #ddd;
			padding: 4px 0;
			vertical-align: top;
		}}
		th {{ font-weight: 700; }}
		.text-left {{ text-align: left; }}
		.text-right {{ text-align: right; }}
		.text-center {{ text-align: center; }}
		.text-muted {{ color: #555; }}
		.small {{ font-size: 9px; }}
		.font-bold {{ font-weight: 700; }}
		.box, .grid-body, .rows {{ width: 100%; }}
		@media print {{
			body {{ margin: 0; max-width: 80mm; padding: 0; width: 80mm; }}
			.no-print {{ display: none; }}
		}}
	</style>
</head>
<body>
	<div class="header">
		<div class="company">{frappe.utils.escape_html(doc.company or '')}</div>
		<div class="title">{frappe.utils.escape_html(_('POS Closing Report'))}</div>
	</div>
	<div class="meta">
		<div class="meta-row"><span>{frappe.utils.escape_html(_('Closing Shift'))}</span><span>{frappe.utils.escape_html(doc.name)}</span></div>
		<div class="meta-row"><span>{frappe.utils.escape_html(_('POS Profile'))}</span><span>{frappe.utils.escape_html(doc.pos_profile or '')}</span></div>
		<div class="meta-row"><span>{frappe.utils.escape_html(_('Cashier'))}</span><span>{frappe.utils.escape_html(doc.user or '')}</span></div>
		<div class="meta-row"><span>{frappe.utils.escape_html(_('Period Start'))}</span><span>{frappe.utils.escape_html(period_start)}</span></div>
		<div class="meta-row"><span>{frappe.utils.escape_html(_('Period End'))}</span><span>{frappe.utils.escape_html(period_end)}</span></div>
	</div>
	{report_html}
</body>
</html>"""
