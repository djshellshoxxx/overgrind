"""Worker process: the ONLY place that touches the GUI target.

Spawned by `watchdog.WorkerProcess` (multiprocessing 'spawn'; frozen apps must call
`multiprocessing.freeze_support()` first thing in `__main__`). It builds the adapter from the
picklable `AdapterSpec`, pulls `Case`s from the generator, executes them and talks to the
controller over a one-way pipe with COMPACT messages:

    ("ready", target_pid)
    ("batch", {...})            # every ~200 ms: counters, current case, ring tail, coverage, checkpoint
    ("failure", ErrorEvent, screenshot_path | None)   # first occurrence of a signature only
    ("fatal", reason)           # unrecoverable: controller stops with this reason
    ("done", reason)            # generator exhausted / max_cases reached / stop requested

Successful cases only increment counters; nothing is written to disk for them.
"""
from __future__ import annotations

import importlib
import multiprocessing
import os
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .types import (
    Action, AdapterSpec, Case, ErrorEvent, Health, HarnessError, Outcome, Project,
)

OK = Outcome(ok=True)
HARNESS_LIMIT = 20
_OFF_STATES = ("off", "false", "0", "none", "")


@dataclass
class WorkerConfig:
    project: Project
    adapter_spec: AdapterSpec
    run_id: str
    generator_factory: str | None = None      # 'pkg.mod:callable(project, checkpoint=)'; default scheduler.make_generator
    checkpoint: dict | None = None
    skip: int = 0                              # cases to discard after restoring the checkpoint
    max_cases: int | None = None               # remaining budget for this incarnation
    shot_dir: str = ""                         # temp dir for failure screenshots
    known_signatures: tuple[str, ...] = ()
    batch_interval_s: float = 0.2
    harness_limit: int = HARNESS_LIMIT
    env_extra: dict = field(default_factory=dict)


def load_callable(path: str) -> Any:
    mod, _, attr = path.partition(":")
    if not mod or not attr:
        raise HarnessError(f"bad factory {path!r}; expected 'package.module:callable'")
    try:
        return getattr(importlib.import_module(mod), attr)
    except (ImportError, AttributeError) as exc:
        raise HarnessError(f"cannot load {path!r}: {exc}") from exc


def is_active(state: str | None) -> bool:
    """A feature counts as 'active' (for control scope) when its state is not an off-state."""
    return state is not None and str(state).strip().lower() not in _OFF_STATES


def short_case(case: Case, limit: int = 140) -> str:
    on = [k for k, v in case.state.items() if is_active(v)]
    s = f"#{case.seq} on={','.join(on) or '-'}"
    return s if len(s) <= limit else s[: limit - 1] + "…"


class _Stop(Exception):
    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind, self.reason = kind, reason


