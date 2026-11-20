"""Incremental categorical-freeze probe against the *local* patsy source.

No patsy source file is modified: decision points are only wrapped at runtime
so we can record which chunk froze which pattern.

Four paths are kept distinguishable end to end:
  P1 first all-empty block is float64  -> factor frozen numeric too early
  P2 explicit C(..., levels=...)       -> first explicit categorical claim wins
  P3 prediction carrier declares a different (but known) level order
  P4 prediction carrier carries a genuinely unknown label
"""

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import patsy
from patsy import PatsyError, dmatrix
from patsy.build import guess_categorical as _orig_guess
from patsy.build import CategoricalSniffer, build_design_matrices
from patsy.highlevel import incr_dbuilder

from incremental_probe.upstream_schema import (
    CANON_LEVELS,
    TRAIN_COLUMNS,
    normalize_block as normalize_chunk,
)

CANON = CANON_LEVELS  # non-lexicographic training level order
SCHEMA_ORDER = TRAIN_COLUMNS  # original full-table training column order
FORMULA_NAT = "x + grp"
FORMULA_C = "x + C(grp, levels=['c', 'a', 'b'])"

decision_log = []
current = {"chunk": None}


# ---------------------------------------------------------------- instrumentation
def _instrument():
    def logged_guess(value):
        verdict = _orig_guess(value)
        dtype = getattr(value, "dtype", type(value).__name__)
        decision_log.append(
            ("guess_categorical", current["chunk"], str(dtype), verdict)
        )
        return verdict

    orig_sniff = CategoricalSniffer.sniff

    def logged_sniff(self, data):
        done = orig_sniff(self, data)
        dtype = getattr(data, "dtype", type(data).__name__)
        decision_log.append(
            (
                "sniff",
                current["chunk"],
                str(dtype),
                "done=%s levels=%r contrast=%r"
                % (done, self._levels, self._contrast),
            )
        )
        return done

    patsy.build.guess_categorical = logged_guess
    CategoricalSniffer.sniff = logged_sniff
    return orig_sniff


def _restore(orig_sniff):
    patsy.build.guess_categorical = _orig_guess
    CategoricalSniffer.sniff = orig_sniff


def make_iter_maker(pairs):
    """pairs: list of (chunk_id, DataFrame) tuples."""
    seen_passes = []

    def maker():
        seen = []
        seen_passes.append(seen)

        def gen():
            for chunk_id, chunk in pairs:
                current["chunk"] = chunk_id
                seen.append(chunk_id)
                yield chunk
            current["chunk"] = None

        return gen()

    maker.seen_passes = seen_passes
    return maker


# ----------------------------------------------------------------- chunk factories
def chunk0_float():
    # upstream pushed the all-empty string column onto a float dtype
    return pd.DataFrame(
        {"x": [0.1, 0.2], "grp": pd.Series([np.nan, np.nan])},
        columns=SCHEMA_ORDER,
    )


def chunk0_object():
    return pd.DataFrame(
        {"x": [0.1, 0.2], "grp": pd.Series([None, None], dtype=object)},
        columns=SCHEMA_ORDER,
    )


def chunk0_categorical():
    return pd.DataFrame(
        {
            "x": [0.1, 0.2],
            "grp": pd.Categorical([None, None], categories=list(CANON)),
        },
        columns=SCHEMA_ORDER,
    )


def chunk1():
    return pd.DataFrame(
        {
            "x": [1.1, 2.2, 3.3, 4.4],
            "grp": pd.Categorical(
                ["c", "a", "b", "a"], categories=list(CANON)
            ),
        },
        columns=SCHEMA_ORDER,
    )


def carrier_reorder():
    # only known values, but the carrier declares a different, lexicographic order
    return pd.DataFrame(
        {
            "x": [5.1, 6.2],
            "grp": pd.Categorical(["b", "a"], categories=["a", "b"]),
        },
        columns=SCHEMA_ORDER,
    )


def carrier_new_label():
    return pd.DataFrame(
        {"x": [7.1, 8.2], "grp": pd.Series(["a", "zzz_new"], dtype=object)},
        columns=SCHEMA_ORDER,
    )


