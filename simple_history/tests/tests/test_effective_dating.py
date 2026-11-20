from datetime import datetime

from django.test import TestCase

from simple_history.exceptions import (
    MissingBatchIdFieldError,
    NotEffectiveDatedModelError,
)
from simple_history.manager import SIMPLE_HISTORY_REVERSE_ATTR_NAME

from ..models import Customer, CustomerStatus, LegacyCustomer, Poll


def dt(day, hour=10):
    return datetime(2026, 6, day, hour, 0, 0)


class BitemporalCustomerStatusTests(TestCase):
    """
    Warehouse scenario:

    - 6/1 entered: active record effective on 6/1
    - 6/8 backfilled: inactive record effective on 6/2 (batch backfill-0608)
    - 6/10 correction: withdraws the 6/8 backfill, also effective on 6/2
    - independent record of another customer, only effective on 6/4

    Business date is fixed at 6/3; exports run at system cutoffs 6/3, 6/9,
    6/11 and must read active, inactive, active in that order.
    """

    def setUp(self):
        self.customer = Customer.objects.create(name="Acme")
        self.other_customer = Customer.objects.create(name="Globex")
        self.history_model = CustomerStatus.history.model

        # 6/1: original active fact, effective the same day
        self.active_row = self._insert(
            history_date=dt(1),
            effective_date=dt(1),
            status=CustomerStatus.ACTIVE,
            history_type="+",
            batch_id="initial",
        )

        # independent fact for another customer, effective only from 6/4
        self.independent_row = self._insert(
            history_date=dt(1),
            effective_date=dt(4),
            status=CustomerStatus.ACTIVE,
            history_type="+",
            batch_id="initial",
            customer=self.other_customer,
            obj_id=2,
        )

        # 6/8: backfilled inactive fact, effective back on 6/2
        self.backfilled_row = self._insert(
            history_date=dt(8),
            effective_date=dt(2),
            status=CustomerStatus.INACTIVE,
            history_type="~",
            batch_id="backfill-0608",
        )

        # 6/10: correction withdrawing the 6/8 backfill, re-asserting active
        self.correction_row = self._insert(
            history_date=dt(10),
            effective_date=dt(2),
            status=CustomerStatus.ACTIVE,
            history_type="~",
            batch_id="correction-0610",
            corrected=self.backfilled_row,
        )

        self.business_date = dt(3)

    def _insert(
        self,
        history_date,
        effective_date,
        status,
        history_type,
        batch_id,
        customer=None,
        corrected=None,
        obj_id=None,
    ):
        row = self.history_model(
            id=obj_id if obj_id is not None else 1,
            history_date=history_date,
            history_type=history_type,
            business_effective_date=effective_date,
            import_batch_id=batch_id,
            customer_id=(customer or self.customer).pk,
            status=status,
            corrected_record_id=corrected.history_id if corrected else None,
        )
        row.save()
        return row

    def _status_at_cutoff(self, cutoff):
        return CustomerStatus.history.effective_as_of(
            self.business_date, system_date=cutoff
        ).get()

    def test_three_cutoffs_give_active_inactive_active(self):
        self.assertEqual(self._status_at_cutoff(dt(3)).status, CustomerStatus.ACTIVE)
        self.assertEqual(self._status_at_cutoff(dt(9)).status, CustomerStatus.INACTIVE)
        self.assertEqual(self._status_at_cutoff(dt(11)).status, CustomerStatus.ACTIVE)

    def test_as_of_keyword_matches_effective_as_of(self):
        instance = CustomerStatus.history.as_of(
            dt(9), business_date=self.business_date
        ).get()
        self.assertEqual(instance.status, CustomerStatus.INACTIVE)

    def test_winning_historical_record_is_identifiable(self):
        # at the 6/9 cutoff the backfilled inactive fact is the adopted record
        inactive = self._status_at_cutoff(dt(9))
        self.assertEqual(
            getattr(inactive, SIMPLE_HISTORY_REVERSE_ATTR_NAME).history_id,
            self.backfilled_row.history_id,
        )

        # at the 6/11 cutoff the correction supersedes it on the business line
        active = self._status_at_cutoff(dt(11))
        adopted = getattr(active, SIMPLE_HISTORY_REVERSE_ATTR_NAME)
        self.assertEqual(adopted.history_id, self.correction_row.history_id)
        # and the correction keeps a pointer to the record it withdrew, while
        # both original rows remain stored
        self.assertEqual(
            adopted.corrected_record_id, self.backfilled_row.history_id
        )
        self.assertEqual(
            CustomerStatus.history.count(), 4
        )  # 3 customer rows + independent row
        self.assertTrue(
            CustomerStatus.history.filter(
                history_id=self.backfilled_row.history_id
            ).exists()
        )

    def test_state_events_follow_business_timeline(self):
        events = {
            event["pk"]: event
            for event in CustomerStatus.history.state_events(
                self.business_date, system_date=dt(11)
            )
        }
        event = events[self.customer.pk]
        self.assertEqual(event["instance"].status, CustomerStatus.ACTIVE)
        self.assertEqual(
            event["record"].history_id, self.correction_row.history_id
        )
        # the independent fact is not effective yet on business day 6/3
        self.assertNotIn(self.other_customer.pk, events)

    def test_independent_record_survives_withdrawal(self):
        # on a business day after 6/4 the independent fact must still be here;
        # withdrawing the 6/2 backfill must not erase an unrelated 6/4 fact
        events = {
            event["pk"]: event
            for event in CustomerStatus.history.state_events(
                dt(5), system_date=dt(11)
            )
        }
        self.assertEqual(
            events[self.other_customer.pk]["record"].history_id,
            self.independent_row.history_id,
        )
        self.assertEqual(
            events[self.customer.pk]["instance"].status, CustomerStatus.ACTIVE
        )

    def test_replaying_backfill_batch_is_idempotent(self):
        def snapshot():
            return {
                cutoff: [
                    item.status
                    for item in CustomerStatus.history.effective_as_of(
                        self.business_date, system_date=cutoff
                    )
                ]
                for cutoff in (dt(3), dt(9), dt(11))
            }

        before = snapshot()
        events_before = CustomerStatus.history.state_events(
            self.business_date, system_date=dt(11)
        )
        rows_before = CustomerStatus.history.count()

        replay_row = self.history_model(
            id=1,
            history_date=dt(8),
            history_type="~",
            business_effective_date=dt(2),
            import_batch_id="backfill-0608",
            customer_id=self.customer.pk,
            status=CustomerStatus.INACTIVE,
        )
        created, skipped = CustomerStatus.history.replay_batch(
            [replay_row], batch_id="backfill-0608"
        )

        self.assertEqual((created, skipped), (0, 1))
        self.assertEqual(CustomerStatus.history.count(), rows_before)
        self.assertEqual(snapshot(), before)
        events_after = CustomerStatus.history.state_events(
            self.business_date, system_date=dt(11)
        )
        self.assertEqual(
            [e["record"].history_id for e in events_after],
            [e["record"].history_id for e in events_before],
        )

    def test_instance_level_effective_as_of(self):
        instance = CustomerStatus(pk=1, customer=self.customer)
        result = instance.history.effective_as_of(
            self.business_date, system_date=dt(11)
        )
        self.assertEqual(result.status, CustomerStatus.ACTIVE)
        self.assertEqual(result.pk, 1)

    def test_save_signal_carries_effective_dating_attrs(self):
        # values can be passed as same-named dynamic attributes...
        backfill = CustomerStatus(
            customer=self.customer, status=CustomerStatus.INACTIVE
        )
        backfill._history_date = dt(8)
        backfill.business_effective_date = dt(2)
        backfill.import_batch_id = "backfill-signal"
        backfill.save()
        row = CustomerStatus.history.filter(import_batch_id="backfill-signal").first()
        self.assertEqual(row.history_date, dt(8))
        self.assertEqual(row.business_effective_date, dt(2))

        # ...or through the underscore-prefixed fallback names, including the
        # pointer to the record being corrected
        correction = CustomerStatus(
            customer=self.customer, status=CustomerStatus.ACTIVE
        )
        correction._history_date = dt(10)
        correction._history_effective_date = dt(2)
        correction._history_batch_id = "correction-signal"
        correction._history_correction = row
        correction.save()
        new_row = CustomerStatus.history.filter(
            import_batch_id="correction-signal"
        ).first()
        self.assertEqual(new_row.business_effective_date, dt(2))
        self.assertEqual(new_row.corrected_record_id, row.history_id)

    def test_effective_dating_requires_field(self):
        with self.assertRaises(NotEffectiveDatedModelError):
            Poll.history.all().effective_dated()
        with self.assertRaises(NotEffectiveDatedModelError):
            Poll.history.effective_as_of(self.business_date)

    def test_bulk_backfill_carries_effective_dating_attrs(self):
        obj = CustomerStatus(pk=1, customer=self.customer)
        CustomerStatus.history.bulk_history_create(
            [obj],
            default_date=dt(8),
            custom_historical_attrs={
                "business_effective_date": dt(2),
                "import_batch_id": "bulk-backfill",
            },
        )
        row = CustomerStatus.history.filter(import_batch_id="bulk-backfill").first()
        self.assertEqual(row.history_date, dt(8))
        self.assertEqual(row.business_effective_date, dt(2))
        instance = CustomerStatus.history.effective_as_of(
            self.business_date, system_date=dt(9)
        ).get()
        self.assertEqual(
            getattr(instance, SIMPLE_HISTORY_REVERSE_ATTR_NAME).history_id,
            row.history_id,
        )

    def test_batch_id_requires_field(self):
        row = Poll.history.model(
            history_date=dt(1), history_type="+", question="q", pub_date=dt(1)
        )
        with self.assertRaises(MissingBatchIdFieldError):
            Poll.history.replay_batch([row], batch_id="x")


