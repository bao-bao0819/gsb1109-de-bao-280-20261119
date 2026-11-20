# This file is part of Patsy
# Copyright (C) 2026
# See file LICENSE.txt for license information.

import io
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
import pytest

from patsy import (
    PatsyError,
    Treatment,
    build_design_matrices,
    design_info_from_dict,
    design_info_from_json,
    design_info_to_dict,
    design_info_to_json,
    dmatrix,
    dmatrices,
    incr_dbuilder,
    load_design_info,
    save_design_info,
    stateful_transform,
)
from patsy import DesignInfo
from patsy.portable_design_info import _DESCRIPTION_FORMAT


def _roundtrip(design_info):
    return design_info_from_json(design_info_to_json(design_info))


def _matrix(design_info, data, **kwargs):
    return np.asarray(
        build_design_matrices([design_info], data, **kwargs)[0]
    )


def test_basic_formula_roundtrip():
    # Spaces in real column names, explicit categorical reference level,
    # a learned center() statistic, and interactions.
    data = {
        "my x": [1.0, 2.0, 3.0, 4.0],
        "cat": ["b", "a", "c", "a"],
        "z": [10.0, 20.0, 30.0, 40.0],
    }
    formula = (
        "Q('my x') + C(cat, Treatment('b')) + center(z)"
        " + center(z):C(cat) + Q('my x'):center(z)"
    )
    design = dmatrix(formula, data).design_info
    rebuilt = _roundtrip(design)

    assert rebuilt.column_names == design.column_names
    assert rebuilt.term_names == design.term_names

    new_data = {
        "my x": [5.0, 6.0, 7.0],
        "cat": ["c", "a", "b"],
        "z": [50.0, 60.0, 70.0],
    }
    np.testing.assert_array_equal(
        _matrix(design, new_data), _matrix(rebuilt, new_data)
    )


def test_chunked_training_keeps_learned_center_statistic():
    chunks = [
        {"x": [1.0, 2.0], "g": ["b", "a"]},
        {"x": [3.0, 4.0], "g": ["a", "c"]},
    ]
    design = incr_dbuilder(
        "center(x) + C(g)", lambda: iter(chunks)
    )
    description = design_info_to_dict(design)

    # The training mean (2.5) is recorded explicitly in the description.
    transform_state = description["factors"][0]["transforms"][0]["state"]
    np.testing.assert_allclose(transform_state["_sum"]["__ndarray__"], [10.0])
    assert transform_state["_count"] == 4

    rebuilt = design_info_from_dict(description)
    new_data = {"x": [10.0, 20.0], "g": ["a", "b"]}
    np.testing.assert_array_equal(
        _matrix(design, new_data), _matrix(rebuilt, new_data)
    )
    # New-batch mean would be 15; the stored training mean is 2.5, so the
    # centered values must be x - 2.5 rather than x - 15.
    centered = _matrix(rebuilt, new_data)[
        :, rebuilt.column_name_indexes["center(x)"]
    ]
    np.testing.assert_allclose(centered, [7.5, 17.5])


def test_each_center_uses_its_own_statistic():
    data = {
        "x": [1.0, 2.0, 3.0, 4.0],
        "y": [100.0, 200.0, 300.0, 400.0],
        "M": np.array([[1.0, 10.0], [3.0, 20.0], [5.0, 30.0], [7.0, 40.0]]),
    }
    design = dmatrix("center(x) + center(y) + center(M)", data).design_info
    rebuilt = _roundtrip(design)

    description = design_info_to_dict(design)
    center_states = [
        factor["transforms"][0]["state"]
        for factor in description["factors"]
        if factor["transforms"]
    ]
    assert len(center_states) == 3
    # Multi-column center remembers one sum per column.
    m_sum = center_states[2]["_sum"]["__ndarray__"]
    np.testing.assert_allclose(m_sum, [16.0, 100.0])

    new_data = {
        "x": [10.0],
        "y": [100.0],
        "M": np.array([[100.0, 100.0]]),
    }
    np.testing.assert_allclose(
        _matrix(design, new_data), _matrix(rebuilt, new_data)
    )
    rebuilt_values = _matrix(rebuilt, new_data)[0]
    # x centered by 2.5, y centered by 250, M centered by [4, 25]
    assert rebuilt_values[rebuilt.column_name_indexes["center(x)"]] == 7.5
    assert rebuilt_values[rebuilt.column_name_indexes["center(y)"]] == -150.0
    np.testing.assert_allclose(
        rebuilt_values[
            rebuilt.column_name_indexes["center(M)[0]"]
            : rebuilt.column_name_indexes["center(M)[1]"] + 1
        ],
        [96.0, 75.0],
    )


