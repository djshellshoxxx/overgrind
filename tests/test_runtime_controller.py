import json
import os
import tempfile
import threading
import time
import tracemalloc
import unittest
from pathlib import Path

from overgrind import evidence
from overgrind.controller import RunController
from overgrind.types import (
    Case, ControlDef, Domain, FeatureDef, HarnessError, Health, RunConfig, Selector, StopConditions,
)
from overgrind.worker import Worker, WorkerConfig
from test_runtime_common import SIMPLE_GEN, make_project, mock_spec


def rss_kb(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def run(cfg_kw=None, spec_kw=None, stop=None, wait=None, ckpt=None, tmp=None, resume=False, project=None):
    """Start a controller, wait for it (bounded), return (controller, evidence_dir)."""
    d = tmp or tempfile.mkdtemp(prefix="og-ev-")
    base = dict(capture_screenshot=False, settle_ms=0, hang_threshold_ms=40, op_timeout_ms=600)
    base.update(cfg_kw or {})
    p = project or make_project(run=RunConfig(**base))
    c = RunController(p, mock_spec(**(spec_kw or {})), d, stop=stop or StopConditions(),
                      generator_factory=SIMPLE_GEN, checkpoint_interval_s=ckpt, resume=resume)
    c.start()
    if wait is not None:
        if not c.join(wait):
            c.stop()
    return c, d


class UnboundedRunTests(unittest.TestCase):
    def test_unbounded_run_bounded_memory_no_files_and_fast_stop(self):
        d = tempfile.mkdtemp(prefix="og-ev-")
        shutil_dir = Path(d) / "evidence"                  # must never even be created
        c, _ = run(tmp=str(shutil_dir))
        try:
            time.sleep(1.2)
            tracemalloc.start()
            snap1 = tracemalloc.take_snapshot()
            rss1 = rss_kb(c._w.pid)
            n1 = c.stats().cases
            time.sleep(2.0)
            st = c.stats()
            snap2 = tracemalloc.take_snapshot()
            rss2 = rss_kb(c._w.pid)
            grown = sum(s.size_diff for s in snap2.compare_to(snap1, "filename"))
            tracemalloc.stop()
            self.assertEqual(st.state, "running")
            self.assertGreater(st.cases, n1 + 1000, "run must keep making progress")
            self.assertGreater(st.cases_per_s, 0)
            self.assertLess(grown, 512 * 1024, "controller memory grows with cases")
            if rss1 and rss2:
                self.assertLess(rss2 - rss1, 15 * 1024, "worker RSS grows with cases")
            self.assertFalse(shutil_dir.exists(), "success cases must write nothing")
            self.assertEqual(st.errors_total, 0)
            self.assertLess(len(st.recent), 50)
        finally:
            t0 = time.monotonic()
            c.stop()
            latency = time.monotonic() - t0
        self.assertLess(latency, 2.0)
        self.assertEqual(c.state, "finished")
        self.assertEqual(c.stats().stop_reason, "stopped by user")
        self.assertFalse(shutil_dir.exists())

    def test_pause_resume_between_cases_and_timed_stop_excludes_pause(self):
        c, _ = run(stop=StopConditions(max_seconds=2.0))
        time.sleep(0.8)
        c.pause()
        self.assertEqual(c.state, "paused")
        time.sleep(0.4)                       # let the in-flight case finish
        n1 = c.stats().cases
        time.sleep(1.2)                       # > max_seconds in wall time, but paused time is excluded
        self.assertEqual(c.stats().cases, n1)
        self.assertEqual(c.state, "paused")
        self.assertLess(c.stats().elapsed_s, 1.6)
        c.resume()
        self.assertEqual(c.state, "running")
        t0 = time.monotonic()
        self.assertTrue(c.join(8))
        st = c.stats()
        self.assertGreater(st.cases, n1)
        self.assertEqual(st.stop_reason, "max_seconds")
        self.assertAlmostEqual(st.elapsed_s, 2.0, delta=0.5)
        self.assertLess(time.monotonic() - t0, 3.0)

    def test_pause_counts_toward_time_when_configured(self):
        c, _ = run(cfg_kw=dict(pause_counts_toward_time=True), stop=StopConditions(max_seconds=1.2))
        time.sleep(0.3)
        c.pause()
        self.assertTrue(c.join(5), "paused time must count toward max_seconds")
        self.assertEqual(c.stats().stop_reason, "max_seconds")

    def test_max_cases_is_exact_and_first_error_stops(self):
        c, _ = run(stop=StopConditions(max_cases=500), wait=15)
        self.assertEqual(c.stats().cases, 500)
        self.assertEqual(c.stats().stop_reason, "max_cases")
        c, d = run(spec_kw=dict(bad_combo={"cb_a": "on", "cb_b": "on"}), cfg_kw=dict(restart_on_crash=True),
                   stop=StopConditions(first_error=True), wait=15)
        self.assertEqual(c.stats().stop_reason, "first_error")
        self.assertEqual(len(evidence.EvidenceStore(d).list()), 1)


class FailureHandlingTests(unittest.TestCase):
    def test_one_deduped_event_per_distinct_signature(self):
        seen = []
        d = tempfile.mkdtemp(prefix="og-ev-")
        p = make_project(run=RunConfig(capture_screenshot=True, settle_ms=0, hang_threshold_ms=40,
                                       restart_on_crash=True))
        c = RunController(p, mock_spec(crash_rate=0.03, hang_rate=0.03, dialog_rate=0.03, seed=5,
                                       bad_combo={"cb_a": "on", "cb_b": "on", "cb_g": "on"}),
                          d, stop=StopConditions(max_seconds=3.0), generator_factory=SIMPLE_GEN)
        c.on_error(seen.append)
        c.start()
        self.assertTrue(c.join(15))
        st = c.stats()
        events = evidence.EvidenceStore(d).list()
        sigs = [e.signature for e in events]
        self.assertEqual(len(sigs), len(set(sigs)), "duplicate signature persisted twice")
        self.assertEqual({e.category for e in events}, {"crash", "hang", "dialog"})
        self.assertEqual(len(events), len(seen))
        self.assertEqual(st.errors_unique, len(events))
        self.assertGreater(st.crashes, 3)
        self.assertGreater(st.hangs, 3)
        self.assertGreater(st.errors_total, len(events) * 3, "repeats must dedup, not multiply events")
        counts = evidence.EvidenceStore(d).counts()
        self.assertEqual(sum(counts.values()), st.errors_total)
        self.assertGreater(st.restarts, 5)
        self.assertGreater(st.cases, 100, "run must continue after failures")
        for e in events:
            self.assertTrue(e.history, "history ring must be attached")
            self.assertIn("screenshot", e.attachments)
            self.assertTrue((Path(d) / e.attachments["screenshot"]).is_file())
        # tmp screenshots are cleaned up
        self.assertFalse(os.path.isdir(c._shot_dir))

    def test_crash_without_restart_stops_the_run(self):
        c, d = run(spec_kw=dict(bad_combo={"cb_a": "on"}), cfg_kw=dict(restart_on_crash=False), wait=15)
        self.assertTrue(c.stats().stop_reason.startswith("target crashed"), c.stats().stop_reason)
        self.assertEqual(len(evidence.EvidenceStore(d).list()), 1)

    def test_watchdog_kills_wedged_worker_and_run_continues(self):
        c, d = run(spec_kw=dict(wedge_rate=0.004, seed=2), cfg_kw=dict(op_timeout_ms=300, restart_on_crash=True),
                   stop=StopConditions(max_seconds=4.0), wait=20)
        st = c.stats()
        self.assertGreaterEqual(st.restarts, 1)
        self.assertGreater(st.cases, 300)
        events = evidence.EvidenceStore(d).list()
        self.assertEqual(len(events), 1, [e.message for e in events])
        self.assertEqual(events[0].category, "hang")
        self.assertTrue(events[0].message.startswith("watchdog:"))
        self.assertGreater(events[0].case.seq, 0, "in-flight case must be recovered for replay")
        self.assertEqual(st.stop_reason, "max_seconds")

    def test_stop_returns_quickly_even_if_worker_is_wedged(self):
        c, _ = run(spec_kw=dict(wedge_rate=1.0), cfg_kw=dict(op_timeout_ms=60000, case_timeout_ms=60000))
        time.sleep(1.2)
        self.assertEqual(c.state, "running")
        t0 = time.monotonic()
        c.stop()
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertEqual(c.state, "finished")

    def test_repeated_wedges_do_not_loop_forever(self):
        c, _ = run(spec_kw=dict(wedge_rate=1.0), cfg_kw=dict(op_timeout_ms=200, restart_on_crash=True), wait=40)
        self.assertEqual(c.state, "finished")
        self.assertIn("no progress", c.stats().stop_reason)

    def test_harness_errors_are_not_app_defects(self):
        c, d = run(spec_kw=dict(harness_rate=0.03, seed=4), stop=StopConditions(max_seconds=1.5), wait=15)
        st = c.stats()
        self.assertGreater(st.harness_errors, 10)
        self.assertGreater(st.cases, 10)
        self.assertEqual((st.errors_total, st.errors_unique, st.crashes, st.hangs), (0, 0, 0, 0))
        self.assertFalse(Path(d, "errors").exists())

    def test_persistent_harness_failure_stops_as_automation_unusable(self):
        c, d = run(spec_kw=dict(harness_rate=1.0), wait=15)
        st = c.stats()
        self.assertTrue(st.stop_reason.startswith("automation unusable"), st.stop_reason)
        self.assertEqual(st.errors_total, 0)
        self.assertGreaterEqual(st.harness_errors, 21)
        self.assertFalse(Path(d, "errors").exists())


class CheckpointTests(unittest.TestCase):
    def _seq(self, d):
        return json.loads(Path(d, "checkpoint.json").read_text())["generator"]["seq"]

    def test_checkpoint_is_tiny_and_resume_continues(self):
        d = tempfile.mkdtemp(prefix="og-ev-")
        c, _ = run(tmp=d, ckpt=0.3, stop=StopConditions(max_seconds=1.5), wait=15)
        path = Path(d, "checkpoint.json")
        self.assertTrue(path.is_file())
        self.assertLess(path.stat().st_size, 2048)
        s1 = self._seq(d)
        self.assertGreater(s1, 500)
        c, _ = run(tmp=d, ckpt=0.3, stop=StopConditions(max_cases=300), resume=True, wait=15)
        s2 = self._seq(d)
        self.assertGreaterEqual(s2, s1 + 300)
        self.assertLess(s2, s1 + 300 + 3000)
        # a different project must not pick up a foreign checkpoint
        other = make_project(n_features=3, run=RunConfig(capture_screenshot=False, settle_ms=0))
        c, _ = run(tmp=d, ckpt=0.3, stop=StopConditions(max_cases=300), resume=True, wait=15, project=other)
        self.assertLess(self._seq(d), s1)


class FakeTx:
    def __init__(self):
        self.msgs = []

    def send(self, m):
        self.msgs.append(m)

    def close(self):
        pass


class _V:
    value = 0


class _HB:
    def __init__(self):
        self.op_start, self.case_start, self.pos, self.done = _V(), _V(), _V(), _V()


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    def attach(self, t): pass
    def detach(self, kill=False): pass
    def pid(self): return None
    def reset(self, p): self.calls.append(("reset",))
    def set_feature_state(self, f, s): self.calls.append(("state", f.id, s))
    def invoke(self, f): self.calls.append(("invoke", f.id))
    def set_control(self, c, v): self.calls.append(("control", c.id, v))
    def wait_quiescent(self, t): self.calls.append(("wait",))
    def health(self): return Health("ok")


def worker_for(project, adapter):
    w = Worker(WorkerConfig(project, mock_spec(), "r"), FakeTx(), _HB(), threading.Event(), threading.Event())
    w.adapter = adapter
    return w


class WorkerExecutionTests(unittest.TestCase):
    def test_order_scope_dangerous_and_skip(self):
        feats = (
            FeatureDef("a", selector=Selector(auto_id="a")),
            FeatureDef("b", selector=Selector(auto_id="b")),
            FeatureDef("go", kind="action", states=(), default_state="", selector=Selector(auto_id="go")),
            FeatureDef("boom", kind="action", states=(), default_state="", dangerous=True, selector=Selector(auto_id="x")),
            FeatureDef("off", enabled=False, selector=Selector(auto_id="y")),
        )
        d = Domain("integer", 0, 9, 1)
        ctrls = (
            ControlDef("scoped", "integer", Selector(auto_id="s"), d, default=0, scope=("a",)),
            ControlDef("glob", "integer", Selector(auto_id="g"), d, default=0),
            ControlDef("ro", "integer", Selector(auto_id="r"), d, default=0, read_only=True),
            ControlDef("sk", "integer", Selector(auto_id="k"), d, default=0, skip=True),
        )
        from overgrind.types import Project, TargetRef
        p = Project("t", TargetRef(), feats, ctrls, run=RunConfig(settle_ms=0, capture_screenshot=False))
        ad = RecordingAdapter()
        w = worker_for(p, ad)
        vals = {"scoped": 3, "glob": 4, "ro": 5, "sk": 6}
        w.execute(Case(1, {"a": "off", "b": "on", "off": "on"}, vals, ("go", "boom", "b", "a")))
        self.assertEqual(ad.calls, [("reset",), ("invoke", "go"), ("state", "b", "on"), ("state", "a", "off"),
                                    ("control", "glob", 4), ("wait",)])
        ad.calls.clear()
        w.execute(Case(2, {"a": "on", "b": "off"}, vals, ("a", "b")))
        self.assertIn(("control", "scoped", 3), ad.calls)
        self.assertNotIn(("invoke", "boom"), ad.calls)
        self.assertEqual([c for c in ad.calls if c[0] == "control"], [("control", "scoped", 3), ("control", "glob", 4)])

    def test_reset_policies(self):
        p = make_project(controls=False)

        def resets(policy, interval=3):
            pr = make_project(controls=False, run=RunConfig(reset_policy=policy, reset_interval=interval,
                                                           settle_ms=0, capture_screenshot=False))
            ad = RecordingAdapter()
            w = worker_for(pr, ad)
            for i in range(6):
                w.execute(Case(i + 1, {"cb_a": "on"}, {}, ("cb_a",)))
            return sum(1 for c in ad.calls if c[0] == "reset")

        self.assertEqual(resets("every_case"), 6)
        self.assertEqual(resets("interval", 3), 2)
        self.assertEqual(resets("never"), 0)


if __name__ == "__main__":
    unittest.main()
