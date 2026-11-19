import tablib
from core.models import Author, Book
from django.test import TestCase

from import_export import instance_loaders, resources
from import_export.results import RowResult


class BaseInstanceLoaderTest(TestCase):
    def test_get_instance(self):
        instance_loader = instance_loaders.BaseInstanceLoader(None)
        with self.assertRaises(NotImplementedError):
            instance_loader.get_instance(None)


class ModelInstanceLoaderTest(TestCase):
    def setUp(self):
        self.resource = resources.modelresource_factory(Book)()

    def test_get_instance_returns_None_when_params_is_empty(self):
        # setting an empty array of import_id_fields will mean
        # that 'params' is never set
        self.resource._meta.import_id_fields = []
        instance_loader = instance_loaders.ModelInstanceLoader(self.resource)
        self.assertIsNone(instance_loader.get_instance([]))


class CachedInstanceLoaderTest(TestCase):
    def setUp(self):
        self.resource = resources.modelresource_factory(Book)()
        self.dataset = tablib.Dataset(headers=["id", "name", "author_email"])
        self.book = Book.objects.create(name="Some book")
        self.book2 = Book.objects.create(name="Some other book")
        row = [str(self.book.pk), "Some book", "test@example.com"]
        self.dataset.append(row)
        self.instance_loader = instance_loaders.CachedInstanceLoader(
            self.resource, self.dataset
        )

    def test_all_instances(self):
        self.assertTrue(self.instance_loader.all_instances)
        self.assertEqual(len(self.instance_loader.all_instances), 1)
        self.assertEqual(list(self.instance_loader.all_instances), [self.book.pk])

    def test_get_instance(self):
        obj = self.instance_loader.get_instance(self.dataset.dict[0])
        self.assertEqual(obj, self.book)


class CachedInstanceLoaderWithAbsentImportIdFieldTest(TestCase):
    """Ensure that the cache is empty when the PK field is absent
    in the inbound dataset.
    """

    def setUp(self):
        self.resource = resources.modelresource_factory(Book)()
        self.dataset = tablib.Dataset(headers=["name", "author_email"])
        self.book = Book.objects.create(name="Some book")
        self.book2 = Book.objects.create(name="Some other book")
        row = ["Some book", "test@example.com"]
        self.dataset.append(row)
        self.instance_loader = instance_loaders.CachedInstanceLoader(
            self.resource, self.dataset
        )

    def test_all_instances(self):
        self.assertEqual(self.instance_loader.all_instances, {})
        self.assertEqual(len(self.instance_loader.all_instances), 0)

    def test_get_instance(self):
        obj = self.instance_loader.get_instance(self.dataset.dict[0])
        self.assertEqual(obj, None)


class CachedInstanceLoaderDuplicateImportIdTest(TestCase):
    """When the import id matches more than one row, ``CachedInstanceLoader``
    must raise ``MultipleObjectsReturned`` instead of silently returning one of
    them, consistent with ``ModelInstanceLoader`` (#2162).
    """

    def setUp(self):
        self.resource = resources.modelresource_factory(Book)()
        self.resource._meta.import_id_fields = ["name"]
        self.dataset = tablib.Dataset(headers=["id", "name", "author_email"])
        # Two existing rows share the same import id ("name").
        Book.objects.create(name="Dup")
        Book.objects.create(name="Dup")
        self.dataset.append(["", "Dup", "test@example.com"])

    def test_cached_loader_raises_for_duplicate_import_id(self):
        instance_loader = instance_loaders.CachedInstanceLoader(
            self.resource, self.dataset
        )
        with self.assertRaisesMessage(
            Book.MultipleObjectsReturned,
            "CachedInstanceLoader found multiple Book objects for import id "
            "field 'name'.",
        ) as cm:
            instance_loader.get_instance(self.dataset.dict[0])
        # The user-supplied import id value must not leak into the message: it
        # could otherwise end up in logs that render it unescaped. Only the
        # configured field name (not the row value) is reported.
        self.assertNotIn("Dup", str(cm.exception))

    def test_cached_loader_returns_unique_import_id_match(self):
        book = Book.objects.create(name="Unique")
        dataset = tablib.Dataset(headers=["id", "name", "author_email"])
        dataset.append(["", "Unique", "test@example.com"])
        instance_loader = instance_loaders.CachedInstanceLoader(self.resource, dataset)

        self.assertEqual(instance_loader.get_instance(dataset.dict[0]), book)

    def test_model_loader_raises_for_duplicate_import_id(self):
        # Documents the behavior that CachedInstanceLoader is made consistent
        # with: the uncached loader already raises here.
        instance_loader = instance_loaders.ModelInstanceLoader(
            self.resource, self.dataset
        )
        with self.assertRaises(Book.MultipleObjectsReturned):
            instance_loader.get_instance(self.dataset.dict[0])


