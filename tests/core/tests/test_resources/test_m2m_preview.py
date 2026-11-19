import tablib
from core.models import Book, Category
from django.test import TestCase

from import_export import fields, resources, results, widgets


class BookCategoryResource(resources.ModelResource):
    categories = fields.Field(
        attribute="categories",
        column_name="categories",
        widget=widgets.ManyToManyWidget(Category, field="name"),
    )

    class Meta:
        model = Book
        fields = ("id", "name", "categories")


class ManyToManyPreviewTest(TestCase):
    def setUp(self):
        self.fiction = Category.objects.create(name="Fiction")
        self.history = Category.objects.create(name="History")
        self.science = Category.objects.create(name="Science")
        self.book = Book.objects.create(name="Old Book")
        self.book.categories.set([self.fiction])
        self.resource = BookCategoryResource()
        self.idx = self.resource.get_diff_headers().index("categories")

    def _dataset(self):
        dataset = tablib.Dataset(headers=["id", "name", "categories"])
        # existing book: remove Fiction, add History and Science
        dataset.append([self.book.id, "Old Book", "History,Science"])
        # brand new book with one theme
        dataset.append(["", "New Book", "Fiction"])
        # invalid theme name which does not exist
        dataset.append(["", "Bad Book", "No Such Theme"])
        return dataset

    def _through_pks(self):
        return sorted(
            Book.categories.through.objects.values_list("book_id", "category_id")
        )

    def test_preview_reports_added_and_removed_themes_per_book(self):
        result = self.resource.import_data(self._dataset(), dry_run=True)

        update_row = result.rows[0]
        self.assertEqual(results.RowResult.IMPORT_TYPE_UPDATE, update_row.import_type)
        self.assertEqual(
            '<del style="background:#ffe6e6;">Fiction</del>'
            '<ins style="background:#e6ffe6;">History</ins>'
            '<ins style="background:#e6ffe6;">Science</ins>',
            update_row.diff[self.idx],
        )

        new_row = result.rows[1]
        self.assertEqual(results.RowResult.IMPORT_TYPE_NEW, new_row.import_type)
        self.assertEqual(
            '<ins style="background:#e6ffe6;">Fiction</ins>',
            new_row.diff[self.idx],
        )

    def test_preview_matches_the_associations_created_on_confirm(self):
        preview = self.resource.import_data(self._dataset(), dry_run=True)

        confirmed = self.resource.import_data(self._dataset(), dry_run=False)

        # the confirmed import reports the same m2m diff as the preview
        for preview_row, confirmed_row in zip(
            preview.valid_rows(), confirmed.valid_rows()
        ):
            self.assertEqual(preview_row.diff, confirmed_row.diff)

        old_book = Book.objects.get(name="Old Book")
        self.assertNotIn(self.fiction, old_book.categories.all())
        self.assertCountEqual(
            [self.history, self.science], list(old_book.categories.all())
        )
        new_book = Book.objects.get(name="New Book")
        self.assertCountEqual([self.fiction], list(new_book.categories.all()))
        self.assertFalse(Book.objects.filter(name="Bad Book").exists())

    def test_invalid_theme_name_marks_the_row_invalid(self):
        result = self.resource.import_data(self._dataset(), dry_run=True)

        self.assertTrue(result.has_validation_errors())
        self.assertEqual(1, len(result.invalid_rows))
        invalid_row = result.invalid_rows[0]
        self.assertEqual(3, invalid_row.number)
        self.assertEqual(
            invalid_row.field_specific_errors["categories"],
            [
                "No matching category found for the following value(s): "
                "No Such Theme."
            ],
        )
        # the raw, unparseable input is echoed back so the user can fix it
        values = dict(zip(result.diff_headers, invalid_row.values))
        self.assertEqual("No Such Theme", values["categories"])
        self.assertEqual(
            results.RowResult.IMPORT_TYPE_INVALID, result.rows[2].import_type
        )

    def test_preview_does_not_create_books_or_relations(self):
        books_before = Book.objects.count()
        through_before = self._through_pks()
        categories_before = Category.objects.count()

        self.resource.import_data(self._dataset(), dry_run=True)

        self.assertEqual(books_before, Book.objects.count())
        self.assertEqual(categories_before, Category.objects.count())
        self.assertEqual(through_before, self._through_pks())
        self.assertEqual(
            [self.fiction], list(Book.objects.get(name="Old Book").categories.all())
        )

    def test_unchanged_theme_is_not_reported_as_a_change(self):
        dataset = tablib.Dataset(headers=["id", "name", "categories"])
        dataset.append([self.book.id, "Old Book", "Fiction"])

        result = self.resource.import_data(dataset, dry_run=True)
        row = result.rows[0]
        self.assertEqual(results.RowResult.IMPORT_TYPE_UPDATE, row.import_type)
        self.assertEqual("<span>Fiction</span>", row.diff[self.idx])

    def test_multiple_themes_ignore_ordering(self):
        self.book.categories.set([self.history, self.science])
        dataset = tablib.Dataset(headers=["id", "name", "categories"])
        dataset.append([self.book.id, "Old Book", "Science,History"])

        result = self.resource.import_data(dataset, dry_run=True)
        self.assertEqual(
            "<span>History</span><span>Science</span>",
            result.rows[0].diff[self.idx],
        )

    def test_clearing_all_themes_shows_only_removals(self):
        dataset = tablib.Dataset(headers=["id", "name", "categories"])
        dataset.append([self.book.id, "Old Book", ""])

        result = self.resource.import_data(dataset, dry_run=True)
        self.assertEqual(
            '<del style="background:#ffe6e6;">Fiction</del>',
            result.rows[0].diff[self.idx],
        )
        # and the confirmed import really clears the relation
        self.resource.import_data(dataset, dry_run=False)
        self.assertEqual(0, Book.objects.get(name="Old Book").categories.count())

    def test_invalid_theme_is_invalid_even_with_skip_unchanged(self):
        class SkippingResource(BookCategoryResource):
            class Meta(BookCategoryResource.Meta):
                skip_unchanged = True

        dataset = tablib.Dataset(headers=["id", "name", "categories"])
        dataset.append(["", "Bad Book", "No Such Theme"])

        result = SkippingResource().import_data(dataset, dry_run=True)
        self.assertTrue(result.has_validation_errors())
        self.assertEqual(
            result.invalid_rows[0].field_specific_errors["categories"],
            [
                "No matching category found for the following value(s): "
                "No Such Theme."
            ],
        )
        self.assertFalse(Book.objects.filter(name="Bad Book").exists())

    def test_duplicate_invalid_values_are_all_reported(self):
        dataset = tablib.Dataset(headers=["id", "name", "categories"])
        dataset.append(["", "Bad Book", "Missing One,Fiction,Missing Two"])

        result = self.resource.import_data(dataset, dry_run=True)
        message = result.invalid_rows[0].field_specific_errors["categories"][0]
        self.assertIn("Missing One", message)
        self.assertIn("Missing Two", message)
        self.assertNotIn("Fiction", message)
