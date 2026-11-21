# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import time
import unittest

from s2sphere import CellId
from s2sphere.keyclean import (
    ALIAS, BatchCleaner, EMPTY, INVALID_FACE, INVALID_LEVEL_BITS,
    LEXICAL, LEGACY, STANDARD, TOO_LONG, inspect_text,
)


S = CellId.from_token('01')          # level 2 cell on face 0
L = CellId(1)                        # level 30 leaf, raw id 1
K = S.child(0)                       # token '004', level 3 child of S

SOURCE_APPENDS = {
    'srcA': [{'append': 'a1', 'version': 'old'},
             {'append': 'a2', 'version': 'new'}],
    'srcB': [{'append': 'b1', 'version': 'old'},
             {'append': 'b2', 'version': None},
             {'append': 'b3', 'version': 'new'}],
    'srcC': [{'append': 'c1', 'version': 'new'}],
}
VERSION_STYLES = {'old': LEGACY, 'new': STANDARD}

ROW_SPECS = [
    ('srcA', 1, '01', 'a1', None, None, None, []),
    ('srcB', 1, '01', 'b1', None, None, None,
     [('same', 'srcA', 1)]),
    ('srcC', 1, '0X01', 'c1', None, None, S.id(), []),
    ('srcC', 2, K.to_token(), 'c1', None, 3, None,
     [('parent', 'srcB', 1)]),
    ('srcA', 2, '07', 'a1', None, None, None, []),
    ('srcB', 2, '07', 'b2', None, None, None,
     [('same', 'srcA', 2)]),
    ('srcA', 3, '1', 'a1', 0, 0, None, []),
    ('srcB', 3, '05', 'b2', None, None, None, []),
    ('srcC', 3, '05', 'c1', None, None, None,
     [('parent', 'srcB', 3)]),
    ('srcA', 4, '01', 'a2', None, None, L.id(), []),
    ('srcB', 4, '01', 'b3', None, None, S.id(),
     [('same', 'srcA', 4)]),
    ('srcA', 8, '00', 'a1', None, None, None, []),
    ('srcA', 9, '', 'a1', None, None, None, []),
    ('srcB', 8, '0a', 'b1', None, None, None, []),
    ('srcB', 9, '0123456789abcdef0', 'b2', None, None, None, []),
    ('srcC', 8, '+1', 'c1', None, None, None, []),
    ('srcC', 9, 'e', 'c1', None, None, None, []),
]


def make_cleaner(source_order=('srcA', 'srcB', 'srcC')):
    """Three-source mixed batch covering every narrative situation."""
    cleaner = BatchCleaner()
    for source in source_order:
        appends = SOURCE_APPENDS[source]
        known = {fact['version']: VERSION_STYLES[fact['version']]
                 for fact in appends if fact['version']}
        cleaner.add_source(source, appends, version_styles=known)
    for (source, seq, text, append, face, level, id_,
            links) in ROW_SPECS:
        cleaner.add_record(
            source, seq, text, append=append, face=face, level=level,
            id_=id_, links=links)
    return cleaner


class TestInspectText(unittest.TestCase):

    def test_short_key_has_two_valid_readings(self):
        inspection = inspect_text('01')
        self.assertIsNone(inspection.error)
        styles = {
            reading.style: reading.cell for reading in inspection.readings}
        self.assertEqual(set(styles), {STANDARD, LEGACY})
        self.assertEqual(styles[STANDARD], S)
        self.assertEqual(styles[LEGACY], L)
        self.assertEqual(styles[STANDARD].face(), styles[LEGACY].face())
        self.assertNotEqual(styles[STANDARD], styles[LEGACY])

    def test_prefix_and_uppercase_are_normalised(self):
        for text in ('0X01', '0x01'):
            inspection = inspect_text(text)
            self.assertEqual(
                {reading.style for reading in inspection.readings},
                {STANDARD, LEGACY})
        upper = inspect_text('ABCD')
        lower = inspect_text('abcd')
        self.assertEqual(
            [reading.cell.id() for reading in upper.readings],
            [reading.cell.id() for reading in lower.readings])

    def test_full_length_key_has_one_reading(self):
        text = format(S.id(), '016x')
        inspection = inspect_text(text)
        self.assertEqual(len(inspection.readings), 1)
        self.assertEqual(inspection.readings[0].cell, S)

    def test_invalid_inputs_get_dedicated_reasons(self):
        cases = {
            '': EMPTY,
            '   ': EMPTY,
            None: EMPTY,
            '0123456789abcdef0': TOO_LONG,
            '+1': LEXICAL,
            '1_0': LEXICAL,
            'e': INVALID_FACE,
            '00': INVALID_LEVEL_BITS,
            '0a': INVALID_LEVEL_BITS,
        }
        for text, reason in cases.items():
            inspection = inspect_text(text)
            self.assertIsNotNone(inspection.error, text)
            self.assertEqual(inspection.error.reason, reason, text)


