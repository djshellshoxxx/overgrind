"""Step 7: errors list, detail, reproduce-and-shrink, export."""
from __future__ import annotations

import html
import os
import time
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QProgressBar, QPushButton,
    QSplitter, QTableView, QTextBrowser, QVBoxLayout, QWidget,
)

from ..types import ErrorEvent
from . import backend, theme
from .widgets import PageBase, empty_state, muted
from .workers import run_async

STEP_TEXT = {
    "reset": lambda t, v: "Put the app back to its defaults", "state": lambda t, v: f"Set feature <b>{t}</b> to <b>{v}</b>",
    "invoke": lambda t, v: f"Press <b>{t}</b>", "control": lambda t, v: f"Set control <b>{t}</b> to <b>{v}</b>",
    "wait": lambda t, v: f"Wait for the app to settle ({v} ms)", "restart": lambda t, v: "Restart the app",
}


def step_html(kind: str, target: str, value) -> str:
    fn = STEP_TEXT.get(kind)
    if value is None and kind in ("wait",):
        return "Wait for the app to settle"
    t, v = html.escape(str(target)), html.escape("(empty)" if value == "" else str(value))
    return fn(t, v) if fn else f"{html.escape(kind)} {t} {v}"


class _ClickLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, e) -> None:
        self.clicked.emit()


def fmt_ts(ts: str) -> str:
    """ISO timestamp -> the tester's local wall-clock time, e.g. '2026-10-14 15:13:17'."""
    try:
        d = datetime.fromisoformat(ts)
        if d.tzinfo is not None:
            d = d.astimezone()
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return ts.replace("T", " ")[:19]


COLS = ["Time", "Kind", "What happened", "Signature", "Seen", "Reliability"]
_REL_ICON = {"reliable": "ok", "flaky": "warn", "not_reproducible": "fail", "unconfirmed": "unknown"}
_REL_COLOUR = {"ok": "ok", "warn": "warn", "fail": "fail", "unknown": "muted"}