class CachedInstanceLoaderCacheMutationTest(TestCase):
    """Instances persisted or deleted during an import are added to / removed
    from the cache, so that later rows of the same dataset see them."""

    def setUp(self):
        self.resource = resources.modelresource_factory(Book)()
        self.resource._meta.import_id_fields = ["name"]
        self.dataset = tablib.Dataset(headers=["id", "name", "author_email"])
        self.dataset.append(["", "Some book", "test@example.com"])
        self.instance_loader = instance_loaders.CachedInstanceLoader(
            self.resource, self.dataset
        )

    def test_add_instance_stores_instance_under_import_id(self):
        book = Book.objects.create(name="Some book")
        self.instance_loader.add_instance(book)
        self.assertEqual(self.instance_loader.all_instances, {"Some book": book})
        self.assertEqual(
            self.instance_loader.get_instance(self.dataset.dict[0]), book
        )

    def test_add_instance_ignores_instance_without_import_id_value(self):
        self.instance_loader.add_instance(Book(name=None))
        self.assertEqual(self.instance_loader.all_instances, {})

    def test_add_instance_noop_when_pk_column_missing_from_dataset(self):
        dataset = tablib.Dataset(headers=["id", "author_email"])
        dataset.append(["1", "test@example.com"])
        instance_loader = instance_loaders.CachedInstanceLoader(
            self.resource, dataset
        )
        instance_loader.add_instance(Book.objects.create(name="Some book"))
        self.assertEqual(instance_loader.all_instances, {})

    def test_remove_instance_evicts_instance(self):
        book = Book.objects.create(name="Some book")
        instance_loader = instance_loaders.CachedInstanceLoader(
            self.resource, self.dataset
        )
        self.assertEqual(
            instance_loader.get_instance(self.dataset.dict[0]), book
        )
        instance_loader.remove_instance(book)
        self.assertEqual(instance_loader.all_instances, {})
        self.assertIsNone(instance_loader.get_instance(self.dataset.dict[0]))

    def test_remove_instance_ignores_unknown_instance(self):
        self.instance_loader.remove_instance(Book(name="Unknown"))
        self.assertEqual(self.instance_loader.all_instances, {})


