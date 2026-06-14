"""Runtime compatibility patches for mixed Frappe/ERPNext versions."""

from __future__ import annotations

import inspect
import frappe


def patch_round_floats_in_signature(document_class):
	"""Make Document.round_floats_in accept do_not_round_fields when missing.

	ERPNext (newer) calls:
	    doc.round_floats_in(row, do_not_round_fields=[...])

	Older Frappe implementations only accept:
	    round_floats_in(doc, fieldnames=None)
	"""

	original = getattr(document_class, "round_floats_in", None)
	if not original:
		return

	signature = inspect.signature(original)
	if "do_not_round_fields" in signature.parameters:
		return

	if getattr(document_class, "_pos_next_round_floats_signature_patched", False):
		return

	def round_floats_in(self, doc, fieldnames=None, do_not_round_fields=None):
		if do_not_round_fields:
			do_not_round_fields = set(do_not_round_fields)
			if fieldnames:
				fieldnames = [f for f in fieldnames if f not in do_not_round_fields]
			else:
				fieldnames = [
					df.fieldname
					for df in doc.meta.get(
						"fields", {"fieldtype": ["in", ["Currency", "Float", "Percent"]]}
					)
					if df.fieldname not in do_not_round_fields
				]

		return original(self, doc, fieldnames=fieldnames)

	document_class._pos_next_original_round_floats_in = original
	document_class.round_floats_in = round_floats_in
	document_class._pos_next_round_floats_signature_patched = True


def patch_pos_settings_singles_compat():
	"""
	Patch Database.get_single_value and frappe.get_single_value
	to intercept queries for 'POS Settings' and fetch them from the Singles
	table or return default values. This is needed because POS Next overrides
	ERPNext's 'POS Settings' to be a non-Single doctype, but ERPNext's core code
	still queries it via get_single_value.
	"""
	import frappe
	from frappe.database.database import Database
	import frappe.model.document

	# 1. Patch Database.get_single_value
	original_get_single_value_db = Database.get_single_value

	def patched_get_single_value_db(self, doctype, fieldname, cache=True, *args, **kwargs):
		if doctype == "POS Settings":
			if cache and not kwargs.get("for_update") and kwargs.get("run", True) and fieldname in self.value_cache[doctype]:
				return self.value_cache[doctype][fieldname]

			Singles = frappe.qb.DocType("Singles")
			result = (
				frappe.qb.from_(Singles)
				.select(Singles.value)
				.where(Singles.doctype == "POS Settings")
				.where(Singles.field == fieldname)
				.run()
			)
			if result and result[0][0] is not None:
				val = result[0][0]
			else:
				# Fallbacks
				if fieldname == "invoice_type":
					val = "Sales Invoice"
				elif fieldname == "post_change_gl_entries":
					val = "0"
				else:
					val = None

			if fieldname == "post_change_gl_entries":
				from frappe.utils import cint
				val = cint(val)

			if cache and not kwargs.get("for_update") and kwargs.get("run", True):
				self.value_cache[doctype][fieldname] = val

			return val

		return original_get_single_value_db(self, doctype, fieldname, cache=cache, *args, **kwargs)

	Database.get_single_value = patched_get_single_value_db

	# 2. Patch frappe.model.document.get_single_value
	original_get_single_value_doc = frappe.model.document.get_single_value

	def patched_get_single_value_doc(setting, fieldname, /, *, as_dict=False):
		if setting == "POS Settings":
			val = frappe.db.get_single_value(setting, fieldname)
			if as_dict:
				return frappe._dict({fieldname: val})
			return val
		return original_get_single_value_doc(setting, fieldname, as_dict=as_dict)

	frappe.model.document.get_single_value = patched_get_single_value_doc
	frappe.get_single_value = patched_get_single_value_doc

	# 3. Patch frappe.model.document.get_doc
	original_get_doc = frappe.model.document.get_doc

	def patched_get_doc(*args, **kwargs):
		if args and args[0] == "POS Settings":
			name = None
			if len(args) > 1:
				name = args[1]
			if name is None or name == "POS Settings":
				return LegacyPOSSettingsDocument()
		return original_get_doc(*args, **kwargs)

	frappe.model.document.get_doc = patched_get_doc
	frappe.get_doc = patched_get_doc


class LegacyPOSSettingsDocument(frappe.model.document.Document):
	def __init__(self, *args, **kwargs):
		super().__init__({
			"doctype": "POS Settings",
			"name": "POS Settings",
			"invoice_type": "Sales Invoice",
			"post_change_gl_entries": 0,
		})
		self.__dict__.update({
			"invoice_type": "Sales Invoice",
			"post_change_gl_entries": 0,
		})
		self._load_from_singles()
		self._load_child_tables()

	def _load_from_singles(self):
		for field in ("invoice_type", "post_change_gl_entries"):
			val = frappe.db.get_single_value("POS Settings", field)
			if val is not None:
				setattr(self, field, val)
				self.__dict__[field] = val

	def _load_child_tables(self):
		self.set(
			"invoice_fields",
			frappe.get_all(
				"POS Field",
				filters={
					"parent": "POS Settings",
					"parenttype": "POS Settings",
					"parentfield": "invoice_fields",
				},
				fields=[
					"name",
					"idx",
					"fieldname",
					"label",
					"fieldtype",
					"options",
					"default_value",
					"reqd",
					"read_only",
				],
				order_by="idx asc",
			)
		)
		self.set(
			"pos_search_fields",
			frappe.get_all(
				"POS Search Fields",
				filters={
					"parent": "POS Settings",
					"parenttype": "POS Settings",
					"parentfield": "pos_search_fields",
				},
				fields=["name", "idx", "field", "fieldname"],
				order_by="idx asc",
			)
		)

	def get_valid_dict(self, *args, **kwargs):
		d = super().get_valid_dict(*args, **kwargs)
		d.update({
			"invoice_type": self.invoice_type,
			"post_change_gl_entries": self.post_change_gl_entries,
			"invoice_fields": self.invoice_fields,
			"pos_search_fields": self.pos_search_fields,
		})
		return d

	def as_dict(self, *args, **kwargs):
		d = super().as_dict(*args, **kwargs)
		d.update({
			"invoice_type": self.invoice_type,
			"post_change_gl_entries": self.post_change_gl_entries,
			"invoice_fields": self.invoice_fields,
			"pos_search_fields": self.pos_search_fields,
		})
		return d

	def db_insert(self, *args, **kwargs):
		self.db_update(*args, **kwargs)

	def db_update(self, *args, **kwargs):
		for field in ("invoice_type", "post_change_gl_entries"):
			val = getattr(self, field, None)
			if val is not None:
				frappe.db.set_single_value("POS Settings", field, val)