def grp_factor_info(design_info):
    for factor, fi in design_info.factor_infos.items():
        if factor.code == "grp" or factor.code.startswith("C(grp"):
            return fi
    raise AssertionError(
        "grp factor not found: %s" % list(design_info.factor_infos)
    )


def run_builder(label, formula, pairs):
    decision_log.clear()
    maker = make_iter_maker(pairs)
    orig_sniff = _instrument()
    try:
        di = incr_dbuilder(formula, maker, eval_env=0)
    finally:
        _restore(orig_sniff)
    fi = grp_factor_info(di)
    print("  -- type-decision scan log --")
    for entry in decision_log:
        print("    ", entry)
    print("  -- iterator passes (memorize pass + type-examine pass) --")
    for i, seen in enumerate(maker.seen_passes):
        print("     pass %d visited chunks: %s" % (i, seen))
    print("  FROZEN grp factor : type=%s categories=%r" % (fi.type, fi.categories))
    print("  FROZEN columns    : %s" % (di.column_names,))
    return di, fi


def try_predict(label, di, data):
    try:
        (m,) = build_design_matrices([di], data)
    except PatsyError as exc:
        print("  %-12s REJECTED (PatsyError)   %s" % (label, exc.args[0]))
        return None
    except Exception as exc:  # noqa: BLE001 - diagnostic probe, keep scanning
        print("  %-12s REJECTED (%s)  %s"
              % (label, type(exc).__name__, exc))
        return None
    print("  %-12s OK\n%s" % (label, np.asarray(m)))
    return m


# ------------------------------------------------------- upstream normalization rule
# normalize_chunk is imported from upstream_schema.py (single source of truth).
# It keeps the original training column order, rejects unknown labels before
# pandas can silently coerce them to NaN, and always emits a declared
# Categorical with the canonical level order -- patsy itself is not touched.

print("=" * 78)
print("ENVIRONMENT")
print("  python", __import__("sys").version.split()[0],
      "| pandas", pd.__version__, "| numpy", np.__version__,
      "| patsy", patsy.__version__)
print("  patsy loaded from:", patsy.__file__)

# --------------------------------------------------------------- section 1: builds
print("=" * 78)
print("SECTION 1  incremental builder scans (2 formulas x 2 first-block dtypes)")

results = {}
for formula_name, formula in [("natural", FORMULA_NAT), ("explicit_C", FORMULA_C)]:
    for first_name, first in [
        ("float_first", chunk0_float()),
        ("object_first", chunk0_object()),
    ]:
        label = "%s/%s" % (formula_name, first_name)
        print("-" * 78)
        print("CONFIG", label, " formula=%r" % formula)
        print("  chunk0 grp dtype: %-8s | chunk1 grp dtype: %s categories=%s"
              % (first["grp"].dtype, chunk1()["grp"].dtype,
                 list(chunk1()["grp"].cat.categories)))
        di, fi = run_builder(label, formula, [("chunk0", first), ("chunk1", chunk1())])
        results[label] = (di, fi)

# ------------------------------------------- section 2: the reported symptom itself
print("=" * 78)
print("SECTION 2  replaying the later training chunk through each frozen builder")
for label, (di, fi) in results.items():
    print("-" * 78)
    print("BUILDER", label, "(grp frozen as %s)" % fi.type)
    try_predict("chunk1", di, chunk1())

print("-" * 78)
print("SECTION 2b  P1 numeric-freeze: intended check vs pandas-3 masked crash")
di_float, _ = results["natural/float_first"]
dict_carrier = {
    "x": np.array([5.1, 6.2]),
    "grp": np.array(["b", "a"], dtype=object),
}
print("  dict carrier (ndarray, no DataFrame wrapper):")
try_predict("ndarray", di_float, dict_carrier)
print("  same values as a DataFrame:")
try_predict("dataframe", di_float, pd.DataFrame(dict_carrier, columns=SCHEMA_ORDER))

# ------------------------------------- section 3: the two anomalous predict carriers
print("=" * 78)
print("SECTION 3  prediction carriers (reordered known values vs true new label)")
for label, (di, fi) in results.items():
    print("-" * 78)
    print("BUILDER", label, "(grp frozen as %s, categories=%r)"
          % (fi.type, fi.categories))
    try_predict("reorder", di, carrier_reorder())
    try_predict("new_label", di, carrier_new_label())

