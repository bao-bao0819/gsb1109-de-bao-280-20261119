from unittest import mock

import tablib
from core.models import Book, Category, UUIDBook
from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase

from import_export import exceptions, fields, resources, widgets
from import_export.instance_loaders import ModelInstanceLoader
from import_export.results import BatchError


class BulkTest(TestCase):
    def setUp(self):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True

        self.resource = _BookResource()
        rows = [(i + 1, "book_name") for i in range(10)]
        self.dataset = tablib.Dataset(*rows, headers=["id", "name"])

    def init_update_test_data(self, model=Book):
        [model.objects.create(name="book_name") for _ in range(10)]
        self.assertEqual(10, model.objects.count())
        rows = model.objects.all().values_list("id", "name")
        updated_rows = [(r[0], "UPDATED") for r in rows]
        self.dataset = tablib.Dataset(*updated_rows, headers=["id", "name"])


class BulkCreateTest(BulkTest):
    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_does_not_call_object_save(self, mock_bulk_create):
        with mock.patch("core.models.Book.save") as mock_obj_save:
            self.resource.import_data(self.dataset)
            mock_obj_save.assert_not_called()
        mock_bulk_create.assert_called_with(mock.ANY, batch_size=None)

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_batch_size_of_5(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 5

        resource = _BookResource()
        result = resource.import_data(self.dataset)
        self.assertEqual(2, mock_bulk_create.call_count)
        mock_bulk_create.assert_called_with(mock.ANY, batch_size=5)
        self.assertEqual(10, result.total_rows)

    @mock.patch("core.models.UUIDBook.objects.bulk_create")
    def test_bulk_create_uuid_model(self, mock_bulk_create):
        """Test create of a Model which defines uuid not pk (issue #1274)"""

        class _UUIDBookResource(resources.ModelResource):
            class Meta:
                model = UUIDBook
                use_bulk = True
                batch_size = 5
                fields = (
                    "id",
                    "name",
                )

        resource = _UUIDBookResource()
        result = resource.import_data(self.dataset)
        self.assertEqual(2, mock_bulk_create.call_count)
        mock_bulk_create.assert_called_with(mock.ANY, batch_size=5)
        self.assertEqual(10, result.total_rows)

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_no_batch_size(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = None

        resource = _BookResource()
        result = resource.import_data(self.dataset)
        self.assertEqual(1, mock_bulk_create.call_count)
        mock_bulk_create.assert_called_with(mock.ANY, batch_size=None)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["new"])

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_called_dry_run(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = None

        resource = _BookResource()
        result = resource.import_data(self.dataset, dry_run=True)
        self.assertEqual(1, mock_bulk_create.call_count)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["new"])

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_not_called_when_not_using_transactions(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            def import_data(
                self,
                dataset,
                dry_run=False,
                raise_errors=False,
                use_transactions=None,
                collect_failed_rows=False,
                **kwargs,
            ):
                # override so that we can enforce not using_transactions
                using_transactions = False
                return self.import_data_inner(
                    dataset,
                    dry_run,
                    raise_errors,
                    using_transactions,
                    collect_failed_rows,
                    **kwargs,
                )

            class Meta:
                model = Book
                use_bulk = True

        resource = _BookResource()
        resource.import_data(self.dataset, dry_run=True)
        mock_bulk_create.assert_not_called()

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_batch_size_of_4(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 4

        resource = _BookResource()
        result = resource.import_data(self.dataset)
        self.assertEqual(3, mock_bulk_create.call_count)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["new"])

    def test_no_changes_for_errors_if_use_transactions_enabled(self):
        with mock.patch("import_export.results.Result.has_errors") as mock_has_errors:
            mock_has_errors.return_val = True
            self.resource.import_data(self.dataset)
        self.assertEqual(0, Book.objects.count())

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_use_bulk_disabled(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = False

        resource = _BookResource()
        result = resource.import_data(self.dataset)
        mock_bulk_create.assert_not_called()
        self.assertEqual(10, Book.objects.count())
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["new"])

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_bad_batch_size_value(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = "a"

        resource = _BookResource()
        with self.assertRaises(ValueError):
            resource.import_data(self.dataset)
        mock_bulk_create.assert_not_called()

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_negative_batch_size_value_negative(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = -1

        resource = _BookResource()
        with self.assertRaisesMessage(
            ValueError, "Batch size must be a positive integer"
        ):
            resource.import_data(self.dataset)
        mock_bulk_create.assert_not_called()

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_negative_batch_size_value_0(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 0

        resource = _BookResource()
        with self.assertRaisesMessage(
            ValueError, "Batch size must be a positive integer"
        ):
            resource.import_data(self.dataset)
        mock_bulk_create.assert_not_called()

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_oversized_batch_size_value(self, mock_bulk_create):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 100

        resource = _BookResource()
        result = resource.import_data(self.dataset)
        self.assertEqual(1, mock_bulk_create.call_count)
        mock_bulk_create.assert_called_with(mock.ANY, batch_size=None)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["new"])

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_logs_exception(self, mock_bulk_create):
        e = ValidationError("invalid field")
        mock_bulk_create.side_effect = e

        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 100

        resource = _BookResource()
        with mock.patch("logging.Logger.debug") as mock_exception:
            resource.import_data(self.dataset)
            mock_exception.assert_called_with(e, exc_info=e)

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_raises_exception(self, mock_bulk_create):
        mock_bulk_create.side_effect = ValidationError("invalid field")

        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 100

        resource = _BookResource()
        with self.assertRaises(exceptions.ImportError):
            resource.import_data(self.dataset, raise_errors=True)

    @mock.patch("core.models.Book.objects.bulk_create")
    def test_bulk_create_exception_gathered_on_dry_run(self, mock_bulk_create):
        mock_bulk_create.side_effect = ValidationError("invalid field")

        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 100

        resource = _BookResource()
        result = resource.import_data(self.dataset, dry_run=True, raise_errors=False)
        self.assertTrue(result.has_errors())

    def test_m2m_not_called_for_bulk(self):
        mock_m2m_widget = mock.Mock(spec=widgets.ManyToManyWidget)

        class BookM2MResource(resources.ModelResource):
            categories = fields.Field(attribute="categories", widget=mock_m2m_widget)

            class Meta:
                model = Book
                use_bulk = True

        resource = BookM2MResource()
        self.dataset.append_col(["Cat 1|Cat 2"] * 10, header="categories")
        resource.import_data(self.dataset, raise_errors=True)
        mock_m2m_widget.assert_not_called()

    def test_force_init_instance(self):
        class _BookResource(resources.ModelResource):
            def get_instance(self, instance_loader, row):
                raise AssertionError("should not be called")

            class Meta:
                model = Book
                force_init_instance = True

        resource = _BookResource()
        self.assertIsNotNone(
            resource.get_or_init_instance(
                ModelInstanceLoader(resource), self.dataset[0]
            )
        )

    @mock.patch("import_export.resources.atomic_if_using_transaction")
    def test_no_sub_transaction_on_row_for_bulk(self, mock_atomic_if_using_transaction):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True

        resource = _BookResource()
        resource.import_data(self.dataset)
        self.assertIn(
            False, [x[0][0] for x in mock_atomic_if_using_transaction.call_args_list]
        )


class BulkUpdateTest(BulkTest):
    class _BookResource(resources.ModelResource):
        class Meta:
            model = Book
            use_bulk = True
            fields = ("id", "name")
            import_id_fields = ("id",)

    def setUp(self):
        super().setUp()
        self.init_update_test_data()
        self.resource = self._BookResource()

    def test_bulk_update(self):
        self.assertEqual(["name"], self.resource.get_bulk_update_fields())
        result = self.resource.import_data(self.dataset)
        [self.assertEqual("UPDATED", b.name) for b in Book.objects.all()]
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["update"])

    @mock.patch("core.models.Book.objects.bulk_update")
    def test_bulk_update_batch_size_of_4(self, mock_bulk_update):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 4

        resource = _BookResource()
        self.assertEqual(
            [
                "name",
                "author_id",
                "author_email",
                "imported",
                "published",
                "published_time",
                "price",
                "added",
                "categories",
            ],
            resource.get_bulk_update_fields(),
        )
        result = resource.import_data(self.dataset)
        self.assertEqual(3, mock_bulk_update.call_count)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["update"])

    @mock.patch("core.models.Book.objects.bulk_update")
    def test_bulk_update_batch_size_of_5(self, mock_bulk_update):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 5

        resource = _BookResource()
        result = resource.import_data(self.dataset)
        self.assertEqual(2, mock_bulk_update.call_count)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["update"])

    @mock.patch("core.models.Book.objects.bulk_update")
    def test_bulk_update_no_batch_size(self, mock_bulk_update):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = None

        resource = _BookResource()
        result = resource.import_data(self.dataset)
        self.assertEqual(1, mock_bulk_update.call_count)
        mock_bulk_update.assert_called_with(mock.ANY, mock.ANY, batch_size=None)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["update"])

    @mock.patch("core.models.Book.objects.bulk_update")
    def test_bulk_update_not_called_when_not_using_transactions(self, mock_bulk_update):
        class _BookResource(resources.ModelResource):
            def import_data(
                self,
                dataset,
                dry_run=False,
                raise_errors=False,
                use_transactions=None,
                collect_failed_rows=False,
                **kwargs,
            ):
                # override so that we can enforce not using_transactions
                using_transactions = False
                return self.import_data_inner(
                    dataset,
                    dry_run,
                    raise_errors,
                    using_transactions,
                    collect_failed_rows,
                    **kwargs,
                )

            class Meta:
                model = Book
                use_bulk = True

        resource = _BookResource()
        resource.import_data(self.dataset, dry_run=True)
        mock_bulk_update.assert_not_called()

    @mock.patch("core.models.Book.objects.bulk_update")
    def test_bulk_update_called_for_dry_run(self, mock_bulk_update):
        self.resource.import_data(self.dataset, dry_run=True)
        self.assertEqual(1, mock_bulk_update.call_count)

    @mock.patch("core.models.Book.objects.bulk_update")
    def test_bulk_not_called_when_use_bulk_disabled(self, mock_bulk_update):
        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = False

        resource = _BookResource()
        result = resource.import_data(self.dataset)
        self.assertEqual(10, Book.objects.count())
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["update"])
        mock_bulk_update.assert_not_called()

    @mock.patch("core.models.Book.objects.bulk_update")
    def test_bulk_update_logs_exception(self, mock_bulk_update):
        e = ValidationError("invalid field")
        mock_bulk_update.side_effect = e

        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True

        resource = _BookResource()
        with mock.patch("logging.Logger.debug") as mock_exception:
            resource.import_data(self.dataset)
            mock_exception.assert_called_with(e, exc_info=e)

    @mock.patch("core.models.Book.objects.bulk_update")
    def test_bulk_update_raises_exception(self, mock_bulk_update):
        e = ValidationError("invalid field")
        mock_bulk_update.side_effect = e

        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True

        resource = _BookResource()
        with self.assertRaises(exceptions.ImportError) as raised_exc:
            resource.import_data(self.dataset, raise_errors=True)
            self.assertEqual(e, raised_exc)

    def test_bulk_update_with_different_name_than_attribute(self):
        class _BookResource(resources.ModelResource):
            name_field = fields.Field(attribute="name", column_name="name")

            class Meta:
                model = Book
                use_bulk = True
                fields = ("id", "name_field")
                import_id_fields = ("id",)

        resource = _BookResource()
        self.assertEqual(["name"], resource.get_bulk_update_fields())
        result = resource.import_data(self.dataset)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["update"])
        self.assertEqual(10, Book.objects.filter(name="UPDATED").count())

    def test_bulk_update_with_readonly_field(self):
        class _BookResource(resources.ModelResource):
            name = fields.Field(attribute="name", column_name="name", readonly=True)

            class Meta:
                model = Book
                use_bulk = True
                fields = ("id", "name")
                import_id_fields = ("id",)
                skip_unchanged = True

        resource = _BookResource()
        self.assertEqual([], resource.get_bulk_update_fields())
        result = resource.import_data(self.dataset)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(0, result.totals["update"])
        self.assertEqual(10, Book.objects.filter(name="book_name").count())

    def test_bulk_update_with_related_field(self):
        class _BookResource(resources.ModelResource):
            author__name = fields.Field(
                attribute="author__name", column_name="author_name"
            )

            class Meta:
                model = Book
                use_bulk = True
                fields = ("id", "author__name")
                import_id_fields = ("id",)
                skip_unchanged = True

        resource = _BookResource()
        self.assertEqual([], resource.get_bulk_update_fields())
        result = resource.import_data(self.dataset)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(0, result.totals["update"])