class LegacyEffectiveDateTests(TestCase):
    """Rows with no business effective time keep using entry time."""

    def setUp(self):
        self.first = LegacyCustomer.history.model(
            id=1,
            history_date=dt(1),
            history_type="+",
            business_effective_date=None,
            name="old",
            status="active",
        )
        self.first.save()
        self.second = LegacyCustomer.history.model(
            id=1,
            history_date=dt(5),
            history_type="~",
            business_effective_date=None,
            name="old",
            status="inactive",
        )
        self.second.save()

    def test_legacy_as_of_is_unchanged(self):
        instance = LegacyCustomer.history.as_of(dt(3)).get()
        self.assertEqual(instance.status, "active")
        instance = LegacyCustomer.history.as_of(dt(6)).get()
        self.assertEqual(instance.status, "inactive")

    def test_effective_as_of_falls_back_to_entry_date(self):
        instance = LegacyCustomer.history.effective_as_of(
            business_date=dt(3), system_date=dt(6)
        ).get()
        self.assertEqual(instance.status, "active")
        instance = LegacyCustomer.history.effective_as_of(
            business_date=dt(6), system_date=dt(6)
        ).get()
        self.assertEqual(instance.status, "inactive")

    def test_mixed_null_and_dated_rows(self):
        dated = LegacyCustomer.history.model(
            id=1,
            history_date=dt(8),
            history_type="~",
            business_effective_date=dt(2),
            name="old",
            status="active",
        )
        dated.save()
        # business date 6/3: the dated row (eff 6/2) beats the legacy row whose
        # effective date falls back to its entry date 6/1
        instance = LegacyCustomer.history.effective_as_of(
            business_date=dt(3), system_date=dt(9)
        ).get()
        self.assertEqual(instance.status, "active")
        adopted = getattr(instance, SIMPLE_HISTORY_REVERSE_ATTR_NAME)
        self.assertEqual(adopted.history_id, dated.history_id)

        # before the dated row was entered, the legacy view is unaffected
        instance = LegacyCustomer.history.effective_as_of(
            business_date=dt(3), system_date=dt(7)
        ).get()
        adopted = getattr(instance, SIMPLE_HISTORY_REVERSE_ATTR_NAME)
        self.assertEqual(adopted.history_id, self.first.history_id)