class ErrorTableModel(QAbstractTableModel):
    """Errors as a lazy model: rows are drawn from the events on demand, so 5,000 errors cost nothing to show and a
    reload with a few new errors only inserts those rows (no flicker, scroll position and selection survive)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[ErrorEvent] = []
        self._index: dict[str, int] = {}
        self._icons: dict[tuple[str, str], QIcon] = {}

    # -- model API
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLS)

    def headerData(self, s, o, role=Qt.ItemDataRole.DisplayRole):
        if o == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLS[s]
        return None

    def event_at(self, row: int) -> ErrorEvent:
        return self._rows[row]

    def row_of(self, eid: str) -> int:
        return self._index.get(eid, -1)

    def _icon(self, kind: str, colour: str) -> QIcon:
        key = (kind, colour)
        if key not in self._icons:
            self._icons[key] = theme.glyph(kind, theme.sem(colour) if colour in theme._SEMANTIC else theme.sem_hex_color(kind))
        return self._icons[key]

    def data(self, ix, role=Qt.ItemDataRole.DisplayRole):
        if not ix.isValid():
            return None
        e = self._rows[ix.row()]; c = ix.column()
        cat = e.category if e.category in theme.CATEGORY_LABEL else "harness"
        rel = e.reliability if e.reliability in theme.RELIABILITY else "unconfirmed"
        if role == Qt.ItemDataRole.DisplayRole:
            return (fmt_ts(e.ts), theme.CATEGORY_LABEL[cat], e.message or "(no message)", e.signature, f"{e.count:,}", theme.RELIABILITY[rel][0])[c]
        if role == Qt.ItemDataRole.UserRole:
            return e.id if c == 0 else cat if c == 1 else None
        if role == Qt.ItemDataRole.UserRole + 1:      # sort key
            return (e.ts, cat, e.message, e.signature, e.count, rel)[c]
        if role == Qt.ItemDataRole.DecorationRole:
            if c == 1:
                return self._icon(cat, "fail" if cat in ("crash",) else "warn" if cat in ("hang", "dialog") else "muted")
            if c == 5:
                ic = _REL_ICON[rel]
                return self._icon(ic, _REL_COLOUR[ic])
        if role == Qt.ItemDataRole.ToolTipRole:
            if c == 5:
                return theme.RELIABILITY[rel][2]
            if c in (2, 3):
                return (e.message if c == 2 else e.signature) or None
        return None

    # -- updates
    def set_events(self, evs) -> None:
        new = {e.id: e for e in evs}
        if any(i not in new for i in self._index):        # something disappeared: rebuild once
            self.beginResetModel()
            self._rows = sorted(new.values(), key=lambda e: e.ts)
            self._index = {e.id: n for n, e in enumerate(self._rows)}
            self.endResetModel()
            return
        for eid, e in new.items():
            r = self._index.get(eid)
            if r is not None and self._rows[r] != e:
                self._rows[r] = e
                self.dataChanged.emit(self.index(r, 0), self.index(r, len(COLS) - 1))
        added = sorted((e for i, e in new.items() if i not in self._index), key=lambda e: e.ts)
        if added:
            start = len(self._rows)
            self.beginInsertRows(QModelIndex(), start, start + len(added) - 1)
            for e in added:
                self._index[e.id] = len(self._rows); self._rows.append(e)
            self.endInsertRows()

    def clear(self) -> None:
        self.beginResetModel(); self._rows = []; self._index = {}; self.endResetModel()


class Proxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.category = ""; self.text = ""
        self.setSortRole(Qt.ItemDataRole.UserRole + 1)

    def filterAcceptsRow(self, row, parent) -> bool:
        e = self.sourceModel().event_at(row)
        if self.category and (e.category if e.category in theme.CATEGORY_LABEL else "harness") != self.category:
            return False
        if self.text:
            return self.text in f"{e.message} {e.signature}".lower()
        return True


class ErrorsPage(PageBase):
    title = "What went wrong"
    hint = "Pick a failure to see how to make it happen again. <b>Reproduce and shrink</b> replays it and removes every step that isn't needed."

    def __init__(self, model, session, parent=None):
        super().__init__(parent)
        self.model, self.session = model, session
        self.events: dict[str, ErrorEvent] = {}
        self.store = None
        self.dir = ""
        self._reduced_running = False
        self._reducer = None
        self._cancel = False

        bar = QHBoxLayout()
        self.cat = QComboBox(); self.cat.addItem("All kinds", ""); [self.cat.addItem(v, k) for k, v in theme.CATEGORY_LABEL.items()]
        self.cat.setAccessibleName("Filter by kind")
        self.search = QLineEdit(); self.search.setPlaceholderText("Filter by message or signature (Ctrl+F)"); self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Filter errors")
        self.btn_reload = QPushButton("Reload"); self.btn_reload.setToolTip("Read the error folder again.")
        bar.addWidget(self.cat); bar.addWidget(self.search, 1); bar.addWidget(self.btn_reload)
        self.body.addLayout(bar)
        self.empty = empty_state("No failures so far.\nFailures appear here the moment they happen.")
        self.body.addWidget(self.empty, 1)

        split = QSplitter(Qt.Orientation.Vertical); split.setChildrenCollapsible(False)
        self.sm = ErrorTableModel(self)
        self.proxy = Proxy(); self.proxy.setSourceModel(self.sm)
        self.view = QTableView(); self.view.setModel(self.proxy); self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setAlternatingRowColors(True); self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(26)
        self.view.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.view.setAccessibleName("Errors"); self.view.setMinimumHeight(120)
        h = self.view.horizontalHeader(); h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, w in enumerate((132, 92, 0, 96, 50, 124)):      # fixed widths: sizing to contents would read every row
            if w:
                h.resizeSection(col, w)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch); h.setMinimumSectionSize(48)
        split.addWidget(self.view)

        det = QWidget(); dl = QVBoxLayout(det); dl.setContentsMargins(0, 6, 0, 0); dl.setSpacing(6)
        top = QHBoxLayout()
        self.d_title = QLabel(); self.d_title.setWordWrap(True); self.d_title.setTextFormat(Qt.TextFormat.PlainText)
        f = self.d_title.font(); f.setBold(True); f.setPointSizeF(f.pointSizeF() * 1.15); self.d_title.setFont(f)
        self.badge = theme.Badge()
        top.addWidget(self.d_title, 1); top.addWidget(self.badge)
        dl.addLayout(top)
        # the actions come first so they are never below the fold
        btns = QHBoxLayout()
        self.btn_reduce = QPushButton("Reproduce and shrink"); self.btn_reduce.setObjectName("Primary")
        self.btn_cancel = QPushButton("Cancel"); self.btn_cancel.setVisible(False)
        self.btn_copy = QPushButton("Copy as issue"); self.btn_save = QPushButton("Save as file..."); self.btn_open = QPushButton("Open folder")
        self.btn_reduce.setToolTip("Replays this failure several times to see how reliable it is, then removes every step that is not needed.")
        self.btn_cancel.setToolTip("Stop shrinking. Whatever was found so far is kept.")
        self.btn_copy.setToolTip("Copy a ready-to-paste bug report: the shortest steps, the exact values and how reliable it is.")
        self.btn_save.setToolTip("Save the same report as a text file you can attach to an issue.")
        self.btn_open.setToolTip("Open the folder that holds the evidence for this failure (screenshot, details).")
        for b in (self.btn_reduce, self.btn_cancel, self.btn_copy, self.btn_save, self.btn_open):
            btns.addWidget(b)
        btns.addStretch(1)
        dl.addLayout(btns)
        self.progress = QProgressBar(); self.progress.setVisible(False); self.progress.setTextVisible(True)
        self.progress.setAccessibleName("Shrinking progress")
        dl.addWidget(self.progress)
        self.status = muted(""); dl.addWidget(self.status)
        mid = QHBoxLayout()
        self.detail = QTextBrowser(); self.detail.setOpenLinks(False); self.detail.setMinimumHeight(110)
        self.detail.setAccessibleName("Error details")
        mid.addWidget(self.detail, 1)
        self.shot = _ClickLabel(); self.shot.setFixedWidth(230); self.shot.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.shot.setVisible(False); self.shot.setAccessibleName("Screenshot of the failure"); self.shot.setToolTip("Click to open the screenshot full size.")
        self.shot.setCursor(Qt.CursorShape.PointingHandCursor)
        mid.addWidget(self.shot)
        dl.addLayout(mid, 1)
        split.addWidget(det)
        split.setStretchFactor(0, 2); split.setStretchFactor(1, 3)
        self.split = split
        self.body.addWidget(split, 1)

        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(500); self.timer.timeout.connect(self.reload)
        self.poll = QTimer(self); self.poll.setInterval(300); self.poll.timeout.connect(self._poll_reduce)
        self.cat.currentIndexChanged.connect(self._filter); self.search.textChanged.connect(self._filter)
        self.btn_reload.clicked.connect(self.reload)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self._show())
        self.view.doubleClicked.connect(lambda _ix: self._open())
        self.btn_reduce.clicked.connect(self._reduce); self.btn_copy.clicked.connect(self._copy); self.btn_open.clicked.connect(self._open)
        self.btn_cancel.clicked.connect(self._cancel_reduce); self.btn_save.clicked.connect(self._save)
        self.shot.clicked.connect(self._open_shot)
        session.errorFound.connect(lambda _e: self.timer.start())
        session.stateChanged.connect(lambda st: self.timer.start() if st in ("finished", "idle") else None)
        self._fill()

    # ---- data
    def set_dir(self, directory: str) -> None:
        self.dir = directory
        try:
            self.store = backend.evidence_store(directory) if directory else None
        except backend.BackendMissing:
            self.store = None
        self.events = {}; self.sm.clear(); self._fill()
        self.reload()

    def reload(self) -> None:
        if self.session.evidence_dir and self.session.evidence_dir != self.dir:
            self.set_dir(self.session.evidence_dir); return
        store = self.store
        if store is None:
            return
        run_async(store.list, self._loaded, lambda m: self.status.setText(f"Could not read the error folder. {m}"))

    def refresh(self) -> None:
        self.reload()

    def _loaded(self, evs) -> None:
        self.events = {e.id: e for e in evs}
        self._fill()

    def _fill(self) -> None:
        keep = self._current_id()
        self.sm.set_events(self.events.values())
        has = bool(self.events)
        self.empty.setVisible(not has); self.split.setVisible(has)
        if has:
            if keep:
                if keep != self._current_id():
                    self._select(keep)
            elif self.proxy.rowCount() and not self.view.selectionModel().hasSelection():
                self.view.selectRow(0)
        self._show()

    def _filter(self) -> None:
        self.proxy.category = self.cat.currentData() or ""
        self.proxy.text = self.search.text().strip().lower()
        self.proxy.invalidate()

    def _current_id(self) -> str:
        rows = self.view.selectionModel().selectedRows() if self.view.selectionModel() else []
        return self.proxy.index(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) if rows else ""

    def _select(self, eid: str) -> None:
        r = self.sm.row_of(eid)
        if r >= 0:
            ix = self.proxy.mapFromSource(self.sm.index(r, 0))
            if ix.isValid():
                self.view.selectRow(ix.row())

    def current(self) -> ErrorEvent | None:
        return self.events.get(self._current_id())

    # ---- detail
    def _show(self) -> None:
        e = self.current()
        for b in (self.btn_copy, self.btn_open, self.btn_save):
            b.setEnabled(e is not None)
        self.btn_reduce.setEnabled(e is not None and not self._reduced_running)
        if e is None:
            self.d_title.setText(""); self.detail.clear(); self.shot.setVisible(False); self.badge.setVisible(False)
            return
        self.badge.setVisible(True)
        cat = theme.CATEGORY_LABEL.get(e.category, e.category)
        self.d_title.setText(f"{cat}: {e.message or e.signature}")
        self.badge.set_reliability(e.reliability if e.reliability in theme.RELIABILITY else "unconfirmed")
        self.detail.setHtml(self._html(e))
        self._screenshot(e)

    def _html(self, e: ErrorEvent) -> str:
        names = {f.id: (f.name or f.id) for f in self.model.project.features}
        out = [f"<p>Seen <b>{e.count}</b> time(s). First recorded {html.escape(fmt_ts(e.ts))}. Error signature: <code>{html.escape(e.signature)}</code></p>"]
        red = e.reduced
        if red and red.get("steps") is not None:
            steps = [step_html(s.get("kind", ""), s.get("target", ""), s.get("value")) for s in red["steps"]]
            pct = red.get("reliability_pct")
            out.append(f"<h4>Shortest way to make it happen ({len(steps)} of {red.get('original_steps', len(steps))} recorded steps)</h4>")
            if e.reliability == "flaky":
                out.append("<p><b>Flaky:</b> it only fails some of the time, so this shortened list may not be complete.</p>")
            if pct is not None:
                out.append(f"<p>It failed in {pct}% of replays.</p>")
            state, vals = red.get("state", {}), red.get("values", {})
        else:
            steps = [step_html(a.kind, a.target, a.value) for a in e.history[-20:]]
            out.append(f"<h4>Last {len(steps)} steps before it failed (not shrunk yet)</h4>")
            state, vals = e.case.state, e.case.values
        out.append("<ol>" + "".join(f"<li>{s}</li>" for s in steps) + "</ol>" if steps else "<p>No steps were recorded.</p>")
        if state or vals:
            out.append("<h4>Settings in effect</h4><ul>")
            out += [f"<li>{html.escape(names.get(k, k))}: <b>{html.escape(str(v))}</b></li>" for k, v in state.items()]
            out += [f"<li>{html.escape(k)} = <b>{html.escape(str(v))}</b></li>" for k, v in vals.items()]
            out.append("</ul>")
        if e.attachments:
            out.append("<h4>Files kept</h4><ul>" + "".join(f"<li>{html.escape(k)}: {html.escape(v)}</li>" for k, v in e.attachments.items()) + "</ul>")
        return "".join(out)

    def _attachment_path(self, e: ErrorEvent, rel: str) -> str:
        return rel if os.path.isabs(rel) else os.path.join(self.dir, rel)

    def _screenshot(self, e: ErrorEvent) -> None:
        self._shot_path = ""
        for k, v in e.attachments.items():
            if v.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                path = self._attachment_path(e, v)
                px = QPixmap(path)
                if not px.isNull() and px.width() > 8 and px.height() > 8:
                    self.shot.setPixmap(px.scaled(226, 170, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                    self.shot.setVisible(True); self._shot_path = path
                    return
        self.shot.setVisible(False)

    def _open_shot(self) -> None:
        if getattr(self, "_shot_path", ""):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._shot_path))

    # ---- actions
    def _reduce(self) -> None:
        e = self.current()
        if e is None or self._reduced_running:
            return
        try:
            spec = backend.resolve_spec(self.model.adapter_spec)
        except backend.BackendMissing as ex:
            self.status.setText(str(ex)); return
        self._reduced_running = True; self._cancel = False; self._reducer = None
        self.btn_reduce.setEnabled(False); self.btn_cancel.setVisible(True)
        self.progress.setRange(0, 0); self.progress.setVisible(True); self.progress.setFormat("Replaying...")
        self.status.setText("Replaying the failure and removing unneeded steps. This can take a minute; the original evidence is never changed.")
        proj, store = self.model.project, self.store
        # Cancelling jumps the reducer's clock past its time budget, so it stops at the next replay and keeps the best result so far.
        clock = lambda: time.monotonic() + (1e9 if self._cancel else 0.0)  # noqa: E731

        def work():
            R = backend._fn("reduce", "Reducer")
            try:
                red = R(proj, spec, store=store, clock=clock)
            except TypeError:
                red = R(proj, spec)
            self._reducer = red
            try:
                return red.reduce(e)
            finally:
                try:
                    red.close()
                except Exception:
                    pass
        self.poll.start()
        run_async(work, self._reduced, self._reduce_fail)

    def _poll_reduce(self) -> None:
        red = self._reducer
        n, cap = getattr(red, "replays", None), getattr(red, "max_replays", None)
        if isinstance(n, int) and isinstance(cap, int) and cap > 0:
            self.progress.setRange(0, cap); self.progress.setValue(min(n, cap))
            self.progress.setFormat(f"Replays: {n} of at most {cap}")

    def _cancel_reduce(self) -> None:
        self._cancel = True
        self.btn_cancel.setEnabled(False)
        self.status.setText("Stopping... the best result found so far is kept.")

    def _reduce_done_ui(self) -> None:
        self._reduced_running = False; self._reducer = None; self.poll.stop()
        self.progress.setVisible(False); self.btn_cancel.setVisible(False); self.btn_cancel.setEnabled(True)
        self.btn_reduce.setEnabled(self.current() is not None)

    def _reduce_fail(self, msg: str) -> None:
        self._reduce_done_ui()
        self.status.setText(f"We could not replay this failure. {msg}")

    def _reduced(self, ev: ErrorEvent) -> None:
        self._reduce_done_ui()
        self.events[ev.id] = ev
        rel = theme.RELIABILITY.get(ev.reliability, theme.RELIABILITY["unconfirmed"])
        partial = " It was stopped early, so the steps may not be as short as possible." if self._cancel else ""
        self.status.setText(f"Done. Reliability: {rel[0]}. {rel[2]}{partial}")
        self._fill()

    def _copy(self) -> None:
        e = self.current()
        if e is None:
            return
        QGuiApplication.clipboard().setText(backend.export_recipe(e, self.model.project))
        self.status.setText("Copied. Paste it into your issue tracker.")

    def _save(self) -> None:
        e = self.current()
        if e is None:
            return
        name = "issue-" + "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in e.signature)[:40] + ".md"
        path, _ = QFileDialog.getSaveFileName(self, "Save the issue text", name, "Markdown (*.md);;Text (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(backend.export_recipe(e, self.model.project))
        except OSError as ex:
            self.status.setText(f"Could not save the file. {ex.strerror or ex}")
            return
        self.status.setText(f"Saved {os.path.basename(path)}.")

    def _open(self) -> None:
        e = self.current()
        if e is None or self.store is None:
            return
        p = self.store.incident_path(e.signature, e.id).parent
        target = p if p.exists() else self.store.dir
        if not os.path.isdir(str(target)):
            self.status.setText("The evidence folder is not there any more. It may have been moved or deleted.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
