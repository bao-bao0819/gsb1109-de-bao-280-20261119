import os
from unittest.mock import patch

from core.models import Book
from core.tests.admin_integration.mixins import AdminTestMixin
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.test.utils import override_settings
from django.utils.translation import gettext_lazy as _

from import_export.constants import FORM_FIELD_PREFIX
from import_export.tmp_storages import CacheStorage, TempFolderStorage


class StaleImportUploadTests(AdminTestMixin, TestCase):
    """
    The confirmation step is opened some time after the preview step, so the
    temporary upload may have been cleaned up in the meantime. In that case the
    user must be told explicitly to re-upload rather than facing a server
    error. When the file still exists but can no longer be parsed, the error
    must identify the confirm-import step as the point of failure.
    """

    success_message = _(
        "Import finished: {} new, {} updated, {} deleted and {} skipped {}."
    ).format(1, 0, 0, 0, Book._meta.verbose_name_plural)

    def _confirm_data(self, preview_response):
        data = preview_response.context["confirm_form"].initial
        self._prepend_form_prefix(data)
        return data

    def test_removed_temp_file_prompts_reupload(self):
        preview = self._do_import_post(self.book_import_url, "books.csv")
        tmp_name = preview.context["confirm_form"].initial["import_file_name"]
        storage = TempFolderStorage(name=tmp_name)
        full_path = storage.get_full_path()
        storage.remove()

        response = self._post_url_response(
            self.book_process_import_url,
            self._confirm_data(preview),
            follow=True,
        )

        self.assertRedirects(response, self.book_import_url)
        self.assertContains(response, "has expired or was removed")
        self.assertContains(response, "books.csv")
        self.assertFalse(os.path.exists(full_path))
        self.assertFalse(Book.objects.exists())

    def test_temp_file_vanishing_between_check_and_read_prompts_reupload(self):
        preview = self._do_import_post(self.book_import_url, "books.csv")

        with patch.object(TempFolderStorage, "exists", return_value=True):
            with patch.object(
                TempFolderStorage,
                "read",
                side_effect=FileNotFoundError("removed concurrently"),
            ):
                response = self._post_url_response(
                    self.book_process_import_url,
                    self._confirm_data(preview),
                    follow=True,
                )

        self.assertRedirects(response, self.book_import_url)
        self.assertContains(response, "has expired or was removed")
        self.assertFalse(Book.objects.exists())
        TempFolderStorage(
            name=preview.context["confirm_form"].initial["import_file_name"]
        ).remove()

    def test_existing_but_unparseable_temp_file_reports_confirm_step(self):
        # xlsx is format index 2 in the default format list
        preview = self._do_import_post(
            self.book_import_url, "books.xlsx", input_format=2
        )
        tmp_name = preview.context["confirm_form"].initial["import_file_name"]
        storage = TempFolderStorage(name=tmp_name)
        full_path = storage.get_full_path()
        with open(full_path, "wb") as f:
            f.write(b"this is no longer a valid workbook")

        response = self._post_url_response(
            self.book_process_import_url,
            self._confirm_data(preview),
            follow=True,
        )

        self.assertRedirects(response, self.book_import_url)
        self.assertContains(response, "failed validation at the confirm-import step")
        self.assertContains(response, "BadZipFile")
        # the unparseable file is left in place; nothing was imported
        self.assertTrue(os.path.exists(full_path))
        self.assertFalse(Book.objects.exists())
        storage.remove()

    @override_settings(
        IMPORT_EXPORT_TMP_STORAGE_CLASS="import_export.tmp_storages.CacheStorage"
    )
    def test_expired_cache_entry_prompts_reupload(self):
        preview = self._do_import_post(self.book_import_url, "books.csv")
        tmp_name = preview.context["confirm_form"].initial["import_file_name"]
        cache.delete(CacheStorage.CACHE_PREFIX + tmp_name)

        response = self._post_url_response(
            self.book_process_import_url,
            self._confirm_data(preview),
            follow=True,
        )

        self.assertRedirects(response, self.book_import_url)
        self.assertContains(response, "has expired or was removed")
        self.assertFalse(Book.objects.exists())

    @override_settings(
        IMPORT_EXPORT_TMP_STORAGE_CLASS="import_export.tmp_storages.CacheStorage"
    )
    def test_cache_read_returning_none_prompts_reupload(self):
        preview = self._do_import_post(self.book_import_url, "books.csv")
        tmp_name = preview.context["confirm_form"].initial["import_file_name"]
        self.addCleanup(CacheStorage(name=tmp_name).remove)

        with patch.object(CacheStorage, "exists", return_value=True):
            with patch.object(CacheStorage, "read", return_value=None):
                response = self._post_url_response(
                    self.book_process_import_url,
                    self._confirm_data(preview),
                    follow=True,
                )

        self.assertRedirects(response, self.book_import_url)
        self.assertContains(response, "has expired or was removed")
        self.assertFalse(Book.objects.exists())

    def test_same_filename_uploads_keep_each_user_preview_isolated(self):
        # first user uploads books.csv and leaves the confirmation page open
        preview_one = self._do_import_post(self.book_import_url, "books.csv")
        name_one = preview_one.context["confirm_form"].initial["import_file_name"]

        # a second user uploads a file with the same original name
        user_two = User.objects.create_user(
            "admin-two", "two@example.com", "password"
        )
        user_two.is_staff = True
        user_two.is_superuser = True
        user_two.save()
        client_two = Client()
        client_two.login(username="admin-two", password="password")

        filename = os.path.join(
            os.path.dirname(__file__),
            os.path.pardir,
            os.path.pardir,
            "exports",
            "books.csv",
        )
        with open(filename, "rb") as f:
            preview_two = client_two.post(
                self.book_import_url,
                {f"{FORM_FIELD_PREFIX}format": "0", "import_file": f},
            )
        name_two = preview_two.context["confirm_form"].initial["import_file_name"]

        # every upload gets its own opaque storage key regardless of filename
        self.assertNotEqual(name_one, name_two)
        self.assertTrue(TempFolderStorage(name=name_one).exists())
        self.assertTrue(TempFolderStorage(name=name_two).exists())

        # user one's temporary file is cleaned up before they confirm
        TempFolderStorage(name=name_one).remove()

        response_one = self._post_url_response(
            self.book_process_import_url,
            self._confirm_data(preview_one),
            follow=True,
        )
        self.assertContains(response_one, "has expired or was removed")
        # user two's preview input is untouched by user one's failure
        self.assertTrue(TempFolderStorage(name=name_two).exists())
        self.assertFalse(Book.objects.exists())

        # user two confirms their own still-valid preview and imports once
        data_two = preview_two.context["confirm_form"].initial
        self._prepend_form_prefix(data_two)
        response_two = client_two.post(
            self.book_process_import_url, data_two, follow=True
        )
        self.assertContains(response_two, self.success_message)
        self.assertEqual(1, Book.objects.count())
        self.assertFalse(TempFolderStorage(name=name_two).exists())
