"""QA-BACKEND regression tests: worker / controller / cli / mock / coverage (written before the fixes)."""
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import zlib
from dataclasses import replace
from pathlib import Path

from overgrind import evidence, scheduler
from overgrind import project as P
from overgrind.adapters.mock import MockAdapter, SimpleGenerator
from overgrind.controller import RunController
from overgrind.types import (
    Case, ConstraintDef, ControlDef, Coverage, Domain, FeatureDef, Project, RunConfig, Selector,
    StopConditions, TargetRef,
)
from overgrind.worker import Worker, WorkerConfig
from test_engine2_common import control, project as eproject, take, toggles
from test_runtime_common import SIMPLE_GEN, make_project, mock_spec
from test_runtime_controller import FakeTx, RecordingAdapter, _HB, worker_for

ROOT = Path(__file__).resolve().parent.parent


class WorkerScopeSemantics(unittest.TestCase):
    def test_scope_active_means_not_the_first_state(self):
        """types.ControlDef.scope + constraints: a feature is 'active' when state != states[0].
        The worker used a hard-coded off-word list, so Low/Medium/High scopes were always 'active'."""
        feats = (FeatureDef("mode", kind="multistate", states=("Low", "Mid", "High"), default_state="Low",
                            selector=Selector(auto_id="m")),
                 FeatureDef("flag", states=("no", "yes"), default_state="no", selector=Selector(auto_id="f")))
        d = Domain("integer", 0, 9, 1)
        ctrls = (ControlDef("c_mode", "integer", Selector(auto_id="a"), d, default=0, scope=("mode",)),
                 ControlDef("c_flag", "integer", Selector(auto_id="b"), d, default=0, scope=("flag",)))
        p = Project("t", TargetRef(), feats, ctrls, run=RunConfig(settle_ms=0, capture_screenshot=False))
        ad = RecordingAdapter()
        w = worker_for(p, ad)
        w.execute(Case(1, {"mode": "Low", "flag": "no"}, {"c_mode": 1, "c_flag": 2}, ("mode", "flag")))
        self.assertEqual([c for c in ad.calls if c[0] == "control"], [])
        ad.calls.clear()
        w.execute(Case(2, {"mode": "High", "flag": "yes"}, {"c_mode": 1, "c_flag": 2}, ("mode", "flag")))
        self.assertEqual([c for c in ad.calls if c[0] == "control"], [("control", "c_mode", 1), ("control", "c_flag", 2)])
        ad.calls.clear()
        w.execute(Case(3, {"mode": "Mid"}, {"c_mode": 1, "c_flag": 2}, ("mode",)))     # flag absent -> default 'no'
        self.assertEqual([c for c in ad.calls if c[0] == "control"], [("control", "c_mode", 1)])


class SensitiveNeverReachesDisk(unittest.TestCase):
    def test_controller_redacts_sensitive_controls_everywhere(self):
        # pick a seed whose first case has a long text value for the sensitive control
        dom = Domain("text", max_length=12, alphabet="XQ")
        feats = (FeatureDef("cb_a", selector=Selector(auto_id="a")),)
        ctl = ControlDef("field7", "text", Selector(auto_id="f7"), dom, default="", sensitive=True)
        secret = None
        for seed in range(1, 200):
            p = Project("t", TargetRef(), feats, (ctl,),
                        run=RunConfig(seed=seed, capture_screenshot=False, settle_ms=0, hang_threshold_ms=40))
            c0 = next(iter(SimpleGenerator(p)))
            if len(c0.values["field7"]) >= 8:
                secret = c0.values["field7"]
                break
        self.assertIsNotNone(secret)
        d = tempfile.mkdtemp(prefix="og-sens-")
        c = RunController(p, mock_spec(crash_rate=1.0), d, stop=StopConditions(max_cases=5),
                          generator_factory=SIMPLE_GEN, checkpoint_interval_s=0.1)
        c.start()
        self.assertTrue(c.join(30))
        self.assertEqual(c.stats().errors_unique, 1)
        blob = b"".join(f.read_bytes() for f in Path(d).rglob("*") if f.is_file())
        self.assertNotIn(secret.encode(), blob)
        self.assertIn(b"field7", blob)                  # the control is still named in the evidence
        ev = evidence.EvidenceStore(d).list()[0]
        self.assertEqual(ev.case.values["field7"], "***")
        self.assertTrue(any(a.value == "***" for a in ev.history if a.target == "field7"))