# ----------------- section 3b: first explicit categorical claim wins --------------
print("=" * 78)
print("SECTION 3b  conflicting explicit C(levels=...) claims: first one freezes")
from patsy.categorical import C, CategoricalSniffer, categorical_to_int
from patsy.missing import NAAction

# Emulate _examine_factor_types: on done=True the factor leaves examine_needed
# (patsy/build.py:496-497), so later chunks are never sniffed for it again.
sniffer = CategoricalSniffer(NAAction())
claim0 = C([None, None], levels=list(CANON))        # block 0 explicit claim
claim1 = C(["c", "a"], levels=["a", "b", "c"])     # block 1 conflicting claim
done0 = sniffer.sniff(claim0)
frozen = sniffer.levels_contrast()[0]
print("  block0 claim levels=%r -> sniff done=%s; factor leaves examine_needed"
      % (claim0.levels, done0))
print("  block1 claim levels=%r is never sniffed; frozen levels stay %r"
      % (claim1.levels, frozen))
try:
    categorical_to_int(claim1, CANON, NAAction())
except PatsyError as exc:
    print("  replay-time guard on the conflicting box ->", exc.args[0])

# --------------------------- section 4: full-table run is no guarantee for incr API
print("=" * 78)
print("SECTION 4  why a passing full-table run does not certify the incr pipeline")
naive_full = pd.concat([chunk0_float(), chunk1()], ignore_index=True)
print("  concat of float-empty + later strings -> grp dtype:",
      naive_full["grp"].dtype)
m_full = dmatrix(FORMULA_NAT, naive_full)
print("  one-shot dmatrix succeeds; columns:", m_full.design_info.column_names)
print("  but the incremental natural/float_first builder (SECTION 2) rejected the")
print("  same later chunk: type examination is per chunk, freezing happens on chunk0")

# ----------------------------- section 5: upstream fix (no patsy code touched) -----
print("=" * 78)
print("SECTION 5  upstream schema fix: grp is a declared Categorical on EVERY chunk")

print("  -- variant A: natural formula, categorical emitted from block 0 --")
di_a, fi_a = run_builder(
    "fixed/natural+cat_block0", FORMULA_NAT,
    [("chunk0", chunk0_categorical()), ("chunk1", chunk1())],
)

print("  -- variant B: natural formula, raw blocks passed through normalize_chunk --")
norm0 = normalize_chunk(chunk0_float())
print("  normalized chunk0 grp dtype:", norm0["grp"].dtype,
      "| categories:", list(norm0["grp"].cat.categories))
di_b, fi_b = run_builder(
    "fixed/natural+normalized", FORMULA_NAT,
    [("chunk0", norm0), ("chunk1", normalize_chunk(chunk1()))],
)
assert fi_b.type == "categorical" and fi_b.categories == CANON

m_chunk1 = try_predict("chunk1", di_b, normalize_chunk(chunk1()))
m_reorder = try_predict("reorder", di_b, normalize_chunk(carrier_reorder()))
print("  new-label carrier at the upstream gate:")
try:
    normalize_chunk(carrier_new_label())
except ValueError as exc:
    print("   ", exc)

# ---------------- section 6: equivalence with the ordered full-table baseline ----
print("=" * 78)
print("SECTION 6  fixed incremental matrices vs ordered full-table baseline")
base = dmatrix(FORMULA_NAT, chunk1())  # canonical order ('c','a','b') baseline
base_reorder = dmatrix(FORMULA_NAT, normalize_chunk(carrier_reorder()))
checks = [
    ("chunk1", m_chunk1, base),
    ("reorder", m_reorder, base_reorder),
]
all_ok = True
for name, incr_mat, base_mat in checks:
    same_cols = list(incr_mat.design_info.column_names) == list(
        base_mat.design_info.column_names
    )
    same_vals = np.allclose(np.asarray(incr_mat), np.asarray(base_mat))
    all_ok &= same_cols and same_vals
    print("  %-8s same column order: %s | same values: %s"
          % (name, same_cols, same_vals))
    print("   columns:", incr_mat.design_info.column_names)
print("  EQUIVALENCE:", "PASS" if all_ok else "FAIL")
