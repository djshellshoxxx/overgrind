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
import multiprocessing
from dataclasses import dataclass, field
from typing import Any

from .adapters import resolve as R
from .types import (
    AdapterSpec, ControlDef, ElementInfo, FeatureDef, HarnessError, Project, Selector,
)

_RANK = {"ok": 0, "info": 0, "warn": 1, "fail": 2}
LOW_SCORE = 50


@dataclass
class Check:
    name: str
    status: str            # ok | info | warn | fail
    detail: str = ""


@dataclass
class PreflightItem:
    kind: str              # target | feature | control | reset
    id: str
    status: str = "ok"
    tier: str = ""
    score: int = 0
    checks: list[Check] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))
        if _RANK[status] > _RANK[self.status]:
            self.status = status


@dataclass
class PreflightReport:
    items: list[PreflightItem] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        worst = max((_RANK[i.status] for i in self.items), default=0)
        return {0: "ok", 1: "warn", 2: "fail"}[worst]

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "items": [dataclasses.asdict(i) for i in self.items]}

    def summary(self) -> str:
        n = {"ok": 0, "warn": 0, "fail": 0}
        for i in self.items:
            n[i.status] += 1
        lines = [f"preflight: {self.verdict.upper()} ({n['ok']} ok, {n['warn']} warn, {n['fail']} fail)"]
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
            item.add(label, "fail", f"{label} matches {len(res.candidates)} elements (ambiguous)")
            first = elements[res.candidates[0]] if res.candidates else None
            if first is not None:
                better = R.suggest_selector(first, elements)
                item.hints.append(f'make it unique, e.g. "selector": {sel_json(better)} '
                                  f'or add "index": 0..{len(res.candidates) - 1} / a longer "path"')
        else:
            item.add(label, "fail", f"{label} resolves to nothing ({R.explain(sel, elements)})")
            near = _closest(sel, elements)
            if near is not None:
                better = R.suggest_selector(near, elements)
                item.hints.append(f'closest element is "{near.name}" ({near.control_type}); '
                                  f'try "selector": {sel_json(better)}')
            else:
                item.hints.append("open the Inspector, re-pick the element and replace the selector; "
                                  "the target may be showing a different page/tab")
        return False
    if res.ambiguous:
        item.add(label, "warn", f"resolved via {res.tier} but several elements matched earlier tiers")
    elif res.tier != "auto_id" and sel.auto_id:
        item.add(label, "warn", f"AutomationId did not match; healed via {res.tier} (selector is drifting)")
        item.hints.append(f'update to "selector": {sel_json(R.suggest_selector(elements[res.index], elements))}')
    else:
        item.add(label, "ok", f"unique via {res.tier}")
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


def run_preflight(project: Project, adapter_spec: AdapterSpec) -> PreflightReport:
    """In-process dry run (use `preflight()` unless you ARE the worker/test)."""
    from .worker import load_callable
    rep = PreflightReport()
    tgt = PreflightItem("target", project.target.exe_path or project.target.title_regex
                        or str(project.target.pid or project.target.process_name))
    rep.items.append(tgt)
    adapter = None
    try:
        adapter = load_callable(adapter_spec.factory)(**dict(adapter_spec.kwargs))
        adapter.attach(project.target)
        elements = adapter.inspect()
        h = adapter.health()
        tgt.add("attach", "ok" if h.status == "ok" else "fail", f"health={h.status} {h.detail}".strip())
        tgt.add("inspect", "ok" if elements else "fail", f"{len(elements)} elements visible")
    except Exception as exc:    # noqa: BLE001
        tgt.add("attach", "fail", f"{type(exc).__name__}: {exc}")
        tgt.hints.append("check the target selection; if the app runs elevated, start Overgrind elevated too")
        _close(adapter)
        return rep
    try:
        for f in project.features:
            if f.enabled:
                rep.items.append(_check_feature(adapter, f, elements))
        for c in project.controls:
            if not c.skip:
                rep.items.append(_check_control(adapter, c, elements))
        rep.items.append(_check_reset(adapter, project))
    finally:
        _close(adapter)
    return rep


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


def _check_feature(adapter: Any, f: FeatureDef, elements: list[ElementInfo]) -> PreflightItem:
    it = PreflightItem("feature", f.id)
    sels = [("selector", f.selector)] if f.kind != "multistate" or not f.state_selectors else []
    sels += [(f"selector[{k}]", s) for k, s in f.state_selectors.items()]
    ok = all([_locate(it, s, elements, label) for label, s in sels]) if sels else True
    if f.dangerous:
        it.add("safety", "info", "flagged dangerous: never clicked, not tested here")
        return it
    if not ok:
        return it
    if f.kind == "action":
        it.add("action", "info", "actions are not invoked during preflight")
        return it
    _enabled_check(adapter, it, f.selector)
    try:
        adapter.set_feature_state(f, f.default_state)
        getter = getattr(adapter, "get_feature_state", None)
        if getter is None:
            it.add("read-back", "info", "adapter has no get_feature_state; write succeeded")
        else:
            got = getter(f)
            if _same(got, f.default_state):
                it.add("read-back", "ok", f"default {f.default_state!r} reads back")
            else:
                it.add("read-back", "fail", f"set {f.default_state!r} but read {got!r}")
                it.hints.append(f'check "default_state"/"states" of {f.id}; the app reports {got!r}')
    except HarnessError as exc:
        it.add("set-default", "fail", str(exc))
        it.hints.append("the element exists but cannot be driven via UIA patterns; use a per-state selector "
                        "or a different feature kind")
    return it


def _check_control(adapter: Any, c: ControlDef, elements: list[ElementInfo]) -> PreflightItem:
    it = PreflightItem("control", c.id)
    if not _locate(it, c.selector, elements):
        return it
    _enabled_check(adapter, it, c.selector)
    if c.read_only or c.default is None:
        it.add("read-back", "info", "read-only or no default: not written")
        return it
    try:
        adapter.set_control(c, c.default)
        got = adapter.get_control(c)
        if _same(got, c.default):
            it.add("read-back", "ok", f"default {c.default!r} reads back")
        else:
            it.add("read-back", "fail", f"set {c.default!r} but read {got!r}")
            it.hints.append(f'the app clamps/ignores this value; fix "default" for {c.id} or narrow its "fuzz_domain"')
    except HarnessError as exc:
        it.add("set-default", "fail", str(exc))
        it.hints.append(f'mark {c.id} "skip": true or choose a different element (value patterns missing)')
    return it


def _check_reset(adapter: Any, project: Project) -> PreflightItem:
    it = PreflightItem("reset", "reset()")
    try:
        adapter.reset(project)
        h = adapter.health()
        it.add("reset", "ok" if h.status == "ok" else "fail", f"health after reset: {h.status} {h.detail}".strip())
    except Exception as exc:    # noqa: BLE001
        it.add("reset", "fail", f"{type(exc).__name__}: {exc}")
        it.hints.append('set run.reset_policy to "never" or fix the defaults flagged above')
    return it


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