class MockScreenshot(unittest.TestCase):
    def test_png_is_structurally_valid(self):
        d = tempfile.mkdtemp()
        m = MockAdapter()
        m.attach()
        path = os.path.join(d, "s.png")
        self.assertTrue(m.screenshot(path))
        raw = Path(path).read_bytes()
        self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
        pos, chunks, idat = 8, [], b""
        while pos < len(raw):
            (ln,) = struct.unpack(">I", raw[pos:pos + 4])
            tag, data = raw[pos + 4:pos + 8], raw[pos + 8:pos + 8 + ln]
            (crc,) = struct.unpack(">I", raw[pos + 8 + ln:pos + 12 + ln])
            self.assertEqual(crc, zlib.crc32(tag + data) & 0xFFFFFFFF, tag)
            chunks.append(tag)
            if tag == b"IHDR":
                w, h, depth, ctype, comp, filt, inter = struct.unpack(">IIBBBBB", data)
            if tag == b"IDAT":
                idat += data
            pos += 12 + ln
        self.assertEqual(chunks[0], b"IHDR")
        self.assertEqual(chunks[-1], b"IEND")
        self.assertGreaterEqual(min(w, h), 16)
        bpp = {2: 3, 6: 4}[ctype]
        self.assertEqual(len(zlib.decompress(idat)), h * (1 + w * bpp))

    def test_qt_can_load_it_when_available(self):
        try:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6.QtGui import QGuiApplication, QImage
        except Exception:
            self.skipTest("PySide6 not installed")
        app = QGuiApplication.instance() or QGuiApplication([])
        d = tempfile.mkdtemp()
        m = MockAdapter()
        m.attach()
        m.screenshot(os.path.join(d, "s.png"))
        img = QImage(os.path.join(d, "s.png"))
        self.assertFalse(img.isNull())
        self.assertGreaterEqual(img.width(), 16)
        del app


class CoverageCounts(unittest.TestCase):
    def test_new_fields_are_additive_with_defaults(self):
        c = Coverage()
        self.assertEqual((c.infeasible, c.unknown, c.evidence, c.state_counts, c.visit_hist), (0, 0, "", (), ()))
        # every pre-existing positional field keeps its slot
        c = Coverage(8, 3, 1, 2, 54, 10, 4, 3, "g")
        self.assertEqual((c.states_total, c.states_visited, c.epochs, c.tway_t, c.tway_total, c.tway_covered,
                          c.buckets_total, c.buckets_visited, c.guarantee), (8, 3, 1, 2, 54, 10, 4, 3, "g"))

    def test_state_cover_reports_per_state_visit_counts(self):
        p = eproject(toggles(3), seed=4)
        g = scheduler.make_generator(p)
        take(g, 5)
        cv = g.coverage()
        self.assertEqual(len(cv.state_counts), 8)
        self.assertEqual(sum(cv.state_counts), 5)
        self.assertEqual(max(cv.state_counts), 1)
        self.assertEqual(cv.states_visited, 5)
        self.assertEqual(cv.visit_hist[:2], (3, 5))
        self.assertEqual(sum(cv.visit_hist), 8)
        take(g, 6)                                      # 11 = one full epoch + 3 of the second
        cv = g.coverage()
        self.assertEqual(sorted(set(cv.state_counts)), [1, 2])
        self.assertEqual(sum(cv.state_counts), 11)
        self.assertEqual(cv.visit_hist[1:3], (5, 3))

    def test_counts_survive_checkpoint_and_match_rank_to_state(self):
        p = eproject(toggles(4), seed=9)
        g = scheduler.make_generator(p)
        cases = take(g, 21)
        ck = json.loads(json.dumps(g.checkpoint()))
        g2 = scheduler.make_generator(p, ck)
        self.assertEqual(g.coverage().state_counts, g2.coverage().state_counts)
        self.assertEqual(g.coverage().visit_hist, g2.coverage().visit_hist)
        # rank i of state_counts is the i-th valid state (scheduler.unrank_state)
        seen = {}
        for c in cases:
            seen[tuple(sorted(c.state.items()))] = seen.get(tuple(sorted(c.state.items())), 0) + 1
        cv = g.coverage()
        for i, n in enumerate(cv.state_counts):
            st = scheduler.unrank_state(p, i)
            self.assertEqual(seen.get(tuple(sorted(st.items())), 0), n)

    def test_big_spaces_report_histogram_but_no_per_state_vector(self):
        p = eproject(toggles(14), seed=1)               # 16384 states > per-state cap
        g = scheduler.make_generator(p)
        take(g, 10)
        cv = g.coverage()
        self.assertEqual(cv.state_counts, ())
        self.assertEqual(cv.visit_hist[:2], (16384 - 10, 10))

    def test_twise_reports_infeasible_and_unknown(self):
        p = eproject(toggles(4), constraints=[ConstraintDef("mutex", features=("f0", "f1"))],
                     strategy="twise", seed=2)
        g = scheduler.make_generator(p)
        take(g, 20)
        cv = g.coverage()
        self.assertEqual(cv.infeasible, 1 * 1 * 0 + g.progress()["infeasible"])
        self.assertGreater(cv.infeasible, 0)
        self.assertEqual(cv.unknown, 0)


