"""Single seam between the GUI and the other owners' modules.

Everything is imported lazily and results are normalised, so a missing/changed backend
module degrades to a friendly message instead of a crash. No business logic lives here
beyond tolerant adaptation and a few clearly-labelled local fallbacks.
"""
from __future__ import annotations

import dataclasses as dc
import importlib
import inspect as _inspect
import json
import os
import struct
import sys
from typing import Any, Callable

from ..types import (
    AdapterSpec, Case, ConstraintDef, ControlDef, Domain, ElementInfo, ErrorEvent, Project,
    Selector, StopConditions, TargetRef,
)


if False:  # pragma: no cover - never runs; lets PyInstaller's static analysis see modules imported by name below
    from .. import (constraints, controller, diffrun, domain, evidence, infer, planning, preflight, project, reduce,  # noqa: F401
                    scheduler, states, watchdog, worker)
    from ..adapters import mock as _a_mock, procwatch as _a_procwatch, resolve as _a_resolve, uia as _a_uia  # noqa: F401
    from ..strategies import (adaptive as _s_adaptive, boundary as _s_boundary, chaos as _s_chaos,  # noqa: F401
                              ordered_pairs as _s_ordered, state_cover as _s_state, twise as _s_twise)


class BackendMissing(RuntimeError):
    """A backend module/function is not available (yet)."""


def _mod(name: str):
    try:
        return importlib.import_module(f"overgrind.{name}")
    except ImportError as e:  # includes ModuleNotFoundError
        raise BackendMissing(f"The '{name}' component is not installed in this build ({e}).") from e


def _fn(mod: str, *names: str) -> Callable:
    m = _mod(mod)
    for n in names:
        f = getattr(m, n, None)
        if callable(f):
            return f
    raise BackendMissing(f"'{mod}' does not provide {' / '.join(names)}.")


def available(mod: str) -> bool:
    try:
        _mod(mod)
        return True
    except BackendMissing:
        return False


# ------------------------------------------------------------------ project
def load_project(path: str) -> Project:
    return _fn("project", "load")(path)


def save_project(p: Project, path: str) -> None:
    _fn("project", "save")(p, path)


def migrate_v1(path: str) -> Project:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return _fn("project", "migrate_v1")(data)


def validate(p: Project) -> list[str]:
    try:
        return list(_fn("project", "validate")(p))
    except BackendMissing:
        return _validate_fallback(p)


def _validate_fallback(p: Project) -> list[str]:
    """Only used if project.py is absent: the bare minimum so the UI stays honest."""
    out = []
    if not p.features:
        out.append("no features selected")
    return out


# ------------------------------------------------------------------ domains
def effective_domain(c: ControlDef) -> Domain:
    try:
        return _fn("domain", "effective")(c)
    except BackendMissing:
        return c.fuzz_domain or c.domain


def describe_domain(d: Domain | None) -> str:
    if d is None:
        return "unknown"
    k = d.kind
    if k == "boolean":
        return "on / off"
    if k in ("integer", "float"):
        lo = "?" if d.minimum is None else _num(d.minimum)
        hi = "?" if d.maximum is None else _num(d.maximum)
        step = f", step {_num(d.step)}" if d.step else ""
        return f"{lo} to {hi}{step}"
    if k == "choice":
        return ", ".join(d.options) if d.options else "(no choices)"
    if k == "text":
        return f"text up to {d.max_length if d.max_length is not None else '?'} characters"
    return k


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


# ------------------------------------------------------------------ selectors
@dc.dataclass
class SelectorReport:
    score: int                     # 0..100
    reason: str
    fixes: list[str]
    fallbacks: list[tuple[str, int | None]]    # (description, score)
    estimated: bool = False        # True when computed by the GUI-side fallback


def describe_selector(s: Selector) -> str:
    parts = []
    if s.auto_id:
        parts.append(f"ID '{s.auto_id}'")
    if s.title:
        parts.append(f"name '{s.title}'")
    if s.control_type:
        parts.append(s.control_type)
    if s.class_name:
        parts.append(f"class {s.class_name}")
    if s.path:
        parts.append("in " + " > ".join(s.path))
    if s.index is not None:
        parts.append(f"#{s.index + 1} of identical siblings")
    return ", ".join(parts) or "(empty address)"


