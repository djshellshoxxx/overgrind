"""Step 5: strategy, duration, estimate, preview, preflight."""
from __future__ import annotations

import random

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QRadioButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..types import StopConditions
from . import backend, theme
from .widgets import PageBase, muted
from .workers import run_async

STRATEGIES = [
    ("state_cover", "State coverage (recommended)", "Visits every allowed combination of feature states in a random order before repeating any. Best first choice."),
    ("ordered_pairs", "Ordered pairs (legacy)", "Tries every feature followed by every other feature (A then B). Finds order problems like 'X after Y'."),
    ("twise", "t-wise combinatorial", "Every combination of any t features is seen together at least once, with far fewer cases than all states."),
    ("boundary", "Boundary", "Focuses on edge values: smallest, largest, just inside, the default."),
    ("adaptive", "Adaptive", "Learns where problems show up and tries similar cases more often, while still exploring."),
    ("chaos", "Chaos", "Random, rapid changes in random order, like a restless user. Not systematic, but good at surprises."),
]
RESET = [("every_case", "After every test case (cleanest)"), ("interval", "Every N test cases"), ("never", "Never (keep going from the last state)")]


class Card(QFrame):
    clicked = Signal()

    def __init__(self, key: str, title: str, desc: str, group: QButtonGroup):
        super().__init__()
        self.key = key
        self.setObjectName("Card"); self.setProperty("selected", False)
        lay = QVBoxLayout(self); lay.setContentsMargins(10, 8, 10, 8); lay.setSpacing(2)
        self.radio = QRadioButton(title); f = self.radio.font(); f.setBold(True); self.radio.setFont(f)
        group.addButton(self.radio)
        d = QLabel(desc); d.setWordWrap(True)
        lay.addWidget(self.radio); lay.addWidget(d)
        self.radio.toggled.connect(self._sync)

    def _sync(self, on: bool) -> None:
        self.setProperty("selected", on)
        self.style().unpolish(self); self.style().polish(self)

    def mousePressEvent(self, e) -> None:
        self.radio.setChecked(True); self.radio.setFocus()