class TestNarrativeBatch(unittest.TestCase):

    def setUp(self):
        self.report = make_cleaner().clean()

    def _by_key(self, records):
        return {(record.source, record.seq): record for record in records}

    def test_group_one_resolves_through_chain_and_full_reference(self):
        resolved = self._by_key(self.report.resolved)
        a1 = resolved[('srcA', 1)]
        b1 = resolved[('srcB', 1)]
        c1 = resolved[('srcC', 1)]
        c2 = resolved[('srcC', 2)]

        # the B same-link carries the A row; the C full reference pins
        # what the standard writing of '01' is, while the trusted level-3
        # parent row can only contain the legacy leaf reading.
        self.assertEqual(a1.cell, L)
        self.assertEqual(a1.style, LEGACY)
        self.assertEqual(b1.cell, L)
        self.assertEqual(b1.style, LEGACY)
        self.assertEqual(c1.cell, S)
        self.assertEqual(c1.style, STANDARD)
        self.assertEqual(c2.cell, K)
        self.assertEqual(c2.style, STANDARD)
        self.assertEqual(c2.face, K.face())
        self.assertEqual(c2.level, 3)
        self.assertTrue(K.contains(L))
        self.assertTrue(any('ref id=' in item for item in c1.evidence))
        self.assertTrue(any('same-link' in item for item in b1.evidence))
        self.assertTrue(any('parent-link' in item for item in c2.evidence))

    def test_under_determined_group_stays_ambiguous_with_both_readings(self):
        ambiguous = self._by_key(self.report.ambiguous)
        for key in (('srcA', 2), ('srcB', 2)):
            record = ambiguous[key]
            self.assertEqual(record.text, '07')
            self.assertEqual(record.source, key[0])
            self.assertEqual(
                {style for style, _, _ in record.readings},
                {STANDARD, LEGACY})
            self.assertEqual(
                len({cell.id() for _, cell, _ in record.readings}), 2)
            self.assertIn('manual review', record.reason)

    def test_independent_conclusion_does_not_resolve_other_group(self):
        resolved = self._by_key(self.report.resolved)
        self.assertEqual(resolved[('srcA', 3)].token, '1')
        ambiguous_keys = {
            (record.source, record.seq) for record in self.report.ambiguous}
        self.assertIn(('srcA', 2), ambiguous_keys)
        self.assertIn(('srcB', 2), ambiguous_keys)

    def test_hierarchy_conflict_names_both_inputs(self):
        rows = {frozenset(conflict.rows): conflict
                for conflict in self.report.conflicts}
        conflict = rows[frozenset((('srcB', 3), ('srcC', 3)))]
        self.assertIn('hierarchy', conflict.reason)
        self.assertTrue(
            any('parent-link' in item for item in conflict.evidence))

    def test_same_cell_conflict_between_pinned_rows(self):
        rows = {frozenset(conflict.rows): conflict
                for conflict in self.report.conflicts}
        conflict = rows[frozenset((('srcA', 4), ('srcB', 4)))]
        self.assertIn('no reading in common', conflict.reason)
        self.assertIn(('srcA', 4), conflict.rows)
        self.assertIn(('srcB', 4), conflict.rows)

    def test_invalid_rows_are_separate_and_do_not_block_valid_files(self):
        invalid = self._by_key(self.report.invalid)
        self.assertEqual(invalid[('srcA', 9)].reason, EMPTY)
        self.assertEqual(invalid[('srcB', 9)].reason, TOO_LONG)
        self.assertEqual(invalid[('srcC', 8)].reason, LEXICAL)
        self.assertEqual(invalid[('srcC', 9)].reason, INVALID_FACE)
        self.assertEqual(invalid[('srcA', 8)].reason, INVALID_LEVEL_BITS)
        self.assertEqual(invalid[('srcB', 8)].reason, INVALID_LEVEL_BITS)
        resolved = self._by_key(self.report.resolved)
        self.assertIn(('srcA', 1), resolved)

    def test_arrival_order_reversed_but_append_seq_kept(self):
        blocks = []
        for outcome in self.report.ordered:
            if not blocks or blocks[-1] != outcome.source:
                blocks.append(outcome.source)
        self.assertEqual(blocks, ['srcC', 'srcB', 'srcA'])
        for source in ('srcA', 'srcB', 'srcC'):
            seqs = [outcome.seq for outcome in self.report.ordered
                    if outcome.source == source]
            self.assertEqual(seqs, sorted(seqs))
            self.assertTrue(all(outcome.append is not None
                                for outcome in self.report.ordered
                                if outcome.source == source))

    def test_standard_tokens_roundtrip_and_old_key_mapping_is_kept(self):
        for record in self.report.resolved:
            self.assertEqual(
                CellId.from_token(record.token).id(), record.cell.id())
        old = self.report.old_key_map
        self.assertEqual(old[('srcA', 1)].token, L.to_token())
        self.assertEqual(old[('srcA', 1)].text, '01')
        self.assertEqual(old[('srcC', 1)].token, S.to_token())
        self.assertEqual(old[('srcC', 1)].text, '0X01')

    def test_conflict_rows_appear_as_conflict_outcomes(self):
        conflicted = {
            key for conflict in self.report.conflicts
            for key in conflict.rows}
        ordered_keys = {
            (outcome.source, outcome.seq) for outcome in self.report.ordered}
        self.assertTrue(conflicted.issubset(ordered_keys))
        conflict_outcomes = [
            outcome for outcome in self.report.ordered
            if type(outcome).__name__ == 'ConflictOutcome']
        self.assertEqual(
            {(o.source, o.seq) for o in conflict_outcomes}, conflicted)

    def test_trusted_ref_matching_neither_reading_is_one_conflict(self):
        cleaner = BatchCleaner()
        cleaner.add_source(
            'srcX', [{'append': 'x1', 'version': 'new'}],
            version_styles={'new': STANDARD})
        cleaner.add_record('srcX', 1, '1', append='x1', level=5)
        report = cleaner.clean()
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0].rows, (('srcX', 1),))
        self.assertIn('trusted evidence', report.conflicts[0].reason)
        self.assertEqual(report.resolved, ())
        self.assertEqual(report.ambiguous, ())