def test_categorical_levels_order_and_reference_are_fixed():
    # Training levels are seen in an order that is not lexicographic, and the
    # reference level is fixed explicitly.
    data = {
        "g": ["zebra", "apple", "mango", "apple", "zebra"],
        "flag": [True, False, True, False, True],
        "n": [30, 10, 20, 10, 30],
        "tup": [("k", 1), ("k", 2), ("k", 1), ("k", 2), ("k", 1)],
        "raw": [b"x", b"y", b"x", b"y", b"x"],
    }
    # Explicit levels fix an ordering that is not the default lexicographic
    # one, and the treatment reference level is fixed explicitly.
    formula = (
        "C(g, Treatment('mango'), levels=['zebra', 'apple', 'mango'])"
        " + C(flag) + C(n) + C(tup) + C(raw)"
    )
    design = dmatrix(formula, data).design_info
    description = design_info_to_dict(design)
    rebuilt = design_info_from_dict(description)

    assert rebuilt.column_names == design.column_names
    rebuilt_categories = {
        factor["code"]: tuple(factor["categories"]["__tuple__"])
        for factor in description["factors"]
        if factor["type"] == "categorical"
    }
    assert rebuilt_categories[
        "C(g, Treatment('mango'), levels=['zebra', 'apple', 'mango'])"
    ] == ("zebra", "apple", "mango")
    np.testing.assert_array_equal(
        _matrix(design, data), _matrix(rebuilt, data)
    )


def test_interactions_and_full_rank_coding_roundtrip():
    data = {
        "a": ["z", "y", "z", "y", "z"],
        "b": ["p", "q", "q", "p", "p"],
        "x": [1.0, 2.0, 3.0, 4.0, 5.0],
    }
    # C(a):C(b) forces patsy's redundancy handling (multiple SubtermInfos),
    # and the leading 0 requests full-rank categorical coding.
    formula = "0 + C(a, Treatment) + C(a):C(b) + x:C(b)"
    design = dmatrix(formula, data).design_info
    rebuilt = _roundtrip(design)

    assert rebuilt.column_names == design.column_names
    assert [term.name() for term in rebuilt.terms] == [
        term.name() for term in design.terms
    ]
    np.testing.assert_array_equal(
        _matrix(design, data), _matrix(rebuilt, data)
    )


def test_missing_value_behavior_matches_original():
    data = {"x": [1.0, 2.0, 3.0, 4.0], "g": ["a", "b", "a", "b"]}
    design = dmatrix("x + C(g)", data).design_info
    rebuilt = _roundtrip(design)

    new_data = {
        "x": [1.0, np.nan, 3.0],
        "g": ["a", "b", "a"],
    }
    np.testing.assert_array_equal(
        _matrix(design, new_data, NA_action="drop"),
        _matrix(rebuilt, new_data, NA_action="drop"),
    )
    for candidate in (design, rebuilt):
        pytest.raises(
            PatsyError,
            build_design_matrices,
            [candidate],
            new_data,
            NA_action="raise",
        )


def test_unknown_level_behavior_matches_original():
    data = {"g": ["a", "b", "a", "b"]}
    design = dmatrix("C(g)", data).design_info
    rebuilt = _roundtrip(design)

    new_data = {"g": ["a", "never-seen"]}
    errors = []
    for candidate in (design, rebuilt):
        with pytest.raises(PatsyError) as exc_info:
            build_design_matrices([candidate], new_data)
        errors.append(str(exc_info.value).splitlines()[0])
    assert errors[0] == errors[1]
    assert "never-seen" in errors[0]


def test_rejects_custom_plain_function_at_export():
    def double_it(values):
        return np.asarray(values) * 2

    design = dmatrix("double_it(x)", {"x": [1.0, 2.0, 3.0]}).design_info
    with pytest.raises(PatsyError, match="double_it"):
        design_info_to_json(design)


