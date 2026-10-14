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
                     "message": msg, "hint": "; ".join(it.hints)})
    return rows


def suggest_dependencies(project: Project, spec: AdapterSpec | None) -> list[dict]:
    """Observe the app (read-only probing, defaults restored) and return proposals:
    {proposal, text, confidence, evidence}. Slow: call from a worker thread."""
    infer = _mod("infer")
    ad = make_adapter(spec)
    try:
        ad.attach(project.target)
        present = {_sel_key(e.selector) for e in ad.inspect()}

        def probe(adapter, proj):
            now = {_sel_key(e.selector) for e in adapter.inspect()}
            flags = {}
            for x in list(proj.features) + list(proj.controls):
                flags[x.id] = {"enabled": True, "visible": _sel_key(x.selector) in now or not now}
            return flags
        obs = infer.gather_observations(ad, project, probe)
        props = infer.infer_dependencies(obs, project)
    finally:
        try:
            ad.detach(False)
        except Exception:
            pass
    return [{"proposal": p, "text": p.text or f"{p.kind}: {p.subject} {p.features}", "confidence": p.confidence,
             "evidence": p.evidence} for p in props]


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


def reduce_event(project: Project, spec: AdapterSpec, event: ErrorEvent) -> ErrorEvent:
    R = _fn("reduce", "Reducer")
    return R(project, spec).reduce(event)


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


def list_windows() -> list[dict]:
    """Visible top-level windows. Windows: ctypes EnumWindows. Elsewhere: a demo list."""
    if sys.platform != "win32":
        return [dict(w) for w in DEMO_WINDOWS]
    try:
        return _list_windows_win32()
    except Exception:
        return [dict(w) for w in DEMO_WINDOWS]


def _list_windows_win32() -> list[dict]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    out: list[dict] = []
    me = os.getpid()
    PROC_QUERY = 0x1000

    def proc_info(pid: int) -> tuple[str, str]:
        h = kernel32.OpenProcess(PROC_QUERY, False, pid)
        if not h:
            return "", "unknown"
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            path = buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
            wow = wintypes.BOOL(False)
            arch = "unknown"
            if kernel32.IsWow64Process(h, ctypes.byref(wow)):
                arch = "32-bit" if wow.value else "64-bit"
            return path, arch
        finally:
            kernel32.CloseHandle(h)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me:
            return True
        n = user32.GetWindowTextLengthW(hwnd) + 1
        buf = ctypes.create_unicode_buffer(n)
        user32.GetWindowTextW(hwnd, buf, n)
        path, arch = proc_info(pid.value)
        out.append({"pid": pid.value, "process": os.path.basename(path), "title": buf.value, "path": path, "arch": arch})
        return True

    user32.EnumWindows(cb, 0)
    return sorted(out, key=lambda w: (w["process"].lower(), w["title"].lower()))