class TestAliasIsNotSameCell(unittest.TestCase):

    def test_alias_keeps_distinct_cells(self):
        cleaner = BatchCleaner()
        cleaner.add_source(
            'srcA', [{'append': 'a1', 'version': 'new'}],
            version_styles={'new': STANDARD})
        cleaner.add_source(
            'srcB', [{'append': 'b1', 'version': 'old'}],
            version_styles={'old': LEGACY})
        cleaner.add_record('srcA', 1, '01', append='a1',
                           face=S.face(), level=S.level())
        cleaner.add_record('srcB', 1, '01', append='b1', id_=L.id(),
                           links=[(ALIAS, 'srcA', 1)])
        report = cleaner.clean()
        resolved = {(r.source, r.seq): r for r in report.resolved}
        self.assertEqual(resolved[('srcA', 1)].cell, S)
        self.assertEqual(resolved[('srcB', 1)].cell, L)
        self.assertNotEqual(
            resolved[('srcA', 1)].cell, resolved[('srcB', 1)].cell)
        self.assertEqual(report.alias_pairs,
                         ((('srcB', 1), ('srcA', 1)),))
        self.assertEqual(report.conflicts, ())


class TestEvidenceWithdrawal(unittest.TestCase):

    def _cleaner(self):
        cleaner = BatchCleaner()
        cleaner.add_source(
            'srcC', [{'append': 'c1', 'version': 'new'}],
            version_styles={'new': STANDARD})
        cleaner.add_source(
            'srcB', [{'append': 'b1', 'version': 'old'}],
            version_styles={'old': LEGACY})
        cleaner.add_source(
            'srcA', [{'append': 'a1', 'version': 'old'}],
            version_styles={'old': LEGACY})
        cleaner.add_record('srcC', 1, '0X01', append='c1', id_=S.id())
        cleaner.add_record('srcB', 1, '01', append='b1',
                           links=[('same', 'srcC', 1)])
        cleaner.add_record('srcA', 1, '1', append='a1', face=0, level=0)
        return cleaner

    def test_ref_added_and_removed_flips_ambiguity(self):
        cleaner = self._cleaner()
        report = cleaner.clean()
        resolved = {(r.source, r.seq): r for r in report.resolved}
        self.assertEqual(resolved[('srcB', 1)].cell, S)

        cleaner.drop_ref('srcC', 1, 'id_')
        report = cleaner.clean()
        ambiguous = {(a.source, a.seq) for a in report.ambiguous}
        self.assertIn(('srcB', 1), ambiguous)
        self.assertIn(('srcC', 1), ambiguous)
        resolved = {(r.source, r.seq): r for r in report.resolved}
        self.assertEqual(resolved[('srcA', 1)].cell,
                         CellId.from_token('1'))

        cleaner.set_ref('srcC', 1, 'id_', S.id())
        report = cleaner.clean()
        resolved = {(r.source, r.seq): r for r in report.resolved}
        self.assertEqual(resolved[('srcB', 1)].cell, S)

    def test_link_added_and_removed_flips_ambiguity(self):
        cleaner = BatchCleaner()
        cleaner.add_source(
            'srcP', [{'append': 'p1', 'version': 'new'}],
            version_styles={'new': STANDARD})
        cleaner.add_record('srcP', 1, '01', append='p1', id_=S.id())
        cleaner.add_record('srcP', 2, '01', append='p1')
        self.assertEqual(
            {(a.source, a.seq) for a in cleaner.clean().ambiguous},
            {('srcP', 2)})

        cleaner.add_link('srcP', 2, 'same', 'srcP', 1)
        resolved = {(r.source, r.seq): r for r in cleaner.clean().resolved}
        self.assertEqual(resolved[('srcP', 2)].cell, S)

        cleaner.drop_link('srcP', 2, 'same', 'srcP', 1)
        self.assertEqual(
            {(a.source, a.seq) for a in cleaner.clean().ambiguous},
            {('srcP', 2)})
        resolved = {(r.source, r.seq): r for r in cleaner.clean().resolved}
        self.assertEqual(resolved[('srcP', 1)].cell, S)


