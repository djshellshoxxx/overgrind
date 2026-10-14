import json
import tempfile
import unittest
from pathlib import Path
from random import Random

from overgrind.models import AppSpec, ControlSpec
from overgrind.planning import generate_cases
from overgrind.sampling import sample_value
from overgrind.runner import execute


def application():
    return AppSpec.from_dict({
        "title_regex": "Test App",
        "features": [
            {"id": "filter", "selector": {"auto_id": "filter"}},
            {"id": "delay", "selector": {"auto_id": "delay"}},
            {"id": "reverb", "selector": {"auto_id": "reverb"}},
        ],
        "controls": [
            {"id": "gain", "kind": "float", "minimum": -12, "maximum": 12,
             "selector": {"auto_id": "gain"}},
            {"id": "steps", "kind": "integer", "minimum": 1, "maximum": 16,
             "selector": {"auto_id": "steps"}},
            {"id": "enabled", "kind": "boolean", "selector": {"auto_id": "enabled"}},
            {"id": "mode", "kind": "choice", "options": ["clean", "dirty"],
             "selector": {"auto_id": "mode"}},
            {"id": "label", "kind": "text", "max_length": 8,
             "selector": {"auto_id": "label"}},
        ]
    })


class PlannerTests(unittest.TestCase):
    def test_every_ordered_pair_including_self_is_covered(self):
        cases = list(generate_cases(application(), samples=2, seed=41))
        self.assertEqual(len(cases), 18)
        self.assertEqual(
            {(c.first, c.second) for c in cases},
            {(a, b) for a in ("filter", "delay", "reverb")
             for b in ("filter", "delay", "reverb")}
        )
        self.assertEqual(set({(c.first, c.second): sum(
            1 for item in cases if (item.first, item.second) == (c.first, c.second)
        ) for c in cases}.values()), {2})

    def test_same_seed_is_replayable_and_pair_order_is_randomized(self):
        first = list(generate_cases(application(), samples=4, seed=91))
        again = list(generate_cases(application(), samples=4, seed=91))
        other = list(generate_cases(application(), samples=4, seed=92))
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertEqual(sorted((c.first, c.second) for c in first),
                         sorted((c.first, c.second) for c in other))

    def test_all_controls_receive_legal_values(self):
        spec = application()
        for case in generate_cases(spec, samples=20, seed=7):
            self.assertEqual(set(case.values), {control.id for control in spec.controls})
            self.assertTrue(-12 <= case.values["gain"] <= 12)
            self.assertTrue(1 <= case.values["steps"] <= 16)
            self.assertIs(type(case.values["steps"]), int)
            self.assertIs(type(case.values["enabled"]), bool)
            self.assertIn(case.values["mode"], ["clean", "dirty"])
            self.assertLessEqual(len(case.values["label"]), 8)


class ValidationTests(unittest.TestCase):
    def test_rejects_unbounded_float_or_text(self):
        with self.assertRaises(ValueError):
            ControlSpec.from_dict({"id": "bad", "kind": "float",
                                   "selector": {"auto_id": "bad"}})
        with self.assertRaises(ValueError):
            ControlSpec.from_dict({"id": "bad", "kind": "text",
                                   "selector": {"auto_id": "bad"}})

    def test_rejects_bad_range_and_empty_choices(self):
        with self.assertRaises(ValueError):
            ControlSpec.from_dict({"id": "bad", "kind": "integer", "minimum": 5,
                                   "maximum": 3, "selector": {"auto_id": "bad"}})
        with self.assertRaises(ValueError):
            ControlSpec.from_dict({"id": "bad", "kind": "choice", "options": [],
                                   "selector": {"auto_id": "bad"}})

    def test_rejects_duplicate_ids(self):
        raw = {
            "title_regex": "App",
            "features": [{"id": "a", "selector": {"auto_id": "a"}},
                         {"id": "a", "selector": {"auto_id": "b"}}],
            "controls": []
        }
        with self.assertRaises(ValueError):
            AppSpec.from_dict(raw)

    def test_rejects_missing_selector(self):
        with self.assertRaises(ValueError):
            ControlSpec.from_dict({"id": "gain", "kind": "boolean"})


class FeatureModeTests(unittest.TestCase):
    def test_enable_mode_is_supported_for_checkbox_features(self):
        spec = AppSpec.from_dict({
            "title_regex": "Test",
            "features": [{"id": "x", "mode": "enable",
                          "selector": {"auto_id": "toggle"}}],
            "controls": []
        })
        self.assertEqual(spec.features[0].mode, "enable")

    def test_unknown_feature_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            AppSpec.from_dict({
                "title_regex": "Test",
                "features": [{"id": "x", "mode": "unknown",
                              "selector": {"auto_id": "toggle"}}],
                "controls": []
            })


