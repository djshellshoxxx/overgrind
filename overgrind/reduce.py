"""DIAG: Auto-Reduce Reproducer (reliability, ddmin, state/value simplification, export).

The replay engine is injectable: ``runner(actions: list[Action]) -> Outcome`` (each call must
start from a clean reset). Originals are never mutated.
"""
from __future__ import annotations

import copy
import importlib
import math
import time
from dataclasses import replace
from typing import Any, Callable, Sequence

from .evidence import capture_env, classify, outcome_fingerprint, redact_reduced
from .types import (
    Action, AdapterSpec, Case, ControlDef, ErrorEvent, HarnessError, Outcome, Project,
)

Runner = Callable[[list], Outcome]


class BudgetExceeded(Exception):
    pass


# ------------------------------------------------------------------ reliability
ALPHA = 0.05          # one-sided 95% bounds


def classify_reliability(results: Sequence[Any]) -> str:
    """results: bools or Outcomes (True / not ok == reproduced). Labels follow spec U3 3.1:

    reliable        every one of n >= 3 replays reproduced (p >= 0.37 at 95%)
    flaky           0 < r < n
    not_reproducible none of n >= 5 replays reproduced (p <= 0.45 at 95%: 'may still exist at that rate')
    unconfirmed     anything else (too few replays to say more)
    """
    flags = [(not r.ok) if isinstance(r, Outcome) else bool(r) for r in results]
    if not flags:
        return "unconfirmed"
    n, r = len(flags), sum(flags)
    if r == n:
        return "reliable" if n >= 3 else "unconfirmed"
    if r:
        return "flaky"
    return "not_reproducible" if n >= 5 else "unconfirmed"


