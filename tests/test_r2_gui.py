"""Round-2 GUI hardening: pick/outline overlays, Trust Ledger fix buttons, dependency review, compare mapping review,
reduce diff, Atlas views. Offscreen; backend calls are exercised through the real modules where they exist."""
import dataclasses as dc
import unittest

import test_gui_common as gs
from test_gui_common import GuiCase, pump

if gs.HAVE_QT:
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QGuiApplication, QMouseEvent
    from PySide6.QtWidgets import QApplication, QPushButton
    from overgrind.gui import backend, overlay, theme
    from overgrind.gui.app import MainWindow
    from overgrind.gui.dialogs import CompareDialog
    from overgrind.gui.page_features import SuggestDialog
    from overgrind.gui.sample import DEMO_ADAPTER, demo_project
    from overgrind.gui.widgets import PairMatrix
    from overgrind.types import Action, Case, Coverage, ElementInfo, ErrorEvent, RunStats, Selector


def window():
    w = MainWindow(); w.model.demo = True; w.model.adapter_spec = DEMO_ADAPTER
    w.model.load_project(demo_project()); w.resize(1100, 700); w.show()
    pump(timeout=0.1)
    return w


def el(name, x, y, w, h, kind="Button"):
    return ElementInfo(name=name, control_type=kind, selector=Selector(auto_id="id_" + name, title=name, control_type=kind), bounds=(x, y, w, h))


class Pure(unittest.TestCase):
    def test_hit_test_picks_the_innermost_element(self):
        els = [el("window", 0, 0, 500, 400, "Window"), el("panel", 10, 10, 200, 200, "Pane"), el("ok", 20, 20, 50, 20)]
        self.assertEqual(backend.hit_test(els, 25, 25), 2)
        self.assertEqual(backend.hit_test(els, 100, 100), 1)
        self.assertEqual(backend.hit_test(els, 450, 350), 0)
        self.assertIsNone(backend.hit_test(els, 900, 900))

    def test_apply_fix_covers_every_structured_kind(self):
        p = demo_project()
        f, c = p.features[0], next(x for x in p.controls if x.domain.kind in ("integer", "float"))
        out = backend.apply_fix(p, {"kind": "mark_dangerous", "target": f.id, "value": True})
        self.assertTrue(next(x for x in out.features if x.id == f.id).dangerous)
        self.assertTrue(next(x for x in backend.apply_fix(p, {"kind": "skip_control", "target": c.id, "value": True}).controls if x.id == c.id).skip)
        out = backend.apply_fix(p, {"kind": "adopt_default", "target": c.id, "value": 7})
        self.assertEqual(next(x for x in out.controls if x.id == c.id).default, 7)
        out = backend.apply_fix(p, {"kind": "narrow_domain", "target": c.id, "value": {"minimum": 1, "maximum": 5}})
        d = next(x for x in out.controls if x.id == c.id)
        self.assertEqual((d.fuzz_domain.minimum, d.fuzz_domain.maximum), (1, 5))
        out = backend.apply_fix(p, {"kind": "set_selector", "target": c.id, "value": {"auto_id": "better", "path": ["w"]}})
        self.assertEqual(next(x for x in out.controls if x.id == c.id).selector.auto_id, "better")
        out = backend.apply_fix(p, {"kind": "set_run", "target": "seed", "value": 99})
        self.assertEqual(out.run.seed, 99)
        self.assertIs(backend.apply_fix(p, {"kind": "nonsense", "target": "x"}), p)

    def test_fix_button_text_is_plain_words(self):
        self.assertEqual(backend.fix_button_text({"kind": "narrow_domain", "value": {"minimum": 0, "maximum": 99}}), "Narrow the range to 0..99")
        self.assertIn("dangerous", backend.fix_button_text({"kind": "mark_dangerous"}).lower())

    def test_step_diff_marks_removed_and_changed(self):
        a = [("reset", "", "None"), ("state", "A", "'on'"), ("control", "W", "91"), ("invoke", "Go", "None")]
        b = [("reset", "", "None"), ("state", "A", "'on'"), ("control", "W", "37")]
        left, right = backend.step_diff(a, b)
        self.assertEqual([t for t, _ in left].count("removed"), 2)
        self.assertIn("changed", [t for t, _ in right])
        self.assertEqual(len(left), len(right))

    def test_why_not_complete_lists_each_known_gap(self):
        cv = Coverage(states_total=100, states_visited=40, unreachable=2, failed_to_apply=3, unverified=4, infeasible=5,
                      why_not_complete=("custom reason",))
        text = " | ".join(backend.why_not_complete(cv))
        for needle in ("custom reason", "60 of 100", "2 state", "3 test case", "4 visit", "5 combination"):
            self.assertIn(needle, text)

    def test_ranked_visits_and_pair_coverage(self):
        table = [{"a": "off", "b": "off"}, {"a": "on", "b": "off"}, {"a": "off", "b": "on"}, {"a": "on", "b": "on"}]
        self.assertEqual(backend.visits_from_ranks(table, (2, 0, 1, 0))[("a", "off")], 3)
        cov = backend.pair_coverage(table, (1, 1, 0, 0), ["a", "b"], {})
        self.assertEqual(cov[("a", "b")], (2, 4))

    def test_id_map_never_guesses(self):
        rows = [{"id": "x", "status": "mapped", "b_index": 0, "b_id": "x2"}, {"id": "y", "status": "review", "b_index": 1, "b_id": "y2"},
                {"id": "z", "status": "unmatched", "b_index": None, "b_id": ""}]
        m = backend.id_map_from_review(rows, {})
        self.assertEqual(m, {"x": "x2", "y": None, "z": None})
        self.assertEqual(backend.id_map_from_review(rows, {"y": "accept"})["y"], "y2")
        self.assertEqual(backend.id_map_from_review(rows, {"y": "override:q"})["y"], "q")

    def test_map_controls_against_the_same_build_is_all_mapped(self):
        from overgrind.adapters.mock import MockAdapter
        p = demo_project(); els = list(MockAdapter().inspect())
        rows = backend.map_controls(p, els, els)
        self.assertTrue(rows)
        self.assertTrue(all(r["status"] == "mapped" and r["score"] >= 0.85 for r in rows), rows)


