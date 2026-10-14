"""Step 4: controls and the range we will test."""
from __future__ import annotations

import dataclasses as dc

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSpinBox, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..types import ControlDef, Domain, Provenance, Selector
from . import backend, theme
from .widgets import PageBase, empty_state, fit_table, muted

KIND_LABEL = {"boolean": "On/off", "integer": "Whole number", "float": "Decimal number", "choice": "Choice", "text": "Text"}
HEAD = ["Control", "Type", "Original range", "We will test", "Only when", "Flags", "Source"]


def _dspin(decimals: int) -> QDoubleSpinBox:
    s = QDoubleSpinBox(); s.setRange(-1e12, 1e12); s.setDecimals(decimals); s.setKeyboardTracking(False)
    return s


class ControlsPage(PageBase):
    title = "Choose the values to try"
    hint = "Pick a control, then narrow the range if some values are not meant to be tried. The app's own range is always shown on the left for comparison."

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self._busy = False
        self.table = QTableWidget(0, len(HEAD)); self.table.setHorizontalHeaderLabels(HEAD)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True); self.table.verticalHeader().setVisible(False)
        self.table.setAccessibleName("Controls")
        h = self.table.horizontalHeader(); h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch); h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.body.addWidget(self.table)
        self.empty = empty_state("No controls yet.\nGo to Inspect and press <i>Add as control</i>, or press <b>Add control manually</b>.")
        self.body.addWidget(self.empty)
        row = QHBoxLayout()
        self.btn_add = QPushButton("Add control manually"); self.btn_del = QPushButton("Remove selected")
        row.addWidget(self.btn_add); row.addWidget(self.btn_del); row.addStretch(1)
        self.body.addLayout(row)

        self.editor = QWidget(); ev = QVBoxLayout(self.editor); ev.setContentsMargins(0, 0, 0, 0)
        self.e_title = QLabel(); self.e_title.setTextFormat(Qt.TextFormat.PlainText); f = self.e_title.font(); f.setBold(True); f.setPointSizeF(f.pointSizeF() * 1.15); self.e_title.setFont(f)
        ev.addWidget(self.e_title)
        side = QHBoxLayout()
        orig = QGroupBox("Original: what the app says"); ol = QVBoxLayout(orig)
        self.o_range = QLabel(); self.o_range.setWordWrap(True); self.o_range.setTextFormat(Qt.TextFormat.PlainText)
        self.o_badge = theme.Badge()
        ol.addWidget(self.o_range); ol.addWidget(self.o_badge, 0, Qt.AlignmentFlag.AlignLeft); ol.addStretch(1)
        rest = QGroupBox("Restricted: what we will test"); rl = QVBoxLayout(rest)
        self.stack = QStackedWidget()
        # 0 none/boolean
        self.s_note = QLabel("Both on and off are tried. To test only one value, set Starts as below and tick Skip.")
        self.s_note.setWordWrap(True); self.stack.addWidget(self.s_note)
        # 1 numeric
        nw = QWidget(); nf = QFormLayout(nw); nf.setContentsMargins(0, 0, 0, 0)
        self.n_min, self.n_max, self.n_step = _dspin(6), _dspin(6), _dspin(6)
        nf.addRow("Lowest:", self.n_min); nf.addRow("Highest:", self.n_max); nf.addRow("Step (0 = any):", self.n_step)
        self.stack.addWidget(nw)
        # 2 choice
        self.c_list = QListWidget(); self.c_list.setMaximumHeight(110); self.c_list.setAccessibleName("Allowed choices")
        cw = QWidget(); cl = QVBoxLayout(cw); cl.setContentsMargins(0, 0, 0, 0); cl.addWidget(QLabel("Tick the choices to try:")); cl.addWidget(self.c_list)
        self.stack.addWidget(cw)
        # 3 text
        tw = QWidget(); tf = QFormLayout(tw); tf.setContentsMargins(0, 0, 0, 0)
        self.t_len = QSpinBox(); self.t_len.setRange(0, 100000); self.t_alpha = QLineEdit(); self.t_alpha.setPlaceholderText("Empty = a default mix of characters")
        tf.addRow("Longest text:", self.t_len); tf.addRow("Characters to use:", self.t_alpha)
        self.stack.addWidget(tw)
        rl.addWidget(self.stack)
        self.r_warn = QLabel(); self.r_warn.setWordWrap(True); rl.addWidget(self.r_warn)
        self.btn_orig = QPushButton("Use the original range"); rl.addWidget(self.btn_orig, 0, Qt.AlignmentFlag.AlignLeft)
        side.addWidget(orig, 1); side.addWidget(rest, 2)
        ev.addLayout(side)

        more = QHBoxLayout()
        gb = QGroupBox("Settings"); gf = QFormLayout(gb)
        self.e_default = QLineEdit(); self.e_default.setAccessibleName("Starts as")
        gf.addRow("Starts as:", self.e_default)
        self.cb_skip = QCheckBox("Skip: never change this control"); self.cb_ro = QCheckBox("Read-only: only read it, never write")
        self.cb_sens = QCheckBox("Sensitive: hide its values in error reports")
        for cb in (self.cb_skip, self.cb_ro, self.cb_sens):
            gf.addRow(cb)
        sc = QGroupBox("Only change it while these features are on"); sl = QVBoxLayout(sc)
        self.scope = QListWidget(); self.scope.setMaximumHeight(120); self.scope.setAccessibleName("Scope")
        sl.addWidget(self.scope); sl.addWidget(muted("Nothing ticked means it is changed at any time."))
        more.addWidget(gb, 1); more.addWidget(sc, 1)
        ev.addLayout(more)
        self.body.addWidget(self.editor)
        self.body.addStretch(1)

        self.table.itemSelectionChanged.connect(self._load_editor)
        self.btn_add.clicked.connect(self._add); self.btn_del.clicked.connect(self._remove)
        for w in (self.n_min, self.n_max, self.n_step):
            w.valueChanged.connect(self._apply)
        self.t_len.valueChanged.connect(self._apply); self.t_alpha.editingFinished.connect(self._apply)
        self.c_list.itemChanged.connect(lambda *_: self._apply())
        self.btn_orig.clicked.connect(self._use_original)
        self.e_default.editingFinished.connect(self._apply_default)
        for cb, key in ((self.cb_skip, "skip"), (self.cb_ro, "read_only"), (self.cb_sens, "sensitive")):
            cb.toggled.connect(lambda v, k=key: self._flag(k, v))
        self.scope.itemChanged.connect(lambda *_: self._apply_scope())
        self._scope_sig: tuple = ()
        self.refresh()

    # ---- helpers
    def current(self) -> ControlDef | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        it = self.table.item(rows[0].row(), 0)
        return self.model.control(it.data(Qt.ItemDataRole.UserRole)) if it else None

    def refresh(self) -> None:
        if self._busy:
            return
        cur = self.current()
        cur_id = cur.id if cur else None
        ctrls = self.model.project.controls
        fit_table(self.table, len(ctrls), 3, 7)
        self.empty.setVisible(not ctrls); self.table.setVisible(bool(ctrls)); self.btn_del.setEnabled(bool(ctrls))
        names = {f.id: (f.name or f.id) for f in self.model.project.features}
        self.table.blockSignals(True)
        self.table.setRowCount(len(ctrls))
        for r, c in enumerate(ctrls):
            eff = c.fuzz_domain or c.domain
            flags = ", ".join(x for x, on in (("skip", c.skip), ("read-only", c.read_only), ("sensitive", c.sensitive)) if on) or "-"
            lvl = c.provenance.confidence if c.provenance.confidence in theme.CONFIDENCE else "unknown"
            cells = [c.id, KIND_LABEL.get(c.kind, c.kind), backend.describe_domain(c.domain),
                     backend.describe_domain(eff) + ("" if c.fuzz_domain else "  (same)"),
                     ", ".join(names.get(s, s) for s in c.scope) or "always", flags, theme.CONFIDENCE[lvl][0]]
            for col, v in enumerate(cells):
                it = QTableWidgetItem(v); it.setData(Qt.ItemDataRole.UserRole, c.id)
                if col == 6:
                    it.setIcon(theme.glyph(theme.CONFIDENCE[lvl][1], theme.sem(theme.CONFIDENCE[lvl][2])))
                self.table.setItem(r, col, it)
            if c.id == cur_id:
                self.table.selectRow(r)
        self.table.blockSignals(False)
        if cur_id is None and ctrls and not self.table.selectionModel().selectedRows():
            self.table.selectRow(0)
        self._load_editor()

    def _load_editor(self) -> None:
        c = self.current()
        self.editor.setVisible(c is not None)
        if c is None:
            return
        self._busy = True
        try:
            self.e_title.setText(f"Editing: {c.id}")
            self.o_range.setText(backend.describe_domain(c.domain))
            self.o_badge.set_confidence(c.provenance.confidence if c.provenance.confidence in theme.CONFIDENCE else "unknown")
            d = c.fuzz_domain or c.domain
            k = c.domain.kind
            self.stack.setCurrentIndex({"integer": 1, "float": 1, "choice": 2, "text": 3}.get(k, 0))
            if k in ("integer", "float"):
                dec = 0 if k == "integer" else 6
                for w in (self.n_min, self.n_max, self.n_step):
                    w.setDecimals(dec)
                # The editor can only narrow the app's own range (going outside it is the Plan's "invalid values" option).
                lo = c.domain.minimum if c.domain.minimum is not None else -1e12
                hi = c.domain.maximum if c.domain.maximum is not None else 1e12
                for w in (self.n_min, self.n_max):
                    w.setRange(lo, hi)
                self.n_step.setRange(0, max(hi - lo, 1))
                self.n_min.setValue(d.minimum if d.minimum is not None else 0)
                self.n_max.setValue(d.maximum if d.maximum is not None else 0)
                self.n_step.setValue(d.step or 0)
            elif k == "choice":
                self.c_list.clear()
                for o in c.domain.options:
                    it = QListWidgetItem(o); it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    it.setCheckState(Qt.CheckState.Checked if o in d.options else Qt.CheckState.Unchecked)
                    self.c_list.addItem(it)
            elif k == "text":
                self.t_len.setValue(d.max_length or 0); self.t_alpha.setText(d.alphabet)
            self.btn_orig.setEnabled(c.fuzz_domain is not None)
            self.e_default.setText("" if c.default is None else str(c.default))
            self.cb_skip.setChecked(c.skip); self.cb_ro.setChecked(c.read_only); self.cb_sens.setChecked(c.sensitive)
            sig = tuple((f.id, f.name) for f in self.model.project.features)
            self.scope.clear()
            for f in self.model.project.features:
                if f.kind == "action":
                    continue
                it = QListWidgetItem(f.name or f.id); it.setData(Qt.ItemDataRole.UserRole, f.id)
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Checked if f.id in c.scope else Qt.CheckState.Unchecked)
                self.scope.addItem(it)
            self._warn(c)
        finally:
            self._busy = False

    def _warn(self, c: ControlDef) -> None:
        self.r_warn.setText("")
        if c.fuzz_domain is None:
            return
        try:
            from ..domain import subset_reasons
            reasons = subset_reasons(c.fuzz_domain, c.domain)
        except Exception:
            reasons = []
        if reasons:
            self.r_warn.setText("Problem: " + "; ".join(reasons) + ". Start is blocked until this is back inside the app's own range. "
                                "To try out-of-range values on purpose, tick the invalid-values option on the Plan page instead.")
            self.r_warn.setStyleSheet(f"color:{theme.sem_hex('fail')};")

    # ---- edits
    def _build_domain(self, c: ControlDef) -> Domain:
        d = c.domain; k = d.kind
        if k in ("integer", "float"):
            cast = int if k == "integer" else float
            step = self.n_step.value()
            return dc.replace(d, minimum=cast(self.n_min.value()), maximum=cast(self.n_max.value()), step=(cast(step) if step else None))
        if k == "choice":
            opts = tuple(self.c_list.item(i).text() for i in range(self.c_list.count()) if self.c_list.item(i).checkState() == Qt.CheckState.Checked)
            return dc.replace(d, options=opts)
        if k == "text":
            return dc.replace(d, max_length=self.t_len.value(), alphabet=self.t_alpha.text())
        return d

    def _apply(self, *_a) -> None:
        c = self.current()
        if c is None or self._busy:
            return
        if c.domain.kind in ("integer", "float") and self.n_min.value() > self.n_max.value():
            # keep the range valid: the box the tester did NOT just edit follows
            self._busy = True
            try:
                (self.n_max if self.sender() is self.n_min else self.n_min).setValue(self.n_min.value() if self.sender() is self.n_min else self.n_max.value())
            finally:
                self._busy = False
        nd = self._build_domain(c)
        new = None if nd == c.domain else nd
        if new == c.fuzz_domain:
            return
        self._busy = True
        try:
            self.model.update_control(c.id, fuzz_domain=new)
        finally:
            self._busy = False
        c2 = self.model.control(c.id)
        self.btn_orig.setEnabled(c2.fuzz_domain is not None)
        self._warn(c2)
        self._refresh_row(c2)

    def _refresh_row(self, c: ControlDef) -> None:
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == c.id:
                eff = c.fuzz_domain or c.domain
                self.table.item(r, 3).setText(backend.describe_domain(eff) + ("" if c.fuzz_domain else "  (same)"))

    def _use_original(self) -> None:
        c = self.current()
        if c:
            self.model.update_control(c.id, fuzz_domain=None)
            self._load_editor()

    def _apply_default(self) -> None:
        c = self.current()
        if c is None or self._busy:
            return
        txt = self.e_default.text().strip()
        try:
            val = {"integer": lambda t: int(float(t)), "float": float, "boolean": lambda t: t.lower() in ("1", "true", "on", "yes")}.get(c.kind, str)(txt) if txt != "" else (None if c.kind != "text" else "")
        except ValueError:
            self.e_default.setStyleSheet(f"border:1px solid {theme.sem_hex('fail')};")
            self.e_default.setToolTip("That is not a valid value for this control type.")
            return
        self.e_default.setStyleSheet(""); self.e_default.setToolTip("")
        if val != c.default:
            self._busy = True
            try:
                self.model.update_control(c.id, default=val)
            finally:
                self._busy = False

    def _flag(self, key: str, v: bool) -> None:
        c = self.current()
        if c and not self._busy and getattr(c, key) != v:
            self._busy = True
            try:
                self.model.update_control(c.id, **{key: v})
            finally:
                self._busy = False
            self.refresh_row_flags()

    def refresh_row_flags(self) -> None:
        self._busy = False
        self.refresh()

    def _apply_scope(self) -> None:
        c = self.current()
        if c is None or self._busy:
            return
        scope = tuple(self.scope.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.scope.count())
                      if self.scope.item(i).checkState() == Qt.CheckState.Checked)
        if scope != c.scope:
            self._busy = True
            try:
                self.model.update_control(c.id, scope=scope)
            finally:
                self._busy = False
            self.refresh_row_flags()

    def _add(self) -> None:
        kind_label, ok = QInputDialog.getItem(self, "Add control", "What kind of value is it?", list(KIND_LABEL.values()), 1, False)
        if not ok:
            return
        kind = next(k for k, v in KIND_LABEL.items() if v == kind_label)
        doms = {"boolean": Domain("boolean"), "integer": Domain("integer", 0, 100, 1), "float": Domain("float", 0.0, 1.0, 0.1),
                "choice": Domain("choice", options=("a", "b")), "text": Domain("text", max_length=32)}
        defaults = {"boolean": False, "integer": 0, "float": 0.0, "choice": "a", "text": ""}
        cid = self.model.unique_id("control")
        self.model.add_control(ControlDef(cid, kind, Selector(), doms[kind], None, defaults[kind], provenance=Provenance("manual", "manual", "")))
        self.table.selectRow(len(self.model.project.controls) - 1)

    def _remove(self) -> None:
        c = self.current()
        if c:
            self.model.remove_control(c.id)
