import contextlib
import dataclasses
import io
import json
import tempfile
import unittest
from pathlib import Path

from overgrind import project as P
from overgrind.__main__ import main as v1_main
from overgrind.adapters.mock import MockAdapter
from overgrind.cli2 import main as cli
from overgrind.preflight import preflight, run_preflight
from overgrind.types import FeatureDef, RunConfig, Selector, AdapterSpec
from test_runtime_common import SIMPLE_GEN, make_project, mock_spec


class ClampAdapter(MockAdapter):
    """Reads back a different value than written (app clamps)."""
    def get_control(self, control):
        v = super().get_control(control)
        return v + 1 if control.id == "volume" else v


class RecordingMock(MockAdapter):
    log: list = []

    def invoke(self, feature):
        RecordingMock.log.append(("invoke", feature.id))
        super().invoke(feature)

    def set_feature_state(self, feature, state):
        RecordingMock.log.append(("state", feature.id))
        super().set_feature_state(feature, state)


def call(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli(argv)
    return rc, out.getvalue(), err.getvalue()


class PreflightTests(unittest.TestCase):
    def test_clean_project_is_ok_and_never_touches_dangerous(self):
        RecordingMock.log = []
        p = make_project(dangerous=True)
        rep = run_preflight(p, AdapterSpec("test_runtime_cli_preflight:RecordingMock"))
        self.assertEqual(rep.verdict, "ok", rep.summary())
        self.assertNotIn(("invoke", "btn_apply"), RecordingMock.log)
        self.assertFalse([e for e in RecordingMock.log if e[1] == "btn_delete"])
        item = next(i for i in rep.items if i.id == "btn_delete")
        self.assertTrue(any("dangerous" in c.detail for c in item.checks))
        ok = next(i for i in rep.items if i.id == "cb_a")
        self.assertEqual(ok.tier, "auto_id")
        self.assertTrue(any(c.name == "read-back" and c.status == "ok" for c in ok.checks))
        self.assertTrue(any(i.kind == "reset" and i.status == "ok" for i in rep.items))

    def test_wrong_selector_fails_with_copy_pastable_hint(self):
        p = make_project()
        bad = FeatureDef("cb_a", selector=Selector(auto_id="cb_alfa", control_type="CheckBox"))
        p = dataclasses.replace(p, features=(bad,) + p.features[1:])
        rep = run_preflight(p, mock_spec())
        self.assertEqual(rep.verdict, "fail")
        item = next(i for i in rep.items if i.id == "cb_a")
        self.assertEqual(item.status, "fail")
        hint = " ".join(item.hints)
        self.assertIn('"auto_id": "cb_alpha"', hint)
        json.loads(hint[hint.index("{"):hint.rindex("}") + 1])      # the snippet is valid JSON

    def test_readback_mismatch_is_reported(self):
        rep = run_preflight(make_project(), AdapterSpec("test_runtime_cli_preflight:ClampAdapter"))
        vol = next(i for i in rep.items if i.id == "volume")
        self.assertEqual(vol.status, "fail")
        self.assertTrue(any("read" in c.detail for c in vol.checks if c.status == "fail"))

    def test_fragile_selector_warns(self):
        p = make_project()
        frag2 = FeatureDef("cb_b", selector=Selector(class_name="CheckBox", control_type="CheckBox", index=1))
        p = dataclasses.replace(p, features=(p.features[0], frag2) + p.features[2:])
        rep = run_preflight(p, mock_spec())
        item = next(i for i in rep.items if i.id == "cb_b")
        self.assertEqual(item.status, "warn")
        self.assertEqual(item.tier, "class+index")
        self.assertTrue(any("auto_id" in h for h in item.hints))

    def test_isolated_child_process(self):
        rep = preflight(make_project(), mock_spec(), isolated=True, timeout_s=60)
        self.assertEqual(rep.verdict, "ok", rep.summary())

    def test_isolated_timeout_fails_cleanly(self):
        rep = preflight(make_project(), mock_spec(latency_ms=2000), isolated=True, timeout_s=1.0)
        self.assertEqual(rep.verdict, "fail")
        self.assertIn("did not finish", rep.summary())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="og-cli-")
        self.proj = str(Path(self.tmp, "p.json"))
        P.save(make_project(run=RunConfig(capture_screenshot=False, settle_ms=0, restart_on_crash=True,
                                           hang_threshold_ms=40)), self.proj)
        self.ev = str(Path(self.tmp, "ev"))
        self.mock = "overgrind.adapters.mock:MockAdapter"

    def test_run_exit_codes_and_errors_commands(self):
        rc, out, _ = call(["run", "--project", self.proj, "--adapter", self.mock, "--generator", SIMPLE_GEN,
                           "--max-seconds", "1", "--evidence", self.ev])
        self.assertEqual(rc, 0)
        self.assertGreater(json.loads(out)["cases"], 0)
        self.assertFalse(Path(self.ev, "errors").exists())
        rc, out, err = call(["run", "--project", self.proj, "--adapter", self.mock, "--generator", SIMPLE_GEN,
                             "--adapter-kwargs", '{"bad_combo": {"cb_a": "on"}}', "--max-seconds", "1.5",
                             "--evidence", self.ev])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["errors_unique"], 1)
        rc, out, _ = call(["errors", "list", "--evidence", self.ev])
        rows = json.loads(out)
        self.assertEqual((rc, len(rows)), (1, 1))
        rc, out, _ = call(["errors", "show", rows[0]["id"], "--evidence", self.ev])
        self.assertEqual(json.loads(out)["category"], "crash")
        rc, out, _ = call(["reduce", rows[0]["id"], "--project", self.proj, "--adapter", self.mock,
                           "--adapter-kwargs", '{"bad_combo": {"cb_a": "on"}}', "--evidence", self.ev])
        self.assertIn("reliability", json.loads(out))

    def test_preflight_preview_estimate_migrate(self):
        rc, out, _ = call(["preflight", "--project", self.proj, "--adapter", self.mock, "--in-process"])
        self.assertEqual((rc, json.loads(out)["verdict"]), (0, "ok"))
        rc, out, _ = call(["preflight", "--project", self.proj, "--adapter", self.mock, "--in-process",
                           "--adapter-kwargs", '{"latency_ms": 0}', "--text"])
        self.assertIn("preflight: OK", out)
        rc, out, _ = call(["preview", "--project", self.proj, "-n", "3"])
        self.assertEqual((rc, len(json.loads(out))), (0, 3))
        rc, out, _ = call(["estimate", "--project", self.proj])
        self.assertIn("valid_states", json.loads(out))
        dst = str(Path(self.tmp, "v2.json"))
        rc, out, _ = call(["migrate", "examples/demo.json", dst])
        self.assertEqual(rc, 0)
        self.assertTrue(Path(dst).is_file())

    def test_usage_errors_exit_2_and_v1_delegation(self):
        self.assertEqual(call([])[0], 2)
        self.assertEqual(call(["run"])[0], 2)
        self.assertEqual(call(["run", "--project", "/nonexistent.json"])[0], 2)
        self.assertEqual(call(["run", "--project", self.proj, "--adapter", "nocolon"])[0], 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = v1_main(["v2", "preview", "--project", self.proj, "-n", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out.getvalue())), 1)


if __name__ == "__main__":
    unittest.main()
