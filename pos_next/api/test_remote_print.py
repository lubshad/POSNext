# Copyright (c) 2026, BrainWise and contributors

import unittest
from unittest.mock import Mock, patch

import frappe

from pos_next.api import remote_print


class TestRemotePrintAPI(unittest.TestCase):
	@patch("pos_next.api.remote_print._is_online", return_value=False)
	@patch("pos_next.api.remote_print._batch_pos_profiles", return_value={"PRINTER-1": []})
	@patch("pos_next.api.remote_print._batch_allowed_types", return_value={"PRINTER-1": ["General"]})
	@patch("pos_next.api.remote_print.frappe.get_all")
	def test_list_keeps_stale_enabled_printer_visible_when_requested(
		self,
		mock_get_all: Mock,
		_mock_allowed_types: Mock,
		_mock_pos_profiles: Mock,
		_mock_is_online: Mock,
	) -> None:
		mock_get_all.return_value = [
			frappe._dict(
				name="PRINTER-1",
				printer_name="Receipt Printer",
				hub_id="hub-1",
				qz_printer_name="POS80",
				enabled=1,
				last_seen="2026-07-17 10:00:00",
			)
		]

		printers = remote_print.list_remote_printers(include_offline=1)

		self.assertEqual(len(printers), 1)
		self.assertFalse(printers[0].online)
		self.assertEqual(printers[0].enabled, 1)
		self.assertEqual(mock_get_all.call_args.kwargs["filters"], {"enabled": 1})

	@patch("pos_next.api.remote_print.frappe.db.get_value")
	def test_create_job_rejects_explicitly_disabled_printer(self, mock_get_value: Mock) -> None:
		mock_get_value.return_value = frappe._dict(
			name="PRINTER-1",
			hub_id="hub-1",
			enabled=0,
			last_seen="2026-07-17 10:00:00",
		)

		with self.assertRaises(frappe.ValidationError) as error:
			remote_print.create_remote_print_job(
				remote_printer="PRINTER-1",
				job_type="Invoice",
				reference_doctype="Sales Invoice",
				reference_name="ACC-SINV-2026-00093",
			)

		self.assertIn("disabled", str(error.exception))