def _fix_hints(s: Selector) -> list[str]:
    fixes = []
    if not s.auto_id:
        fixes.append("Ask the developer to give this element an AutomationId. It is the most stable way to find it.")
    if s.index is not None:
        fixes.append("This element is told apart only by its position among identical siblings. Give it a unique name or ID so adding or moving a control does not break it.")
    if not s.title and not s.auto_id:
        fixes.append("It has no name either, so it can only be found by type and position.")
    if s.title and not s.auto_id:
        fixes.append("It is found by its visible text; translations or renamed labels will break it.")
    return fixes


def selector_report(s: Selector) -> SelectorReport:
    score_reason: tuple[Any, str] | None = None
    try:
        raw = _fn("project", "selector_score")(s)
        score_reason = _normalise_score(raw)
    except BackendMissing:
        try:
            raw = _fn("domain", "selector_score")(s)
            score_reason = _normalise_score(raw)
        except BackendMissing:
            pass
    estimated = score_reason is None
    if estimated:
        score_reason = _local_score(s)
    score, reason = score_reason
    fallbacks: list[tuple[str, int | None]] = []
    try:
        raw_fb = None
        for mod in ("project", "domain"):
            try:
                raw_fb = _fn(mod, "selector_fallbacks")(s)
                break
            except BackendMissing:
                continue
        for item in raw_fb or []:
            fallbacks.append(_normalise_fallback(item))
    except Exception:
        pass
    fixes = _fix_hints(s)
    if score >= 90 or any(k in reason.lower() for k in ("hint", "ask developers", "add automationid")):
        fixes = []
    return SelectorReport(score, reason, fixes, fallbacks, estimated)


def _normalise_score(raw) -> tuple[int, str]:
    reason = ""
    val = raw
    if isinstance(raw, tuple) and raw:
        val = raw[0]
        reason = str(raw[1]) if len(raw) > 1 else ""
    elif isinstance(raw, dict):
        val = raw.get("score", 0); reason = str(raw.get("reason", ""))
    elif hasattr(raw, "score"):
        val = raw.score; reason = str(getattr(raw, "reason", ""))
    val = float(val)
    if 0.0 <= val <= 1.0:
        val *= 100
    return int(round(max(0.0, min(100.0, val)))), reason


def _normalise_fallback(item) -> tuple[str, int | None]:
    sel, sc = item, None
    if isinstance(item, tuple) and item:
        sel = item[0]
        if len(item) > 1 and isinstance(item[1], (int, float)):
            sc = _normalise_score(item[1])[0]
    if isinstance(sel, Selector):
        if sc is None:
            try:
                sc = _normalise_score(_fn("project", "selector_score")(sel))[0]
            except Exception:
                sc = None
        return describe_selector(sel), sc
    return str(sel), sc


def _local_score(s: Selector) -> tuple[int, str]:
    if s.auto_id:
        return 92, "It has an AutomationId, which rarely changes between builds."
    if s.title and s.control_type and s.path:
        return 62, "Found by name, type and the path of its parents. A renamed label or moved panel will break it."
    if s.title and s.control_type:
        return 48, "Found by visible name and type only. It may match several elements or break if the text changes."
    if s.index is not None:
        return 18, "Found only by its position among identical siblings. Any layout change will break it."
    return 30, "Found by type only. It will probably match the wrong element."


def score_label(score: int) -> tuple[str, str]:
    """(word, semantic colour) for a 0..100 score."""
    if score >= 75:
        return "Stable", "ok"
    if score >= 45:
        return "Fair", "warn"
    return "Fragile", "fail"


# ------------------------------------------------------------------ planning
def estimate(p: Project) -> dict:
    d = _fn("scheduler", "estimate")(p)
    return dict(d) if isinstance(d, dict) else {"text": str(d)}


def preview(p: Project, n: int = 20) -> list[Case]:
    return list(_fn("scheduler", "preview")(p, n))


# ------------------------------------------------------------------ adapters / runtime
def make_adapter(spec: AdapterSpec | None):
    """Build an adapter instance in-process (for Inspect). None -> real UIA adapter (Windows)."""
    if spec is None:
        if sys.platform != "win32":
            raise BackendMissing("Scanning a real Windows app needs Windows. Use --demo to explore the interface here.")
        return _mod("adapters.uia").UiaAdapter()
    mod, _, name = spec.factory.partition(":")
    obj = getattr(importlib.import_module(mod), name)
    return obj(**spec.kwargs)


