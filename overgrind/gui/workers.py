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


class TaskHost(QObject):
    """Holder: keeps running tasks alive, lets owners wait for them on close, and surfaces failures
    nobody asked to handle (`unhandled`) instead of swallowing them."""
    unhandled = Signal(str)

    def __init__(self):
        super().__init__()
        self._tasks: set[Task] = set()

    @property
    def busy(self) -> bool:
        return any(t.isRunning() for t in self._tasks)

    def run_async(self, fn, on_done=None, on_fail=None, parent=None) -> Task:
        t = Task(fn, parent)
        self._tasks.add(t)

        def cleanup():
            self._tasks.discard(t)
            t.deleteLater()
        if on_done:
            t.done.connect(on_done)
        t.failed.connect(on_fail if on_fail else self.unhandled)
        t.finished.connect(cleanup)
        t.start()
        return t

    def wait_tasks(self, ms: int = 3000) -> bool:
        """Wait up to `ms` in total. True when nothing is left running."""
        end = time.monotonic() + ms / 1000.0
        for t in list(self._tasks):
            t.wait(max(1, int((end - time.monotonic()) * 1000)))
        return not self.busy


HOST = TaskHost()


def run_async(fn, on_done=None, on_fail=None) -> Task:
    return HOST.run_async(fn, on_done, on_fail)


def default_evidence_dir(project: Project) -> str:
    base = os.environ.get("OVERGRIND_DATA") or os.path.join(tempfile.gettempdir(), "overgrind-runs")
    uid = "".join(ch for ch in (project.uuid or "project")[:16] if ch.isalnum() or ch in "-_") or "project"
    d = os.path.join(base, uid, time.strftime("%Y%m%d-%H%M%S"))
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
        self._gen = 0                       # bumped by shutdown(): a controller built for an older start is discarded
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
        try:
            self.evidence_dir = default_evidence_dir(project)
        except OSError as e:
            self.problem.emit(f"The run could not start. Overgrind needs a writable folder to keep error evidence, "
                              f"but could not create one ({e.strerror or e}). Set the OVERGRIND_DATA environment variable to a folder you can write to.")
            return
        self._set("starting")
        gen = self._gen

        def go():
            c = backend.new_controller(project, spec, self.evidence_dir, stop)
            try:
                c.on_error(lambda ev: self._errorBridge.emit(ev))
            except Exception:
                pass
            if gen != self._gen:        # window closed while we were building it: never start
                return None
            c.start()
            if gen != self._gen:        # closed in the tiny gap: stop it here, this thread owns it
                c.stop()
                c.join(5)
                return None
            return c

        def ok(c):
            if c is None or gen != self._gen:
                return
            self.controller = c
            self._set("running")
            self._timer.start()
            self._poll()

        def bad(msg):
            if gen != self._gen:
                return
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

    def shutdown(self, wait: bool = True) -> None:
        """Called on window close. wait=False stops the controller on a thread (state 'stopping' until it is
        done, then 'idle') so the window never freezes; wait=True blocks (tests, last-resort cleanup)."""
        c = self.controller
        self._gen += 1
        self._timer.stop()
        self.controller = None
        if c is None:
            self._set("idle")
            return

        def halt():
            try:
                c.stop()
                c.join(5)
            except Exception:
                pass
            return None
        if wait:
            halt()
            self._set("idle")
            return
        self._set("stopping")
        run_async(halt, lambda _r: self._set("idle"), lambda _m: self._set("idle"))
