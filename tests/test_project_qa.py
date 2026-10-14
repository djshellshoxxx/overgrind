"""QA-BACKEND regression tests: project / domain / estimate / danger / validation gaps.

Every test here was written first against a real defect found in the v0.2 audit
(see docs/audit/feature-audit-backend.md, section "Code audit").
"""
import json
import math
import os
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from overgrind import domain as D
from overgrind import project as P
from overgrind import scheduler
from overgrind.types import (
    ConstraintDef, ControlDef, Domain, FeatureDef, Project, RunConfig, Selector, TargetRef,
)


def feat(i, **kw):
    return FeatureDef(id=f"f{i}", selector=Selector(auto_id=f"f{i}"), **kw)


def proj(features=None, controls=(), constraints=(), run=None, target=None):
    return Project("qa", target or TargetRef(kind="window", title_regex="App"),
                   tuple(features if features is not None else [feat(0), feat(1)]),
                   tuple(controls), tuple(constraints), run or RunConfig())


def probs(p):
    return P.validate(p)


class EstimateOverflow(unittest.TestCase):
    def test_huge_spaces_are_formatted_not_overflowed(self):
        ctrls = [ControlDef(f"c{i}", "integer", Selector(auto_id=f"c{i}"), Domain("integer", 0, 10 ** 6))
                 for i in range(100)]
        many = [feat(i) for i in range(2000)]
        for p in (proj(many, ctrls, [ConstraintDef("mutex", features=("f0", "f1"))]),
                  proj(many[:3], ctrls)):
            est = scheduler.estimate(p)           # used to raise OverflowError (CLI exit 2, GUI crash)
            self.assertIsInstance(est["text"], str)
            self.assertTrue(est["text"])
            self.assertIsNotNone(est["valid_states"])
        est = scheduler.estimate(proj(many[:3], ctrls))
        self.assertGreater(est["cartesian"], 10 ** 600)


class DomainExtremes(unittest.TestCase):
    CASES = [
        Domain("float", 0, 1e308, 5e-324),
        Domain("float", -1e308, -100, 0.1),
        Domain("float", -10 ** 30, 0.1, 0.1),               # int bound beyond 2**53 on a float domain
        Domain("float", -10 ** 30, -10 ** 30, 1),
        Domain("float", -1e308, 1.7976931348623157e308, 1e-9),
        Domain("integer", -10 ** 30, 10 ** 30, 10 ** 20),
        Domain("float", 5e-324, 1e15, 5e-324),
    ]

    def test_no_exceptions_and_samples_are_members(self):
        rng = random.Random(3)
        for d in self.CASES:
            for _ in range(20):
                v = D.sample(d, rng)
                self.assertTrue(D.contains(d, v, strict_step=False), (d, v))
            for b in D.buckets(d, 3):
                v = D.sample_bucket(d, b, rng)
                self.assertTrue(D.contains(d, v, strict_step=False), (d, b, v))
            D.boundary_values(d)
            D.invalid_values(d, rng)
            D.count(d)
            for v in (float("nan"), float("inf"), -float("inf"), 0, 1e300, -1e300, 10 ** 40):
                D.clamp(d, v)
                D.contains(d, v)


