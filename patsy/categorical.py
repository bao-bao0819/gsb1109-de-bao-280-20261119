# This file is part of Patsy
# Copyright (C) 2011-2013 Nathaniel Smith <njs@pobox.com>
# See file LICENSE.txt for license information.

__all__ = [
    "C",
    "guess_categorical",
    "CategoricalSniffer",
    "categorical_to_int",
    "UnknownValueError",
]

# How we handle categorical data: the big picture
# -----------------------------------------------
#
# There is no Python/NumPy standard for how to represent categorical data.
# There is no Python/NumPy standard for how to represent missing data.
#
# Together, these facts mean that when we receive some data object, we must be
# able to heuristically infer what levels it has -- and this process must be
# sensitive to the current missing data handling, because maybe 'None' is a
# level and maybe it is missing data.
#
# We don't know how missing data is represented until we get into the actual
# builder code, so anything which runs before this -- e.g., the 'C()' builtin
# -- cannot actually do *anything* meaningful with the data.
#
# Therefore, C() simply takes some data and arguments, and boxes them all up
# together into an object called (appropriately enough) _CategoricalBox. All
# the actual work of handling the various different sorts of categorical data
# (lists, string arrays, bool arrays, pandas.Categorical, etc.) happens inside
# the builder code, and we just extend this so that it also accepts
# _CategoricalBox objects as yet another categorical type.
#
# Originally this file contained a container type (called 'Categorical'), and
# the various sniffing, conversion, etc., functions were written as methods on
# that type. But we had to get rid of that type, so now this file just
# provides a set of plain old functions which are used by patsy.build to
# handle the different stages of categorical data munging.

import numpy as np

from patsy import PatsyError
from patsy.util import (
    SortAnythingKey,
    safe_scalar_isnan,
    iterable,
    have_pandas,
    have_pandas_categorical,
    have_pandas_categorical_dtype,
    safe_is_pandas_categorical,
    pandas_Categorical_from_codes,
    pandas_Categorical_categories,
    pandas_Categorical_codes,
    safe_issubdtype,
    no_pickling,
    assert_no_pickling,
)

if have_pandas:
    import pandas


class UnknownValueError(PatsyError):
    """Raised when categorical data contains a value that was not seen when
    the :class:`~patsy.design_info.DesignInfo` was built, and the offending
    factor has no reserved "unknown value" level to catch it.

    This is a :class:`~patsy.PatsyError` subclass, so existing code which
    catches :class:`~patsy.PatsyError` keeps working unchanged.
    """


# Objects of this type will always be treated as categorical, with the
# specified levels and contrast (if given).
class _CategoricalBox(object):
    def __init__(self, data, contrast, levels, unknown_value=None):
        self.data = data
        self.contrast = contrast
        self.levels = levels
        self.unknown_value = unknown_value

    __getstate__ = no_pickling


def C(data, contrast=None, levels=None, unknown_value=None):
    """
    Marks some `data` as being categorical, and specifies how to interpret
    it.

    This is used for three reasons:

    * To explicitly mark some data as categorical. For instance, integer data
      is by default treated as numerical. If you have data that is stored
      using an integer type, but where you want patsy to treat each different
      value as a different level of a categorical factor, you can wrap it in a
      call to `C` to accomplish this. E.g., compare::

        dmatrix("a", {"a": [1, 2, 3]})
        dmatrix("C(a)", {"a": [1, 2, 3]})

    * To explicitly set the levels or override the default level ordering for
      categorical data, e.g.::

        dmatrix("C(a, levels=["a2", "a1"])", balanced(a=2))
    * To override the default coding scheme for categorical data. The
      `contrast` argument can be any of:

      * A :class:`ContrastMatrix` object
      * A simple 2d ndarray (which is treated the same as a ContrastMatrix
        object except that you can't specify column names)
      * An object with methods called `code_with_intercept` and
        `code_without_intercept`, like the built-in contrasts
        (:class:`Treatment`, :class:`Diff`, :class:`Poly`, etc.). See
        :ref:`categorical-coding` for more details.
      * A callable that returns one of the above.

    :arg unknown_value: If given, this reserves an extra level named
      `unknown_value` which is *not* expected to appear in the training data,
      but is appended to the factor's list of levels. When the resulting
      :class:`~patsy.design_info.DesignInfo` is later used to build a design
      matrix from new data, any value that does not match one of the ordinary
      levels is coded against this reserved level instead of raising an
      error. This lets a model trained on one set of categories be applied to
      data that contains brand-new categories (e.g. a new supply region).
      Ordinary missing values keep being handled by the configured
      :class:`~patsy.missing.NAAction` and are *not* redirected into this
      bucket. If `unknown_value` collides with an ordinary level, or a value
      equal to `unknown_value` is actually present in the training data, a
      :class:`~patsy.PatsyError` is raised while building the design.
      Categorical factors without an `unknown_value` keep the default strict
      behaviour, where an unseen value raises an error.
    """
    if isinstance(data, _CategoricalBox):
        if contrast is None:
            contrast = data.contrast
        if levels is None:
            levels = data.levels
        if unknown_value is None:
            unknown_value = data.unknown_value
        data = data.data
    return _CategoricalBox(data, contrast, levels, unknown_value)


