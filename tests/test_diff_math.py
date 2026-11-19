from decimal import Decimal
import re

import pytest

from deepdiff import DeepDiff


class TestDiffMath:
    def test_math_diff(self):
        """Testing for the correct setting and usage of epsilon."""
        d1 = {"a": Decimal("3.5")}
        d2 = {"a": Decimal("4")}
        ep = 0.5
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("2.5")}
        d2 = {"a": Decimal("3")}
        ep = 0.5
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("2.5")}
        d2 = {"a": Decimal("2")}
        ep = 0.5
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("7.175")}
        d2 = {"a": Decimal("7.174")}
        ep = 0.1
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("7.175")}
        d2 = {"a": Decimal("7.174")}
        ep = 0.01
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("7.175")}
        d2 = {"a": Decimal("7.174")}
        ep = 0.001
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("7.175")}
        d2 = {"a": Decimal("7.174")}
        ep = 0.0001
        expected = {
            "values_changed": {
                "root['a']": {
                    "new_value": Decimal("7.174"),
                    "old_value": Decimal("7.175"),
                }
            }
        }
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == expected

    def test_math_diff_special_case(self):
        """Testing epsilon on a special Decimal case.
        
        Even though the Decimal looks different, math will evaluate it for us."""
        d1 = {"a": Decimal("9.709999618320632")}
        d2 = {"a": Decimal("9.710000038146973")}
        ep = 0.001
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("9.709999618320632")}
        d2 = {"a": Decimal("9.710000038146973")}
        ep = 0.0001
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("9.709999618320632")}
        d2 = {"a": Decimal("9.710000038146973")}
        ep = 0.00001
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("9.709999618320632")}
        d2 = {"a": Decimal("9.710000038146973")}
        ep = 0.000001
        res = DeepDiff(d1, d2, math_epsilon=ep)
        assert res == {}
        d1 = {"a": Decimal("9.709999618320632")}
        d2 = {"a": Decimal("9.710000038146973")}
        ep = 0.0000001
        res = DeepDiff(d1, d2, math_epsilon=ep)
        expected = {
            "values_changed": {
                "root['a']": {
                    "new_value": Decimal("9.710000038146973"),
                    "old_value": Decimal("9.709999618320632"),
                }
            }
        }
        assert res == expected

    def test_math_diff_ignore_order(self):
        """math_close will not work with ignore_order=true.
        
        Items are hashed if order is ignored, that will not work."""
        d1 = {"a": [Decimal("9.709999618320632"), Decimal("9.709999618320632")]}
        d2 = {"a": [Decimal("9.710000038146973"), Decimal("9.709999618320632")]}
        ep = 0.0001
        res = DeepDiff(d1, d2, ignore_order=False, math_epsilon=ep)
        assert res == {}

    def test_math_diff_ignore_order_warning(self, caplog):
        """math_close will not work with ignore_order=true.
        
        Items are hashed if order is ignored, that will not work."""
        d1 = {"a": [Decimal("9.709999618320632"), Decimal("9.709999618320632")]}
        d2 = {"a": [Decimal("9.710000038146973"), Decimal("9.709999618320632")]}
        ep = 0.0001
        res = DeepDiff(d1, d2, ignore_order=True, math_epsilon=ep)
        expected = {
            "iterable_item_added": {"root['a'][0]": Decimal("9.710000038146973")}
        }
        assert res == expected
        # assert "math_epsilon will be ignored." in caplog.text

    def test_ignore_numeric_type_changes_with_numeric_keys_and_no_significant_digits(self):
        """Test that ignore_numeric_type_changes works with numeric keys when significant_digits is None.
        
        This test covers the bug fix in _get_clean_to_keys_mapping where significant_digits=None
        caused a crash when number_to_string was called without the required parameter.
        """
        # Test case with numeric keys and ignore_numeric_type_changes=True, significant_digits=None
        d1 = {1: "value1", 2.5: "value2"}  
        d2 = {1.0: "value1", 2.5: "value2"}  # int vs float keys
        
        # This should not crash and should treat 1 and 1.0 as the same key
        result = DeepDiff(d1, d2, ignore_numeric_type_changes=True, significant_digits=None)
        assert result == {}


