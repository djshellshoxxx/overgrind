"""Dialogs: Compare builds, About, Keyboard shortcuts."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QTextBrowser, QVBoxLayout, QWidget,
)

from ..types import AdapterSpec, Project
from . import backend
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

    def spec(self) -> AdapterSpec:
        if self.kind.currentIndex() == 0:
            return AdapterSpec("overgrind.adapters.mock:MockAdapter", dict(self.demo_kwargs))
        if not self.path.text().strip():
            raise ValueError("Choose the program file for this build.")
        return AdapterSpec("overgrind.adapters.uia:UiaAdapter", {"exe_path": self.path.text().strip()})


class CompareDialog(QDialog):
    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Compare builds")
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        lay.addWidget(self._lbl("Run the same test cases on an older build (A) and a newer build (B). We report what got worse, what got better, and what can't be compared."))
        self.a = SpecPicker("Build A (older, the baseline)", {"seed": 1})
        self.b = SpecPicker("Build B (newer, being checked)", {"seed": 1, "bad_combo": {"cb_alpha": "on", "cmb_mode": "High"}})
        lay.addWidget(self.a); lay.addWidget(self.b)
        row = QHBoxLayout()
        self.n = QSpinBox(); self.n.setRange(1, 100000); self.n.setValue(20)
        row.addWidget(QLabel("Number of test cases:")); row.addWidget(self.n); row.addStretch(1)
        self.go = QPushButton("Compare"); self.go.setObjectName("Primary"); row.addWidget(self.go)
        lay.addLayout(row)
        self.status = QLabel(""); self.status.setWordWrap(True); lay.addWidget(self.status)
        self.out = QTextBrowser(); self.out.setAccessibleName("Comparison result"); lay.addWidget(self.out, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); bb.rejected.connect(self.reject); lay.addWidget(bb)
        self.go.clicked.connect(self.compare)

    @staticmethod
    def _lbl(t: str) -> QLabel:
        l = QLabel(t); l.setWordWrap(True); return l

    def compare(self) -> None:
        try:
            a, b = self.a.spec(), self.b.spec()
        except ValueError as e:
            self.status.setText(str(e)); return
        self.go.setEnabled(False); self.status.setText("Comparing... both builds run one after the other.")
        proj, n = self.project, self.n.value()
        run_async(lambda: backend.normalise_diff(backend.differential(proj, a, b, n_cases=n, seed=proj.run.seed)), self._done, self._fail)

    def _fail(self, msg: str) -> None:
        self.go.setEnabled(True); self.status.setText(f"The comparison could not run. {msg}")

    def _done(self, r: dict) -> None:
        self.go.setEnabled(True)
        total = sum(len(r[k]) for k in ("regressions", "fixes", "mismatches", "incomparable"))
        self.status.setText("No differences found." if total == 0 else f"{len(r['regressions'])} regression(s), {len(r['fixes'])} fix(es), {len(r['mismatches'])} other difference(s), {len(r['incomparable'])} not comparable.")
        parts = [f"<p>{html.escape(r['summary'])}</p>"]
        for key, title, note in (("regressions", "Regressions (worse in B)", "Worked in A, fails in B."),
                                 ("fixes", "Fixes (better in B)", "Failed in A, works in B."),
                                 ("mismatches", "Different results", "Both ran, but values or timing differ."),
                                 ("incomparable", "Cannot compare", "A control could not be found reliably in one build, so these cases say nothing either way.")):
            parts.append(f"<h4>{title} ({len(r[key])})</h4><p><i>{note}</i></p>")
            if r[key]:
                parts.append("<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in r[key][:200]) + "</ul>")
        self.out.setHtml("".join(parts))


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