class Worker:
    def __init__(self, cfg: WorkerConfig, tx: Any, hb: Any, stop_ev: Any, pause_ev: Any) -> None:
        from . import evidence
        self.ev = evidence
        self.cfg, self.tx, self.hb, self.stop_ev, self.pause_ev = cfg, tx, hb, stop_ev, pause_ev
        self.project = cfg.project
        self.run = cfg.project.run
        self.feats = {f.id: f for f in self.project.features}
        self.ctrls = {c.id: c for c in self.project.controls}
        self.known = set(cfg.known_signatures)
        self.t0 = time.monotonic()
        self.ring = evidence.RingBuffer(self.run.ring_size)
        self.adapter: Any = None
        self.gen: Any = None
        self.d_cases = self.d_harness = self.d_restarts = 0
        self.dups: dict[str, list] = {}
        self.executed = 0
        self.consec_harness = 0
        self.since_reset = 0
        self.need_reset = self.run.reset_policy != "never"
        self.last_case: Case | None = None
        self.last_batch = 0.0
        self.pos = 0
        env_extra = {"run_id": cfg.run_id, "seed": self.run.seed, "strategy": self.run.strategy,
                     "project": self.project.name, **cfg.env_extra}
        try:
            self.env = evidence.capture_env(self.project.target.exe_path, env_extra)
        except Exception:
            self.env = dict(env_extra)

    # ------------------------------------------------------------------ messaging
    def send(self, msg: tuple) -> None:
        try:
            self.tx.send(msg)
        except (BrokenPipeError, OSError, EOFError):
            os._exit(0)             # controller is gone; never keep fuzzing orphaned

    def batch(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_batch < self.cfg.batch_interval_s:
            return
        self.last_batch = now
        if not self._parent_alive():
            os._exit(0)
        try:
            cov = self.gen.coverage() if self.gen is not None else None
            ckpt = self.gen.checkpoint() if self.gen is not None else None
        except Exception:
            cov, ckpt = None, None
        try:
            pid = self.adapter.pid() if self.adapter is not None else None
        except Exception:
            pid = None
        payload = {
            "cases": self.d_cases, "harness": self.d_harness, "restarts": self.d_restarts,
            "pos": self.hb.pos.value, "current": short_case(self.last_case) if self.last_case else "",
            "recent": tuple(self.ring.snapshot()[-12:]), "coverage": cov, "checkpoint": ckpt,
            "pid": pid, "dups": self.dups,
        }
        self.d_cases = self.d_harness = self.d_restarts = 0
        self.dups = {}
        self.send(("batch", payload))

    @staticmethod
    def _parent_alive() -> bool:
        p = multiprocessing.parent_process()
        return p is None or p.is_alive()

    # ------------------------------------------------------------------ operations
    def op(self, kind: str, target: str, value: Any, fn: Any, *args: Any) -> None:
        t = time.monotonic()
        self.hb.op_start.value = t
        ok = False
        try:
            fn(*args)
            ok = True
        finally:
            self.hb.op_start.value = 0.0
            self.ring.push(Action(kind, target, value, (t - self.t0) * 1000.0, ok))

    def execute(self, case: Case) -> None:
        a, run = self.adapter, self.run
        reset = self.need_reset or run.reset_policy == "every_case" or (
            run.reset_policy == "interval" and self.since_reset >= max(1, run.reset_interval))
        if reset:
            self.op("reset", "*", None, a.reset, self.project)
            self.since_reset = 0
            self.need_reset = False
        self.since_reset += 1
        applied: set[str] = set()
        for fid in list(case.order) + list(case.state):
            if fid in applied:
                continue
            applied.add(fid)
            f = self.feats.get(fid)
            if f is None:
                raise HarnessError(f"case references unknown feature {fid!r}")
            if not f.enabled or f.dangerous:
                continue                            # dangerous features are never auto-activated
            if f.kind == "action":
                if fid in case.order:
                    self.op("invoke", fid, None, a.invoke, f)
            elif fid in case.state:
                st = case.state[fid]
                self.op("state", fid, st, a.set_feature_state, f, st)
        for cid, val in case.values.items():
            c = self.ctrls.get(cid)
            if c is None or c.skip or c.read_only:
                continue
            if c.scope and not self.scope_ok(c, case):
                continue
            self.op("control", cid, val, a.set_control, c, val)
        dwell = int(case.dwell_ms) + int(run.settle_ms)
        if dwell > 0:
            self.ring.push(Action("wait", "*", dwell, (time.monotonic() - self.t0) * 1000.0))
            end = time.monotonic() + dwell / 1000.0
            while True:
                left = end - time.monotonic()
                if left <= 0 or self.stop_ev.is_set():
                    break
                time.sleep(min(left, 0.05))
        self.op("wait", "quiesce", None, a.wait_quiescent, max(50, run.op_timeout_ms // 2))

    def scope_ok(self, c: Any, case: Case) -> bool:
        """types.ControlDef.scope: every scope feature must be *active*, i.e. in a state other than its
        first (off) state. Same rule as constraints/states/strategies; unknown features fall back to the
        off-word heuristic."""
        for sid in c.scope:
            f = self.feats.get(sid)
            st = case.state.get(sid, f.default_state if f else None)
            if f is not None and f.states:
                if st == f.states[0]:
                    return False
            elif not is_active(st):
                return False
        return True

    # ------------------------------------------------------------------ health / classification
    def confirm_health(self) -> Health:
        h = self.adapter.health()
        if h.status != "hung":
            return h
        deadline = time.monotonic() + self.run.hang_threshold_ms / 1000.0
        while time.monotonic() < deadline:
            time.sleep(0.03)
            h = self.adapter.health()
            if h.status != "hung":
                return Health("ok", "transient busy") if h.status == "ok" else h
        return h

    def judge(self, exc: BaseException | None) -> Outcome:
        ev = self.ev
        try:
            h = self.confirm_health()
        except Exception as hexc:               # health itself failing is a harness problem
            return ev.classify(exc) if exc is not None else ev.classify(hexc)
        if h.status != "ok":
            msg = h.detail
            if exc is not None and not msg:
                msg = str(exc)
            return ev.classify(h, msg)
        return ev.classify(exc) if exc is not None else OK

    # ------------------------------------------------------------------ failures
    def report(self, case: Case, out: Outcome) -> None:
        ev = self.ev
        sig = ev.outcome_fingerprint(out)
        if sig in self.known:
            d = self.dups.setdefault(sig, [out.category, 0])
            d[1] += 1
            return
        self.known.add(sig)
        eid = uuid.uuid4().hex[:16]
        shot = None
        if self.run.capture_screenshot and self.cfg.shot_dir:
            try:
                os.makedirs(self.cfg.shot_dir, exist_ok=True)
                path = os.path.join(self.cfg.shot_dir, eid + ".png")
                self.hb.op_start.value = time.monotonic()
                try:
                    if self.adapter.screenshot(path):
                        shot = path
                finally:
                    self.hb.op_start.value = 0.0
            except Exception:
                shot = None
        event = ErrorEvent(
            id=eid, ts=datetime.now(timezone.utc).isoformat(), run_id=self.cfg.run_id,
            category=out.category, signature=sig, message=out.message, case=case,
            history=self.ring.snapshot(), env=dict(self.env))
        self.send(("failure", event, shot))

    def recover(self, out: Outcome) -> None:
        """Bring the target back into a testable state or stop the run."""
        cat = out.category
        if cat in ("crash", "hang"):
            if not self.run.restart_on_crash:
                raise _Stop("fatal", f"target {'crashed' if cat == 'crash' else 'hung'} "
                                     f"(restart_on_crash is off): {out.message}"[:300])
            self.restart_target()
        elif cat == "dialog":
            try:
                self.op("reset", "dialog", None, self.adapter.reset, self.project)
                ok = self.adapter.health().status == "ok"
            except Exception:
                ok = False
            if not ok:
                if self.run.restart_on_crash:
                    self.restart_target()
                else:
                    raise _Stop("fatal", "modal dialog could not be dismissed")
        self.need_reset = True

    def restart_target(self) -> None:
        self.ring.push(Action("restart", "target", None, (time.monotonic() - self.t0) * 1000.0))
        self.hb.op_start.value = time.monotonic()
        try:
            try:
                self.adapter.detach(kill=True)
            except Exception:
                pass
            self.adapter.attach(self.project.target)
        except Exception as exc:
            raise _Stop("fatal", f"automation unusable: cannot restart target: {exc}"[:300])
        finally:
            self.hb.op_start.value = 0.0
        self.d_restarts += 1
        self.ring = self.ev.RingBuffer(self.run.ring_size)
        self.need_reset = True
        self.since_reset = 0

    # ------------------------------------------------------------------ main loop
    def setup(self) -> None:
        cfg = self.cfg
        factory = load_callable(cfg.adapter_spec.factory)
        self.adapter = factory(**dict(cfg.adapter_spec.kwargs))
        try:
            self.adapter.attach(self.project.target)
        except Exception as exc:
            raise _Stop("fatal", f"automation unusable: cannot attach to target: {exc}"[:300])
        gf = load_callable(cfg.generator_factory or "overgrind.scheduler:make_generator")
        self.gen = gf(self.project, checkpoint=cfg.checkpoint)
        self.it = iter(self.gen)
        for _ in range(max(0, cfg.skip)):
            next(self.it)
        self.pos = max(0, cfg.skip)
        self.hb.pos.value = self.pos
        self.send(("ready", self.adapter.pid()))

    def loop(self) -> str:
        hb, run, cfg = self.hb, self.run, self.cfg
        limit = cfg.harness_limit
        while True:
            if self.stop_ev.is_set():
                return "stopped"
            if self.pause_ev.is_set():
                self.batch(force=True)
                while self.pause_ev.is_set() and not self.stop_ev.is_set():
                    time.sleep(0.02)
                    self.batch()
                continue
            if cfg.max_cases is not None and self.executed >= cfg.max_cases:
                return "max_cases"
            try:
                case = next(self.it)
            except StopIteration:
                return "generator exhausted"
            self.pos += 1
            hb.pos.value = self.pos
            self.last_case = case
            hb.case_start.value = time.monotonic()
            exc: BaseException | None = None
            try:
                self.execute(case)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as e:      # noqa: BLE001 - everything is classified
                exc = e
            try:
                out = self.judge(exc)
            finally:
                hb.case_start.value = 0.0
            if out.ok:
                self.consec_harness = 0
                self.executed += 1
                self.d_cases += 1
                self.gen.feedback(case, OK)
            elif out.category == "harness":
                self.consec_harness += 1
                self.d_harness += 1
                self.need_reset = True
                self.gen.feedback(case, out)
                if self.consec_harness > limit:
                    self.last_batch = 0.0
                    raise _Stop("fatal", f"automation unusable: {self.consec_harness} consecutive "
                                         f"harness errors (last: {out.message})"[:300])
            else:
                self.consec_harness = 0
                self.executed += 1
                self.d_cases += 1
                self.gen.feedback(case, out)
                self.report(case, out)
                self.recover(out)
            hb.done.value = self.executed
            self.batch()

    def run_all(self) -> None:
        reason = "stopped"
        try:
            self.setup()
            reason = self.loop()
        except _Stop as s:
            self.batch(force=True)
            self.send((s.kind, s.reason))
            self.cleanup()
            return
        self.batch(force=True)
        self.cleanup()
        self.send(("done", reason))

    def cleanup(self) -> None:
        try:
            if self.adapter is not None:
                self.adapter.detach(kill=False)
        except Exception:
            pass


def worker_main(cfg: WorkerConfig, tx: Any, hb: Any, stop_ev: Any, pause_ev: Any) -> None:
    try:
        w = Worker(cfg, tx, hb, stop_ev, pause_ev)
        w.run_all()
    except BaseException as exc:    # noqa: BLE001 - last resort: tell the controller why
        try:
            tx.send(("fatal", f"worker crashed: {type(exc).__name__}: {exc}\n"
                              + traceback.format_exc(limit=4)))
        except Exception:
            pass
    finally:
        try:
            tx.close()
        except Exception:
            pass
