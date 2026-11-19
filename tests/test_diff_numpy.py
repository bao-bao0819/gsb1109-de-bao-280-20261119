import re

import pytest
from deepdiff import DeepDiff, Delta
from deepdiff.helper import np, np_ndarray
from tests import parameterize_cases

"""
These are numpy specific test cases.
There are more numpy tests for delta additions in the test_delta.py
"""

NUMPY_CASES = {
    'numpy_bools': {
        't1': np.array([True, False, True, False], dtype=bool),
        't2': np.array([False, True, True, False], dtype=bool),
        'deepdiff_kwargs': {},
        'expected_result': {'values_changed': {'root[0]': {'new_value': False, 'old_value': True},
                            'root[1]': {'new_value': True, 'old_value': False}}},
    },
    'numpy_bools_ignore_order': {
        't1': np.array([True, False, True, False], dtype=bool),
        't2': np.array([False, True, True, False], dtype=bool),
        'deepdiff_kwargs': {'ignore_order': True},
        'expected_result': {},
    },
    'numpy_multi_dimensional1': {
        't1': np.array([[[1, 2, 3], [4, 5, 6]]], np.int32),
        't2': np.array([[[1, 2, 5], [3, 5, 6]]], np.int32),
        'deepdiff_kwargs': {},
        'expected_result': {'values_changed':
                            {'root[0][0][2]': {'new_value': 5, 'old_value': 3},
                             'root[0][1][0]': {'new_value': 3, 'old_value': 4}}},
    },
    'numpy_array2_type_change': {
        't1': np.array([1, 2, 3], np.int8),
        't2': np.array([1, 2, 5], np.int32),
        'deepdiff_kwargs': {'verbose_level': 0},
        'expected_result': {'type_changes': {'root': {'old_type': np.int8, 'new_type': np.int32}}},
    },
    'numpy_array3_ignore_number_type_changes': {
        't1': np.array([1, 2, 3], np.int8),
        't2': np.array([1, 2, 5], np.int32),
        'deepdiff_kwargs': {'ignore_numeric_type_changes': True},
        'expected_result': {'values_changed': {'root[2]': {'new_value': 5, 'old_value': 3}}},
    },
    'numpy_array4_ignore_number_type_changes_and_ignore_order': {
        't1': np.array([1, 2, 3], np.int8),
        't2': np.array([3, 1, 2], np.int32),
        'deepdiff_kwargs': {'ignore_numeric_type_changes': True, 'ignore_order': True},
        'expected_result': {},
    },
    'numpy_array5_ignore_number_type_changes_and_ignore_order': {
        't1': np.array([1, 2, 4, 3], np.int8),
        't2': np.array([3, 1, 2, 5], np.int32),
        'deepdiff_kwargs': {'ignore_numeric_type_changes': True, 'ignore_order': True},
        'expected_result': {'values_changed': {'root[2]': {'new_value': 5, 'old_value': 4}}},
    },
    'numpy_array6_ignore_order_and_report_repetition': {
        't1': np.array([1, 2, 3, 3], np.int8),
        't2': np.array([3, 1, 2, 5], np.int8),
        'deepdiff_kwargs': {'report_repetition': True, 'ignore_order': True},
        'expected_result': {'iterable_item_added': {'root[3]': 5},
                            'repetition_change': {'root[2]': {'old_repeat': 2, 'new_repeat': 1,
                                                              'old_indexes': [2, 3], 'new_indexes': [0], 'value': 3}}},
    },
    'numpy_array7_ignore_order_multi_dimensional_array': {
        't1': np.array([[1, 2, 3, 4], [4, 2, 2, 1]], np.int8),
        't2': np.array([[4, 1, 1, 1], [1, 3, 2, 4]], np.int8),
        'deepdiff_kwargs': {'report_repetition': True, 'ignore_order': True},
        'expected_result': {
            'iterable_item_removed': {
                'root[1][1]': 2,
                'root[1][2]': 2
            },
            'repetition_change': {
                'root[1][3]': {
                    'old_repeat': 1,
                    'new_repeat': 3,
                    'old_indexes': [3],
                    'new_indexes': [1, 2, 3],
                    'value': 1
                }
            }
        },
    },
    'numpy_array8_ignore_order_multi_dimensional_array_converted_to_list': {
        't1': np.array([[1, 2, 3, 4], [4, 2, 2, 1]], np.int8).tolist(),
        't2': np.array([[4, 1, 1, 1], [1, 3, 2, 4]], np.int8).tolist(),
        'deepdiff_kwargs': {
            'report_repetition': True,
            'ignore_order': True
        },
        'expected_result': {
            'iterable_item_removed': {
                'root[1][1]': 2,
                'root[1][2]': 2
            },
            'repetition_change': {
                'root[1][3]': {
                    'old_repeat': 1,
                    'new_repeat': 3,
                    'old_indexes': [3],
                    'new_indexes': [1, 2, 3],
                    'value': 1
                }
            }
        },
    },
    'numpy_array9_ignore_nan_inequality_float32': {
        't1': np.array([1, 2, 3, np.nan], np.float32),
        't2': np.array([1, 2, 4, np.nan], np.float32),
        'deepdiff_kwargs': {
            'ignore_nan_inequality': True,
        },
        'expected_result': {'values_changed': {'root[2]': {'new_value': 4.0, 'old_value': 3.0}}}
    },
    'numpy_almost_equal': {
        't1': np.array([1.0, 2.3333333333333]),
        't2': np.array([1.0, 2.33333334]),
        'deepdiff_kwargs': {'significant_digits': 3},
        'expected_result': {},
    },
    'numpy_almost_equal2': {
        't1': np.array(['a', 'b'], dtype=object),
        't2': np.array(['a', 'b'], dtype=object),
        'deepdiff_kwargs': {'significant_digits': 6},
        'expected_result': {},
    },
    'numpy_different_shape': {
        't1': np.array([[1, 1], [2, 3]]),
        't2': np.array([1]),
        'deepdiff_kwargs': {},
        'expected_result': {
            'type_changes': {
                'root[0]': {
                    'old_type': list,
                    'new_type': int,
                    'old_value': [1, 1],
                    'new_value': 1
                }
            },
            'iterable_item_removed': {
                'root[1]': [2, 3]
            }
        },
    },
    'numpy_datetime_equal': {
        't1': np.datetime64('2023-07-05T10:11:12'),
        't2': np.datetime64('2023-07-05T10:11:12'),
        'deepdiff_kwargs': {},
        'expected_result': {},
    },
    'numpy_datetime_unequal': {
        't1': np.datetime64('2023-07-05T10:11:12'),
        't2': np.datetime64('2024-07-05T10:11:12'),
        'deepdiff_kwargs': {},
        'expected_result': {
            'values_changed': {
                'root': {
                    'new_value': np.datetime64('2024-07-05T10:11:12'),
                    'old_value': np.datetime64('2023-07-05T10:11:12'),
                }
            },
        },
    },
}


