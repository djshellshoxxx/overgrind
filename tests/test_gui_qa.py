"""QA-GUI regression tests: each test pins a defect found while driving the real app."""
import ast
import dataclasses as dc
import json
import os
import sys
import tempfile
import time
import unittest

import test_gui_common as gs
from test_gui_common import GuiCase, pump

if gs.HAVE_QT:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QMessageBox
    from overgrind.gui import backend
    from overgrind.gui.app import MainWindow, suggest_filename
    from overgrind.gui.model import ProjectModel, empty_project
    from overgrind.gui.sample import DEMO_ADAPTER, demo_project
    from overgrind.gui.workers import RunSession, run_async
    from overgrind.types import (
        AdapterSpec, Case, ElementInfo, ErrorEvent, FeatureDef, Selector,
    )

GUI_DIR = os.path.join(os.path.dirname(__file__), "..", "overgrind", "gui")


def demo_window():
    w = MainWindow()
    w.model.demo = True
    w.model.adapter_spec = DEMO_ADAPTER
    w.model.load_project(demo_project())
    w.show()
    pump(lambda: not w.run_page._blocking(), 3)
    return w


class ModelDirtyUndo(GuiCase):
    def test_undo_back_to_saved_state_is_clean(self):
        m = ProjectModel()
        m.mark_saved("/tmp/x.ovg.json")
        m.rename("changed")
        self.assertTrue(m.dirty)
        m.undo()
        self.assertFalse(m.dirty, "undoing back to the saved project must clear the dirty flag")
        m.redo()
        self.assertTrue(m.dirty)

    def test_redo_after_save_marks_dirty_and_save_point_survives(self):
        m = ProjectModel()
        m.rename("a")
        m.mark_saved("/tmp/x.ovg.json")
        m.rename("b")
        m.undo()
        self.assertFalse(m.dirty)
        m.undo()
        self.assertTrue(m.dirty)

    def test_typing_edits_merge_into_one_undo_step(self):
        m = ProjectModel()
        before = m.project.run.seed
        for v in (5, 52, 523):
            m.edit("Change seed", lambda p, v=v: dc.replace(p, run=dc.replace(p.run, seed=v)), merge="seed")
        self.assertEqual(m.project.run.seed, 523)
        m.undo()
        self.assertEqual(m.project.run.seed, before)
        self.assertFalse(m.can_undo())

    def test_different_merge_keys_do_not_merge(self):
        m = ProjectModel()
        m.edit("a", lambda p: dc.replace(p, name="a"), merge="x")
        m.edit("b", lambda p: dc.replace(p, name="b"), merge="y")
        m.undo()
        self.assertEqual(m.project.name, "a")


