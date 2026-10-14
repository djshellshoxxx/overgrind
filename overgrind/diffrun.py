"""DIAG: Differential Twin-Build. Same cases against two adapters (builds), sequentially."""
from __future__ import annotations

import importlib
import time
from dataclasses import dataclass, field, replace
from random import Random
from typing import Any, Callable, Iterable

from .evidence import classify
from .types import (
    AdapterSpec, Case, ControlDef, FeatureDef, HarnessError, Outcome, Project,
)

UNRESOLVED = object()


@dataclass(frozen=True)
class DiffItem:
    kind: str            # regression | fix | outcome | value | timing | incomparable
    case_seq: int
    target: str = ""     # control / feature id, "" for whole-case
    a: Any = None
    b: Any = None
    detail: str = ""


@dataclass(frozen=True)
class DiffReport:
    n_cases: int
    compared: int
    regressions: tuple[DiffItem, ...] = ()
    fixes: tuple[DiffItem, ...] = ()
    mismatches: tuple[DiffItem, ...] = ()
    incomparable: tuple[DiffItem, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.regressions or self.fixes or self.mismatches or self.incomparable)

    def to_dict(self) -> dict[str, Any]:
        f = lambda xs: [dict(kind=i.kind, case=i.case_seq, target=i.target, a=i.a, b=i.b, detail=i.detail) for i in xs]
        return {"n_cases": self.n_cases, "compared": self.compared, "regressions": f(self.regressions),
                "fixes": f(self.fixes), "mismatches": f(self.mismatches), "incomparable": f(self.incomparable)}

    def to_markdown(self) -> str:
        L = ["# Differential report (A = baseline build, B = candidate build)", "",
             f"- Cases: {self.n_cases}, fully comparable: {self.compared}",
             f"- Regressions (A ok, B fails): {len(self.regressions)}",
             f"- Fixes (A fails, B ok): {len(self.fixes)}",
             f"- Mismatches: {len(self.mismatches)}",
             f"- Cannot compare: {len(self.incomparable)}", ""]
        for title, items in (("Regressions", self.regressions), ("Fixes", self.fixes),
                             ("Mismatches", self.mismatches), ("Cannot compare", self.incomparable)):
            if items:
                L.append(f"## {title}")
                for i in items:
                    tgt = f" `{i.target}`" if i.target else ""
                    L.append(f"- case {i.case_seq}{tgt}: A={i.a!r} B={i.b!r} {i.detail}".rstrip())
                L.append("")
        return "\n".join(L)


@dataclass
class _CaseResult:
    outcome: Outcome
    elapsed_s: float
    readback: dict[str, Any] = field(default_factory=dict)   # control id (A naming) -> value | UNRESOLVED
    unresolved: set[str] = field(default_factory=set)


# ------------------------------------------------------------------ case generation
def simple_cases(project: Project, n: int, seed: int) -> list[Case]:
    rng = Random(seed)
    feats = [f for f in project.features if f.enabled and f.kind != "action" and not f.dangerous]
    out = []
    for i in range(n):
        state = {f.id: rng.choice(f.states) for f in feats}
        values: dict[str, Any] = {}
        for c in project.controls:
            if c.skip or c.read_only:
                continue
            d = c.fuzz_domain or c.domain
            values[c.id] = _sample(d, rng, c.default)
        out.append(Case(seq=i, state=state, values=values, order=tuple(state)))
    return out


def _sample(d, rng: Random, default: Any) -> Any:
    k = d.kind
    if k == "boolean":
        return rng.random() < 0.5
    if k == "choice":
        return rng.choice(d.options) if d.options else default
    if k == "integer":
        lo, hi = int(d.minimum if d.minimum is not None else 0), int(d.maximum if d.maximum is not None else 100)
        return rng.randint(lo, max(lo, hi))
    if k == "float":
        lo, hi = float(d.minimum if d.minimum is not None else 0.0), float(d.maximum if d.maximum is not None else 1.0)
        return round(rng.uniform(lo, hi), 6)
    alphabet = d.alphabet or "abcdefghijklmnopqrstuvwxyz0123456789 "
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, d.max_length if d.max_length is not None else 12)))


def make_cases(project: Project, n: int, seed: int) -> list[Case]:
    """Use ENGINE's scheduler.make_generator when present; otherwise a simple seeded generator."""
    try:
        from . import scheduler  # type: ignore
        p = replace(project, run=replace(project.run, seed=seed))
        gen = scheduler.make_generator(p)
        cases = []
        for c in gen:
            cases.append(c)
            if len(cases) >= n:
                break
        if cases:
            return cases
    except Exception:  # missing module / unfinished API / empty state space
        pass
    return simple_cases(project, n, seed)


# ------------------------------------------------------------------ execution
def _build(spec: AdapterSpec, project: Project) -> Any:
    mod, _, attr = spec.factory.partition(":")
    a = getattr(importlib.import_module(mod), attr)(**dict(spec.kwargs))
    a.attach(project.target)
    return a


def _active(state: dict[str, str], fid: str) -> bool:
    return str(state.get(fid, "off")).lower() != "off"


