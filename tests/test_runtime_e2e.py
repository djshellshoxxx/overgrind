"""End-to-end QA: CLI subcommands via subprocess with MockAdapter, and a 20 s unbounded crash-injection run."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from overgrind import evidence
from overgrind import project as P
from overgrind.controller import RunController
from overgrind.types import RunConfig, StopConditions
from test_runtime_common import make_project, mock_spec
from test_runtime_controller import rss_kb

ROOT = Path(__file__).resolve().parent.parent
MOCK = "overgrind.adapters.mock:MockAdapter"


class CliEndToEnd(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="og-e2e-"))
        P.save(make_project(run=RunConfig(capture_screenshot=True, settle_ms=0, restart_on_crash=True,
                                           hang_threshold_ms=40)), self.d / "p.json")
        self.ev = self.d / "ev"

    def cli(self, *a):
        r = subprocess.run([sys.executable, "-m", "overgrind", "v2", *a], capture_output=True, cwd=self.d,
                           env=dict(os.environ, PYTHONPATH=str(ROOT)), timeout=120)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")

    def test_full_workflow(self):
        pj = str(self.d / "p.json")
        bad = '{"bad_combo": {"cb_a": "on", "cb_b": "on"}}'
        rc, out, _ = self.cli("estimate", "--project", pj)
        self.assertEqual(rc, 0)
        self.assertGreater(json.loads(out)["valid_states"], 1)
        rc, out, _ = self.cli("preview", "--project", pj, "-n", "4")
        self.assertEqual((rc, len(json.loads(out))), (0, 4))
        rc, out, err = self.cli("run", "--project", pj, "--adapter", MOCK, "--adapter-kwargs", bad,
                                "--max-seconds", "4", "--evidence", str(self.ev))
        self.assertEqual(rc, 1, err)
        st = json.loads(out)
        self.assertEqual(st["errors_unique"], 1)
        self.assertGreater(st["cases"], 20)
        files = [f for f in self.ev.rglob("*") if f.is_file()]
        incidents = [f for f in files if f.parent.parent.name == "errors" and f.suffix == ".json"]
        self.assertEqual(len(incidents), 1)                       # exactly-once persistence
        self.assertTrue(any(f.suffix == ".png" for f in files))   # screenshot attached
        rc, out, _ = self.cli("errors", "list", "--evidence", str(self.ev))
        rows = json.loads(out)
        self.assertEqual((rc, len(rows)), (1, 1))
        self.assertGreaterEqual(rows[0]["count"], 1)
        rc, out, _ = self.cli("reduce", rows[0]["id"], "--project", pj, "--adapter", MOCK,
                              "--adapter-kwargs", bad, "--evidence", str(self.ev))
        red = json.loads(out)["reduced"]
        self.assertEqual(red["state"], {"cb_a": "on", "cb_b": "on"})
        self.assertLessEqual(red["reduced_steps"], 3)
        self.assertEqual(json.loads(out)["reliability"], "reliable")
        rc, out, _ = self.cli("diff", "--project", pj, "--adapter-a", MOCK, "--adapter-b", MOCK,
                              "--kwargs-b", bad, "--cases", "30")
        rep = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertTrue(rep["regressions"])
        rc, out, _ = self.cli("diff", "--project", pj, "--adapter-a", MOCK, "--adapter-b", MOCK, "--cases", "20")
        self.assertEqual(rc, 0, out)
        v1 = {"title_regex": "App", "features": [{"id": "a", "selector": {"auto_id": "a"}}], "controls": []}
        (self.d / "v1.json").write_text(json.dumps(v1), encoding="utf-8")
        rc, out, _ = self.cli("migrate", "v1.json", "v2.json")
        self.assertEqual(rc, 0)
        self.assertEqual(P.load(self.d / "v2.json").features[0].id, "a")


class LongCrashInjection(unittest.TestCase):
    def test_20s_unbounded_run_with_crashes(self):
        d = tempfile.mkdtemp(prefix="og-long-")
        p = make_project(run=RunConfig(capture_screenshot=False, settle_ms=0, restart_on_crash=True,
                                       hang_threshold_ms=30, op_timeout_ms=800, case_timeout_ms=1500,
                                       strategy="state_cover", seed=5))
        c = RunController(p, mock_spec(crash_rate=0.02, hang_rate=0.01, wedge_rate=0.0005, seed=3), d,
                          stop=StopConditions(), checkpoint_interval_s=2.0)
        c.start()
        time.sleep(3)
        pid = c._w.pid
        r0 = rss_kb(pid)
        time.sleep(17)
        st = c.stats()
        r1 = rss_kb(c._w.pid) if r0 and c._w is not None else None
        c.stop(5)
        c.join(10)
        st = c.stats()
        self.assertGreater(st.cases, 5000)
        self.assertGreater(st.errors_total, 20)
        store = evidence.EvidenceStore(d)
        evs = store.list()
        sigs = [e.signature for e in evs]
        self.assertEqual(len(sigs), len(set(sigs)))                         # exactly-once per signature
        self.assertEqual(len(sigs), st.errors_unique)
        self.assertLessEqual(len(sigs), 4)                                  # crash, hang (+ wedge) only
        self.assertEqual(sum(store.counts().values()), st.errors_total)     # every repeat is counted, none lost
        names = {f.name for f in Path(d).rglob("*") if f.is_file()}
        self.assertFalse([n for n in names if "success" in n.lower() or n.endswith(".jsonl")])
        n_files = len([f for f in Path(d).rglob("*") if f.is_file()])
        self.assertLess(n_files, 20)                                        # no per-case files
        if r0 and r1:
            self.assertLess(r1 - r0, 30 * 1024, f"worker RSS grew {r1 - r0} KB")


if __name__ == "__main__":
    unittest.main()
