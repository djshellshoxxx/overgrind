"""Project <-> JSON (schema v2), v1 migration, validation, selector stability scoring."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import tempfile
import time
import types as _pytypes
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from . import danger as _danger
from . import domain as _dom
from .types import (SCHEMA_VERSION, ConstraintDef, ControlDef, Domain, FeatureDef,
                    Project, Provenance, RunConfig, Selector, TargetRef)

# ---------------------------------------------------------------- (de)serialisation
_NONE = type(None)


def to_dict(obj: Any) -> Any:
    """Dataclass (or nested value) -> JSON-able structure; fields in declaration order."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_dict(v) for k, v in obj.items()}
    return obj


def _conv(tp: Any, v: Any, where: str) -> Any:
    if tp is Any:
        return v
    origin = get_origin(tp)
    if origin is Union or origin is _pytypes.UnionType:
        args = get_args(tp)
        if v is None and _NONE in args:
            return None
        errors = []
        for a in args:
            if a is _NONE:
                continue
            try:
                return _conv(a, v, where)
            except ValueError as exc:
                errors.append(str(exc))
        raise ValueError(errors[0] if len(errors) == 1 else f"{where}: unexpected value {v!r}")
    if origin is tuple:
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"{where}: expected list")
        et = get_args(tp)[0]
        return tuple(_conv(et, x, f"{where}[{i}]") for i, x in enumerate(v))
    if origin is list:
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"{where}: expected list")
        et = get_args(tp)[0]
        return [_conv(et, x, f"{where}[{i}]") for i, x in enumerate(v)]
    if origin is dict:
        if not isinstance(v, dict):
            raise ValueError(f"{where}: expected object")
        kt, vt = get_args(tp)
        return {_conv(kt, k, where): _conv(vt, x, f"{where}.{k}") for k, x in v.items()}
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _from(tp, v, where)
    if tp is bool:
        ok = isinstance(v, bool)
    elif tp is int:
        ok = isinstance(v, int) and not isinstance(v, bool)
    elif tp is float:
        ok = isinstance(v, (int, float)) and not isinstance(v, bool)
    elif tp is str:
        ok = isinstance(v, str)
    elif tp is _NONE:
        ok = v is None
    else:
        ok = True
    if not ok:
        raise ValueError(f"{where}: expected {getattr(tp, '__name__', tp)}, got {v!r}")
    return v


def _from(cls: type, data: Any, where: str) -> Any:
    if not isinstance(data, dict):
        raise ValueError(f"{where}: expected object for {cls.__name__}")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - set(fields))
    if unknown:
        raise ValueError(f"{where}: unknown keys {unknown}")
    hints = get_type_hints(cls)
    kwargs = {}
    for name, f in fields.items():
        if name in data:
            kwargs[name] = _conv(hints[name], data[name], f"{where}.{name}")
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise ValueError(f"{where}: missing required key {name!r}")
    return cls(**kwargs)


def from_dict(cls: type, data: Any) -> Any:
    """Build any types.py dataclass from JSON data; unknown keys rejected, lists -> tuples."""
    obj = _from(cls, data, cls.__name__)
    if isinstance(obj, Project) and obj.schema_version != SCHEMA_VERSION:
        raise ValueError(f"Project: unsupported schema_version {obj.schema_version}")
    return obj


def project_to_dict(p: Project) -> dict[str, Any]:
    return to_dict(p)


def project_from_dict(data: dict[str, Any]) -> Project:
    return from_dict(Project, data)