class RunSessionLifecycle(GuiCase):
    def test_unwritable_data_dir_does_not_wedge_the_session(self):
        blocker = tempfile.NamedTemporaryFile(delete=False)
        blocker.close()
        old = os.environ["OVERGRIND_DATA"]
        os.environ["OVERGRIND_DATA"] = blocker.name  # a FILE where a directory is needed
        try:
            s = RunSession()
            problems = []
            s.problem.connect(problems.append)
            s.start(demo_project(), DEMO_ADAPTER, __import__("overgrind.types", fromlist=["StopConditions"]).StopConditions())
            pump(lambda: s.state != "starting", 3)
            self.assertEqual(s.state, "idle")
            self.assertTrue(problems, "the tester must be told why the run did not start")
            self.assertIn("evidence", problems[0].lower())
        finally:
            os.environ["OVERGRIND_DATA"] = old
            os.unlink(blocker.name)

    def test_shutdown_while_starting_does_not_leak_a_running_controller(self):
        from overgrind.types import StopConditions
        s = RunSession()
        made = []
        orig = backend.new_controller

        def slow(project, spec, d, stop=None):
            time.sleep(0.4)
            c = orig(project, spec, d, stop)
            made.append(c)
            return c
        backend.new_controller = slow
        try:
            s.start(demo_project(), DEMO_ADAPTER, StopConditions())
            self.assertEqual(s.state, "starting")
            s.shutdown()
            pump(lambda: bool(made), 5)
            pump(timeout=1.5)
            self.assertEqual(s.state, "idle")
            self.assertIsNone(s.controller)
            self.assertTrue(made)
            self.assertNotIn(made[0].state, ("running", "paused"), "a controller started after shutdown was left running")
        finally:
            backend.new_controller = orig

    def test_start_rechecks_validation_synchronously(self):
        w = demo_window()
        w.model.edit("x", lambda p: dc.replace(p, features=()))  # banner debounce (150 ms) has not run yet
        w.run_page.start()
        self.assertEqual(w.session.state, "idle", "a stale validation result let an invalid project start")
        w.close()

    def test_close_while_running_does_not_block_the_ui_thread(self):
        w = demo_window()
        w.run_page.start()
        self.assertTrue(pump(lambda: w.session.state == "running", 20))
        slow = []
        c = w.session.controller
        orig_stop = c.stop

        def stop(*a, **k):
            slow.append(1)
            time.sleep(0.8)
            return orig_stop(*a, **k)
        c.stop = stop
        t0 = time.time()
        w.close()
        blocked = time.time() - t0
        self.assertLess(blocked, 0.5, "closeEvent called controller.stop() on the GUI thread")
        pump(lambda: not w.isVisible(), 15)
        pump(timeout=1.0)
        self.assertFalse(w.isVisible())


class InspectEscaping(GuiCase):
    def test_element_names_with_markup_are_shown_literally(self):
        w = demo_window()
        ip = w.pages[1]
        evil = "<b>Bold</b> &amp; <i>x</i>"
        sel = Selector(auto_id="evil", title=evil, control_type="CheckBox", path=("A",))
        el = ElementInfo(sel, evil, "CheckBox", 0, None, (0, 0, 1, 1), None, (), "boolean", backend.Domain("boolean"), "verified")
        w.go_to(1)
        ip._scanned([el])
        ip.tree.setCurrentItem(ip.tree.topLevelItem(0))
        ip._add_feature()
        self.assertNotIn("<b>Bold</b>", ip.msg.text().replace("<b>" + evil + "</b>", ""))
        self.assertIn("&lt;b&gt;Bold&lt;/b&gt;", ip.msg.text())
        choice = ElementInfo(sel, "Mode", "ComboBox", 0, None, (0, 0, 1, 1), None, (), "choice",
                             backend.Domain("choice", options=("<i>one</i>", "two")), "verified")
        ip._scanned([choice])
        ip.tree.setCurrentItem(ip.tree.topLevelItem(0))
        self.assertIn("&lt;i&gt;one&lt;/i&gt;", ip.d_suggest.text())
        w.close()


class ControlsEditing(GuiCase):
    def test_numeric_editor_cannot_leave_the_original_range_or_invert(self):
        w = demo_window()
        cp = w.pages[3]
        w.go_to(3)
        cp.table.selectRow(0)  # sld_volume 0..100, testing 10..90
        pump(timeout=0.1)
        cp.n_max.setValue(500)
        self.assertLessEqual(cp.n_max.value(), 100)
        cp.n_min.setValue(80)
        cp.n_max.setValue(20)
        c = w.model.control("sld_volume")
        fz = c.fuzz_domain or c.domain
        self.assertLessEqual(fz.minimum, fz.maximum)
        pump(timeout=0.4)
        self.assertEqual([i for i in w.issues if i[0] == "fail" and i[2] == 3 and "subset" in i[1].lower()], [])
        w.close()