def _run_build(project: Project, spec: AdapterSpec, cases: list[Case], id_map: dict[str, str | None],
               clock: Callable[[], float], factory: Callable[[], Any] | None) -> list[_CaseResult]:
    feats = {f.id: f for f in project.features}
    ctls = {c.id: c for c in project.controls}
    mk = factory or (lambda: _build(spec, project))
    adapter: Any = None
    results: list[_CaseResult] = []
    try:
        for case in cases:
            if adapter is None:
                adapter = mk()
            unresolved: set[str] = set()
            readback: dict[str, Any] = {}
            t0 = clock()
            outcome = Outcome()
            try:
                adapter.reset(project)
                for fid in list(case.order) + [f for f in case.state if f not in case.order]:
                    if id_map.get(fid, fid) is None:
                        unresolved.add(fid)
                        continue
                    f: FeatureDef = feats[fid]
                    f = replace(f, id=id_map.get(fid, fid))
                    try:
                        if fid in case.state:
                            adapter.set_feature_state(f, case.state[fid])
                        else:
                            adapter.invoke(f)
                    except HarnessError:
                        unresolved.add(fid)
                for cid, val in case.values.items():
                    c = ctls.get(cid)
                    if c is None or any(not _active(case.state, s) for s in c.scope):
                        continue
                    mapped = id_map.get(cid, cid)
                    if mapped is None:
                        unresolved.add(cid)
                        continue
                    try:
                        adapter.set_control(replace(c, id=mapped), val)
                    except HarnessError:
                        unresolved.add(cid)
                adapter.wait_quiescent(project.run.settle_ms)
                outcome = classify(adapter.health())
                if outcome.ok:
                    for cid in case.values:
                        c = ctls.get(cid)
                        if c is None or cid in unresolved or any(not _active(case.state, s) for s in c.scope):
                            continue
                        try:
                            readback[cid] = adapter.get_control(replace(c, id=id_map.get(cid, cid)))
                        except HarnessError:
                            unresolved.add(cid)
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                outcome = classify(exc)
            results.append(_CaseResult(outcome, clock() - t0, readback, unresolved))
            if not outcome.ok:
                try:
                    adapter.detach(kill=True)
                except Exception:
                    pass
                adapter = None
    finally:
        if adapter is not None:
            try:
                adapter.detach(kill=True)
            except Exception:
                pass
    return results


def _close(a: Any, b: Any, tol: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol or abs(a - b) <= tol * max(abs(a), abs(b))
    return a == b


def differential(project: Project, spec_a: AdapterSpec, spec_b: AdapterSpec, n_cases: int = 20,
                 seed: int = 1, tolerance: float = 1e-6, *, id_map: dict[str, str | None] | None = None,
                 time_factor: float = 3.0, time_floor_s: float = 0.05,
                 cases: list[Case] | None = None, clock: Callable[[], float] = time.monotonic,
                 factory_a: Callable[[], Any] | None = None,
                 factory_b: Callable[[], Any] | None = None) -> DiffReport:
    """Run identical cases on build A then build B (sequential, never concurrent).

    id_map maps control/feature ids of A to ids in B (None = known missing in B).
    """
    cs = list(cases) if cases is not None else make_cases(project, n_cases, seed)
    ra = _run_build(project, spec_a, cs, {}, clock, factory_a)
    rb = _run_build(project, spec_b, cs, dict(id_map or {}), clock, factory_b)
    reg: list[DiffItem] = []
    fix: list[DiffItem] = []
    mis: list[DiffItem] = []
    inc: list[DiffItem] = []
    compared = 0
    for case, a, b in zip(cs, ra, rb):
        s = case.seq
        for tgt in sorted(a.unresolved | b.unresolved):
            side = "A" if tgt in a.unresolved and tgt not in b.unresolved else \
                   "B" if tgt in b.unresolved and tgt not in a.unresolved else "A and B"
            inc.append(DiffItem("incomparable", s, tgt, detail=f"cannot resolve in {side}"))
        if a.outcome.ok and b.outcome.ok:
            clean = not (a.unresolved | b.unresolved)
            for cid in sorted(set(a.readback) & set(b.readback)):
                if not _close(a.readback[cid], b.readback[cid], tolerance):
                    mis.append(DiffItem("value", s, cid, a.readback[cid], b.readback[cid],
                                        f"differs by more than {tolerance}"))
                    clean = False
            ta, tb = a.elapsed_s, b.elapsed_s
            if max(ta, tb) >= time_floor_s and min(ta, tb) * time_factor < max(ta, tb):
                mis.append(DiffItem("timing", s, "", round(ta, 4), round(tb, 4),
                                    f"ratio beyond x{time_factor}"))
                clean = False
            compared += clean
        elif a.outcome.ok and not b.outcome.ok:
            if b.outcome.category == "harness":
                inc.append(DiffItem("incomparable", s, "", "ok", "harness", b.outcome.message))
            else:
                reg.append(DiffItem("regression", s, "", "ok", b.outcome.category, b.outcome.message))
        elif b.outcome.ok and not a.outcome.ok:
            if a.outcome.category == "harness":
                inc.append(DiffItem("incomparable", s, "", "harness", "ok", a.outcome.message))
            else:
                fix.append(DiffItem("fix", s, "", a.outcome.category, "ok", a.outcome.message))
        elif a.outcome.category != b.outcome.category:
            mis.append(DiffItem("outcome", s, "", a.outcome.category, b.outcome.category,
                                "both fail with different categories"))
    return DiffReport(len(cs), compared, tuple(reg), tuple(fix), tuple(mis), tuple(inc))
