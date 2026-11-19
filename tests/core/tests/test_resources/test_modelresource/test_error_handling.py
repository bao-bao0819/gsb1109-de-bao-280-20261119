from unittest import mock

import tablib
from core.models import Author, Book
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

    def test_import_data_reports_all_conversion_errors_for_a_row(self):
        # a row containing two unconvertible values must report both errors
        resource = BookResource()
        dataset = tablib.Dataset(headers=["name", "published_time", "added"])
        dataset.append(["Some book", "not-a-time", "not-a-datetime"])

        result = resource.import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_validation_errors())
        self.assertIs(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_INVALID)
        invalid_row = result.invalid_rows[0]
        self.assertEqual(2, invalid_row.error_count)
        self.assertIn("published_time", invalid_row.field_specific_errors)
        self.assertIn("added", invalid_row.field_specific_errors)

    def test_import_data_reports_conversion_and_model_errors_for_a_row(self):
        # a row containing a type conversion failure and a model validation
        # error must report both errors
        class TestResource(resources.ModelResource):
            class Meta:
                model = Author
                clean_model_instances = True

        resource = TestResource()
        dataset = tablib.Dataset(headers=["name", "birthday"])
        dataset.append(["123", "1882test-01-18"])

        result = resource.import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_validation_errors())
        self.assertIs(result.rows[0].import_type, results.RowResult.IMPORT_TYPE_INVALID)
        invalid_row = result.invalid_rows[0]
        self.assertEqual(2, invalid_row.error_count)
        self.assertIn("birthday", invalid_row.field_specific_errors)
        self.assertIn("name", invalid_row.field_specific_errors)

    def test_import_data_combines_errors_for_fields_sharing_an_attribute(self):
        # two import fields can map to the same attribute: both conversion
        # errors must be retained
        class TestResource(resources.ModelResource):
            birthday_duplicate = fields.Field(
                attribute="birthday",
                column_name="birthday_duplicate",
                widget=widgets.DateTimeWidget(),
            )

            class Meta:
                model = Author
                fields = ("birthday", "birthday_duplicate")

        resource = TestResource()
        dataset = tablib.Dataset(headers=["birthday", "birthday_duplicate"])
        dataset.append(["bad-date", "another-bad-date"])

        result = resource.import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_validation_errors())
        invalid_row = result.invalid_rows[0]
        self.assertEqual(2, invalid_row.error_count)
        self.assertEqual(2, len(invalid_row.field_specific_errors["birthday"]))

    def test_failed_row_visible_to_both_error_consumers(self):
        # a failed row must be readable both as field-keyed validation errors
        # and as the original input cells in the failed dataset
        resource = BookResource()
        dataset = tablib.Dataset(headers=["name", "added"])
        dataset.append(["Some book", "not-a-date"])

        result = resource.import_data(
            dataset, raise_errors=False, collect_failed_rows=True
        )

        self.assertIn("added", result.invalid_rows[0].field_specific_errors)
        failed_row = result.failed_dataset.dict[0]
        self.assertEqual("Some book", failed_row["name"])
        self.assertEqual("not-a-date", failed_row["added"])

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
