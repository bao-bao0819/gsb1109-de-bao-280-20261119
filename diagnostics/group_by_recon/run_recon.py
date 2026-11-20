"""Reproducible recon harness for the group_by / ignore_order ambiguity.

Run from the repository root:

    .venv/bin/python diagnostics/group_by_recon/run_recon.py

The script never modifies deepdiff; it temporarily wraps two internal methods
to capture evidence that the public DeepDiff result hides:

  * which concrete records survive grouping and enter comparison,
  * which candidate (t1, t2) record pairs are evaluated, their deep distance,
    and which pairs are actually formed,
  * the raw iterable_item_added / iterable_item_removed reports before
    TreeResult.mutual_add_removes_to_become_value_changes() merges same-path
    pairs into values_changed.
"""

import copy

from deepdiff import DeepDiff
from deepdiff.diff import DeepDiff as _DD

from snapshots import (
    T1,
    T2_ORIG,
    T2_SWAP,
    CONTROL_ID_CHANGE_T1,
    CONTROL_ID_CHANGE_T2,
    CONTROL_TINY_DESC_T1,
    CONTROL_TINY_DESC_T2,
    INNER_LIST_T1,
    INNER_LIST_T2,
    INTERSECTION_T1,
    INTERSECTION_T2,
    LOSSY_GROUP_BY,
    FAITHFUL_GROUP_BY,
    FAITHFUL_SORT_KEY,
    INNER_SORT_KEY,
    business_key,
)

LINE = "=" * 78


def heading(title):
    print("\n" + LINE)
    print(title)
    print(LINE)


def grouped_view(records, group_by, sort_key=None):
    """Return exactly what _group_iterable_to_dict feeds to the diff engine."""
    probe = DeepDiff([], [], group_by=group_by, group_by_sort_key=sort_key)
    probe.group_by_keys = set()
    return probe._group_iterable_to_dict(copy.deepcopy(records), group_by, "probe")


def traced_diff(t1, t2, **options):
    """Run a diff while capturing pairing evidence and raw report events."""
    evidence = {"pair_calls": [], "raw_reports": []}
    original_pairs = _DD._get_most_in_common_pairs_in_iterables
    original_report = _DD._report_result

    def pair_spy(self, hashes_added, hashes_removed,
                 t1_hashtable, t2_hashtable, parents_ids, _original_type=None):
        candidates = []
        for added_hash in hashes_added:
            for removed_hash in hashes_removed:
                distance = self._get_rough_distance_of_hashed_objs(
                    added_hash, removed_hash,
                    t2_hashtable[added_hash], t1_hashtable[removed_hash])
                candidates.append({
                    "distance": round(distance, 4),
                    "t1": t1_hashtable[removed_hash].item,
                    "t2": t2_hashtable[added_hash].item,
                })
        result = original_pairs(
            self, hashes_added, hashes_removed,
            t1_hashtable, t2_hashtable, parents_ids, _original_type)
        evidence["pair_calls"].append({
            "candidates": candidates,
            "formed_pairs": len(result) // 2,
        })
        return result

    def report_spy(self, report_type, level, local_tree=None):
        evidence["raw_reports"].append((report_type, level.path()))
        return original_report(self, report_type, level, local_tree=local_tree)

    _DD._get_most_in_common_pairs_in_iterables = pair_spy
    _DD._report_result = report_spy
    try:
        diff = DeepDiff(t1, t2, **options)
    finally:
        _DD._get_most_in_common_pairs_in_iterables = original_pairs
        _DD._report_result = original_report
    return diff, evidence


def stable_report(diff):
    """Report with paths sorted, neutralizing set-iteration order noise.

    TreeResult.mutual_add_removes_to_become_value_changes() builds
    mutual_paths with a plain set comprehension, whose iteration order varies
    with PYTHONHASHSEED. The categories and paths are stable; only their
    listing order flips, so we sort for reproducible comparison.
    """
    return {key: sorted(value) for key, value in diff.items()}


def print_report(diff):
    print("  final report:", stable_report(diff) or "{} (empty)")


def stage_a_lossy_overwrite():
    heading("STAGE A | lossy grouping: group_by='id' only, ignore_order=True")
    print("Business key is (supplier, id), but grouping uses id alone.")
    print("Source t1:", T1)
    print("Grouped t1 (id -> single row):", grouped_view(T1, LOSSY_GROUP_BY))
    for label, t2 in (("SWAP import order", T2_SWAP), ("ORIG import order", T2_ORIG)):
        print("\n  ---", label)
        print("  source t2:", t2)
        print("  grouped t2 (id -> single row):", grouped_view(t2, LOSSY_GROUP_BY))
        for cutoff in (1.0, 0.3, 0.05, 0.0):
            diff, _ = traced_diff(
                T1, t2, group_by=LOSSY_GROUP_BY,
                ignore_order=True, cutoff_distance_for_pairs=cutoff)
            print(f"  cutoff_distance_for_pairs={cutoff:<4} ->",
                  stable_report(diff) or "{}")
    print("\n  Observation: the A-7 record never reaches pairing under either")
    print("  order. cutoff_distance_for_pairs has no effect because the grouped")
    print("  structure is a dict keyed by id, aligned directly by key.")


