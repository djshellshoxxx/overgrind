"""Parent-side watchdog: heartbeat block, deadline checks and the worker-process handle.

The controller process never touches the GUI target. It owns a `WorkerProcess` (multiprocessing
'spawn', so it also works frozen by PyInstaller - call `multiprocessing.freeze_support()` first
thing in the frozen entry point) and watches a shared-memory `Heartbeat` that the worker updates
around every adapter operation and case. A worker that overruns a deadline is killed (process
tree) and replaced; nothing here can deadlock because every wait is bounded.
"""
from __future__ import annotations

import multiprocessing
import time
from dataclasses import dataclass
from typing import Any

from .adapters import procwatch


class Heartbeat:
    """Lock-free shared doubles written by the worker, read by the parent (monotonic seconds)."""

    def __init__(self, ctx: Any) -> None:
        self.op_start = ctx.RawValue("d", 0.0)     # 0 = not inside an adapter operation
        self.case_start = ctx.RawValue("d", 0.0)   # 0 = between cases
        self.done = ctx.RawValue("q", 0)           # cases executed this incarnation (credited on kill)
        self.pos = ctx.RawValue("q", 0)            # generator position (cases pulled) this incarnation


@dataclass(frozen=True)
class Violation:
    kind: str          # op | case | died
    detail: str


def check_deadlines(now: float, op_start: float, case_start: float,
                    op_timeout_ms: int, case_timeout_ms: int) -> Violation | None:
    if op_start and (now - op_start) * 1000.0 > op_timeout_ms:
        return Violation("op", f"adapter operation exceeded {op_timeout_ms} ms")
    if case_start and (now - case_start) * 1000.0 > case_timeout_ms:
        return Violation("case", f"case exceeded {case_timeout_ms} ms")
    return None


def classify_wedge(target_pid: int | None, alive=procwatch.is_alive) -> str:
    """Wedged worker: target gone => 'crash'; target alive (or unknown) but stuck => 'hang'."""
    if target_pid and not alive(target_pid):
        return "crash"
    return "hang"


class WorkerProcess:
    """One incarnation of the worker: process + one-way pipe + shared heartbeat + flags."""

    def __init__(self, cfg: Any) -> None:
        from . import worker
        ctx = multiprocessing.get_context("spawn")
        self.hb = Heartbeat(ctx)
        self.stop_ev = ctx.Event()
        self.pause_ev = ctx.Event()
        self._rx, tx = ctx.Pipe(duplex=False)
        self.proc = ctx.Process(target=worker.worker_main, name="overgrind-worker", daemon=True,
                                args=(cfg, tx, self.hb, self.stop_ev, self.pause_ev))
        self.proc.start()
        tx.close()                      # parent keeps only the read end => EOF on worker death
        self.eof = False
        self.pid: int | None = self.proc.pid

    def poll(self, timeout: float = 0.0, limit: int = 256) -> list[Any]:
        out: list[Any] = []
        if self.eof:
            return out
        try:
            while len(out) < limit and self._rx.poll(timeout if not out else 0):
                out.append(self._rx.recv())
        except (EOFError, OSError):
            self.eof = True
        except Exception:                # truncated/garbled message from a killed worker
            self.eof = True
        return out

    def alive(self) -> bool:
        return self.proc.is_alive()

    def exitcode(self) -> int | None:
        return self.proc.exitcode

    def request_stop(self) -> None:
        self.stop_ev.set()

    def kill(self) -> None:
        """Kill the worker process tree. Bounded (<~2.5 s) and never raises."""
        try:
            if self.proc.pid:
                procwatch.kill_tree(self.proc.pid)
            if self.proc.is_alive():
                self.proc.kill()
            self.proc.join(0.5)
        except Exception:
            pass
        try:
            self._rx.close()
        except Exception:
            pass
        self.eof = True

    def shutdown(self, grace_s: float = 0.6) -> None:
        """Ask nicely (so the worker can detach), then kill."""
        self.stop_ev.set()
        deadline = time.monotonic() + grace_s
        while self.proc.is_alive() and time.monotonic() < deadline:
            self.poll(0.02)
        if self.proc.is_alive():
            self.kill()
        else:
            self.proc.join(0.2)
            try:
                self._rx.close()
            except Exception:
                pass


def parent_alive() -> bool:
    p = multiprocessing.parent_process()
    return p is None or p.is_alive()