def test_C():
    c1 = C("asdf")
    assert isinstance(c1, _CategoricalBox)
    assert c1.data == "asdf"
    assert c1.levels is None
    assert c1.contrast is None
    c2 = C("DATA", "CONTRAST", "LEVELS")
    assert c2.data == "DATA"
    assert c2.contrast == "CONTRAST"
    assert c2.levels == "LEVELS"
    c3 = C(c2, levels="NEW LEVELS")
    assert c3.data == "DATA"
    assert c3.contrast == "CONTRAST"
    assert c3.levels == "NEW LEVELS"
    c4 = C(c2, "NEW CONTRAST")
    assert c4.data == "DATA"
    assert c4.contrast == "NEW CONTRAST"
    assert c4.levels == "LEVELS"

    assert_no_pickling(c4)

    c5 = C("DATA", unknown_value="UNKNOWN")
    assert c5.unknown_value == "UNKNOWN"
    # re-wrapping a box preserves the reserved level unless overridden
    c6 = C(c5)
    assert c6.unknown_value == "UNKNOWN"
    c7 = C(c5, unknown_value="OTHER")
    assert c7.unknown_value == "OTHER"
    assert C("DATA").unknown_value is None


def guess_categorical(data):
    if safe_is_pandas_categorical(data):
        return True
    if isinstance(data, _CategoricalBox):
        return True
    data = np.asarray(data)
    if safe_issubdtype(data.dtype, np.number):
        return False
    return True


def test_guess_categorical():
    if have_pandas_categorical:
        c = pandas.Categorical([1, 2, 3])
        assert guess_categorical(c)
        if have_pandas_categorical_dtype:
            assert guess_categorical(pandas.Series(c))
    assert guess_categorical(C([1, 2, 3]))
    assert guess_categorical([True, False])
    assert guess_categorical(["a", "b"])
    assert guess_categorical(["a", "b", np.nan])
    assert guess_categorical(["a", "b", None])
    assert not guess_categorical([1, 2, 3])
    assert not guess_categorical([1, 2, 3, np.nan])
    assert not guess_categorical([1.0, 2.0, 3.0])
    assert not guess_categorical([1.0, 2.0, 3.0, np.nan])


def _categorical_shape_fix(data):
    # helper function
    # data should not be a _CategoricalBox or pandas Categorical or anything
    # -- it should be an actual iterable of data, but which might have the
    # wrong shape.
    if hasattr(data, "ndim") and data.ndim > 1:
        raise PatsyError("categorical data cannot be >1-dimensional")
    # coerce scalars into 1d, which is consistent with what we do for numeric
    # factors. (See statsmodels/statsmodels#1881)
    if not iterable(data) or isinstance(data, (str, bytes)):
        data = [data]
    return data


