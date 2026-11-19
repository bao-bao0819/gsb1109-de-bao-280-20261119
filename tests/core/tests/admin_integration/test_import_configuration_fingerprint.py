import os.path
from unittest.mock import patch

from core.admin import BookAdmin
from core.models import Book
from core.tests.admin_integration.mixins import AdminTestMixin
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.testcases import TestCase
from django.utils.translation import gettext_lazy as _

from import_export.fields import Field
from import_export.resources import ModelResource
from import_export.tmp_storages import TempFolderStorage
from import_export.widgets import DecimalWidget


# Header 'cost' maps onto the Book.price attribute.
COST_CSV = b"id,cost\n1,12.50\n"
# Header 'price' maps onto the Book.price attribute.
PRICE_CSV = b"id,price\n1,12.50\n"


class CostMappedBookResource(ModelResource):
    price = Field(attribute="price", column_name="cost", widget=DecimalWidget())

    class Meta:
        model = Book
        fields = ("id", "price")


class PriceMappedBookResource(ModelResource):
    price = Field(attribute="price", column_name="price", widget=DecimalWidget())

    class Meta:
        model = Book
        fields = ("id", "price")


class DisplayVariantBookResource(CostMappedBookResource):
    # Identical import mapping to CostMappedBookResource, only the reviewer
    # facing display name differs.
    class Meta:
        model = Book
        fields = ("id", "price")
        name = "Reviewer-facing price import"


class ImportConfigurationFingerprintTests(AdminTestMixin, TestCase):
    def _preview(self, resource_class, file_content=COST_CSV, file_name="prices.csv"):
        with patch.object(BookAdmin, "resource_classes", [resource_class]):
            upload = SimpleUploadedFile(file_name, file_content)
            response = self.client.post(
                self.book_import_url,
                {"django-import-export-format": "0", "import_file": upload},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("confirm_form", response.context)
        return response

    def _confirm_data(self, confirm_form):
        data = dict(confirm_form.initial)
        self._prepend_form_prefix(data)
        return data

    def _confirm(self, confirm_form):
        return self.client.post(
            self.book_process_import_url,
            self._confirm_data(confirm_form),
            follow=True,
        )

    def test_confirm_form_carries_fingerprint(self):
        response = self._preview(CostMappedBookResource)

        fingerprint = response.context["confirm_form"].initial[
            "import_configuration_fingerprint"
        ]
        self.assertEqual(
            fingerprint,
            CostMappedBookResource().get_import_configuration_fingerprint(),
        )
        # the fingerprint is posted back through a hidden input
        self.assertContains(
            response,
            'name="import_configuration_fingerprint"',
        )

    def test_unchanged_configuration_imports_data(self):
        with patch.object(BookAdmin, "resource_classes", [CostMappedBookResource]):
            response = self._preview(CostMappedBookResource)
            response = self._confirm(response.context["confirm_form"])

        self.assertEqual(Book.objects.count(), 1)
        self.assertEqual(Book.objects.get(id=1).price, 12.50)
        self.assertContains(response, "Import finished")

    def test_mapping_change_between_preview_and_confirm_blocks_import(self):
        # Preview while the 'cost' column is mapped onto Book.price.
        response = self._preview(CostMappedBookResource)
        confirm_form = response.context["confirm_form"]
        tmp_storage_name = confirm_form.initial["import_file_name"]
        tmp_storage = TempFolderStorage(name=tmp_storage_name)
        self.addCleanup(
            lambda: os.path.exists(tmp_storage.get_full_path())
            and tmp_storage.remove()
        )

        # The resource mapping is changed before the reviewer confirms:
        # the same file would now be read with a 'price' column mapping.
        with patch.object(BookAdmin, "resource_classes", [PriceMappedBookResource]):
            response = self.client.post(
                self.book_process_import_url,
                self._confirm_data(confirm_form),
                follow=True,
            )

        # No data may be written with the new mapping.
        self.assertEqual(Book.objects.count(), 0)
        # The reviewer is sent back to the import page to preview again.
        self.assertTrue(
            response.redirect_chain[0][0].endswith("/admin/core/book/import/")
        )
        messages = [str(message) for message in response.context["messages"]]
        self.assertIn(
            str(
                _(
                    "The resource configuration changed after this preview "
                    "was generated. Please preview the import again before "
                    "confirming it."
                )
            ),
            messages,
        )
        # the uploaded file is retained so it can be previewed again
        self.assertTrue(os.path.exists(tmp_storage.get_full_path()))

    def test_repreview_after_mapping_change_then_confirm_imports(self):
        # Stale preview is refused.
        response = self._preview(CostMappedBookResource)
        stale_form = response.context["confirm_form"]
        with patch.object(BookAdmin, "resource_classes", [PriceMappedBookResource]):
            blocked = self.client.post(
                self.book_process_import_url,
                self._confirm_data(stale_form),
            )
            self.assertEqual(blocked.status_code, 302)
            self.assertEqual(Book.objects.count(), 0)

            # A fresh preview is generated against the current configuration,
            # using a file whose header matches the new mapping.
            response = self._preview(
                PriceMappedBookResource,
                file_content=PRICE_CSV,
                file_name="prices-new.csv",
            )
            response = self._confirm(response.context["confirm_form"])

        self.assertEqual(Book.objects.count(), 1)
        self.assertEqual(Book.objects.get(id=1).price, 12.50)
        self.assertContains(response, "Import finished")

    def test_display_only_change_does_not_invalidate_preview(self):
        # Preview with the resource, then swap to a resource whose import
        # mapping is identical but whose reviewer-facing name differs.
        response = self._preview(CostMappedBookResource)

        with patch.object(BookAdmin, "resource_classes", [DisplayVariantBookResource]):
            response = self._confirm(response.context["confirm_form"])

        self.assertEqual(Book.objects.count(), 1)
        self.assertEqual(Book.objects.get(id=1).price, 12.50)
        self.assertContains(response, "Import finished")

    def test_missing_fingerprint_keeps_legacy_behavior(self):
        # Confirm forms which do not carry a fingerprint (for example custom
        # forms rendered before this feature) must not be blocked.  This also
        # demonstrates what the fingerprint protects against: under the new
        # mapping the file's 'cost' column is ignored, so the row is written
        # with price left empty rather than the previewed 12.50.
        response = self._preview(CostMappedBookResource)
        data = response.context["confirm_form"].initial
        data.pop("import_configuration_fingerprint")
        self._prepend_form_prefix(data)

        with patch.object(BookAdmin, "resource_classes", [PriceMappedBookResource]):
            response = self.client.post(
                self.book_process_import_url, data, follow=True
            )

        self.assertEqual(Book.objects.count(), 1)
        self.assertIsNone(Book.objects.get(id=1).price)
        self.assertContains(response, "Import finished")