NUMPY_CASES_PARAMS = parameterize_cases('test_name, t1, t2, deepdiff_kwargs, expected_result', NUMPY_CASES)


PATH_INDEXES = re.compile(r'\[(\d+)\]')


def _path_axes(path):
    """The axes a reported array path points at, e.g. root[1][0] -> (1, 0)."""
    return tuple(int(index) for index in PATH_INDEXES.findall(path))


# Same data, different shape. The diff must reopen the data along t1's axes instead of
# flattening it, and the replay must rebuild t2 with t2's shape.
ARRAY_SHAPE_CASES = {
    '2x2_to_4': {
        't1': np.array([[1, 2], [3, 4]], np.int8),
        't2': np.array([1, 2, 3, 4], np.int8),
        'deepdiff_kwargs': {},
        'expected_axes': {'root[0]', 'root[1]', 'root[2]', 'root[3]'},
        'expected_row_paths': {'root[0]', 'root[1]'},
    },
    '2x3_to_6': {
        't1': np.array([[1, 2, 3], [4, 5, 6]], np.int8),
        't2': np.array([1, 2, 3, 4, 5, 6], np.int8),
        'deepdiff_kwargs': {},
        'expected_axes': {'root[0]', 'root[1]', 'root[2]', 'root[3]', 'root[4]', 'root[5]'},
        'expected_row_paths': {'root[0]', 'root[1]'},
    },
    '1x2x2_to_4': {
        't1': np.array([[[1, 2], [3, 4]]], np.int8),
        't2': np.array([1, 2, 3, 4], np.int8),
        'deepdiff_kwargs': {},
        'expected_axes': {'root[0]', 'root[1]', 'root[2]', 'root[3]'},
        'expected_row_paths': {'root[0]'},
    },
    '2x3_to_3x2': {
        't1': np.array([[1, 2, 3], [4, 5, 6]], np.int8),
        't2': np.array([[1, 2], [3, 4], [5, 6]], np.int8),
        'deepdiff_kwargs': {},
        'expected_axes': {'root[1][0]', 'root[1][1]', 'root[2]', 'root[0][2]', 'root[1][2]'},
        'expected_row_paths': {'root[2]'},
        'replayable': False,
    },
}

