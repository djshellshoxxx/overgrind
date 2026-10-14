"""RunController: parent-side supervisor of a fuzzing run.

The controller process NEVER touches the GUI target. A spawned worker process (worker.py) builds
the adapter, executes cases and reports compact messages; this module aggregates them into
bounded-memory `RunStats`, persists failures (and only failures) through `evidence.EvidenceStore`,
enforces stop conditions on a monotonic clock, checkpoints the generator into
`evidence_dir/checkpoint.json` and runs the watchdog (watchdog.py): a worker that overruns an
operation/case deadline or dies is killed (process tree), classified and replaced; the run goes on.

Frozen (PyInstaller) builds must call `multiprocessing.freeze_support()` first thing in the
entry point so that spawned workers re-enter `overgrind.worker`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import evidence
from .adapters import procwatch
from .types import (
    Action, AdapterSpec, Case, Coverage, ErrorEvent, Outcome, Project, RunStats, StopConditions,
)
from .watchdog import WorkerProcess, check_deadlines, classify_wedge
from .worker import WorkerConfig, load_callable

MAX_NO_PROGRESS_RESTARTS = 5
CHECKPOINT_NAME = "checkpoint.json"


def _project_hash(project: Project) -> str:
    try:
        from . import project as proj_mod
        return proj_mod.project_hash(project)
    except Exception:
        return hashlib.sha256(repr(project).encode()).hexdigest()[:16]


class RunController:
    def __init__(self, project: Project, adapter_spec: AdapterSpec, evidence_dir: str | os.PathLike,
                 *, stop: StopConditions | None = None, resume: bool = False,
                 checkpoint_interval_s: float | None = 30.0, generator_factory: str | None = None,
                 liveness: Callable[[int | None], bool] | None = None, poll_s: float = 0.05) -> None:
        self.project, self.adapter_spec = project, adapter_spec
        self.evidence_dir = Path(evidence_dir)
        self.stop_cond = stop or StopConditions()
        self.checkpoint_interval_s = checkpoint_interval_s
        self.generator_factory = generator_factory
        self.poll_s = poll_s
        self._alive = liveness or procwatch.is_alive
        self.run_id = uuid.uuid4().hex[:12]
        self.store = evidence.EvidenceStore(self.evidence_dir)
        self._lock = threading.RLock()
        self._cbs: list[Callable[[ErrorEvent], None]] = []
        self._thread: threading.Thread | None = None
        self._stop_req = threading.Event()
        self._state = "idle"
        self._stop_reason = ""
        self._w: WorkerProcess | None = None
        self._pause_flag = False
        # counters (all O(1) memory)
        self._cases = self._harness = self._restarts = 0
        self._crashes = self._hangs = self._errors_total = 0
        self._run_unique: set[str] = set()
        self._coverage = Coverage()
        self._current = ""
        self._recent: tuple[Action, ...] = ()
        self._rate = 0.0
        self._last_batch_t = 0.0
        self._active_s = self._paused_s = 0.0
        self._last_tick = 0.0
        self._target_pid: int | None = None
        self._ckpt: dict | None = None
        self._ckpt_pos = 0
        self._ckpt_dirty = False
        self._no_progress = 0
        self._inc_counted = 0
        self._shot_dir = os.path.join(tempfile.gettempdir(), f"overgrind-shots-{self.run_id}")
        self._phash = _project_hash(project)
        if resume:
            self._load_checkpoint()
        try:
            self._env = evidence.capture_env(project.target.exe_path, {"run_id": self.run_id})
        except Exception:
            self._env = {}

    # ------------------------------------------------------------------ public API
    @property
    def state(self) -> str:
        return self._state

    def on_error(self, cb: Callable[[ErrorEvent], None]) -> None:
        self._cbs.append(cb)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("RunController already started")
            self._state = "running"
            self._last_tick = time.monotonic()
            self._thread = threading.Thread(target=self._supervise, name="overgrind-controller",
                                            daemon=True)
            self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self._state != "running":
                return
            self._state = "paused"
            self._pause_flag = True
            self._rate = 0.0
            if self._w:
                self._w.pause_ev.set()

    def resume(self) -> None:
        with self._lock:
            if self._state != "paused":
                return
            self._state = "running"
            self._pause_flag = False
            self._last_batch_t = time.monotonic()
            if self._w:
                self._w.pause_ev.clear()

    def stop(self, timeout: float = 2.0) -> None:
        """Request a stop and wait (bounded) for the run to wind down; never blocks on a wedged worker."""
        with self._lock:
            if self._state in ("idle", "finished"):
                if self._state == "idle":
                    self._state = "finished"
                    self._stop_reason = self._stop_reason or "stopped"
                return
            self._state = "stopping"
            self._stop_reason = self._stop_reason or "stopped by user"
        self._stop_req.set()
        self.join(timeout)

    def join(self, timeout: float | None = None) -> bool:
        t = self._thread
        if t is None:
            return self._state == "finished"
        t.join(timeout)
        return not t.is_alive()

    def stats(self) -> RunStats:
        with self._lock:
            return RunStats(
                state=self._state, elapsed_s=self._active_s, cases=self._cases,
                cases_per_s=self._rate if self._state == "running" else 0.0,
                errors_total=self._errors_total, errors_unique=len(self._run_unique),
                crashes=self._crashes, hangs=self._hangs, harness_errors=self._harness,
                restarts=self._restarts, coverage=self._coverage, current=self._current,
                recent=self._recent, stop_reason=self._stop_reason)

    # ------------------------------------------------------------------ checkpoint
    @property
    def checkpoint_path(self) -> Path:
        return self.evidence_dir / CHECKPOINT_NAME

    def _load_checkpoint(self) -> None:
        try:
            d = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(d, dict) and d.get("project_hash") == self._phash and isinstance(d.get("generator"), dict):
            self._ckpt = d["generator"]

    def _save_checkpoint(self) -> None:
        if self._ckpt is None or self.checkpoint_interval_s is None:
            return
        data = {"version": 1, "run_id": self.run_id, "project_hash": self._phash,
                "saved": datetime.now(timezone.utc).isoformat(), "generator": self._ckpt,
                "cases": self._cases, "errors_unique": len(self._run_unique)}
        try:
            evidence.atomic_write_text(self.checkpoint_path, json.dumps(data, separators=(",", ":")))
        except OSError:
            pass

    # ------------------------------------------------------------------ supervisor thread
    def _supervise(self) -> None:
        try:
            self._main()
        except BaseException as exc:    # noqa: BLE001 - never leave a run hanging in 'running'
            with self._lock:
                self._stop_reason = self._stop_reason or f"controller error: {type(exc).__name__}: {exc}"
        finally:
            try:
                self._teardown()
            finally:
                with self._lock:
                    self._state = "finished"
                    self._stop_reason = self._stop_reason or "stopped"

    def _teardown(self) -> None:
        self._stop_worker()
        self._save_checkpoint()
        shutil.rmtree(self._shot_dir, ignore_errors=True)

    def _tick_clock(self) -> None:
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now
        with self._lock:
            if self._state == "paused":
                self._paused_s += dt
            elif self._state == "running":
                self._active_s += dt

    def _counted_seconds(self) -> float:
        return self._active_s + (self._paused_s if self.project.run.pause_counts_toward_time else 0.0)

    def _set_stop(self, reason: str) -> None:
        with self._lock:
            if not self._stop_reason:
                self._stop_reason = reason
            self._state = "stopping"

    def _main(self) -> None:
        self._spawn(skip=0)
        next_ckpt = time.monotonic() + (self.checkpoint_interval_s or 0)
        while not self._stop_req.is_set():
            w = self._w
            self._tick_clock()
            for msg in w.poll(self.poll_s):
                if self._handle(msg):
                    return
            if self._conditions_met():
                return
            now = time.monotonic()
            if not w.alive():
                for msg in self._drain(w):
                    if self._handle(msg):
                        return
                if not self._stop_req.is_set():
                    if self._worker_failed("died", f"worker process died unexpectedly "
                                                   f"(exit code {w.exitcode()})"):
                        return
                continue
            v = check_deadlines(now, w.hb.op_start.value, w.hb.case_start.value,
                                self.project.run.op_timeout_ms, self.project.run.case_timeout_ms)
            if v is not None and self._worker_failed(v.kind, v.detail):
                return
            if self.checkpoint_interval_s and now >= next_ckpt:
                next_ckpt = now + self.checkpoint_interval_s
                self._save_checkpoint()
        self._set_stop("stopped by user")

    def _drain(self, w: WorkerProcess) -> list:
        out: list = []
        for _ in range(20):
            got = w.poll(0.02)
            out.extend(got)
            if w.eof or not got:
                break
        return out

    def _conditions_met(self) -> bool:
        sc, reason = self.stop_cond, ""
        with self._lock:
            if sc.max_seconds is not None and self._counted_seconds() >= sc.max_seconds:
                reason = "max_seconds"
            elif sc.max_cases is not None and self._cases >= sc.max_cases:
                reason = "max_cases"
            elif sc.max_unique_errors is not None and len(self._run_unique) >= sc.max_unique_errors:
                reason = "max_unique_errors"
            elif sc.first_error and self._errors_total > 0:
                reason = "first_error"
            elif sc.coverage_complete and self._coverage.epochs >= 1:
                reason = "coverage_complete"
        if reason:
            self._set_stop(reason)
            return True
        return False

    # ------------------------------------------------------------------ worker lifecycle
    def _spawn(self, skip: int) -> None:
        sc = self.stop_cond
        remaining = None if sc.max_cases is None else max(0, sc.max_cases - self._cases)
        cfg = WorkerConfig(
            project=self.project, adapter_spec=self.adapter_spec, run_id=self.run_id,
            generator_factory=self.generator_factory, checkpoint=self._ckpt, skip=skip,
            max_cases=remaining, shot_dir=self._shot_dir if self.project.run.capture_screenshot else "",
            known_signatures=tuple(self._known_signatures()))
        self._w = WorkerProcess(cfg)
        self._ckpt_pos = 0
        self._inc_counted = 0
        if self._pause_flag:
            self._w.pause_ev.set()
        self._last_batch_t = time.monotonic()

    def _known_signatures(self) -> set[str]:
        known = set(self._run_unique)
        try:
            known |= set(self.store.counts())
        except Exception:
            pass
        return known

    def _stop_worker(self) -> None:
        w = self._w
        if w is None:
            return
        w.stop_ev.set()
        deadline = time.monotonic() + 0.8
        try:
            while time.monotonic() < deadline and (w.alive() or not w.eof):
                for msg in w.poll(0.02):
                    self._handle(msg, stopping=True)
                if w.eof:
                    break
        except Exception:
            pass
        if w.alive():
            w.kill()
        else:
            w.shutdown(0.0)
        self._w = None

    def _worker_failed(self, kind: str, detail: str) -> bool:
        """Watchdog path. Returns True if the run must end."""
        w = self._w
        run = self.project.run
        skip = max(0, w.hb.pos.value - self._ckpt_pos)
        cat = classify_wedge(self._target_pid, self._alive)
        recent = self._recent
        extra = max(0, int(w.hb.done.value) - self._inc_counted)
        with self._lock:
            self._cases += extra
        if extra:
            self._no_progress = 0
        case = self._regenerate_case(skip)
        w.kill()
        if run.restart_on_crash and cat == "hang" and self._target_pid and self.project.target.kind == "exe":
            procwatch.kill_tree(self._target_pid)
        msg = f"watchdog: {detail}"
        sig = evidence.fingerprint(cat, msg, "watchdog")
        history = list(recent) + [Action("wait", "watchdog:" + kind, None, 0.0, False)]
        event = ErrorEvent(id=uuid.uuid4().hex[:16], ts=datetime.now(timezone.utc).isoformat(),
                           run_id=self.run_id, category=cat, signature=sig, message=msg, case=case,
                           history=history, env=dict(self._env))
        self._record(event, None)
        with self._lock:
            self._restarts += 1
        self._no_progress += 1
        if cat == "crash" and not run.restart_on_crash:
            self._set_stop("target crashed (restart_on_crash is off)")
            return True
        if self._no_progress >= MAX_NO_PROGRESS_RESTARTS:
            self._set_stop(f"watchdog: no progress after {self._no_progress} consecutive worker restarts")
            return True
        if self._stop_req.is_set():
            return True
        self._spawn(skip)
        return False

    def _regenerate_case(self, pulls: int) -> Case:
        """Rebuild the in-flight case deterministically from the last checkpoint."""
        placeholder = Case(seq=-1, state={}, values={})
        if pulls <= 0:
            return placeholder
        try:
            gf = load_callable(self.generator_factory or "overgrind.scheduler:make_generator")
            it = iter(gf(self.project, checkpoint=self._ckpt))
            case = placeholder
            for _ in range(pulls):
                case = next(it)
            return case
        except Exception:
            return placeholder

    # ------------------------------------------------------------------ messages
    def _handle(self, msg: tuple, stopping: bool = False) -> bool:
        """Apply one worker message. True => the run is over."""
        kind = msg[0]
        if kind == "batch":
            self._on_batch(msg[1])
        elif kind == "failure":
            self._record(msg[1], msg[2])
        elif kind == "ready":
            self._target_pid = msg[1]
        elif kind == "fatal":
            self._set_stop(str(msg[1]))
            return True
        elif kind == "done":
            if not stopping and not self._stop_req.is_set():
                self._set_stop(str(msg[1]))
            return not stopping
        return False

    def _on_batch(self, b: dict) -> None:
        now = time.monotonic()
        with self._lock:
            n = int(b.get("cases", 0))
            self._cases += n
            self._inc_counted += n
            self._harness += int(b.get("harness", 0))
            self._restarts += int(b.get("restarts", 0))
            dt = now - self._last_batch_t
            self._last_batch_t = now
            if dt > 0 and self._state == "running":
                self._rate = n / dt if self._rate == 0.0 else 0.6 * self._rate + 0.4 * (n / dt)
            if n:
                self._no_progress = 0
            if b.get("coverage") is not None:
                self._coverage = b["coverage"]
            if b.get("current"):
                self._current = b["current"]
            if b.get("recent"):
                self._recent = tuple(b["recent"])
            if b.get("pid"):
                self._target_pid = b["pid"]
            if b.get("checkpoint") is not None:
                self._ckpt = b["checkpoint"]
                self._ckpt_pos = int(b.get("pos", 0))
        for sig, (cat, count) in (b.get("dups") or {}).items():
            with self._lock:
                self._errors_total += count
                self._crashes += count if cat == "crash" else 0
                self._hangs += count if cat == "hang" else 0
                self._run_unique.add(sig)
            try:
                self.store.bump(sig, count)
            except Exception:
                pass

    def _record(self, event: ErrorEvent, shot: str | None) -> None:
        """Persist one failure (first occurrence of its signature writes; repeats only bump)."""
        with self._lock:
            self._errors_total += 1
            self._crashes += event.category == "crash"
            self._hangs += event.category == "hang"
            self._run_unique.add(event.signature)
        written = False
        try:
            if self.store.known(event.signature):
                self.store.bump(event.signature)
            else:
                self.store.write(event, {"screenshot": shot} if shot and os.path.isfile(shot) else None)
                written = True
        except Exception as exc:       # evidence must never kill the run
            with self._lock:
                self._current = f"evidence write failed: {exc}"[:200]
        if shot:
            try:
                os.unlink(shot)
            except OSError:
                pass
        if written:
            for cb in list(self._cbs):
                try:
                    cb(event)
                except Exception:
                    pass