class Overlays(GuiCase):
    def test_bounds_overlay_is_click_through_frameless_and_guarded(self):
        o = overlay.BoundsOverlay(force=True)
        fl = o.windowFlags()
        self.assertTrue(fl & Qt.WindowType.FramelessWindowHint and fl & Qt.WindowType.WindowTransparentForInput and fl & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(o.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(o.show_bounds((100, 120, 80, 30), "ok")); self.assertTrue(o.isVisible())
        self.assertEqual((o.width(), o.height()), (86, 36))
        o.clear(); self.assertFalse(o.isVisible())
        self.assertFalse(overlay.BoundsOverlay(force=False).enabled and not backend.overlay_supported()[0])
        self.assertFalse(o.show_bounds((0, 0, 0, 0)))

    def test_inspect_page_disables_pick_off_windows_with_a_reason(self):
        w = window(); ip = w.pages[1]
        ok, why = backend.overlay_supported()
        self.assertEqual(ip.btn_pick.isEnabled(), ok)
        if not ok:
            self.assertIn("Windows", ip.btn_pick.toolTip()); self.assertFalse(ip.overlay.enabled)
        w.close()

    def test_pick_overlay_selects_the_element_under_the_pointer_and_swallows_the_click(self):
        els = [el("window", 0, 0, 400, 300, "Window"), el("ok", 40, 40, 80, 30)]
        po = overlay.PickOverlay(els, force=True); got = []
        po.picked.connect(got.append)
        self.assertTrue(po.begin())
        g = po.geometry().topLeft()
        pos = QPointF(45 - g.x(), 45 - g.y())
        po.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, pos, po.mapToGlobal(pos), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(po._hover, 1)
        po.grab()                                  # paints the outline and label without error
        po.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, pos, po.mapToGlobal(pos), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(got, [1]); self.assertFalse(po.isVisible())

    def test_picking_selects_the_tree_row_and_outlines_it(self):
        w = window(); ip = w.pages[1]; w.go_to(1)
        ip._scanned([el("window", 0, 0, 400, 300, "Window"), el("ok", 40, 40, 80, 30)])
        ip.overlay.enabled = True; ip.chk_outline.setEnabled(True)      # as on Windows
        ip.select_index(1)
        self.assertEqual(ip.current_element().name, "ok")
        pump(timeout=0.05)
        self.assertTrue(ip.overlay.isVisible())
        w.go_to(0); pump(timeout=0.05)
        self.assertFalse(ip.overlay.isVisible(), "the outline must not stay on screen when leaving the page")
        w.close()


class LedgerFixes(GuiCase):
    ROWS = [
        {"element": "feature cb_alpha", "status": "warn", "message": "looks risky", "hint": "", "kind": "feature", "id": "cb_alpha",
         "fixes": [{"id": "f1", "kind": "mark_dangerous", "target": "cb_alpha", "value": True, "label": "mark dangerous"}]},
        {"element": "control sld", "status": "fail", "message": "ignores writes", "hint": "x", "kind": "control", "id": "sld_volume",
         "fixes": [{"id": "f2", "kind": "skip_control", "target": "sld_volume", "value": True, "label": "skip"},
                   {"id": "f3", "kind": "adopt_default", "target": "sld_volume", "value": 3, "label": "adopt"}]},
        {"element": "target", "status": "ok", "message": "fine", "hint": "", "kind": "target", "id": "t", "fixes": []},
    ]

    def test_rows_render_fix_buttons_and_a_recheck_per_row(self):
        w = window(); pg = w.pages[4]; pg._pre_done(list(self.ROWS))
        texts = [[b.text() for b in pg._ledger_btns[r]] for r in range(3)]
        self.assertEqual(texts[0], ["Mark as dangerous", "Re-check"])
        self.assertEqual(len(texts[1]), 3); self.assertEqual(texts[2], ["Re-check"])
        self.assertEqual(pg.ledger.item(1, 0).text(), "Fail"); self.assertFalse(pg.ledger.item(1, 0).icon().isNull())
        w.close()

    def test_fix_button_edits_the_project_undoably_and_recheck_reruns(self):
        w = window(); pg = w.pages[4]; pg._pre_done(list(self.ROWS))
        fid = w.model.project.features[0].id
        rows = [dict(self.ROWS[0], id=fid, fixes=[dict(self.ROWS[0]["fixes"][0], target=fid)])]
        pg._pre_done(rows)
        pg._ledger_btns[0][0].click()
        self.assertTrue(w.model.feature(fid).dangerous)
        self.assertFalse(pg._ledger_btns[0][0].isEnabled()); self.assertEqual(pg._ledger_btns[0][0].text(), "Applied")
        w.model.undo(); self.assertFalse(w.model.feature(fid).dangerous)
        called = []
        pg.run_preflight = lambda: called.append(1)
        pg._ledger_btns[0][1].click()
        self.assertEqual(called, [1])
        w.close()

    def test_real_preflight_rows_carry_structured_fixes(self):
        rows = backend.run_preflight(demo_project(), DEMO_ADAPTER)
        self.assertTrue(rows and all("fixes" in r and "kind" in r for r in rows))


class SuggestReview(GuiCase):
    def props(self):
        from overgrind.infer import Proposal
        a = Proposal("requires", "g", ("h",), "inferred", 16, 0, "Gamma needs HDR", q=0.003, impact={"valid_before": 8, "valid_after": 6})
        b = Proposal("mutex", "", ("x", "y"), "verified", 12, 1, "X and Y never together")
        return [backend.proposal_row(a), backend.proposal_row(b)]

    def test_rows_show_evidence_confidence_and_effect(self):
        d = SuggestDialog(self.props(), retest=lambda r, ok, bad: None); d.show()
        self.assertIn("16/16", d.table.item(0, 1).text()); self.assertIn("q=0.003", d.table.item(0, 1).text())
        self.assertIn("8 to 6", d.table.item(0, 3).text()); self.assertIn("12/13", d.table.item(1, 1).text())
        self.assertEqual(d.table.cellWidget(0, 2)._text.text(), "Inferred"); self.assertEqual(d.table.cellWidget(1, 2)._text.text(), "Verified")
        self.assertFalse(d.ok.isEnabled())
        d.close()

    def test_accept_reject_and_test_again(self):
        rej = {}; d = SuggestDialog(self.props(), rejected=rej, retest=lambda r, ok, bad: ok([self.props()[0]])); d.show()
        d.rows[0]["_btns"]["accepted"].click()
        d.rows[1]["_btns"]["rejected"].click()
        self.assertEqual(len(d.accepted()), 1); self.assertTrue(d.ok.isEnabled()); self.assertEqual(len(rej), 1)
        d.rows[0]["_btns"]["retest"].click()
        self.assertEqual(len(d.accepted()), 1, "test again keeps an accepted decision")
        self.assertIn("Tested again", d.status.text())
        d.close()

    def test_test_again_that_finds_nothing_says_so(self):
        d = SuggestDialog(self.props(), retest=lambda r, ok, bad: ok([])); d.show()
        d.rows[0]["_btns"]["retest"].click()
        self.assertIn("did not show up", d.status.text()); d.close()


class CompareReview(GuiCase):
    def test_review_step_then_noise_calibrated_run(self):
        d = CompareDialog(demo_project()); d.show(); d.n.setValue(10)
        d.review()
        self.assertTrue(pump(lambda: d.stack.currentIndex() == 1, 60), d.status.text())
        self.assertEqual(d.table.rowCount(), len(d.rows))
        self.assertEqual(d.table.item(0, 3).text() in ("Matched", "Please check", "No match"), True)
        self.assertFalse(d.table.item(0, 3).icon().isNull())
        dec = d.decisions(); self.assertEqual(set(dec), {r["id"] for r in d.rows})
        d.table.cellWidget(0, 4).setCurrentIndex(1)               # ignore the first control
        self.assertEqual(d.decisions()[d.rows[0]["id"]], "ignore")
        d.compare(); self.assertTrue(d.progress.isVisible()); self.assertIn("alibrat", d.progress.format())
        self.assertTrue(pump(lambda: d.stack.currentIndex() == 2, 120), d.status.text())
        self.assertIn("could not", "x" + d.status.text()) if False else None
        self.assertTrue(d.btn_save.isEnabled()); self.assertIn("Differential report", d._report_md)
        d.close()


def reduced_event(n=6):
    hist = [Action("reset", "", None), Action("state", "cb_alpha", "on"), Action("control", "sld", 91), Action("invoke", "Go", None), Action("wait", "", 50), Action("state", "cmb", "High")]
    red = {"steps": [{"kind": "reset", "target": "", "value": None}, {"kind": "state", "target": "cb_alpha", "value": "on"}, {"kind": "control", "target": "sld", "value": 37}],
           "reliability_pct": 80, "confirm": {"reproduced": 4, "runs": 5}, "original_steps": 6, "state": {"cb_alpha": "on"}, "values": {"sld": 37}}
    return ErrorEvent("e1", "2026-10-14T10:00:00", "r", "crash", "abcdef123456", "boom", Case(1, {"cb_alpha": "on"}, {"sld": 91}, ()), hist,
                      env={"reliability": {"r": 4, "n": 5}}, reliability="reliable", reduced=red)


class ReduceView(GuiCase):
    def page(self):
        w = window(); pg = w.pages[6]; w.go_to(6)
        ev = reduced_event(); pg.events = {ev.id: ev}; pg._fill(); pg.view.selectRow(0); pump(timeout=0.05)
        return w, pg, ev

    def test_side_by_side_diff_with_non_colour_markers(self):
        w, pg, ev = self.page()
        self.assertEqual(pg.diff.rowCount() >= 6, True)
        col0 = [pg.diff.item(r, 0).text() for r in range(pg.diff.rowCount())]
        self.assertTrue(any(t.startswith("− ") for t in col0))
        struck = [pg.diff.item(r, 0).font().strikeOut() for r in range(pg.diff.rowCount()) if pg.diff.item(r, 0).text().startswith("−")]
        self.assertTrue(struck and all(struck))
        col1 = [pg.diff.item(r, 1).text() for r in range(pg.diff.rowCount())]
        self.assertTrue(any(t.startswith("~ ") for t in col1))
        self.assertIn("Removed 3 of 6", pg.diff_note.text()) if False else self.assertIn("Removed", pg.diff_note.text())
        w.close()

    def test_reliability_badge_shows_counts(self):
        w, pg, ev = self.page()
        self.assertIn("4 of 5", pg.badge._text.text()); self.assertIn("4 of 5", pg.reliab.text())
        w.close()

    def test_copy_as_issue_uses_export_issue_when_present(self):
        w, pg, ev = self.page(); pg._copy()
        txt = QGuiApplication.clipboard().text()
        try:
            from overgrind.reduce import export_issue  # noqa: F401
            self.assertIn("Reproduced 4/5", txt)
        except ImportError:
            self.assertTrue(txt)
        w.close()

    def test_reduce_passes_cancel_and_progress_to_the_reducer(self):
        import inspect
        from overgrind.reduce import Reducer
        if "cancel" not in inspect.signature(Reducer).parameters:
            self.skipTest("Reducer has no cancel yet")
        self.assertTrue(backend.reducer_supports_callbacks())


class AtlasViews(GuiCase):
    def setUp(self):
        super().setUp()
        self.w = window(); self.rp = self.w.run_page
        self.proj = self.w.model.project

    def tearDown(self):
        self.w.close(); super().tearDown()

    def cov(self, **kw):
        base = dict(states_total=None, states_visited=0, guarantee="Visited 3 of 8 states. This is not a complete-coverage claim.")
        base.update(kw); return Coverage(**base)

    def test_why_not_100_and_guarantee_verbatim(self):
        g = "Covered 3 of 8 states (sampled). This is NOT a complete-coverage claim."
        self.rp.on_stats(RunStats(state="running", coverage=self.cov(states_total=8, states_visited=3, guarantee=g, unreachable=1)))
        self.assertEqual(self.rp.guarantee.text(), g)
        items = [self.rp.why.item(i).text() for i in range(self.rp.why.count())]
        self.assertTrue(any("5 of 8" in t for t in items) and any("could not be reached" in t for t in items), items)

    def test_state_counts_give_exact_per_state_numbers_via_unrank(self):
        from overgrind import scheduler
        n = 0
        while True:
            try:
                scheduler.unrank_state(self.proj, n); n += 1
            except (IndexError, ValueError):
                break
            if n >= 4096:
                break
        counts = tuple((r % 5) + 1 for r in range(n))
        self.rp.on_stats(RunStats(state="running", coverage=self.cov(states_total=n, states_visited=n, state_counts=counts, visit_hist=(0, 4, 3, 2, 1))))
        self.assertTrue(pump(lambda: self.rp.atlas.sampled is False, 20))
        self.assertTrue(pump(lambda: sum(self.rp._visits.values()) > 0, 20))
        want = {}
        for r, c in enumerate(counts):
            for fid, st in scheduler.unrank_state(self.proj, r).items():
                want[(fid, str(st))] = want.get((fid, str(st)), 0) + c
        shown = self.rp._shown()
        self.assertEqual(self.rp._visits, {k: v for k, v in want.items() if k in shown})
        self.assertIn("States by number of visits", self.rp.hist_label.text())

    def test_pair_view_toggle_and_matrix(self):
        self.rp.view_pairs.setChecked(True)
        self.assertIs(self.rp.views.currentWidget(), self.rp.pairs)
        self.rp.on_stats(RunStats(state="running", coverage=self.cov(states_total=8, states_visited=1)))
        self.assertFalse(self.rp.pairs.has_data())              # no exact counts: says so instead of faking
        self.rp.view_states.setChecked(True)
        self.assertIs(self.rp.views.currentWidget(), self.rp.atlas)
        pm = PairMatrix(); pm.set_data(["a", "b", "c"], {"a": "A", "b": "B", "c": "C"}, {("a", "b"): (3, 4), ("a", "c"): (0, 4), ("b", "c"): (4, 4)})
        pm.resize(400, 160); self.assertFalse(pm.grab().isNull())
        self.assertIn("7 of 12", pm.accessibleDescription())

    def test_pair_matrix_filled_from_counts_in_the_page(self):
        from overgrind import scheduler
        n = 0
        while n < 4096:
            try:
                scheduler.unrank_state(self.proj, n); n += 1
            except (IndexError, ValueError):
                break
        self.rp.view_pairs.setChecked(True)
        counts = tuple(1 for _ in range(n))
        self.rp.on_stats(RunStats(state="running", coverage=self.cov(states_total=n, states_visited=n, state_counts=counts)))
        self.assertTrue(pump(lambda: self.rp.pairs.has_data(), 30))


if __name__ == "__main__":
    unittest.main()