ARRAY_DTYPE_CASES = {
    'int8_to_int32_same_values': {
        't1': np.array([[1, 2], [3, 4]], np.int8),
        't2': np.array([[1, 2], [3, 4]], np.int32),
        'deepdiff_kwargs': {},
    },
    'int8_to_int32_with_value_change': {
        't1': np.array([[1, 2], [3, 4]], np.int8),
        't2': np.array([[1, 2], [3, 5]], np.int32),
        'deepdiff_kwargs': {},
    },
}

ARRAY_TRANSPOSE_CASES = {
    'square_2x2': {
        't1': np.array([[1, 2], [3, 4]], np.int8),
        't2': np.array([[1, 3], [2, 4]], np.int8),
        'deepdiff_kwargs': {},
        'expected_axes': {'root[0][1]', 'root[1][0]'},
    },
    'square_3x3': {
        't1': np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]], np.int8),
        't2': np.array([[1, 4, 7], [2, 5, 8], [3, 6, 9]], np.int8),
        'deepdiff_kwargs': {},
        'expected_axes': {'root[0][1]', 'root[0][2]', 'root[1][0]',
                          'root[1][2]', 'root[2][0]', 'root[2][1]'},
    },
    'square_2x2_ignore_order': {
        't1': np.array([[1, 2], [3, 4]], np.int8),
        't2': np.array([[1, 3], [2, 4]], np.int8),
        'deepdiff_kwargs': {'ignore_order': True, 'report_repetition': True},
        'expected_axes': {'root[0][1]', 'root[1][0]'},
        'replay_kwargs': {'ignore_order': True},
    },
    # A transposed permutation matrix has the same rows as the original, so an unordered
    # row comparison used to return {} and silently made the axes interchangeable.
    'permutation_3x3_ignore_order': {
        't1': np.array([[0, 1, 0], [0, 0, 1], [1, 0, 0]], np.int8),
        't2': np.array([[0, 0, 1], [1, 0, 0], [0, 1, 0]], np.int8),
        'deepdiff_kwargs': {'ignore_order': True, 'report_repetition': True},
        'expected_axes': {'root[0][1]', 'root[0][2]', 'root[1][0]',
                          'root[1][2]', 'root[2][0]', 'root[2][1]'},
        'replay_kwargs': {'ignore_order': True},
    },
}


def _reported_paths(diff):
    return {path for paths in diff.values() for path in paths}


