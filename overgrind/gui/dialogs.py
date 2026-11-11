"""Dialogs: Compare builds, About, Keyboard shortcuts."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QProgressBar, QPushButton, QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QTextBrowser,
    QVBoxLayout, QWidget,
)

from ..types import AdapterSpec, Project
from . import backend, theme
from .workers import run_async

VERSION = "0.2.0-dev"
SHORTCUTS = [
    ("Ctrl+N / Ctrl+O", "New / Open project"), ("Ctrl+S / Ctrl+Shift+S", "Save / Save As"), ("Ctrl+Z / Ctrl+Y", "Undo / Redo"),
    ("Ctrl+1 ... Ctrl+7", "Jump to a step"), ("Alt+Left / Alt+Right", "Back / Next"), ("Ctrl+F", "Search the element tree"),
    ("F4", "Scan the app"), ("Ctrl+R", "Run the preflight check"), ("F5", "Start the test"), ("F6", "Pause / Resume"),
    ("Shift+F5", "Stop the test"), ("F1", "This list"), ("Ctrl+Q", "Quit"),
]


class SpecPicker(QGroupBox):
    """Choose one build: the built-in demo app, or a Windows EXE."""

    def __init__(self, title: str, demo_kwargs: dict, parent=None):
        super().__init__(title, parent)
        self.demo_kwargs = demo_kwargs
        f = QFormLayout(self)
        self.kind = QComboBox(); self.kind.addItems(["Demo app (sample, works anywhere)", "Windows program (EXE)"])
        self.kind.setAccessibleName(f"{title} type")
        self.path = QLineEdit(); self.path.setPlaceholderText(r"C:\Builds\MyApp\myapp.exe")
        self.browse = QPushButton("Browse...")
        row = QHBoxLayout(); row.addWidget(self.path, 1); row.addWidget(self.browse)
        f.addRow("Kind:", self.kind); f.addRow("Program file:", row)
        self.browse.clicked.connect(self._browse)
        self.kind.currentIndexChanged.connect(self._sync); self._sync()

    def _sync(self) -> None:
        exe = self.kind.currentIndex() == 1
        self.path.setEnabled(exe); self.browse.setEnabled(exe)

    def _browse(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Choose a build", "", "Programs (*.exe);;All files (*)")
        if p:
            self.path.setText(p)

    def build(self) -> tuple[AdapterSpec, object]:
        """(adapter spec, optional factory that launches this build's EXE)."""
        if self.kind.currentIndex() == 0:
            return AdapterSpec("overgrind.adapters.mock:MockAdapter", dict(self.demo_kwargs)), None
        exe = self.path.text().strip()
        if not exe:
            raise ValueError("Choose the program file for this build.")
        spec = AdapterSpec("overgrind.adapters.uia:UiaAdapter")
        return spec, backend.exe_factory(spec, exe)


class CompareDialog(QDialog):
    """Compare builds in three steps: choose builds, review how controls were matched, read the result."""
    STATUS = {"mapped": ("Matched", "ok"), "review": ("Please check", "warn"), "unmatched": ("No match", "fail")}

    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Compare builds")
        self.resize(760, 580); self.setMinimumSize(420, 320)
        self.rows: list[dict] = []
        self.elems_b: list = []
        self._specs = None
        self._prog = {"text": ""}
        lay = QVBoxLayout(self)
        self.stack = QStackedWidget(); lay.addWidget(self.stack, 1)

        # step 1: builds
        p1 = QWidget(); l1 = QVBoxLayout(p1)
        l1.addWidget(self._lbl("Run the same test cases on an older build (A) and a newer build (B). We report what got worse, what got better, and what can't be compared."))
        self.a = SpecPicker("Build A (older, the baseline)", {"seed": 1})
        self.b = SpecPicker("Build B (newer, being checked)", {"seed": 1, "bad_combo": {"cb_alpha": "on", "cmb_mode": "High"}})
        l1.addWidget(self.a); l1.addWidget(self.b)
        row = QHBoxLayout()
        self.n = QSpinBox(); self.n.setRange(1, 100000); self.n.setValue(20)
        row.addWidget(QLabel("Number of test cases:")); row.addWidget(self.n); row.addStretch(1)
        self.next1 = QPushButton("Next: review matches"); self.next1.setObjectName("Primary"); row.addWidget(self.next1)
        l1.addLayout(row); l1.addStretch(1)
        self.stack.addWidget(p1)

        # step 2: mapping review
        p2 = QWidget(); l2 = QVBoxLayout(p2)
        l2.addWidget(self._lbl("<b>Check how each control was matched.</b> A wrong match would blame the wrong control, so anything unsure is left out "
                               "unless you confirm it. Pick a different element from the list to override."))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Control in A", "Match in B", "Similarity", "Result", "Your decision"])
        self.table.setAccessibleName("Control matches between the two builds")
        self.table.verticalHeader().setVisible(False); self.table.setWordWrap(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        h = self.table.horizontalHeader(); h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch); h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c, w in ((2, 150), (3, 110), (4, 170)):
            self.table.setColumnWidth(c, w)
        l2.addWidget(self.table, 1)
        r2 = QHBoxLayout()
        self.back2 = QPushButton("Back"); self.go = QPushButton("Run comparison"); self.go.setObjectName("Primary")
        r2.addWidget(self.back2); r2.addStretch(1); r2.addWidget(self.go)
        l2.addLayout(r2)
        self.stack.addWidget(p2)

        # step 3: result
        p3 = QWidget(); l3 = QVBoxLayout(p3)
        self.out = QTextBrowser(); self.out.setAccessibleName("Comparison result"); l3.addWidget(self.out, 1)
        r3 = QHBoxLayout()
        self.back3 = QPushButton("Back to matches"); self.btn_save = QPushButton("Save report..."); self.btn_save.setEnabled(False)
        r3.addWidget(self.back3); r3.addStretch(1); r3.addWidget(self.btn_save)
        l3.addLayout(r3)
        self.stack.addWidget(p3)

        self.progress = QProgressBar(); self.progress.setVisible(False); self.progress.setRange(0, 0); self.progress.setTextVisible(True)
        self.progress.setAccessibleName("Comparison progress")
        lay.addWidget(self.progress)
        self.status = QLabel(""); self.status.setWordWrap(True); lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); bb.rejected.connect(self.reject); lay.addWidget(bb)
        self.next1.clicked.connect(self.review)
        self.back2.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        self.back3.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        self.go.clicked.connect(self.compare)
        self.btn_save.clicked.connect(self._save)
        self._poll = QTimer(self); self._poll.setInterval(200); self._poll.timeout.connect(lambda: self.progress.setFormat(self._prog["text"]))
        self._report_md = ""

    @staticmethod
    def _lbl(t: str) -> QLabel:
        l = QLabel(t); l.setWordWrap(True); l.setTextFormat(Qt.TextFormat.RichText); return l

    def _busy(self, on: bool, text: str = "") -> None:
        for b in (self.next1, self.go, self.back2, self.back3):
            b.setEnabled(not on)
        self.progress.setVisible(on); self._prog["text"] = text; self.progress.setFormat(text)
        (self._poll.start if on else self._poll.stop)()

    # ---- step 1 -> 2
    def review(self) -> None:
        try:
            (a, fa), (b, fb) = self.a.build(), self.b.build()
        except ValueError as e:
            self.status.setText(str(e)); return
        self._specs = (a, fa, b, fb)
        proj = self.project
        self._busy(True, "Reading both builds (nothing is clicked)...")
        self.status.setText("")

        def work():
            ea, eb = backend.inspect_build(a, fa, proj), backend.inspect_build(b, fb, proj)
            return eb, backend.map_controls(proj, ea, eb)
        run_async(work, self._reviewed, self._fail)

    def _reviewed(self, res) -> None:
        self._busy(False)
        self.elems_b, self.rows = res
        self.fill_review()
        self.stack.setCurrentIndex(1)
        n = {k: sum(1 for r in self.rows if r["status"] == k) for k in self.STATUS}
        self.status.setText(f"{n['mapped']} matched, {n['review']} to check, {n['unmatched']} without a match." if self.rows else
                            "There are no features or controls to compare yet. Add some first.")

    def fill_review(self) -> None:
        self.table.clearContents(); self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            self.table.setItem(i, 0, QTableWidgetItem(f"{r['name']}  ({r['a']})"))
            self.table.setItem(i, 1, QTableWidgetItem(r["b"] or "(nothing found)"))
            sc = QTableWidgetItem(f"{r['score']:.2f}  " + "\u25a0" * round(r["score"] * 6) + "\u25a1" * (6 - round(r["score"] * 6)))
            sc.setToolTip("0 = nothing alike, 1 = identical (name, ID, type, place in the window).")
            self.table.setItem(i, 2, sc)
            word, colour = self.STATUS[r["status"]]
            it = QTableWidgetItem(word); it.setIcon(theme.glyph({"ok": "ok", "warn": "warn", "fail": "fail"}[colour], theme.sem(colour)))
            self.table.setItem(i, 3, it)
            cb = QComboBox(); cb.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon); cb.setMinimumContentsLength(8)
            cb.setAccessibleName(f"Decision for {r['name']}")
            cb.addItem("Use this match" if r["b_index"] is not None else "Use this match (none)", "accept")
            cb.addItem("Ignore this control", "ignore")
            for j, e in enumerate(self.elems_b[:300]):
                cb.addItem(f"Use instead: {e.name or e.control_type} ({e.selector.auto_id or e.control_type})", f"override:{e.selector.auto_id or r['id']}")
            cb.setCurrentIndex(0 if r["status"] == "mapped" else 1)       # unsure matches default to "ignore": never guess
            if r["b_index"] is None:
                cb.setCurrentIndex(1)
            self.table.setCellWidget(i, 4, cb)

    def decisions(self) -> dict[str, str]:
        out = {}
        for i, r in enumerate(self.rows):
            cb = self.table.cellWidget(i, 4)
            if cb is not None:
                out[r["id"]] = cb.currentData()
        return out

    # ---- step 2 -> 3
    def compare(self) -> None:
        if not self._specs:
            return
        a, fa, b, fb = self._specs
        proj, n = self.project, self.n.value()
        idmap = backend.id_map_from_review(self.rows, self.decisions())
        self._busy(True, "Calibrating: running build A against itself to find noisy controls...")
        self.status.setText("Both builds run one after the other. Differences that also appear when A is compared with itself are masked as noise.")
        prog = self._prog

        def work():
            noisy: set = set()
            try:
                aa = backend.differential(proj, a, a, n_cases=min(n, 10), seed=proj.run.seed, factory_a=fa, factory_b=fa)
                noisy = backend.noisy_targets(aa)
            except Exception:
                pass                      # calibration is best-effort; the real comparison still runs
            prog["text"] = f"Calibration done: {len(noisy)} noisy control(s) masked. Comparing A with B..."
            rep = backend.differential(proj, a, b, n_cases=n, seed=proj.run.seed, factory_a=fa, factory_b=fb, id_map=idmap)
            return backend.normalise_diff(rep, noisy)
        run_async(work, self._done, self._fail)

    def _fail(self, msg: str) -> None:
        self._busy(False)
        self.status.setText(f"The comparison could not run. {msg}")

    def _done(self, r: dict) -> None:
        self._busy(False)
        self.btn_save.setEnabled(True); self._report_md = r["markdown"]
        total = sum(len(r[k]) for k in ("regressions", "fixes", "mismatches", "incomparable"))
        self.status.setText("No differences found." if total == 0 else f"{len(r['regressions'])} regression(s), {len(r['fixes'])} fix(es), {len(r['mismatches'])} other difference(s), {len(r['incomparable'])} not comparable.")
        parts = [f"<p>{html.escape(r['summary'])}</p>"]
        if r.get("noisy_controls"):
            parts.append(f"<p><b>Noise calibration:</b> {len(r['noisy_controls'])} control(s) differ even when build A is run twice, so their differences are masked: "
                         + html.escape(", ".join(r["noisy_controls"])) + ".</p>")
        for key, title, note in (("regressions", "Regressions (worse in B)", "Worked in A, fails in B."),
                                 ("fixes", "Fixes (better in B)", "Failed in A, works in B."),
                                 ("mismatches", "Different results", "Both ran, but values or timing differ."),
                                 ("incomparable", "Cannot compare", "A control could not be found reliably in one build, so these cases say nothing either way."),
                                 ("noise", "Masked as noise", "These differences also appear when A is compared with itself.")):
            if key == "noise" and not r.get(key):
                continue
            parts.append(f"<h4>{title} ({len(r[key])})</h4><p><i>{note}</i></p>")
            if r[key]:
                parts.append("<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in r[key][:200]) + "</ul>")
        self.out.setHtml("".join(parts))
        self.stack.setCurrentIndex(2)

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save the comparison report", "comparison-report.md", "Markdown (*.md)")
        if path:
            try:
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(self._report_md)
                self.status.setText(f"Saved {path}.")
            except OSError as e:
                self.status.setText(f"Could not save the report. {e.strerror or e}")


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Overgrind")
        lay = QVBoxLayout(self)
        lab = QLabel(f"<h2>Overgrind {VERSION}</h2><p>Continuous black-box GUI fuzzing for Windows programs.<br>"
                     "You teach it what the program has; it tries the combinations and keeps only the failures.</p>"
                     "<p>We say exactly what was covered. We never claim to have tested every value.</p>")
        lab.setWordWrap(True); lab.setTextFormat(Qt.TextFormat.RichText); lay.addWidget(lab)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok); bb.accepted.connect(self.accept); lay.addWidget(bb)


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        lay = QVBoxLayout(self)
        rows = "".join(f"<tr><td style='padding-right:18px'><b>{html.escape(k)}</b></td><td>{html.escape(v)}</td></tr>" for k, v in SHORTCUTS)
        lab = QLabel(f"<table>{rows}</table>"); lab.setTextFormat(Qt.TextFormat.RichText); lay.addWidget(lab)
        lay.addWidget(QLabel("Everything can also be reached with Tab and the arrow keys."))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok); bb.accepted.connect(self.accept); lay.addWidget(bb)