class BomAndEncoding(unittest.TestCase):
    def test_load_accepts_utf8_bom(self):
        d = tempfile.mkdtemp()
        p = proj()
        path = Path(d) / "p.json"
        P.save(p, path)
        raw = path.read_bytes()
        path.write_bytes(b"\xef\xbb\xbf" + raw)          # what Windows Notepad writes
        self.assertEqual(P.load(path).features, p.features)

    def test_migrate_cli_reads_bom_v1(self):
        from overgrind import cli2
        d = Path(tempfile.mkdtemp())
        v1 = {"title_regex": "App", "features": [{"id": "a", "selector": {"auto_id": "a"}}], "controls": []}
        (d / "v1.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(v1).encode())
        self.assertEqual(cli2.main(["migrate", str(d / "v1.json"), str(d / "v2.json")]), 0)


class RunConfigValidation(unittest.TestCase):
    def err(self, **run):
        return [m for m in probs(proj(run=RunConfig(**run))) if not m.startswith("warning:")]

    def test_defaults_are_clean_and_confirm_runs_default_is_three(self):
        self.assertEqual(RunConfig().confirm_runs, 3)
        self.assertEqual(self.err(), [])

    def test_bad_values_are_rejected(self):
        bad = [dict(strategy="nope"), dict(confirm_runs=1), dict(confirm_runs=True),
               dict(op_timeout_ms=0), dict(case_timeout_ms=-1), dict(hang_threshold_ms=0),
               dict(ring_size=0), dict(reset_interval=0), dict(settle_ms=-5),
               dict(seed=True), dict(seed=1.5), dict(seed=float("nan")),
               dict(reset_policy="sometimes"), dict(op_timeout_ms=float("nan")),
               dict(strategy="twise", strategy_options={"t": 9}),
               dict(strategy="twise", strategy_options={"t": 0}),
               dict(strategy_options={"buckets": 10 ** 9}),
               dict(strategy_options={"buckets": 0}),
               dict(strategy_options={"tuple_cap": -1}),
               dict(strategy_options={"t": True}),
               ]
        for run in bad:
            self.assertTrue(self.err(**run), f"accepted {run}")

    def test_good_values_stay_valid(self):
        for run in (dict(strategy="twise", strategy_options={"t": 3, "buckets": 5}),
                    dict(strategy="adaptive", strategy_options={"explore": 0.2}),
                    dict(confirm_runs=2), dict(seed=0), dict(seed=-7), dict(reset_policy="never"),
                    dict(strategy="boundary", invalid_values=True)):
            self.assertEqual(self.err(**run), [], run)

    def test_stop_conditions_are_not_in_project(self):
        # (StopConditions live outside Project; cli2 validates them: see test_runtime_qa)
        self.assertEqual(self.err(), [])

    def test_hang_threshold_longer_than_case_timeout_warns(self):
        w = probs(proj(run=RunConfig(hang_threshold_ms=60000, case_timeout_ms=30000)))
        self.assertTrue(any("hang_threshold_ms" in m for m in w))


class TargetValidation(unittest.TestCase):
    def test_bad_title_regex_is_an_error(self):
        p = proj(target=TargetRef(kind="window", title_regex="(unclosed"))
        self.assertTrue(any("title_regex" in m and not m.startswith("warning:") for m in probs(p)))

    def test_empty_target_is_only_a_warning(self):
        m = probs(proj(target=TargetRef()))
        self.assertTrue(any(x.startswith("warning:") and "target" in x for x in m))
        self.assertFalse([x for x in m if not x.startswith("warning:")])


class IdentifierValidation(unittest.TestCase):
    def test_feature_and_control_id_collision_and_blank_names(self):
        c = ControlDef("f0", "integer", Selector(auto_id="x"), Domain("integer", 0, 5))
        m = probs(proj(controls=[c]))
        self.assertTrue(any("f0" in x and "both" in x for x in m), m)
        c2 = ControlDef(" ", "integer", Selector(auto_id="x"), Domain("integer", 0, 5))
        self.assertTrue(probs(proj(controls=[c2])))

    def test_nan_and_bool_defaults(self):
        c = ControlDef("c", "float", Selector(auto_id="c"), Domain("float", 0.0, 1.0), default=float("nan"))
        self.assertTrue([x for x in probs(proj(controls=[c])) if not x.startswith("warning:")])
        c = ControlDef("c", "integer", Selector(auto_id="c"), Domain("integer", 0, 5), default=True)
        self.assertTrue([x for x in probs(proj(controls=[c])) if not x.startswith("warning:")])

    def test_huge_text_max_length_and_options(self):
        c = ControlDef("c", "text", Selector(auto_id="c"), Domain("text", max_length=10 ** 9))
        self.assertTrue([x for x in probs(proj(controls=[c])) if not x.startswith("warning:")])
        c = ControlDef("c", "choice", Selector(auto_id="c"), Domain("choice", options=("a", "")))
        self.assertTrue([x for x in probs(proj(controls=[c])) if not x.startswith("warning:")])


class SaveLoadRobustness(unittest.TestCase):
    def test_save_retries_transient_permission_error(self):
        """Windows: os.replace fails with PermissionError while AV/the GUI briefly holds the file."""
        from unittest import mock
        d = tempfile.mkdtemp()
        path = Path(d) / "p.json"
        real = os.replace
        calls = []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, "sharing violation")
            return real(a, b)
        with mock.patch.object(P.os, "replace", side_effect=flaky):
            P.save(proj(), path)
        self.assertEqual(len(calls), 3)
        self.assertTrue(path.is_file())
        self.assertFalse(list(Path(d).glob("*.tmp")))

    def test_roundtrip_keeps_int_float_distinction_and_hash(self):
        c = ControlDef("c", "float", Selector(auto_id="c"), Domain("float", 0, 1.5, 0.5), default=1.0,
                       fuzz_domain=Domain("float", 0.5, 1.0, 0.5))
        p = replace(proj(controls=[c]), run=RunConfig(strategy="twise", strategy_options={"t": 2, "x": [1, 2]}))
        d = tempfile.mkdtemp()
        P.save(p, Path(d) / "x.json")
        q = P.load(Path(d) / "x.json")
        self.assertEqual(P.project_hash(p), P.project_hash(q))
        self.assertEqual(q.controls[0].domain.minimum, 0)
        self.assertIsInstance(q.controls[0].domain.minimum, int)
        self.assertIsInstance(q.controls[0].default, float)


