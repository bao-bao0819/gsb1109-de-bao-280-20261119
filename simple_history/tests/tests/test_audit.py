from datetime import datetime
from decimal import Decimal

from django.test import TestCase

from simple_history.audit import (
    AuditReportReceiver,
    audit_history_rows,
    audit_segments_for_object,
    build_audit_segments,
    build_audit_shards,
    merge_audit_segments,
)
from simple_history.tests.models import AuditProduct

T1 = datetime(2026, 1, 1, 9, 0, 0)
T2 = datetime(2026, 1, 2, 9, 0, 0)
T3 = datetime(2026, 1, 3, 9, 0, 0)
T4 = datetime(2026, 1, 4, 9, 0, 0)
T5 = datetime(2026, 1, 5, 9, 0, 0)
T6 = datetime(2026, 1, 6, 9, 0, 0)
T_LATE = datetime(2026, 1, 2, 12, 0, 0)

ON = "on"
OFF = "off"
HOLD = "hold"
TEN = Decimal("10.00")
TWENTY = Decimal("20.00")


def make_row(product, history_date, price, status, history_type, reason=None):
    return AuditProduct.history.model.objects.create(
        id=product.pk,
        price=price,
        status=status,
        history_date=history_date,
        history_type=history_type,
        history_change_reason=reason,
    )


def base_history(product):
    """
    Six rows, with the r4/r5 seam (same price/status) used as the shard
    boundary and r1-r3 differing only by remark inside one segment.
    """
    return [
        make_row(product, T1, TEN, ON, "+", "create"),
        make_row(product, T2, TEN, ON, "~", "note A"),
        make_row(product, T3, TEN, ON, "~", "note B"),
        make_row(product, T4, TWENTY, OFF, "~", "price change"),
        make_row(product, T5, TWENTY, OFF, "~", "remark only"),
        make_row(product, T6, TEN, ON, "~", "revert"),
    ]


