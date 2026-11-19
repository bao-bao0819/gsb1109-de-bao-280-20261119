from unittest.mock import patch

from core.models import Book, Category
from core.tests.admin_integration.mixins import AdminTestMixin
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from import_export import fields, resources, widgets
from import_export.constants import FORM_FIELD_PREFIX


class BookCategoryResource(resources.ModelResource):
    categories = fields.Field(
        attribute="categories",
        column_name="categories",
        widget=widgets.ManyToManyWidget(Category, field="name", separator="|"),
    )

    class Meta:
        model = Book
        fields = ("id", "name", "categories")


class ManyToManyPreviewAdminTest(AdminTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.fiction = Category.objects.create(name="Fiction")
        self.history = Category.objects.create(name="History")
        self.science = Category.objects.create(name="Science")
        self.book = Book.objects.create(name="Old Book")
        self.book.categories.set([self.fiction])

    def _csv(self, include_invalid_row=True):
        rows = [
            "id,name,categories",
            f"{self.book.id},Old Book,History|Science",
            ",New Book,Fiction",
        ]
        if include_invalid_row:
            rows.append(",Bad Book,No Such Theme")
        return "\n".join(rows) + "\n"

    def _post_preview(self, csv_content):
        upload = SimpleUploadedFile(
            "books.csv", csv_content.encode(), content_type="text/csv"
        )
        data = {
            f"{FORM_FIELD_PREFIX}format": "0",
            "import_file": upload,
        }
        with patch(
            "core.admin.BookAdmin.get_import_resource_classes",
            return_value=[BookCategoryResource],
        ):
            return self.client.post(self.book_import_url, data)

    def test_preview_shows_added_removed_themes_and_invalid_row(self):
        through_before = sorted(
            Book.categories.through.objects.values_list("book_id", "category_id")
        )
        books_before = Book.objects.count()

        response = self._post_preview(self._csv())
        result = response.context["result"]

        self.assertTrue(result.has_validation_errors())
        # a batch containing an invalid row cannot be confirmed as-is
        self.assertNotIn("confirm_form", response.context)

        html = response.content.decode()
        # the existing book's removed and added themes are both visible
        self.assertIn(
            '<del style="background:#ffe6e6;">Fiction</del>'
            '<ins style="background:#e6ffe6;">History</ins>'
            '<ins style="background:#e6ffe6;">Science</ins>',
            html,
        )
        # the new book shows its single theme as an addition
        self.assertIn('<ins style="background:#e6ffe6;">Fiction</ins>', html)
        # the invalid row points at the bad input
        self.assertIn(
            "No matching category found for the following value(s): No Such Theme.",
            html,
        )
        self.assertIn("No Such Theme", html)

        # the preview must not have written any books or relations
        self.assertEqual(books_before, Book.objects.count())
        self.assertEqual(
            through_before,
            sorted(
                Book.categories.through.objects.values_list("book_id", "category_id")
            ),
        )
        self.assertEqual(
            [self.fiction], list(Book.objects.get(name="Old Book").categories.all())
        )

    def test_confirmed_import_applies_exactly_the_previewed_associations(self):
        response = self._post_preview(self._csv(include_invalid_row=False))
        result = response.context["result"]
        self.assertFalse(result.has_validation_errors())
        self.assertIn("confirm_form", response.context)
        categories_idx = result.diff_headers.index("categories")
        update_diff = result.rows[0].diff[categories_idx]
        new_diff = result.rows[1].diff[categories_idx]
        self.assertIn("<del", update_diff)
        self.assertIn("<ins", update_diff)
        self.assertIn("<ins", new_diff)

        data = response.context["confirm_form"].initial
        self._prepend_form_prefix(data)
        with patch(
            "core.admin.BookAdmin.get_import_resource_classes",
            return_value=[BookCategoryResource],
        ):
            self.client.post(self.book_process_import_url, data, follow=True)

        old_book = Book.objects.get(name="Old Book")
        self.assertCountEqual(
            [self.history, self.science], list(old_book.categories.all())
        )
        new_book = Book.objects.get(name="New Book")
        self.assertCountEqual([self.fiction], list(new_book.categories.all()))
        self.assertFalse(Book.objects.filter(name="Bad Book").exists())
