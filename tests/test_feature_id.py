"""
Tests for stable, persistable feature ID allocation across publishing batches.
"""

import json
import tempfile
import unittest
from pathlib import Path

from mapbox_vector_tile import decode, encode
from mapbox_vector_tile.feature_id import (
    FeatureIdConflictError,
    FeatureIdEntry,
    FeatureIdError,
    FeatureIdMapping,
    FeatureIdRangeError,
    FeatureIdRangeExhaustedError,
    FeatureIdValueError,
)

GEOMETRY = "POINT (1 1)"


def feature(source=None, geometry=GEOMETRY, properties=None, fid=None):
    result = {"geometry": geometry, "properties": properties if properties is not None else {}}
    if source is not None:
        result["source"] = source
    if fid is not None:
        result["id"] = fid
    return result


def encode_layer(features, mapping, layer_name="water"):
    pbf = encode(
        [{"name": layer_name, "features": features}],
        default_options={"feature_id_mapping": mapping},
    )
    return decode(pbf)[layer_name]["features"]


def emitted_ids(features):
    return [item["id"] for item in features]


class PublishingScenarioTest(unittest.TestCase):
    def setUp(self):
        self.mapping = FeatureIdMapping("water", 1, 10)

    def test_two_string_keys_get_numeric_ids(self):
        features = encode_layer([feature("a"), feature("b")], self.mapping)
        self.assertEqual(emitted_ids(features), [1, 2])
        self.assertEqual(self.mapping.lookup(1), "a")
        self.assertEqual(self.mapping.lookup(2), "b")
        self.assertEqual(self.mapping.kind_for(1), "string")

    def test_absent_feature_keeps_its_id_for_later_batch(self):
        encode_layer([feature("a"), feature("b")], self.mapping)
        # "b" is temporarily absent; a new string key arrives next batch.
        features = encode_layer([feature("a"), feature("c")], self.mapping)

        self.assertEqual(emitted_ids(features), [1, 3])
        self.assertEqual(self.mapping.id_for("a"), 1)
        self.assertEqual(self.mapping.id_for("b"), 2)
        self.assertEqual(self.mapping.id_for("c"), 3)

        # When "b" returns, it still has its original ID.
        features = encode_layer([feature("b"), feature("c")], self.mapping)
        self.assertEqual(emitted_ids(features), [2, 3])

    def test_native_numeric_id_is_used_and_looked_up(self):
        features = encode_layer([feature(5), feature("later")], self.mapping)
        self.assertEqual(emitted_ids(features), [5, 1])
        self.assertEqual(self.mapping.lookup(5), 5)
        self.assertEqual(self.mapping.kind_for(5), "number")

    def test_string_that_looks_numeric_stays_distinct_from_native_id(self):
        features = encode_layer([feature("2")], self.mapping)
        # The string "2" receives the lowest free ID (1), not 2.
        self.assertEqual(emitted_ids(features), [1])
        self.assertEqual(self.mapping.id_for("2"), 1)
        self.assertEqual(self.mapping.lookup(1), "2")
        self.assertEqual(self.mapping.kind_for(1), "string")

        # Native 2 is still free and is reserved as its own source.
        features = encode_layer([feature(2)], self.mapping)
        self.assertEqual(emitted_ids(features), [2])
        self.assertEqual(self.mapping.lookup(2), 2)
        self.assertEqual(self.mapping.kind_for(2), "number")
        self.assertNotEqual(self.mapping.lookup(1), self.mapping.lookup(2))

    def test_input_reordering_produces_same_assignments(self):
        other = FeatureIdMapping("water", 1, 10)
        first = encode_layer([feature("a"), feature("b"), feature("c")], self.mapping)
        second = encode_layer([feature("c"), feature("a"), feature("b")], other)

        self.assertEqual(self.mapping.to_dict(), other.to_dict())
        self.assertEqual(
            dict(zip(["a", "b", "c"], emitted_ids(first))),
            dict(zip(["c", "a", "b"], emitted_ids(second))),
        )

    def test_mixed_native_and_string_reordering_matches(self):
        first = FeatureIdMapping("scope", 1, 10)
        second = FeatureIdMapping("scope", 1, 10)
        sources = ["alpha", 3, "beta", 7, "gamma"]
        first.allocate(sources)
        second.allocate(list(reversed(sources)))
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_existing_ids_do_not_move_when_batch_reordered(self):
        encode_layer([feature("a"), feature("b")], self.mapping)
        before = self.mapping.allocated

        encode_layer([feature("b"), feature("a"), feature("new")], self.mapping)
        after = self.mapping.allocated

        self.assertEqual(after["a"], before["a"])
        self.assertEqual(after["b"], before["b"])
        self.assertEqual(after["new"], 3)

    def test_persisted_mapping_round_trips_between_batches(self):
        encode_layer([feature("a"), feature("b"), feature("c")], self.mapping)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ids.json"
            self.mapping.save(path)
            restored = FeatureIdMapping.load(path)

        self.assertEqual(restored.to_dict(), self.mapping.to_dict())
        features = encode_layer([feature("b"), feature("d")], restored)
        self.assertEqual(emitted_ids(features), [2, 4])
        self.assertEqual(restored.lookup(2), "b")

    def test_conflict_names_both_sources(self):
        encode_layer([feature("a"), feature("b")], self.mapping)
        # Native 2 is the ID previously given to the string key "b".
        with self.assertRaises(FeatureIdConflictError) as raised:
            encode_layer([feature(2)], self.mapping)

        error = raised.exception
        self.assertEqual(error.scope, "water")
        self.assertEqual(error.numeric_id, 2)
        self.assertEqual(error.existing_source, "b")
        self.assertEqual(error.existing_kind, "string")
        self.assertEqual(error.requested_source, 2)
        self.assertEqual(error.requested_kind, "number")
        self.assertEqual(error.sources, ("b", 2))
        self.assertIn("'b'", str(error))
        self.assertIn("2", str(error))

    def test_conflict_does_not_borrow_absent_id_or_partially_update(self):
        encode_layer([feature("a"), feature("b"), feature("c")], self.mapping)
        snapshot = self.mapping.to_dict()

        # "b" is temporarily absent. The batch also carries fresh string keys;
        # the native 2 conflicts and none of the fresh keys may be committed.
        with self.assertRaises(FeatureIdConflictError):
            encode_layer([feature("new-1"), feature(2), feature("new-2")], self.mapping)

        self.assertEqual(self.mapping.to_dict(), snapshot)
        self.assertNotIn("new-1", self.mapping.allocated)
        self.assertNotIn("new-2", self.mapping.allocated)
        self.assertEqual(self.mapping.id_for("b"), 2)

        # The failed batch must not have consumed ID 2. A later, valid batch
        # still never assigns the absent ID 2 to anyone else.
        self.assertEqual(self.mapping.allocate(["late"]), {"late": 4})
        self.assertEqual(self.mapping.lookup(2), "b")

    def test_conflict_does_not_emit_a_partial_layer(self):
        encode_layer([feature("a"), feature("b")], self.mapping)
        with self.assertRaises(FeatureIdConflictError):
            encode(
                [{"name": "water", "features": [feature("new"), feature(2)]}],
                default_options={"feature_id_mapping": self.mapping},
            )
        self.assertEqual(self.mapping.allocated, {"a": 1, "b": 2})

    def test_conflict_in_a_later_layer_rolls_back_every_layer(self):
        water = FeatureIdMapping("water", 1, 10)
        land = FeatureIdMapping("land", 1, 10)
        encode_layer([feature("a"), feature("b")], land, layer_name="land")
        land_before = land.to_dict()

        source = [
            {"name": "water", "features": [feature("new-water")]},
            {"name": "land", "features": [feature("new-land"), feature(2)]},
        ]
        with self.assertRaises(FeatureIdConflictError):
            encode(
                source,
                per_layer_options={
                    "water": {"feature_id_mapping": water},
                    "land": {"feature_id_mapping": land},
                },
            )
        self.assertEqual(water.allocated, {})
        self.assertEqual(land.to_dict(), land_before)

    def test_a_second_scope_is_independent(self):
        other = FeatureIdMapping("land", 1, 10)
        encode_layer([feature("a"), feature("b")], self.mapping)
        encode_layer([feature("b")], other, layer_name="land")

        self.assertEqual(self.mapping.id_for("a"), 1)
        self.assertEqual(self.mapping.id_for("b"), 2)
        self.assertEqual(other.id_for("b"), 1)
        self.assertNotEqual(self.mapping.to_dict(), other.to_dict())

    def test_every_emitted_id_looks_up_back_to_its_source(self):
        sources = ["alpha", 3, "beta", 7, "gamma"]
        features = encode_layer([feature(source) for source in sources], self.mapping)

        expected = {"alpha": 1, 3: 3, "beta": 2, 7: 7, "gamma": 4}
        self.assertEqual(emitted_ids(features), [expected[source] for source in sources])
        for item in features:
            numeric_id = item["id"]
            entry = self.mapping.entry_for(numeric_id)
            self.assertEqual(entry.scope, "water")
            self.assertTrue(self.mapping.minimum <= numeric_id <= self.mapping.maximum)
            self.assertIn(entry.source, sources)
            self.assertEqual(
                self.mapping.resolve(numeric_id),
                (entry.source, entry.kind, "water", 1, 10),
            )


