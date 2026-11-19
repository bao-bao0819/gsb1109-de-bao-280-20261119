import tablib
from core.models import Author, Book, Category
from django.test import TestCase

from import_export import resources, results
from import_export.fields import Field
from import_export.widgets import (
    CharWidget,
    DateWidget,
    DecimalWidget,
    IntegerWidget,
)


class RawValueTest(TestCase):
    def setUp(self):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                store_row_values = True

        self.resource = _BookResource()

        self.book = Book.objects.create(name="Some book")
        self.dataset = tablib.Dataset(headers=["id", "name", "author_email", "price"])
        row = [self.book.pk, "Some book", "test@example.com", "10.25"]
        self.dataset.append(row)

    def test_import_data(self):
        result = self.resource.import_data(self.dataset, raise_errors=True)

        self.assertFalse(result.has_errors())
        self.assertEqual(len(result.rows), 1)
        self.assertTrue(result.rows[0].diff)
        self.assertEqual(
            result.rows[0].import_type, results.RowResult.IMPORT_TYPE_UPDATE
        )
        self.assertEqual(result.rows[0].row_values.get("name"), "Some book")
        self.assertEqual(
            result.rows[0].row_values.get("author_email"), "test@example.com"
        )
        self.assertEqual(result.rows[0].row_values.get("price"), "10.25")


class ResourcesHelperFunctionsTest(TestCase):
    """
    Test the helper functions in resources.
    """

    def test_has_natural_foreign_key(self):
        """
        Ensure that resources.has_natural_foreign_key detects correctly
        whether a model has a natural foreign key
        """
        cases = {Book: True, Author: True, Category: False}

        for model, expected_result in cases.items():
            self.assertEqual(resources.has_natural_foreign_key(model), expected_result)


class ResourceSubscriptTest(TestCase):
    def test_resource_is_subscriptable(self):
        alias = resources.ModelResource[Book]
        self.assertEqual(alias.__origin__, resources.ModelResource)
        self.assertEqual(alias.__args__, (Book,))

    def test_subclass_can_subscript_resource_base(self):
        class BookResource(resources.ModelResource[Book]):
            class Meta:
                model = Book

        self.assertTrue(issubclass(BookResource, resources.ModelResource))


class ImportConfigurationFingerprintTest(TestCase):
    def _resource(self, **field_kwargs):
        class _BookResource(resources.ModelResource):
            price = Field(
                attribute="price",
                column_name="cost",
                widget=DecimalWidget(),
                **field_kwargs,
            )

            class Meta:
                model = Book
                fields = ("id", "price")

        return _BookResource()

    def test_fingerprint_is_stable_for_identical_configuration(self):
        self.assertEqual(
            self._resource().get_import_configuration_fingerprint(),
            self._resource().get_import_configuration_fingerprint(),
        )

    def test_column_name_change_changes_fingerprint(self):
        baseline = self._resource()

        class _ChangedResource(resources.ModelResource):
            price = Field(
                attribute="price", column_name="price", widget=DecimalWidget()
            )

            class Meta:
                model = Book
                fields = ("id", "price")

        self.assertNotEqual(
            baseline.get_import_configuration_fingerprint(),
            _ChangedResource().get_import_configuration_fingerprint(),
        )

    def test_attribute_change_changes_fingerprint(self):
        baseline = self._resource()

        class _ChangedResource(resources.ModelResource):
            price = Field(
                attribute="author_email", column_name="cost", widget=CharWidget()
            )

            class Meta:
                model = Book
                fields = ("id", "price")

        self.assertNotEqual(
            baseline.get_import_configuration_fingerprint(),
            _ChangedResource().get_import_configuration_fingerprint(),
        )

    def test_widget_type_change_changes_fingerprint(self):
        baseline = self._resource()

        class _ChangedResource(resources.ModelResource):
            price = Field(
                attribute="price", column_name="cost", widget=IntegerWidget()
            )

            class Meta:
                model = Book
                fields = ("id", "price")

        self.assertNotEqual(
            baseline.get_import_configuration_fingerprint(),
            _ChangedResource().get_import_configuration_fingerprint(),
        )

    def test_widget_format_change_changes_fingerprint(self):
        baseline = self._resource()

        class _ChangedResource(resources.ModelResource):
            published = Field(
                attribute="published",
                column_name="published",
                widget=DateWidget(format="%d/%m/%Y"),
            )

            class Meta:
                model = Book
                fields = ("id", "published")

        class _BaselineDateResource(resources.ModelResource):
            published = Field(
                attribute="published",
                column_name="published",
                widget=DateWidget(),
            )

            class Meta:
                model = Book
                fields = ("id", "published")

        self.assertNotEqual(
            _BaselineDateResource().get_import_configuration_fingerprint(),
            _ChangedResource().get_import_configuration_fingerprint(),
        )
        # sanity: the baseline price and date resources are different too
        self.assertNotEqual(
            baseline.get_import_configuration_fingerprint(),
            _BaselineDateResource().get_import_configuration_fingerprint(),
        )

    def test_readonly_change_changes_fingerprint(self):
        self.assertNotEqual(
            self._resource().get_import_configuration_fingerprint(),
            self._resource(readonly=True).get_import_configuration_fingerprint(),
        )

    def test_display_only_options_leave_fingerprint_unchanged(self):
        baseline = self._resource()

        class _DisplayNameResource(resources.ModelResource):
            price = Field(
                attribute="price", column_name="cost", widget=DecimalWidget()
            )

            class Meta:
                model = Book
                fields = ("id", "price")
                name = "A different reviewer-facing name"

        # Meta.name (resource display name) is presentational
        self.assertEqual(
            baseline.get_import_configuration_fingerprint(),
            _DisplayNameResource().get_import_configuration_fingerprint(),
        )

        # widget coerce_to_string only changes rendered (preview) values
        class _CoerceResource(resources.ModelResource):
            price = Field(
                attribute="price",
                column_name="cost",
                widget=DecimalWidget(coerce_to_string=False),
            )

            class Meta:
                model = Book
                fields = ("id", "price")

        self.assertEqual(
            baseline.get_import_configuration_fingerprint(),
            _CoerceResource().get_import_configuration_fingerprint(),
        )
