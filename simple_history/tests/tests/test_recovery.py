from datetime import datetime
from unittest.mock import patch

from django.contrib.admin.utils import quote
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from simple_history.recovery import (
    RECOVERY_MODE_FULL,
    RECOVERY_MODE_PARTIAL,
    RecoveryPreviewStore,
    StaleRecoveryPreview,
    UnrecoverableRelationsError,
    apply_recovery,
    build_recovery_preview,
)

from ..models import BillingGroup, GroupQuota, ReconciledConfig

User = get_user_model()

IDENTITY_FIELDS = {"tests.billinggroup": "code"}
T0 = datetime(2026, 1, 1, 12, 0, 0)
T1 = datetime(2026, 1, 2, 12, 0, 0)
T2 = datetime(2026, 1, 3, 12, 0, 0)


def recovery_url(obj, history_id):
    return reverse(
        "admin:tests_reconciledconfig_recovery",
        args=[quote(obj.pk), quote(history_id)],
    )


class RecoveryEngineTest(TestCase):
    def setUp(self):
        self.config = ReconciledConfig(name="cfg", status="ok")
        self.config._history_date = T0
        self.config.save()

        self.group_a = BillingGroup(code="GA", name="Group A")
        self.group_a._history_date = T0
        self.group_a.save()
        self.group_b = BillingGroup(code="GB", name="Group B")
        self.group_b._history_date = T0
        self.group_b.save()

        self.quota_a = GroupQuota(
            config=self.config, group=self.group_a, quota=6
        )
        self.quota_a._history_date = T0
        self.quota_a.save()
        self.quota_b = GroupQuota(
            config=self.config, group=self.group_b, quota=12
        )
        self.quota_b._history_date = T0
        self.quota_b.save()

        self.good_history_id = self.config.history.first().history_id

    def _good_version(self):
        return self.config.history.get(pk=self.good_history_id)

    def _bad_import(self, *, relink=True):
        """Simulate the bad import: replace a group reusing its primary key."""
        old_pk = self.group_a.pk
        self.group_a.delete()
        imposter = BillingGroup.objects.create(
            pk=old_pk, code="GA-DIFFERENT", name="Imposter"
        )
        if relink:
            self.quota_a.group = imposter
        else:
            self.quota_a.group = None
        self.quota_a._history_date = T1
        self.quota_a.save()

        self.quota_b.quota = 20
        self.quota_b._history_date = T1
        self.quota_b.save()

        self.config.name = "cfg-broken"
        self.config.status = "bad"
        self.config._history_date = T1
        self.config.save()
        return imposter

    def test_preview_before_mutation_shows_both_recoverable_associations(self):
        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)

        self.assertEqual(len(preview.items), 2)
        self.assertTrue(all(item.is_recoverable for item in preview.items))
        self.assertTrue(
            all(
                item.status == "current"
                for item in preview.items
            )
        )
        quotas = {
            item.historical_pk: item.historical.quota for item in preview.items
        }
        self.assertEqual(quotas[self.quota_a.pk], 6)
        self.assertEqual(quotas[self.quota_b.pk], 12)

    def test_full_recovery_on_fresh_preview_restores_everything(self):
        original_group_pk = self.group_a.pk
        self._bad_import()

        # Make the deleted group recognizable again by restoring a group with
        # the same stable business code and re-attaching the old association.
        BillingGroup.objects.filter(code="GA-DIFFERENT").delete()
        restored_group = BillingGroup.objects.create(
            pk=original_group_pk, code="GA", name="Group A restored"
        )
        self.quota_a.group = restored_group
        self.quota_a._history_date = T2
        self.quota_a.save()

        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)
        self.assertEqual(preview.unrecoverable_items, [])

        result = apply_recovery(
            preview, RECOVERY_MODE_FULL, identity_fields=IDENTITY_FIELDS
        )

        self.config.refresh_from_db()
        self.quota_a.refresh_from_db()
        self.quota_b.refresh_from_db()
        self.assertEqual(self.config.status, "ok")
        self.assertEqual(self.config.name, "cfg")
        self.assertEqual(self.quota_a.quota, 6)
        self.assertEqual(self.quota_b.quota, 12)
        self.assertEqual(result.skipped_items, [])

    def test_same_stale_preview_token_rejected_for_full_then_used_for_partial(self):
        # The preview (and its token) is produced while both groups still exist.
        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)
        self._bad_import()

        history_counts = (
            ReconciledConfig.history.count(),
            GroupQuota.history.count(),
        )

        # Full recovery rejects the outdated preview without writing anything.
        with self.assertRaises(StaleRecoveryPreview):
            apply_recovery(preview, RECOVERY_MODE_FULL, identity_fields=IDENTITY_FIELDS)
        self.assertEqual(
            (ReconciledConfig.history.count(), GroupQuota.history.count()),
            history_counts,
        )

        # The same preview can still drive a partial recovery, which rebuilds
        # its decisions from the current database state.
        result = apply_recovery(
            preview, RECOVERY_MODE_PARTIAL, identity_fields=IDENTITY_FIELDS
        )
        self.quota_b.refresh_from_db()
        self.assertEqual(self.quota_b.quota, 12)
        self.assertEqual(len(result.skipped_items), 1)

    def test_preview_after_mutation_flags_replaced_group_as_unrecoverable(self):
        self._bad_import()

        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)
        item_a = next(
            item for item in preview.items if item.historical_pk == self.quota_a.pk
        )
        item_b = next(
            item for item in preview.items if item.historical_pk == self.quota_b.pk
        )

        self.assertFalse(item_a.is_recoverable)
        self.assertEqual(item_a.identity_field, "code")
        self.assertEqual(item_a.historical_identity, "GA")
        self.assertEqual(item_a.current_identity, "GA-DIFFERENT")
        self.assertIn("different object", item_a.reason)

        self.assertTrue(item_b.is_recoverable)
        self.assertEqual(
            [(change.field, change.current, change.historical)
             for change in item_b.field_changes],
            [("quota", 20, 12)],
        )
        self.assertEqual(
            {(change.field, change.historical)
             for change in preview.scalar_changes},
            {("name", "cfg"), ("status", "ok")},
        )

    def test_full_recovery_with_stale_preview_writes_nothing(self):
        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)
        self._bad_import()

        config_history_count = self.config.history.count()
        quota_history_count = GroupQuota.history.count()

        with self.assertRaises(StaleRecoveryPreview):
            apply_recovery(preview, RECOVERY_MODE_FULL, identity_fields=IDENTITY_FIELDS)

        self.config.refresh_from_db()
        self.quota_a.refresh_from_db()
        self.quota_b.refresh_from_db()
        self.assertEqual(self.config.status, "bad")
        self.assertEqual(self.quota_b.quota, 20)
        self.assertEqual(self.config.history.count(), config_history_count)
        self.assertEqual(GroupQuota.history.count(), quota_history_count)

    def test_full_recovery_after_mutation_rejects_unrecoverable_relation(self):
        self._bad_import()
        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)

        with self.assertRaises(UnrecoverableRelationsError) as caught:
            apply_recovery(preview, RECOVERY_MODE_FULL, identity_fields=IDENTITY_FIELDS)
        self.assertEqual(
            [item.historical_pk for item in caught.exception.items],
            [self.quota_a.pk],
        )

        self.config.refresh_from_db()
        self.quota_b.refresh_from_db()
        self.assertEqual(self.config.status, "bad")
        self.assertEqual(self.quota_b.quota, 20)

    def test_partial_recovery_restores_only_recognizable_association(self):
        imposter = self._bad_import()
        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)

        result = apply_recovery(
            preview, RECOVERY_MODE_PARTIAL, identity_fields=IDENTITY_FIELDS
        )

        self.config.refresh_from_db()
        self.quota_a.refresh_from_db()
        self.quota_b.refresh_from_db()

        self.assertEqual(self.config.name, "cfg")
        self.assertEqual(self.config.status, "ok")
        self.assertEqual(self.quota_b.quota, 12)

        # The imposter group that reused the old primary key must never receive
        # the historical association or its old quota.
        self.assertEqual(self.quota_a.group_id, imposter.pk)
        self.assertEqual(self.quota_a.quota, 6)
        self.assertEqual(GroupQuota.objects.get(pk=self.quota_a.pk).group.code,
                         "GA-DIFFERENT")
        self.assertFalse(
            GroupQuota.objects.filter(group=imposter, quota=6)
            .exclude(pk=self.quota_a.pk)
            .exists()
        )

        self.assertEqual(len(result.applied_items), 1)
        self.assertEqual(result.applied_items[0].historical_pk, self.quota_b.pk)
        self.assertEqual(len(result.skipped_items), 1)
        skipped = result.skipped_items[0]
        self.assertEqual(skipped.historical_pk, self.quota_a.pk)
        self.assertIn("GA", skipped.reason)

    def test_partial_recovery_when_old_group_was_deleted(self):
        self._bad_import(relink=False)
        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)

        result = apply_recovery(
            preview, RECOVERY_MODE_PARTIAL, identity_fields=IDENTITY_FIELDS
        )

        self.quota_a.refresh_from_db()
        self.quota_b.refresh_from_db()
        self.assertIsNone(self.quota_a.group_id)
        self.assertEqual(self.quota_b.quota, 12)
        self.assertEqual(result.skipped_items[0].historical_pk, self.quota_a.pk)
        self.assertIn("deleted", result.skipped_items[0].reason)

    def test_successful_recovery_can_be_requeried_through_new_versions(self):
        self._bad_import()
        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)

        result = apply_recovery(
            preview, RECOVERY_MODE_PARTIAL, identity_fields=IDENTITY_FIELDS
        )

        config_version = self.config.history.get(pk=result.config_history_id)
        self.assertEqual(config_version.instance.status, "ok")
        self.assertEqual(config_version.instance.name, "cfg")

        quota_version = GroupQuota.history.get(
            pk=result.item_history_ids[self.quota_b.pk]
        )
        self.assertEqual(quota_version.instance.quota, 12)

    def test_failed_write_rolls_back_scalars_and_quotas(self):
        self._bad_import()
        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)

        with patch.object(GroupQuota, "save", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                apply_recovery(
                    preview,
                    RECOVERY_MODE_PARTIAL,
                    identity_fields=IDENTITY_FIELDS,
                )

        self.config.refresh_from_db()
        self.quota_b.refresh_from_db()
        self.assertEqual(self.config.status, "bad")
        self.assertEqual(self.config.name, "cfg-broken")
        self.assertEqual(self.quota_b.quota, 20)

    def test_normal_history_browsing_remains_available(self):
        self._bad_import()

        snapshot = list(
            GroupQuota.history.as_of(T0)
            .order_by("pk")
            .values_list("pk", "quota")
        )
        self.assertEqual(snapshot, [(self.quota_a.pk, 6), (self.quota_b.pk, 12)])

    def test_preview_round_trips_through_the_session_store(self):
        request = type("Request", (), {"session": {}})()
        store = RecoveryPreviewStore(request)
        preview = build_recovery_preview(self._good_version(), IDENTITY_FIELDS)

        token = store.store(preview)
        loaded = store.get(token)

        self.assertEqual(loaded.history_id, preview.history_id)
        self.assertEqual(
            [item.historical_pk for item in loaded.items],
            [item.historical_pk for item in preview.items],
        )
        store.delete(token)
        self.assertIsNone(store.get(token))


class RecoveryAdminTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            "admin", "admin@example.com", "pass"
        )
        self.client.force_login(self.user)

        self.config = ReconciledConfig(name="cfg", status="ok")
        self.config._history_date = T0
        self.config.save()
        self.group_a = BillingGroup(code="GA", name="Group A")
        self.group_a._history_date = T0
        self.group_a.save()
        self.group_b = BillingGroup(code="GB", name="Group B")
        self.group_b._history_date = T0
        self.group_b.save()
        self.quota_a = GroupQuota(config=self.config, group=self.group_a, quota=6)
        self.quota_a._history_date = T0
        self.quota_a.save()
        self.quota_b = GroupQuota(config=self.config, group=self.group_b, quota=12)
        self.quota_b._history_date = T0
        self.quota_b.save()
        self.good_history_id = self.config.history.first().history_id

    def _bad_import(self):
        old_pk = self.group_a.pk
        self.group_a.delete()
        imposter = BillingGroup.objects.create(
            pk=old_pk, code="GA-DIFFERENT", name="Imposter"
        )
        self.quota_a.group = imposter
        self.quota_a._history_date = T1
        self.quota_a.save()
        self.quota_b.quota = 20
        self.quota_b._history_date = T1
        self.quota_b.save()
        self.config.name = "cfg-broken"
        self.config.status = "bad"
        self.config._history_date = T1
        self.config.save()
        return imposter

    def test_preview_page_renders_scalars_associations_and_actions(self):
        self._bad_import()
        response = self.client.get(
            recovery_url(self.config, self.good_history_id)
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "simple_history/recovery_preview.html")
        preview = response.context["preview"]
        self.assertEqual(len(preview.items), 2)
        self.assertContains(response, "Recover everything")
        self.assertContains(response, "Recover recognizable associations only")
        self.assertContains(response, "GA-DIFFERENT")
        self.assertContains(response, "Cannot be recovered")
        # Full recovery must be offered but disabled in the UI.
        self.assertFalse(response.context["full_recovery_available"])

    def test_full_recovery_post_with_stale_preview_is_rejected(self):
        response_get = self.client.get(
            recovery_url(self.config, self.good_history_id)
        )
        token = response_get.context["recovery_token"]
        self._bad_import()

        response = self.client.post(
            recovery_url(self.config, self.good_history_id),
            data={"recovery_token": token, "_recover_full": "1"},
        )

        self.assertEqual(response.status_code, 302)
        self.config.refresh_from_db()
        self.quota_b.refresh_from_db()
        self.assertEqual(self.config.status, "bad")
        self.assertEqual(self.quota_b.quota, 20)

    def test_partial_recovery_post_restores_quota_and_reports_missing_group(self):
        imposter = self._bad_import()
        response_get = self.client.get(
            recovery_url(self.config, self.good_history_id)
        )
        token = response_get.context["recovery_token"]

        response = self.client.post(
            recovery_url(self.config, self.good_history_id),
            data={"recovery_token": token, "_recover_partial": "1"},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.quota_a.refresh_from_db()
        self.quota_b.refresh_from_db()
        self.assertEqual(self.config.status, "ok")
        self.assertEqual(self.quota_b.quota, 12)
        self.assertEqual(self.quota_a.group.code, "GA-DIFFERENT")
        self.assertEqual(self.quota_a.quota, 6)
        self.assertTrue(
            GroupQuota.objects.filter(group=imposter, quota=6)
            .exclude(pk=self.quota_a.pk)
            .count()
            == 0
        )
        messages = list(response.context["messages"])
        self.assertTrue(any("Could not recover" in str(m) for m in messages))
        self.assertTrue(any("12" in str(m) for m in messages))

    def test_post_with_unknown_token_is_rejected(self):
        self._bad_import()
        response = self.client.post(
            recovery_url(self.config, self.good_history_id),
            data={"recovery_token": "not-a-real-token", "_recover_partial": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.quota_b.refresh_from_db()
        self.assertEqual(self.quota_b.quota, 20)

    def test_full_then_partial_then_review_with_one_preview_token(self):
        url = recovery_url(self.config, self.good_history_id)

        # 1. Open the preview while both groups still exist: full recovery is
        #    available because every association is still identifiable.
        response = self.client.get(url)
        token = response.context["recovery_token"]
        self.assertTrue(response.context["full_recovery_available"])
        self.assertEqual(len(response.context["preview"].items), 2)

        # 2. Afterwards one group is deleted and a different group reuses its
        #    primary key; the other association gets a new quota of 20.
        imposter = self._bad_import()

        # 3. Ask for a full recovery using the now-outdated preview: it must be
        #    rejected with no writes at all.
        response = self.client.post(
            url, data={"recovery_token": token, "_recover_full": "1"}
        )
        self.assertEqual(response.status_code, 302)
        self.config.refresh_from_db()
        self.quota_b.refresh_from_db()
        self.assertEqual(self.config.status, "bad")
        self.assertEqual(self.quota_b.quota, 20)

        # 4. Ask for a partial recovery using the same preview token.
        response = self.client.post(
            url,
            data={"recovery_token": token, "_recover_partial": "1"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.quota_a.refresh_from_db()
        self.quota_b.refresh_from_db()
        self.assertEqual(self.config.status, "ok")
        self.assertEqual(self.quota_b.quota, 12)
        self.assertEqual(self.quota_a.group.code, "GA-DIFFERENT")
        self.assertEqual(self.quota_a.quota, 6)
        self.assertEqual(GroupQuota.objects.filter(group=imposter).count(), 1)
        messages = list(response.context["messages"])
        self.assertTrue(any("Could not recover" in str(m) for m in messages))

        # 5. Open the history view and the current change form to review.
        history_url = reverse(
            "admin:tests_reconciledconfig_history",
            args=[quote(self.config.pk)],
        )
        response = self.client.get(history_url)
        self.assertEqual(response.status_code, 200)
        change_url = reverse(
            "admin:tests_reconciledconfig_change", args=[quote(self.config.pk)]
        )
        response = self.client.get(change_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ok")

    def test_preview_requires_view_permission_and_post_requires_change_permission(self):
        viewer = User.objects.create_user("viewer", password="pass")
        viewer.is_staff = True
        viewer.is_superuser = False
        viewer.save()
        viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="tests",
                codename="view_reconciledconfig",
            ),
            Permission.objects.get(
                content_type__app_label="tests",
                codename="view_billinggroup",
            ),
            Permission.objects.get(
                content_type__app_label="tests",
                codename="view_groupquota",
            ),
        )
        self.client.force_login(viewer)

        response = self.client.get(
            recovery_url(self.config, self.good_history_id)
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_change_permission"])

        response = self.client.post(
            recovery_url(self.config, self.good_history_id),
            data={"recovery_token": "whatever", "_recover_partial": "1"},
        )
        self.assertEqual(response.status_code, 403)

    def test_history_list_and_form_still_work(self):
        history_url = reverse(
            "admin:tests_reconciledconfig_history", args=[quote(self.config.pk)]
        )
        response = self.client.get(history_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse(
                "admin:tests_reconciledconfig_simple_history",
                args=[quote(self.config.pk), quote(self.good_history_id)],
            ),
        )

        form_url = reverse(
            "admin:tests_reconciledconfig_simple_history",
            args=[quote(self.config.pk), quote(self.good_history_id)],
        )
        response = self.client.get(form_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Preview recovery")
        self.assertContains(
            response,
            reverse(
                "admin:tests_reconciledconfig_recovery",
                args=[quote(self.config.pk), quote(self.good_history_id)],
            ),
        )
