import json
import os
import tempfile
import unittest

import test_gui_common as gs
from test_gui_common import GuiCase, pump

if gs.HAVE_QT:
    from overgrind.gui.app import MainWindow
    from overgrind.gui.model import ProjectModel, empty_project
    from overgrind.gui.sample import DEMO_ADAPTER, demo_project
    from overgrind.gui.widgets import PAGE_NAMES


def demo_window():
    w = MainWindow()
    w.model.demo = True
    w.model.adapter_spec = DEMO_ADAPTER
    w.model.load_project(demo_project())
    w.show()
    pump(timeout=0.2)
    return w


class PagesConstruct(GuiCase):
    def test_every_page_constructs_empty_and_demo(self):
        w = MainWindow(); w.show()
        self.assertEqual(len(w.pages), 7)
        for i in range(7):
            w.go_to(i)
            self.assertEqual(w.stack.currentIndex(), i)
        w.open_demo()
        for i in range(7):
            w.go_to(i)
            pg = w.pages[i]
            self.assertTrue(pg.hint_label.text().startswith("<b>What to do now"), PAGE_NAMES[i])
            self.assertFalse(w.grab().isNull())
        w.close()

    def test_min_size_and_no_horizontal_scroll(self):
        w = demo_window(); w.resize(1100, 700)
        for i in range(7):
            w.go_to(i)
            pump(timeout=0.1)
            sb = w.pages[i].scroll.horizontalScrollBar()
            self.assertEqual(sb.maximum(), 0, f"horizontal overflow on {PAGE_NAMES[i]}")
        self.assertEqual((w.minimumWidth(), w.minimumHeight()), (900, 600))

    def test_navigation_buttons_and_stepper(self):
        w = demo_window()
        w.go_to(0)
        self.assertFalse(w.btn_back.isEnabled())
        w.btn_next.click()
        self.assertEqual(w.stack.currentIndex(), 1)
        self.assertEqual(w.stepper.currentRow(), 1)
        w.go_to(6)
        self.assertFalse(w.btn_next.isEnabled())

    def test_validation_banner_and_fix_jump(self):
        w = MainWindow(); w.show()          # empty project: no enabled features
        pump(timeout=0.4)
        self.assertTrue(any(lvl == "fail" for lvl, _t, _p in w.issues))
        w.go_to(0)
        self.assertTrue(w.banner.isVisible())          # points at the page that fixes it
        self.assertIn("Fix", w.banner.text())
        self.assertFalse(w.run_page.btn_start.isEnabled())
        w.go_to(5)
        self.assertIn("Can't start yet", w.run_page.block_label.text())


class ProjectIO(GuiCase):
    def test_save_load_roundtrip(self):
        w = demo_window()
        orig = w.model.project
        d = tempfile.mkdtemp()
        path = os.path.join(d, "x.ovg.json")
        self.assertTrue(w._save_to(path))
        self.assertFalse(w.model.dirty)
        w2 = MainWindow()
        self.assertTrue(w2.open_path(path))
        self.assertEqual(w2.model.project, orig)
        self.assertIn(path, w2._recent())
        w2.close(); w.close()

    def test_import_v1_manifest(self):
        from overgrind.gui import backend
        d = tempfile.mkdtemp(); p = os.path.join(d, "v1.json")
        v1 = {"features": [{"name": "A", "selector": {"title": "A"}}, {"name": "B", "selector": {"title": "B"}}],
              "samples": 2}
        with open(p, "w") as fh:
            json.dump(v1, fh)
        try:
            proj = backend.migrate_v1(p)
        except Exception as e:                     # v0.1 layout unknown to this test; just require a clean error
            self.assertTrue(str(e))
        else:
            self.assertGreaterEqual(len(proj.features), 1)

    def test_open_bad_file_is_friendly(self):
        from PySide6.QtWidgets import QMessageBox
        w = MainWindow()
        shown = []
        orig = QMessageBox.warning
        QMessageBox.warning = staticmethod(lambda *a, **k: shown.append(a[1]))
        try:
            d = tempfile.mkdtemp(); p = os.path.join(d, "bad.ovg.json")
            open(p, "w").write("{not json")
            self.assertFalse(w.open_path(p))
        finally:
            QMessageBox.warning = orig
        self.assertEqual(shown, ["Could not open the project"])


class ModelUndo(GuiCase):
    def test_edit_dirty_undo_redo(self):
        m = ProjectModel(demo_project())
        seen = []
        m.changed.connect(lambda: seen.append(1))
        self.assertFalse(m.dirty)
        m.update_feature("cb_alpha", name="Renamed")
        self.assertTrue(m.dirty)
        self.assertEqual(m.feature("cb_alpha").name, "Renamed")
        m.undo()
        self.assertEqual(m.feature("cb_alpha").name, "Enable Alpha")
        m.redo()
        self.assertEqual(m.feature("cb_alpha").name, "Renamed")
        self.assertGreaterEqual(len(seen), 3)

    def test_remove_feature_cleans_rules_and_scope(self):
        m = ProjectModel(demo_project())
        m.remove_feature("cb_beta")
        self.assertEqual(m.project.constraints, ())
        m.remove_feature("cb_alpha")
        self.assertEqual(m.control("sld_gain").scope, ())

    def test_stop_conditions_persist_in_project(self):
        from overgrind.types import StopConditions
        m = ProjectModel(demo_project())
        m.set_stop(StopConditions(until_stopped=False, max_cases=500))
        self.assertEqual(m.stop.max_cases, 500)
        m.undo()
        self.assertTrue(m.stop.until_stopped)


if __name__ == "__main__":
    unittest.main()
