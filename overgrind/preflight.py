"""Trust Ledger: a safe dry run that tells the tester, BEFORE a long run, which parts of the project
the automation can actually drive.

Per feature/control it reports whether the selector resolves uniquely (and via which fallback tier),
whether the element is enabled, whether writing the default value reads back identically, and
whether `reset()` works, with a verdict ok/warn/fail and copy-pastable fix hints. It only ever
writes DEFAULT values and never invokes actions or touches features flagged `dangerous`.
`preflight()` runs in a separate spawned process (the caller never touches the GUI target).
"""
from __future__ import annotations

import dataclasses
import difflib
import json
import math
import multiprocessing
import os
import random
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import danger as _danger
from . import domain as _dom
from .adapters import resolve as R
from .registry import resolve_callable
from .types import (
    AdapterSpec, ControlDef, ElementInfo, FeatureDef, HarnessError, Project, Selector,
)

_RANK = {"ok": 0, "info": 0, "warn": 1, "fail": 2}
LOW_SCORE = 50


@dataclass(frozen=True)
class FixAction:
    """Structured fix the GUI renders as a button (data only; `apply()` performs the accepted ones).

    kind: mark_dangerous | adopt_default | narrow_domain | skip_control | set_run | acknowledge | set_selector
    target: feature/control id (or run-config field for set_run); value: the suggested value (JSON-able)."""
    id: str
    kind: str
    target: str
    value: Any = None
    label: str = ""


@dataclass
class Check:
    name: str
    status: str            # ok | info | warn | fail
    detail: str = ""
    code: str = ""         # spec check id P01..P14 ("" for legacy checks)


@dataclass
class PreflightItem:
    kind: str              # target | feature | control | reset
    id: str
    status: str = "ok"
    tier: str = ""
    score: int = 0
    checks: list[Check] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    fixes: list[FixAction] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "", code: str = "") -> None:
        self.checks.append(Check(name, status, detail, code))
        if _RANK[status] > _RANK[self.status]:
            self.status = status

    def fix(self, kind: str, target: str, value: Any = None, label: str = "") -> FixAction:
        fa = FixAction(f"{self.kind}:{self.id}:{kind}", kind, target, value, label or kind)
        if fa not in self.fixes:
            self.fixes.append(fa)
        return fa


@dataclass
class PreflightReport:
    items: list[PreflightItem] = field(default_factory=list)
    incomplete: bool = False          # budget exhausted before every check ran
    target_hash: str = ""             # sha256[:8] of the target exe ('' unknown)
    started: float = 0.0              # wall-clock (time.time) seconds
    finished: float = 0.0
    unknown_ids: list[str] = field(default_factory=list)
    acknowledged: bool = False        # the 'I know' box for unknown-confidence facts
    ops: int = 0
    summary_text: str = ""

    @property
    def verdict(self) -> str:
        worst = max((_RANK[i.status] for i in self.items), default=0)
        return {0: "ok", 1: "warn", 2: "fail"}[worst]

    @property
    def can_start(self) -> bool:
        """No fail, not truncated by the budget, and unknown facts (if any) acknowledged."""
        return (self.verdict != "fail" and not self.incomplete
                and (not self.unknown_ids or self.acknowledged))

    def fixes(self) -> list[FixAction]:
        return [f for i in self.items for f in i.fixes]

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "can_start": self.can_start, "incomplete": self.incomplete,
                "target_hash": self.target_hash, "started": self.started, "finished": self.finished,
                "unknown_ids": list(self.unknown_ids), "acknowledged": self.acknowledged,
                "items": [dataclasses.asdict(i) for i in self.items]}

    def summary(self) -> str:
        n = {"ok": 0, "warn": 0, "fail": 0}
        for i in self.items:
            n[i.status] += 1
        lines = [f"preflight: {self.verdict.upper()} ({n['ok']} ok, {n['warn']} warn, {n['fail']} fail)"
                 + (" - incomplete" if self.incomplete else "")]
        for i in self.items:
            if i.status != "ok":
                lines.append(f"  [{i.status}] {i.kind} {i.id}: " +
                             "; ".join(c.detail for c in i.checks if c.status in ("warn", "fail")))
                lines += [f"      fix: {h}" for h in i.hints]
        return "\n".join(lines)


