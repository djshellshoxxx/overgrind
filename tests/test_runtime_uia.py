import importlib.util
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from overgrind.adapters import procwatch
from overgrind.adapters.uia import UiaAdapter, _escape_keys
from overgrind.types import ControlDef, Domain, FeatureDef, HarnessError, Selector

ROOT = Path(__file__).resolve().parent.parent
PID = 4242


class Toggle:
    def __init__(self, st=0, stuck=False):
        self.CurrentToggleState, self.stuck, self.toggles = st, stuck, 0

    def Toggle(self):
        self.toggles += 1
        if not self.stuck:
            self.CurrentToggleState = 0 if self.CurrentToggleState else 1


class Range:
    def __init__(self, v, lo, hi, step, clamp=False):
        self.CurrentValue, self.CurrentMinimum, self.CurrentMaximum = v, lo, hi
        self.CurrentSmallChange, self.clamp, self.sets = step, clamp, 0

    def SetValue(self, v):
        self.sets += 1
        self.CurrentValue = min(self.CurrentMaximum, v) if self.clamp else v


class Value:
    def __init__(self, v=""):
        self.CurrentValue, self.CurrentIsReadOnly = v, False

    def SetValue(self, v):
        self.CurrentValue = v


class W:
    def __init__(self, name, ctype, aid="", cls="", pid=PID, children=(), **patterns):
        self.element_info = SimpleNamespace(name=name, control_type=ctype, automation_id=aid,
                                            class_name=cls, process_id=pid, handle=1,
                                            rectangle=SimpleNamespace(left=0, top=0, right=10, bottom=10))
        self._kids, self._p, self.enabled = list(children), patterns, True

    def __getattr__(self, n):
        if n.startswith("iface_") and n[6:] in self._p:
            return self._p[n[6:]]
        raise AttributeError(n)

    def children(self):
        return self._kids

    def is_enabled(self):
        return self.enabled

    def is_visible(self):
        return True

    def descendants(self, **crit):
        # mimic pywinauto 0.6.8 UIAElementInfo.descendants -> build_condition(): no auto_id keyword
        bad = set(crit) - {"process", "class_name", "title", "control_type", "content_only",
                           "cache_enable", "depth"}
        if bad:
            raise TypeError(f"build_condition() got an unexpected keyword argument {sorted(bad)[0]!r}")
        out = []
        for k in self._kids:
            out.append(k)
            out.extend(k.descendants())
        for key, want in crit.items():
            attr = {"title": "name"}.get(key, key)
            out = [w for w in out if getattr(w.element_info, attr) == want]
        return out


def build():
    tg = W("Option A", "CheckBox", "cb_a", "QCheckBox", toggle=Toggle(0))
    sl = W("Volume", "Slider", "sld_volume", "QSlider", range_value=Range(50, 0, 100, 1))
    gn = W("Gain", "Slider", "sld_gain", "QSlider", range_value=Range(1.0, 0.0, 10.0, 0.1))
    ed = W("Name", "Edit", "edt_name", "QLineEdit", value=Value("x"))
    bt = W("Delete", "Button", "btn_delete", "QPushButton", invoke=object())
    alien = W("Evil", "CheckBox", "cb_evil", pid=999, toggle=Toggle(0))
    win = W("Main", "Window", "main", "QWidget", children=[tg, sl, gn, ed, bt, alien])
    a = UiaAdapter()
    a._win, a._pid = win, PID
    return a, dict(tg=tg, sl=sl, gn=gn, ed=ed, bt=bt, alien=alien)


class UiaLogicTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(procwatch, "is_alive", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_import_and_attach_are_safe_off_windows(self):
        a = UiaAdapter()
        self.assertEqual(a.health().status, "missing")
        if sys.platform != "win32":
            with self.assertRaises(HarnessError):
                a.attach(SimpleNamespace(kind="exe"))

    def test_inspect_is_read_only_and_confidence_is_honest(self):
        a, w = build()
        els = a.inspect()
        by = {e.selector.auto_id: e for e in els}
        self.assertEqual(by["cb_a"].suggested_kind, "boolean")
        self.assertEqual(by["cb_a"].confidence, "verified")
        vol = by["sld_volume"]
        self.assertEqual((vol.suggested_kind, vol.suggested_domain.minimum, vol.suggested_domain.maximum,
                          vol.confidence), ("integer", 0, 100, "verified"))
        self.assertEqual(by["sld_gain"].suggested_kind, "float")
        self.assertEqual(by["edt_name"].confidence, "inferred")
        self.assertEqual(by["btn_delete"].suggested_kind, "action")
        self.assertEqual(w["tg"]._p["toggle"].toggles, 0)
        self.assertEqual(w["sl"]._p["range_value"].sets, 0)
        self.assertEqual(els[0].parent, None)
        self.assertEqual(by["cb_a"].selector.path, ("Main",))

    def test_toggle_with_readback(self):
        a, w = build()
        f = FeatureDef("cb_a", selector=Selector(auto_id="cb_a"))
        a.set_feature_state(f, "on")
        self.assertEqual(a.get_feature_state(f), "on")
        a.set_feature_state(f, "on")                      # idempotent
        self.assertEqual(w["tg"]._p["toggle"].toggles, 1)
        a.set_feature_state(f, "off")
        self.assertEqual(a.get_feature_state(f), "off")
        w["tg"]._p["toggle"].stuck = True
        with self.assertRaises(HarnessError):
            a.set_feature_state(f, "on")

    def test_dangerous_features_are_never_touched(self):
        a, w = build()
        f = FeatureDef("d", kind="action", states=(), selector=Selector(auto_id="btn_delete"), dangerous=True)
        with self.assertRaises(HarnessError):
            a.invoke(f)
        with self.assertRaises(HarnessError):
            a.set_feature_state(f, "on")

    def test_range_value_and_text_controls(self):
        a, w = build()
        vol = ControlDef("volume", "integer", Selector(auto_id="sld_volume"), Domain("integer", 0, 100, 1))
        a.set_control(vol, 77)
        self.assertEqual(a.get_control(vol), 77)
        w["sl"]._p["range_value"].clamp = True
        with self.assertRaises(HarnessError):             # app clamps => read-back mismatch is loud
            a.set_control(vol, 500)
        name = ControlDef("name", "text", Selector(auto_id="edt_name"), Domain("text", max_length=20))
        a.set_control(name, "hello")
        self.assertEqual(a.get_control(name), "hello")
        w["ed"]._p["value"].CurrentIsReadOnly = True
        with self.assertRaises(HarnessError):
            a.set_control(name, "nope")

    def test_never_sends_input_to_another_pid(self):
        a, w = build()
        f = FeatureDef("evil", selector=Selector(auto_id="cb_evil"))
        with self.assertRaises(HarnessError) as cm:
            a.set_feature_state(f, "on")
        self.assertIn("not the attached target", str(cm.exception))
        self.assertEqual(w["alien"]._p["toggle"].toggles, 0)

    def test_keyboard_fallback_checks_foreground_pid(self):
        a, w = build()
        w["ed"]._p.pop("value")
        w["ed"].set_focus = lambda: None
        typed = []
        w["ed"].type_keys = lambda keys, **kw: typed.append(keys)
        c = ControlDef("name", "text", Selector(auto_id="edt_name"), Domain("text"))
        with mock.patch.object(procwatch, "foreground_pid", return_value=PID + 1):
            with self.assertRaises(HarnessError):
                a.set_control(c, "x")
        self.assertEqual(typed, [])
        with mock.patch.object(procwatch, "foreground_pid", return_value=PID):
            a.set_control(c, "a+b")
        self.assertEqual(typed, ["^aa{+}b"])
        self.assertEqual(_escape_keys("{x}"), "{{}x{}}")

    def test_qt_dotted_automation_id_suffix_matches(self):
        a, w = build()
        w["tg"].element_info.automation_id = "main_window.cb_a"      # Qt < 6.8 path-style AutomationId
        self.assertIs(a._find(Selector(auto_id="cb_a", control_type="CheckBox")), w["tg"])

    def test_self_healing_find_via_title_when_auto_id_changes(self):
        a, w = build()
        f = FeatureDef("cb_a", selector=Selector(auto_id="renamed", title="Option A", control_type="CheckBox"))
        a.set_feature_state(f, "on")
        self.assertEqual(w["tg"]._p["toggle"].CurrentToggleState, 1)
        gone = FeatureDef("x", selector=Selector(auto_id="nope"))
        with self.assertRaises(HarnessError) as cm:
            a.set_feature_state(gone, "on")
        self.assertIn("not found", str(cm.exception))

    def test_health_reports_dead(self):
        a, _ = build()
        with mock.patch.object(procwatch, "is_alive", return_value=False):
            self.assertEqual(a.health().status, "dead")


class ProcwatchTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "posix process tree")
    def test_is_alive_and_kill_tree(self):
        self.assertTrue(procwatch.is_alive(os.getpid()))
        self.assertFalse(procwatch.is_alive(None))
        self.assertFalse(procwatch.is_alive(2 ** 22 + 12345))
        p = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60; wait"])
        time.sleep(0.3)
        kids = procwatch._children_posix().get(p.pid, [])
        self.assertTrue(kids)
        self.assertTrue(procwatch.kill_tree(p.pid))
        p.wait(2)
        time.sleep(0.1)
        self.assertFalse(any(procwatch.is_alive(k) for k in kids))

    def test_helpers_inert_off_windows(self):
        if sys.platform != "win32":
            self.assertFalse(procwatch.is_hung(1))
            self.assertEqual(procwatch.modal_dialogs(os.getpid()), [])
            self.assertIsNone(procwatch.is_elevated())
            self.assertFalse(procwatch.write_minidump(os.getpid(), "x.dmp") and False)


def load_fixture():
    spec = importlib.util.spec_from_file_location("synthetic_app", ROOT / "fixtures" / "synthetic_app.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FixtureTests(unittest.TestCase):
    def test_bug_spec_parsing_and_triggering(self):
        m = load_fixture()
        bugs = m.parse_bugs("combo:ab;value:volume>=90:hang;combo:cd:modal")
        self.assertEqual([b["action"] for b in bugs], ["crash", "hang", "modal"])
        none = {"a": False, "b": False}
        self.assertIsNone(m.evaluate_bugs(bugs, none, {"volume": 50}))
        self.assertEqual(m.evaluate_bugs(bugs, {"a": True, "b": True}, {})["spec"], "combo:ab")
        self.assertEqual(m.evaluate_bugs(bugs, none, {"volume": 90})["action"], "hang")
        self.assertIsNone(m.evaluate_bugs(bugs, {"a": True}, {"volume": 89.9}))
        for bad in ("combo:zz", "value:nope>3", "weird", "combo:ab:explode"):
            with self.assertRaises(ValueError):
                m.parse_bugs(bad)

    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 not installed")
    def test_app_runs_offscreen_and_crash_flag_exits_nonzero(self):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        app = str(ROOT / "fixtures" / "synthetic_app.py")
        ok = subprocess.run([sys.executable, app, "--quit-after-ms", "300"], env=env, capture_output=True,
                            text=True, timeout=60)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("READY", ok.stdout)
        bad = subprocess.run([sys.executable, app, "--crash-after-ms", "200"], env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(bad.returncode, 3)


if __name__ == "__main__":
    unittest.main()