class AuditSegmentationTestCase(TestCase):
    def setUp(self):
        self.product = AuditProduct.objects.create(price=TEN, status=ON)
        AuditProduct.history.all().delete()

    def test_folds_equal_price_status_and_keeps_remark_evidence(self):
        base_history(self.product)
        segments = audit_segments_for_object(AuditProduct, self.product.pk)

        self.assertEqual(len(segments), 3)
        first, middle, last = segments
        self.assertEqual((first.price, first.status), (TEN, ON))
        self.assertEqual(first.history_count, 3)
        self.assertEqual(first.first_change_reason, "create")
        self.assertEqual(first.last_change_reason, "note B")
        self.assertEqual((first.start_date, first.end_date), (T1, T3))
        self.assertEqual((middle.price, middle.status), (TWENTY, OFF))
        self.assertEqual(middle.history_count, 2)
        self.assertEqual((last.price, last.status), (TEN, ON))
        self.assertEqual(last.history_count, 1)

        payload = first.as_dict()
        self.assertEqual(payload["price"], TEN)
        self.assertEqual(payload["status"], ON)
        self.assertEqual(payload["history_count"], 3)
        expected_keys = set(first.endpoint_evidence()) | {
            "price",
            "status",
            "history_type",
            "history_count",
        }
        self.assertEqual(set(payload), expected_keys)

    def test_separated_equal_segments_are_not_merged(self):
        base_history(self.product)
        segments = audit_segments_for_object(AuditProduct, self.product.pk)

        self.assertEqual(segments[0].price, segments[-1].price)
        self.assertEqual(segments[0].status, segments[-1].status)
        self.assertNotEqual(
            segments[0].first_history_id, segments[-1].first_history_id
        )

    def test_delete_and_rebuild_split_even_when_values_identical(self):
        make_row(self.product, T1, TEN, ON, "+", "create")
        make_row(self.product, T2, TEN, ON, "~", "remark only")
        make_row(self.product, T3, TEN, ON, "-", "deleted")
        make_row(self.product, T4, TEN, ON, "+", "rebuilt")
        make_row(self.product, T5, TWENTY, OFF, "~", "changed")

        segments = audit_segments_for_object(AuditProduct, self.product.pk)

        self.assertEqual(len(segments), 4)
        self.assertEqual(
            [(s.price, s.status, s.history_type, s.history_count) for s in segments],
            [
                (TEN, ON, "+", 2),
                (TEN, ON, "-", 1),
                (TEN, ON, "+", 1),
                (TWENTY, OFF, "~", 1),
            ],
        )

    def test_delete_blocks_fold_across_a_shard_seam(self):
        make_row(self.product, T1, TEN, ON, "+", "create")
        make_row(self.product, T2, TEN, ON, "-", "deleted")
        make_row(self.product, T3, TEN, ON, "+", "rebuilt")
        rows = audit_history_rows(AuditProduct, self.product.pk)

        left = build_audit_segments(rows[:2])
        right = build_audit_segments(rows[2:])
        merged = merge_audit_segments(left, right)

        self.assertEqual(len(merged), 3)
        self.assertEqual([s.history_count for s in merged], [1, 1, 1])

    def test_same_timestamp_keeps_version_order(self):
        same_date = datetime(2026, 2, 1, 0, 0, 0)
        first = make_row(self.product, same_date, TEN, ON, "~", "version 1")
        second = make_row(self.product, same_date, TWENTY, OFF, "~", "version 2")
        third = make_row(self.product, same_date, TWENTY, OFF, "~", "version 3")

        rows = audit_history_rows(AuditProduct, self.product.pk)

        self.assertEqual(
            [row.history_id for row in rows],
            [first.history_id, second.history_id, third.history_id],
        )
        segments = build_audit_segments(rows)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].first_history_id, first.history_id)
        self.assertEqual(segments[1].last_history_id, third.history_id)

    def test_backfilled_record_splits_around_same_timestamp_neighbors(self):
        same_date = datetime(2026, 3, 2, 9, 0, 0)
        after_date = datetime(2026, 3, 3, 9, 0, 0)
        make_row(self.product, T1, TEN, ON, "+", "create")
        second = make_row(self.product, same_date, TEN, ON, "~", "v1")
        third = make_row(self.product, same_date, TEN, ON, "~", "v2")
        make_row(self.product, after_date, TWENTY, OFF, "~", "price change")

        # A different-status row back-filled into the middle of the run; the
        # two same-timestamp neighbors keep their id-based version order.
        make_row(
            self.product,
            datetime(2026, 3, 2, 12, 0, 0),
            TEN,
            HOLD,
            "~",
            "late",
        )

        rows = audit_history_rows(AuditProduct, self.product.pk)
        same_time_ids = [
            row.history_id for row in rows if row.history_date == same_date
        ]
        self.assertEqual(same_time_ids, [second.history_id, third.history_id])

        segments = build_audit_segments(rows)
        self.assertEqual(
            [(s.price, s.status, s.history_count) for s in segments],
            [(TEN, ON, 3), (TEN, HOLD, 1), (TWENTY, OFF, 1)],
        )

    def test_raw_history_remains_queryable_in_legacy_order(self):
        base_history(self.product)
        canonical = audit_history_rows(AuditProduct, self.product.pk)
        legacy = list(AuditProduct.history.all())

        self.assertEqual(len(canonical), AuditProduct.history.count())
        self.assertEqual(
            [r.history_id for r in canonical],
            list(reversed([r.history_id for r in legacy])),
        )