def sel_json(sel: Selector) -> str:
    d = {k: (list(v) if isinstance(v, tuple) else v) for k, v in dataclasses.asdict(sel).items()
         if v not in ("", (), None)}
    return json.dumps(d, ensure_ascii=False)


def _score(sel: Selector, elements: list[ElementInfo]) -> tuple[int, str]:
    try:
        from . import project as P
        return P.selector_score(sel)
    except Exception:
        return R.stability_score(sel, elements)


def _closest(sel: Selector, elements: list[ElementInfo]) -> ElementInfo | None:
    want = sel.auto_id or sel.title
    if not want:
        return None
    pool = [e for e in elements if not sel.control_type or e.control_type == sel.control_type] or elements
    best, ratio = None, 0.0
    for e in pool:
        for cand in (e.selector.auto_id, e.name):
            r = difflib.SequenceMatcher(None, want.lower(), cand.lower()).ratio() if cand else 0.0
            if r > ratio:
                best, ratio = e, r
    return best if ratio >= 0.5 else None


def _locate(item: PreflightItem, sel: Selector, elements: list[ElementInfo], label: str = "selector") -> bool:
    res = R.resolve(sel, elements)
    item.tier = res.tier
    item.score, why = _score(sel, elements)
    if not res.found:
        if res.ambiguous:
            names = ", ".join(f"#{i}:{elements[i].name or elements[i].selector.auto_id!r}"
                              for i in res.candidates[:6])
            item.add(label, "fail", f"{label} matches {len(res.candidates)} elements (ambiguous): {names}",
                     "P02")
            item.evidence["candidates"] = [elements[i].name or elements[i].selector.auto_id
                                           for i in res.candidates[:6]]
            first = elements[res.candidates[0]] if res.candidates else None
            if first is not None:
                better = R.suggest_selector(first, elements)
                item.hints.append(f'make it unique, e.g. "selector": {sel_json(better)} '
                                  f'or add "index": 0..{len(res.candidates) - 1} / a longer "path"')
        else:
            item.add(label, "fail", f"{label} resolves to nothing ({R.explain(sel, elements)})", "P02")
            near = _closest(sel, elements)
            if near is not None:
                better = R.suggest_selector(near, elements)
                item.fix("set_selector", item.id, dataclasses.asdict(better), "use the closest element")
                item.hints.append(f'closest element is "{near.name}" ({near.control_type}); '
                                  f'try "selector": {sel_json(better)}')
            else:
                item.hints.append("open the Inspector, re-pick the element and replace the selector; "
                                  "the target may be showing a different page/tab")
        return False
    if res.ambiguous:
        item.add(label, "warn", f"resolved via {res.tier} but several elements matched earlier tiers "
                 f"(disambiguated by index)", "P02")
    elif res.tier != "auto_id" and sel.auto_id:
        item.add(label, "warn", f"AutomationId did not match; healed via {res.tier} (selector is drifting)", "P02")
        item.hints.append(f'update to "selector": {sel_json(R.suggest_selector(elements[res.index], elements))}')
    else:
        item.add(label, "ok", f"unique via {res.tier}", "P02")
        if res.tier in ("title+type", "class+index"):      # tier >= 3: fragile (spec P02)
            item.add(label, "warn", f"resolved only via fragile tier {res.tier}", "P02")
    if item.score < LOW_SCORE:
        item.add("stability", "warn", f"fragile selector ({item.score}/100): {why}")
        item.hints.append(f'prefer an AutomationId: "selector": {sel_json(R.suggest_selector(elements[res.index], elements))}')
    else:
        item.add("stability", "ok", f"{item.score}/100")
    return True


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        return abs(float(a) - float(b)) <= 1e-6 + 1e-6 * abs(float(a))
    except (TypeError, ValueError):
        return str(a) == str(b)


class _Ctx:
    """Shared state of one preflight run: clock/budget, op latency samples, op counter."""

    def __init__(self, clock: Callable[[], float], budget_s: float) -> None:
        self.clock, self.deadline = clock, clock() + budget_s
        self.lat: list[float] = []
        self.ops = 0

    def over(self) -> bool:
        return self.clock() > self.deadline

    def call(self, fn: Any, *a: Any) -> Any:
        t = self.clock()
        try:
            return fn(*a)
        finally:
            self.lat.append(self.clock() - t)
            self.ops += 1