def test_rejects_custom_stateful_transform_at_export():
    class CustomTransform(object):
        def __init__(self):
            self.total = 0.0

        def memorize_chunk(self, values):
            self.total += np.sum(values)

        def memorize_finish(self):
            pass

        def transform(self, values):
            return np.asarray(values) - self.total

    custom = stateful_transform(CustomTransform)
    design = dmatrix("custom(x)", {"x": [1.0, 2.0, 3.0]}).design_info
    # The name is not a Patsy builtin, so it is refused before any output.
    with pytest.raises(PatsyError, match="custom"):
        design_info_to_json(design)


def test_rejects_stateful_transform_shadowing_a_builtin_name():
    class FakeCenter(object):
        def __init__(self):
            self.total = 0.0

        def memorize_chunk(self, values):
            self.total += np.sum(values)

        def memorize_finish(self):
            pass

        def transform(self, values):
            return np.asarray(values) + self.total

    # Same call name as the builtin, but a different transform object.
    center = stateful_transform(FakeCenter)
    design = dmatrix("center(x)", {"x": [1.0, 2.0, 3.0]}).design_info
    with pytest.raises(PatsyError, match="redefined"):
        design_info_to_json(design)


def test_rejects_captured_training_process_variable():
    captured = np.array([7.0, 8.0, 9.0])
    design = dmatrix("x + captured", {"x": [1.0, 2.0, 3.0]}).design_info
    with pytest.raises(PatsyError, match="captured"):
        design_info_to_json(design)


def test_rejects_minimal_design_info():
    with pytest.raises(PatsyError, match="no factor or term information"):
        design_info_to_json(DesignInfo(["a", "b"]))


def test_standardize_roundtrip():
    data = {"x": [1.0, 2.0, 3.0, 4.0, 5.0]}
    design = dmatrix(
        "standardize(x) + center(x)", data
    ).design_info
    rebuilt = _roundtrip(design)

    new_data = {"x": [2.0, 9.0]}
    np.testing.assert_allclose(
        _matrix(design, new_data), _matrix(rebuilt, new_data)
    )


def test_stateful_transform_inside_arithmetic():
    data = {"x": [1.0, 2.0, 3.0, 4.0], "y": [10.0, 20.0, 30.0, 40.0]}
    design = dmatrix("I(center(x) + y)", data).design_info
    rebuilt = _roundtrip(design)
    np.testing.assert_allclose(
        _matrix(design, data), _matrix(rebuilt, data)
    )


def test_spline_transforms_roundtrip():
    pytest.importorskip("scipy")
    from patsy import bs, cc, cr, te

    data = {
        "x": np.linspace(0.0, 1.0, 20),
        "u": np.linspace(0.0, 1.0, 20),
        "v": np.linspace(0.0, 1.0, 20),
        "w": np.linspace(2.0, 3.0, 20),
    }
    formula = (
        "bs(x, df=4) + cr(u, df=5) + cc(v, df=4)"
        " + te(cr(v, df=3), cr(w, df=3), constraints='center')"
    )
    design = dmatrix(formula, data).design_info
    rebuilt = _roundtrip(design)

    assert rebuilt.column_names == design.column_names
    new_data = {
        "x": [0.2, 0.8],
        "u": [0.3, 0.7],
        "v": [0.1, 0.9],
        "w": [2.2, 2.8],
    }
    np.testing.assert_allclose(
        _matrix(design, new_data), _matrix(rebuilt, new_data), atol=1e-12
    )

    # A rebuilt spline design must itself be exportable without carrying the
    # constructor's scratch state.
    re_rebuilt = _roundtrip(rebuilt)
    np.testing.assert_allclose(
        _matrix(re_rebuilt, new_data), _matrix(design, new_data), atol=1e-12
    )


def test_dict_json_and_file_interfaces_roundtrip():
    design = dmatrix(
        "x + center(x) + C(g)",
        {"x": [1.0, 2.0, 3.0], "g": ["a", "b", "a"]},
    ).design_info

    as_dict = design_info_to_dict(design)
    assert isinstance(as_dict, dict)
    assert as_dict["format"] == _DESCRIPTION_FORMAT
    assert design_info_from_dict(as_dict).column_names == design.column_names

    as_json = design_info_to_json(design)
    assert json.loads(as_json)["format"] == _DESCRIPTION_FORMAT
    assert design_info_from_json(as_json).column_names == design.column_names

    buffer = io.StringIO()
    save_design_info(design, buffer)
    buffer.seek(0)
    assert load_design_info(buffer).column_names == design.column_names

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "design.json")
        save_design_info(design, path)
        loaded = load_design_info(path)
    assert loaded.column_names == design.column_names

    # Re-exporting a rebuilt description is stable.
    rebuilt = design_info_from_json(as_json)
    assert design_info_to_json(rebuilt) == as_json