def inspect_target(spec: AdapterSpec | None, target: TargetRef) -> list[ElementInfo]:
    ad = make_adapter(spec)
    try:
        ad.attach(target)
        return list(ad.inspect())
    finally:
        try:
            ad.detach(False)
        except Exception:
            pass


def resolve_spec(spec: AdapterSpec | None) -> AdapterSpec:
    """The adapter a real run uses: the given one (demo) or UIA on Windows."""
    if spec is not None:
        return spec
    if sys.platform == "win32":
        return AdapterSpec("overgrind.adapters.uia:UiaAdapter")
    raise BackendMissing("Testing a real program needs Windows. Use File > Open demo project to try the interface here.")


def new_controller(project: Project, spec: AdapterSpec, evidence_dir: str, stop: StopConditions | None = None):
    cls = _fn("controller", "RunController")
    params = _inspect.signature(cls).parameters
    kwargs = {}
    if stop is not None:
        for key in ("stop", "stop_conditions", "stop_when"):
            if key in params:
                kwargs[key] = stop
                break
    return cls(project, spec, evidence_dir, **kwargs)


def evidence_store(directory: str):
    return _mod("evidence").EvidenceStore(directory)


def run_preflight(project: Project, spec: AdapterSpec | None) -> list[dict]:
    """Trust Ledger rows: {element, status(ok|warn|fail), message, hint}. Slow: worker thread only."""
    f = _fn("preflight", "preflight")
    report = f(project, resolve_spec(spec))
    rows = []
    for it in report.items:
        st = {"info": "ok"}.get(it.status, it.status)
        bad = [c for c in it.checks if c.status in ("warn", "fail")]
        msg = "; ".join(f"{c.name}: {c.detail}" for c in bad) or "; ".join(c.detail for c in it.checks if c.detail) or "Looks fine."
        if it.tier and st == "ok":
            msg += f" (found by {it.tier}, stability {it.score}/100)"
        rows.append({"element": f"{it.kind} {it.id}", "status": st if st in ("ok", "warn", "fail") else "warn",
                     "message": msg, "hint": "; ".join(it.hints), "kind": it.kind, "id": it.id,
                     "fixes": [fix_dict(fx) for fx in (getattr(it, "fixes", None) or [])]})
    return rows


def fix_dict(fx) -> dict:
    """A structured fix action (preflight.FixAction or dict) as plain data: {id, kind, target, value, label}."""
    if isinstance(fx, dict):
        d = dict(fx)
    else:
        d = {k: getattr(fx, k, None) for k in ("id", "kind", "target", "value", "label")}
    d.setdefault("label", "")
    return d


FIX_WORDS = {
    "mark_dangerous": "Mark as dangerous", "adopt_default": "Use the value we saw as the default",
    "narrow_domain": "Narrow the range", "skip_control": "Skip this control", "set_run": "Change a plan setting",
    "acknowledge": "I understand the risk", "set_selector": "Use the closest element",
}


def fix_button_text(fx: dict) -> str:
    """Plain-words button caption for a fix action (the backend label is kept as the tooltip)."""
    base = FIX_WORDS.get(fx.get("kind", ""), fx.get("label") or "Fix")
    kind, v = fx.get("kind"), fx.get("value")
    if kind == "narrow_domain" and isinstance(v, dict) and "minimum" in v and "maximum" in v:
        return f"Narrow the range to {_num(v['minimum'])}..{_num(v['maximum'])}"
    if kind == "set_run" and fx.get("label"):
        return fx["label"][:1].upper() + fx["label"][1:]
    return base


