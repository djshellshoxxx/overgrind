import unittest

import test_gui_common as gs
from test_gui_common import GuiCase, pump

if gs.HAVE_QT:
    from PySide6.QtCore import Qt
    from overgrind.gui import backend, theme
    from overgrind.gui.app import MainWindow
    from overgrind.gui.model import ProjectModel
    from overgrind.gui.page_controls import ControlsPage
    from overgrind.gui.page_features import FeatureTableModel, FeaturesPage
    from overgrind.gui.page_inspect import InspectPage
    from overgrind.gui.page_plan import PlanPage
    from overgrind.gui.sample import DEMO_ADAPTER, demo_project
    from overgrind.gui.widgets import Atlas, friendly_issue
    from overgrind.types import Domain, Selector


class Helpers(unittest.TestCase):
    def test_friendly_issue(self):
        lvl, text, page = friendly_issue("warning: feature 'x' is marked dangerous and is enabled; it will be exercised")
        self.assertEqual((lvl, page), ("warn", 2))
        self.assertTrue(text[0].isupper() and text.endswith("."))
        self.assertEqual(friendly_issue("control 'v': default 5 is outside the effective domain")[::2], ("fail", 3))
        self.assertEqual(friendly_issue("no enabled features")[2], 2)

    def test_selector_report_explains_fragility(self):
        frag = backend.selector_report(Selector(control_type="Button", index=2))
        self.assertLess(frag.score, 45)
        self.assertEqual(backend.score_label(frag.score)[0], "Fragile")
        self.assertTrue(frag.reason)
        self.assertTrue(frag.fixes or "hint" in frag.reason.lower() or "AutomationId" in frag.reason)
        good = backend.selector_report(Selector(auto_id="saveButton", control_type="Button"))
        self.assertGreaterEqual(good.score, 75)
        self.assertEqual(backend.score_label(good.score)[0], "Stable")

    def test_describe_domain_and_pe_arch(self):
        self.assertEqual(backend.describe_domain(Domain("integer", 0, 100, 1)), "0 to 100, step 1")
        self.assertEqual(backend.describe_domain(Domain("boolean")), "on / off")
        self.assertEqual(backend.pe_arch("/nonexistent"), "unknown")

    def test_list_windows_has_rows(self):
        rows = backend.list_windows()
        self.assertTrue(rows and {"pid", "process", "title", "path", "arch"} <= set(rows[0]))


class TargetAndInspect(GuiCase):
    def test_target_pick_sets_model(self):
        from overgrind.gui import backend
        orig = backend.list_windows
        backend.list_windows = lambda: [dict(x) for x in backend.DEMO_WINDOWS]   # never depend on the host's real windows
        self.addCleanup(setattr, backend, "list_windows", orig)
        w = MainWindow(); w.show()
        pg = w.pages[0]
        self.assertTrue(pump(lambda: pg.table.rowCount() > 0, 5))
        pg.table.selectRow(0)
        pump(timeout=0.1)
        self.assertTrue(w.model.project.target.process_name)
        pg.refresh()
        self.assertIn("(sample window)", pg.v_title.text())
        pg.r_exe.setChecked(True)
        pg.exe_path.setText("/tmp/app.exe"); pg.exe_args.setText("--x 1")
        pg._exe_changed()
        self.assertEqual(w.model.project.target.kind, "exe")
        self.assertEqual(w.model.project.target.args, ("--x", "1"))
        w.close()

    def test_inspect_scan_search_and_add(self):
        m = ProjectModel(demo_project()); m.demo = True; m.adapter_spec = DEMO_ADAPTER
        # start from a project without the demo features to see additions
        import dataclasses as dc
        m.load_project(dc.replace(m.project, features=m.project.features[:1], controls=()))
        pg = InspectPage(m); pg.show()
        pg.scan()
        self.assertTrue(pump(lambda: len(pg.elements) > 5, 10))
        beta = next(i for i, e in enumerate(pg.elements) if e.selector.auto_id == "cb_beta")
        pg.tree.setCurrentItem(pg._items[beta]); pump(timeout=0.1)
        self.assertEqual(pg.badge._text.text(), "Verified")
        self.assertIn("100", pg.meter._label)
        pg._add_feature()
        self.assertIsNotNone(m.feature("cb_beta"))
        self.assertEqual(m.feature("cb_beta").provenance.confidence, "verified")
        vol = next(i for i, e in enumerate(pg.elements) if e.selector.auto_id == "sld_volume")
        pg.tree.setCurrentItem(pg._items[vol]); pg._add_control()
        c = m.control("sld_volume")
        self.assertEqual(c.domain, Domain("integer", 0, 100, 1))
        # actions cannot be controls
        btn = next(i for i, e in enumerate(pg.elements) if e.selector.auto_id == "btn_apply")
        pg.tree.setCurrentItem(pg._items[btn]); pump(timeout=0.05)
        self.assertFalse(pg.btn_ctrl.isEnabled())
        pg._add_feature()
        self.assertEqual(m.feature("btn_apply").kind, "action")
        # search hides non-matches
        pg.search.setText("volume"); pump(timeout=0.05)
        self.assertTrue(pg._items[beta].isHidden())
        self.assertFalse(pg._items[vol].isHidden())

    def test_inspect_failure_is_friendly(self):
        m = ProjectModel(demo_project()); m.adapter_spec = None
        import sys
        if sys.platform == "win32":
            self.skipTest("real adapter available")
        pg = InspectPage(m); pg.show()
        pg.scan()
        self.assertTrue(pump(lambda: pg.btn_scan.isEnabled(), 5))
        self.assertEqual(pg.elements, [])