def run_preflight(project: Project, adapter_spec: AdapterSpec, *, budget_s: float = 120.0,
                  progress: Callable[[str], None] | None = None, clock: Callable[[], float] = time.monotonic,
                  evidence_dir: str | None = None, ack_unknown: bool | None = None,
                  adapter: Any = None, safe_to_probe: tuple[str, ...] = ()) -> PreflightReport:
    """In-process dry run (use `preflight()` unless you ARE the worker/test).

    Spec U2 P01-P14 that need no real GUI. `can_start` on the report is the verdict. Never invokes
    action features unless listed in `safe_to_probe`; baseline is restored in `finally`."""
    from . import project as P
    ctx = _Ctx(clock, budget_s)
    rep = PreflightReport(started=time.time(), target_hash=P.exe_hash(None, project))
    rep.unknown_ids = P.unknown_ids(project)
    rep.acknowledged = bool(ack_unknown) if ack_unknown is not None else P.unknown_acknowledged(project)

    def note(msg: str) -> None:
        if progress is not None:
            try:
                progress(msg)
            except Exception:       # noqa: BLE001  GUI callback must not break the run
                pass

    tgt = PreflightItem("target", project.target.exe_path or project.target.title_regex
                        or str(project.target.pid or project.target.process_name))
    rep.items.append(tgt)
    _ledger_item(rep, project, P)
    try:
        if adapter is None:
            adapter = resolve_callable(adapter_spec.factory)(**dict(adapter_spec.kwargs))
        adapter.attach(project.target)
        elements = adapter.inspect()
        h = adapter.health()
        tgt.add("attach", "ok" if h.status == "ok" else "fail", f"health={h.status} {h.detail}".strip(), "P01")
        tgt.add("inspect", "ok" if elements else "fail", f"{len(elements)} elements visible")
        _identity(tgt, adapter, rep)
    except Exception as exc:    # noqa: BLE001
        tgt.add("attach", "fail", f"{type(exc).__name__}: {exc}", "P01")
        tgt.hints.append("check the target selection; if the app runs elevated, start Overgrind elevated too")
        _close(adapter)
        rep.finished = time.time()
        return rep
    try:
        try:
            adapter.reset(project)                      # P05 needs a known baseline
        except Exception:                               # noqa: BLE001
            pass
        for f in project.features:
            if f.enabled:
                if ctx.over():
                    rep.incomplete = True
                    break
                note(f"feature {f.id}")
                rep.items.append(_guarded("feature", f.id, _check_feature, adapter, f, elements, ctx,
                                          f.id in safe_to_probe))
        for c in project.controls:
            if not c.skip and not rep.incomplete:
                if ctx.over():
                    rep.incomplete = True
                    break
                note(f"control {c.id}")
                rep.items.append(_guarded("control", c.id, _check_control, adapter, c, elements, ctx))
        if not rep.incomplete:
            rep.items.append(_guarded("reset", "reset()", _check_reset, adapter, project))
            rep.items.append(_guarded("timing", "latency", _check_timing, project, ctx))
            rep.items.append(_guarded("stability", "selectors", _check_stability, adapter, project, elements))
            rep.items.append(_guarded("danger", "neighbours", _check_neighbours, project, elements))
            rep.items.append(_guarded("modal", "dialogs", _check_modal, adapter))
            if evidence_dir:
                rep.items.append(_guarded("evidence", str(evidence_dir), _check_evidence, adapter, evidence_dir))
        else:
            it = PreflightItem("budget", "preflight")
            it.add("budget", "fail", f"incomplete: {budget_s:g}s budget exhausted before every check ran", "P14")
            rep.items.append(it)
    finally:
        try:                                    # probes wrote values: always leave the target at its baseline
            adapter.reset(project)
        except Exception:                       # noqa: BLE001
            pass
        _close(adapter)
        rep.ops = ctx.ops
        rep.finished = time.time()
    return rep


def _ledger_item(rep: PreflightReport, project: Any, P: Any) -> None:
    it = PreflightItem("ledger", "confidence")
    cnt = P.confidence_counts(project)
    it.evidence = {k: v for k, v in cnt.items()}
    if rep.unknown_ids:
        if rep.acknowledged:
            it.add("unknown", "warn", f"{len(rep.unknown_ids)} unknown-confidence item(s) acknowledged: "
                   + ", ".join(rep.unknown_ids[:6]))
        else:
            it.add("unknown", "fail", f"{len(rep.unknown_ids)} item(s) of unknown type/confidence: "
                   + ", ".join(rep.unknown_ids[:6]) + "; acknowledge them before starting")
            it.fix("acknowledge", project.name, True, "I know: acknowledge unknown items")
    dem = P.demoted_ids(project)
    if dem:
        it.add("demoted", "warn", f"{len(dem)} fact(s) demoted from verified (build changed or stale): "
               + ", ".join(dem[:6]) + "; this preflight re-verifies them")
    if not it.checks:
        it.add("ledger", "ok", "all facts have a known confidence")
    rep.items.append(it)