def apply_fix(project: Project, fx: dict) -> Project:
    """The project with one structured fix applied (pure; mirrors preflight.apply for a single accepted action)."""
    kind, target, value = fx.get("kind"), fx.get("target"), fx.get("value")
    if kind == "mark_dangerous":
        return dc.replace(project, features=tuple(dc.replace(f, dangerous=True) if f.id == target else f for f in project.features))
    if kind in ("adopt_default", "narrow_domain", "skip_control", "set_selector"):
        out = []
        for c in project.controls:
            if c.id == target:
                if kind == "adopt_default":
                    c = dc.replace(c, default=value)
                elif kind == "skip_control":
                    c = dc.replace(c, skip=True)
                elif kind == "narrow_domain":
                    base = c.fuzz_domain or c.domain
                    v = value or {}
                    c = dc.replace(c, fuzz_domain=dc.replace(base, minimum=v.get("minimum", base.minimum), maximum=v.get("maximum", base.maximum)))
                elif kind == "set_selector" and isinstance(value, dict):
                    c = dc.replace(c, selector=_selector_from(value, c.selector))
            out.append(c)
        feats = project.features
        if kind == "set_selector" and isinstance(value, dict):
            feats = tuple(dc.replace(f, selector=_selector_from(value, f.selector)) if f.id == target else f for f in project.features)
        return dc.replace(project, controls=tuple(out), features=feats)
    if kind == "set_run" and target and hasattr(project.run, target):
        return dc.replace(project, run=dc.replace(project.run, **{target: value}))
    if kind == "acknowledge":
        try:
            return _fn("project", "acknowledge_unknown")(project, project.name)
        except BackendMissing:
            return project
    return project


def _selector_from(d: dict, fallback: Selector) -> Selector:
    names = {f.name for f in dc.fields(Selector)}
    kw = {k: (tuple(v) if k == "path" and isinstance(v, list) else v) for k, v in d.items() if k in names}
    return dc.replace(fallback, **kw) if kw else fallback


def suggest_dependencies(project: Project, spec: AdapterSpec | None, rejected: dict | None = None, cancel=None,
                         progress=None) -> list[dict]:
    """Observe the app (read-only probing, defaults restored) and return proposals:
    {proposal, text, confidence, evidence, contradictions, q, impact, status, fingerprint}. Slow: worker thread only."""
    infer = _mod("infer")
    ad = make_adapter(spec)
    try:
        ad.attach(project.target)
        stat = getattr(infer, "infer_statistical", None)
        if callable(stat):
            kw = {}
            params = _inspect.signature(stat).parameters
            for k, v in (("rejected", rejected), ("cancel", cancel), ("progress", progress)):
                if v is not None and k in params:
                    kw[k] = v
            props = stat(project, ad, **kw)
        else:
            props = _legacy_infer(infer, ad, project)
    finally:
        try:
            ad.detach(False)
        except Exception:
            pass
    return [proposal_row(p) for p in props]


def proposal_row(p) -> dict:
    return {"proposal": p, "text": p.text or f"{p.kind}: {p.subject} {p.features}", "confidence": p.confidence,
            "evidence": p.evidence, "contradictions": getattr(p, "contradictions", 0), "q": getattr(p, "q", None),
            "impact": dict(getattr(p, "impact", None) or {}), "status": getattr(p, "status", "pending"),
            "fingerprint": getattr(p, "fingerprint", "")}


def _legacy_infer(infer, ad, project):
    present = {_sel_key(e.selector) for e in ad.inspect()}

    def probe(adapter, proj):
        now = {_sel_key(e.selector) for e in adapter.inspect()}
        flags = {}
        for x in list(proj.features) + list(proj.controls):
            flags[x.id] = {"enabled": True, "visible": _sel_key(x.selector) in now or not now}
        return flags
    obs = infer.gather_observations(ad, project, probe)
    return infer.infer_dependencies(obs, project)


def _sel_key(s: Selector) -> tuple:
    return (s.auto_id, s.title, s.control_type, s.path, s.index)


def describe_constraint(c: ConstraintDef, project: Project) -> str:
    names = {f.id: (f.name or f.id) for f in project.features}
    n = lambda i: names.get(i, i)  # noqa: E731
    if c.kind == "mutex":
        return "Never turn these on together: " + ", ".join(n(i) for i in c.features)
    if c.kind == "requires":
        return f"{', '.join(n(i) for i in c.features)} needs {', '.join(n(i) for i in c.needs)} to be on first"
    if c.kind == "forbid":
        return "Never use this exact combination: " + ", ".join(f"{n(k)} = {v}" for k, v in c.assignment.items())
    return c.kind