class MappingApiTest(unittest.TestCase):
    def test_new_string_keys_use_sorted_order(self):
        mapping = FeatureIdMapping("scope", 1, 10)
        self.assertEqual(mapping.allocate(["z", "a", "m"]), {"a": 1, "m": 2, "z": 3})

    def test_native_ids_are_not_reassigned_to_strings(self):
        mapping = FeatureIdMapping("scope", 1, 5)
        mapping.allocate([1, 3])
        self.assertEqual(mapping.allocate(["a", "b"]), {"a": 2, "b": 4})

    def test_native_id_outside_range_rejected(self):
        mapping = FeatureIdMapping("scope", 10, 20)
        with self.assertRaises(FeatureIdRangeError):
            mapping.allocate([5])
        self.assertEqual(mapping.allocated, {})

    def test_range_exhaustion_is_atomic(self):
        mapping = FeatureIdMapping("scope", 1, 2)
        mapping.allocate(["a"])
        with self.assertRaises(FeatureIdRangeExhaustedError):
            mapping.allocate(["b", "c"])
        self.assertEqual(mapping.allocated, {"a": 1})

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(FeatureIdValueError):
            FeatureIdMapping("", 1, 2)
        with self.assertRaises(FeatureIdRangeError):
            FeatureIdMapping("scope", 3, 2)
        with self.assertRaises(FeatureIdRangeError):
            FeatureIdMapping("scope", -1, 2)
        with self.assertRaises(FeatureIdValueError):
            FeatureIdMapping("scope", 1, 2).allocate([True])
        with self.assertRaises(FeatureIdValueError):
            FeatureIdMapping("scope", 1, 2).allocate([None])
        with self.assertRaises(FeatureIdValueError):
            FeatureIdMapping("scope", 1, 2).allocate([1.5])

    def test_duplicate_sources_in_one_batch_allocate_once(self):
        mapping = FeatureIdMapping("scope", 1, 5)
        self.assertEqual(mapping.allocate(["a", "a", 3, 3]), {"a": 1, 3: 3})
        self.assertEqual(mapping.allocated, {"a": 1, 3: 3})

    def test_transaction_rolls_back(self):
        mapping = FeatureIdMapping("scope", 1, 5)
        with self.assertRaises(RuntimeError), mapping.transaction():
            mapping.allocate(["a"])
            raise RuntimeError("emit failed")
        self.assertEqual(mapping.allocated, {})

    def test_saved_json_is_stable_and_portable(self):
        mapping = FeatureIdMapping("scope", 1, 5)
        mapping.allocate(["b", "a", 4])
        text = mapping.to_json()
        self.assertEqual(json.loads(text)["scope"], "scope")
        self.assertEqual(FeatureIdMapping.from_json(text).to_dict(), mapping.to_dict())
        self.assertEqual(FeatureIdMapping.from_dict(mapping.to_dict()).to_json(), text)

    def test_entries_can_restore_a_mapping(self):
        entries = [
            FeatureIdEntry(scope="scope", numeric_id=1, source="a", kind="string"),
            FeatureIdEntry(scope="scope", numeric_id=4, source=4, kind="number"),
        ]
        mapping = FeatureIdMapping("scope", 1, 5, entries=entries)
        self.assertEqual(mapping.lookup(1), "a")
        self.assertEqual(mapping.lookup(4), 4)

    def test_entry_kind_must_match_source(self):
        with self.assertRaises(FeatureIdValueError):
            FeatureIdMapping(
                "scope",
                1,
                5,
                entries=[FeatureIdEntry(scope="scope", numeric_id=1, source=1, kind="string")],
            )

    def test_lookup_unknown_id_raises(self):
        mapping = FeatureIdMapping("scope", 1, 5)
        with self.assertRaises(KeyError):
            mapping.lookup(3)
        with self.assertRaises(KeyError):
            mapping.id_for("missing")

    def test_invalid_mapping_json_rejected(self):
        with self.assertRaises(FeatureIdError):
            FeatureIdMapping.from_json("not json")
        with self.assertRaises(FeatureIdError):
            FeatureIdMapping.from_dict({"version": 99, "scope": "s", "range": [1, 2]})