def _identity(item: PreflightItem, adapter: Any, rep: PreflightReport) -> None:
    pid = None
    try:
        pid = adapter.pid()
    except Exception:           # noqa: BLE001
        pass
    if pid is not None:
        from .adapters import procwatch
        alive = procwatch.is_alive(pid)
        item.add("pid", "ok" if alive else "fail", f"pid {pid} {'alive' if alive else 'is not running'}", "P01")
    el = getattr(adapter, "is_elevated", None)
    if el is not None:
        try:
            if el():
                item.add("integrity", "fail", "target is elevated and Overgrind is not (UIPI blocks input)", "P01")
                item.hints.append("start Overgrind elevated too")
        except Exception:       # noqa: BLE001
            pass
    item.add("hash", "ok" if rep.target_hash else "info",
             f"exe hash {rep.target_hash}" if rep.target_hash else "exe hash unavailable", "P01")


def _owner_check(item: PreflightItem, adapter: Any, sel: Selector) -> None:
    fn = getattr(adapter, "owner_pid", None)
    if fn is None:
        return
    try:
        owner, mine = fn(sel), adapter.pid()
    except Exception:           # noqa: BLE001
        return
    if owner is not None and mine is not None and owner != mine:
        item.add("owner", "fail", f"element belongs to pid {owner}, not the target ({mine})", "P03")


def _guarded(kind: str, ident: str, fn: Any, *args: Any) -> PreflightItem:
    """One misbehaving adapter call must fail ITS item, not abort the whole preflight."""
    try:
        return fn(*args)
    except Exception as exc:    # noqa: BLE001
        it = PreflightItem(kind, ident)
        it.add("probe", "fail", f"unexpected {type(exc).__name__}: {exc}")
        it.hints.append("the adapter raised something other than HarnessError; check the target is still running")
        return it


def _close(adapter: Any) -> None:
    if adapter is not None:
        try:
            adapter.detach(kill=False)
        except Exception:
            pass


def _enabled_check(adapter: Any, item: PreflightItem, sel: Selector) -> None:
    fn = getattr(adapter, "selector_enabled", None)
    if fn is None:
        item.add("enabled", "info", "adapter cannot report enabled state")
        return
    try:
        en = fn(sel)
    except Exception as exc:
        item.add("enabled", "warn", f"could not read enabled state: {exc}")
        return
    if en is None:
        item.add("enabled", "info", "enabled state unknown")
    elif en:
        item.add("enabled", "ok", "enabled")
    else:
        item.add("enabled", "warn", "element is disabled in the current app state; it may need a prerequisite")
        item.hints.append("set the prerequisite feature/page first or add it to the control's scope")


_NEED_PATTERN = {"boolean": ("Toggle",), "integer": ("RangeValue", "Value"), "float": ("RangeValue", "Value"),
                 "text": ("Value",), "choice": ("Value", "Selection", "ExpandCollapse", "SelectionItem")}


def _pattern_fit(item: PreflightItem, c: ControlDef, elements: list[ElementInfo]) -> None:
    r = R.resolve(c.selector, elements)
    if not r.found:
        return
    pats = elements[r.index].patterns
    need = _NEED_PATTERN.get(c.kind)
    if not pats or not need:
        item.add("patterns", "info", "element reports no patterns; cannot check type fit", "P04")
    elif any(p in pats for p in need):
        item.add("patterns", "ok", f"supports {', '.join(p for p in pats if p in need)}", "P04")
    else:
        item.add("patterns", "fail", f"{c.kind} control needs one of {'/'.join(need)} but element has "
                 f"{', '.join(pats)}", "P04")
        item.fix("skip_control", c.id, True, "skip this control")