def reduce_event(project: Project, spec: AdapterSpec, event: ErrorEvent, store=None, cancel=None, on_progress=None) -> ErrorEvent:
    """Replay and shrink. Passes cancel/on_progress/store only when this build of Reducer accepts them."""
    R = _fn("reduce", "Reducer")
    params = _inspect.signature(R).parameters
    kw = {k: v for k, v in (("store", store), ("cancel", cancel), ("on_progress", on_progress)) if v is not None and k in params}
    red = R(project, spec, **kw)
    try:
        return red.reduce(event)
    finally:
        try:
            red.close()
        except Exception:
            pass


def reducer_supports_callbacks() -> bool:
    try:
        params = _inspect.signature(_fn("reduce", "Reducer")).parameters
    except BackendMissing:
        return False
    return "cancel" in params and "on_progress" in params


def export_issue_text(event: ErrorEvent, project: Project | None = None) -> str:
    """Ready-to-paste issue: reduce.export_issue when this build has it, else the older recipe."""
    try:
        f = _fn("reduce", "export_issue")
    except BackendMissing:
        return export_recipe(event, project)
    try:
        return str(f(event, "md", project=project))
    except Exception:
        return export_recipe(event, project)


def reliability_counts(event: ErrorEvent) -> tuple[int, int] | None:
    """(reproduced k, runs n) from the event, or None when it was never replayed."""
    env = event.env or {}
    rel = env.get("reliability") if isinstance(env, dict) else None
    if isinstance(rel, dict) and rel.get("n"):
        return int(rel.get("r", 0)), int(rel["n"])
    c = (event.reduced or {}).get("confirm") if event.reduced else None
    if isinstance(c, dict) and c.get("runs"):
        return int(c.get("reproduced", 0)), int(c["runs"])
    return None


