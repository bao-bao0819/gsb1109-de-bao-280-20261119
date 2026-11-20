from datetime import date, datetime

from django.test import TestCase

from simple_history.export import (
    CustomerStatusExportCursor,
    CustomerStatusExporter,
    month_end,
    month_start,
)
from simple_history.tests.custom_user.models import CustomUser
from simple_history.tests.models import Customer

ENABLED = Customer.Status.ENABLED
DISABLED = Customer.Status.DISABLED


class CustomerStatusExportTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = CustomUser.objects.create_user(username="alice", password="x")
        cls.bob = CustomUser.objects.create_user(username="bob", password="x")
        cls.month = date(2021, 2, 1)
        cls.january = datetime(2021, 1, 31, 23, 0)
        cls.batch_time = datetime(2021, 2, 10, 12, 0)

    def make_customer(self, name, *, status=ENABLED, at=None):
        customer = Customer(name=name, status=status)
        customer._history_date = at or self.january
        customer.save()
        return customer

    def update(
        self,
        customer,
        *,
        at,
        status=None,
        notes=None,
        user=None,
    ):
        if status is not None:
            customer.status = status
        if notes is not None:
            customer.notes = notes
        customer._history_date = at
        if user is not None:
            customer._history_user = user
        else:
            customer.__dict__.pop("_history_user", None)
        customer.save()
        return customer.history.latest("history_id")

    def delete(self, customer, *, at, user=None):
        customer._history_date = at
        if user is not None:
            customer._history_user = user
        else:
            customer.__dict__.pop("_history_user", None)
        customer.delete()

    def exporter(self):
        return CustomerStatusExporter(Customer)

    def event_keys(self, events):
        return [
            (event.object_id, event.old_status, event.new_status)
            for event in events
        ]

    def test_month_bounds_are_natural_month(self):
        self.assertEqual(month_start(self.month), datetime(2021, 2, 1, 0, 0))
        self.assertEqual(month_end(self.month), datetime(2021, 3, 1, 0, 0))
        self.assertEqual(
            month_end(date(2021, 12, 15)), datetime(2022, 1, 1, 0, 0)
        )

    def test_no_change_in_month_returns_nothing(self):
        self.make_customer("steady")
        events, next_cursor = self.exporter().read_batch(self.month)
        self.assertEqual(events, [])
        self.assertIsNone(next_cursor)

    def test_pre_month_history_only_establishes_baseline(self):
        customer = self.make_customer(
            "mover", status=DISABLED, at=datetime(2021, 1, 10)
        )
        self.update(
            customer, at=datetime(2021, 1, 20), status=ENABLED, user=self.alice
        )
        self.update(
            customer, at=self.batch_time, status=DISABLED, user=self.bob
        )

        events = list(self.exporter().stream_status_changes(self.month))

        self.assertEqual(len(events), 1)
        self.assertEqual(
            self.event_keys(events),
            [(customer.pk, ENABLED, DISABLED)],
        )
        self.assertEqual(events[0].history_user, self.bob)
        self.assertEqual(events[0].history_user_id, self.bob.pk)

    def test_same_timestamp_rows_delivered_by_history_id_and_notes_row_skipped(
        self,
    ):
        customer_a = self.make_customer("A")
        customer_b = self.make_customer("B")

        row_disable = self.update(
            customer_a,
            at=self.batch_time,
            status=DISABLED,
            notes="blocked",
            user=self.alice,
        )
        row_notes_only = self.update(
            customer_a, at=self.batch_time, notes="blocked again"
        )
        row_other_customer = self.update(
            customer_b, at=self.batch_time, status=DISABLED, user=self.alice
        )
        row_restore = self.update(
            customer_a, at=self.batch_time, status=ENABLED, user=self.bob
        )

        events = list(self.exporter().stream_status_changes(self.month))

        self.assertEqual(
            self.event_keys(events),
            [
                (customer_a.pk, ENABLED, DISABLED),
                (customer_b.pk, ENABLED, DISABLED),
                (customer_a.pk, DISABLED, ENABLED),
            ],
        )
        self.assertEqual(
            [event.history_id for event in events],
            [
                row_disable.history_id,
                row_other_customer.history_id,
                row_restore.history_id,
            ],
        )
        self.assertNotIn(
            row_notes_only.history_id,
            [event.history_id for event in events],
        )
        self.assertTrue(
            [event.history_id for event in events]
            == sorted(event.history_id for event in events)
        )
        self.assertEqual(events[0].history_date, self.batch_time)
        self.assertEqual(events[0].history_type, "~")
        self.assertEqual(events[0].history_change_reason, None)
        self.assertEqual(events[0].history_user, self.alice)
        self.assertEqual(events[2].history_user, self.bob)

    def test_resume_after_first_disable_then_replay_batch(self):
        customer_a = self.make_customer("A")
        customer_b = self.make_customer("B")
        first_row = self.update(
            customer_a, at=self.batch_time, status=DISABLED, user=self.alice
        )
        self.update(customer_a, at=self.batch_time, notes="note only")
        other_row = self.update(
            customer_b, at=self.batch_time, status=DISABLED, user=self.alice
        )
        restore_row = self.update(
            customer_a, at=self.batch_time, status=ENABLED, user=self.bob
        )

        exporter = self.exporter()
        first_batch, cursor = exporter.read_batch(self.month, limit=1)

        self.assertEqual(len(first_batch), 1)
        self.assertEqual(
            self.event_keys(first_batch),
            [(customer_a.pk, ENABLED, DISABLED)],
        )
        self.assertEqual(cursor.history_date, self.batch_time)
        self.assertEqual(cursor.history_id, first_row.history_id)

        remaining, exhausted = exporter.read_batch(self.month, after=cursor)
        self.assertEqual(
            self.event_keys(remaining),
            [
                (customer_b.pk, ENABLED, DISABLED),
                (customer_a.pk, DISABLED, ENABLED),
            ],
        )
        self.assertEqual(
            [event.history_id for event in remaining],
            [other_row.history_id, restore_row.history_id],
        )
        self.assertIsNone(exhausted)

        replayed, replayed_cursor = exporter.read_batch(
            self.month, after=cursor
        )
        self.assertEqual(
            self.event_keys(replayed), self.event_keys(remaining)
        )
        self.assertEqual(
            [event.history_id for event in replayed],
            [event.history_id for event in remaining],
        )
        self.assertIsNone(replayed_cursor)

        full_run = list(exporter.stream_status_changes(self.month))
        self.assertEqual(
            self.event_keys(first_batch + remaining),
            self.event_keys(full_run),
        )

    def test_cursor_token_roundtrip_and_rejects_garbage(self):
        cursor = CustomerStatusExportCursor(
            history_date=self.batch_time, history_id=42
        )
        token = cursor.encode()
        decoded = CustomerStatusExportCursor.decode(token)
        self.assertEqual(decoded, cursor)
        with self.assertRaises(ValueError):
            CustomerStatusExportCursor.decode("not-a-token")

    def test_invalid_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.exporter().read_batch(self.month, limit=0)

    def test_cursor_from_other_month_is_rejected(self):
        cursor = CustomerStatusExportCursor(
            history_date=datetime(2021, 1, 31, 23, 59), history_id=1
        )
        with self.assertRaises(ValueError):
            self.exporter().read_batch(self.month, after=cursor)
        with self.assertRaises(ValueError):
            list(
                self.exporter().stream_status_changes(self.month, after=cursor)
            )

    def test_resume_within_shared_timestamp_does_not_skip_later_rows(self):
        customer_a = self.make_customer("A")
        customer_b = self.make_customer("B")
        self.update(
            customer_a, at=self.batch_time, status=DISABLED, user=self.alice
        )
        notes_row = self.update(
            customer_a, at=self.batch_time, notes="note only"
        )
        other_row = self.update(
            customer_b, at=self.batch_time, status=DISABLED, user=self.bob
        )
        restore_row = self.update(
            customer_a, at=self.batch_time, status=ENABLED, user=self.bob
        )

        first_batch, cursor = self.exporter().read_batch(self.month, limit=1)
        self.assertEqual(cursor.history_date, self.batch_time)

        resumed, _ = self.exporter().read_batch(self.month, after=cursor)
        resumed_ids = [event.history_id for event in resumed]
        self.assertNotIn(first_batch[0].history_id, resumed_ids)
        self.assertNotIn(notes_row.history_id, resumed_ids)
        self.assertEqual(
            resumed_ids, [other_row.history_id, restore_row.history_id]
        )

    def test_notes_only_change_emits_no_status_event(self):
        customer = self.make_customer("note lover")
        self.update(customer, at=self.batch_time, notes="hello")
        self.update(
            customer,
            at=datetime(2021, 2, 11),
            notes="world",
            user=self.alice,
        )

        events = list(self.exporter().stream_status_changes(self.month))
        self.assertEqual(events, [])

    def test_other_customer_event_is_not_skipped_on_resume(self):
        customer_a = self.make_customer("A")
        customer_b = self.make_customer("B")
        self.update(
            customer_a, at=self.batch_time, status=DISABLED, user=self.alice
        )
        b_row = self.update(
            customer_b, at=self.batch_time, status=DISABLED, user=self.bob
        )

        _, cursor = self.exporter().read_batch(self.month, limit=1)
        remaining, _ = self.exporter().read_batch(self.month, after=cursor)

        self.assertEqual(
            self.event_keys(remaining),
            [(customer_b.pk, ENABLED, DISABLED)],
        )
        self.assertEqual(remaining[0].history_id, b_row.history_id)
        self.assertEqual(remaining[0].history_user, self.bob)

    def test_deleted_and_rebuilt_customer_keeps_periods_separate(self):
        customer = self.make_customer("rebuilt")
        self.delete(customer, at=datetime(2021, 2, 5), user=self.alice)

        rebuilt = Customer(name="rebuilt", status=DISABLED)
        rebuilt._history_date = datetime(2021, 2, 12)
        rebuilt._history_user = self.bob
        rebuilt.save()
        restore_row_history = rebuilt.history.latest("history_id")
        self.update(
            rebuilt,
            at=datetime(2021, 2, 20),
            status=ENABLED,
            user=self.alice,
        )

        events = list(self.exporter().stream_status_changes(self.month))

        self.assertEqual(
            self.event_keys(events),
            [
                (rebuilt.pk, None, DISABLED),
                (rebuilt.pk, DISABLED, ENABLED),
            ],
        )
        self.assertEqual(events[0].history_type, "+")
        self.assertEqual(
            events[0].history_id, restore_row_history.history_id
        )
        self.assertEqual(events[0].history_user, self.bob)
        self.assertTrue(
            all(event.object_id != customer.pk for event in events)
        )

    def test_rebuilt_customer_does_not_inherit_status_after_resume(self):
        customer = self.make_customer("rebuilt")
        self.delete(customer, at=datetime(2021, 2, 5))
        rebuilt = Customer(name="rebuilt", status=DISABLED)
        rebuilt._history_date = datetime(2021, 2, 12)
        rebuilt.save()
        self.update(
            rebuilt, at=datetime(2021, 2, 20), status=ENABLED, user=self.bob
        )

        first_batch, cursor = self.exporter().read_batch(self.month, limit=1)
        self.assertEqual(
            self.event_keys(first_batch),
            [(rebuilt.pk, None, DISABLED)],
        )
        remaining, next_cursor = self.exporter().read_batch(
            self.month, after=cursor
        )
        self.assertEqual(
            self.event_keys(remaining),
            [(rebuilt.pk, DISABLED, ENABLED)],
        )
        self.assertIsNone(next_cursor)

    def test_change_without_user_has_null_user(self):
        customer = self.make_customer("anonymous")
        self.update(customer, at=self.batch_time, status=DISABLED)

        events = list(self.exporter().stream_status_changes(self.month))
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].history_user_id)
        self.assertIsNone(events[0].history_user)

    def test_month_boundaries_are_respected(self):
        customer = self.make_customer("boundary")
        first_moment = datetime(2021, 2, 1, 0, 0)
        next_month = datetime(2021, 3, 1, 0, 0)
        self.update(customer, at=first_moment, status=DISABLED)
        self.update(customer, at=next_month, status=ENABLED)

        events = list(self.exporter().stream_status_changes(self.month))
        self.assertEqual(
            self.event_keys(events),
            [(customer.pk, ENABLED, DISABLED)],
        )

    def test_multiple_batches_consume_everything_in_order(self):
        customer_a = self.make_customer("A")
        customer_b = self.make_customer("B")
        rows = [
            self.update(
                customer_a,
                at=datetime(2021, 2, 2),
                status=DISABLED,
                user=self.alice,
            ),
            self.update(
                customer_b,
                at=datetime(2021, 2, 3),
                status=DISABLED,
                user=self.bob,
            ),
            self.update(
                customer_a,
                at=datetime(2021, 2, 4),
                status=ENABLED,
                user=self.bob,
            ),
        ]

        exporter = self.exporter()
        collected = []
        cursor = None
        for _ in range(10):
            batch, cursor = exporter.read_batch(
                self.month, limit=1, after=cursor
            )
            collected.extend(batch)
            if cursor is None:
                break

        self.assertEqual(
            [event.history_id for event in collected],
            [row.history_id for row in rows],
        )
        self.assertEqual(
            self.event_keys(collected),
            [
                (customer_a.pk, ENABLED, DISABLED),
                (customer_b.pk, ENABLED, DISABLED),
                (customer_a.pk, DISABLED, ENABLED),
            ],
        )