class BulkUUIDBookUpdateTest(BulkTest):
    def setUp(self):
        super().setUp()
        self.init_update_test_data(model=UUIDBook)

    @mock.patch("core.models.UUIDBook.objects.bulk_update")
    def test_bulk_update_uuid_model(self, mock_bulk_update):
        """Test update of a Model which defines uuid not pk (issue #1274)"""

        class _UUIDBookResource(resources.ModelResource):
            class Meta:
                model = UUIDBook
                use_bulk = True
                batch_size = 5
                fields = (
                    "id",
                    "name",
                )

        resource = _UUIDBookResource()
        result = resource.import_data(self.dataset)
        self.assertEqual(2, mock_bulk_update.call_count)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["update"])


class BulkDeleteTest(BulkTest):
    class DeleteBookResource(resources.ModelResource):
        def for_delete(self, row, instance):
            return True

        class Meta:
            model = Book
            use_bulk = True
            # there are errors when diffing with mocks
            # therefore disable diff with this flag
            skip_diff = True

    def setUp(self):
        super().setUp()
        self.resource = self.DeleteBookResource()
        self.resource._meta.batch_size = 1000
        self.resource._meta.use_bulk = True
        self.init_update_test_data()

    @mock.patch("core.models.Book.delete")
    def test_bulk_delete_use_bulk_is_false(self, mock_obj_delete):
        self.resource._meta.use_bulk = False
        self.resource.import_data(self.dataset)
        self.assertEqual(10, mock_obj_delete.call_count)

    @mock.patch("core.models.Book.objects")
    def test_bulk_delete_batch_size_of_4(self, mock_obj_manager):
        self.resource._meta.batch_size = 4
        result = self.resource.import_data(self.dataset)
        self.assertEqual(3, mock_obj_manager.filter.return_value.delete.call_count)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["delete"])

    @mock.patch("core.models.Book.objects")
    def test_bulk_delete_batch_size_of_5(self, mock_obj_manager):
        self.resource._meta.batch_size = 5
        result = self.resource.import_data(self.dataset)
        self.assertEqual(2, mock_obj_manager.filter.return_value.delete.call_count)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["delete"])

    @mock.patch("core.models.Book.objects")
    def test_bulk_delete_batch_size_is_none(self, mock_obj_manager):
        self.resource._meta.batch_size = None
        result = self.resource.import_data(self.dataset)
        self.assertEqual(1, mock_obj_manager.filter.return_value.delete.call_count)
        self.assertEqual(10, result.total_rows)
        self.assertEqual(10, result.totals["delete"])

    @mock.patch("core.models.Book.objects")
    def test_bulk_delete_not_called_when_not_using_transactions(self, mock_obj_manager):
        class _BookResource(self.DeleteBookResource):
            def import_data(
                self,
                dataset,
                dry_run=False,
                raise_errors=False,
                use_transactions=None,
                collect_failed_rows=False,
                **kwargs,
            ):
                # override so that we can enforce not using_transactions
                using_transactions = False
                return self.import_data_inner(
                    dataset,
                    dry_run,
                    raise_errors,
                    using_transactions,
                    collect_failed_rows,
                    **kwargs,
                )

        resource = _BookResource()
        resource.import_data(self.dataset, dry_run=True)
        self.assertEqual(0, mock_obj_manager.filter.return_value.delete.call_count)

    @mock.patch("core.models.Book.objects")
    def test_bulk_delete_called_for_dry_run(self, mock_obj_manager):
        self.resource.import_data(self.dataset, dry_run=True)
        self.assertEqual(1, mock_obj_manager.filter.return_value.delete.call_count)

    @mock.patch("core.models.Book.objects")
    def test_bulk_delete_logs_exception(self, mock_obj_manager):
        e = Exception("invalid")
        mock_obj_manager.filter.return_value.delete.side_effect = e

        with mock.patch("logging.Logger.debug") as mock_exception:
            self.resource.import_data(self.dataset)
            mock_exception.assert_called_with(e, exc_info=mock.ANY)
            self.assertEqual(1, mock_exception.call_count)

    @mock.patch("core.models.Book.objects")
    def test_bulk_delete_raises_exception(self, mock_obj_manager):
        e = Exception("invalid")
        mock_obj_manager.filter.return_value.delete.side_effect = e

        with self.assertRaises(Exception) as raised_exc:
            self.resource.import_data(self.dataset, raise_errors=True)
            self.assertEqual(e, raised_exc)