class CategoricalSniffer(object):
    def __init__(self, NA_action, origin=None):
        self._NA_action = NA_action
        self._origin = origin
        self._contrast = None
        self._levels = None
        self._level_set = set()
        self._unknown_value = None

    def levels_contrast(self):
        if self._levels is None:
            levels = list(self._level_set)
            levels.sort(key=SortAnythingKey)
            self._levels = levels
        levels = tuple(self._levels)
        unknown_value = self._unknown_value
        if unknown_value is not None:
            try:
                collision = unknown_value in levels
            except TypeError:
                raise PatsyError(
                    "Error interpreting categorical data: "
                    "the unknown_value level must be hashable",
                    self._origin,
                )
            if collision:
                raise PatsyError(
                    "unknown_value %r collides with an existing level of "
                    "this categorical factor; the reserved unknown-value "
                    "bucket must not be used as a real level" % (unknown_value,),
                    self._origin,
                )
            levels = levels + (unknown_value,)
        return levels, self._contrast, unknown_value

    def sniff(self, data):
        if hasattr(data, "contrast"):
            self._contrast = data.contrast
        # returns a bool: are we confident that we found all the levels?
        if isinstance(data, _CategoricalBox):
            if data.unknown_value is not None:
                self._unknown_value = data.unknown_value
            if data.levels is not None:
                self._levels = tuple(data.levels)
                return True
            else:
                # unbox and fall through
                data = data.data
        if safe_is_pandas_categorical(data):
            # pandas.Categorical has its own NA detection, so don't try to
            # second-guess it.
            self._levels = tuple(pandas_Categorical_categories(data))
            return True
        # fastpath to avoid doing an item-by-item iteration over boolean
        # arrays, as requested by #44
        if hasattr(data, "dtype") and safe_issubdtype(data.dtype, np.bool_):
            self._level_set = set([True, False])
            return True

        data = _categorical_shape_fix(data)

        for value in data:
            if self._NA_action.is_categorical_NA(value):
                continue
            if value is True or value is False:
                self._level_set.update([True, False])
            else:
                try:
                    self._level_set.add(value)
                except TypeError:
                    raise PatsyError(
                        "Error interpreting categorical data: "
                        "all items must be hashable",
                        self._origin,
                    )
        # If everything we've seen is boolean, assume that everything else
        # would be too. Otherwise we need to keep looking.
        return self._level_set == set([True, False])

    __getstate__ = no_pickling


def test_CategoricalSniffer():
    from patsy.missing import NAAction

    def t(
        NA_types,
        datas,
        exp_finish_fast,
        exp_levels,
        exp_contrast=None,
        exp_unknown=None,
    ):
        sniffer = CategoricalSniffer(NAAction(NA_types=NA_types))
        for data in datas:
            done = sniffer.sniff(data)
            if done:
                assert exp_finish_fast
                break
            else:
                assert not exp_finish_fast
        assert sniffer.levels_contrast() == (
            exp_levels,
            exp_contrast,
            exp_unknown,
        )

    if have_pandas_categorical:
        # We make sure to test with both boxed and unboxed pandas objects,
        # because we used to have a bug where boxed pandas objects would be
        # treated as categorical, but their levels would be lost...
        preps = [lambda x: x, C]
        if have_pandas_categorical_dtype:
            preps += [pandas.Series, lambda x: C(pandas.Series(x))]
        for prep in preps:
            t([], [prep(pandas.Categorical([1, 2, None]))], True, (1, 2))
            # check order preservation
            t(
                [],
                [prep(pandas_Categorical_from_codes([1, 0], ["a", "b"]))],
                True,
                ("a", "b"),
            )
            t(
                [],
                [prep(pandas_Categorical_from_codes([1, 0], ["b", "a"]))],
                True,
                ("b", "a"),
            )
            # check that if someone sticks a .contrast field onto our object
            obj = prep(pandas.Categorical(["a", "b"]))
            obj.contrast = "CONTRAST"
            t([], [obj], True, ("a", "b"), "CONTRAST")

    t([], [C([1, 2]), C([3, 2])], False, (1, 2, 3))
    # check order preservation
    t([], [C([1, 2], levels=[1, 2, 3]), C([4, 2])], True, (1, 2, 3))
    t([], [C([1, 2], levels=[3, 2, 1]), C([4, 2])], True, (3, 2, 1))

    # do some actual sniffing with NAs in
    t(["None", "NaN"], [C([1, np.nan]), C([10, None])], False, (1, 10))
    # But 'None' can be a type if we don't make it represent NA:
    sniffer = CategoricalSniffer(NAAction(NA_types=["NaN"]))
    sniffer.sniff(C([1, np.nan, None]))
    # The level order here is different on py2 and py3 :-( Because there's no
    # consistent way to sort mixed-type values on both py2 and py3. Honestly
    # people probably shouldn't use this, but I don't know how to give a
    # sensible error.
    levels, _, _ = sniffer.levels_contrast()
    assert set(levels) == set([None, 1])

    # bool special cases
    t(["None", "NaN"], [C([True, np.nan, None])], True, (False, True))
    t([], [C([10, 20]), C([False]), C([30, 40])], False, (False, True, 10, 20, 30, 40))
    # exercise the fast-path
    t([], [np.asarray([True, False]), ["foo"]], True, (False, True))

    # check tuples too
    t(
        ["None", "NaN"],
        [C([("b", 2), None, ("a", 1), np.nan, ("c", None)])],
        False,
        (("a", 1), ("b", 2), ("c", None)),
    )

    # contrasts
    t([], [C([10, 20], contrast="FOO")], False, (10, 20), "FOO")

    # no box
    t([], [[10, 30], [20]], False, (10, 20, 30))
    t([], [["b", "a"], ["a"]], False, ("a", "b"))

    # 0d
    t([], ["b"], False, ("b",))

    import pytest

    # unhashable level error:
    sniffer = CategoricalSniffer(NAAction())
    pytest.raises(PatsyError, sniffer.sniff, [{}])

    # >1d is illegal
    pytest.raises(PatsyError, sniffer.sniff, np.asarray([["b"]]))

    # unknown_value reserves an extra level that is never observed
    t(
        [],
        [C(["a", "b"], unknown_value="UNK")],
        False,
        ("a", "b", "UNK"),
        exp_unknown="UNK",
    )
    t(
        [],
        [C(["b", "a"], levels=["b", "a"], unknown_value="UNK")],
        True,
        ("b", "a", "UNK"),
        exp_unknown="UNK",
    )
    # the reserved bucket is appended after an empty set of real levels too
    t([], [C([], levels=[], unknown_value="UNK")], True, ("UNK",), exp_unknown="UNK")

    # a reserved name that is also a real observed level is a configuration
    # error, both when levels are sniffed and when they are given explicitly
    sniffer = CategoricalSniffer(NAAction())
    sniffer.sniff(C(["a", "b", "UNK"], unknown_value="UNK"))
    pytest.raises(PatsyError, sniffer.levels_contrast)
    sniffer = CategoricalSniffer(NAAction())
    sniffer.sniff(C(["a", "b"], levels=["a", "UNK"], unknown_value="UNK"))
    pytest.raises(PatsyError, sniffer.levels_contrast)