def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def reproduce_lower_bound(r: int, n: int, alpha: float = ALPHA) -> float:
    """One-sided exact (Clopper-Pearson) lower bound on the per-replay reproduction probability."""
    if n <= 0 or r <= 0:
        return 0.0
    if r >= n:
        return alpha ** (1.0 / n)
    lo, hi = 0.0, 1.0                    # P(X >= r | p) is increasing in p; find where it equals alpha
    for _ in range(60):
        mid = (lo + hi) / 2
        if 1.0 - _binom_cdf(r - 1, n, mid) > alpha:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def reproduce_upper_bound(r: int, n: int, alpha: float = ALPHA) -> float:
    """One-sided exact upper bound (r == 0 gives 1 - alpha**(1/n): 'not reproduced in n tries')."""
    if n <= 0:
        return 1.0
    if r >= n:
        return 1.0
    if r == 0:
        return 1.0 - alpha ** (1.0 / n)
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _binom_cdf(r, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ------------------------------------------------------------------ actions from a case
def actions_from_case(project: Project, case: Case) -> list[Action]:
    acts = [Action("reset", "*")]
    done: set[str] = set()
    for fid in list(case.order) + sorted(case.state):
        if fid in done:
            continue
        done.add(fid)
        if fid in case.state:
            acts.append(Action("state", fid, case.state[fid]))
        else:
            acts.append(Action("invoke", fid))
    for cid in sorted(case.values):
        acts.append(Action("control", cid, case.values[cid]))
    return acts


# ------------------------------------------------------------------ default replay engine
class AdapterRunner:
    """Builds the adapter from an AdapterSpec, replays actions from a clean reset.

    The adapter is reused while healthy and rebuilt after any failure.
    """

    def __init__(self, project: Project, spec: AdapterSpec) -> None:
        self.project, self.spec = project, spec
        self._adapter: Any = None
        self._feat = {f.id: f for f in project.features}
        self._ctl = {c.id: c for c in project.controls}

    def _build(self) -> Any:
        mod, _, attr = self.spec.factory.partition(":")
        fac = getattr(importlib.import_module(mod), attr)
        a = fac(**dict(self.spec.kwargs))
        a.attach(self.project.target)
        return a

    def close(self) -> None:
        if self._adapter is not None:
            try:
                self._adapter.detach(kill=True)
            except Exception:
                pass
            self._adapter = None

    def __call__(self, actions: list) -> Outcome:
        try:
            if self._adapter is None:
                self._adapter = self._build()
            a = self._adapter
            a.reset(self.project)
            for act in actions:
                self._apply(a, act)
                h = a.health()
                if h.status != "ok":
                    out = classify(h)
                    break
            else:
                a.wait_quiescent(self.project.run.settle_ms)
                out = classify(a.health())
        except BaseException as exc:  # noqa: BLE001 - mapped, KeyboardInterrupt re-raised below
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            out = classify(exc)
        if not out.ok:
            self.close()
        return out

    def _apply(self, a: Any, act: Action) -> None:
        k = act.kind
        if k == "reset":
            a.reset(self.project)
        elif k == "state":
            a.set_feature_state(self._need(self._feat, act.target), str(act.value))
        elif k == "invoke":
            a.invoke(self._need(self._feat, act.target))
        elif k == "control":
            a.set_control(self._need(self._ctl, act.target), act.value)
        elif k == "wait":
            a.wait_quiescent(int(act.value or 0))
        elif k == "restart":
            a.detach(kill=True)
            a.attach(self.project.target)
        else:
            raise HarnessError(f"unknown action kind {k!r}")

    @staticmethod
    def _need(table: dict, key: str) -> Any:
        if key not in table:
            raise HarnessError(f"unknown target {key!r}")
        return table[key]


# ------------------------------------------------------------------ ddmin
def ddmin(items: list, test: Callable[[list], bool]) -> list:
    """Zeller's ddmin. `test(sub)` is True if sub still fails (interesting)."""
    n = 2
    while len(items) >= 2:
        chunk = max(1, len(items) // n)
        subsets = [items[i:i + chunk] for i in range(0, len(items), chunk)]
        reduced = False
        for s in subsets:
            if test(s):
                items, n, reduced = s, 2, True
                break
        if not reduced:
            for i in range(len(subsets)):
                comp = [x for j, s in enumerate(subsets) if j != i for x in s]
                if comp and test(comp):
                    items, n, reduced = comp, max(n - 1, 2), True
                    break
        if not reduced:
            if n >= len(items):
                break
            n = min(len(items), n * 2)
    if len(items) == 1 and test([]):
        return []
    return items


# ------------------------------------------------------------------ value shrinking
def shrink_candidates(value: Any, default: Any, kind: str = "") -> list:
    """Simpler values to try, most aggressive first. Never contains `value` itself."""
    c: list = []
    if value == default and type(value) is type(default):
        return c
    if default is not None:
        c.append(default)
    if isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)) and isinstance(default, (int, float)) and not isinstance(default, bool):
        if isinstance(value, int) and isinstance(default, int):
            diff = value - default
            sign, mag = (1 if diff > 0 else -1), abs(diff)
            while mag > 1:
                mag //= 2                                           # exact integer bisection (huge ints ok)
                c.append(default + sign * mag)
            c.append(default + (1 if value > default else -1))
        else:
            try:
                diff = float(value) - float(default)
            except OverflowError:
                diff = 0.0
            for _ in range(12 if diff else 0):
                diff /= 2
                c.append(float(default) + diff)
    elif isinstance(value, str):
        c += ["", value[:1], value[: len(value) // 2], value[:-1]]
        simple = "a" * len(value)
        c.append(simple)
        if len(value) > 1:
            c.append("a")
    seen, out = set(), []
    for x in c:
        key = (type(x).__name__, repr(x))
        if key in seen or (type(x) is type(value) and x == value):
            continue
        seen.add(key)
        out.append(x)
    return out


# ------------------------------------------------------------------ reducer
class Reducer:
    def __init__(self, project: Project, adapter_spec: AdapterSpec | None = None,
                 runner: Runner | None = None, *, max_replays: int = 300,
                 max_seconds: float = 120.0, confirm_runs: int | None = None,
                 store: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.project, self.spec = project, adapter_spec
        self._own_runner = runner is None
        if runner is None:
            if adapter_spec is None:
                raise ValueError("need adapter_spec or runner")
            runner = AdapterRunner(project, adapter_spec)
        self.runner: Runner = runner
        self.max_replays, self.max_seconds = max_replays, max_seconds
        self.confirm_runs = confirm_runs if confirm_runs is not None else max(1, project.run.confirm_runs)
        self.store, self.clock = store, clock
        self._defaults = {c.id: c.default for c in project.controls}
        self._fdefault = {f.id: f.default_state for f in project.features}
        self._reset_budget()

    def _reset_budget(self) -> None:
        self.replays = 0
        self._t0 = self.clock()
        self._cache: dict[tuple, bool] = {}
        self._baseline_sig: str | None = None
        self._target_sig = ""
        self._best: list | None = None
        self._neg_retries = 0            # extra tries of a negative candidate (flaky targets)
        self._partial_reason = ""

    def close(self) -> None:
        if self._own_runner and hasattr(self.runner, "close"):
            self.runner.close()

    # -- matching / replay
    def _same(self, out: Outcome) -> bool:
        if out.ok or out.category == "harness":
            return False
        sig = outcome_fingerprint(out)
        return sig == self._target_sig or sig == self._baseline_sig

    def _replay(self, actions: list) -> Outcome:
        if self.replays >= self.max_replays or self.clock() - self._t0 > self.max_seconds:
            raise BudgetExceeded()
        self.replays += 1
        return self.runner(list(actions))

    def _test(self, actions: list) -> bool:
        key = tuple((a.kind, a.target, repr(a.value)) for a in actions)
        if key not in self._cache:
            res = self._same(self._replay(actions))
            tries = 0
            while not res and tries < self._neg_retries:      # flaky: a miss is weak evidence, retry it
                tries += 1
                res = self._same(self._replay(actions))
            self._cache[key] = res
        return self._cache[key]

    # -- main
    @staticmethod
    def _cut(history: list) -> tuple[list, bool]:
        """Prefix cut (spec 3.3 step 1): start at the last `reset`; without one the pre-state is unknown."""
        for i in range(len(history) - 1, -1, -1):
            if history[i].kind == "reset":
                return history[i:], False
        return history, True

    def _confirm(self, orig: list, event: ErrorEvent) -> list[Outcome]:
        """Replay the failing sequence: n0 = max(confirm_runs, 3); no repro -> 5 (10 for hang/resource);
        mixed -> 10. Harness errors are not counted (retried at most twice in total)."""
        results: list[Outcome] = []
        harness = 0

        def until(n: int) -> bool:
            nonlocal harness
            while len(results) < n:
                out = self._replay(orig)
                if not out.ok and out.category == "harness":
                    harness += 1
                    if harness > 2:
                        return False
                    continue
                results.append(out)
                if not out.ok and self._baseline_sig is None:
                    sig = outcome_fingerprint(out)
                    if sig == event.signature or out.category == event.category:
                        self._baseline_sig = sig
            return True

        if not until(max(self.confirm_runs, 3)):
            return results
        r = sum(self._same(o) for o in results)
        if r == 0:
            until(10 if event.category in ("hang", "resource") else 5)
        elif r < len(results):
            until(10)
        return results

    def reduce(self, event: ErrorEvent) -> ErrorEvent:
        self._reset_budget()
        self._target_sig = event.signature
        full = list(event.history)
        unknown_pre = False
        if full:
            orig, unknown_pre = self._cut(full)
        else:
            orig = full = actions_from_case(self.project, event.case)
        results: list[Outcome] = []
        exhausted = False
        steps = orig
        repro: list[bool] = []
        try:
            # (1) reliability: replay the unmodified failing sequence (original observation not counted)
            results = self._confirm(orig, event)
            repro = [self._same(o) for o in results]
            reliability = classify_reliability(repro)
            if any(repro):
                if 0 < sum(repro) < len(repro):
                    p_hat = sum(repro) / len(repro)
                    need = math.ceil(math.log(ALPHA) / math.log(1.0 - p_hat))
                    self._neg_retries = min(10, need) - 1
                    if need > 10:
                        self._partial_reason = "low reproduction rate: negative candidates were retried only 10 times"
                steps = self._minimise(orig)
        except BudgetExceeded:
            exhausted = True
            repro = [self._same(o) for o in results]
            reliability = classify_reliability(repro) if repro else "unconfirmed"
            steps = self._best or orig
        reduced = self._build_reduced(event, full, steps, reliability, repro, exhausted)
        sens = [c.id for c in self.project.controls if c.sensitive]
        if sens:                                   # same redaction as on disk, whoever ends up writing it
            reduced = redact_reduced(reduced, sens)
        if unknown_pre:
            reduced["assumes_unknown_prestate"] = True
        if self._partial_reason:
            reduced["partial"], reduced["reason"] = True, self._partial_reason
        new = copy.deepcopy(event)
        new.reduced = reduced
        new.reliability = reliability
        n, k = len(repro), sum(repro)
        if n:
            new.env = dict(new.env)
            new.env["reliability"] = {"n": n, "r": k, "p_lo": round(reproduce_lower_bound(k, n), 4),
                                      "p_hi": round(reproduce_upper_bound(k, n), 4), "level": 0.95}
        if self.store is not None:
            try:
                path = self.store.write_reduced(event, reduced)
                new.attachments = dict(new.attachments)
                new.attachments["reduced"] = str(path)
            except OSError:
                pass
        return new

    def _minimise(self, orig: list) -> list:
        self._best = orig
        # (2) ddmin over the action list
        def t(sub: list) -> bool:
            ok = self._test(sub)
            if ok and len(sub) < len(self._best):
                self._best = list(sub)
            return ok
        steps = list(ddmin(list(orig), t))
        self._best = steps
        # (3a) state vector: turn features back to default one at a time
        for i, a in enumerate(list(steps)):
            if a.kind == "state" and a.value != self._fdefault.get(a.target, a.value):
                cand = steps[:i] + [replace(a, value=self._fdefault[a.target])] + steps[i + 1:]
                if self._test(cand):
                    steps = cand
                    self._best = steps
        # drop now-default state actions if still failing
        for a in list(steps):
            if a.kind == "state" and a.value == self._fdefault.get(a.target):
                cand = [x for x in steps if x is not a]
                if self._test(cand):
                    steps = cand
                    self._best = steps
        # (3b) shrink control values toward defaults
        for i in range(len(steps)):
            a = steps[i]
            if a.kind != "control":
                continue
            default = self._defaults.get(a.target)
            progressed = True
            while progressed:
                progressed = False
                for cand_v in shrink_candidates(steps[i].value, default):
                    cand = steps[:i] + [replace(steps[i], value=cand_v)] + steps[i + 1:]
                    if self._test(cand):
                        steps = cand
                        self._best = steps
                        progressed = True
                        break
        return steps

    def _build_reduced(self, event: ErrorEvent, orig: list, steps: list, reliability: str,
                       repro: list, exhausted: bool) -> dict[str, Any]:
        n = len(repro)
        k = sum(repro)
        state = {a.target: a.value for a in steps if a.kind == "state"}
        values = {a.target: a.value for a in steps if a.kind == "control"}
        return {
            "signature": event.signature,
            "category": event.category,
            "message": event.message,
            "reliability": reliability,
            "reliability_pct": round(100.0 * k / n) if n else None,
            "confirm": {"reproduced": k, "runs": n},
            "version": 1,
            "original_steps": len(orig),
            "reduced_steps": len(steps),
            "steps": [{"kind": a.kind, "target": a.target, "value": a.value} for a in steps],
            "state": state,
            "values": values,
            "replays": self.replays,
            "budget_exhausted": exhausted,
            "env": copy.deepcopy(event.env) or capture_env(self.project.target.exe_path),
        }


# ------------------------------------------------------------------ export
def export_recipe(event: ErrorEvent) -> tuple[str, dict[str, Any]]:
    red = event.reduced
    if red is None:
        steps = [{"kind": a.kind, "target": a.target, "value": a.value} for a in event.history]
        red = {"steps": steps, "reliability": event.reliability, "reliability_pct": None,
               "original_steps": len(steps), "reduced_steps": len(steps), "state": dict(event.case.state),
               "values": dict(event.case.values)}
    rel = red.get("reliability", event.reliability)
    pct = red.get("reliability_pct")
    data = {
        "signature": event.signature, "category": event.category, "message": event.message,
        "reliability": rel, "reliability_pct": pct, "env": event.env,
        "steps": red["steps"], "state": red.get("state", {}), "values": red.get("values", {}),
        "original_steps": red.get("original_steps"), "reduced_steps": red.get("reduced_steps"),
    }
    lines = [
        f"# {event.category.capitalize()}: {event.message or event.signature}",
        "",
        f"- Signature: `{event.signature}`",
        f"- Reliability: {rel}" + (f" ({pct}% of confirmation runs)" if pct is not None else ""),
        f"- Occurrences seen: {event.count}",
        "",
        "## Environment",
    ]
    lines += [f"- {k}: {v}" for k, v in sorted((event.env or {}).items())] or ["- (not captured)"]
    lines += ["", f"## Steps to reproduce (from a clean reset; {len(red['steps'])} of "
                  f"{red.get('original_steps', len(red['steps']))} recorded steps)"]
    for i, s in enumerate(red["steps"], 1):
        k, t, v = s["kind"], s["target"], s.get("value")
        txt = {"reset": "Reset the application to its defaults",
               "state": f"Set feature `{t}` to `{v}`",
               "invoke": f"Invoke `{t}`",
               "control": f"Set control `{t}` to `{v!r}`",
               "wait": f"Wait for the UI to settle ({v} ms)",
               "restart": "Restart the application"}.get(k, f"{k} {t} {v!r}")
        lines.append(f"{i}. {txt}")
    if red.get("state") or red.get("values"):
        lines += ["", "## Effective inputs"]
        lines += [f"- feature `{k}` = `{v}`" for k, v in sorted(red.get("state", {}).items())]
        lines += [f"- control `{k}` = `{v!r}`" for k, v in sorted(red.get("values", {}).items())]
    lines += ["", "## Expected / actual", "- Expected: the application keeps running and stays responsive.",
              f"- Actual: {event.category} - {event.message}", ""]
    return "\n".join(lines), data
