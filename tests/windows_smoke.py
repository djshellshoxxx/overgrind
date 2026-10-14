"""Windows-only end-to-end smoke tests against fixtures/synthetic_app.py (skipped elsewhere).

Run on Windows CI (interactive desktop):  python -X utf8 tests/windows_smoke.py

Self-diagnosing: on any failure the UIA tree, process list, session/screen facts and versions are
written to $SMOKE_DIAG_DIR (default ./smoke-diag, small text files only; the workflow uploads it).
A global faulthandler timeout and an atexit sweep make sure a wedged test or leaked fixture process
can never stall the CI job. Exit code is non-zero on any failure/error.
"""
from __future__ import annotations

import atexit
import faulthandler
import functools
import os
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = str(ROOT / "fixtures" / "synthetic_app.py")
WIN = sys.platform == "win32"
DIAG = Path(os.environ.get("SMOKE_DIAG_DIR", "smoke-diag"))
GLOBAL_TIMEOUT_S = int(os.environ.get("SMOKE_TIMEOUT_S", "900"))

if WIN:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- diagnostics
def _write(name: str, text: str) -> None:
    try:
        DIAG.mkdir(parents=True, exist_ok=True)
        (DIAG / name).write_text(text[:200_000], encoding="utf-8", errors="replace")
    except OSError:
        pass


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
        return (r.stdout or "") + (r.stderr or "")
    except Exception as exc:                      # diagnostics must never raise
        return f"<{cmd[0]} failed: {exc!r}>"


def environment_facts() -> str:
    import ctypes
    k32, u32 = ctypes.WinDLL("kernel32"), ctypes.WinDLL("user32")
    sid = ctypes.c_ulong()
    k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    facts = [f"python {sys.version}", f"session_id={sid.value} (0 = services/non-interactive)",
             f"screen={u32.GetSystemMetrics(0)}x{u32.GetSystemMetrics(1)}",
             f"foreground_hwnd={u32.GetForegroundWindow()}"]
    for mod in ("pywinauto", "comtypes", "PySide6"):
        try:
            m = __import__(mod)
            facts.append(f"{mod} {getattr(m, '__version__', '?')}")
        except Exception as exc:
            facts.append(f"{mod}: import failed {exc!r}")
    try:
        from overgrind.adapters import procwatch
        facts.append(f"elevated={procwatch.is_elevated()}")
    except Exception as exc:
        facts.append(f"elevated: {exc!r}")
    return "\n".join(facts)


def dump_diag(tag: str, adapter=None) -> None:
    """UIA tree + processes + environment. Best effort, bounded."""
    _write(f"{tag}-env.txt", environment_facts())
    _write(f"{tag}-processes.txt", _run(["tasklist", "/V", "/FO", "CSV"]))
    if adapter is not None:
        try:
            lines = []
            for e in adapter.inspect():
                s = e.selector
                lines.append(f"{'  ' * e.depth}{e.control_type or '?'} name={e.name!r} auto_id={s.auto_id!r} "
                             f"class={s.class_name!r} pats={list(e.patterns)} bounds={e.bounds}")
            _write(f"{tag}-uia-tree.txt", "\n".join(lines))
        except Exception:
            _write(f"{tag}-uia-tree.txt", "inspect failed:\n" + traceback.format_exc())
        try:
            hwnd = adapter._hwnd()
            if hwnd and adapter.screenshot(str(DIAG / f"{tag}.bmp")):
                pass
        except Exception:
            pass


def kill_fixture_processes() -> None:
    """Sweep any synthetic_app.py left running (leaked by a failed test)."""
    if not WIN:
        return
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*synthetic_app.py*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30)