def stage_b_faithful_pairing():
    heading("STAGE B | faithful grouping: group_by=(supplier,id), all rows retained")
    print("Grouped t1:", grouped_view(T1, FAITHFUL_GROUP_BY, FAITHFUL_SORT_KEY))
    print("Grouped t2:", grouped_view(T2_SWAP, FAITHFUL_GROUP_BY, FAITHFUL_SORT_KEY))
    for cutoff in (1.0, 0.3, 0.05):
        diff, evidence = traced_diff(
            T1, T2_SWAP, group_by=FAITHFUL_GROUP_BY,
            group_by_sort_key=FAITHFUL_SORT_KEY,
            ignore_order=True, cutoff_distance_for_pairs=cutoff)
        print(f"\n  cutoff_distance_for_pairs={cutoff}")
        for call in evidence["pair_calls"]:
            for candidate in call["candidates"]:
                print("    candidate distance", candidate["distance"],
                      "| t1", candidate["t1"], "| t2", candidate["t2"])
            print("    pairs formed:", call["formed_pairs"])
        print_report(diff)
    print("\n  Observation: both A-7 rows stay in the candidate set at every")
    print("  cutoff. At 0.05 the distance (0.0714) exceeds the threshold, the")
    print("  pair is not formed, yet no record was overwritten: this is the")
    print("  'still a candidate, rejected by distance' path, distinct from A.")


def stage_c_controls():
    heading("STAGE C | controls: identity change vs near-identical description")
    diff_id, ev_id = traced_diff(
        CONTROL_ID_CHANGE_T1, CONTROL_ID_CHANGE_T2, ignore_order=True)
    diff_desc, ev_desc = traced_diff(
        CONTROL_TINY_DESC_T1, CONTROL_TINY_DESC_T2, ignore_order=True)
    print("  id 8 -> 9, description identical:")
    for call in ev_id["pair_calls"]:
        for candidate in call["candidates"]:
            print("    candidate distance", candidate["distance"],
                  "| pairs formed", call["formed_pairs"])
    print_report(diff_id)
    print("  id unchanged, one word added to description:")
    for call in ev_desc["pair_calls"]:
        for candidate in call["candidates"]:
            print("    candidate distance", candidate["distance"],
                  "| pairs formed", call["formed_pairs"])
    print_report(diff_desc)

    multi_t1 = T1
    multi_t2 = [
        {"supplier": "A", "id": 7, "desc": "bolt M8 short"},
        {"supplier": "B", "id": 7, "desc": "gasket blue"},
        {"supplier": "A", "id": 9, "desc": "washer flat 10mm zinc"},
    ]
    diff_multi, _ = traced_diff(
        multi_t1, multi_t2, group_by=FAITHFUL_GROUP_BY,
        group_by_sort_key=FAITHFUL_SORT_KEY, ignore_order=True)
    print("\n  Same id change embedded among several business groups:")
    print_report(diff_multi)
    print("  Observation: a changed business key surfaces as dictionary")
    print("  add/remove of the group itself, while a description edit stays a")
    print("  values_changed inside the surviving group.")


def stage_d_add_remove_merge_and_short_circuit():
    heading("STAGE D | add/remove reporting, path merge, and intersection gate")
    diff, evidence = traced_diff(
        INNER_LIST_T1, INNER_LIST_T2, group_by=FAITHFUL_GROUP_BY,
        group_by_sort_key=INNER_SORT_KEY,
        ignore_order=True, cutoff_distance_for_pairs=0.05)
    print("  Inner list, heavily rewritten row, tight cutoff (0.05):")
    print("  raw reports before merge:", evidence["raw_reports"])
    print_report(diff)
    print("  -> same-path add+remove is converted to values_changed by")
    print("     TreeResult.mutual_add_removes_to_become_value_changes().")

    diff_loose, _ = traced_diff(
        INTERSECTION_T1, INTERSECTION_T2,
        ignore_order=True, cutoff_intersection_for_pairs=0.7)
    diff_gated, _ = traced_diff(
        INTERSECTION_T1, INTERSECTION_T2,
        ignore_order=True, cutoff_intersection_for_pairs=0.5)
    print("\n  Mostly-changed list, default intersection cutoff 0.7:")
    print_report(diff_loose)
    print("  Same data, intersection cutoff lowered to 0.5 (pairing skipped):")
    print_report(diff_gated)
    print("  -> distinct index paths keep iterable_item_added/removed visible;")
    print("     the gate decides whether pairing runs at all.")


def main():
    stage_a_lossy_overwrite()
    stage_b_faithful_pairing()
    stage_c_controls()
    stage_d_add_remove_merge_and_short_circuit()
    heading("END | fixed snapshots only; no algorithm or threshold was changed")


if __name__ == "__main__":
    main()