class CliRobustness(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="og-cli-"))
        p = Project("Löschen é☃", TargetRef(kind="window", title_regex="App"),
                    (FeatureDef("cb_é", name="Enable ☃", selector=Selector(auto_id="a")),
                     FeatureDef("cb_b", selector=Selector(auto_id="b"))),
                    (control("vol", "integer"),), run=RunConfig(capture_screenshot=False, settle_ms=0))
        self.proj = self.dir / "p.json"
        P.save(p, self.proj)

    def cli(self, *args, env=None, cwd=None):
        e = dict(os.environ, **(env or {}))
        e["PYTHONPATH"] = str(ROOT)
        return subprocess.run([sys.executable, "-m", "overgrind", "v2", *args], capture_output=True,
                              cwd=cwd or self.dir, env=e, timeout=120)

    def test_non_ascii_output_survives_a_legacy_codepage_stdout(self):
        for args in (("preview", "--project", str(self.proj), "-n", "2"),
                     ("estimate", "--project", str(self.proj))):
            r = self.cli(*args, env={"PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0"})
            self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace"))
            json.loads(r.stdout.decode("ascii"))                     # machine-readable and pure ASCII

    def test_bad_numeric_stop_conditions_are_usage_errors(self):
        for flag, val in (("--max-seconds", "nan"), ("--max-seconds", "-1"), ("--max-seconds", "inf"),
                          ("--max-cases", "-3"), ("--max-unique-errors", "0")):
            r = self.cli("run", "--project", str(self.proj), "--adapter", "overgrind.adapters.mock:MockAdapter",
                         flag, val)
            self.assertEqual(r.returncode, 2, (flag, val, r.stdout, r.stderr))

    def test_unknown_adapter_and_unknown_error_id_exit_2(self):
        r = self.cli("run", "--project", str(self.proj), "--adapter", "no.such.module:Thing", "--max-cases", "3")
        self.assertEqual(r.returncode, 2, r.stderr.decode(errors="replace"))
        r = self.cli("errors", "show", "nope", "--evidence", str(self.dir / "ev"))
        self.assertEqual(r.returncode, 2)

    def test_errors_list_on_empty_dir_is_clean_and_creates_nothing(self):
        r = self.cli("errors", "list", "--evidence", str(self.dir / "never"))
        self.assertEqual((r.returncode, json.loads(r.stdout)), (0, []))
        self.assertFalse((self.dir / "never").exists())

    def test_invalid_project_is_rejected_with_exit_2(self):
        bad = self.dir / "bad.json"
        p = P.load(self.proj)
        P.save(replace(p, run=RunConfig(strategy="nope")), bad)
        r = self.cli("estimate", "--project", str(bad))
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"strategy", r.stderr)


if __name__ == "__main__":
    unittest.main()


class CliWarningsDoNotBlock(unittest.TestCase):
    def test_warning_only_project_runs_and_warns_on_stderr(self):
        """validate() warnings (dangerous feature, empty target, ...) used to abort every CLI command."""
        d = Path(tempfile.mkdtemp(prefix="og-cliw-"))
        f = (FeatureDef("cb_a", selector=Selector(auto_id="a")),
             FeatureDef("btn_delete", name="Delete all", kind="action", states=(), default_state="",
                        selector=Selector(auto_id="d")))
        p = Project("w", TargetRef(), f, ())
        P.save(p, d / "p.json")
        self.assertTrue(any(m.startswith("warning:") for m in P.validate(p)))
        r = subprocess.run([sys.executable, "-m", "overgrind", "v2", "estimate", "--project", str(d / "p.json")],
                           capture_output=True, cwd=d, env=dict(os.environ, PYTHONPATH=str(ROOT)), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"warning:", r.stderr)
        self.assertIn("valid_states", json.loads(r.stdout))


class CheckpointDoesNotPersistSensitiveValues(unittest.TestCase):
    def test_chaos_and_adaptive_checkpoints_drop_sensitive_values(self):
        for strategy in ("chaos", "adaptive"):
            dom = Domain("text", max_length=12, alphabet="XQ")
            feats = (FeatureDef("cb_a", selector=Selector(auto_id="a")), FeatureDef("cb_b", selector=Selector(auto_id="b")))
            ctl = (ControlDef("field7", "text", Selector(auto_id="f7"), dom, default="XQXQXQXQ", sensitive=True),
                   ControlDef("vol", "integer", Selector(auto_id="v"), Domain("integer", 0, 9), default=1))
            p = Project("t", TargetRef(), feats, ctl,
                        run=RunConfig(strategy=strategy, seed=3, capture_screenshot=False, settle_ms=0))
            d = tempfile.mkdtemp(prefix="og-ck-")
            c = RunController(p, mock_spec(), d, stop=StopConditions(max_cases=60), checkpoint_interval_s=0.05)
            c.start()
            self.assertTrue(c.join(60))
            ck = (Path(d) / "checkpoint.json").read_text()
            self.assertIn('"generator"', ck)
            self.assertNotIn("field7", ck, strategy)
            self.assertNotIn("XQXQXQXQ", ck, strategy)
            # and the run can resume from it (sensitive controls simply get fresh values)
            c2 = RunController(p, mock_spec(), d, stop=StopConditions(max_cases=50), resume=True,
                               checkpoint_interval_s=None)
            c2.start()
            self.assertTrue(c2.join(60))
            self.assertEqual(c2.stats().stop_reason, "max_cases")
            self.assertEqual(c2.stats().harness_errors, 0)