class FeaturesControlsPlan(GuiCase):
    def test_feature_table_edits_flow_to_model(self):
        m = ProjectModel(demo_project())
        tm = FeatureTableModel(m)
        row = [f.id for f in m.project.features].index("cmb_mode")
        self.assertTrue(tm.setData(tm.index(row, 0), "Quality"))
        self.assertTrue(tm.setData(tm.index(row, 2), "Off, Low, Mid, High"))
        self.assertEqual(m.feature("cmb_mode").states, ("Off", "Low", "Mid", "High"))
        self.assertFalse(tm.setData(tm.index(row, 3), "Nope"))               # not a state
        self.assertTrue(tm.setData(tm.index(row, 3), "Mid"))
        self.assertTrue(tm.setData(tm.index(row, 4), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole))
        self.assertFalse(m.feature("cmb_mode").enabled)
        self.assertTrue(tm.setData(tm.index(row, 1), "On/off switch"))
        self.assertEqual(m.feature("cmb_mode").states, ("off", "on"))
        self.assertTrue(m.dirty)

    def test_rule_builder_all_three_kinds(self):
        m = ProjectModel(demo_project())
        pg = FeaturesPage(m); pg.show()
        n0 = len(m.project.constraints)
        pg.rule_kind.setCurrentIndex(0)
        pg._add_rule()                                                         # nothing ticked: friendly refusal
        self.assertEqual(len(m.project.constraints), n0)
        self.assertIn("two", pg.rule_msg.text())
        for i in (0, 2):
            pg.mx_list.item(i).setCheckState(Qt.CheckState.Checked)
        pg._add_rule()
        pg.rule_kind.setCurrentIndex(1)
        pg.rq_feature.setCurrentIndex(0)
        pg.rq_needs.item(1).setCheckState(Qt.CheckState.Checked)
        pg._add_rule()
        pg.rule_kind.setCurrentIndex(2)
        pg._add_rule()
        self.assertEqual([c.kind for c in m.project.constraints[n0:]], ["mutex", "requires", "forbid"])
        self.assertFalse([e for e in backend.validate(m.project) if not e.startswith("warning:")])
        pg.refresh()
        pg.rules.setCurrentRow(len(m.project.constraints) - 1); pg._del_rule()
        self.assertEqual(len(m.project.constraints), n0 + 2)

    def test_controls_restrict_range_and_flags(self):
        m = ProjectModel(demo_project())
        pg = ControlsPage(m); pg.show()
        pg.table.selectRow([c.id for c in m.project.controls].index("spn_count"))
        pump(timeout=0.05)
        self.assertEqual(pg.o_range.text(), "0 to 50, step 1")                # original range always visible
        pg.n_max.setValue(20)
        c = m.control("spn_count")
        self.assertEqual(c.fuzz_domain, Domain("integer", 0, 20, 1))
        self.assertEqual(c.domain, Domain("integer", 0, 50, 1))              # original untouched
        pg.btn_orig.click()
        self.assertIsNone(m.control("spn_count").fuzz_domain)
        pg.cb_sens.setChecked(True)
        self.assertTrue(m.control("spn_count").sensitive)
        pg.scope.item(0).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(len(m.control("spn_count").scope), 1)
        pg.e_default.setText("7"); pg._apply_default()
        self.assertEqual(m.control("spn_count").default, 7)

    def test_plan_page_strategy_duration_and_estimate(self):
        m = ProjectModel(demo_project())
        pg = PlanPage(m); pg.show()
        self.assertTrue(pg.d_until.isChecked())                              # default: until I stop it
        self.assertTrue(pg.cards["state_cover"].radio.isChecked())
        pg.cards["twise"].radio.setChecked(True)
        pg.t_spin.setValue(3)
        self.assertEqual((m.project.run.strategy, m.project.run.strategy_options["t"]), ("twise", 3))
        pg.d_cases.setChecked(True); pg.sp_cases.setValue(250)
        self.assertEqual((m.stop.max_cases, m.stop.until_stopped), (250, False))
        pg.d_time.setChecked(True); pg.cb_unit.setCurrentIndex(1); pg.sp_time.setValue(2)
        self.assertEqual(m.stop.max_seconds, 7200.0)
        pg.d_first.setChecked(True)
        self.assertTrue(m.stop.first_error)
        pg.cb_alloff.setChecked(False)
        self.assertFalse(m.project.run.include_all_off)
        self.assertTrue(pump(lambda: "valid feature states" in pg.est_text.text(), 10), pg.est_text.text())
        pg._preview()
        self.assertTrue(pump(lambda: pg.prev_table.rowCount() == 20, 10), pg.prev_status.text())
        pg.btn_reset.click()
        pg.refresh()
        self.assertTrue(m.project.run.include_all_off)
        self.assertTrue(m.stop.until_stopped)
        self.assertTrue(pg.d_until.isChecked())

    def test_preflight_ledger_renders(self):
        m = ProjectModel(demo_project()); m.demo = True; m.adapter_spec = DEMO_ADAPTER
        pg = PlanPage(m); pg.show()
        pg.run_preflight()
        self.assertTrue(pump(lambda: pg.ledger.rowCount() > 0 or "could not" in pg.pre_status.text(), 90), pg.pre_status.text())
        self.assertGreater(pg.ledger.rowCount(), 3, pg.pre_status.text())
        self.assertIn(pg.ledger.item(0, 0).text(), ("OK", "Warning", "Fail"))
        self.assertFalse(pg.ledger.item(0, 0).icon().isNull())               # icon AND word