def _probe_values(c: ControlDef) -> list[Any]:
    """Low / mid / high of the FUZZ domain (spec P06), deduplicated."""
    d = _dom.effective(c)
    if d.kind == "boolean":
        vals: list[Any] = [False, True]
    elif d.kind == "choice":
        o = list(d.options)
        vals = [o[0], o[len(o) // 2], o[-1]] if o else []
    elif d.kind == "text":
        n = d.max_length if d.max_length is not None else 8
        vals = ["", "a" * max(1, n // 2), "a" * n]
    else:
        lo = d.minimum if d.minimum is not None else 0
        hi = d.maximum if d.maximum is not None else lo + 100
        mid = (lo + hi) // 2 if d.kind == "integer" else (lo + hi) / 2
        vals = [lo, mid, hi]
    out: list[Any] = []
    for v in vals:
        if v not in out:
            out.append(v)
    return out[:3]


def _check_feature(adapter: Any, f: FeatureDef, elements: list[ElementInfo], ctx: _Ctx | None = None,
                   safe: bool = False) -> PreflightItem:
    ctx = ctx or _Ctx(time.monotonic, 1e9)
    it = PreflightItem("feature", f.id)
    sels = [("selector", f.selector)] if f.kind != "multistate" or not f.state_selectors else []
    sels += [(f"selector[{k}]", s) for k, s in f.state_selectors.items()]
    ok = all([_locate(it, s, elements, label) for label, s in sels]) if sels else True
    if ok:
        for _, s in sels:
            _owner_check(it, adapter, s)
    if not f.dangerous:
        hit = _danger.scan_feature(f)
        if hit is not None:
            # spec P12: 'block' items fail until excluded or flagged; other tiers are a warning
            it.add("danger", "fail" if hit.tier == "block" else "warn",
                   f"{f.id!r} looks dangerous ({hit.reason()}); it would be exercised thousands of times", "P12")
            it.hints.append(f'mark "{f.id}" dangerous (it is then never clicked) or disable it; run on a '
                            f'disposable VM/profile with test data (keyword check is a heuristic)')
            it.fix("mark_dangerous", f.id, True, "mark dangerous")
    if f.dangerous:
        it.add("safety", "info", "flagged dangerous: never clicked, not tested here")
        return it
    if not ok:
        return it
    if f.kind == "action":
        if safe:
            ctx.call(adapter.invoke, f)
            it.add("action", "ok", "invoked (flagged safe_to_probe)")
        else:
            it.add("action", "info", "actions are not invoked during preflight")
        return it
    _enabled_check(adapter, it, f.selector)
    getter = getattr(adapter, "get_feature_state", None)
    try:
        states = [s for s in f.states if s != f.default_state][:5] + [f.default_state]
        bad = []
        for st in states:                              # P07: every state round-trips, default last
            ctx.call(adapter.set_feature_state, f, st)
            if getter is not None:
                got = ctx.call(getter, f)
                if not _same(got, st):
                    bad.append((st, got))
        if getter is None:
            it.add("read-back", "info", "adapter has no get_feature_state; write succeeded", "P07")
        elif not bad:
            it.add("read-back", "ok", f"default {f.default_state!r} reads back", "P07")
        else:
            st, got = bad[0]
            it.add("read-back", "fail", f"set {st!r} but read {got!r}", "P07")
            it.hints.append(f'check "default_state"/"states" of {f.id}; the app reports {got!r}')
    except HarnessError as exc:
        it.add("set-default", "fail", str(exc), "P07")
        it.hints.append("the element exists but cannot be driven via UIA patterns; use a per-state selector "
                        "or a different feature kind")
    return it


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_control(adapter: Any, c: ControlDef, elements: list[ElementInfo], ctx: _Ctx | None = None) -> PreflightItem:
    ctx = ctx or _Ctx(time.monotonic, 1e9)
    it = PreflightItem("control", c.id)
    if not _locate(it, c.selector, elements):
        return it
    _owner_check(it, adapter, c.selector)
    _pattern_fit(it, c, elements)
    _enabled_check(adapter, it, c.selector)
    if c.read_only or c.default is None:
        it.add("read-back", "info", "read-only or no default: not written")
        return it
    show = (lambda v: "***") if c.sensitive else repr
    try:
        base = ctx.call(adapter.get_control, c)                       # P05 baseline read
        if _same(base, c.default):
            it.add("baseline", "ok", f"baseline {show(base)} equals the declared default", "P05")
        else:
            it.add("baseline", "warn", f"app shows {show(base)} but the declared default is {show(c.default)}", "P05")
            if not c.sensitive:
                it.fix("adopt_default", c.id, base, "adopt observed default")
        results: list[tuple[Any, Any]] = []
        try:
            for v in _probe_values(c)[:3] + [c.default]:               # P06 low/mid/high then the default
                ctx.call(adapter.set_control, c, v)
                results.append((v, ctx.call(adapter.get_control, c)))
        finally:
            try:
                adapter.set_control(c, c.default)
            except Exception:                                          # noqa: BLE001
                pass
        probes = results[:-1]                      # low/mid/high; the last entry is the default write (restore)
        okn = sum(_same(g, v) for v, g in probes)
        if okn == len(probes):
            it.add("read-back", "ok", f"default {show(c.default)} reads back; {len(probes)} values round-trip", "P06")
        elif okn == 0:
            v, g = probes[-1]
            it.add("read-back", "fail", f"set {show(v)} but read {show(g)}", "P06")
            it.hints.append(f'the app clamps/ignores this value; fix "default" for {c.id} or narrow its "fuzz_domain"')
            it.fix("skip_control", c.id, True, "skip this control")
        else:
            wrong = [(v, g) for v, g in probes if not _same(g, v)]
            v, g = wrong[0]
            it.add("read-back", "warn", f"set {show(v)} but read {show(g)} (clamped)", "P06")
            gots = [g for _, g in results if _num(g)]
            d = _dom.effective(c)
            if gots and c.kind in ("integer", "float") and not c.sensitive:
                lo, hi = min(gots), max(gots)
                lo = max(lo, d.minimum) if d.minimum is not None else lo
                hi = min(hi, d.maximum) if d.maximum is not None else hi
                it.fix("narrow_domain", c.id, {"minimum": lo, "maximum": hi}, f"narrow domain to {lo}..{hi}")
                it.hints.append(f"narrow the fuzz domain of {c.id} to {lo}..{hi}")
        it.evidence["readback"] = [[show(v), show(g)] for v, g in results]
    except HarnessError as exc:
        it.add("set-default", "fail", str(exc), "P06")
        it.hints.append(f'mark {c.id} "skip": true or choose a different element (value patterns missing)')
        it.fix("skip_control", c.id, True, "skip this control")
    return it


def _check_reset(adapter: Any, project: Project) -> PreflightItem:
    """P08: perturb 3 random controls+features, reset, verify EVERY declared default; twice."""
    it = PreflightItem("reset", "reset()")
    try:
        adapter.reset(project)
        h = adapter.health()
        it.add("reset", "ok" if h.status == "ok" else "fail", f"health after reset: {h.status} {h.detail}".strip(),
               "P08")
    except Exception as exc:    # noqa: BLE001
        it.add("reset", "fail", f"{type(exc).__name__}: {exc}", "P08")
        it.hints.append('set run.reset_policy to "never" or fix the defaults flagged above')
        return it
    getter = getattr(adapter, "get_feature_state", None)
    ctrls = [c for c in project.controls if not (c.skip or c.read_only) and c.default is not None]
    feats = [f for f in project.features if f.enabled and not f.dangerous and f.kind != "action"
             and getter is not None]
    pool: list[Any] = ctrls + feats
    if not pool:
        return it
    rng = random.Random(0)
    residue: list[str] = []
    try:
        for _ in range(2):
            for x in rng.sample(pool, min(3, len(pool))):
                if isinstance(x, ControlDef):
                    cand = [v for v in _dom.boundary_values(_dom.effective(x)) if not _same(v, x.default)]
                    if cand:
                        adapter.set_control(x, cand[0])
                else:
                    cand = [s for s in x.states if s != x.default_state]
                    if cand:
                        adapter.set_feature_state(x, cand[0])
            adapter.reset(project)
            for c in ctrls:
                if not _same(adapter.get_control(c), c.default) and c.id not in residue:
                    residue.append(c.id)
            for f in feats:
                if not _same(getter(f), f.default_state) and f.id not in residue:
                    residue.append(f.id)
    except HarnessError as exc:
        it.add("residue", "fail", f"probe failed: {exc}", "P08")
        return it
    if residue:
        it.add("residue", "fail", "reset() left residue in: " + ", ".join(residue)
               + " (cases cannot claim to be independent)", "P08")
        it.evidence["residue"] = residue
        it.hints.append("fix reset() / the declared defaults, or set run.reset_policy to a policy that restarts")
        for rid in residue:
            it.fix("skip_control", rid, True, f"skip {rid}")
    else:
        it.add("residue", "ok", "all declared defaults restored after reset (2 rounds)", "P08")
    return it


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(math.ceil(q * len(s))) - 1)]


def _check_timing(project: Project, ctx: _Ctx) -> PreflightItem:
    it = PreflightItem("timing", "latency")
    if len(ctx.lat) < 20:
        it.add("latency", "info", f"only {len(ctx.lat)} timed ops (< 20); no statistics", "P10")
        return it
    p50, p95 = _pct(ctx.lat, 0.5) * 1000, _pct(ctx.lat, 0.95) * 1000
    it.evidence = {"p50_ms": round(p50, 3), "p95_ms": round(p95, 3), "n": len(ctx.lat)}
    need = math.ceil(5 * p95)
    if project.run.op_timeout_ms < 5 * p95:
        it.add("latency", "warn", f"op_timeout_ms={project.run.op_timeout_ms} < 5 x p95 ({p95:.1f} ms)", "P10")
        it.fix("set_run", "op_timeout_ms", need, f"set op_timeout_ms to {need}")
    else:
        it.add("latency", "ok", f"p50 {p50:.1f} ms, p95 {p95:.1f} ms", "P10")
    return it


def _check_stability(adapter: Any, project: Project, first: list[ElementInfo]) -> PreflightItem:
    """P11: re-inspect and re-resolve every selector; flips are fragile selectors."""
    it = PreflightItem("stability", "selectors")
    second = adapter.inspect()
    flips = []
    sels = [(f.id, f.selector) for f in project.features if f.enabled and f.kind != "action"] + \
           [(c.id, c.selector) for c in project.controls if not c.skip]
    for ident, s in sels:
        a, b = R.resolve(s, first), R.resolve(s, second)
        if a.found != b.found or (a.found and (a.tier != b.tier or
                                               first[a.index].name != second[b.index].name)):
            flips.append(ident)
    if flips:
        it.add("re-resolve", "warn", "selectors resolved differently on a second look: " + ", ".join(flips[:8]), "P11")
    else:
        it.add("re-resolve", "ok", f"{len(sels)} selectors resolve identically twice", "P11")
    return it


def _check_neighbours(project: Project, elements: list[ElementInfo]) -> PreflightItem:
    """P12 (reachable neighbours): a selected element next to a block-tier sibling is a mis-click risk."""
    it = PreflightItem("danger", "neighbours")
    hits = _danger.scan_elements(elements)
    selected = set()
    for s in [f.selector for f in project.features if f.enabled and not f.dangerous and f.kind != "action"] + \
             [c.selector for c in project.controls if not c.skip]:
        r = R.resolve(s, elements)
        if r.found:
            selected.add(r.index)
    risky = []
    for i in selected:
        par = elements[i].parent
        for j, f in hits.items():
            if j != i and j not in selected and f.tier == "block" and elements[j].parent == par and par is not None:
                risky.append(f"{elements[i].name!r} next to {elements[j].name!r}")
    if risky:
        it.add("neighbours", "warn", "dangerous siblings in the same container: " + "; ".join(risky[:5]), "P12")
    else:
        it.add("neighbours", "ok", "no dangerous siblings next to selected elements", "P12")
    return it


def _check_modal(adapter: Any) -> PreflightItem:
    it = PreflightItem("modal", "dialogs")
    h = adapter.health()
    if h.status == "modal":
        it.add("modal", "warn", f"a modal dialog appeared during probes: {h.detail}", "P13")
        it.hints.append("set a dismiss policy for dialogs before running")
    else:
        it.add("modal", "ok", "no modal dialog appeared", "P13")
    return it


def _check_evidence(adapter: Any, directory: str) -> PreflightItem:
    it = PreflightItem("evidence", directory)
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, delete=True):
            pass
        free = shutil.disk_usage(directory).free
        if free < (1 << 30):
            it.add("space", "warn", f"only {free / (1 << 20):.0f} MiB free (>= 1 GiB recommended)", "P14")
        else:
            it.add("space", "ok", f"{free / (1 << 30):.1f} GiB free", "P14")
    except OSError as exc:
        it.add("writable", "fail", f"evidence folder not writable: {exc}", "P14")
        return it
    shot = os.path.join(directory, ".preflight-shot.png")
    try:
        okshot = bool(adapter.screenshot(shot))
    except Exception:           # noqa: BLE001
        okshot = False
    finally:
        try:
            os.unlink(shot)
        except OSError:
            pass
    it.add("screenshot", "ok" if okshot else "warn", "screenshot works" if okshot else "screenshot failed", "P14")
    return it


# ------------------------------------------------------------------ apply (the only place provenance is rewritten)
def apply(project: Project, report: PreflightReport, accept: Any = ()) -> Project:
    """New frozen Project with the fixes in `accept` (FixAction ids) applied and passing facts marked verified.

    Facts never silently upgrade: nothing changes unless the caller runs this. A fact becomes `verified`
    when its item is ok, or warn with an accepted fix; failed items never upgrade."""
    from . import project as P
    from .types import Provenance
    acc = set(accept)
    by_id = {(i.kind, i.id): i for i in report.items}
    stamp = P._now_iso()

    def note(item: PreflightItem) -> str:
        return P.make_note(t=stamp, n=report.ops, tier=item.tier, rb="ok", build=report.target_hash)

    def good(item: PreflightItem | None) -> bool:
        if item is None or item.status == "fail":
            return False
        return item.status == "ok" or any(f.id in acc for f in item.fixes)

    feats = []
    for f in project.features:
        it = by_id.get(("feature", f.id))
        for fx in (it.fixes if it else []):
            if fx.id in acc and fx.kind == "mark_dangerous":
                f = dataclasses.replace(f, dangerous=True)
        if good(it) and not f.dangerous:
            f = dataclasses.replace(f, provenance=Provenance("uia", "verified", note(it)))
        feats.append(f)
    ctrls = []
    for c in project.controls:
        it = by_id.get(("control", c.id))
        for fx in (it.fixes if it else []):
            if fx.id not in acc:
                continue
            if fx.kind == "adopt_default":
                c = dataclasses.replace(c, default=fx.value)
            elif fx.kind == "narrow_domain":
                v = fx.value or {}
                base = c.fuzz_domain or c.domain
                c = dataclasses.replace(c, fuzz_domain=dataclasses.replace(
                    base, minimum=v.get("minimum", base.minimum), maximum=v.get("maximum", base.maximum)))
            elif fx.kind == "skip_control":
                c = dataclasses.replace(c, skip=True)
        if good(it) and not c.skip:
            c = dataclasses.replace(c, provenance=Provenance("uia", "verified", note(it)))
        ctrls.append(c)
    out = dataclasses.replace(project, features=tuple(feats), controls=tuple(ctrls))
    run = out.run
    for fx in report.fixes():
        if fx.id in acc and fx.kind == "set_run" and hasattr(run, fx.target):
            run = dataclasses.replace(run, **{fx.target: fx.value})
    if run is not out.run:
        out = dataclasses.replace(out, run=run)
    if any(fx.id in acc and fx.kind == "acknowledge" for fx in report.fixes()):
        out = P.acknowledge_unknown(out, project.name)
    return out


def _child(project: Project, spec: AdapterSpec, conn: Any) -> None:
    try:
        conn.send(("ok", run_preflight(project, spec)))
    except BaseException as exc:    # noqa: BLE001
        conn.send(("err", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def preflight(project: Project, adapter_spec: AdapterSpec, isolated: bool = True,
              timeout_s: float = 90.0) -> PreflightReport:
    """Dry run; `isolated=True` (default) drives the target from a spawned child with a deadline."""
    if not isolated:
        return run_preflight(project, adapter_spec)
    from .adapters import procwatch
    ctx = multiprocessing.get_context("spawn")
    rx, tx = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(project, adapter_spec, tx), daemon=True)
    proc.start()
    tx.close()
    rep = PreflightReport()
    try:
        if rx.poll(timeout_s):
            kind, payload = rx.recv()
            if kind == "ok":
                return payload
            msg = payload
        else:
            msg = f"preflight did not finish within {timeout_s:g}s (automation wedged?)"
    except (EOFError, OSError):
        msg = "preflight process died unexpectedly"
    finally:
        if proc.is_alive():
            procwatch.kill_tree(proc.pid)
            proc.join(1.0)
    it = PreflightItem("target", "preflight")
    it.add("run", "fail", msg)
    rep.items.append(it)
    return rep