class BulkUUIDBookDeleteTest(BulkTest):
    class DeleteBookResource(resources.ModelResource):
        def for_delete(self, row, instance):
            return True

        class Meta:
            model = UUIDBook
            use_bulk = True
            batch_size = 5

    def setUp(self):
        super().setUp()
        self.resource = self.DeleteBookResource()
        self.init_update_test_data(model=UUIDBook)

    def test_bulk_delete_batch_size_of_5(self):
        self.assertEqual(10, UUIDBook.objects.count())
        self.resource.import_data(self.dataset)
        self.assertEqual(0, UUIDBook.objects.count())


class NoTransactionCategoryResource(resources.ModelResource):
    """A bulk resource which forces commits without an outer transaction,
    so that a failed batch is genuinely not persisted while other batches
    are (the large-scale bulk import scenario)."""

    class Meta:
        model = Category
        use_bulk = True
        batch_size = 5

    def import_data(
        self,
        dataset,
        dry_run=False,
        raise_errors=False,
        use_transactions=None,
        collect_failed_rows=False,
        **kwargs,
    ):
        return self.import_data_inner(
            dataset,
            dry_run,
            raise_errors,
            False,
            collect_failed_rows,
            **kwargs,
        )


class BatchErrorReceiptTest(TransactionTestCase):
    def setUp(self):
        # Category.name has a unique constraint, providing a real database
        # constraint violation at the point bulk_create() is executed.
        self.occupied = Category.objects.create(id=100, name="device-100")
        # the first batch (rows 1-5) succeeds; the second batch (rows 6-10)
        # contains a duplicate name and therefore fails as a whole
        self.rows = [
            (1, "name-1"),
            (2, "name-2"),
            (3, "name-3"),
            (4, "name-4"),
            (5, "name-5"),
            (6, "name-6"),
            (7, "name-7"),
            (8, "name-8"),
            (9, "name-9"),
            (10, "device-100"),
        ]
        self.dataset = tablib.Dataset(*self.rows, headers=["id", "name"])

    def test_failed_batch_receipt_identifies_rows_and_object_ids(self):
        resource = NoTransactionCategoryResource()

        result = resource.import_data(self.dataset, raise_errors=False)

        self.assertTrue(result.has_errors())
        self.assertEqual(1, len(result.batch_errors))
        batch_error = result.batch_errors[0]
        self.assertEqual("create", batch_error.operation)
        self.assertEqual(6, batch_error.row_start)
        self.assertEqual(10, batch_error.row_end)
        self.assertEqual([6, 7, 8, 9, 10], batch_error.row_numbers)
        # the supplied device numbers are echoed back so the input can be
        # identified and resubmitted
        self.assertEqual([6, 7, 8, 9, 10], batch_error.object_ids)

    def test_successful_batch_is_persisted_and_failed_batch_is_not(self):
        resource = NoTransactionCategoryResource()

        result = resource.import_data(self.dataset, raise_errors=False)

        # only the first batch plus the pre-existing row are present
        self.assertEqual(6, Category.objects.count())
        self.assertTrue(Category.objects.filter(id=5).exists())
        self.assertFalse(Category.objects.filter(id=6).exists())
        self.assertFalse(Category.objects.filter(id=10).exists())
        # failed rows must not be reported as successfully imported
        self.assertEqual(5, result.totals["new"])
        self.assertEqual(5, result.totals["error"])
        failed_numbers = {r.row_number for r in result.rows if r.batch_failed}
        self.assertEqual({6, 7, 8, 9, 10}, failed_numbers)
        self.assertTrue(all(not r.batch_failed for r in result.rows[:5]))
        self.assertEqual(5, len(result.valid_rows()))

    def test_failed_rows_can_be_resubmitted_from_the_receipt(self):
        resource = NoTransactionCategoryResource()
        result = resource.import_data(self.dataset, raise_errors=False)
        batch_error = result.batch_errors[0]

        # rebuild the failed input from the row range / device numbers carried
        # in the receipt, correcting the offending value
        rows_by_id = {row[0]: row for row in self.rows}
        resubmit_rows = []
        for object_id in batch_error.object_ids:
            original = rows_by_id[object_id]
            if object_id == 10:
                resubmit_rows.append((original[0], "name-10"))
            else:
                resubmit_rows.append(original)
        resubmit_dataset = tablib.Dataset(*resubmit_rows, headers=["id", "name"])

        retry_result = NoTransactionCategoryResource().import_data(
            resubmit_dataset, raise_errors=False
        )

        self.assertFalse(retry_result.has_errors())
        self.assertEqual([], retry_result.batch_errors)
        self.assertEqual(11, Category.objects.count())
        self.assertEqual(5, retry_result.totals["new"])
        self.assertTrue(Category.objects.filter(id=10, name="name-10").exists())

    def test_all_batches_successful_reports_no_errors(self):
        dataset = tablib.Dataset(
            *[(i, f"ok-{i}") for i in range(1, 11)], headers=["id", "name"]
        )

        result = NoTransactionCategoryResource().import_data(
            dataset, raise_errors=False
        )

        self.assertFalse(result.has_errors())
        self.assertEqual([], result.batch_errors)
        self.assertEqual(10, result.totals["new"])
        self.assertEqual(0, result.totals["error"])
        self.assertTrue(all(not r.batch_failed for r in result.rows))
        self.assertEqual(11, Category.objects.count())