# returns either a 1d ndarray or a pandas.Series
def categorical_to_int(
    data,
    levels,
    NA_action,
    origin=None,
    unknown_value=None,
    reserve_guard=False,
):
    assert isinstance(levels, tuple)
    # In this function, missing values are always mapped to -1
    unknown_int = None
    if unknown_value is not None:
        try:
            unknown_int = levels.index(unknown_value)
        except ValueError:
            raise PatsyError(
                "unknown_value %r is not one of the levels of this "
                "categorical factor" % (unknown_value,),
                origin,
            )

    def _reserve_conflict(value):
        raise PatsyError(
            "the reserved unknown-value level %r must not be occupied by a "
            "real observation; it is meant only for values unseen during "
            "training. Choose a different unknown_value, or remove this level "
            "from the data (observed value: %r)" % (unknown_value, value),
            origin,
        )

    if safe_is_pandas_categorical(data):
        data_levels_tuple = tuple(pandas_Categorical_categories(data))
        if unknown_value is None:
            if not data_levels_tuple == levels:
                raise PatsyError(
                    "mismatching levels: expected %r, got %r"
                    % (levels, data_levels_tuple),
                    origin,
                )
            # pandas.Categorical also uses -1 to indicate NA, and we don't try
            # to second-guess its NA detection, so we can just pass it back.
            return pandas_Categorical_codes(data)
        # A reserved unknown-value level is in use. The incoming pandas
        # Categorical may use a different category ordering, declare levels
        # that were absent during training, or both. Translate each pandas
        # code onto this factor's level indices; any category not among the
        # known levels lands in the reserved bucket. NA keeps its -1 code and
        # is still handled by the configured NA action.
        factor_index = dict(zip(levels, range(len(levels))))
        lookup = np.full(len(data_levels_tuple), unknown_int, dtype=int)
        for pandas_code, level in enumerate(data_levels_tuple):
            if level in factor_index:
                factor_code = factor_index[level]
                if reserve_guard and factor_code == unknown_int:
                    _reserve_conflict(level)
                lookup[pandas_code] = factor_code
            elif reserve_guard:
                # Training data must not declare categories outside the real
                # levels: the reserved bucket cannot absorb a training level.
                raise PatsyError(
                    "training data contains category %r which is not among "
                    "the declared levels; the reserved unknown-value bucket "
                    "cannot be used by a real training level" % (level,),
                    origin,
                )
        codes = np.asarray(pandas_Categorical_codes(data))
        # Map only the non-missing codes. Pandas uses -1 for NA, and an
        # all-missing Categorical has no categories at all, so indexing the
        # (possibly empty) lookup must be restricted to valid codes; NA keeps
        # its -1 code and is still handled by the configured NA action.
        out = np.full(codes.shape, -1, dtype=int)
        valid = codes >= 0
        if np.any(valid):
            out[valid] = lookup[codes[valid]]
        if have_pandas and isinstance(data, pandas.Series):
            out = pandas.Series(out, index=data.index)
        return out

    if isinstance(data, _CategoricalBox):
        if data.levels is not None:
            box_levels = tuple(data.levels)
            if unknown_value is not None:
                # The reserved bucket is stored alongside the ordinary levels
                # but is not something the caller lists explicitly.
                expected_levels = tuple(
                    level for level in levels if level != unknown_value
                )
            else:
                expected_levels = levels
            if box_levels != expected_levels:
                raise PatsyError(
                    "mismatching levels: expected %r, got %r"
                    % (expected_levels, box_levels),
                    origin,
                )
        data = data.data

    data = _categorical_shape_fix(data)

    try:
        level_to_int = dict(zip(levels, range(len(levels))))
    except TypeError:
        raise PatsyError(
            "Error interpreting categorical data: all items must be hashable", origin
        )

    # fastpath to avoid doing an item-by-item iteration over boolean arrays,
    # as requested by #44. Skipped when a reserved unknown-value level is in
    # play, since the per-element loop below owns the bucket routing/guard.
    if (
        unknown_int is None
        and hasattr(data, "dtype")
        and safe_issubdtype(data.dtype, np.bool_)
    ):
        if level_to_int[False] == 0 and level_to_int[True] == 1:
            return data.astype(np.int_)
    out = np.empty(len(data), dtype=int)
    for i, value in enumerate(data):
        if NA_action.is_categorical_NA(value):
            out[i] = -1
        else:
            try:
                code = level_to_int[value]
            except KeyError:
                if unknown_int is not None:
                    if reserve_guard:
                        _reserve_conflict(value)
                    out[i] = unknown_int
                    continue
                SHOW_LEVELS = 4
                level_strs = []
                if len(levels) <= SHOW_LEVELS:
                    level_strs += [repr(level) for level in levels]
                else:
                    level_strs += [repr(level) for level in levels[: SHOW_LEVELS // 2]]
                    level_strs.append("...")
                    level_strs += [repr(level) for level in levels[-SHOW_LEVELS // 2 :]]
                level_str = "[%s]" % (", ".join(level_strs))
                raise UnknownValueError(
                    "Error converting data to categorical: "
                    "observation with value %r does not match "
                    "any of the expected levels (expected: %s)" % (value, level_str),
                    origin,
                )
            except TypeError:
                raise PatsyError(
                    "Error converting data to categorical: "
                    "encountered unhashable value %r" % (value,),
                    origin,
                )
            else:
                if reserve_guard and code == unknown_int:
                    _reserve_conflict(value)
                out[i] = code
    if have_pandas and isinstance(data, pandas.Series):
        out = pandas.Series(out, index=data.index)
    return out


def test_categorical_to_int():
    import pytest
    from patsy.missing import NAAction

    if have_pandas:
        s = pandas.Series(["a", "b", "c"], index=[10, 20, 30])
        c_pandas = categorical_to_int(s, ("a", "b", "c"), NAAction())
        assert np.all(c_pandas == [0, 1, 2])
        assert np.all(c_pandas.index == [10, 20, 30])
        # Input must be 1-dimensional
        pytest.raises(
            PatsyError,
            categorical_to_int,
            pandas.DataFrame({10: s}),
            ("a", "b", "c"),
            NAAction(),
        )
    if have_pandas_categorical:
        constructors = [pandas_Categorical_from_codes]
        if have_pandas_categorical_dtype:

            def Series_from_codes(codes, categories):
                c = pandas_Categorical_from_codes(codes, categories)
                return pandas.Series(c)

            constructors.append(Series_from_codes)
        for con in constructors:
            cat = con([1, 0, -1], ("a", "b"))
            conv = categorical_to_int(cat, ("a", "b"), NAAction())
            assert np.all(conv == [1, 0, -1])
            # Trust pandas NA marking
            cat2 = con([1, 0, -1], ("a", "None"))
            conv2 = categorical_to_int(cat, ("a", "b"), NAAction(NA_types=["None"]))
            assert np.all(conv2 == [1, 0, -1])
            # But levels must match
            pytest.raises(
                PatsyError,
                categorical_to_int,
                con([1, 0], ("a", "b")),
                ("a", "c"),
                NAAction(),
            )
            pytest.raises(
                PatsyError,
                categorical_to_int,
                con([1, 0], ("a", "b")),
                ("b", "a"),
                NAAction(),
            )

    def t(data, levels, expected, NA_action=NAAction()):
        got = categorical_to_int(data, levels, NA_action)
        assert np.array_equal(got, expected)

    t(["a", "b", "a"], ("a", "b"), [0, 1, 0])
    t(np.asarray(["a", "b", "a"]), ("a", "b"), [0, 1, 0])
    t(np.asarray(["a", "b", "a"], dtype=object), ("a", "b"), [0, 1, 0])
    t([0, 1, 2], (1, 2, 0), [2, 0, 1])
    t(np.asarray([0, 1, 2]), (1, 2, 0), [2, 0, 1])
    t(np.asarray([0, 1, 2], dtype=float), (1, 2, 0), [2, 0, 1])
    t(np.asarray([0, 1, 2], dtype=object), (1, 2, 0), [2, 0, 1])
    t(["a", "b", "a"], ("a", "d", "z", "b"), [0, 3, 0])
    t([("a", 1), ("b", 0), ("a", 1)], (("a", 1), ("b", 0)), [0, 1, 0])

    pytest.raises(
        PatsyError, categorical_to_int, ["a", "b", "a"], ("a", "c"), NAAction()
    )

    t(C(["a", "b", "a"]), ("a", "b"), [0, 1, 0])
    t(C(["a", "b", "a"]), ("b", "a"), [1, 0, 1])
    t(C(["a", "b", "a"], levels=["b", "a"]), ("b", "a"), [1, 0, 1])
    # Mismatch between C() levels and expected levels
    pytest.raises(
        PatsyError,
        categorical_to_int,
        C(["a", "b", "a"], levels=["a", "b"]),
        ("b", "a"),
        NAAction(),
    )

    # ndim == 0 is okay
    t("a", ("a", "b"), [0])
    t("b", ("a", "b"), [1])
    t(True, (False, True), [1])

    # ndim == 2 is disallowed
    pytest.raises(
        PatsyError,
        categorical_to_int,
        np.asarray([["a", "b"], ["b", "a"]]),
        ("a", "b"),
        NAAction(),
    )

    # levels must be hashable
    pytest.raises(
        PatsyError, categorical_to_int, ["a", "b"], ("a", "b", {}), NAAction()
    )
    pytest.raises(
        PatsyError, categorical_to_int, ["a", "b", {}], ("a", "b"), NAAction()
    )

    t(
        ["b", None, np.nan, "a"],
        ("a", "b"),
        [1, -1, -1, 0],
        NAAction(NA_types=["None", "NaN"]),
    )
    t(
        ["b", None, np.nan, "a"],
        ("a", "b", None),
        [1, -1, -1, 0],
        NAAction(NA_types=["None", "NaN"]),
    )
    t(
        ["b", None, np.nan, "a"],
        ("a", "b", None),
        [1, 2, -1, 0],
        NAAction(NA_types=["NaN"]),
    )

    # Smoke test for the branch that formats the ellipsized list of levels in
    # the error message:
    pytest.raises(
        PatsyError,
        categorical_to_int,
        ["a", "b", "q"],
        ("a", "b", "c", "d", "e", "f", "g", "h"),
        NAAction(),
    )

    # --- unknown_value reserves a catch-all bucket -------------------------
    reserved_levels = ("a", "b", "UNK")
    # unseen values land in the reserved bucket; known values keep their codes
    got = categorical_to_int(
        ["a", "new1", "b", "new2"], reserved_levels, NAAction(), unknown_value="UNK"
    )
    assert np.array_equal(got, [0, 2, 1, 2])
    # the strict path is unchanged for factors without a reserved bucket
    pytest.raises(
        UnknownValueError,
        categorical_to_int,
        ["a", "new"],
        ("a", "b"),
        NAAction(),
    )
    # UnknownValueError is just a specialised PatsyError
    assert issubclass(UnknownValueError, PatsyError)
    # genuine missing values are NOT redirected into the bucket
    got_na = categorical_to_int(
        ["a", None, "new", np.nan],
        reserved_levels,
        NAAction(NA_types=["None", "NaN"]),
        unknown_value="UNK",
    )
    assert np.array_equal(got_na, [0, -1, 2, -1])
    # C() boxes carry the ordinary levels only (the bucket is not listed)
    got_box = categorical_to_int(
        C(["a", "new", "b"], levels=["a", "b"]),
        reserved_levels,
        NAAction(),
        unknown_value="UNK",
    )
    assert np.array_equal(got_box, [0, 2, 1])
    # the reserved bucket must actually be among the levels
    pytest.raises(
        PatsyError,
        categorical_to_int,
        ["a"],
        ("a", "b"),
        NAAction(),
        unknown_value="UNK",
    )

    if have_pandas_categorical:
        # A pandas Categorical whose category ordering differs and which
        # declares the never-seen training value:
        cat = pandas_Categorical_from_codes([1, 0, 2, -1], ["b", "a", "new_region"])
        got_pd = categorical_to_int(
            cat, reserved_levels, NAAction(), unknown_value="UNK"
        )
        # values are ["a", "b", "new_region", NA] -> factor codes [0, 1, 2, -1]
        assert np.array_equal(got_pd, [0, 1, 2, -1])
        # categories that match known levels are remapped despite ordering;
        # any extra declared category still routes to the reserved bucket
        cat2 = pandas_Categorical_from_codes([0, 1, 2], ["new_x", "a", "new_y"])
        got_pd2 = categorical_to_int(
            cat2, reserved_levels, NAAction(), unknown_value="UNK"
        )
        assert np.array_equal(got_pd2, [2, 0, 2])
        if have_pandas_categorical_dtype:
            ser = pandas.Series(
                pandas_Categorical_from_codes([2, 1], ["a", "b", "brand_new"])
            )
            got_ser = categorical_to_int(
                ser, reserved_levels, NAAction(), unknown_value="UNK"
            )
            assert np.array_equal(got_ser, [2, 1])
            assert np.all(got_ser.index == ser.index)
        # An all-missing Categorical has no categories at all; its -1 NA codes
        # must survive instead of indexing the empty lookup table.
        all_na = pandas.Categorical([None, None])
        got_all_na = categorical_to_int(
            all_na, reserved_levels, NAAction(NA_types=["None"]), unknown_value="UNK"
        )
        assert np.array_equal(got_all_na, [-1, -1])

    # --- reserve_guard (training-time configuration checks) ---------------
    # an observation whose value equals the reserved name is refused ...
    pytest.raises(
        PatsyError,
        categorical_to_int,
        ["a", "UNK"],
        reserved_levels,
        NAAction(),
        None,
        "UNK",
        True,
    )
    # ... as is an unseen value, so a real training level can't be absorbed
    pytest.raises(
        PatsyError,
        categorical_to_int,
        ["a", "brand_new"],
        reserved_levels,
        NAAction(),
        None,
        "UNK",
        True,
    )
    # legitimate known values still pass under the guard
    got_guard = categorical_to_int(
        ["a", "b", "a"],
        reserved_levels,
        NAAction(),
        unknown_value="UNK",
        reserve_guard=True,
    )
    assert np.array_equal(got_guard, [0, 1, 0])
    # the guard never redirects genuine missing values
    got_guard_na = categorical_to_int(
        ["a", None],
        reserved_levels,
        NAAction(NA_types=["None"]),
        unknown_value="UNK",
        reserve_guard=True,
    )
    assert np.array_equal(got_guard_na, [0, -1])

    if have_pandas_categorical:
        # pandas Categorical declaring the reserved name as a real category
        guarded_cat = pandas_Categorical_from_codes([0, 1], ["a", "UNK"])
        pytest.raises(
            PatsyError,
            categorical_to_int,
            guarded_cat,
            reserved_levels,
            NAAction(),
            None,
            "UNK",
            True,
        )
        # pandas Categorical declaring an extra non-reserved category
        extra_cat = pandas_Categorical_from_codes([0, 1], ["a", "extra"])
        pytest.raises(
            PatsyError,
            categorical_to_int,
            extra_cat,
            reserved_levels,
            NAAction(),
            None,
            "UNK",
            True,
        )
