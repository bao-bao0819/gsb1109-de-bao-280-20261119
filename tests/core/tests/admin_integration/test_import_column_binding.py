"""Regression tests for supplier CSV column binding.

These tests guard against a class of defect where the admin preview looks
correct but the confirmed import (or the ``import`` management command)
writes values into the wrong model field, because column identity was lost
between parsing the raw bytes and building the row dict handed to the
resource.

Coverage matrix:

* two logically identical *valid* files - one with canonical headers, one
  with a UTF-8 BOM on the first column and the columns swapped - go through
  the admin preview and the confirm step;
* the same valid files are also driven through the real byte -> format ->
  resource path, including the management command entry point;
* an alias/canonical conflict (legacy ``store_id`` next to ``store_no``,
  each holding a different value) and an exact duplicate header are fed to
  both entry points and must be rejected with the two physical header
  positions, leaving nothing in the database.

The assertions always start from the raw file bytes; a test built on a
hand-made ``tablib.Dataset`` would not notice an entry point dropping column
identity.
"""

import os
import re
from io import StringIO
from unittest import expectedFailure

from core.admin import SupplierResource
from core.models import Supplier
from core.tests.admin_integration.mixins import AdminTestMixin
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from import_export.formats.base_formats import CSV

EXPORTS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "exports")

VALID_FIXTURE = "supplier-columns.csv"
BOM_FIXTURE = "supplier-columns-bom.csv"
ALIAS_CONFLICT_FIXTURE = "supplier-alias-conflict.csv"
DUPLICATE_FIXTURE = "supplier-duplicate-header.csv"

# Logical records shared by the two valid files, independent of column order.
EXPECTED_SUPPLIERS = [
    {"order_no": "100001", "store_no": "200002"},
    {"order_no": "100003", "store_no": "200004"},
]


def fixture_path(name):
    return os.path.join(EXPORTS_DIR, name)


def read_fixture(name):
    with open(fixture_path(name), "rb") as fh:
        return fh.read()


def assert_suppliers_match_expected(test_case):
    test_case.assertEqual(Supplier.objects.count(), len(EXPECTED_SUPPLIERS))
    persisted = list(
        Supplier.objects.order_by("order_no").values("order_no", "store_no")
    )
    test_case.assertEqual(persisted, EXPECTED_SUPPLIERS)


def _preview_values_by_field(result):
    """Map each preview row's diff cells to the resource's diff headers.

    The preview renders one cell per import field, so zipping the cells with
    ``result.diff_headers`` proves that a value shown for a field really
    originated from that field's source column rather than its position.
    """
    rows = []
    for row_result in result.valid_rows():
        cells = [re.sub(r"<[^>]+>", "", cell) for cell in row_result.diff]
        rows.append(dict(zip(result.diff_headers, cells)))
    return rows


class SupplierColumnBindingAdminTest(AdminTestMixin, TestCase):
    """Admin preview + confirm workflow, driven by real uploaded bytes."""

    supplier_import_url = "/admin/core/supplier/import/"
    supplier_process_import_url = "/admin/core/supplier/process_import/"

    def _preview(self, filename):
        return self._do_import_post(
            self.supplier_import_url, filename, input_format=0
        )

    def _confirm(self, preview_response):
        confirm_form = preview_response.context["confirm_form"]
        data = confirm_form.initial
        self._prepend_form_prefix(data)
        return self._post_url_response(
            self.supplier_process_import_url, data, follow=True
        )

    def test_valid_columns_preview_then_confirm(self):
        response = self._preview(VALID_FIXTURE)

        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertFalse(result.has_errors())
        self.assertFalse(result.has_validation_errors())
        self.assertIn("confirm_form", response.context)

        # every preview cell must trace back to its declared field
        preview_rows = _preview_values_by_field(result)
        self.assertEqual(
            [
                {"order_no": row["order_no"], "store_no": row["store_no"]}
                for row in preview_rows
            ],
            EXPECTED_SUPPLIERS,
        )

        response = self._confirm(response)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Import finished: 2 new, 0 updated, 0 deleted and 0 skipped suppliers.",
        )
        # the confirmed row carries exactly the values previewed per field
        assert_suppliers_match_expected(self)

    def test_bom_and_swapped_columns_preview_then_confirm(self):
        # the admin decodes with utf-8-sig, so the BOM must not make the
        # first physical column bind to a different field
        response = self._preview(BOM_FIXTURE)

        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertFalse(result.has_errors())
        self.assertIn("confirm_form", response.context)

        # column order is swapped, but each value still lands on its field
        preview_rows = _preview_values_by_field(result)
        self.assertEqual(
            [
                {"order_no": row["order_no"], "store_no": row["store_no"]}
                for row in preview_rows
            ],
            EXPECTED_SUPPLIERS,
        )

        response = self._confirm(response)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Import finished: 2 new, 0 updated, 0 deleted and 0 skipped suppliers.",
        )
        # the persisted values follow the headers, not the column positions
        assert_suppliers_match_expected(self)

    def test_alias_conflict_is_rejected_with_header_positions(self):
        response = self._preview(ALIAS_CONFLICT_FIXTURE)

        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertTrue(result.has_errors())
        self.assertNotIn("confirm_form", response.context)
        # the report must lead back to both physical header locations
        message = result.base_errors[0].error.messages[0]
        self.assertIn("columns 1 and 2", message)
        self.assertIn("'store_id'", message)
        self.assertIn("'store_no'", message)
        self.assertContains(response, "columns 1 and 2")
        self.assertEqual(Supplier.objects.count(), 0)

    def test_duplicate_header_is_rejected_with_header_positions(self):
        response = self._preview(DUPLICATE_FIXTURE)

        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertTrue(result.has_errors())
        self.assertNotIn("confirm_form", response.context)
        # both occurrences are the same raw header, on columns 1 and 2
        message = result.base_errors[0].error.messages[0]
        self.assertIn("columns 1 and 2", message)
        self.assertIn("'order_no'", message)
        self.assertContains(response, "columns 1 and 2")
        self.assertEqual(Supplier.objects.count(), 0)


