"""Overgrind GUI entry point and main window."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import backend, theme
from .dialogs import AboutDialog, CompareDialog, ShortcutsDialog
from .model import ProjectModel, empty_project
from .page_controls import ControlsPage
from .page_errors import ErrorsPage
from .page_features import FeaturesPage
from .page_inspect import InspectPage
from .page_plan import PlanPage
from .page_run import RunPage, friendly_reason
from .page_target import TargetPage
from .sample import DEMO_ADAPTER, demo_project
from .widgets import PAGE_NAMES, ElidedLabel, friendly_issue
from .workers import HOST, RunSession

FILTER = "Overgrind projects (*.ovg.json);;JSON files (*.json);;All files (*)"


def settings() -> QSettings:
    d = os.environ.get("OVERGRIND_SETTINGS_DIR")
    if d:
        return QSettings(os.path.join(d, "overgrind.ini"), QSettings.Format.IniFormat)
    return QSettings("Overgrind", "Overgrind")


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def suggest_filename(name: str) -> str:
    """A project name made safe as a Windows file name (no reserved characters or device names)."""
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" ._")
    base = re.sub(r"\s+", " ", base)[:80].strip(" ._")
    if not base:
        base = "project"
    if base.lower() in _RESERVED:
        base = "_" + base
    return base + ".ovg.json"


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def log_dir() -> str:
    d = os.environ.get("OVERGRIND_DATA") or os.path.join(os.path.expanduser("~"), ".overgrind")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.getcwd()
    return d


class MainWindow(QMainWindow):
    def __init__(self, model: ProjectModel | None = None):
        super().__init__()
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)
        self.model = model or ProjectModel()
        self.session = RunSession(parent=self)
        self.issues: list[tuple[str, str, int]] = []
        self._closing = False
        self._last_report = 0.0
        self._build_ui()
        self._build_menus()
        self.model.changed.connect(self._on_changed)
        self.model.dirtyChanged.connect(lambda _d: self._update_title())
        self.model.pathChanged.connect(lambda _p: self._update_title())
        self.session.stateChanged.connect(self._on_run_state)
        self.session.statsUpdated.connect(lambda st: self._update_status())
        self.run_page.revalidate = self._recheck          # F5 / Start never trusts a stale (debounced) check
        HOST.unhandled.connect(self.report_unexpected)
        self.destroyed.connect(lambda *_: _safe_disconnect(HOST.unhandled, self.report_unexpected))
        self._issue_timer = QTimer(self); self._issue_timer.setSingleShot(True); self._issue_timer.setInterval(150)
        self._issue_timer.timeout.connect(self._recheck)
        s = settings()
        geo = s.value("geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:
                pass
        self._recheck(); self._update_title(); self._update_status(); self.go_to(0)

    # ---- construction
    def _build_ui(self) -> None:
        central = QWidget(); root = QHBoxLayout(central); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        self.stepper = QListWidget(); self.stepper.setObjectName("Stepper"); self.stepper.setFixedWidth(190)
        self.stepper.setAccessibleName("Steps"); self.stepper.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for i, n in enumerate(PAGE_NAMES):
            it = QListWidgetItem(f"{i + 1}   {n}"); self.stepper.addItem(it)
        root.addWidget(self.stepper)
        right = QVBoxLayout(); right.setContentsMargins(0, 0, 0, 0); right.setSpacing(0)
        self.banner = QLabel(); self.banner.setWordWrap(True); self.banner.setTextFormat(Qt.TextFormat.RichText)
        self.banner.setContentsMargins(16, 8, 16, 8); self.banner.setVisible(False)
        self.banner.linkActivated.connect(lambda h: self.go_to(int(h)))
        self.banner.setAccessibleName("Problems with the project")
        right.addWidget(self.banner)
        self.stack = QStackedWidget()
        self.pages = [
            TargetPage(self.model), InspectPage(self.model), FeaturesPage(self.model), ControlsPage(self.model),
            PlanPage(self.model), RunPage(self.model, self.session), ErrorsPage(self.model, self.session),
        ]
        for p in self.pages:
            self.stack.addWidget(p)
            p.jump.connect(self.go_to)
        self.run_page: RunPage = self.pages[5]; self.errors_page: ErrorsPage = self.pages[6]
        self.run_page.block_label.linkActivated.connect(lambda h: self.go_to(int(h)))
        right.addWidget(self.stack, 1)
        nav = QHBoxLayout(); nav.setContentsMargins(16, 8, 16, 8)
        self.btn_back = QPushButton("< Back"); self.btn_next = QPushButton("Next >"); self.btn_next.setObjectName("Primary")
        nav.addWidget(self.btn_back); nav.addStretch(1); nav.addWidget(self.btn_next)
        right.addLayout(nav)
        root.addLayout(right, 1)
        self.setCentralWidget(central)
        self.lbl_target = ElidedLabel(); self.lbl_run = ElidedLabel(); self.lbl_run.setMaximumWidth(380)
        self.statusBar().addWidget(self.lbl_target, 1); self.statusBar().addPermanentWidget(self.lbl_run)
        self.stepper.currentRowChanged.connect(self.go_to)
        self.btn_back.clicked.connect(lambda: self.go_to(self.stack.currentIndex() - 1))
        self.btn_next.clicked.connect(lambda: self.go_to(self.stack.currentIndex() + 1))

    def _act(self, menu, text, slot, shortcut=None) -> QAction:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut)); a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        self._act(m, "&New project", self.new_project, "Ctrl+N")
        self._act(m, "&Open...", self.open_dialog, "Ctrl+O")
        self._act(m, "Open &demo project", self.open_demo)
        self.recent_menu = m.addMenu("Open &recent")
        m.addSeparator()
        self._act(m, "&Save", self.save, "Ctrl+S")
        self._act(m, "Save &As...", self.save_as, "Ctrl+Shift+S")
        m.addSeparator()
        self._act(m, "&Import v0.1 manifest...", self.import_v1)
        m.addSeparator()
        self._act(m, "&Quit", self.close, "Ctrl+Q")
        e = mb.addMenu("&Edit")
        self.act_undo = self._act(e, "&Undo", self.model.undo, "Ctrl+Z")
        self.act_redo = self._act(e, "&Redo", self.model.redo, "Ctrl+Y")
        v = mb.addMenu("&Go")
        for i, n in enumerate(PAGE_NAMES):
            self._act(v, f"{i + 1}  {n}", lambda _c=False, i=i: self.go_to(i), f"Ctrl+{i + 1}")
        self._act(v, "Back", lambda: self.go_to(self.stack.currentIndex() - 1), "Alt+Left")
        self._act(v, "Next", lambda: self.go_to(self.stack.currentIndex() + 1), "Alt+Right")
        self._act(v, "Search elements", self._focus_search, "Ctrl+F")
        r = mb.addMenu("&Run")
        self.act_start = self._act(r, "&Start", lambda: (self.go_to(5), self.run_page.start()), "F5")
        self.act_pause = self._act(r, "&Pause / Resume", self.run_page.toggle_pause, "F6")
        self.act_stop = self._act(r, "S&top", self.session.stop, "Shift+F5")
        self.act_pause.setEnabled(False); self.act_stop.setEnabled(False)
        t = mb.addMenu("&Tools")
        self._act(t, "&Scan app", lambda: (self.go_to(1), self.pages[1].scan()), "F4")
        self._act(t, "Run &preflight check", lambda: (self.go_to(4), self.pages[4].run_preflight()), "Ctrl+R")
        self._act(t, "&Compare builds...", self.compare_builds)
        h = mb.addMenu("&Help")
        self._act(h, "&Keyboard shortcuts", lambda: ShortcutsDialog(self).exec(), "F1")
        self._act(h, "&About Overgrind", lambda: AboutDialog(self).exec())
        self._rebuild_recent()

    def _focus_search(self) -> None:
        if self.stack.currentIndex() == 1:
            self.pages[1].focus_search()
        elif self.stack.currentIndex() == 6:
            self.pages[6].search.setFocus()

    # ---- navigation
    def go_to(self, i: int) -> None:
        i = max(0, min(len(self.pages) - 1, i))
        self.stack.setCurrentIndex(i)
        if self.stepper.currentRow() != i:
            self.stepper.blockSignals(True); self.stepper.setCurrentRow(i); self.stepper.blockSignals(False)
        self.btn_back.setEnabled(i > 0); self.btn_next.setEnabled(i < len(self.pages) - 1)
        self.btn_next.setText(f"Next: {PAGE_NAMES[i + 1]} >" if i < len(self.pages) - 1 else "Next >")
        self.btn_back.setText(f"< Back: {PAGE_NAMES[i - 1]}" if i > 0 else "< Back")
        self.pages[i].refresh()
        self._show_banner()

    def _on_changed(self) -> None:
        self._issue_timer.start()
        self.pages[self.stack.currentIndex()].refresh()
        self.run_page.refresh() if self.stack.currentIndex() != 5 else None
        self._update_status()
        self.act_undo.setEnabled(self.model.can_undo()); self.act_redo.setEnabled(self.model.can_redo())

    # ---- validation banner
    def _recheck(self) -> None:
        try:
            msgs = backend.validate(self.model.project)
        except Exception as e:  # noqa: BLE001
            msgs = [f"Project check failed: {e}"]
        self.issues = [friendly_issue(m) for m in msgs]
        self.run_page.set_issues(self.issues)
        self._decorate_stepper()
        self._show_banner()

    def _show_banner(self) -> None:
        cur = self.stack.currentIndex()
        if cur in (5, 6) or not self.issues:
            self.banner.setVisible(False); return
        mine = [x for x in self.issues if x[2] == cur]
        if not mine:
            blockers = [x for x in self.issues if x[0] == "fail"]
            if not blockers:
                self.banner.setVisible(False); return
            first = blockers[0]
            self.banner.setText(f"<b>Problem on another page:</b> {first[1]} <a href='{first[2]}'>Fix: go to {PAGE_NAMES[first[2]]}</a>"
                                + (f" (and {len(blockers) - 1} more)" if len(blockers) > 1 else ""))
            c = theme.sem("fail"); c.setAlpha(36)
            self.banner.setStyleSheet(f"background:rgba({c.red()},{c.green()},{c.blue()},{c.alpha()});")
            self.banner.setVisible(True); return
        lines = []
        for level, text, page in mine[:3]:
            tag = "Problem" if level == "fail" else "Heads-up"
            col = theme.sem_hex("fail" if level == "fail" else "warn")
            fix = ""
            lines.append(f"<span style='color:{col}'><b>{tag}:</b></span> {text}{fix}")
        if len(mine) > 3:
            lines.append(f"...and {len(mine) - 3} more.")
        self.banner.setText("<br>".join(lines))
        worst = "fail" if any(x[0] == "fail" for x in mine) else "warn"
        c = theme.sem(worst); c.setAlpha(36)
        self.banner.setStyleSheet(f"background:rgba({c.red()},{c.green()},{c.blue()},{c.alpha()});")
        self.banner.setVisible(True)

    def _decorate_stepper(self) -> None:
        for i in range(self.stepper.count()):
            bad = [x for x in self.issues if x[2] == i]
            it = self.stepper.item(i)
            if bad:
                kind = "fail" if any(x[0] == "fail" for x in bad) else "warn"
                it.setIcon(theme.glyph(kind, theme.sem(kind))); it.setToolTip(f"{len(bad)} thing(s) to fix on this page")
            else:
                it.setIcon(theme.glyph("dot", theme.sem("muted"), 8)); it.setToolTip("")

    # ---- status
    def _update_status(self) -> None:
        t = self.model.project.target
        name = t.process_name or t.exe_path or "none chosen"
        if self.model.demo:
            name += " (demo)"
        self.lbl_target.setText(f"Target: {name}")
        st = self.session.state
        text = {"idle": "Run: ready", "starting": "Run: starting", "running": "Run: running", "paused": "Run: paused",
                "stopping": "Run: stopping", "finished": "Run: finished"}.get(st, st)
        if st in ("running", "paused"):
            s = self.session.last_stats
            text += f"  |  {s.cases:,} cases, {s.errors_unique} different error(s)"
        self.lbl_run.setText(text)

    def _sync_run_actions(self) -> None:
        st = self.session.state
        busy = self.session.active
        self.act_start.setEnabled(not busy)
        self.act_pause.setEnabled(st in ("running", "paused"))
        self.act_stop.setEnabled(st in ("running", "paused"))

    def _on_run_state(self, st: str) -> None:
        self._sync_run_actions()
        if self._closing and not self.session.active:
            QTimer.singleShot(0, self.close)
        self._update_status()
        if st == "finished":
            reason = self.session.last_stats.stop_reason
            if reason:
                self.statusBar().showMessage("Run finished: " + friendly_reason(reason), 8000)
        self.run_page.refresh_blockers()

    def _update_title(self) -> None:
        p = self.model.project
        self.setWindowTitle(f"{'*' if self.model.dirty else ''}{p.name} - Overgrind")

    # ---- file operations
    def confirm_discard(self) -> bool:
        if self.session.active:
            QMessageBox.information(self, "A test is running", "Stop the test first (Shift+F5), then try again.")
            return False
        if not self.model.dirty:
            return True
        r = QMessageBox.question(self, "Unsaved changes", f"Save changes to '{self.model.project.name}' first?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save()
        return r == QMessageBox.StandardButton.Discard

    def new_project(self) -> None:
        if self.confirm_discard():
            self.model.demo = False; self.model.adapter_spec = None
            self.model.load_project(empty_project())
            self.go_to(0)

    def open_demo(self) -> None:
        if self.confirm_discard():
            self.model.demo = True; self.model.adapter_spec = DEMO_ADAPTER
            self.model.load_project(demo_project())
            self.go_to(0)

    def open_dialog(self) -> None:
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "", FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str) -> bool:
        try:
            p = backend.load_project(path)
        except Exception as e:  # noqa: BLE001
            gone = not os.path.exists(path)
            if gone:
                self._forget_recent(path)
            QMessageBox.warning(self, "Could not open the project",
                                f"{os.path.basename(path)} " + ("no longer exists, so it was removed from the recent list." if gone else f"could not be opened.\n\n{e}"))
            return False
        self.model.demo = p.uuid == "demo"; self.model.adapter_spec = DEMO_ADAPTER if self.model.demo else None
        self.model.load_project(p, path)
        self._add_recent(path)
        self.go_to(0)
        return True

    def save(self) -> bool:
        if not self.model.path:
            return self.save_as()
        return self._save_to(self.model.path)

    def save_as(self) -> bool:
        suggested = self.model.path or suggest_filename(self.model.project.name)
        path, _ = QFileDialog.getSaveFileName(self, "Save project", suggested, FILTER)
        if not path:
            return False
        if not path.lower().endswith(".json"):
            path += ".ovg.json"
        return self._save_to(path)

    def _save_to(self, path: str) -> bool:
        try:
            backend.save_project(self.model.project, path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Could not save", f"Your project was not saved.\n\n{e}")
            return False
        self.model.mark_saved(path)
        self._add_recent(path)
        self.statusBar().showMessage(f"Saved {os.path.basename(path)}", 4000)
        return True

    def import_v1(self) -> None:
        if not self.confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import a v0.1 manifest", "", "JSON files (*.json);;All files (*)")
        if not path:
            return
        try:
            p = backend.migrate_v1(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Could not import", f"This file could not be converted.\n\n{e}"); return
        self.model.demo = False; self.model.adapter_spec = None
        self.model.load_project(p, "", dirty=True)
        self.go_to(0)

    def _recent(self) -> list[str]:
        st = settings()
        try:
            v = json.loads(str(st.value("recent_json", "[]")))
            if isinstance(v, list):
                return [str(x) for x in v if x]
        except (ValueError, TypeError):
            pass
        return []

    def _store_recent(self, items: list[str]) -> None:
        settings().setValue("recent_json", json.dumps(items[:8]))
        self._rebuild_recent()

    def _add_recent(self, path: str) -> None:
        key = _path_key(path)
        self._store_recent([path] + [p for p in self._recent() if _path_key(p) != key])

    def _forget_recent(self, path: str) -> None:
        key = _path_key(path)
        self._store_recent([p for p in self._recent() if _path_key(p) != key])

    def _rebuild_recent(self) -> None:
        self.recent_menu.clear()
        items = self._recent()
        self.recent_menu.setEnabled(bool(items))
        for p in items:
            a = self.recent_menu.addAction(os.path.basename(p).replace("&", "&&") or p); a.setToolTip(p); a.setStatusTip(p)
            a.triggered.connect(lambda _c=False, p=p: self.confirm_discard() and self.open_path(p))

    def compare_builds(self) -> None:
        CompareDialog(self.model.project, self).exec()

    # ---- unexpected problems
    def report_unexpected(self, text: str) -> None:
        """A failure nobody was waiting for (a background task or a slot raised). Logged, shown calmly, never fatal."""
        try:
            with open(os.path.join(log_dir(), "overgrind-gui.log"), "a", encoding="utf-8") as fh:
                fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
        except OSError:
            pass
        first = (text.strip().splitlines() or [""])[-1][:160]
        self.statusBar().showMessage(f"Something went wrong inside Overgrind: {first} (details are in overgrind-gui.log)", 15000)
        now = time.monotonic()
        if not self.session.active and now - self._last_report > 30 and self.isVisible():
            self._last_report = now
            QMessageBox.warning(self, "Something went wrong",
                                "Overgrind hit an unexpected problem. Your project is not affected, but please save your work.\n\n"
                                f"{first}\n\nMore details were written to overgrind-gui.log in {log_dir()}.")

    # ---- close
    def closeEvent(self, ev) -> None:
        if self.session.active:
            if not self._closing:
                r = QMessageBox.question(self, "A test is running", "A test is still running. Stop it and close?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if r != QMessageBox.StandardButton.Yes:
                    ev.ignore(); return
            self._closing = True
            self.statusBar().showMessage("Stopping the test before closing...")
            self.session.shutdown(wait=False)       # never block the window on the controller
            if self.session.active:
                ev.ignore(); return                  # _on_run_state closes the window when the stop is done
        self._closing = False
        if self.model.dirty:
            r = QMessageBox.question(self, "Unsaved changes", f"Save changes to '{self.model.project.name}' before closing?",
                                     QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel or (r == QMessageBox.StandardButton.Save and not self.save()):
                ev.ignore(); return
        HOST.wait_tasks(3000)
        settings().setValue("geometry", self.saveGeometry())
        ev.accept()


def _safe_disconnect(signal, slot) -> None:
    try:
        signal.disconnect(slot)
    except (RuntimeError, TypeError):
        pass


def make_app(argv: list[str] | None = None) -> QApplication:
    if QGuiApplication.instance() is None:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Overgrind"); app.setOrganizationName("Overgrind")
    app.setStyle("Fusion")
    app.setStyleSheet(theme.qss())
    return app


def main(argv: list[str] | None = None) -> int:
    import multiprocessing
    multiprocessing.freeze_support()
    ap = argparse.ArgumentParser(prog="python -m overgrind.gui", description="Overgrind desktop GUI")
    ap.add_argument("--demo", action="store_true", help="load a sample project wired to the built-in mock app")
    ap.add_argument("project", nargs="?", help="a .ovg.json project to open")
    args, rest = ap.parse_known_args(argv if argv is not None else sys.argv[1:])
    app = make_app([sys.argv[0]] + rest)
    win = MainWindow()
    if args.demo:
        win.model.demo = True; win.model.adapter_spec = DEMO_ADAPTER
        win.model.load_project(demo_project())
    elif args.project:
        win.open_path(args.project)
    prev_hook = sys.excepthook

    def hook(et, ev, tb):            # slots that raise must not vanish silently (PySide only prints them)
        prev_hook(et, ev, tb)
        win.report_unexpected("".join(traceback.format_exception(et, ev, tb)))
    sys.excepthook = hook
    win.show()
    code = app.exec()
    if HOST.busy:                    # a background task outlived the window: skip Qt teardown, which would abort
        sys.stdout.flush(); sys.stderr.flush()
        os._exit(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