class ChromeAndFiles(GuiCase):
    def test_long_target_name_cannot_widen_the_window(self):
        w = demo_window()
        w.model.set_target(dc.replace(w.model.project.target, process_name="x" * 400 + ".exe"))
        pump(timeout=0.2)
        self.assertLessEqual(w.minimumSizeHint().width(), 900)
        self.assertLessEqual(w.statusBar().sizeHint().width(), 900)
        w.close()

    def test_suggest_filename_is_valid_on_windows(self):
        s = suggest_filename('My: "app" / v2?*<>|  ')
        for ch in '<>:"/\\|?*':
            self.assertNotIn(ch, s)
        self.assertTrue(s.endswith(".ovg.json"))
        self.assertEqual(suggest_filename(""), "project.ovg.json")
        self.assertTrue(suggest_filename("CON").lower() != "con.ovg.json")

    def test_recent_list_drops_missing_files_and_survives_odd_paths(self):
        w = demo_window()
        d = tempfile.mkdtemp(prefix="ovg odd,dir ")
        good = os.path.join(d, "a, b.ovg.json")
        backend.save_project(demo_project(), good)
        w._add_recent(good)
        w._add_recent(r"C:\Users\Dana Q\proj,1.ovg.json")
        rec = w._recent()
        self.assertIn(good, rec)
        self.assertIn(r"C:\Users\Dana Q\proj,1.ovg.json", rec)
        # opening a file that vanished tells the user once and forgets it
        shown = []
        orig = QMessageBox.warning
        QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[1]))
        try:
            self.assertFalse(w.open_path(r"C:\Users\Dana Q\proj,1.ovg.json"))
        finally:
            QMessageBox.warning = orig
        self.assertTrue(shown)
        self.assertNotIn(r"C:\Users\Dana Q\proj,1.ovg.json", w._recent())
        w.close()

    def test_recent_dedupes_slash_variants(self):
        w = demo_window()
        w._add_recent("/tmp/a/b.ovg.json")
        w._add_recent("/tmp/a//b.ovg.json")
        self.assertEqual(len(w._recent()), 1)
        w.close()

    def test_unicode_and_rtl_names_roundtrip_through_save_and_load(self):
        w = demo_window()
        name = "בדיקה \u202e tést 测试 😀"
        w.model.rename(name)
        path = os.path.join(tempfile.mkdtemp(), "ü ñ.ovg.json")
        self.assertTrue(w._save_to(path))
        w2 = demo_window()
        self.assertTrue(w2.open_path(path))
        self.assertEqual(w2.model.project.name, name)
        self.assertFalse(w2.model.dirty)
        w.close(); w2.close()


class TargetPageInput(GuiCase):
    def test_pasted_quoted_exe_path_is_cleaned(self):
        w = MainWindow(); w.show()
        tp = w.pages[0]
        tp.r_exe.setChecked(True)
        tp.exe_path.setText(r'  "C:\Program Files\My App\app.exe"  ')
        tp._exe_changed()
        self.assertEqual(w.model.project.target.exe_path, r"C:\Program Files\My App\app.exe")
        self.assertEqual(w.model.project.target.process_name, "app.exe")
        w.close()

    def test_title_with_backslash_displays_literally(self):
        w = MainWindow(); w.show()
        tp = w.pages[0]
        tp._windows = [{"pid": 5, "process": "a.exe", "title": r"C:\temp\file - Editor", "path": "", "arch": "64-bit"}]
        tp._fill()
        tp.table.selectRow(0)
        pump(timeout=0.1)
        self.assertEqual(tp.v_title.text(), r"C:\temp\file - Editor")
        w.close()