class BackwardsCompatibilityTest(unittest.TestCase):
    def test_existing_numeric_id_without_mapping_is_unchanged(self):
        pbf = encode([{"name": "water", "features": [feature(fid=42)]}])
        self.assertEqual(decode(pbf)["water"]["features"][0]["id"], 42)

    def test_mapping_and_legacy_id_together_use_the_mapped_id(self):
        mapping = FeatureIdMapping("water", 1, 10)
        pbf = encode(
            [{"name": "water", "features": [feature("a", fid=99)]}],
            default_options={"feature_id_mapping": mapping},
        )
        self.assertEqual(decode(pbf)["water"]["features"][0]["id"], 1)
        self.assertEqual(mapping.allocated, {"a": 1})

    def test_features_without_ids_stay_unassigned(self):
        pbf = encode([{"name": "water", "features": [feature()]}])
        # MVT omits the optional id field; the decoder reports its default 0.
        self.assertEqual(decode(pbf)["water"]["features"][0]["id"], 0)

    def test_mapping_can_be_applied_to_only_one_layer(self):
        mapped = FeatureIdMapping("water", 1, 100)
        pbf = encode(
            [
                {"name": "water", "features": [feature("a", fid=9)]},
                {"name": "other", "features": [feature(fid=42), feature()]},
            ],
            per_layer_options={"water": {"feature_id_mapping": mapped}},
        )
        decoded = decode(pbf)
        self.assertEqual(emitted_ids(decoded["water"]["features"]), [1])
        self.assertEqual(emitted_ids(decoded["other"]["features"]), [42, 0])
        self.assertEqual(mapped.allocated, {"a": 1})

    def test_mapping_option_is_validated(self):
        with self.assertRaises(ValueError):
            encode(
                [{"name": "water", "features": [feature()]}],
                default_options={"feature_id_mapping": {"scope": "water"}},
            )