def _canonical(p: Project) -> str:
    d = to_dict(p)
    d.pop("uuid", None)
    d.pop("name", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def project_hash(p: Project) -> str:
    """sha256 of canonical JSON, excluding name and uuid."""
    return hashlib.sha256(_canonical(p).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- files
def migrate_v1(d: dict[str, Any]) -> Project:
    """v1 manifest (title_regex/features/controls) -> v2 Project. Raises ValueError if invalid."""
    from .models import DEFAULT_ALPHABET, AppSpec   # v0.1 validator, imported lazily

    spec = AppSpec.from_dict(d)
    prov = Provenance(source="v1-import", confidence="manual", note="migrated from v1 manifest")

    def sel(m: dict[str, str]) -> Selector:
        return Selector(auto_id=m.get("auto_id", ""), title=m.get("title", ""),
                        control_type=m.get("control_type", ""))

    features = []
    for f in spec.features:
        if f.mode == "enable":
            features.append(FeatureDef(id=f.id, name=f.id, kind="toggle", selector=sel(f.selector),
                                       provenance=prov))
        else:
            features.append(FeatureDef(id=f.id, name=f.id, kind="action", states=(), default_state="",
                                       selector=sel(f.selector), provenance=prov))
    controls = []
    for c in spec.controls:
        dom = Domain(kind=c.kind, minimum=c.minimum, maximum=c.maximum, options=tuple(c.options),
                     max_length=c.max_length,
                     alphabet="" if c.kind != "text" or c.alphabet == DEFAULT_ALPHABET else c.alphabet)
        controls.append(ControlDef(id=c.id, kind=c.kind, selector=sel(c.selector), domain=dom,
                                   scope=tuple(c.requires_features), provenance=prov))
    name = d.get("name") if isinstance(d.get("name"), str) and d.get("name") else "Imported project"
    return Project(name=name, target=TargetRef(kind="window", title_regex=spec.title_regex),
                   features=tuple(features), controls=tuple(controls))


def load(path: str | os.PathLike) -> Project:
    """Read a project file; v1 manifests are migrated automatically (name = file stem)."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))   # Notepad writes a BOM
    if not isinstance(data, dict):
        raise ValueError("project file must contain a JSON object")
    version = data.get("schema_version")
    if version == SCHEMA_VERSION:
        return from_dict(Project, data)
    if version in (None, 1):
        p = migrate_v1({k: v for k, v in data.items() if k != "schema_version"})
        return dataclasses.replace(p, name=data["name"]) if data.get("name") else \
            dataclasses.replace(p, name=Path(path).stem)
    raise ValueError(f"unsupported schema_version {version!r}")


def replace_with_retry(src: str | os.PathLike, dst: str | os.PathLike, attempts: int = 8) -> None:
    """os.replace that rides out Windows sharing violations (antivirus / a reader holding the file)."""
    delay = 0.01
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def save(p: Project, path: str | os.PathLike) -> None:
    """Atomic write (temp file + os.replace), UTF-8."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_dict(p), indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix="." + target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- validation
_FEATURE_KINDS = ("toggle", "multistate", "action")
_CONSTRAINT_KINDS = ("mutex", "requires", "forbid")


def _domain_problems(label: str, d: Domain) -> list[str]:
    out: list[str] = []
    k = d.kind
    if k not in _dom.KINDS:
        return [f"{label}: unknown domain kind {k!r}"]
    if k in ("integer", "float"):
        lo, hi = d.minimum, d.maximum
        ok = True
        for nm, x in (("minimum", lo), ("maximum", hi)):
            if x is None:
                out.append(f"{label}: {nm} is required for {k}")
                ok = False
            elif isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
                out.append(f"{label}: {nm} must be a finite number (got {x!r})")
                ok = False
        if ok and lo > hi:
            out.append(f"{label}: minimum {lo} > maximum {hi}")
        if k == "integer" and ok and (lo != int(lo) or hi != int(hi)):
            out.append(f"{label}: integer bounds must be whole numbers")
        if d.step is not None:
            if isinstance(d.step, bool) or not isinstance(d.step, (int, float)) \
                    or not math.isfinite(d.step) or d.step <= 0:
                out.append(f"{label}: step must be > 0 (got {d.step!r})")
            elif k == "integer" and d.step != int(d.step):
                out.append(f"{label}: integer step must be whole")
    elif k == "choice":
        if not d.options:
            out.append(f"{label}: choice needs at least one option")
        elif len(set(d.options)) != len(d.options):
            out.append(f"{label}: duplicate options")
    elif k == "text":
        m = d.max_length
        if isinstance(m, bool) or not isinstance(m, int) or not 0 <= m <= 4096:
            out.append(f"{label}: text needs max_length between 0 and 4096 (got {m!r})")
    return out


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


_RESET_POLICIES = ("every_case", "interval", "never")
# strategy_options key -> (checker, human description)
_OPTION_RULES: dict[str, tuple[Any, str]] = {
    "t": (lambda v: _is_int(v) and 1 <= v <= 4, "an integer 1..4"),
    "buckets": (lambda v: _is_int(v) and 1 <= v <= 1000, "an integer 1..1000"),
    "tuple_cap": (lambda v: _is_int(v) and 1 <= v <= 2 ** 40, "a positive integer"),
    "scan_cap": (lambda v: _is_int(v) and v >= 1, "a positive integer"),
    "gain_cap": (lambda v: _is_int(v) and 1 <= v <= 10 ** 6, "a positive integer"),
    "budget": (lambda v: _is_int(v) and v >= 1, "a positive integer"),
    "candidates": (lambda v: _is_int(v) and 1 <= v <= 1000, "an integer 1..1000"),
    "explore": (lambda v: _is_num(v) and 0 <= v <= 1, "a number 0..1"),
    "failure_focus": (lambda v: _is_num(v) and 0 <= v <= 1, "a number 0..1"),
    "invalid_rate": (lambda v: _is_num(v) and 0 <= v <= 1, "a number 0..1"),
    "reset_rate": (lambda v: _is_num(v) and 0 <= v <= 1, "a number 0..1"),
}


def _run_problems(r: RunConfig) -> tuple[list[str], list[str]]:
    from .strategies import STRATEGY_NAMES
    errs: list[str] = []
    warns: list[str] = []
    if r.strategy not in STRATEGY_NAMES:
        errs.append(f"run.strategy {r.strategy!r} is unknown (choose one of {', '.join(STRATEGY_NAMES)})")
    if r.reset_policy not in _RESET_POLICIES:
        errs.append(f"run.reset_policy {r.reset_policy!r} must be one of {', '.join(_RESET_POLICIES)}")
    for name, lo, hi in (("seed", None, None), ("reset_interval", 1, None), ("settle_ms", 0, 3_600_000),
                         ("op_timeout_ms", 1, None), ("case_timeout_ms", 1, None),
                         ("hang_threshold_ms", 1, None), ("ring_size", 1, 1_000_000),
                         ("confirm_runs", 2, 1000)):
        v = getattr(r, name)
        if not _is_int(v):
            errs.append(f"run.{name} must be an integer (got {v!r})")
        elif (lo is not None and v < lo) or (hi is not None and v > hi):
            rng = f">= {lo}" if hi is None else f"between {lo} and {hi}"
            errs.append(f"run.{name} must be {rng} (got {v})")
    if not isinstance(r.strategy_options, dict):
        errs.append("run.strategy_options must be an object")
    else:
        for k, v in r.strategy_options.items():
            rule = _OPTION_RULES.get(k)
            if rule is None:
                warns.append(f"warning: run.strategy_options has an unknown key {k!r} (ignored)")
            elif not rule[0](v):
                errs.append(f"run.strategy_options[{k!r}] must be {rule[1]} (got {v!r})")
    ok = all(_is_int(getattr(r, n)) for n in ("hang_threshold_ms", "case_timeout_ms", "op_timeout_ms"))
    if ok and r.hang_threshold_ms > r.case_timeout_ms:
        warns.append(f"warning: run.hang_threshold_ms ({r.hang_threshold_ms}) exceeds case_timeout_ms "
                     f"({r.case_timeout_ms}); the watchdog will kill a slow case before it can be called a hang")
    if ok and r.op_timeout_ms > r.case_timeout_ms:
        warns.append(f"warning: run.op_timeout_ms ({r.op_timeout_ms}) exceeds case_timeout_ms ({r.case_timeout_ms})")
    return errs, warns


def _target_problems(t: TargetRef) -> tuple[list[str], list[str]]:
    errs: list[str] = []
    warns: list[str] = []
    if t.title_regex:
        try:
            re.compile(t.title_regex)
        except re.error as exc:
            errs.append(f"target.title_regex is not a valid regular expression: {exc}")
    if t.pid is not None and (not _is_int(t.pid) or t.pid <= 0):
        errs.append(f"target.pid must be a positive integer (got {t.pid!r})")
    if not (t.exe_path or t.title_regex or t.process_name or t.pid):
        warns.append("warning: target is empty (no exe_path, title_regex, process_name or pid); "
                     "nothing can be attached")
    return errs, warns


def validate(p: Project) -> list[str]:
    """Human-readable problems; entries starting with 'warning:' are non-blocking."""
    errs: list[str] = []
    warns: list[str] = []
    if p.schema_version != SCHEMA_VERSION:
        errs.append(f"unsupported schema_version {p.schema_version}")
    e, w = _run_problems(p.run)
    errs += e
    warns += w
    e, w = _target_problems(p.target)
    errs += e
    warns += w

    fids: dict[str, FeatureDef] = {}
    for f in p.features:
        if not f.id or not f.id.strip():
            errs.append("feature with empty id")
        if f.id in fids:
            errs.append(f"duplicate feature id {f.id!r}")
        fids[f.id] = f
        if f.kind not in _FEATURE_KINDS:
            errs.append(f"feature {f.id!r}: unknown kind {f.kind!r}")
        elif f.kind != "action":
            if len(f.states) < 2:
                errs.append(f"feature {f.id!r}: {f.kind} needs at least 2 states (has {len(f.states)})")
            if len(set(f.states)) != len(f.states):
                errs.append(f"feature {f.id!r}: duplicate states")
            if f.default_state not in f.states:
                errs.append(f"feature {f.id!r}: default_state {f.default_state!r} not in states {list(f.states)}")
            for s in f.state_selectors:
                if s not in f.states:
                    errs.append(f"feature {f.id!r}: state_selectors key {s!r} is not a state")
        if f.dangerous and f.enabled:
            warns.append(f"warning: feature {f.id!r} is marked dangerous and is enabled; "
                         f"it will be exercised unless you disable it or clear the flag")
        elif f.enabled and f.kind in _FEATURE_KINDS:
            hit = _danger.scan_feature(f)
            if hit is not None:
                warns.append(f"warning: feature {f.id!r} looks dangerous ({hit.reason()}); mark it dangerous "
                             f"(it is then never clicked) or disable it, and fuzz only a disposable VM/profile "
                             f"with test data. This keyword check is a heuristic, not a guarantee")
    if not any(f.enabled for f in p.features):
        errs.append("no enabled features")

    cids: set[str] = set()
    for c in p.controls:
        lbl = f"control {c.id!r}"
        if not c.id or not c.id.strip():
            errs.append("control with empty id")
        elif c.id in fids:
            warns.append(f"warning: id {c.id!r} is used by both a feature and a control; evidence and "
                         f"replay refer to targets by id, so this is ambiguous")
        if c.id in cids:
            errs.append(f"duplicate control id {c.id!r}")
        cids.add(c.id)
        if c.kind != c.domain.kind:
            errs.append(f"{lbl}: kind {c.kind!r} != domain kind {c.domain.kind!r}")
        for s in c.scope:
            if s not in fids:
                errs.append(f"{lbl}: scope references unknown feature {s!r}")
        dprob = _domain_problems(f"{lbl} domain", c.domain)
        errs += dprob
        eff = None if dprob else c.domain
        if c.fuzz_domain is not None:
            fprob = _domain_problems(f"{lbl} fuzz_domain", c.fuzz_domain)
            errs += fprob
            if not dprob and not fprob:
                eff = c.fuzz_domain
                for why in _dom.subset_reasons(c.fuzz_domain, c.domain):
                    errs.append(f"{lbl}: fuzz_domain is not a subset of domain ({why})")
            elif fprob:
                eff = None
        if c.default is not None and eff is not None:
            if not _dom.contains(eff, c.default, strict_step=False):
                errs.append(f"{lbl}: default {c.default!r} is outside the effective domain")

    for i, k in enumerate(p.constraints):
        lbl = f"constraint #{i + 1} ({k.kind})"
        if k.kind not in _CONSTRAINT_KINDS:
            errs.append(f"{lbl}: unknown kind")
            continue
        refs = list(k.features) + list(k.needs) + list(k.assignment)
        for r in refs:
            if r not in fids:
                errs.append(f"{lbl}: unknown feature {r!r}")
        if k.kind == "mutex" and len(k.features) < 2:
            errs.append(f"{lbl}: mutex needs at least 2 features")
        if k.kind == "requires" and (not k.features or not k.needs):
            errs.append(f"{lbl}: requires needs both features and needs")
        if k.kind == "forbid":
            if not k.assignment:
                errs.append(f"{lbl}: forbid needs a non-empty assignment")
            for fid, st in k.assignment.items():
                f = fids.get(fid)
                if f is None:
                    continue
                if f.kind == "action":
                    errs.append(f"{lbl}: feature {fid!r} is an action and has no states")
                elif st not in f.states:
                    errs.append(f"{lbl}: unknown state {st!r} for feature {fid!r}")
    return errs + warns


# ---------------------------------------------------------------- selector stability
_GUID = re.compile(r"^\{?[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}?$")
_HEX = re.compile(r"^(?:0x)?[0-9a-fA-F]{8,}$")
_CLASSNUM = re.compile(r"^[A-Za-z]+[_-]?\d+$")
_FRAMEWORK = re.compile(r"^(?:WindowsForms10\.|qt_|Afx:|ID_\d|window\d+$)", re.IGNORECASE)
_DATE = re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b|\b\d{1,2}:\d{2}\b|"
                   r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b",
                   re.IGNORECASE)
_ADD_ID = "ask developers to set a stable, human-readable AutomationId on this element"


def _auto_id_score(a: str) -> tuple[int, str]:
    if _GUID.match(a):
        return 30, f"AutomationId {a!r} looks like a GUID, so it likely changes per run; {_ADD_ID}"
    if a.isdigit():
        return 35, f"AutomationId {a!r} is purely numeric (runtime-assigned); {_ADD_ID}"
    if _HEX.match(a) and any(ch.isdigit() for ch in a):
        return 35, f"AutomationId {a!r} looks like a hex/hash id; {_ADD_ID}"
    if _FRAMEWORK.match(a):
        return 40, f"AutomationId {a!r} looks framework-generated; {_ADD_ID}"
    if _CLASSNUM.match(a):
        return 60, f"AutomationId {a!r} looks auto-numbered (class name + counter) and breaks when controls are reordered; {_ADD_ID}"
    if len(a) > 40:
        return 65, f"AutomationId is {len(a)} chars long, which usually means generated; {_ADD_ID}"
    if len(a) < 3:
        return 90, f"AutomationId {a!r} is very short; stable but easy to collide"
    return 100, f"AutomationId {a!r} is a stable identifier"


def _title_penalty(t: str) -> tuple[int, str]:
    if _DATE.search(t):
        return 25, "title contains a date/time"
    if any(ch.isdigit() for ch in t):
        return 15, "title contains digits (document counters and values change)"
    return 0, ""


def selector_score(s: Selector) -> tuple[int, str]:
    """Stability 0..100 and an actionable reason (the 'why is this locator fragile' feature)."""
    if s.auto_id:
        return _auto_id_score(s.auto_id)
    if s.path and s.control_type:
        score, note = 70, "located by window path + control type; moves if the layout changes"
        if any(any(ch.isdigit() for ch in seg) for seg in s.path):
            score -= 5
            note += "; a path segment contains digits"
        return score, f"{note}. Hint: add AutomationId for a stable locator"
    if s.title and s.control_type:
        pen, why = _title_penalty(s.title)
        note = "located by title + control type; breaks if the label is renamed or localised"
        if why:
            note += f"; {why}"
        if s.index is not None:
            pen += 5
        return 55 - pen, f"{note}. Hint: add AutomationId (or a path of ancestors) to avoid depending on the label"
    if s.title:
        pen, why = _title_penalty(s.title)
        return max(0, 40 - pen), ("located by title only, so any control type matches"
                                  + (f"; {why}" if why else "")
                                  + ". Hint: add control_type and an AutomationId")
    if s.class_name:
        return 30, "located by class name only; matches every control of that class. Hint: add AutomationId or title + control_type"
    if s.control_type and s.index is not None:
        return 10, "located by control type and position only; any layout change breaks it. Hint: add AutomationId"
    if s.index is not None:
        return 10, "located by position only; any layout change breaks it. Hint: add AutomationId"
    if s.control_type:
        return 15, "control type only is ambiguous. Hint: add AutomationId"
    return 0, "empty selector. Hint: add AutomationId"


def selector_fallbacks(s: Selector) -> list[Selector]:
    """Alternative selectors derivable from s (excluding s), most to least stable."""
    cands: list[Selector] = []
    if s.auto_id:
        cands.append(Selector(auto_id=s.auto_id, control_type=s.control_type))
        cands.append(Selector(auto_id=s.auto_id))
    if s.path and s.control_type:
        cands.append(Selector(path=s.path, control_type=s.control_type, title=s.title))
        cands.append(Selector(path=s.path, control_type=s.control_type))
    if s.title and s.control_type:
        cands.append(Selector(title=s.title, control_type=s.control_type))
    if s.class_name:
        cands.append(Selector(class_name=s.class_name, control_type=s.control_type, title=s.title)
                     if s.title else Selector(class_name=s.class_name, control_type=s.control_type))
    if s.index is not None and (s.control_type or s.class_name):
        cands.append(Selector(control_type=s.control_type, class_name=s.class_name, index=s.index))
    seen: list[Selector] = [s]
    scored = []
    for order, c in enumerate(cands):
        if c in seen:
            continue
        seen.append(c)
        sc = selector_score(c)[0]
        if sc > 0:
            scored.append((-sc, order, c))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [c for _, _, c in scored]
