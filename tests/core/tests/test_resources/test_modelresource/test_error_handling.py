from unittest import mock

import tablib
from core.models import Author, Book, Store
from core.tests.resources import (
    AuthorResource,
    BookResource,
    BookResourceWithLineNumberLogger,
    ProfileResource,
)
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import TestCase

from import_export import exceptions, fields, resources, results, widgets


class ErrorHandlingTest(TestCase):
    def setUp(self):
        self.resource = BookResource()
        self.book = Book.objects.create(name="Some book")
        self.dataset = tablib.Dataset(headers=["id", "name", "author_email", "price"])
        row = [self.book.pk, "Some book", "test@example.com", "10.25"]
        self.dataset.append(row)

    @mock.patch("import_export.resources.connections")
    def test_ImproperlyConfigured_if_use_transactions_set_when_not_supported(
        self, mock_db_connections
    ):
        class Features:
            supports_transactions = False

        class DummyConnection:
            features = Features()

        dummy_connection = DummyConnection()
        mock_db_connections.__getitem__.return_value = dummy_connection
        with self.assertRaises(ImproperlyConfigured):
            self.resource.import_data(
                self.dataset,
                use_transactions=True,
            )

    def test_importing_with_line_number_logging(self):
        resource = BookResourceWithLineNumberLogger()
        resource.import_data(self.dataset, raise_errors=True)
        self.assertEqual(resource.before_lines, [1])
        self.assertEqual(resource.after_lines, [1])

    def test_import_data_raises_field_specific_validation_errors(self):
        resource = AuthorResource()
        dataset = tablib.Dataset(headers=["name", "birthday"])
        dataset.append(["A.A.Milne", "1882test-01-18"])

        result = resource.import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_validation_errors())
        self.assertIs(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_INVALID)
        self.assertIn("birthday", result.invalid_rows[0].field_specific_errors)

    def test_import_data_raises_field_specific_validation_errors_with_skip_unchanged(
        self,
    ):
        resource = AuthorResource()
        author = Author.objects.create(name="Some author")

        dataset = tablib.Dataset(headers=["id", "birthday"])
        dataset.append([author.id, "1882test-01-18"])

        result = resource.import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_validation_errors())
        self.assertIs(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_INVALID)
        self.assertIn("birthday", result.invalid_rows[0].field_specific_errors)

    def test_import_data_empty_dataset_with_collect_failed_rows(self):
        class _AuthorResource(resources.ModelResource):
            class Meta:
                model = Author
                import_id_fields = ["non_existent_field"]

        resource = _AuthorResource()
        with self.assertRaises(exceptions.ImportError) as e:
            resource.import_data(
                tablib.Dataset(), collect_failed_rows=True, raise_errors=True
            )
        self.assertEqual(
            "The following fields are declared in 'import_id_fields' "
            "but are not present in the resource fields: non_existent_field",
            str(e.exception),
        )

    def test_collect_failed_rows(self):
        resource = ProfileResource()
        headers = ["id", "user"]
        # 'user' is a required field, the database will raise an error.
        row = [None, None]
        dataset = tablib.Dataset(row, headers=headers)
        result = resource.import_data(
            dataset,
            dry_run=True,
            use_transactions=True,
            collect_failed_rows=True,
        )
        self.assertEqual(result.failed_dataset.headers, ["id", "user", "Error"])
        self.assertEqual(len(result.failed_dataset), 1)
        # We can't check the error message because it's package- and version-dependent

    def test_row_result_raise_errors(self):
        resource = ProfileResource()
        headers = ["id", "user"]
        # 'user' is a required field, the database will raise an error.
        row = [None, None]
        dataset = tablib.Dataset(row, headers=headers)
        with self.assertRaises(exceptions.ImportError) as exc:
            resource.import_data(
                dataset,
                dry_run=True,
                use_transactions=True,
                raise_errors=True,
            )
        row_error = exc.exception
        self.assertEqual(1, row_error.number)
        self.assertEqual({"id": None, "user": None}, row_error.row)

    def test_collect_failed_rows_validation_error(self):
        resource = ProfileResource()
        row = ["1"]
        dataset = tablib.Dataset(row, headers=["id"])
        with mock.patch(
            "import_export.resources.Field.save", side_effect=Exception("fail!")
        ):
            result = resource.import_data(
                dataset,
                dry_run=True,
                use_transactions=True,
                collect_failed_rows=True,
            )
        self.assertEqual(result.failed_dataset.headers, ["id", "Error"])
        self.assertEqual(
            1,
            len(result.failed_dataset),
        )
        self.assertEqual("1", result.failed_dataset.dict[0]["id"])
        self.assertEqual("fail!", result.failed_dataset.dict[0]["Error"])

    def test_row_result_raise_ValidationError(self):
        resource = ProfileResource()
        row = ["1"]
        dataset = tablib.Dataset(row, headers=["id"])
        with mock.patch(
            "import_export.resources.Field.save", side_effect=ValidationError("fail!")
        ):
            with self.assertRaisesRegex(
                exceptions.ImportError, "{'__all__': \\['fail!'\\]}"
            ):
                resource.import_data(
                    dataset,
                    dry_run=True,
                    use_transactions=True,
                    raise_errors=True,
                )

    def test_row_result_raise_ValidationError_collect_failed_rows(self):
        # 1752
        resource = ProfileResource()
        row = ["1"]
        dataset = tablib.Dataset(row, headers=["id"])
        with mock.patch(
            "import_export.resources.Field.save", side_effect=ValidationError("fail!")
        ):
            res = resource.import_data(
                dataset, use_transactions=True, collect_failed_rows=True
            )
        self.assertEqual(
            res.failed_dataset.dict[0], {"id": "1", "Error": "{'__all__': ['fail!']}"}
        )


