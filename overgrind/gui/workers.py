"""Background execution (QThread) and the run session that wraps RunController."""
from __future__ import annotations

import os
import tempfile
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ..types import AdapterSpec, ErrorEvent, Project, RunStats, StopConditions
from . import backend


class Task(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any], parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except backend.BackendMissing as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001 - surfaced to the user as text
            self.failed.emit(f"{type(e).__name__}: {e}")


class TaskHost:
    """Mixin/holder: keeps running tasks alive and lets owners wait for them on close."""

    def __init__(self):
        self._tasks: set[Task] = set()

    def run_async(self, fn, on_done=None, on_fail=None, parent=None) -> Task:
        t = Task(fn, parent)
        self._tasks.add(t)

        def cleanup():
            self._tasks.discard(t)
            t.deleteLater()
        if on_done:
            t.done.connect(on_done)
        if on_fail:
            t.failed.connect(on_fail)
        t.finished.connect(cleanup)
        t.start()
        return t

    def wait_tasks(self, ms: int = 3000) -> None:
        for t in list(self._tasks):
            t.wait(ms)


HOST = TaskHost()


def run_async(fn, on_done=None, on_fail=None) -> Task:
    return HOST.run_async(fn, on_done, on_fail)


def default_evidence_dir(project: Project) -> str:
    base = os.environ.get("OVERGRIND_DATA") or os.path.join(tempfile.gettempdir(), "overgrind-runs")
    d = os.path.join(base, (project.uuid or "project")[:16] or "project", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(d, exist_ok=True)
    return d


class RunSession(QObject):
    """Owns one RunController at a time. UI thread only calls cheap methods; start/stop run in threads."""
    statsUpdated = Signal(object)        # RunStats
    errorFound = Signal(object)          # ErrorEvent
    stateChanged = Signal(str)           # idle|starting|running|paused|stopping|finished
    problem = Signal(str)                # friendly text
    _errorBridge = Signal(object)

    def __init__(self, poll_ms: int = 400, parent=None):
        super().__init__(parent)
        self.controller = None
        self.evidence_dir = ""
        self.state = "idle"
        self.last_stats = RunStats()
        self._timer = QTimer(self); self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self._poll)
        self._errorBridge.connect(self.errorFound)

    @property
    def active(self) -> bool:
        return self.state in ("starting", "running", "paused", "stopping")

    def _set(self, st: str) -> None:
        if st != self.state:
            self.state = st
            self.stateChanged.emit(st)

    def start(self, project: Project, spec: AdapterSpec, stop: StopConditions) -> None:
        if self.active:
            return
        self._set("starting")
        self.evidence_dir = default_evidence_dir(project)

        def go():
            c = backend.new_controller(project, spec, self.evidence_dir, stop)
            try:
                c.on_error(lambda ev: self._errorBridge.emit(ev))
            except Exception:
                pass
            c.start()
            return c

        def ok(c):
            self.controller = c
            self._set("running")
            self._timer.start()
            self._poll()

        def bad(msg):
            self.problem.emit(f"The run could not start. {msg}")
            self._set("idle")
        run_async(go, ok, bad)

    def pause(self) -> None:
        if self.controller and self.state == "running":
            c = self.controller
            run_async(c.pause, lambda _r: self._set("paused"), self.problem.emit)

    def resume(self) -> None:
        if self.controller and self.state == "paused":
            c = self.controller
            run_async(c.resume, lambda _r: self._set("running"), self.problem.emit)

    def stop(self, wait_ms: int = 0) -> None:
        c = self.controller
        if not c or self.state in ("idle", "stopping"):
            return
        self._set("stopping")

        def go():
            c.stop()
            c.join(10)
            return None

        def fin(_r):
            self._poll()
            self._timer.stop()
            self.controller = None
            self._set("finished")
        t = run_async(go, fin, lambda m: (self.problem.emit(m), self._set("finished")))
        if wait_ms:
            t.wait(wait_ms)

    def _poll(self) -> None:
        c = self.controller
        if c is None:
            return
        try:
            st = c.stats()
        except Exception as e:  # noqa: BLE001
            self.problem.emit(f"Could not read run status: {e}")
            return
        self.last_stats = st
        self.statsUpdated.emit(st)
        if st.state == "finished" and self.state in ("running", "paused"):
            self._timer.stop()
            self.controller = None
            self._set("finished")
        elif st.state == "paused" and self.state == "running":
            self._set("paused")
        elif st.state == "running" and self.state == "paused":
            self._set("running")

    def shutdown(self) -> None:
        """Called on window close: stop synchronously and clean."""
        c = self.controller
        self._timer.stop()
        if c is not None:
            try:
                c.stop()
                c.join(5)
            except Exception:
                pass
            self.controller = None
        self._set("idle")