def step_diff(original: list[tuple], reduced: list[tuple]) -> tuple[list[tuple[str, tuple]], list[tuple[str, tuple]]]:
    """Side-by-side diff of two action lists (tuples). Returns (left, right) rows of (tag, item), tag in
    'same' | 'removed' (left only) | 'gap' (blank on the right, aligned with a removed row)."""
    import difflib
    left: list[tuple[str, tuple]] = []
    right: list[tuple[str, tuple]] = []
    sm = difflib.SequenceMatcher(a=original, b=reduced, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for a, b in zip(original[i1:i2], reduced[j1:j2]):
                left.append(("same", a)); right.append(("same", b))
        else:
            for a in original[i1:i2]:
                left.append(("removed", a)); right.append(("gap", ()))
            for b in reduced[j1:j2]:               # a changed value: shown on the right as changed
                left.append(("gap", ())); right.append(("changed", b))
    return left, right


def export_recipe(event: ErrorEvent, project: Project | None = None) -> str:
    try:
        res = _fn("reduce", "export_recipe")(event)
    except BackendMissing:
        return _recipe_fallback(event)
    return str(res[0] if isinstance(res, tuple) else res)


def _recipe_fallback(e: ErrorEvent) -> str:
    lines = [f"## {e.category}: {e.message}", "", f"- Signature: `{e.signature}`", f"- Seen: {e.count} time(s), reliability: {e.reliability}", "",
             "### State", *[f"- {k} = {v}" for k, v in e.case.state.items()], "", "### Values",
             *[f"- {k} = {v}" for k, v in e.case.values.items()], "", "### Last steps",
             *[f"{i + 1}. {a.kind} {a.target} {'' if a.value is None else a.value}" for i, a in enumerate(e.history[-15:])]]
    return "\n".join(lines)


def exe_factory(spec: AdapterSpec, exe_path: str) -> Callable[[], Any]:
    """Adapter factory that launches a specific EXE (so two builds can be compared with one project)."""
    def make():
        ad = make_adapter(spec)
        ad.attach(TargetRef(kind="exe", exe_path=exe_path))
        return ad
    return make


def differential(project: Project, a: AdapterSpec, b: AdapterSpec, n_cases: int = 20, seed: int = 1,
                 factory_a=None, factory_b=None):
    return _fn("diffrun", "differential")(project, a, b, n_cases=n_cases, seed=seed,
                                          factory_a=factory_a, factory_b=factory_b)


def normalise_diff(rep) -> dict[str, list[str]]:
    def line(i) -> str:
        tgt = f" '{i.target}'" if i.target else ""
        return f"Case {i.case_seq}{tgt}: build A = {i.a!r}, build B = {i.b!r}. {i.detail}".strip()
    return {
        "regressions": [line(i) for i in rep.regressions], "fixes": [line(i) for i in rep.fixes],
        "mismatches": [line(i) for i in rep.mismatches], "incomparable": [line(i) for i in rep.incomparable],
        "summary": f"{rep.n_cases} cases, {rep.compared} fully comparable and identical",
        "markdown": rep.to_markdown(),
    }


# ------------------------------------------------------------------ processes / files
def pe_arch(path: str) -> str:
    """'64-bit' / '32-bit' / 'unknown' by reading the PE header (pure Python)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
            if head[:2] != b"MZ":
                return "unknown"
            off = struct.unpack_from("<I", head, 0x3C)[0]
            fh.seek(off)
            sig = fh.read(6)
            if sig[:4] != b"PE\0\0":
                return "unknown"
            machine = struct.unpack_from("<H", sig, 4)[0]
        return {0x8664: "64-bit", 0x14C: "32-bit", 0xAA64: "64-bit (ARM)"}.get(machine, "unknown")
    except OSError:
        return "unknown"


DEMO_WINDOWS = [
    {"pid": 4120, "process": "demo_app.exe", "title": "Demo App (sample window)", "path": r"C:\Demo\demo_app.exe", "arch": "64-bit"},
    {"pid": 5532, "process": "notepad.exe", "title": "Untitled - Notepad (sample)", "path": r"C:\Windows\System32\notepad.exe", "arch": "64-bit"},
    {"pid": 7788, "process": "legacy_tool.exe", "title": "Legacy Tool 3.1 (sample)", "path": r"C:\Program Files (x86)\Legacy\legacy_tool.exe", "arch": "32-bit"},
]


_SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"})


def window_listable(visible: bool, cloaked: bool, owned: bool, toolwindow: bool, title: str, pid: int, own_pid: int) -> bool:
    """Which top-level windows a tester may pick: real, visible, titled, app-level windows of other processes.
    Pure so it can be tested off Windows."""
    return bool(visible and not cloaked and not owned and not toolwindow and (title or "").strip() and pid and pid != own_pid)


def list_windows() -> list[dict]:
    """Visible top-level windows. Windows: ctypes EnumWindows (raises OSError if enumeration fails: a list of
    pretend windows would be worse than an honest error). Elsewhere: a demo list."""
    if sys.platform != "win32":
        return [dict(w) for w in DEMO_WINDOWS]
    return _list_windows_win32()


def _list_windows_win32() -> list[dict]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes
    # Every function is declared: ctypes otherwise passes/returns C int, which truncates 64-bit HWND/HANDLE values.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    except (OSError, AttributeError):
        dwmapi = None
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsWow64Process.restype = wintypes.BOOL
    PROCESS_QUERY_LIMITED_INFORMATION, GW_OWNER, GWL_EXSTYLE, WS_EX_TOOLWINDOW, DWMWA_CLOAKED = 0x1000, 4, -20, 0x80, 14
    me = os.getpid()
    out: list[dict] = []
    procs: dict[int, tuple[str, str]] = {}

    def proc_info(pid: int) -> tuple[str, str]:
        if pid in procs:
            return procs[pid]
        path, arch = "", "unknown"
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            try:
                buf = ctypes.create_unicode_buffer(32768)          # long-path safe
                size = wintypes.DWORD(len(buf))
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    path = buf.value
                wow = wintypes.BOOL(False)
                if kernel32.IsWow64Process(h, ctypes.byref(wow)):
                    arch = "32-bit" if wow.value else "64-bit"
            finally:
                kernel32.CloseHandle(h)
        if path:
            exact = pe_arch(path)
            if exact != "unknown":
                arch = exact
        procs[pid] = (path, arch)
        return procs[pid]

    def cloaked(hwnd) -> bool:
        if dwmapi is None:
            return False
        v = wintypes.DWORD(0)
        return dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)) == 0 and v.value != 0

    def cb(hwnd, _lp):
        try:
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0 or not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            buf = ctypes.create_unicode_buffer(n + 1)                # UTF-16 text; n counts UTF-16 units
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if not window_listable(True, cloaked(hwnd), bool(user32.GetWindow(hwnd, GW_OWNER)),
                                   bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW), buf.value, pid.value, me):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value in _SHELL_CLASSES:          # desktop ("Program Manager"), taskbar: never a test target
                return True
            path, arch = proc_info(pid.value)
            out.append({"pid": int(pid.value), "process": os.path.basename(path) or f"PID {pid.value}", "title": buf.value, "path": path, "arch": arch})
        except Exception:  # noqa: BLE001 - one odd window must not end the enumeration
            pass
        return True

    cb_ref = enum_proc(cb)                 # keep a reference for the duration of the call
    if not user32.EnumWindows(cb_ref, 0):
        raise OSError(ctypes.get_last_error(), "Windows could not list the open windows")
    return sorted(out, key=lambda w: (w["process"].lower(), w["title"].lower()))


# ------------------------------------------------------------------ coverage detail (Atlas)
MAX_RANKED_STATES = 4096


def state_rank_table(project: Project, n: int) -> list[dict[str, str]]:
    """rank -> {feature id: state} for the first `n` valid states (scheduler.unrank_state). Raises BackendMissing/ValueError
    when the space cannot be unranked. Pure CPU: call from a worker thread for large n."""
    un = _fn("scheduler", "unrank_state")
    return [dict(un(project, r)) for r in range(n)]


def visits_from_ranks(table: list[dict[str, str]], counts, shown: set[tuple[str, str]] | None = None) -> dict[tuple[str, str], int]:
    """Per (feature, state) visit totals from per-rank state counts."""
    out: dict[tuple[str, str], int] = {}
    for st, n in zip(table, counts):
        if not n:
            continue
        for fid, s in st.items():
            k = (fid, str(s))
            if shown is None or k in shown:
                out[k] = out.get(k, 0) + int(n)
    return out


def pair_coverage(table: list[dict[str, str]], counts, fids: list[str], states_of: dict[str, tuple[str, ...]]
                  ) -> dict[tuple[str, str], tuple[int, int]]:
    """For each feature pair (a, b) with a before b in `fids`: (covered, possible) value pairs, where 'possible' are
    the pairs that occur in at least one valid state and 'covered' those seen in a visited state."""
    poss: dict[tuple[str, str], set] = {}
    seen: dict[tuple[str, str], set] = {}
    for st, n in zip(table, list(counts) + [0] * (len(table) - len(counts))):
        for i, a in enumerate(fids):
            if a not in st:
                continue
            for b in fids[i + 1:]:
                if b not in st:
                    continue
                v = (str(st[a]), str(st[b]))
                poss.setdefault((a, b), set()).add(v)
                if n:
                    seen.setdefault((a, b), set()).add(v)
    return {k: (len(seen.get(k, ())), len(p)) for k, p in poss.items()}


def why_not_complete(cv) -> list[str]:
    """Plain-words reasons the coverage is below 100%, from whatever the Coverage object provides (older builds: just the gap)."""
    out = [str(x) for x in (getattr(cv, "why_not_complete", ()) or ())]
    have = " ".join(out).lower()

    def add(n, text, key):
        if n and key not in have:
            out.append(text.format(n=n))
    total, seen = getattr(cv, "states_total", None), getattr(cv, "states_visited", 0)
    if total and seen < total:
        out.append(f"{total - seen:,} of {total:,} feature states have not been visited yet.")
    elif total is None:
        out.append("The number of feature states is too large to count, so a percentage cannot be given.")
    add(getattr(cv, "unreachable", 0), "{n:,} state(s) could not be reached because setting them failed repeatedly; they are left out of the total.", "unreachable")
    add(getattr(cv, "failed_to_apply", 0), "{n:,} test case(s) could not be applied to the app and were not counted.", "could not be applied")
    add(getattr(cv, "unverified", 0), "{n:,} visit(s) were counted as 'applied' but the app's read-back did not confirm them.", "read-back")
    add(getattr(cv, "infeasible", 0), "{n:,} combination(s) are impossible because of your rules and are not counted.", "impossible")
    add(getattr(cv, "unknown", 0), "{n:,} combination(s) could not be proved possible or impossible and are still counted.", "could not be proved")
    ev = getattr(cv, "evidence", "")
    if ev in ("applied", "attempted"):
        out.append("Visits were only attempted/applied, not read back from the app, so they are not proof the state was reached." if ev == "attempted"
                   else "Visits were applied but not read back, so the app may not have shown that state.")
    return out


# ------------------------------------------------------------------ picking on screen / overlays
def overlay_supported() -> tuple[bool, str]:
    """(ok, why not). Click-through highlight windows and screen picking are Windows-only: elsewhere the buttons are
    disabled with this sentence as their tooltip."""
    if sys.platform != "win32":
        return False, "Picking on screen and the highlight outline only work on Windows, where the program being tested runs."
    return True, ""


def hit_test(elements: list[ElementInfo], x: float, y: float) -> int | None:
    """Index of the innermost (smallest-area) scanned element whose bounds contain the screen point, else None.
    Works on the scan result, so it needs no extra access to the app."""
    best, best_area = None, None
    for i, e in enumerate(elements):
        b = e.bounds
        if not b or len(b) < 4:
            continue
        bx, by, bw, bh = b[:4]
        if bw <= 0 or bh <= 0:
            continue
        if bx <= x < bx + bw and by <= y < by + bh:
            area = bw * bh
            if best_area is None or area <= best_area:
                best, best_area = i, area
    return best


# ------------------------------------------------------------------ compare builds: control mapping review
def map_controls(project: Project, elems_a: list[ElementInfo], elems_b: list[ElementInfo]) -> list[dict]:
    """One row per control and feature of the project: how well its element in build A matches an element in build B.
    {id, kind, name, a, b, score, status: mapped|review|unmatched, b_index}. Uses selectors.similarity when this build has it."""
    sim = None
    try:
        sim = _fn("selectors", "similarity")
    except BackendMissing:
        pass
    rows = []
    for kind, items in (("feature", project.features), ("control", project.controls)):
        for it in items:
            sel = it.selector
            a_el = next((e for e in elems_a if e.selector == sel), None)
            src = a_el or sel
            best, second, bi = 0.0, 0.0, None
            for j, e in enumerate(elems_b):
                if not sel.control_type or e.control_type == sel.control_type or (a_el and e.control_type == a_el.control_type):
                    s = float(sim(src, e)) if sim else _local_similarity(sel, e)
                    if s > best:
                        best, second, bi = s, best, j
                    elif s > second:
                        second = s
            status = "unmatched" if bi is None or best < 0.45 else ("mapped" if best >= 0.85 and best - second >= 0.15 else "review")
            nm = getattr(it, "name", "") or it.id
            rows.append({"id": it.id, "kind": kind, "name": nm, "a": describe_selector(sel),
                         "b": (elems_b[bi].name or describe_selector(elems_b[bi].selector)) if bi is not None else "",
                         "b_id": elems_b[bi].selector.auto_id if bi is not None else "",
                         "score": round(best, 2), "status": status, "b_index": bi})
    return rows


def _local_similarity(s: Selector, e: ElementInfo) -> float:
    import difflib
    t = e.selector
    parts = []
    if s.auto_id or t.auto_id:
        parts.append(1.0 if s.auto_id == t.auto_id else difflib.SequenceMatcher(None, s.auto_id, t.auto_id).ratio() * 0.8)
    if s.title or t.title or e.name:
        parts.append(difflib.SequenceMatcher(None, s.title.lower(), (t.title or e.name).lower()).ratio())
    parts.append(1.0 if s.control_type == t.control_type else 0.0)
    return sum(parts) / len(parts)


def id_map_from_review(rows: list[dict], decisions: dict[str, str]) -> dict[str, str | None]:
    """A-id -> B-id for diffrun's id_map. decisions[id] in {'accept', 'ignore', 'override:<b id>'}; undecided 'mapped' rows
    are accepted, undecided 'review'/'unmatched' rows are ignored (never guess)."""
    out: dict[str, str | None] = {}
    for r in rows:
        d = decisions.get(r["id"]) or ("accept" if r["status"] == "mapped" else "ignore")
        if d == "accept":
            out[r["id"]] = r["b_id"] or r["id"] if r["b_index"] is not None else None
        elif d.startswith("override:"):
            out[r["id"]] = d.split(":", 1)[1] or None
        else:
            out[r["id"]] = None
    return out