class TestSwitchAnalysis(unittest.TestCase):

    def _multi_append_cleaner(self):
        cleaner = BatchCleaner()
        cleaner.add_source(
            'srcB',
            [{'append': 'b1', 'version': 'old'},
             {'append': 'b2', 'version': None},
             {'append': 'b3', 'version': 'new'}],
            version_styles={'old': LEGACY, 'new': STANDARD})
        cleaner.add_record('srcB', 1, '01', append='b1', level=30)
        cleaner.add_record('srcB', 2, '07', append='b2')
        cleaner.add_record('srcB', 3, '01', append='b3', level=2)
        return cleaner

    def test_feasible_range_and_per_cut_ids(self):
        report = self._multi_append_cleaner().clean()
        switch = {report_source.source: report_source
                  for report_source in report.switch_reports}['srcB']
        self.assertEqual(switch.feasible_cuts, (1, 2))
        self.assertEqual(
            switch.cut_location[1], 'boundary b1 | b2')
        self.assertEqual(
            switch.cut_location[2], 'boundary b2 | b3')
        self.assertFalse(switch.stable)
        self.assertTrue(
            any("('srcB', 2)" in note for note in switch.notes))
        ids_by_cut = switch.per_cut_ids
        self.assertEqual(ids_by_cut[1][('srcB', 1)], L.id())
        self.assertEqual(ids_by_cut[2][('srcB', 1)], L.id())
        self.assertNotEqual(
            ids_by_cut[1][('srcB', 2)], ids_by_cut[2][('srcB', 2)])
        self.assertTrue(switch.row_id_stable[('srcB', 1)])
        self.assertTrue(switch.row_id_stable[('srcB', 3)])
        self.assertFalse(switch.row_id_stable[('srcB', 2)])

    def test_same_key_in_two_appends_pins_unique_switch(self):
        cleaner = BatchCleaner()
        cleaner.add_source(
            'srcA',
            [{'append': 'a1', 'version': 'old'},
             {'append': 'a2', 'version': 'new'}],
            version_styles={'old': LEGACY, 'new': STANDARD})
        cleaner.add_record('srcA', 1, '01', append='a1', level=30)
        cleaner.add_record('srcA', 2, '01', append='a2', level=2)
        cleaner.add_source(
            'srcC', [{'append': 'c1', 'version': 'new'}],
            version_styles={'new': STANDARD})
        cleaner.add_record('srcC', 1, '01', append='c1', id_=S.id(),
                           links=[('same', 'srcA', 2)])
        cleaner.add_source(
            'srcB', [{'append': 'b1', 'version': 'old'}],
            version_styles={'old': LEGACY})
        cleaner.add_record('srcB', 1, '01', append='b1', level=30,
                           links=[('same', 'srcA', 1)])
        report = cleaner.clean()
        switch_a = next(sr for sr in report.switch_reports
                        if sr.source == 'srcA')
        self.assertEqual(switch_a.feasible_cuts, (1,))
        self.assertEqual(
            switch_a.per_cut_ids[1][('srcA', 1)], L.id())
        self.assertEqual(
            switch_a.per_cut_ids[1][('srcA', 2)], S.id())
        resolved = {(r.source, r.seq): r for r in report.resolved}
        self.assertEqual(resolved[('srcB', 1)].cell, L)
        self.assertEqual(resolved[('srcC', 1)].cell, S)
        switch_b = next(sr for sr in report.switch_reports
                        if sr.source == 'srcB')
        self.assertEqual(switch_b.feasible_cuts, (1,))

    def test_pinned_flip_flop_has_no_feasible_switch(self):
        cleaner = BatchCleaner()
        cleaner.add_source(
            'srcA',
            [{'append': 'a1', 'version': 'old'},
             {'append': 'a2', 'version': 'new'}])
        cleaner.add_record('srcA', 1, '01', append='a1', level=2)
        cleaner.add_record('srcA', 2, '01', append='a2', level=30)
        report = cleaner.clean()
        switch = report.switch_reports[0]
        self.assertEqual(switch.feasible_cuts, ())
        self.assertTrue(switch.notes)
        resolved = {(r.source, r.seq): r for r in report.resolved}
        self.assertEqual(resolved[('srcA', 1)].cell, S)
        self.assertEqual(resolved[('srcA', 2)].cell, L)

    def test_version_facts_can_make_every_switch_infeasible(self):
        cleaner = BatchCleaner()
        cleaner.add_source(
            'srcX', [{'append': 'x1', 'version': 'old'}],
            version_styles={'old': LEGACY})
        cleaner.add_record('srcX', 1, '01', append='x1', level=2)
        report = cleaner.clean()
        switch = report.switch_reports[0]
        self.assertEqual(switch.feasible_cuts, ())
        self.assertEqual(switch.cut_range, (None, None))
        self.assertTrue(switch.notes)

    def test_long_list_reports_local_conflict_without_combinatorics(self):
        cleaner = BatchCleaner()
        appends = [{'append': 'q{0}'.format(i), 'version': None}
                   for i in range(40)]
        cleaner.add_source('srcQ', appends)
        for i in range(40):
            cleaner.add_record('srcQ', i, '1', append='q{0}'.format(i),
                               face=0, level=0)
        cleaner.add_record('srcQ', 40, '05', append='q19')
        cleaner.add_record('srcQ', 41, '05', append='q20',
                           links=[('parent', 'srcQ', 40)])
        start = time.time()
        report = cleaner.clean()
        self.assertLess(time.time() - start, 5.0)
        self.assertEqual(len(report.conflicts), 1)
        switch = report.switch_reports[0]
        self.assertLessEqual(len(switch.feasible_cuts), 41)
        self.assertEqual(len(report.switch_reports), 1)


