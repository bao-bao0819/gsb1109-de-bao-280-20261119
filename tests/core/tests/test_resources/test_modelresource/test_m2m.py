import tablib
from core.models import Author, Book, Category
from core.tests.resources import BookResource
from django.test import TestCase

from import_export import fields, resources, widgets
from import_export.results import RowResult


class ForeignKeyM2MTest(TestCase):
    def setUp(self):
        self.resource = BookResource()
        self.book = Book.objects.create(name="Some book")
        self.dataset = tablib.Dataset(headers=["id", "name", "author_email", "price"])
        row = [self.book.pk, "Some book", "test@example.com", "10.25"]
        self.dataset.append(row)

    def test_foreign_keys_export(self):
        author1 = Author.objects.create(name="Foo")
        self.book.author = author1
        self.book.save()

        dataset = self.resource.export(Book.objects.all())
        self.assertEqual(dataset.dict[0]["author"], author1.pk)

    def test_foreign_keys_import(self):
        author2 = Author.objects.create(name="Bar")
        headers = ["id", "name", "author"]
        row = [None, "FooBook", author2.pk]
        dataset = tablib.Dataset(row, headers=headers)
        self.resource.import_data(dataset, raise_errors=True)

        book = Book.objects.get(name="FooBook")
        self.assertEqual(book.author, author2)

    def test_m2m_export(self):
        cat1 = Category.objects.create(name="Cat 1")
        cat2 = Category.objects.create(name="Cat 2")
        self.book.categories.add(cat1)
        self.book.categories.add(cat2)

        dataset = self.resource.export(Book.objects.all())
        self.assertEqual(dataset.dict[0]["categories"], "%d,%d" % (cat1.pk, cat2.pk))

    def test_m2m_import(self):
        cat1 = Category.objects.create(name="Cat 1")
        headers = ["id", "name", "categories"]
        row = [None, "FooBook", str(cat1.pk)]
        dataset = tablib.Dataset(row, headers=headers)
        self.resource.import_data(dataset, raise_errors=True)

        book = Book.objects.get(name="FooBook")
        self.assertIn(cat1, book.categories.all())

    def test_m2m_import_clear(self):
        cat1 = Category.objects.create(name="Cat 1")
        self.book.categories.add(cat1)
        self.assertEqual(1, self.book.categories.count())
        headers = ["id", "name", "categories"]
        row = [self.book.pk, "FooBook", ""]
        dataset = tablib.Dataset(row, headers=headers)
        self.resource.import_data(dataset, raise_errors=True)

        book = Book.objects.get(name="FooBook")
        self.assertEqual(0, book.categories.count())

    def test_m2m_options_import(self):
        cat1 = Category.objects.create(name="Cat 1")
        cat2 = Category.objects.create(name="Cat 2")
        headers = ["id", "name", "categories"]
        row = [None, "FooBook", "Cat 1|Cat 2"]
        dataset = tablib.Dataset(row, headers=headers)

        class BookM2MResource(resources.ModelResource):
            categories = fields.Field(
                attribute="categories",
                widget=widgets.ManyToManyWidget(Category, field="name", separator="|"),
            )

            class Meta:
                model = Book

        resource = BookM2MResource()
        resource.import_data(dataset, raise_errors=True)
        book = Book.objects.get(name="FooBook")
        self.assertIn(cat1, book.categories.all())
        self.assertIn(cat2, book.categories.all())

    def test_m2m_add(self):
        cat1 = Category.objects.create(name="Cat 1")
        cat2 = Category.objects.create(name="Cat 2")
        cat3 = Category.objects.create(name="Cat 3")
        cat4 = Category.objects.create(name="Cat 4")
        headers = ["id", "name", "categories"]
        row = [None, "FooBook", "Cat 1|Cat 2"]
        dataset = tablib.Dataset(row, headers=headers)

        class BookM2MResource(resources.ModelResource):
            categories = fields.Field(
                attribute="categories",
                m2m_add=True,
                widget=widgets.ManyToManyWidget(Category, field="name", separator="|"),
            )

            class Meta:
                model = Book

        resource = BookM2MResource()
        resource.import_data(dataset, raise_errors=True)
        book = Book.objects.get(name="FooBook")
        self.assertIn(cat1, book.categories.all())
        self.assertIn(cat2, book.categories.all())
        self.assertNotIn(cat3, book.categories.all())
        self.assertNotIn(cat4, book.categories.all())

        row1 = [
            book.id,
            "FooBook",
            "Cat 1|Cat 2",
        ]  # This should have no effect, since Cat 1 and Cat 2 already exist
        row2 = [book.id, "FooBook", "Cat 3|Cat 4"]
        dataset = tablib.Dataset(row1, row2, headers=headers)
        resource.import_data(dataset, raise_errors=True)
        book2 = Book.objects.get(name="FooBook")
        self.assertEqual(book.id, book2.id)
        self.assertEqual(book.categories.count(), 4)
        self.assertIn(cat1, book2.categories.all())
        self.assertIn(cat2, book2.categories.all())
        self.assertIn(cat3, book2.categories.all())
        self.assertIn(cat4, book2.categories.all())


