"""Step 6: live run dashboard."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QProgressBar, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..types import Action, RunStats
from . import backend, theme
from .widgets import Atlas, PageBase, Ring, muted

STATE_TEXT = {
    "idle": "Ready. Press Start when you are happy with the plan.", "starting": "Starting the program and the test worker...",
    "running": "Running. You can leave this open; press Stop when you want to finish.", "paused": "Paused. Press Resume to continue.",
    "stopping": "Stopping safely...", "finished": "Finished.",
}


STOP_REASONS = {
    "stopped by user": "you stopped it.", "stopped": "you stopped it.", "max_seconds": "the time limit was reached.",
    "max_cases": "the test-case limit was reached.", "max_unique_errors": "the error limit was reached.",
    "first_error": "the first error was found.", "coverage_complete": "every state has been visited once.",
}


def friendly_reason(reason: str) -> str:
    """One calm, single-line sentence (the raw reason may be a multi-line traceback)."""
    reason = " ".join(str(reason).split())
    if reason in STOP_REASONS:
        return STOP_REASONS[reason]
    if reason.startswith("controller error"):
        return "Overgrind itself hit a problem and could not continue (not an app bug). Details: " + reason[:200]
    return reason[:240]


def state_visits(cv) -> dict[tuple[str, str], int]:
    """Exact per-state visit counts if the backend supplies them on Coverage (tolerates a few shapes), else {}."""
    for name in ("state_visits", "visits", "visit_counts"):
        raw = getattr(cv, name, None)
        if not raw:
            continue
        out: dict[tuple[str, str], int] = {}
        for k, v in dict(raw).items():
            if isinstance(k, tuple) and len(k) == 2:
                out[(str(k[0]), str(k[1]))] = int(v)
            elif isinstance(v, dict):
                for st, n in v.items():
                    out[(str(k), str(st))] = int(n)
        return out
    return {}


def fmt_time(s: float) -> str:
    s = int(s)
    return f"{s // 3600:d}:{(s % 3600) // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


class Tile(QFrame):
    def __init__(self, caption: str, icon: str | None = None):
        super().__init__()
        self.setObjectName("Card")
        l = QVBoxLayout(self); l.setContentsMargins(10, 6, 10, 6); l.setSpacing(0)
        self.cap = QLabel(caption); self.cap.setStyleSheet(f"color:{theme.sem_hex('muted')};")
        self.val = QLabel("0"); f = self.val.font(); f.setPointSizeF(f.pointSizeF() * 1.8); f.setBold(True); self.val.setFont(f)
        l.addWidget(self.cap); l.addWidget(self.val)
        self.setAccessibleName(caption)

    def set(self, v: str) -> None:
        self.val.setText(v)
        self.setAccessibleDescription(f"{self.cap.text()}: {v}")


def new_actions(prev: tuple[Action, ...], cur: tuple[Action, ...]) -> tuple[Action, ...]:
    """Actions in `cur` not already seen in `prev` (both are consecutive ring tails)."""
    for k in range(min(len(prev), len(cur)), 0, -1):
        if prev[-k:] == cur[:k]:
            return cur[k:]
    return cur


class RunPage(PageBase):
    title = "Run the test"
    hint = "Press <b>Start</b>. Overgrind drives the program and only keeps a record when something goes wrong. Press <b>Stop</b> whenever you like."

    def __init__(self, model, session, parent=None):
        super().__init__(parent)
        self.model, self.session = model, session
        self.issues: list[tuple[str, str, int]] = []
        self._visits: dict[tuple[str, str], int] = {}
        self._errored: set[tuple[str, str]] = set()
        self._prev_recent: tuple[Action, ...] = ()
        self._atlas_sig: tuple | None = None
        self.revalidate = None            # set by the main window: re-runs project.validate() right now
        self._acked = ""                  # project fingerprint the tester already confirmed risks for

        bar = QHBoxLayout()
        self.btn_start = QPushButton("  Start"); self.btn_start.setObjectName("Big"); self.btn_start.setIcon(theme.glyph("play", size=20))
        self.btn_pause = QPushButton("  Pause"); self.btn_pause.setObjectName("Big"); self.btn_pause.setIcon(theme.glyph("pause", size=20))
        self.btn_stop = QPushButton("  Stop"); self.btn_stop.setObjectName("Big"); self.btn_stop.setIcon(theme.glyph("stop", size=20))
        self.btn_start.setToolTip("Start testing (F5). Overgrind drives the program and keeps a record only when something fails.")
        self.btn_pause.setToolTip("Pause or resume the test (F6). The program stays open.")
        self.btn_stop.setToolTip("Stop the test and keep the results (Shift+F5).")
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            bar.addWidget(b)
        bar.addStretch(1)
        self.body.addLayout(bar)
        self.state_label = QLabel(); self.state_label.setWordWrap(True); self.body.addWidget(self.state_label)
        self.block_label = QLabel(); self.block_label.setWordWrap(True); self.block_label.setTextFormat(Qt.TextFormat.RichText)
        self.block_label.linkActivated.connect(lambda h: self.jump.emit(int(h)))
        self.body.addWidget(self.block_label)

        grid = QGridLayout(); grid.setSpacing(8)
        self.t_elapsed = Tile("Time running"); self.t_cases = Tile("Test cases"); self.t_rate = Tile("Cases per second")
        self.t_errors = Tile("Different errors"); self.t_crash = Tile("Crashes"); self.t_hang = Tile("Hangs")
        self.t_harness = Tile("Overgrind problems"); self.t_restart = Tile("Restarts")
        tiles = [self.t_elapsed, self.t_cases, self.t_rate, self.t_errors, self.t_crash, self.t_hang, self.t_harness, self.t_restart]
        for i, t in enumerate(tiles):
            grid.addWidget(t, i // 4, i % 4)
        self.t_harness.setToolTip("The test tool could not drive the app. This is not counted as an app bug.")
        self.body.addLayout(grid)
        self.current = muted("", wrap=True); self.body.addWidget(self.current)

        box = QGroupBox("Feature-State Atlas"); bl = QVBoxLayout(box)
        top = QHBoxLayout()
        self.atlas = Atlas(); top.addWidget(self.atlas, 1)
        right = QVBoxLayout()
        self.ring = Ring(); right.addWidget(self.ring)
        self.bar = QProgressBar(); self.bar.setTextVisible(True); self.bar.setFormat("Value ranges: %p%"); self.bar.setFixedWidth(150)
        self.bar.setAccessibleName("Value range coverage")
        right.addWidget(self.bar); right.addStretch(1)
        top.addLayout(right)
        bl.addLayout(top)
        self.cov_caption = muted("")
        self.btn_copy_cov = QPushButton("Copy coverage report")
        self.btn_copy_cov.setToolTip("Copy what this run covered, in plain text, to paste into a report.")
        self.guarantee = QLabel("What this run guarantees will appear here once it starts."); self.guarantee.setWordWrap(True)
        self.guarantee.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = self.guarantee.font(); f.setItalic(True); self.guarantee.setFont(f)
        self.guarantee.setAccessibleName("Coverage guarantee")
        bl.addWidget(self.cov_caption); bl.addWidget(self.guarantee); bl.addWidget(self.btn_copy_cov, 0, Qt.AlignmentFlag.AlignLeft)
        self.body.addWidget(box)

        self.body.addWidget(QLabel("<b>Recent steps</b>"))
        self.tail = QListWidget(); self.tail.setMaximumHeight(130); self.tail.setAccessibleName("Recent steps")
        self.tail.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.body.addWidget(self.tail)
        self.body.addStretch(1)

        self.btn_start.clicked.connect(self.start)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop.clicked.connect(self.session.stop)
        self.btn_copy_cov.clicked.connect(self.copy_coverage)
        self.session.errorFound.connect(self.on_error)
        self.session.statsUpdated.connect(self.on_stats)
        self.session.stateChanged.connect(self.on_state)
        self.session.problem.connect(self.on_problem)
        self.on_state(self.session.state)
        self.model.preflightChanged.connect(self.refresh_blockers)
        self.refresh()

    # ---- inputs
    def set_issues(self, issues) -> None:
        self.issues = issues
        self._update_buttons()

    def refresh(self) -> None:
        rows = [(f.id, f.name or f.id, f.states) for f in self.model.project.features if f.kind != "action" and f.enabled]
        sig = tuple(rows)
        if sig != self._atlas_sig:               # only a changed feature list invalidates the picture of the last run
            self._atlas_sig = sig
            self.atlas.set_features(rows)
            if not self.session.active:
                self._visits = {}; self._errored = set(); self.atlas.set_visits({}); self.atlas.set_errored(())
        self._update_buttons()

    def _blocking(self):
        return [i for i in self.issues if i[0] == "fail"]

    def preflight_blockers(self) -> list[dict]:
        """Failed Trust Ledger rows of the latest preflight, only while it still describes this exact project."""
        pf = self.model.preflight
        if not pf or pf.get("fingerprint") != self.model.fingerprint():
            return []
        return [r for r in pf["rows"] if r.get("status") == "fail"]

    def risk_notes(self) -> list[str]:
        """Things the tester must knowingly accept before the first Start of this project version."""
        notes = [f"'{f.name or f.id}' is marked dangerous and is switched on, so it will be exercised."
                 for f in self.model.project.features if f.dangerous and f.enabled]
        notes += [f"We could not verify the type or range of control '{c.id}'. We will try it anyway."
                  for c in self.model.project.controls if c.provenance.confidence == "unknown" and not c.skip]
        notes += [f"The preflight check failed for {r['element']}: {r['message']}" for r in self.preflight_blockers()]
        return notes

    def refresh_blockers(self) -> None:
        self._update_buttons()

    def _update_buttons(self) -> None:
        st = self.session.state
        busy = st in ("starting", "running", "paused", "stopping")
        block = self._blocking()
        self.btn_start.setEnabled(not busy and not block)
        self.btn_pause.setEnabled(st in ("running", "paused"))
        self.btn_stop.setEnabled(st in ("running", "paused"))
        self.btn_pause.setText("  Resume" if st == "paused" else "  Pause")
        self.btn_pause.setIcon(theme.glyph("play" if st == "paused" else "pause", size=20))
        pf = self.preflight_blockers()
        if block and not busy:
            first = block[0]
            more = f" (and {len(block) - 1} more)" if len(block) > 1 else ""
            self.block_label.setText(f"<b>Can't start yet:</b> {html.escape(first[1])}{more} <a href='{first[2]}'>Fix this</a>")
            self.block_label.setStyleSheet(f"color:{theme.sem_hex('fail')};")
        elif pf and not busy:
            self.block_label.setText(f"<b>The preflight check found {len(pf)} problem(s).</b> You can fix them on the Plan page, "
                                     f"or start anyway after confirming. <a href='4'>Open the Plan page</a>")
            self.block_label.setStyleSheet(f"color:{theme.sem_hex('warn')};")
        else:
            self.block_label.setText(""); self.block_label.setStyleSheet("")

    def _confirm_name(self, title: str, text: str, expected: str) -> bool:
        """Typed confirmation: the tester must type the project name (a click is too easy to do by reflex)."""
        typed, ok = QInputDialog.getText(self, title, text + f"\n\nTo start anyway, type the project name exactly:\n{expected}", QLineEdit.EchoMode.Normal, "")
        return bool(ok) and typed.strip() == expected.strip()

    def start(self) -> None:
        if self.session.active:
            return
        if self.revalidate:
            self.revalidate()                  # never trust the debounced banner: the project may have changed a moment ago
        if self._blocking():
            return
        fp = self.model.fingerprint()
        notes = self.risk_notes()
        if notes and self._acked != fp:
            body = "Please review before the first run:\n\n- " + "\n- ".join(notes)
            if not self._confirm_name("Review before starting", body, self.model.project.name):
                return
            self._acked = fp
        try:
            spec = backend.resolve_spec(self.model.adapter_spec)
        except backend.BackendMissing as e:
            self.on_problem(str(e)); return
        self._visits = {}; self._errored = set(); self._prev_recent = ()
        self.atlas.set_visits({}); self.atlas.set_errored(()); self.tail.clear()
        self.session.start(self.model.project, spec, self.model.stop)

    def toggle_pause(self) -> None:
        (self.session.resume if self.session.state == "paused" else self.session.pause)()

    # ---- outputs
    def on_state(self, st: str) -> None:
        text = STATE_TEXT.get(st, st)
        if st == "finished" and self.session.last_stats.stop_reason:
            text = "Finished: " + friendly_reason(self.session.last_stats.stop_reason)
        self.state_label.setText(text)
        self._update_buttons()

    def on_problem(self, msg: str) -> None:
        self.state_label.setText(msg)
        self.state_label.setStyleSheet(f"color:{theme.sem_hex('fail')};")

    def on_stats(self, st: RunStats) -> None:
        self.state_label.setStyleSheet("")
        self.t_elapsed.set(fmt_time(st.elapsed_s)); self.t_cases.set(f"{st.cases:,}"); self.t_rate.set(f"{st.cases_per_s:,.1f}")
        self.t_errors.set(f"{st.errors_unique:,}"); self.t_crash.set(f"{st.crashes:,}"); self.t_hang.set(f"{st.hangs:,}")
        self.t_harness.set(f"{st.harness_errors:,}"); self.t_restart.set(f"{st.restarts:,}")
        self.current.setText(f"Now: {st.current}" if st.current else "")
        exact = state_visits(st.coverage)
        if exact:
            self.atlas.sampled = False
            self._visits = exact
        else:                                  # fall back to counting the sampled action tail
            self.atlas.sampled = True
            for a in new_actions(self._prev_recent, st.recent):
                if a.kind == "state":
                    k = (a.target, str(a.value))
                    self._visits[k] = self._visits.get(k, 0) + 1
        self._prev_recent = st.recent
        self.atlas.set_visits(dict(self._visits))
        self._coverage(st)
        self.tail.clear()
        for a in st.recent[-8:]:
            mark = "" if a.ok else "  [failed]"
            self.tail.addItem(f"{a.kind}: {a.target}" + ("" if a.value is None else f" = {a.value}") + mark)
        self.tail.scrollToBottom()
        if st.state == "finished":
            self.on_state("finished")

    def on_error(self, ev) -> None:
        shown = {(f.id, s) for f in self.model.project.features for s in f.states}
        self._errored |= {(k, str(v)) for k, v in ev.case.state.items() if (k, str(v)) in shown}
        self.atlas.set_errored(self._errored)

    def coverage_report(self) -> str:
        st = self.session.last_stats
        cv = st.coverage
        lines = [f"# Coverage report: {self.model.project.name}", "", cv.guarantee or "No coverage statement is available.", "",
                 f"- Test cases run: {st.cases:,}", f"- Feature states seen: {cv.states_visited:,}" + (f" of {cv.states_total:,}" if cv.states_total else " (total not countable)"),
                 f"- Full passes through all states: {cv.epochs}", f"- Different errors: {st.errors_unique:,}"]
        if cv.buckets_total:
            lines.append(f"- Value ranges tried: {cv.buckets_visited:,} of {cv.buckets_total:,}")
        if self._errored:
            lines.append("- States where an error happened: " + ", ".join(f"{a}={b}" for a, b in sorted(self._errored)))
        return "\n".join(lines) + "\n"

    def copy_coverage(self) -> None:
        QGuiApplication.clipboard().setText(self.coverage_report())
        self.cov_caption.setText("Copied. Paste it into your report.")

    def _coverage(self, st: RunStats) -> None:
        cv = st.coverage
        if cv.tway_t is not None and cv.tway_total:
            frac = cv.tway_covered / cv.tway_total
            self.ring.set_value(frac, f"{frac:.0%}", f"{cv.tway_t}-way combos: {cv.tway_covered:,} of {cv.tway_total:,}")
        elif cv.states_total:
            frac = cv.states_visited / cv.states_total
            self.ring.set_value(frac, f"{frac:.0%}", f"States seen: {cv.states_visited:,} of {cv.states_total:,}")
        else:
            self.ring.set_value(None, "?", f"States seen: {cv.states_visited:,} (total not countable)")
        self.bar.setVisible(bool(cv.buckets_total))
        if cv.buckets_total:
            self.bar.setRange(0, cv.buckets_total); self.bar.setValue(cv.buckets_visited)
            self.bar.setFormat(f"Value ranges: {cv.buckets_visited:,}/{cv.buckets_total:,}")
        self.cov_caption.setText(f"Full passes through all states: {cv.epochs}")
        self.guarantee.setText(cv.guarantee or "No coverage statement is available for this strategy yet.")