class BatchErrorMockTest(TestCase):
    @mock.patch("core.models.Category.objects.bulk_create")
    def test_batch_error_is_raised_when_raise_errors_is_true(self, mock_bulk_create):
        mock_bulk_create.side_effect = ValidationError("invalid field")
        resource = NoTransactionCategoryResource()
        dataset = tablib.Dataset(
            *[(i, f"name-{i}") for i in range(1, 6)], headers=["id", "name"]
        )

        with self.assertRaises(exceptions.ImportError):
            resource.import_data(dataset, raise_errors=True)

    @mock.patch("core.models.Category.objects.bulk_create")
    def test_batch_error_is_gathered_on_dry_run(self, mock_bulk_create):
        mock_bulk_create.side_effect = ValidationError("invalid field")

        class _CategoryResource(resources.ModelResource):
            class Meta:
                model = Category
                use_bulk = True
                batch_size = 5

        dataset = tablib.Dataset(
            *[(i, f"name-{i}") for i in range(1, 6)], headers=["id", "name"]
        )
        result = _CategoryResource().import_data(
            dataset, dry_run=True, raise_errors=False
        )

        self.assertTrue(result.has_errors())
        self.assertEqual(1, len(result.batch_errors))
        self.assertEqual("create", result.batch_errors[0].operation)
        self.assertEqual([1, 2, 3, 4, 5], result.batch_errors[0].row_numbers)

    def test_update_batch_failure_reports_existing_object_ids(self):
        books = [Book.objects.create(name=f"book-{i}") for i in range(5)]
        dataset = tablib.Dataset(
            *[(b.id, "UPDATED") for b in books], headers=["id", "name"]
        )

        class _BookResource(resources.ModelResource):
            class Meta:
                model = Book
                use_bulk = True
                batch_size = 5
                fields = ("id", "name")
                import_id_fields = ("id",)

        with mock.patch("core.models.Book.objects.bulk_update") as mock_bulk_update:
            mock_bulk_update.side_effect = ValidationError("invalid field")
            result = _BookResource().import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_errors())
        batch_error = result.batch_errors[0]
        self.assertEqual("update", batch_error.operation)
        self.assertEqual(1, batch_error.row_start)
        self.assertEqual(5, batch_error.row_end)
        self.assertEqual([b.id for b in books], batch_error.object_ids)
        self.assertEqual(5, result.totals["error"])

    def test_delete_batch_failure_reports_operation_and_row_range(self):
        books = [Book.objects.create(name=f"book-{i}") for i in range(5)]
        dataset = tablib.Dataset(
            *[(b.id, b.name) for b in books], headers=["id", "name"]
        )

        class _DeleteResource(resources.ModelResource):
            def for_delete(self, row, instance):
                return True

            class Meta:
                model = Book
                use_bulk = True
                batch_size = 5
                skip_diff = True

        with mock.patch("core.models.Book.objects.filter") as mock_filter:
            mock_filter.return_value.delete.side_effect = Exception("delete failed")
            result = _DeleteResource().import_data(dataset, raise_errors=False)

        self.assertTrue(result.has_errors())
        batch_error = result.batch_errors[0]
        self.assertEqual("delete", batch_error.operation)
        self.assertEqual(1, batch_error.row_start)
        self.assertEqual(5, batch_error.row_end)
        self.assertEqual([b.id for b in books], batch_error.object_ids)
        self.assertEqual(5, result.totals["error"])


class BatchErrorTest(TestCase):
    def test_repr_includes_operation_rows_and_ids(self):
        error = BatchError(
            error=ValidationError("boom"),
            row_start=11,
            row_end=20,
            object_ids=[11, 12],
            operation="create",
        )

        text = repr(error)
        self.assertIn("create", text)
        self.assertIn("11-20", text)
        self.assertIn("[11, 12]", text)

    def test_row_numbers_defaults_to_inclusive_range(self):
        error = BatchError(
            error=ValidationError("boom"),
            row_start=2,
            row_end=4,
            object_ids=[],
            operation="update",
        )

        self.assertEqual([2, 3, 4], error.row_numbers)
        self.assertEqual(2, error.number)
