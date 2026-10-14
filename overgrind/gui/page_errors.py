"""Step 7: errors list, detail, reproduce-and-shrink, export."""
from __future__ import annotations

import html
import os

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSplitter,
    QTableView, QTextBrowser, QVBoxLayout, QWidget,
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


class Proxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.category = ""; self.text = ""
        self.setSortRole(Qt.ItemDataRole.UserRole + 1)

    def filterAcceptsRow(self, row, parent) -> bool:
        m = self.sourceModel()
        cat = m.item(row, 1).data(Qt.ItemDataRole.UserRole)
        if self.category and cat != self.category:
            return False
        if self.text:
            blob = " ".join(m.item(row, c).text() for c in (2, 3)).lower()
            return self.text in blob
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

        bar = QHBoxLayout()
        self.cat = QComboBox(); self.cat.addItem("All kinds", ""); [self.cat.addItem(v, k) for k, v in theme.CATEGORY_LABEL.items()]
        self.cat.setAccessibleName("Filter by kind")
        self.search = QLineEdit(); self.search.setPlaceholderText("Filter by message or signature"); self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Filter errors")
        self.btn_reload = QPushButton("Reload")
        bar.addWidget(self.cat); bar.addWidget(self.search, 1); bar.addWidget(self.btn_reload)
        self.body.addLayout(bar)
        self.empty = empty_state("No failures so far.\nFailures appear here the moment they happen.")
        self.body.addWidget(self.empty, 1)

        split = QSplitter(Qt.Orientation.Vertical); split.setChildrenCollapsible(False)
        self.sm = QStandardItemModel(0, 6)
        self.sm.setHorizontalHeaderLabels(["Time", "Kind", "What happened", "Signature", "Seen", "Reliability"])
        self.proxy = Proxy(); self.proxy.setSourceModel(self.sm)
        self.view = QTableView(); self.view.setModel(self.proxy); self.view.setSortingEnabled(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setAlternatingRowColors(True); self.view.verticalHeader().setVisible(False)
        self.view.setAccessibleName("Errors"); self.view.setMinimumHeight(140)
        h = self.view.horizontalHeader(); h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents); h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        split.addWidget(self.view)

        det = QWidget(); dl = QVBoxLayout(det); dl.setContentsMargins(0, 6, 0, 0)
        top = QHBoxLayout()
        self.d_title = QLabel(); self.d_title.setWordWrap(True); f = self.d_title.font(); f.setBold(True); f.setPointSizeF(f.pointSizeF() * 1.15); self.d_title.setFont(f)
        self.badge = theme.Badge()
        top.addWidget(self.d_title, 1); top.addWidget(self.badge)
        dl.addLayout(top)
        self.shot = QLabel(); self.shot.setMaximumHeight(160); self.shot.setAlignment(Qt.AlignmentFlag.AlignLeft); self.shot.setVisible(False)
        self.shot.setAccessibleName("Screenshot")
        dl.addWidget(self.shot)
        self.detail = QTextBrowser(); self.detail.setOpenLinks(False); self.detail.setMinimumHeight(150)
        self.detail.setAccessibleName("Error details")
        dl.addWidget(self.detail, 1)
        btns = QHBoxLayout()
        self.btn_reduce = QPushButton("Reproduce and shrink"); self.btn_copy = QPushButton("Copy as issue"); self.btn_open = QPushButton("Open folder")
        btns.addWidget(self.btn_reduce); btns.addWidget(self.btn_copy); btns.addWidget(self.btn_open); btns.addStretch(1)
        dl.addLayout(btns)
        self.status = muted(""); dl.addWidget(self.status)
        split.addWidget(det)
        split.setStretchFactor(0, 2); split.setStretchFactor(1, 3)
        self.split = split
        self.body.addWidget(split, 1)

        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(500); self.timer.timeout.connect(self.reload)
        self.cat.currentIndexChanged.connect(self._filter); self.search.textChanged.connect(self._filter)
        self.btn_reload.clicked.connect(self.reload)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self._show())
        self.btn_reduce.clicked.connect(self._reduce); self.btn_copy.clicked.connect(self._copy); self.btn_open.clicked.connect(self._open)
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
        self.events = {}; self._fill()
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
        self.view.setSortingEnabled(False)
        self.sm.removeRows(0, self.sm.rowCount())
        for e in sorted(self.events.values(), key=lambda e: e.ts, reverse=True):
            cat = e.category if e.category in theme.CATEGORY_LABEL else "harness"
            it_time = QStandardItem(e.ts.replace("T", " ")[:19]); it_time.setData(e.id, Qt.ItemDataRole.UserRole); it_time.setData(e.ts, Qt.ItemDataRole.UserRole + 1)
            it_cat = QStandardItem(theme.CATEGORY_LABEL.get(cat, cat)); it_cat.setData(cat, Qt.ItemDataRole.UserRole); it_cat.setData(cat, Qt.ItemDataRole.UserRole + 1)
            it_cat.setIcon(theme.glyph(cat, theme.sem_hex_color(cat)))
            it_msg = QStandardItem(e.message or "(no message)"); it_msg.setData(e.message, Qt.ItemDataRole.UserRole + 1)
            it_sig = QStandardItem(e.signature); it_sig.setData(e.signature, Qt.ItemDataRole.UserRole + 1)
            it_n = QStandardItem(f"{e.count:,}"); it_n.setData(e.count, Qt.ItemDataRole.UserRole + 1)
            rel = e.reliability if e.reliability in theme.RELIABILITY else "unconfirmed"
            it_rel = QStandardItem(theme.RELIABILITY[rel][0]); it_rel.setData(rel, Qt.ItemDataRole.UserRole + 1)
            ic = {"reliable": "ok", "flaky": "warn", "not_reproducible": "fail", "unconfirmed": "unknown"}[rel]
            it_rel.setIcon(theme.glyph(ic, theme.sem({"ok": "ok", "warn": "warn", "fail": "fail", "unknown": "muted"}[ic])))
            it_rel.setToolTip(theme.RELIABILITY[rel][2])
            for it in (it_time, it_cat, it_msg, it_sig, it_n, it_rel):
                it.setEditable(False)
            self.sm.appendRow([it_time, it_cat, it_msg, it_sig, it_n, it_rel])
        self.view.setSortingEnabled(True)
        has = bool(self.events)
        self.empty.setVisible(not has); self.split.setVisible(has)
        if has:
            if keep:
                self._select(keep)
            elif self.proxy.rowCount():
                self.view.selectRow(0)
        self._show()

    def _filter(self) -> None:
        self.proxy.category = self.cat.currentData() or ""
        self.proxy.text = self.search.text().strip().lower()
        self.proxy.invalidateFilter()

    def _current_id(self) -> str:
        rows = self.view.selectionModel().selectedRows() if self.view.selectionModel() else []
        return self.proxy.index(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole) if rows else ""

    def _select(self, eid: str) -> None:
        for r in range(self.proxy.rowCount()):
            if self.proxy.index(r, 0).data(Qt.ItemDataRole.UserRole) == eid:
                self.view.selectRow(r); return

    def current(self) -> ErrorEvent | None:
        return self.events.get(self._current_id())

    # ---- detail
    def _show(self) -> None:
        e = self.current()
        for b in (self.btn_reduce, self.btn_copy, self.btn_open):
            b.setEnabled(e is not None)
        if e is None:
            self.d_title.setText(""); self.detail.clear(); self.shot.setVisible(False); self.badge.setVisible(False)
            return
        self.badge.setVisible(True)
        cat = theme.CATEGORY_LABEL.get(e.category, e.category)
        self.d_title.setText(f"{cat}: {e.message or e.signature}")
        self.badge.set_reliability(e.reliability if e.reliability in theme.RELIABILITY else "unconfirmed")
        self.detail.setHtml(self._html(e))
        self._screenshot(e)
        self.btn_reduce.setEnabled(not self._reduced_running)

    def _html(self, e: ErrorEvent) -> str:
        names = {f.id: (f.name or f.id) for f in self.model.project.features}
        out = [f"<p>Seen <b>{e.count}</b> time(s). First recorded at {html.escape(e.ts)}. Error signature: <code>{html.escape(e.signature)}</code></p>"]
        red = e.reduced
        if red and red.get("steps") is not None:
            steps = [step_html(s.get("kind", ""), s.get("target", ""), s.get("value")) for s in red["steps"]]
            pct = red.get("reliability_pct")
            out.append(f"<h4>Shortest way to make it happen ({len(steps)} of {red.get('original_steps', len(steps))} recorded steps)</h4>")
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
        for k, v in e.attachments.items():
            if v.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                px = QPixmap(self._attachment_path(e, v))
                if not px.isNull():
                    self.shot.setPixmap(px.scaledToHeight(150, Qt.TransformationMode.SmoothTransformation)); self.shot.setVisible(True)
                    return
        self.shot.setVisible(False)

    # ---- actions
    def _reduce(self) -> None:
        e = self.current()
        if e is None or self._reduced_running:
            return
        try:
            spec = backend.resolve_spec(self.model.adapter_spec)
        except backend.BackendMissing as ex:
            self.status.setText(str(ex)); return
        self._reduced_running = True; self.btn_reduce.setEnabled(False)
        self.status.setText("Replaying the failure and removing unneeded steps. This can take a minute; the original evidence is never changed.")
        proj, store = self.model.project, self.store

        def work():
            R = backend._fn("reduce", "Reducer")
            try:
                red = R(proj, spec, store=store)
            except TypeError:
                red = R(proj, spec)
            try:
                return red.reduce(e)
            finally:
                try:
                    red.close()
                except Exception:
                    pass
        run_async(work, self._reduced, self._reduce_fail)

    def _reduce_fail(self, msg: str) -> None:
        self._reduced_running = False; self.btn_reduce.setEnabled(True)
        self.status.setText(f"We could not replay this failure. {msg}")

    def _reduced(self, ev: ErrorEvent) -> None:
        self._reduced_running = False
        self.events[ev.id] = ev
        rel = theme.RELIABILITY.get(ev.reliability, theme.RELIABILITY["unconfirmed"])
        self.status.setText(f"Done. Reliability: {rel[0]}. {rel[2]}")
        self._fill()

    def _copy(self) -> None:
        e = self.current()
        if e is None:
            return
        QGuiApplication.clipboard().setText(backend.export_recipe(e, self.model.project))
        self.status.setText("Copied. Paste it into your issue tracker.")

    def _open(self) -> None:
        e = self.current()
        if e is None or self.store is None:
            return
        p = self.store.incident_path(e.signature, e.id).parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p if p.exists() else self.store.dir)))