class SupplierColumnBindingCommandTest(TestCase):
    """The ``import`` management command is the batch parsing entry point."""

    def setUp(self):
        super().setUp()
        self.out = StringIO()
        self.err = StringIO()

    def _run_command(self, filename):
        call_command(
            "import",
            "core.admin.SupplierResource",
            fixture_path(filename),
            stdout=self.out,
            stderr=self.err,
            format="CSV",
            interactive=False,
        )

    def test_valid_columns_imported_from_file_bytes(self):
        self._run_command(VALID_FIXTURE)
        assert_suppliers_match_expected(self)

    def test_alias_conflict_aborts_without_writing_any_row(self):
        with self.assertRaises(SystemExit):
            self._run_command(ALIAS_CONFLICT_FIXTURE)

        self.assertIn("Import errors!", self.err.getvalue())
        self.assertIn("columns 1 and 2", self.err.getvalue())
        self.assertIn("'store_id'", self.err.getvalue())
        self.assertIn("'store_no'", self.err.getvalue())
        self.assertEqual(Supplier.objects.count(), 0)

    def test_duplicate_header_aborts_without_writing_any_row(self):
        with self.assertRaises(SystemExit):
            self._run_command(DUPLICATE_FIXTURE)

        self.assertIn("Import errors!", self.err.getvalue())
        self.assertIn("columns 1 and 2", self.err.getvalue())
        self.assertEqual(Supplier.objects.count(), 0)

    @expectedFailure
    def test_bom_file_binds_first_column_at_batch_entry(self):
        # First point where evidence is lost: the command creates the CSV
        # format without an encoding, so TextFormat.create_dataset() decodes
        # the raw bytes with plain "utf-8" and the BOM survives into
        # dataset.headers[0] ("﻿store_no").  That header no longer
        # matches the resource's store_no field, and by the time
        # import_data_inner() zips the headers into the row OrderedDict the
        # first column's identity is gone: the field is silently skipped and
        # the rows are saved half-empty instead of being rejected.  The admin
        # entry avoids this by decoding with utf-8-sig.  No fix is included
        # in this test task; the marker records the current limitation.
        self._run_command(BOM_FIXTURE)

        # expected correct state; in practice store_no is "" on every row
        assert_suppliers_match_expected(self)


class SupplierResourceBindingTest(TestCase):
    """Resource-level checks on datasets produced from the actual file bytes."""

    def _import_bytes(self, raw, encoding):
        dataset = CSV(encoding=encoding).create_dataset(raw)
        resource = SupplierResource()
        result = resource.import_data(dataset, raise_errors=False)
        return resource, result

    def test_valid_columns_reach_the_resource_by_header_name(self):
        resource, result = self._import_bytes(read_fixture(VALID_FIXTURE), "utf-8")

        self.assertFalse(result.has_errors())
        self.assertEqual(resource.canonical_headers, ["order_no", "store_no"])
        self.assertEqual(
            resource.received_rows,
            [
                {"order_no": "100001", "store_no": "200002"},
                {"order_no": "100003", "store_no": "200004"},
            ],
        )
        assert_suppliers_match_expected(self)

    def test_bom_is_stripped_and_alias_normalised_before_row_build(self):
        resource, result = self._import_bytes(read_fixture(BOM_FIXTURE), "utf-8-sig")

        self.assertFalse(result.has_errors())
        # BOM removed and the swapped first column still targets store_no
        self.assertEqual(resource.canonical_headers, ["store_no", "order_no"])
        self.assertEqual(
            resource.received_rows[0],
            {"store_no": "200002", "order_no": "100001"},
        )
        assert_suppliers_match_expected(self)

    def test_legacy_alias_header_is_normalised_to_store_no(self):
        raw = b"order_no,store_id\n500009,500010\n"
        resource, result = self._import_bytes(raw, "utf-8")

        self.assertFalse(result.has_errors())
        self.assertEqual(resource.canonical_headers, ["order_no", "store_no"])
        self.assertEqual(
            resource.received_rows,
            [{"order_no": "500009", "store_no": "500010"}],
        )
        supplier = Supplier.objects.get()
        self.assertEqual(supplier.order_no, "500009")
        self.assertEqual(supplier.store_no, "500010")

    def test_alias_conflict_raises_before_any_row_is_built(self):
        raw = read_fixture(ALIAS_CONFLICT_FIXTURE)
        dataset = CSV().create_dataset(raw)
        resource = SupplierResource()

        with self.assertRaises(ValidationError) as ctx:
            resource.before_import(dataset)

        message = str(ctx.exception)
        self.assertIn("columns 1 and 2", message)
        self.assertIn("'store_id'", message)
        self.assertIn("'store_no'", message)
        # the dataset headers were not rewritten because the import aborts
        self.assertEqual(dataset.headers, ["store_id", "store_no"])
        self.assertEqual(resource.received_rows, [])
        self.assertEqual(Supplier.objects.count(), 0)

    def test_duplicate_header_raises_before_any_row_is_built(self):
        raw = read_fixture(DUPLICATE_FIXTURE)
        dataset = CSV().create_dataset(raw)
        resource = SupplierResource()

        with self.assertRaises(ValidationError) as ctx:
            resource.before_import(dataset)

        message = str(ctx.exception)
        self.assertIn("columns 1 and 2", message)
        self.assertIn("'order_no'", message)
        self.assertEqual(resource.received_rows, [])
        self.assertEqual(Supplier.objects.count(), 0)
