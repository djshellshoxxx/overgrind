import unittest

import test_gui_common as gs
from test_gui_common import GuiCase, pump

if gs.HAVE_QT:
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QMessageBox
    from overgrind.gui.app import MainWindow
    from overgrind.gui.page_run import new_actions
    from overgrind.gui.sample import demo_project
    from overgrind.types import Action, AdapterSpec, StopConditions

FAST_CRASH = AdapterSpec("overgrind.adapters.mock:MockAdapter", {"bad_combo": {"cb_alpha": "on"}, "latency_ms": 0.5})


def window(spec=FAST_CRASH):
    w = MainWindow()
    w.model.demo = True
    w.model.adapter_spec = spec
    w.model.load_project(demo_project())
    w.show()
    pump(lambda: not w.run_page._blocking(), 3)
    return w


class RunLifecycle(GuiCase):
    def test_start_pause_resume_stop(self):
        w = window()
        rp, s = w.run_page, w.session
        self.assertTrue(rp.btn_start.isEnabled())
        self.assertFalse(rp.btn_stop.isEnabled())
        rp.start()
        self.assertTrue(pump(lambda: s.state == "running" and s.last_stats.cases > 0, 20), s.state)
        self.assertFalse(rp.btn_start.isEnabled())
        self.assertTrue(rp.btn_stop.isEnabled())
        rp.toggle_pause()
        self.assertTrue(pump(lambda: s.state == "paused", 10))
        self.assertEqual(rp.btn_pause.text().strip(), "Resume")
        rp.toggle_pause()
        self.assertTrue(pump(lambda: s.state == "running", 10))
        s.stop()
        self.assertTrue(pump(lambda: s.state == "finished", 20), s.state)
        self.assertTrue(rp.btn_start.isEnabled())
        self.assertGreater(s.last_stats.cases, 0)
        self.assertIn("Finished", rp.state_label.text())
        w.close()

    def test_run_honours_stop_conditions_from_plan(self):
        w = window()
        w.model.set_stop(StopConditions(until_stopped=False, max_cases=20))
        w.run_page.start()
        self.assertTrue(pump(lambda: w.session.state == "finished", 30), w.session.state)
        self.assertIn(w.session.last_stats.stop_reason, ("max_cases",))
        self.assertIn("test-case limit", w.run_page.state_label.text())
        w.close()

    def test_errors_populate_and_reproduce(self):
        w = window()
        w.run_page.start()
        ep = w.errors_page
        self.assertTrue(pump(lambda: w.session.last_stats.errors_unique > 0, 30))
        w.session.stop()
        self.assertTrue(pump(lambda: w.session.state == "finished", 20))
        w.go_to(6)
        self.assertTrue(pump(lambda: ep.sm.rowCount() > 0, 15), "error list stayed empty")
        self.assertEqual(ep.sm.item(0, 1).data(0x100), "crash")            # category stored in UserRole
        self.assertTrue(ep.btn_reduce.isEnabled())
        self.assertIn("steps", ep.detail.toPlainText().lower())
        ep._copy()
        self.assertIn("crash", QGuiApplication.clipboard().text().lower())
        # filter
        ep.cat.setCurrentIndex(ep.cat.findData("hang")); pump(timeout=0.1)
        self.assertEqual(ep.proxy.rowCount(), 0)
        ep.cat.setCurrentIndex(0); pump(timeout=0.1)
        ep.view.selectRow(0)
        self.assertIsNotNone(ep.current())
        # reproduce & shrink (worker thread, original evidence untouched)
        ep._reduce()
        self.assertTrue(pump(lambda: ep.status.text().startswith("Done") or "could not" in ep.status.text(), 60), ep.status.text())
        self.assertTrue(ep.status.text().startswith("Done"), ep.status.text())
        ev = ep.current()
        self.assertIsNotNone(ev.reduced)
        self.assertIn("Shortest way", ep.detail.toPlainText())
        w.close()

    def test_close_while_running_stops_worker(self):
        w = window()
        w.run_page.start()
        self.assertTrue(pump(lambda: w.session.state == "running", 20))
        w.close()
        self.assertEqual(self.asked[0], "A test is running")
        self.assertIn(w.session.state, ("idle", "finished"))
        self.assertIsNone(w.session.controller)

    def test_start_blocked_without_features(self):
        w = MainWindow(); w.show()
        pump(timeout=0.4)
        w.run_page.start()
        pump(timeout=0.2)
        self.assertEqual(w.session.state, "idle")
        w.close()

    def test_real_target_needs_windows_message(self):
        import sys
        if sys.platform == "win32":
            self.skipTest("Windows has a real adapter")
        w = window(spec=None)
        w.model.adapter_spec = None
        w.run_page.start()
        self.assertIn("needs Windows", w.run_page.state_label.text())
        w.close()


class ActionTail(unittest.TestCase):
    def test_new_actions_overlap(self):
        a = [Action("state", f"f{i}", "on") for i in range(5)]
        self.assertEqual(new_actions((), tuple(a)), tuple(a))
        self.assertEqual(new_actions(tuple(a[:3]), tuple(a[1:5])), tuple(a[3:5]))
        self.assertEqual(new_actions(tuple(a), tuple(a)), ())
        other = (Action("state", "zz", "off"),)
        self.assertEqual(new_actions(tuple(a), other), other)


if __name__ == "__main__":
    unittest.main()