def diag_on_failure(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except BaseException:
            dump_diag(fn.__name__, getattr(self, "a", None))
            raise
    return wrapper


# ---------------------------------------------------------------- project
def _sel(auto_id: str, name: str, ctype: str):
    from overgrind.types import Selector
    # auto_id first; Name+control_type is the adapter's second tier if Qt does not expose AutomationId
    return Selector(auto_id=auto_id, title=name, control_type=ctype)


def _project(bug: str = "", restart: bool = True):
    from overgrind.types import ControlDef, Domain, FeatureDef, Project, RunConfig, TargetRef
    args = (APP,) + (("--bug", bug) if bug else ())
    feats = tuple(FeatureDef(id=f"cb_{k}", name=f"Option {k.upper()}",
                             selector=_sel(f"cb_{k}", f"Option {k.upper()}", "CheckBox"))
                  for k in "abcd")
    feats += (FeatureDef(id="btn_delete", kind="action", states=(), default_state="", dangerous=True,
                         selector=_sel("btn_delete", "Delete all data", "Button")),)
    ctrls = (ControlDef("volume", "integer", _sel("sld_volume", "Volume", "Slider"),
                        Domain("integer", 0, 100, 1), default=50),)
    run = RunConfig(restart_on_crash=restart, capture_screenshot=True, settle_ms=20,
                    op_timeout_ms=8000, case_timeout_ms=30000, hang_threshold_ms=3000)
    return Project("smoke", TargetRef(kind="exe", exe_path=sys.executable, args=("-X", "utf8") + args),
                   feats, ctrls, run=run)


def _button(aid: str, name: str):
    from overgrind.types import FeatureDef
    return FeatureDef(id=aid, kind="action", states=(), default_state="",
                      selector=_sel(aid, name, "Button"))


def _boxes(els):
    # AutomationId is "cb_a" (Qt >= 6.8 identifier) or "main_window.cb_a" (older Qt path form)
    return [e for e in els if e.selector.control_type == "CheckBox"
            and e.selector.auto_id.rsplit(".", 1)[-1].startswith("cb_")]


def _wait_for(pred, timeout_s: float, step: float = 0.1):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


@unittest.skipUnless(WIN, "Windows UI Automation smoke tests")
class UiaSmoke(unittest.TestCase):
    def setUp(self):
        from overgrind.adapters.uia import UiaAdapter
        self.project = _project()
        self.a = UiaAdapter(launch_timeout_s=40)
        self.addCleanup(self._cleanup)
        self.a.attach(self.project.target)
        # Qt only builds its UIA tree once a client asks; poll until the controls are visible.
        if not _wait_for(lambda: len(_boxes(self.a.inspect())) == 4, 20):
            dump_diag("setup", self.a)
            self.fail("fixture controls never appeared in the UIA tree (see smoke-diag/setup-*)")

    def _cleanup(self):
        try:
            self.a.detach(kill=True)
        finally:
            kill_fixture_processes()

    @diag_on_failure
    def test_inspect_is_readonly_and_verified(self):
        els = self.a.inspect()
        boxes = _boxes(els)
        self.assertEqual(len(boxes), 4)
        self.assertTrue(all(e.confidence == "verified" and e.suggested_kind == "boolean" for e in boxes))
        vol = next(e for e in els if e.selector.control_type == "Slider" and e.name == "Volume")
        self.assertEqual((vol.suggested_domain.minimum, vol.suggested_domain.maximum), (0, 100))
        self.assertEqual(self.a.inspect()[0].selector, els[0].selector)

    @diag_on_failure
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

    @diag_on_failure
    def test_dangerous_feature_refused(self):
        from overgrind.types import HarnessError
        with self.assertRaises(HarnessError):
            self.a.invoke(self.project.features[-1])

    @diag_on_failure
    def test_crash_button_detected(self):
        self.a.invoke(_button("btn_crash", "Crash now"))
        self.assertTrue(_wait_for(lambda: self.a.health().status != "ok", 15))
        self.assertEqual(self.a.health().status, "dead")

    @diag_on_failure
    def test_hang_button_detected(self):
        self.a.invoke(_button("btn_hang", "Hang now"))
        self.assertTrue(_wait_for(lambda: self.a.health().status != "ok", 20, 0.2))
        self.assertEqual(self.a.health().status, "hung")

    @diag_on_failure
    def test_modal_button_detected(self):
        self.a.invoke(_button("btn_modal", "Show error"))
        self.assertTrue(_wait_for(lambda: self.a.health().status != "ok", 15))
        self.assertEqual(self.a.health().status, "modal")


@unittest.skipUnless(WIN, "Windows UI Automation smoke tests")
class EndToEnd(unittest.TestCase):
    def tearDown(self):
        kill_fixture_processes()

    def _run(self, bug: str, seconds: float = 25.0):
        from overgrind import evidence
        from overgrind.controller import RunController
        from overgrind.types import AdapterSpec, StopConditions
        with tempfile.TemporaryDirectory() as d:
            ctl = RunController(_project(bug), AdapterSpec("overgrind.adapters.uia:UiaAdapter"), d,
                                stop=StopConditions(max_seconds=seconds))
            ctl.start()
            try:
                finished = ctl.join(seconds + 30)
            finally:
                if ctl.state() != "finished":
                    ctl.stop()
            if not finished:
                dump_diag("e2e-" + bug.replace(":", "_"))
            self.assertTrue(finished, "run did not finish in time")
            return ctl.stats(), evidence.EvidenceStore(d).list()

    def test_combo_crash_is_one_deduped_event_and_run_continues(self):
        st, events = self._run("combo:ab")
        crashes = [e for e in events if e.category == "crash"]
        self.assertEqual(len(crashes), 1, [e.category for e in events])
        self.assertGreater(st.crashes, 0)
        self.assertGreater(st.cases, st.crashes)

    def test_combo_hang(self):
        st, events = self._run("combo:ab:hang")
        self.assertEqual(len([e for e in events if e.category == "hang"]), 1, [e.category for e in events])

    def test_combo_modal(self):
        st, events = self._run("combo:ab:modal")
        self.assertEqual(len([e for e in events if e.category == "dialog"]), 1, [e.category for e in events])


def main() -> int:
    if not WIN:
        print("windows_smoke: not Windows, skipped")
        return 0
    faulthandler.dump_traceback_later(GLOBAL_TIMEOUT_S, exit=True)    # hard stop for wedged runs
    atexit.register(kill_fixture_processes)
    DIAG.mkdir(parents=True, exist_ok=True)
    _write("start-env.txt", environment_facts())
    prog = unittest.main(module=__name__, argv=[sys.argv[0], "-v"], exit=False)
    ok = prog.result.wasSuccessful() and prog.result.testsRun > 0
    print("SMOKE", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