class AuditShardExportTestCase(TestCase):
    def setUp(self):
        self.product = AuditProduct.objects.create(price=TEN, status=ON)
        AuditProduct.history.all().delete()
        base_history(self.product)
        self.rows = audit_history_rows(AuditProduct, self.product.pk)
        self.prefix = f"product-{self.product.pk}"

    def test_sharded_export_reconciles_with_one_shot_export(self):
        one_shot = build_audit_segments(self.rows)
        shards = build_audit_shards(
            self.rows,
            shard_sizes=[4, 2],
            identifier_prefix=self.prefix,
        )

        receiver = AuditReportReceiver()
        for shard in shards:
            self.assertTrue(receiver.apply_shard(shard))
        reconciled = receiver.segments()

        self.assertEqual(
            [s.endpoint_evidence() for s in reconciled],
            [s.endpoint_evidence() for s in one_shot],
        )
        self.assertEqual(reconciled[0].start_date, one_shot[0].start_date)
        self.assertEqual(reconciled[-1].end_date, one_shot[-1].end_date)
        self.assertEqual(receiver.history_row_count(), len(self.rows))
        self.assertEqual(
            sum(s.history_count for s in reconciled),
            sum(s.history_count for s in one_shot),
        )

        # The shard 1 tail and shard 2 head share price/status and differ only
        # by remark: they must rejoin into the single middle segment.
        seam_segment = next(
            segment
            for segment in reconciled
            if (segment.price, segment.status) == (TWENTY, OFF)
        )
        self.assertEqual(seam_segment.history_count, 2)
        self.assertEqual(seam_segment.first_change_reason, "price change")
        self.assertEqual(seam_segment.last_change_reason, "remark only")

    def test_shard_sizes_must_cover_every_row(self):
        with self.assertRaises(ValueError):
            build_audit_shards(
                self.rows,
                shard_sizes=[4, 1],
                identifier_prefix=self.prefix,
            )

    def test_correction_replays_only_affected_shard(self):
        receiver = AuditReportReceiver()
        for shard in build_audit_shards(
            self.rows,
            shard_sizes=[4, 2],
            identifier_prefix=self.prefix,
        ):
            receiver.apply_shard(shard)

        make_row(
            self.product,
            T_LATE,
            TEN,
            HOLD,
            "~",
            "late backfilled record",
        )
        corrected_rows = audit_history_rows(AuditProduct, self.product.pk)
        corrected_one_shot = build_audit_segments(corrected_rows)

        # The late row lands inside shard 1 (rows 1-4 originally); only that
        # shard is rebuilt and replayed, at revision 2.
        corrected_shard = build_audit_shards(
            corrected_rows[:5],
            shard_sizes=[5],
            identifier_prefix=self.prefix,
            revision=2,
            start_index=1,
        )[0]
        self.assertEqual(corrected_shard.supersedes, f"{self.prefix}:shard-1:rev-1")
        self.assertTrue(receiver.apply_shard(corrected_shard))

        reconciled = receiver.segments()
        self.assertEqual(
            [s.endpoint_evidence() for s in reconciled],
            [s.endpoint_evidence() for s in corrected_one_shot],
        )
        self.assertEqual(len(reconciled), 5)
        front, different, back = reconciled[0], reconciled[1], reconciled[2]
        self.assertEqual(
            (front.price, front.status, front.history_count), (TEN, ON, 2)
        )
        self.assertEqual((different.price, different.status), (TEN, HOLD))
        self.assertEqual(
            (back.price, back.status, back.history_count), (TEN, ON, 1)
        )
        self.assertNotEqual(front.first_history_id, back.first_history_id)
        self.assertEqual(receiver.history_row_count(), 7)

        # Shard 2 was never re-sent; its segments survive untouched.
        self.assertIn(
            f"{self.prefix}:shard-2:rev-1",
            receiver.active_identifiers(),
        )

    def test_redelivery_of_same_shard_does_not_add_history_rows(self):
        shards = build_audit_shards(
            self.rows,
            shard_sizes=[4, 2],
            identifier_prefix=self.prefix,
        )
        receiver = AuditReportReceiver()
        for shard in shards:
            receiver.apply_shard(shard)

        self.assertFalse(receiver.apply_shard(shards[0]))
        self.assertFalse(receiver.apply_shard(shards[1]))
        self.assertEqual(receiver.history_row_count(), len(self.rows))
        self.assertEqual(len(receiver.segments()), 3)

    def test_corrected_shard_redelivery_is_idempotent(self):
        receiver = AuditReportReceiver()
        for shard in build_audit_shards(
            self.rows,
            shard_sizes=[4, 2],
            identifier_prefix=self.prefix,
        ):
            receiver.apply_shard(shard)

        make_row(self.product, T_LATE, TEN, HOLD, "~", "late")
        corrected_rows = audit_history_rows(AuditProduct, self.product.pk)
        corrected_shard = build_audit_shards(
            corrected_rows[:5],
            shard_sizes=[5],
            identifier_prefix=self.prefix,
            revision=2,
            start_index=1,
        )[0]
        receiver.apply_shard(corrected_shard)

        # Replaying the corrected shard one more time changes nothing.
        self.assertFalse(receiver.apply_shard(corrected_shard))
        self.assertEqual(receiver.history_row_count(), 7)
        self.assertEqual(len(receiver.segments()), 5)

    def test_stale_superseded_shard_cannot_be_revived(self):
        original = build_audit_shards(
            self.rows,
            shard_sizes=[4, 2],
            identifier_prefix=self.prefix,
        )
        receiver = AuditReportReceiver()
        for shard in original:
            receiver.apply_shard(shard)

        make_row(self.product, T_LATE, TEN, HOLD, "~", "late")
        corrected_rows = audit_history_rows(AuditProduct, self.product.pk)
        corrected_shard = build_audit_shards(
            corrected_rows[:5],
            shard_sizes=[5],
            identifier_prefix=self.prefix,
            revision=2,
            start_index=1,
        )[0]
        receiver.apply_shard(corrected_shard)

        # Re-delivering the now-superseded revision 1 must be ignored.
        self.assertFalse(receiver.apply_shard(original[0]))
        self.assertEqual(
            receiver.active_identifiers(),
            [
                f"{self.prefix}:shard-1:rev-2",
                f"{self.prefix}:shard-2:rev-1",
            ],
        )
        self.assertEqual(len(receiver.segments()), 5)