class WindowsProcessList(unittest.TestCase):
    """The ctypes code only runs on Windows, so the filtering policy is a pure function and the
    ctypes declarations are checked structurally."""

    def test_listing_policy(self):
        k = backend.window_listable
        base = dict(visible=True, cloaked=False, owned=False, toolwindow=False, title="Notepad", pid=10, own_pid=1)
        self.assertTrue(k(**base))
        for change in ({"visible": False}, {"cloaked": True}, {"owned": True}, {"toolwindow": True},
                       {"title": ""}, {"title": "   "}, {"pid": 1}, {"pid": 0}):
            self.assertFalse(k(**{**base, **change}), change)
        self.assertTrue(k(**{**base, "owned": True, "toolwindow": False, "title": "Dlg"}) is False)

    def test_win32_code_declares_argtypes_and_restype_for_64bit_handles(self):
        src = open(os.path.join(GUI_DIR, "backend.py"), encoding="utf-8").read()
        for fn in ("OpenProcess", "CloseHandle", "QueryFullProcessImageNameW", "EnumWindows", "IsWindowVisible",
                   "GetWindowTextW", "GetWindowThreadProcessId", "GetWindowTextLengthW"):
            self.assertRegex(src, rf"\b{fn}\.argtypes\b", f"{fn} needs argtypes (64-bit handles are truncated otherwise)")
        self.assertRegex(src, r"OpenProcess\.restype")
        self.assertIn("use_last_error", src)
        self.assertIn("32768", src, "image path buffer must hold long paths")

    def test_no_fake_windows_on_windows_failure(self):
        src = open(os.path.join(GUI_DIR, "backend.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "list_windows")
        self.assertNotIn("DEMO_WINDOWS", ast.get_source_segment(src, fn).split("win32")[-1],
                         "a failing EnumWindows must raise, not show demo windows as if they were real")


class PyInstallerFriendly(unittest.TestCase):
    def test_every_dynamically_imported_backend_module_is_statically_visible(self):
        src = open(os.path.join(GUI_DIR, "backend.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        dynamic = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") in ("_fn", "_mod") and n.args:
                a = n.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    dynamic.add(a.value)
        dynamic |= {"adapters.mock", "adapters.uia"}
        static = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.level == 2:
                base = n.module + "." if n.module else ""
                static |= {base + a.name for a in n.names}
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.startswith("overgrind."):
                        static.add(a.name[len("overgrind."):])
        missing = {d for d in dynamic if d not in static}
        self.assertFalse(missing, f"PyInstaller cannot see: {sorted(missing)} (add static imports behind `if False:`)")

    def test_entry_points(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        self.assertIn("freeze_support", open(os.path.join(root, "packaging", "gui_entry.py")).read())
        self.assertIn("freeze_support", open(os.path.join(GUI_DIR, "app.py")).read())
        toml = open(os.path.join(root, "pyproject.toml")).read()
        self.assertIn('overgrind-gui = "overgrind.gui.app:main"', toml)
        import subprocess
        r = subprocess.run([sys.executable, "-m", "overgrind.gui", "--help"], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, cwd=root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--demo", r.stdout)


class WorkerCleanup(GuiCase):
    def test_failed_task_without_handler_is_not_silent(self):
        from overgrind.gui import workers
        seen = []
        workers.HOST.unhandled.connect(seen.append)
        run_async(lambda: 1 / 0)
        pump(lambda: bool(seen), 3)
        self.assertTrue(seen and "ZeroDivisionError" in seen[0])

    def test_friendly_reason_is_single_line_and_short(self):
        from overgrind.gui.page_run import friendly_reason
        r = friendly_reason("controller error: RuntimeError: line one\n\n    line two\n" + "x" * 500)
        self.assertNotIn("\n", r)
        self.assertLess(len(r), 330)


class PerformanceWithManyRows(GuiCase):
    def _events(self, n):
        return [ErrorEvent(id=f"e{i}", ts=f"2026-01-01T{i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00:00", run_id="r",
                           category=["crash", "hang", "dialog"][i % 3], signature=f"sig{i:05d}", message=f"message {i} " + "long " * 10,
                           case=Case(0, {"cb_alpha": "on"}, {}), history=(), count=i % 7 + 1, reliability="reliable") for i in range(n)]

    def test_five_thousand_errors_fill_fast_and_refresh_incrementally(self):
        w = demo_window(); ep = w.errors_page
        w.go_to(6)
        evs = self._events(5000)
        t = time.time(); ep._loaded(evs); first = time.time() - t
        self.assertLess(first, 1.2, "filling 5,000 errors blocks the UI thread")
        evs2 = evs + self._events(5010)[5000:]
        t = time.time(); ep._loaded(evs2); again = time.time() - t
        self.assertLess(again, 0.3, "a reload with 10 new errors must not rebuild every row")
        self.assertEqual(ep.sm.rowCount(), 5010)
        w.close()

    def test_five_thousand_elements_scan_and_filter(self):
        w = demo_window(); ip = w.pages[1]
        els = [ElementInfo(Selector(auto_id=f"id{i}", control_type="Button", path=("A",)), f"Element {i}", "Button", 0 if i == 0 else 1,
                           None if i == 0 else 0, (0, 0, 10, 10), None, (), "action", None, "unknown") for i in range(5000)]
        w.go_to(1)
        t = time.time(); ip._scanned(els); pump(timeout=0.01)
        self.assertLess(time.time() - t, 2.0)
        t = time.time(); ip.search.setText("Element 49"); pump(timeout=0.01)
        self.assertLess(time.time() - t, 0.5)
        w.close()


class AtlasRobustness(GuiCase):
    def test_atlas_wraps_many_states_instead_of_clipping(self):
        from overgrind.gui.widgets import Atlas
        a = Atlas()
        a.resize(500, 200)
        a.set_features([("f", "Feature with a rather long display name", tuple(f"state number {i}" for i in range(30)))])
        a.set_visits({})
        self.assertGreater(a.sizeHint().height(), 100)
        self.assertGreater(a.heightForWidth(500), 2 * a.ROW_H)
        self.assertTrue(a.cell_at(a.rect().center()) is None or isinstance(a.cell_at(a.rect().center()), tuple))

    def test_atlas_marks_states_where_an_error_happened(self):
        from overgrind.gui.widgets import Atlas
        a = Atlas()
        a.set_features([("f", "F", ("off", "on"))])
        a.set_visits({("f", "on"): 3})
        a.set_errored({("f", "on")})
        self.assertEqual(a.errored(), {("f", "on")})
        self.assertIn("error", a.accessibleDescription().lower())


class WideFontLayout(GuiCase):
    def test_no_horizontal_overflow_at_min_size_with_wide_fonts(self):
        from PySide6.QtGui import QFont
        app = QApplication.instance()
        old = app.font()
        try:
            for fam, pt in (("DejaVu Sans", 12), ("DejaVu Sans Mono", 11), ("Segoe UI", 9)):
                app.setFont(QFont(fam, pt))
                w = demo_window(); w.resize(900, 600)
                for i in range(7):
                    w.go_to(i); pump(timeout=0.1)
                    self.assertEqual(w.pages[i].scroll.horizontalScrollBar().maximum(), 0, f"{fam} {pt}: overflow on page {i}")
                w.close()
        finally:
            app.setFont(old)

    def test_window_lister_hides_shell_windows(self):
        self.assertIn("Progman", backend._SHELL_CLASSES)


class PreflightGate(GuiCase):
    def test_failed_preflight_blocks_start_until_name_is_typed(self):
        w = demo_window()
        rp = w.run_page
        w.model.preflight = {"fingerprint": w.model.fingerprint(), "rows": [
            {"element": "control sld_volume", "status": "fail", "message": "ignores writes", "hint": ""}]}
        w._recheck()
        asked = []
        rp._confirm_name = lambda title, text, expected: (asked.append((title, expected)) or False)
        rp.start()
        self.assertEqual(w.session.state, "idle")
        self.assertTrue(asked and asked[0][1] == w.model.project.name)
        w.close()

    def test_stale_preflight_does_not_block(self):
        w = demo_window()
        w.model.preflight = {"fingerprint": "old", "rows": [{"element": "x", "status": "fail", "message": "m", "hint": ""}]}
        self.assertEqual(w.run_page.preflight_blockers(), [])
        w.close()

    def test_enabled_dangerous_feature_needs_review_before_start(self):
        w = demo_window()
        w.model.update_feature("btn_delete", enabled=True)
        pump(timeout=0.3)
        asked = []
        w.run_page._confirm_name = lambda title, text, expected: (asked.append(text) or False)
        w.run_page.start()
        self.assertEqual(w.session.state, "idle")
        self.assertTrue(asked and "Delete all data" in asked[0])
        w.close()


if __name__ == "__main__":
    unittest.main()