class ManyToManyOrderInsensitiveSkipTest(TestCase):
    """
    Re-running an import must not report a row as updated when the
    many-to-many column only changes its text ordering or repeats a value,
    because the set of related objects is unchanged.  A row which genuinely
    drops a related object must still be updated and the relation removed.
    """

    def _resource(self):
        class BookM2MResource(resources.ModelResource):
            categories = fields.Field(
                attribute="categories",
                widget=widgets.ManyToManyWidget(
                    Category, field="name", separator="|"
                ),
            )

            class Meta:
                model = Book
                skip_unchanged = True
                report_skipped = True

        return BookM2MResource()

    def setUp(self):
        self.green = Category.objects.create(name="green")
        self.blue = Category.objects.create(name="blue")
        self.equivalent_book = Book.objects.create(name="equivalent")
        self.equivalent_book.categories.set([self.green, self.blue])
        self.removed_book = Book.objects.create(name="removed")
        self.removed_book.categories.set([self.green, self.blue])

    def _dataset(self):
        dataset = tablib.Dataset(headers=["id", "name", "categories"])
        # the same relationship as "green|blue", reordered and duplicated
        dataset.append(
            [self.equivalent_book.pk, "equivalent", "blue|green|green"]
        )
        # the "green" relation is genuinely removed
        dataset.append([self.removed_book.pk, "removed", "blue"])
        return dataset

    def test_equivalent_row_skipped_and_removal_updates(self):
        result = self._resource().import_data(
            self._dataset(), dry_run=False, raise_errors=True
        )
        equivalent_row, removed_row = result.rows

        self.assertEqual(
            equivalent_row.import_type, RowResult.IMPORT_TYPE_SKIP
        )
        self.assertEqual(removed_row.import_type, RowResult.IMPORT_TYPE_UPDATE)

        self.equivalent_book.refresh_from_db()
        self.removed_book.refresh_from_db()
        self.assertSetEqual(
            set(self.equivalent_book.categories.values_list("name", flat=True)),
            {"green", "blue"},
        )
        self.assertSetEqual(
            set(self.removed_book.categories.values_list("name", flat=True)),
            {"blue"},
        )
        self.assertNotIn(self.green, self.removed_book.categories.all())

    def test_rerunning_the_file_keeps_the_equivalent_row_skipped(self):
        resource = self._resource()
        first = resource.import_data(
            self._dataset(), dry_run=False, raise_errors=True
        )
        second = resource.import_data(
            self._dataset(), dry_run=False, raise_errors=True
        )
        # the equivalent row is never reported as an update, even on rerun
        self.assertEqual(
            first.rows[0].import_type, RowResult.IMPORT_TYPE_SKIP
        )
        self.assertEqual(
            second.rows[0].import_type, RowResult.IMPORT_TYPE_SKIP
        )
        # the removal is applied on the first run; the same file then leaves
        # the already-updated row unchanged on the second run
        self.assertEqual(
            first.rows[1].import_type, RowResult.IMPORT_TYPE_UPDATE
        )
        self.assertEqual(
            second.rows[1].import_type, RowResult.IMPORT_TYPE_SKIP
        )
        self.equivalent_book.refresh_from_db()
        self.removed_book.refresh_from_db()
        self.assertSetEqual(
            set(self.equivalent_book.categories.values_list("name", flat=True)),
            {"green", "blue"},
        )
        self.assertSetEqual(
            set(self.removed_book.categories.values_list("name", flat=True)),
            {"blue"},
        )

    def test_equivalent_row_preview_shows_no_change(self):
        result = self._resource().import_data(
            self._dataset(), dry_run=True, raise_errors=True
        )
        index = self._resource().get_diff_headers().index("categories")
        diff_html = result.rows[0].diff[index]
        self.assertNotIn("<del", diff_html)
        self.assertNotIn("<ins", diff_html)