class PlanPage(PageBase):
    title = "Plan the test"
    hint = "The defaults are a good start: <b>State coverage</b>, running until you stop it. Check the estimate, preview some cases, then press <b>Run preflight check</b>."

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self._busy = False
        self._est_seq = 0

        self.body.addWidget(QLabel("<b>1. How should we pick test cases?</b>"))
        grid = QGridLayout(); grid.setSpacing(8)
        self.group = QButtonGroup(self); self.cards: dict[str, Card] = {}
        for i, (k, t, d) in enumerate(STRATEGIES):
            c = Card(k, t, d, self.group); self.cards[k] = c
            grid.addWidget(c, i // 2, i % 2)
            c.radio.toggled.connect(lambda on, k=k: on and self._strategy(k))
        self.body.addLayout(grid)

        opt = QGroupBox("Options"); of = QFormLayout(opt)
        self.t_spin = QSpinBox(); self.t_spin.setRange(2, 6); self.t_spin.setAccessibleName("t for t-wise")
        self.t_label = QLabel("Combine this many features (t):")
        of.addRow(self.t_label, self.t_spin)
        self.cb_alloff = QCheckBox("Include the case where every feature is off")
        of.addRow(self.cb_alloff)
        self.reset = QComboBox(); [self.reset.addItem(t, k) for k, t in RESET]
        self.interval = QSpinBox(); self.interval.setRange(1, 1000000); self.interval.setSuffix(" cases")
        rr = QHBoxLayout(); rr.addWidget(self.reset, 1); rr.addWidget(self.interval)
        of.addRow("Put the app back to its defaults:", rr)
        self.seed = QSpinBox(); self.seed.setRange(0, 2**31 - 1)
        self.btn_seed = QPushButton("Random seed")
        sr = QHBoxLayout(); sr.addWidget(self.seed, 1); sr.addWidget(self.btn_seed)
        of.addRow("Seed (same seed = same cases):", sr)
        self.cb_invalid = QCheckBox("Also try values outside the allowed range (can trigger errors on purpose)")
        self.cb_restart = QCheckBox("Restart the app automatically after a crash")
        self.cb_shot = QCheckBox("Take a screenshot when something fails")
        for cb in (self.cb_invalid, self.cb_restart, self.cb_shot):
            of.addRow(cb)
        self.body.addWidget(opt)

        self.body.addWidget(QLabel("<b>2. How long should it run?</b>"))
        dur = QFrame(); dur.setObjectName("Card"); dl = QVBoxLayout(dur)
        self.dgroup = QButtonGroup(self)
        self.d_until = QRadioButton("Until I stop it"); f = self.d_until.font(); f.setPointSizeF(f.pointSizeF() * 1.4); f.setBold(True); self.d_until.setFont(f)
        self.dgroup.addButton(self.d_until); dl.addWidget(self.d_until)
        self.d_time = QRadioButton("For"); self.sp_time = QSpinBox(); self.sp_time.setRange(1, 100000)
        self.cb_unit = QComboBox(); self.cb_unit.addItems(["minutes", "hours"])
        self.d_cases = QRadioButton("For"); self.sp_cases = QSpinBox(); self.sp_cases.setRange(1, 2**31 - 1); self.sp_cases.setSingleStep(100)
        self.d_first = QRadioButton("Until the first error is found")
        self.d_errs = QRadioButton("Until"); self.sp_errs = QSpinBox(); self.sp_errs.setRange(1, 100000)
        self.d_pass = QRadioButton("One full pass through all states, then stop")
        for r in (self.d_time, self.d_cases, self.d_first, self.d_errs, self.d_pass):
            self.dgroup.addButton(r)
        def line(*ws):
            h = QHBoxLayout(); [h.addWidget(w) for w in ws]; h.addStretch(1); dl.addLayout(h)
        line(self.d_time, self.sp_time, self.cb_unit)
        line(self.d_cases, self.sp_cases, QLabel("test cases"))
        line(self.d_first)
        line(self.d_errs, self.sp_errs, QLabel("different errors are found"))
        line(self.d_pass)
        self.body.addWidget(dur)

        self.body.addWidget(QLabel("<b>3. What will that cover?</b>"))
        est = QFrame(); est.setObjectName("Card"); el = QVBoxLayout(est)
        self.est_text = QLabel(); self.est_text.setWordWrap(True); self.est_text.setTextFormat(Qt.TextFormat.PlainText)
        f = self.est_text.font(); f.setPointSizeF(f.pointSizeF() * 1.15); self.est_text.setFont(f)
        self.est_stats = muted("")
        el.addWidget(self.est_text); el.addWidget(self.est_stats)
        self.body.addWidget(est)

        bar = QHBoxLayout()
        self.btn_prev = QPushButton("Preview 20 cases"); self.btn_pre = QPushButton("Run preflight check")
        self.btn_reset = QPushButton("Reset plan to defaults")
        bar.addWidget(self.btn_prev); bar.addWidget(self.btn_pre); bar.addStretch(1); bar.addWidget(self.btn_reset)
        self.body.addLayout(bar)
        self.prev_status = muted(""); self.body.addWidget(self.prev_status)
        self.prev_table = QTableWidget(0, 3); self.prev_table.setHorizontalHeaderLabels(["#", "Feature states", "Control values"])
        self._table_style(self.prev_table, 1, 2); self.prev_table.setVisible(False)
        self.body.addWidget(self.prev_table)
        self.body.addWidget(QLabel("<b>Trust Ledger</b> (preflight check)"))
        self.pre_status = muted("Not run yet. The check finds every element we will touch and tells you if we can reliably find and drive it.")
        self.body.addWidget(self.pre_status)
        self.ledger = QTableWidget(0, 4); self.ledger.setHorizontalHeaderLabels(["Result", "Element", "What we found", "How to fix"])
        self._table_style(self.ledger, 2, 3); self.ledger.setVisible(False)
        self.body.addWidget(self.ledger)
        self.body.addStretch(1)

        self.est_timer = QTimer(self); self.est_timer.setSingleShot(True); self.est_timer.setInterval(350)
        self.est_timer.timeout.connect(self._estimate)

        self.t_spin.valueChanged.connect(lambda v: self._guard(lambda: self.model.set_strategy_option("t", v)))
        self.cb_alloff.toggled.connect(lambda v: self._guard(lambda: self.model.set_run(include_all_off=v)))
        self.reset.currentIndexChanged.connect(lambda _i: self._guard(lambda: self.model.set_run(reset_policy=self.reset.currentData())))
        self.interval.valueChanged.connect(lambda v: self._guard(lambda: self.model.set_run(reset_interval=v)))
        self.seed.valueChanged.connect(lambda v: self._guard(lambda: self.model.set_run(seed=v)))
        self.btn_seed.clicked.connect(lambda: self.seed.setValue(random.randint(1, 2**31 - 1)))
        self.cb_invalid.toggled.connect(lambda v: self._guard(lambda: self.model.set_run(invalid_values=v)))
        self.cb_restart.toggled.connect(lambda v: self._guard(lambda: self.model.set_run(restart_on_crash=v)))
        self.cb_shot.toggled.connect(lambda v: self._guard(lambda: self.model.set_run(capture_screenshot=v)))
        self.dgroup.buttonToggled.connect(lambda *_: self._stop_changed())
        for sp in (self.sp_time, self.sp_cases, self.sp_errs):
            sp.valueChanged.connect(lambda *_: self._stop_changed())
        self.cb_unit.currentIndexChanged.connect(lambda *_: self._stop_changed())
        self.btn_prev.clicked.connect(self._preview); self.btn_pre.clicked.connect(self.run_preflight)
        self.btn_reset.clicked.connect(self.model.reset_run)
        self.refresh()

    @staticmethod
    def _table_style(t: QTableWidget, *stretch: int) -> None:
        t.setAlternatingRowColors(True); t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        h = t.horizontalHeader(); h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for s in stretch:
            h.setSectionResizeMode(s, QHeaderView.ResizeMode.Stretch)
        t.setMinimumHeight(160); t.setWordWrap(True)

    def _guard(self, fn) -> None:
        if not self._busy:
            self._busy = True
            try:
                fn()
            finally:
                self._busy = False
            self._sync_enabled()
            self.est_timer.start()

    def _strategy(self, key: str) -> None:
        self._guard(lambda: self.model.set_run(strategy=key))

    def _stop_changed(self) -> None:
        if self._busy:
            return
        kw: dict = {}
        if self.d_time.isChecked():
            kw = dict(until_stopped=False, max_seconds=float(self.sp_time.value() * (3600 if self.cb_unit.currentIndex() else 60)))
        elif self.d_cases.isChecked():
            kw = dict(until_stopped=False, max_cases=self.sp_cases.value())
        elif self.d_first.isChecked():
            kw = dict(until_stopped=False, first_error=True)
        elif self.d_errs.isChecked():
            kw = dict(until_stopped=False, max_unique_errors=self.sp_errs.value())
        elif self.d_pass.isChecked():
            kw = dict(until_stopped=False, coverage_complete=True)
        self._guard(lambda: self.model.set_stop(StopConditions(**kw)))
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        self.t_spin.setEnabled(self.cards["twise"].radio.isChecked()); self.t_label.setEnabled(self.t_spin.isEnabled())
        self.interval.setEnabled(self.reset.currentData() == "interval")
        self.sp_time.setEnabled(self.d_time.isChecked()); self.cb_unit.setEnabled(self.d_time.isChecked())
        self.sp_cases.setEnabled(self.d_cases.isChecked()); self.sp_errs.setEnabled(self.d_errs.isChecked())

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            r = self.model.project.run
            self.cards.get(r.strategy, self.cards["state_cover"]).radio.setChecked(True)
            self.t_spin.setValue(int(r.strategy_options.get("t", 2)))
            self.cb_alloff.setChecked(r.include_all_off)
            self.reset.setCurrentIndex(max(0, self.reset.findData(r.reset_policy))); self.interval.setValue(r.reset_interval)
            self.seed.setValue(r.seed)
            self.cb_invalid.setChecked(r.invalid_values); self.cb_restart.setChecked(r.restart_on_crash); self.cb_shot.setChecked(r.capture_screenshot)
            s = self.model.stop
            if s.first_error:
                self.d_first.setChecked(True)
            elif s.max_unique_errors:
                self.d_errs.setChecked(True); self.sp_errs.setValue(s.max_unique_errors)
            elif s.max_cases:
                self.d_cases.setChecked(True); self.sp_cases.setValue(s.max_cases)
            elif s.max_seconds:
                self.d_time.setChecked(True)
                hours = s.max_seconds >= 3600 and s.max_seconds % 3600 == 0
                self.cb_unit.setCurrentIndex(1 if hours else 0)
                self.sp_time.setValue(int(s.max_seconds / (3600 if hours else 60)))
            elif s.coverage_complete:
                self.d_pass.setChecked(True)
            else:
                self.d_until.setChecked(True)
        finally:
            self._busy = False
        self._sync_enabled()
        self.est_text.setText("Calculating...")
        self.est_timer.start()

    # ---- estimate / preview
    def _estimate(self) -> None:
        proj = self.model.project
        if not proj.features:
            self.est_text.setText("Add at least one feature to see an estimate.")
            self.est_stats.setText("")
            return
        self._est_seq += 1
        seq = self._est_seq
        run_async(lambda: backend.estimate(proj), lambda d: self._est_done(seq, d), lambda m: self._est_fail(seq, m))

    def _est_done(self, seq: int, d: dict) -> None:
        if seq != self._est_seq:
            return
        valid, cart = d.get("valid_states"), d.get("cartesian")
        head = f"{valid:,} valid feature states" if isinstance(valid, int) else "Too many feature states to count exactly"
        if cart is None:
            head += "; value combinations cannot be counted (random sampling, not exhaustive)"
        else:
            head += f"; ~{float(cart):.2g} value combinations" if cart >= 10 ** 6 else f"; {cart:,} value combinations"
            if cart >= 10 ** 6:
                head += " (random sampling, not exhaustive)"
        self.est_text.setText(head)
        self.est_stats.setText(str(d.get("text") or ""))

    def _est_fail(self, seq: int, msg: str) -> None:
        if seq == self._est_seq:
            self.est_text.setText(f"We could not calculate the estimate. {msg}")
            self.est_stats.setText("")

    def _preview(self) -> None:
        proj = self.model.project
        self.btn_prev.setEnabled(False); self.prev_status.setText("Building preview...")
        run_async(lambda: backend.preview(proj, 20), lambda cs: self._prev_done(proj, cs), self._prev_fail)

    def _prev_fail(self, msg: str) -> None:
        self.btn_prev.setEnabled(True); self.prev_status.setText(f"We could not build a preview. {msg}"); self.prev_table.setVisible(False)

    def _prev_done(self, proj, cases) -> None:
        self.btn_prev.setEnabled(True)
        names = {f.id: (f.name or f.id) for f in proj.features}
        defaults = {f.id: f.default_state for f in proj.features}
        self.prev_table.setRowCount(len(cases))
        for r, c in enumerate(cases):
            st = ", ".join(f"{names.get(k, k)}={v}" + ("" if v == defaults.get(k) else "*") for k, v in c.state.items()) or "-"
            vals = ", ".join(f"{k}={v}" for k, v in c.values.items()) or "-"
            for col, t in enumerate((str(c.seq), st, vals)):
                self.prev_table.setItem(r, col, QTableWidgetItem(t))
        self.prev_table.setVisible(bool(cases)); self.prev_table.resizeRowsToContents()
        self.prev_status.setText(f"First {len(cases)} cases for seed {proj.run.seed}. A * marks a state that is not the default." if cases else "The plan produced no cases. Check that at least one feature is switched on in the Features page.")

    # ---- preflight
    def run_preflight(self) -> None:
        proj, spec = self.model.project, self.model.adapter_spec
        self.btn_pre.setEnabled(False); self.pre_status.setText("Checking every element... the app is only read, never changed.")
        run_async(lambda: backend.run_preflight(proj, spec), self._pre_done, self._pre_fail)

    def _pre_fail(self, msg: str) -> None:
        self.btn_pre.setEnabled(True); self.ledger.setVisible(False)
        self.pre_status.setText(f"The preflight check could not run. {msg}")

    def _pre_done(self, rows: list[dict]) -> None:
        self.btn_pre.setEnabled(True)
        label = {"ok": "OK", "warn": "Warning", "fail": "Fail"}
        self.ledger.setRowCount(len(rows))
        for r, row in enumerate(rows):
            st = row["status"]
            it = QTableWidgetItem(label[st]); it.setIcon(theme.glyph(st, theme.sem(st)))
            self.ledger.setItem(r, 0, it)
            for col, key in ((1, "element"), (2, "message"), (3, "hint")):
                self.ledger.setItem(r, col, QTableWidgetItem(row[key]))
        self.ledger.setVisible(bool(rows)); self.ledger.resizeRowsToContents()
        n = {k: sum(1 for x in rows if x["status"] == k) for k in label}
        if not rows:
            self.pre_status.setText("Nothing to check yet. Add features and controls first.")
        elif n["fail"]:
            self.pre_status.setText(f"{n['fail']} problem(s) will stop those parts of the test from working. Fix them before you start. ({n['warn']} warning(s), {n['ok']} OK)")
        elif n["warn"]:
            self.pre_status.setText(f"No blocking problems. {n['warn']} warning(s) may make some steps unreliable. ({n['ok']} OK)")
        else:
            self.pre_status.setText(f"All {n['ok']} checks passed.")