class TestOrderInvariance(unittest.TestCase):

    def _interpretation_map(self, report):
        result = {}
        for record in report.resolved:
            result[(record.source, record.seq)] = (
                'resolved', record.cell.id())
        for record in report.ambiguous:
            result[(record.source, record.seq)] = (
                'ambiguous',
                tuple(sorted(cell.id() for _, cell, _ in record.readings)))
        for conflict in report.conflicts:
            for key in conflict.rows:
                result[key] = ('conflict', conflict.reason)
        for record in report.invalid:
            result[(record.source, record.seq)] = (
                'invalid', record.reason)
        return result

    def test_reversed_arrival_keeps_final_interpretation(self):
        first = make_cleaner(('srcA', 'srcB', 'srcC')).clean()
        second = make_cleaner(('srcC', 'srcB', 'srcA')).clean()
        self.assertEqual(
            self._interpretation_map(first),
            self._interpretation_map(second))

        def source_blocks(report):
            blocks = []
            for outcome in report.ordered:
                if not blocks or blocks[-1] != outcome.source:
                    blocks.append(outcome.source)
            return blocks

        self.assertEqual(source_blocks(first), ['srcC', 'srcB', 'srcA'])
        self.assertEqual(source_blocks(second), ['srcA', 'srcB', 'srcC'])

        # each source still keeps its own append ordering intact.
        for report in (first, second):
            for source in ('srcA', 'srcB', 'srcC'):
                seqs = [outcome.seq for outcome in report.ordered
                        if outcome.source == source]
                self.assertEqual(seqs, sorted(seqs))


class TestExistingCellIdBehaviour(unittest.TestCase):

    def test_direct_construction_and_token_apis_unchanged(self):
        self.assertEqual(CellId(1).to_token(), '0000000000000001')
        self.assertEqual(CellId.from_token('01'), S)
        self.assertEqual(CellId.from_token('01').level(), 2)
        self.assertEqual(CellId(int('1', 16)), L)
        self.assertTrue(L.is_valid())
        self.assertTrue(S.is_valid())


if __name__ == '__main__':
    unittest.main()