class TestNumpy:

    @pytest.mark.parametrize(**NUMPY_CASES_PARAMS)
    def test_numpy(self, test_name, t1, t2, deepdiff_kwargs, expected_result):
        diff = DeepDiff(t1, t2, **deepdiff_kwargs)
        assert expected_result == diff, f"test_numpy {test_name} failed."

    @pytest.mark.parametrize('test_name', ARRAY_SHAPE_CASES, ids=ARRAY_SHAPE_CASES)
    def test_different_shape_same_data(self, test_name):
        case = ARRAY_SHAPE_CASES[test_name]
        t1, t2, deepdiff_kwargs = case['t1'], case['t2'], case['deepdiff_kwargs']
        diff = DeepDiff(t1, t2, **deepdiff_kwargs)

        assert _reported_paths(diff) == case['expected_axes'], \
            f"{test_name}: the diff does not point at the changed axes."
        # Rows are reported where they are, not flattened into scalars at some other axis.
        type_change_paths = set(diff.get('type_changes') or {})
        assert type_change_paths <= case['expected_row_paths'], \
            f"{test_name}: a row was reported outside its axis."

        if not case.get('replayable', True):
            return  # Delta cannot yet remove an axis of an existing ndarray in place.

        replay = Delta(diff, bidirectional=False, raise_errors=True) + t1
        assert isinstance(replay, np_ndarray), f"{test_name}: replay must stay a numpy array."
        assert replay.shape == t2.shape, f"{test_name}: replay has the wrong shape."
        assert np.array_equal(replay, t2), f"{test_name}: replay did not reproduce t2."
        assert not DeepDiff(replay, t2, **deepdiff_kwargs), f"{test_name}: replay != t2."

    @pytest.mark.parametrize('test_name', ARRAY_DTYPE_CASES, ids=ARRAY_DTYPE_CASES)
    def test_dtype_change_same_shape(self, test_name):
        case = ARRAY_DTYPE_CASES[test_name]
        t1, t2, deepdiff_kwargs = case['t1'], case['t2'], case['deepdiff_kwargs']
        diff = DeepDiff(t1, t2, **deepdiff_kwargs)

        assert set(diff) == {'type_changes'}, f"{test_name}: a dtype change is a root type change."
        assert _reported_paths(diff) == {'root'}, f"{test_name}: the dtype change must be at the array root."
        assert diff['type_changes']['root']['old_type'] == np.int8
        assert diff['type_changes']['root']['new_type'] == np.int32

        replay = Delta(diff, bidirectional=False, raise_errors=True) + t1
        assert isinstance(replay, np_ndarray), f"{test_name}: replay must stay a numpy array."
        assert replay.shape == t1.shape and replay.dtype == np.int32, \
            f"{test_name}: replay has the wrong shape or dtype."
        assert np.array_equal(replay, t2), f"{test_name}: replay did not reproduce t2."

    @pytest.mark.parametrize('test_name', ARRAY_TRANSPOSE_CASES, ids=ARRAY_TRANSPOSE_CASES)
    def test_transpose_points_at_swapped_axes(self, test_name):
        case = ARRAY_TRANSPOSE_CASES[test_name]
        t1, t2, deepdiff_kwargs = case['t1'], case['t2'], case['deepdiff_kwargs']
        diff = DeepDiff(t1, t2, **deepdiff_kwargs)

        assert _reported_paths(diff) == case['expected_axes'], \
            f"{test_name}: the diff does not point at the transposed cells."
        for path, change in diff['values_changed'].items():
            row, column = _path_axes(path)
            assert t1[row, column] == change['old_value'], f"{test_name}: {path} is not a t1 axis."
            assert t2[row, column] == change['new_value'], f"{test_name}: {path} is not a t2 axis."
            assert t1[row, column] == t2[column, row], f"{test_name}: {path} is not a transposed axis."

        replay_kwargs = case.get('replay_kwargs', {})
        replay = Delta(diff, bidirectional=False, raise_errors=True) + t1
        assert isinstance(replay, np_ndarray), f"{test_name}: replay must stay a numpy array."
        assert replay.shape == t2.shape, f"{test_name}: replay changed the array shape."
        assert np.array_equal(replay, t2), f"{test_name}: replay did not reproduce t2."
        assert not DeepDiff(replay, t2, **replay_kwargs), f"{test_name}: replay != t2."

