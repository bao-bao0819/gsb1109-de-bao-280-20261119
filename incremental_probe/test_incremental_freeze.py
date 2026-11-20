"""Repeatable evidence for the incremental categorical-freeze diagnosis.

Run from the repository root so the *local* patsy source is imported:
    python3 -m pytest incremental_probe/ -v
"""

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from patsy import PatsyError, dmatrix  # noqa: E402
from patsy.build import build_design_matrices  # noqa: E402
from patsy.categorical import C, CategoricalSniffer, categorical_to_int  # noqa: E402
from patsy.highlevel import incr_dbuilder  # noqa: E402
from patsy.missing import NAAction  # noqa: E402

from incremental_probe.upstream_schema import (  # noqa: E402
    CANON_LEVELS,
    SCHEMA,
    SchemaError,
    normalize_block,
)

FORMULA_NAT = "x + grp"
FORMULA_C = "x + C(grp, levels=['c', 'a', 'b'])"


# ----------------------------------------------------------------- test data
def block0_float():
    return pd.DataFrame(
        {"x": [0.1, 0.2], "grp": pd.Series([np.nan, np.nan])},
        columns=SCHEMA,
    )


def block0_object():
    return pd.DataFrame(
        {"x": [0.1, 0.2], "grp": pd.Series([None, None], dtype=object)},
        columns=SCHEMA,
    )


def block1():
    return pd.DataFrame(
        {
            "x": [1.1, 2.2, 3.3, 4.4],
            "grp": pd.Categorical(["c", "a", "b", "a"], categories=list(CANON_LEVELS)),
        },
        columns=SCHEMA,
    )


def carrier_reordered():
    return pd.DataFrame(
        {
            "x": [5.1, 6.2],
            "grp": pd.Categorical(["b", "a"], categories=["a", "b"]),
        },
        columns=SCHEMA,
    )


def carrier_unknown_label():
    return pd.DataFrame(
        {"x": [7.1, 8.2], "grp": pd.Series(["a", "zzz_new"], dtype=object)},
        columns=SCHEMA,
    )


def build(formula, blocks):
    def maker():
        for block in blocks:
            yield block

    return incr_dbuilder(formula, maker, eval_env=0)


def grp_info(design_info):
    return [
        fi
        for fi in design_info.factor_infos.values()
        if fi.factor.code == "grp" or fi.factor.code.startswith("C(grp")
    ][0]


# ------------------------------------------------------------- P1: dtype freeze
def test_p1_float_first_block_freezes_factor_as_numeric():
    design_info = build(FORMULA_NAT, [block0_float(), block1()])
    info = grp_info(design_info)
    assert info.type == "numerical"
    assert design_info.column_names == ["Intercept", "x", "grp"]

    # intended check, visible when the carrier is not a pandas DataFrame
    dict_carrier = {
        "x": np.array([5.1, 6.2]),
        "grp": np.array(["b", "a"], dtype=object),
    }
    with pytest.raises(PatsyError, match="non-numeric data of type 'object'"):
        build_design_matrices([design_info], dict_carrier)

    # on pandas 3 the same numeric check is masked because the error message
    # references DataFrame.dtype, which no longer exists
    with pytest.raises(Exception) as excinfo:
        build_design_matrices([design_info], carrier_reordered())
    if int(pd.__version__.split(".")[0]) >= 3:
        assert isinstance(excinfo.value, AttributeError)
    else:
        assert isinstance(excinfo.value, PatsyError)


def test_p1_object_first_block_keeps_factor_categorical():
    design_info = build(FORMULA_NAT, [block0_object(), block1()])
    info = grp_info(design_info)
    assert info.type == "categorical"
    assert info.categories == ("c", "a", "b")
    assert design_info.column_names == ["Intercept", "grp[T.a]", "grp[T.b]", "x"]


# ------------------------------------------------- P2: first explicit claim wins
def test_p2_explicit_C_overrides_float_first_block():
    for first in (block0_float(), block0_object()):
        design_info = build(FORMULA_C, [first, block1()])
        info = grp_info(design_info)
        assert info.type == "categorical"
        assert info.categories == CANON_LEVELS

    sniffer = CategoricalSniffer(NAAction())
    first_claim = C([None, None], levels=list(CANON_LEVELS))
    conflicting = C(["c", "a"], levels=["a", "b", "c"])
    assert sniffer.sniff(first_claim) is True
    assert sniffer.levels_contrast()[0] == CANON_LEVELS
    with pytest.raises(PatsyError, match="mismatching levels"):
        categorical_to_int(conflicting, CANON_LEVELS, NAAction())


# ------------------------------------------ P3/P4: the two anomalous carriers
def test_p3_reordered_carrier_levels_mismatch_but_known_values():
    info_natural = build(FORMULA_NAT, [block0_object(), block1()])
    with pytest.raises(PatsyError, match="mismatching levels"):
        build_design_matrices([info_natural], carrier_reordered())

    info_explicit = build(FORMULA_C, [block0_float(), block1()])
    matrix = build_design_matrices([info_explicit], carrier_reordered())[0]
    # C() re-boxes on evaluation, so values are remapped by label, not codes
    assert np.asarray(matrix).tolist() == [[1.0, 0.0, 1.0, 5.1], [1.0, 1.0, 0.0, 6.2]]


def test_p4_genuinely_unknown_label_is_always_rejected():
    for formula, first in [
        (FORMULA_NAT, block0_object()),
        (FORMULA_C, block0_float()),
    ]:
        design_info = build(formula, [first, block1()])
        with pytest.raises(PatsyError, match="does not match any of the expected levels"):
            build_design_matrices([design_info], carrier_unknown_label())


# --------------------------------------- full-table success is no incr guarantee
def test_full_table_pass_does_not_certify_incremental_pipeline():
    full = pd.concat([block0_float(), block1()], ignore_index=True)
    assert full["grp"].dtype == object  # concat healed the float column
    one_shot = dmatrix(FORMULA_NAT, full)
    assert one_shot.design_info.column_names == [
        "Intercept", "grp[T.b]", "grp[T.c]", "x",
    ]
    # same data, incremental API: factor was already frozen numeric on block 0
    with pytest.raises(Exception):
        build_design_matrices(
            [build(FORMULA_NAT, [block0_float(), block1()])], block1()
        )


# ------------------------------------------------------------- upstream fix
def test_upstream_gate_normalizes_every_block_and_matches_baseline():
    fixed_blocks = [normalize_block(block0_float()), normalize_block(block1())]
    assert str(fixed_blocks[0]["grp"].dtype) == "category"
    assert tuple(fixed_blocks[0]["grp"].cat.categories) == CANON_LEVELS

    design_info = build(FORMULA_NAT, fixed_blocks)
    assert grp_info(design_info).categories == CANON_LEVELS

    baseline_train = dmatrix(FORMULA_NAT, block1())
    incr_train = build_design_matrices([design_info], normalize_block(block1()))[0]
    assert incr_train.design_info.column_names == baseline_train.design_info.column_names
    assert np.allclose(np.asarray(incr_train), np.asarray(baseline_train))

    baseline_predict = dmatrix(FORMULA_NAT, normalize_block(carrier_reordered()))
    incr_predict = build_design_matrices(
        [design_info], normalize_block(carrier_reordered())
    )[0]
    assert incr_predict.design_info.column_names == baseline_predict.design_info.column_names
    assert np.allclose(np.asarray(incr_predict), np.asarray(baseline_predict))

    with pytest.raises(SchemaError, match="zzz_new"):
        normalize_block(carrier_unknown_label())