class TestMathEpsilonPerPath:
    """Per-path tolerances: configured paths use their own epsilon,
    everything else is still compared exactly."""

    def test_exact_path_tolerance(self):
        """A one-cent amount change is reported, a small temperature change is not,
        and an unconfigured field is compared exactly."""
        t1 = {"amount": Decimal("100.00"), "temperature": 20.0, "ratio": 0.5}
        t2 = {"amount": Decimal("100.01"), "temperature": 20.4, "ratio": 0.5}
        result = DeepDiff(t1, t2, math_epsilon_per_path={
            "root['amount']": 0.001,
            "root['temperature']": 0.5,
        })
        assert result == {
            "values_changed": {
                "root['amount']": {"new_value": Decimal("100.01"), "old_value": Decimal("100.00")}
            }
        }

    def test_unconfigured_path_is_exact(self):
        """Fields without a configured tolerance report even the tiniest change."""
        t1 = {"temperature": 20.0, "ratio": 0.5}
        t2 = {"temperature": 20.1, "ratio": 0.5000001}
        result = DeepDiff(t1, t2, math_epsilon_per_path={"root['temperature']": 0.5})
        assert result == {
            "values_changed": {
                "root['ratio']": {"new_value": 0.5000001, "old_value": 0.5}
            }
        }

    def test_glob_path_tolerance(self):
        t1 = {"readings": [{"temperature": 20.0}, {"temperature": 30.0}]}
        t2 = {"readings": [{"temperature": 20.3}, {"temperature": 30.6}]}
        result = DeepDiff(t1, t2, math_epsilon_per_path={
            "root['readings'][*]['temperature']": 0.5,
        })
        assert result == {
            "values_changed": {
                "root['readings'][1]['temperature']": {"new_value": 30.6, "old_value": 30.0}
            }
        }

    def test_regex_path_tolerance(self):
        t1 = {"temp_indoor": 20.0, "temp_outdoor": 5.0, "amount": 100.0}
        t2 = {"temp_indoor": 20.2, "temp_outdoor": 5.3, "amount": 100.01}
        result = DeepDiff(t1, t2, math_epsilon_per_path={
            re.compile(r"root\['temp_"): 0.5,
            "root['amount']": 0.001,
        })
        assert result == {
            "values_changed": {
                "root['amount']": {"new_value": 100.01, "old_value": 100.0}
            }
        }

    def test_per_path_overrides_global_math_epsilon(self):
        t1 = {"amount": 100.0, "temperature": 20.0}
        t2 = {"amount": 100.01, "temperature": 20.4}
        result = DeepDiff(t1, t2, math_epsilon=1.0, math_epsilon_per_path={
            "root['amount']": 0.001,
        })
        assert result == {
            "values_changed": {
                "root['amount']": {"new_value": 100.01, "old_value": 100.0}
            }
        }

    def test_unordered_devices_matched_by_id(self):
        """Devices are paired by id first, then per-path rules decide what changed."""
        t1 = {"devices": [
            {"id": "a", "amount": Decimal("10.00"), "temperature": 20.0},
            {"id": "b", "amount": Decimal("20.00"), "temperature": 25.0},
        ]}
        t2 = {"devices": [
            {"id": "b", "amount": Decimal("20.01"), "temperature": 25.4},
            {"id": "a", "amount": Decimal("10.00"), "temperature": 20.3},
        ]}
        result = DeepDiff(t1, t2,
                          iterable_match_keys={"root['devices']": "id"},
                          math_epsilon_per_path={
                              "root['devices'][*]['amount']": 0.001,
                              "root['devices'][*]['temperature']": 0.5,
                          })
        # Only device b's amount is off by more than its tolerance.
        assert result == {
            "values_changed": {
                "root['devices'][0]['amount']": {"new_value": Decimal("20.01"), "old_value": Decimal("20.00")}
            }
        }

    def test_invalid_config_raises(self):
        with pytest.raises(ValueError) as excinfo:
            DeepDiff(1, 2, math_epsilon_per_path={"root['a']": "not a number"})
        assert "math_epsilon_per_path values must be numbers" in str(excinfo.value)
        with pytest.raises(ValueError) as excinfo:
            DeepDiff(1, 2, math_epsilon_per_path={"['a'][*]": 0.1})
        assert "Wildcard paths must start with 'root'" in str(excinfo.value)
        with pytest.raises(ValueError) as excinfo:
            DeepDiff(1, 2, math_epsilon_per_path={42: 0.1})
        assert "math_epsilon_per_path keys must be path strings or compiled regexes" in str(excinfo.value)