class HookError(Exception):
    pass


class StoreResource(resources.ModelResource):
    # column names deliberately differ from the model attribute names
    amount = fields.Field(
        attribute="amount", column_name="AMT", widget=widgets.DecimalWidget()
    )
    start_date = fields.Field(
        attribute="start_date", column_name="START", widget=widgets.DateWidget()
    )
    end_date = fields.Field(
        attribute="end_date", column_name="END", widget=widgets.DateWidget()
    )

    class Meta:
        model = Store
        clean_model_instances = True

    def before_import_row(self, row, **kwargs):
        if row.get("name") == "bad-hook":
            raise HookError("hook failed")


class ImportErrorOrganizationTest(TestCase):
    """
    A file whose first row has a column which cannot be converted to a
    decimal, plus a start/end date error which is independently detectable
    from two other valid fields.  The second row raises a plain error from a
    resource hook.  Later rows are valid.
    """

    def setUp(self):
        self.resource = StoreResource()
        self.dataset = tablib.Dataset(headers=["name", "AMT", "START", "END"])
        self.dataset.append(["store-1", "not-a-number", "2026-05-01", "2026-04-01"])
        self.dataset.append(["bad-hook", "10.00", "2026-01-01", "2026-02-01"])
        self.dataset.append(["store-3", "20.00", "2026-01-01", "2026-02-01"])

    def test_collect_mode_first_row_reports_both_problems(self):
        result = self.resource.import_data(
            self.dataset,
            raise_errors=False,
            collect_failed_rows=True,
            use_transactions=False,
        )
        self.assertEqual(
            results.RowResult.IMPORT_TYPE_INVALID, result.rows[0].import_type
        )
        self.assertEqual(1, len(result.invalid_rows))
        invalid_row = result.invalid_rows[0]
        self.assertEqual(1, invalid_row.number)
        # both independently determinable problems are reported,
        # keyed by model attribute name
        self.assertEqual(
            {"amount", "end_date"}, set(invalid_row.field_specific_errors)
        )
        self.assertEqual(2, invalid_row.error_count)
        # values are presented in original (source) column order
        self.assertEqual(["id", "name", "AMT", "START", "END"], result.diff_headers)
        self.assertEqual(
            ("---", "store-1", "not-a-number", "2026-05-01", "2026-04-01"),
            invalid_row.values,
        )

    def test_collect_mode_failed_dataset_reports_both_problems(self):
        result = self.resource.import_data(
            self.dataset,
            raise_errors=False,
            collect_failed_rows=True,
            use_transactions=False,
        )
        self.assertEqual(
            ["name", "AMT", "START", "END", "Error"], result.failed_dataset.headers
        )
        failed = result.failed_dataset.dict
        self.assertEqual(2, len(failed))
        self.assertEqual("store-1", failed[0]["name"])
        self.assertEqual("not-a-number", failed[0]["AMT"])
        self.assertIn("amount", failed[0]["Error"])
        self.assertIn("end_date", failed[0]["Error"])

    def test_collect_mode_hook_error_remains_generic_error(self):
        result = self.resource.import_data(
            self.dataset,
            raise_errors=False,
            collect_failed_rows=True,
            use_transactions=False,
        )
        self.assertEqual(
            results.RowResult.IMPORT_TYPE_ERROR, result.rows[1].import_type
        )
        self.assertEqual(1, len(result.error_rows))
        error_row = result.error_rows[0]
        self.assertEqual(2, error_row.number)
        self.assertEqual(1, len(error_row.errors))
        error = error_row.errors[0]
        self.assertIsInstance(error.error, HookError)
        # the original row is retained on the error
        self.assertEqual(
            {
                "name": "bad-hook",
                "AMT": "10.00",
                "START": "2026-01-01",
                "END": "2026-02-01",
            },
            dict(error.row),
        )
        failed = result.failed_dataset.dict
        self.assertEqual("bad-hook", failed[1]["name"])
        self.assertEqual("hook failed", failed[1]["Error"])

    def test_collect_mode_valid_rows_after_errors_are_saved(self):
        result = self.resource.import_data(
            self.dataset,
            raise_errors=False,
            collect_failed_rows=True,
            use_transactions=False,
        )
        self.assertEqual(1, result.totals[results.RowResult.IMPORT_TYPE_NEW])
        self.assertEqual(["store-3"], [s.name for s in Store.objects.all()])

    def test_raise_errors_mode(self):
        with self.assertRaises(exceptions.ImportError) as cm:
            self.resource.import_data(
                self.dataset, raise_errors=True, use_transactions=False
            )
        self.assertEqual(1, cm.exception.number)
        self.assertIsInstance(cm.exception.error, ValidationError)
        self.assertEqual(
            {"amount", "end_date"}, set(cm.exception.error.error_dict.keys())
        )
        self.assertEqual(0, Store.objects.count())

    def test_rollback_on_validation_errors(self):
        result = self.resource.import_data(
            self.dataset,
            raise_errors=False,
            use_transactions=True,
            rollback_on_validation_errors=True,
        )
        self.assertTrue(result.has_validation_errors())
        self.assertEqual(0, Store.objects.count())

    def test_hook_error_rolls_back_when_using_transactions(self):
        result = self.resource.import_data(
            self.dataset,
            raise_errors=False,
            use_transactions=True,
        )
        self.assertTrue(result.has_errors())
        self.assertEqual(0, Store.objects.count())