class CachedInstanceLoaderImportTest(TestCase):
    """End-to-end tests for ``CachedInstanceLoader`` with a resource scoped to
    a subset of the table (e.g. one 'brand' of a shared product table), where
    the import id is only unique within that subset.  Results must match
    ``ModelInstanceLoader`` for repeated import ids within one dataset.
    """

    def setUp(self):
        self.author = Author.objects.create(name="Author 1")
        self.other_author = Author.objects.create(name="Author 2")

    def get_resource(
        self, author, loader_class=instance_loaders.CachedInstanceLoader
    ):
        class ScopedBookResource(resources.ModelResource):
            class Meta:
                model = Book
                import_id_fields = ["name"]
                instance_loader_class = loader_class

            def get_queryset(self):
                return Book.objects.filter(author=author)

            def init_instance(self, row=None):
                instance = super().init_instance(row)
                instance.author = author
                return instance

            def for_delete(self, row, instance):
                return row.get("delete") == "1"

        return ScopedBookResource()

    def get_dataset(self):
        dataset = tablib.Dataset(headers=["name", "author_email"])
        dataset.append(["Book 1", "first@example.com"])
        dataset.append(["Book 1", "second@example.com"])
        return dataset

    def test_repeated_import_id_updates_instance_created_earlier_in_file(self):
        result = self.get_resource(self.author).import_data(
            self.get_dataset(), raise_errors=True
        )

        self.assertEqual(result.totals[RowResult.IMPORT_TYPE_NEW], 1)
        self.assertEqual(result.totals[RowResult.IMPORT_TYPE_UPDATE], 1)
        book = Book.objects.get()
        self.assertEqual(book.author, self.author)
        self.assertEqual(book.author_email, "second@example.com")

    def test_repeated_import_id_consistent_with_model_instance_loader(self):
        cached_result = self.get_resource(self.author).import_data(
            self.get_dataset(), raise_errors=True
        )
        cached_state = list(
            Book.objects.values("name", "author_email", "author_id")
        )

        Book.objects.all().delete()
        model_result = self.get_resource(
            self.author, instance_loaders.ModelInstanceLoader
        ).import_data(self.get_dataset(), raise_errors=True)
        model_state = list(Book.objects.values("name", "author_email", "author_id"))

        self.assertEqual(cached_result.totals, model_result.totals)
        self.assertEqual(cached_state, model_state)

    def test_cache_does_not_touch_instance_outside_resource_queryset(self):
        other_book = Book.objects.create(
            name="Book 1",
            author=self.other_author,
            author_email="orig@example.com",
        )

        result = self.get_resource(self.author).import_data(
            self.get_dataset(), raise_errors=True
        )

        self.assertEqual(result.totals[RowResult.IMPORT_TYPE_NEW], 1)
        self.assertEqual(result.totals[RowResult.IMPORT_TYPE_UPDATE], 1)
        other_book.refresh_from_db()
        self.assertEqual(other_book.author_email, "orig@example.com")
        self.assertEqual(Book.objects.count(), 2)
        new_book = Book.objects.get(author=self.author)
        self.assertEqual(new_book.author_email, "second@example.com")

    def test_deleted_instance_can_be_recreated_later_in_file(self):
        Book.objects.create(
            name="Book 1", author=self.author, author_email="old@example.com"
        )
        dataset = tablib.Dataset(headers=["name", "author_email", "delete"])
        dataset.append(["Book 1", "", "1"])
        dataset.append(["Book 1", "new@example.com", ""])

        result = self.get_resource(self.author).import_data(
            dataset, raise_errors=True
        )

        self.assertEqual(result.totals[RowResult.IMPORT_TYPE_DELETE], 1)
        self.assertEqual(result.totals[RowResult.IMPORT_TYPE_NEW], 1)
        book = Book.objects.get()
        self.assertEqual(book.author_email, "new@example.com")

    def test_dry_run_repeated_import_id_consistent_with_model_instance_loader(
        self,
    ):
        cached_result = self.get_resource(self.author).import_data(
            self.get_dataset(), dry_run=True
        )
        model_result = self.get_resource(
            self.author, instance_loaders.ModelInstanceLoader
        ).import_data(self.get_dataset(), dry_run=True)

        self.assertEqual(cached_result.totals, model_result.totals)
        self.assertEqual(Book.objects.count(), 0)

    def test_bulk_mode_does_not_cache_created_instances(self):
        # With bulk imports, created instances are buffered and not visible to
        # later rows, consistent with ModelInstanceLoader.
        dataset = self.get_dataset()
        for loader_class in (
            instance_loaders.CachedInstanceLoader,
            instance_loaders.ModelInstanceLoader,
        ):
            with self.subTest(loader_class=loader_class):
                Book.objects.all().delete()
                resource = self.get_resource(self.author, loader_class)
                resource._meta.use_bulk = True
                result = resource.import_data(dataset, raise_errors=True)
                self.assertEqual(result.totals[RowResult.IMPORT_TYPE_NEW], 2)
                self.assertEqual(Book.objects.count(), 2)