def test_lhs_and_rhs_design_infos_roundtrip():
    y, x = dmatrices(
        "y ~ x + center(x)",
        {"y": [1.0, 2.0, 3.0], "x": [4.0, 5.0, 6.0]},
    )
    for design in (y.design_info, x.design_info):
        rebuilt = _roundtrip(design)
        assert rebuilt.column_names == design.column_names


def test_description_is_readable_and_self_contained():
    def local_helper(values):
        return values

    data = {"x": [1.0, 2.0, 3.0, 4.0], "g": ["a", "b", "a", "b"]}
    formula = "Q('x') + C(g, Treatment('a')) + center(x)"
    design = dmatrix(formula, data).design_info
    text = design_info_to_json(design)

    # Plain JSON, with the actual formula fragments visible to a reader.
    parsed = json.loads(text)
    assert "center(x)" in text
    assert "C(g, Treatment('a'))" in text
    for factor in parsed["factors"]:
        for name in factor["env_names"]:
            assert name in {
                "Q",
                "C",
                "Treatment",
                "center",
                "standardize",
                "scale",
                "I",
                "ContrastMatrix",
                "Poly",
                "Sum",
                "Helmert",
                "Diff",
                "bs",
                "cr",
                "cc",
                "te",
            }

    # A plain function reference is refused rather than embedded by name.
    other = dmatrix("local_helper(x)", data).design_info
    pytest.raises(PatsyError, design_info_to_json, other)


def test_corrupt_descriptions_are_rejected():
    design = dmatrix("x + center(x)", {"x": [1.0, 2.0, 3.0]}).design_info
    good_text = design_info_to_json(design)

    with pytest.raises(PatsyError, match="invalid JSON"):
        design_info_from_json("not json at all")
    with pytest.raises(PatsyError, match="not a Patsy design description"):
        design_info_from_json('{"format": "something-else"}')

    parsed = json.loads(good_text)
    parsed["column_names"][0] = "WRONG"
    with pytest.raises(PatsyError, match="corrupt design description"):
        design_info_from_dict(parsed)

    with pytest.raises(PatsyError, match="expected a DesignInfo"):
        design_info_to_dict({"not": "a design info"})


def test_rebuild_in_a_separate_process():
    data = {
        "my x": [1.0, 2.0, 3.0, 4.0],
        "g": ["b", "a", "c", "a"],
        "z": [10.0, 20.0, 30.0, 40.0],
    }
    formula = (
        "Q('my x') + C(g, Treatment('b')) + center(z) + center(z):C(g)"
    )
    design = dmatrix(formula, data).design_info
    new_data = {
        "my x": [5.0, 6.0, 7.0],
        "g": ["c", "a", "b"],
        "z": [50.0, 60.0, 70.0],
    }
    expected = _matrix(design, new_data)

    with tempfile.TemporaryDirectory() as directory:
        description_path = os.path.join(directory, "design.json")
        data_path = os.path.join(directory, "new-data.json")
        output_path = os.path.join(directory, "output.json")
        save_design_info(design, description_path)
        with open(data_path, "w") as handle:
            json.dump(new_data, handle)

        worker = (
            "import json, numpy as np\n"
            "from patsy import load_design_info, build_design_matrices\n"
            "di = load_design_info(%r)\n"
            "data = json.load(open(%r))\n"
            "matrix = build_design_matrices([di], data)[0]\n"
            "json.dump({'columns': matrix.design_info.column_names,\n"
            "           'values': np.asarray(matrix).tolist()},\n"
            "          open(%r, 'w'))\n"
            % (description_path, data_path, output_path)
        )
        result = subprocess.run(
            [sys.executable, "-c", worker],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, result.stderr.decode()
        with open(output_path) as handle:
            output = json.load(handle)

    assert output["columns"] == design.column_names
    np.testing.assert_allclose(np.asarray(output["values"]), expected)