class DangerGuard(unittest.TestCase):
    def test_classify_lexicon(self):
        from overgrind import danger
        hit = ["&Save As...", "Delete all", "btnPayNow", "Remove from cart", "Format disk", "Send",
               "Erase", "Reset all settings", "Uninstall", "Submit", "Publish", "Löschen", "Empty trash"]
        for t in hit:
            ok, why = danger.classify(t)
            self.assertTrue(ok, t)
            self.assertTrue(why, t)
        for t in ["Savannah", "Enable Alpha", "Volume", "Preferences", "Resend-free zone".replace("Resend", "Present"),
                  "Remover", "Sender", "Formatting", "", "Gain", "Mode", "Casual", "Subtitles"]:
            ok, why = danger.classify(t)
            self.assertFalse(ok, (t, why))

    def test_camel_and_snake_ids(self):
        from overgrind import danger
        for t in ("btnDeleteAll", "btn_delete", "chk_send_email", "menuFileSaveAs", "ID_FILE_ERASE"):
            self.assertTrue(danger.classify(t)[0], t)
        for t in ("btnSavannah", "chk_enable_alpha", "sld_volume"):
            self.assertFalse(danger.classify(t)[0], t)

    def test_benign_words_never_match(self):
        from overgrind import danger
        rng = random.Random(7)
        words = ["alpha", "beta", "gamma", "volume", "gain", "width", "height", "color", "theme", "font",
                 "zoom", "mode", "level", "count", "speed", "name", "title", "size", "opacity", "enable",
                 "show", "hide", "open", "view", "help", "about", "next", "previous", "left", "right"]
        for _ in range(1000):
            t = " ".join(rng.choice(words) for _ in range(rng.randint(1, 3)))
            self.assertFalse(danger.classify(t)[0], t)

    def test_validate_warns_on_dangerous_looking_enabled_feature(self):
        f = FeatureDef(id="btn_delete", name="Delete all data", kind="action", states=(), default_state="",
                       selector=Selector(auto_id="btn_delete", title="Delete all data"))
        m = probs(proj([feat(0), f]))
        self.assertTrue(any(x.startswith("warning:") and "btn_delete" in x and "dangerous" in x for x in m), m)
        # flagged dangerous or disabled: the generic dangerous warning/none, but no duplicate "looks dangerous" hint
        f2 = replace(f, dangerous=True)
        m2 = probs(proj([feat(0), f2]))
        self.assertEqual(sum("btn_delete" in x for x in m2), 1, m2)
        f3 = replace(f, enabled=False)
        self.assertFalse([x for x in probs(proj([feat(0), f3])) if "btn_delete" in x])

    def test_scan_project(self):
        from overgrind import danger
        f = FeatureDef(id="b", name="Pay now", kind="action", states=(), default_state="",
                       selector=Selector(auto_id="b"))
        hits = danger.scan_project(proj([feat(0), f]))
        self.assertEqual([h[0] for h in hits], ["b"])
        self.assertIn("pay", hits[0][2].lower())


if __name__ == "__main__":
    unittest.main()