class Widgets(GuiCase):
    def test_atlas_and_badges_render(self):
        a = Atlas(); a.resize(500, 200)
        a.set_features([("a", "Alpha", ("off", "on")), ("m", "Mode", ("lo", "mid", "hi"))])
        a.set_visits({("a", "on"): 5, ("m", "hi"): 1})
        self.assertFalse(a.grab().isNull())
        self.assertIn("2 of 5", a.accessibleDescription())
        for lvl in theme.CONFIDENCE:
            b = theme.Badge(); b.set_confidence(lvl)
            self.assertTrue(b._text.text())                                  # word, not just colour
            self.assertFalse(b._icon.pixmap().isNull())
        for lvl in theme.RELIABILITY:
            b = theme.Badge(); b.set_reliability(lvl)
            self.assertTrue(b._text.text())

    def test_geometry_persists(self):
        from overgrind.gui.app import settings
        w = MainWindow(); w.show(); w.resize(1000, 650)
        w.close()
        self.assertIsNotNone(settings().value("geometry"))

    def test_compare_dialog_runs_demo_builds(self):
        from overgrind.gui.dialogs import CompareDialog
        d = CompareDialog(demo_project()); d.show()
        d.n.setValue(12)
        d.compare()
        self.assertTrue(pump(lambda: d.go.isEnabled(), 60))
        self.assertTrue(d.out.toPlainText(), d.status.text())
        self.assertNotIn("could not run", d.status.text())


if __name__ == "__main__":
    unittest.main()
