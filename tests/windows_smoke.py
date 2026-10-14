"""Windows-only end-to-end smoke tests against fixtures/synthetic_app.py (skipped elsewhere).

Run on Windows CI (interactive desktop):  python -m unittest tests.windows_smoke -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "fixtures" / "synthetic_app.py")
WIN = sys.platform == "win32"

if WIN:
    sys.path.insert(0, str(ROOT))


def _project(bug: str = "", restart: bool = True):
    from overgrind.types import (
        ControlDef, Domain, FeatureDef, Project, RunConfig, Selector, TargetRef,
    )
    args = (APP,) + (("--bug", bug) if bug else ())
    feats = tuple(FeatureDef(id=f"cb_{k}", name=f"Option {k.upper()}",
                             selector=Selector(auto_id=f"cb_{k}", control_type="CheckBox"))
                  for k in "abcd")
    feats += (FeatureDef(id="btn_delete", kind="action", states=(), default_state="", dangerous=True,
                         selector=Selector(auto_id="btn_delete", control_type="Button")),)
    ctrls = (ControlDef("volume", "integer", Selector(auto_id="sld_volume", control_type="Slider"),
                        Domain("integer", 0, 100, 1), default=50),)
    run = RunConfig(restart_on_crash=restart, capture_screenshot=True, settle_ms=20,
                    op_timeout_ms=8000, case_timeout_ms=30000, hang_threshold_ms=3000)
    return Project("smoke", TargetRef(kind="exe", exe_path=sys.executable, args=args),
                   feats, ctrls, run=run)


@unittest.skipUnless(WIN, "Windows UI Automation smoke tests")
class UiaSmoke(unittest.TestCase):
    def setUp(self):
        from overgrind.adapters.uia import UiaAdapter
        self.project = _project()
        self.a = UiaAdapter()
        self.a.attach(self.project.target)

    def tearDown(self):
        self.a.detach(kill=True)

    def test_inspect_is_readonly_and_verified(self):
        els = self.a.inspect()
        boxes = [e for e in els if e.selector.auto_id.startswith("cb_")]
        self.assertEqual(len(boxes), 4)
        self.assertTrue(all(e.confidence == "verified" and e.suggested_kind == "boolean" for e in boxes))
        vol = next(e for e in els if e.selector.auto_id == "sld_volume")
        self.assertEqual((vol.suggested_domain.minimum, vol.suggested_domain.maximum), (0, 100))
        self.assertEqual(self.a.inspect()[0].selector, els[0].selector)

    def test_toggle_slider_roundtrip_and_reset(self):
        f = self.project.features[0]
        c = self.project.controls[0]
        self.a.set_feature_state(f, "on")
        self.assertEqual(self.a.get_feature_state(f), "on")
        self.a.set_control(c, 77)
        self.assertEqual(self.a.get_control(c), 77)
        self.a.reset(self.project)
        self.assertEqual(self.a.get_feature_state(f), "off")
        self.assertEqual(self.a.get_control(c), 50)
        self.assertEqual(self.a.health().status, "ok")

    def test_dangerous_feature_refused(self):
        from overgrind.types import HarnessError
        with self.assertRaises(HarnessError):
            self.a.invoke(self.project.features[-1])

    def test_crash_button_detected(self):
        from overgrind.types import FeatureDef, Selector
        btn = FeatureDef(id="btn_crash", kind="action", states=(), default_state="",
                         selector=Selector(auto_id="btn_crash", control_type="Button"))
        self.a.invoke(btn)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.a.health().status == "ok":
            time.sleep(0.1)
        self.assertEqual(self.a.health().status, "dead")

    def test_hang_button_detected(self):
        from overgrind.types import FeatureDef, Selector
        btn = FeatureDef(id="btn_hang", kind="action", states=(), default_state="",
                         selector=Selector(auto_id="btn_hang", control_type="Button"))
        self.a.invoke(btn)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.a.health().status == "ok":
            time.sleep(0.2)
        self.assertEqual(self.a.health().status, "hung")

    def test_modal_button_detected(self):
        from overgrind.types import FeatureDef, Selector
        btn = FeatureDef(id="btn_modal", kind="action", states=(), default_state="",
                         selector=Selector(auto_id="btn_modal", control_type="Button"))
        self.a.invoke(btn)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.a.health().status == "ok":
            time.sleep(0.1)
        self.assertEqual(self.a.health().status, "modal")


@unittest.skipUnless(WIN, "Windows UI Automation smoke tests")
class EndToEnd(unittest.TestCase):
    def _run(self, bug: str, seconds: float = 25.0):
        from overgrind.controller import RunController
        from overgrind.types import AdapterSpec, StopConditions
        with tempfile.TemporaryDirectory() as d:
            ctl = RunController(_project(bug), AdapterSpec("overgrind.adapters.uia:UiaAdapter"), d,
                                stop=StopConditions(max_seconds=seconds))
            ctl.start()
            self.assertTrue(ctl.join(seconds + 30))
            from overgrind import evidence
            return ctl.stats(), evidence.EvidenceStore(d).list()

    def test_combo_crash_is_one_deduped_event_and_run_continues(self):
        st, events = self._run("combo:ab")
        crashes = [e for e in events if e.category == "crash"]
        self.assertEqual(len(crashes), 1)
        self.assertGreater(st.crashes, 0)
        self.assertGreater(st.cases, st.crashes)

    def test_combo_hang(self):
        st, events = self._run("combo:ab:hang")
        self.assertEqual(len([e for e in events if e.category == "hang"]), 1)

    def test_combo_modal(self):
        st, events = self._run("combo:ab:modal")
        self.assertEqual(len([e for e in events if e.category == "dialog"]), 1)


if __name__ == "__main__":
    unittest.main()
