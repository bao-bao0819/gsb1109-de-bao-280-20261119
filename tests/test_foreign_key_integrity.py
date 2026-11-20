"""
Regression tests for referential integrity checked against data read from actual files.

Earlier tests only fed in-memory tuples, so failures could not be attributed to a specific
cause. The scenarios below use minimal CSV files on disk plus CSVW metadata and keep three
kinds of failure apart:

1. actual data not matching any row in the referenced table,
2. incompatible column references / datatype definitions at the relation level,
3. values that are null under the null policy in effect.

The parent table has a composite key made of a region string and an integer store number.
A parent row uses the literal region ``NA`` and the lexical store number ``001``. Child
tables arrange the same values in different physical column orders; a second set of
columns references the two parent columns in reverse order.
"""
import json
import logging

import pytest

import csvw

PARENT_URL = 'parent.csv'
CHILD_URL = 'child.csv'


class _LogRecorder(logging.Handler):
    """Collects log messages emitted while running checks in inspection mode."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def messages(self):
        return [r.getMessage() for r in self.records]


def _write_tablegroup(
        tmp_path,
        parent_csv,
        child_csv,
        parent_columns,
        child_columns,
        foreign_keys,
        parent_primary_key=None,
        common_props=None,
        parent_url=PARENT_URL,
        child_url=CHILD_URL):
    """Write minimal CSV files plus metadata and load the resulting TableGroup."""
    metadata = {
        '@context': 'http://www.w3.org/ns/csvw',
        'tables': [
            {
                'url': parent_url,
                'tableSchema': {
                    'columns': parent_columns,
                    'primaryKey': parent_primary_key or ['region', 'store'],
                },
            },
            {
                'url': child_url,
                'tableSchema': {
                    'columns': child_columns,
                    'foreignKeys': foreign_keys,
                },
            },
        ],
    }
    if common_props:
        metadata.update(common_props)
    (tmp_path / parent_url).write_text(parent_csv, encoding='utf-8')
    (tmp_path / child_url).write_text(child_csv, encoding='utf-8')
    md_path = tmp_path / 'metadata.json'
    md_path.write_text(json.dumps(metadata), encoding='utf-8')
    return csvw.TableGroup.from_file(str(md_path))


def _integer_parent_columns():
    return [
        {'name': 'region', 'datatype': 'string'},
        {'name': 'store', 'datatype': 'integer'},
    ]


def _string_parent_columns():
    return [
        {'name': 'region', 'datatype': 'string'},
        {'name': 'store', 'datatype': 'string'},
    ]


def _fk(column_reference, reference_columns):
    return {
        'columnReference': column_reference,
        'reference': {
            'resource': PARENT_URL,
            'columnReference': reference_columns,
        },
    }


@pytest.mark.parametrize(
    'parent_csv,child_csv,parent_columns,child_columns',
    [
        # Parent key columns and child foreign-key columns in different physical orders.
        (
            'region,store\nNA,001\nEU,002\n',
            'store_ref,region_ref,note\n1,NA,r1\n2,EU,r2\n',
            _integer_parent_columns(),
            [
                {'name': 'store_ref', 'datatype': 'integer'},
                {'name': 'region_ref', 'datatype': 'string'},
                {'name': 'note', 'datatype': 'string'},
            ],
        ),
        # Same relation, but the parent file leads with the store column and the child
        # file places the note column up front.
        (
            'store,region\n001,NA\n002,EU\n',
            'note,region_ref,store_ref\nr1,NA,1\nr2,EU,2\n',
            _integer_parent_columns(),
            [
                {'name': 'note', 'datatype': 'string'},
                {'name': 'region_ref', 'datatype': 'string'},
                {'name': 'store_ref', 'datatype': 'integer'},
            ],
        ),
    ],
)
def test_composite_key_matches_across_physical_layouts(
        tmp_path, parent_csv, child_csv, parent_columns, child_columns):
    tg = _write_tablegroup(
        tmp_path,
        parent_csv,
        child_csv,
        parent_columns,
        child_columns,
        [_fk(['region_ref', 'store_ref'], ['region', 'store'])])

    parent_rows = list(tg.tabledict[PARENT_URL].iterdicts())
    child_rows = list(tg.tabledict[CHILD_URL].iterdicts())
    # Lexical "001"/"1" are parsed as integers by column declaration, not by position.
    assert parent_rows[0]['store'] == 1
    assert child_rows[0]['store_ref'] == 1
    assert isinstance(parent_rows[0]['store'], int)
    assert isinstance(child_rows[0]['store_ref'], int)
    # The literal region NA is an actual value here (no null declaration), so the row
    # really participates in the lookup rather than being skipped.
    assert child_rows[0]['region_ref'] == 'NA'
    assert tg.check_referential_integrity()
    assert tg.check_referential_integrity(strict=True)


@pytest.mark.parametrize(
    'parent_store,child_store',
    [
        ('001', '1'),
        ('1', '001'),
    ],
)
def test_integer_lexical_001_and_1_match(tmp_path, parent_store, child_store):
    tg = _write_tablegroup(
        tmp_path,
        f'region,store\nNA,{parent_store}\n',
        f'store_ref,region_ref\n{child_store},NA\n',
        _integer_parent_columns(),
        [
            {'name': 'store_ref', 'datatype': 'integer'},
            {'name': 'region_ref', 'datatype': 'string'},
        ],
        [_fk(['region_ref', 'store_ref'], ['region', 'store'])])
    assert tg.check_referential_integrity(strict=True)


def test_string_columns_only_match_identical_lexical_values(tmp_path):
    columns = _string_parent_columns()
    child_columns = [
        {'name': 'region_ref', 'datatype': 'string'},
        {'name': 'store_ref', 'datatype': 'string'},
    ]
    fks = [_fk(['region_ref', 'store_ref'], ['region', 'store'])]

    matching = _write_tablegroup(
        tmp_path,
        'region,store\nNA,001\n',
        'region_ref,store_ref\nNA,001\n',
        columns, child_columns, fks)
    assert matching.check_referential_integrity(strict=True)

    broken = _write_tablegroup(
        tmp_path,
        'region,store\nNA,001\n',
        'region_ref,store_ref\nNA,1\n',
        columns, child_columns, fks)
    # The text "1" is not the text "001": this is a data mismatch, reported with the
    # source location of the offending child row.
    with pytest.raises(ValueError, match='not found in table') as exc_info:
        broken.check_referential_integrity()
    assert f'{CHILD_URL}:2' in str(exc_info.value)

    recorder = _LogRecorder()
    logger = logging.getLogger('csvw.test.string-mismatch')
    logger.addHandler(recorder)
    assert not broken.check_referential_integrity(log=logger)
    assert any(f'{CHILD_URL}:2' in msg and 'not found in table' in msg
               for msg in recorder.messages)


def test_datatype_conflict_is_reported_at_relation_level(tmp_path):
    child_columns = [
        {'name': 'region_ref', 'datatype': 'string'},
        # Same lexical text "1" as in the parent, but an incompatible datatype definition.
        {'name': 'store_ref', 'datatype': 'string'},
    ]
    tg = _write_tablegroup(
        tmp_path,
        'region,store\nNA,1\n',
        'region_ref,store_ref\nNA,1\n',
        _integer_parent_columns(),
        child_columns,
        [_fk(['region_ref', 'store_ref'], ['region', 'store'])])

    # The relation definition itself is invalid; no data lookup may paper over it.
    with pytest.raises(ValueError, match='datatype') as exc_info:
        tg.validate_schema()
    assert 'not found in table' not in str(exc_info.value)
    with pytest.raises(ValueError, match='datatype'):
        tg.check_referential_integrity()

    recorder = _LogRecorder()
    logger = logging.getLogger('csvw.test.datatype-conflict')
    logger.addHandler(recorder)
    assert not tg.check_referential_integrity(log=logger)
    conflict = [r for r in recorder.records if 'datatype' in r.getMessage()]
    assert len(conflict) == 1
    assert conflict[0].levelno == logging.ERROR


def _two_group_columns(null_region):
    region_ref_spec = {'name': 'region_ref', 'datatype': 'string'}
    if null_region:
        region_ref_spec['null'] = ['NA']
    return [
        {'name': 'note', 'datatype': 'string'},
        region_ref_spec,
        {'name': 'store_ref', 'datatype': 'integer'},
        # Second set of fields referencing the same parent columns in reverse order.
        {'name': 'store_ref2', 'datatype': 'integer'},
        {'name': 'region_ref2', 'datatype': 'string'},
    ]


def _two_group_foreign_keys():
    return [
        _fk(['region_ref', 'store_ref'], ['region', 'store']),
        _fk(['store_ref2', 'region_ref2'], ['store', 'region']),
    ]


def test_two_foreign_key_groups_keep_lookups_independent(tmp_path):
    child_columns = _two_group_columns(null_region=True)
    # Row 1: group 1 carries a declared-null region (NA), group 2 references the literal
    # NA region. Row 2: group 1 is legal while group 2 names the wrong region.
    tg = _write_tablegroup(
        tmp_path,
        'region,store\nNA,001\nEU,002\n',
        'note,region_ref,store_ref,store_ref2,region_ref2\n'
        'r1,NA,1,001,NA\n'
        'r2,EU,002,2,ASIA\n',
        _integer_parent_columns(),
        child_columns,
        _two_group_foreign_keys())

    # Lenient mode keeps the existing nullable foreign-key policy: only the genuinely
    # mismatching group-2 row fails, and the error carries the child's source location.
    with pytest.raises(ValueError) as exc_info:
        tg.check_referential_integrity()
    message = str(exc_info.value)
    assert f'{CHILD_URL}:3' in message
    assert "(2, 'ASIA')" in message
    # Group 1's legal (EU, 2) lookup is never reported as an error.
    assert 'EU' not in message

    recorder = _LogRecorder()
    logger = logging.getLogger('csvw.test.two-groups')
    logger.addHandler(recorder)
    assert not tg.check_referential_integrity(log=logger)
    assert len(recorder.messages) == 1
    assert f'{CHILD_URL}:3' in recorder.messages[0]

    # Strict mode additionally rejects the declared-null foreign key of row 1.
    with pytest.raises(ValueError, match='Foreign key column is null'):
        tg.check_referential_integrity(strict=True)


def test_two_foreign_key_groups_both_legal(tmp_path):
    tg = _write_tablegroup(
        tmp_path,
        'region,store\nNA,001\nEU,002\n',
        'note,region_ref,store_ref,store_ref2,region_ref2\n'
        'r1,NA,1,001,NA\n'
        'r2,EU,002,2,EU\n',
        _integer_parent_columns(),
        _two_group_columns(null_region=False),
        _two_group_foreign_keys())
    # NA is a literal region in both referencing columns here, so strict mode passes.
    assert tg.check_referential_integrity(strict=True)


def test_declared_null_vs_literal_na_region(tmp_path):
    fks = [_fk(['region_ref', 'store_ref'], ['region', 'store'])]

    # NA declared as null: the cell parses to None and is treated under the null policy.
    nullable = _write_tablegroup(
        tmp_path,
        'region,store\nNA,001\nEU,002\n',
        'store_ref,region_ref\n1,NA\n2,EU\n',
        _integer_parent_columns(),
        [
            {'name': 'store_ref', 'datatype': 'integer', 'null': ['NA']},
            {'name': 'region_ref', 'datatype': 'string', 'null': ['NA']},
        ],
        fks)
    assert next(nullable.tabledict[CHILD_URL].iterdicts())['region_ref'] is None
    # Lenient mode preserves the nullable foreign-key strategy.
    assert nullable.check_referential_integrity()
    recorder = _LogRecorder()
    logger = logging.getLogger('csvw.test.null-policy')
    logger.addHandler(recorder)
    assert nullable.check_referential_integrity(log=logger)
    assert recorder.messages == []
    # Strict mode rejects the null explicitly instead of silently skipping the row.
    with pytest.raises(ValueError, match='Foreign key column is null'):
        nullable.check_referential_integrity(strict=True)

    # Same NA cell without a null declaration: it is the actual region of the parent row.
    literal = _write_tablegroup(
        tmp_path,
        'region,store\nNA,001\nEU,002\n',
        'store_ref,region_ref\n1,NA\n2,EU\n',
        _integer_parent_columns(),
        [
            {'name': 'store_ref', 'datatype': 'integer'},
            {'name': 'region_ref', 'datatype': 'string'},
        ],
        fks)
    assert next(literal.tabledict[CHILD_URL].iterdicts())['region_ref'] == 'NA'
    assert literal.check_referential_integrity(strict=True)


def test_verdict_unchanged_by_irrelevant_metadata_edits(tmp_path):
    child_columns = [
        {'name': 'store_ref', 'datatype': 'integer'},
        {'name': 'region_ref', 'datatype': 'string'},
        {'name': 'note', 'datatype': 'string'},
    ]
    fks = [_fk(['region_ref', 'store_ref'], ['region', 'store'])]
    tg = _write_tablegroup(
        tmp_path,
        'store,region\n001,NA\n002,EU\n',
        'store_ref,region_ref,note\n1,NA,r1\n2,EU,r2\n',
        _integer_parent_columns(),
        child_columns,
        fks)
    assert tg.check_referential_integrity(strict=True)

    # Unrelated descriptive properties do not affect the integrity verdict.
    tg.common_props['dc:title'] = 'an unrelated title'
    note_column = tg.tabledict[CHILD_URL].tableSchema.get_column('note')
    note_column.propertyUrl = 'http://example.org/note'
    note_column.titles = {'und': ['A note']}
    assert tg.check_referential_integrity(strict=True)

    # Renaming the data file (and its URL in lockstep) leaves the relation intact.
    renamed = _write_tablegroup(
        tmp_path,
        'store,region\n001,NA\n002,EU\n',
        'store_ref,region_ref,note\n1,NA,r1\n2,EU,r2\n',
        _integer_parent_columns(),
        child_columns,
        [{
            'columnReference': ['region_ref', 'store_ref'],
            'reference': {
                'resource': PARENT_URL,
                'columnReference': ['region', 'store'],
            },
        }],
        child_url='child_renamed.csv')
    assert (tmp_path / 'child_renamed.csv').exists()
    assert renamed.check_referential_integrity(strict=True)