class SamplingTests(unittest.TestCase):
    def test_large_float_range_never_emits_nonfinite_values(self):
        import math
        control = ControlSpec.from_dict({
            "id": "x", "kind": "float", "minimum": -1e308,
            "maximum": 1e308, "selector": {"auto_id": "x"}
        })
        for i in range(100):
            value = sample_value(control, Random(i))
            self.assertTrue(math.isfinite(value))
            self.assertLessEqual(abs(value), 1e308)

    def test_bounded_integer_single_point(self):
        control = ControlSpec.from_dict({"id": "x", "kind": "integer",
                                         "minimum": 9, "maximum": 9,
                                         "selector": {"auto_id": "x"}})
        self.assertEqual(sample_value(control, Random(5)), 9)

    def test_numerical_boundary_sampling_visits_extremes(self):
        c = ControlSpec.from_dict({"id": "x", "kind": "float", "minimum": -2,
                                   "maximum": 2, "selector": {"auto_id": "x"}})
        seen = {sample_value(c, Random(i)) for i in range(150)}
        self.assertIn(-2, seen)
        self.assertIn(2, seen)
        self.assertIn(0, seen)


class FakeAdapter:
    def __init__(self, fail_on=None):
        self.sets = []
        self.actions = []
        self.fail_on = fail_on
        self.preparations = 0

    def prepare_case(self, features):
        self.preparations += 1

    def set_control(self, control, value):
        self.sets.append((control.id, value))
        if self.fail_on == control.id:
            raise RuntimeError("cannot set control")

    def activate_feature(self, feature):
        self.actions.append(feature.id)

    def healthy(self):
        return True


class RunnerTests(unittest.TestCase):
    def test_run_records_every_case_and_replays_values(self):
        adapter = FakeAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "out.jsonl"
            counts = execute(application(), adapter, report, samples=2, seed=17)
            rows = [json.loads(line) for line in report.read_text().splitlines()]
        self.assertEqual(counts, {"passed": 18, "failed": 0})
        self.assertEqual(len(rows), 18)
        self.assertEqual([row["index"] for row in rows], list(range(18)))
        self.assertEqual(adapter.actions, [action for row in rows
                         for action in row["pair"]])
        self.assertEqual(len(adapter.sets), 18 * 5)
        self.assertEqual(adapter.preparations, 18)
        self.assertTrue(all(row["status"] == "passed" and row["seed"] == 17
                            for row in rows))

    def test_failures_are_reported_and_do_not_hide_later_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            counts = execute(application(), FakeAdapter(fail_on="gain"),
                             Path(tmp) / "out.jsonl", samples=1, seed=1)
        self.assertEqual(counts, {"passed": 0, "failed": 9})

    def test_stop_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl"
            counts = execute(application(), FakeAdapter(fail_on="gain"),
                             path, samples=2, seed=1, stop_on_failure=True)
            self.assertEqual(counts, {"passed": 0, "failed": 1})
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_feature_scoped_controls_are_only_written_when_available(self):
        spec = AppSpec.from_dict({
            "title_regex": "Test",
            "features": [
                {"id": "a", "selector": {"auto_id": "a"}},
                {"id": "b", "selector": {"auto_id": "b"}}
            ],
            "controls": [
                {"id": "global", "kind": "boolean",
                 "selector": {"auto_id": "global"}},
                {"id": "a_knob", "kind": "boolean", "requires_features": ["a"],
                 "selector": {"auto_id": "a_knob"}},
                {"id": "both", "kind": "boolean", "requires_features": ["a", "b"],
                 "selector": {"auto_id": "both"}}
            ]
        })
        adapter = FakeAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scoped.jsonl"
            counts = execute(spec, adapter, path, samples=1, seed=7)
            rows = [json.loads(x) for x in path.read_text().splitlines()]
        self.assertEqual(counts, {"passed": 4, "failed": 0})
        self.assertEqual(sum(key == "global" for key, _ in adapter.sets), 4)
        self.assertEqual(sum(key == "a_knob" for key, _ in adapter.sets), 3)
        self.assertEqual(sum(key == "both" for key, _ in adapter.sets), 2)
        self.assertTrue(all("global" in row["applied_controls"] for row in rows))

    def test_rejects_unknown_control_feature_dependency(self):
        with self.assertRaises(ValueError):
            AppSpec.from_dict({
                "title_regex": "Test",
                "features": [{"id": "a", "selector": {"auto_id": "a"}}],
                "controls": [
                    {"id": "x", "kind": "boolean", "requires_features": ["missing"],
                     "selector": {"auto_id": "x"}}
                ]
            })

    def test_replay_single_case_preserves_original_index_and_inputs(self):
        app = application()
        plan = list(generate_cases(app, samples=3, seed=28))
        target = plan[10]
        adapter = FakeAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "single.jsonl"
            counts = execute(app, adapter, path, samples=3, seed=28, only_index=10)
            row = json.loads(path.read_text())
        self.assertEqual(counts, {"passed": 1, "failed": 0})
        self.assertEqual(row["index"], 10)
        self.assertEqual(row["values"], target.values)
        self.assertEqual(row["pair"], [target.first, target.second])
        self.assertEqual(len(adapter.actions), 2)

    def test_invalid_replay_index_rejected_without_creating_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "single.jsonl"
            with self.assertRaises(ValueError):
                execute(application(), FakeAdapter(), path,
                        samples=1, seed=1, only_index=99)
            self.assertFalse(path.exists())

    def test_zero_and_negative_samples_are_invalid(self):
        with self.assertRaises(ValueError):
            list(generate_cases(application(), samples=0, seed=1))
        with self.assertRaises(ValueError):
            list(generate_cases(application(), samples=-1, seed=1))


if __name__ == "__main__":
    unittest.main()
