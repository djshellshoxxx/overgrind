"""DIAG: Differential Twin-Build. Same cases against two adapters (builds), sequentially."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from random import Random
from typing import Any, Callable, Iterable, Sequence

from .evidence import classify
from .registry import resolve_callable
from .types import (
    AdapterSpec, Case, ControlDef, ErrorEvent, FeatureDef, HarnessError, Outcome, Project, Selector,
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
    a = resolve_callable(spec.factory)(**dict(spec.kwargs))
    a.attach(project.target)
    return a


def _active(state: dict[str, str], fid: str, feats: dict[str, FeatureDef] | None = None) -> bool:
    """Scope semantics (types.ControlDef.scope): active == state is not the feature's first state."""
    f = feats.get(fid) if feats else None
    if f is not None and f.states:
        return state.get(fid, f.default_state) != f.states[0]
    return str(state.get(fid, "off")).lower() not in ("off", "false", "0", "none", "")


def _retarget(obj: Any, m: Any) -> Any:
    """Apply an id_map value: a str renames the id, a Selector replaces the selector (fuzzy mapping)."""
    if isinstance(m, Selector):
        return replace(obj, selector=m)
    return replace(obj, id=m)


class _Side:
    """One build under test: owns an adapter (rebuilt after a failure) and runs single cases."""

    def __init__(self, project: Project, spec: AdapterSpec, id_map: dict[str, Any],
                 clock: Callable[[], float], factory: Callable[[], Any] | None,
                 b_domains: dict[str, Any] | None = None) -> None:
        self.project, self.spec, self.id_map, self.clock = project, spec, id_map, clock
        self.mk = factory or (lambda: _build(spec, project))
        self.feats = {f.id: f for f in project.features}
        self.ctls = {c.id: c for c in project.controls}
        self.b_domains = b_domains or {}
        self.adapter: Any = None
        self.skipped_domain = 0

    def close(self) -> None:
        if self.adapter is not None:
            try:
                self.adapter.detach(kill=True)
            except Exception:
                pass
            self.adapter = None

    def run(self, case: Case) -> _CaseResult:
        project, feats, ctls, id_map = self.project, self.feats, self.ctls, self.id_map
        if self.adapter is None:
            self.adapter = self.mk()
        adapter = self.adapter
        unresolved: set[str] = set()
        readback: dict[str, Any] = {}
        t0 = self.clock()
        outcome = Outcome()
        try:
            adapter.reset(project)
            for fid in list(case.order) + [f for f in case.state if f not in case.order]:
                if id_map.get(fid, fid) is None:
                    unresolved.add(fid)
                    continue
                f: FeatureDef = _retarget(feats[fid], id_map.get(fid, fid)) if fid in id_map else feats[fid]
                try:
                    if fid in case.state:
                        adapter.set_feature_state(f, case.state[fid])
                    else:
                        adapter.invoke(f)
                except HarnessError:
                    unresolved.add(fid)
            skipped: set[str] = set()
            for cid, val in case.values.items():
                c = ctls.get(cid)
                if c is None or any(not _active(case.state, s, feats) for s in c.scope):
                    continue
                mapped = id_map.get(cid, cid)
                if mapped is None:
                    unresolved.add(cid)
                    continue
                bd = self.b_domains.get(cid)
                if bd is not None:
                    from . import domain as _dom
                    if not _dom.contains(bd, val, strict_step=False):
                        skipped.add(cid)              # outside the build-B domain: skipped, never an alarm
                        self.skipped_domain += 1
                        continue
                try:
                    adapter.set_control(_retarget(c, mapped) if cid in id_map else c, val)
                except HarnessError:
                    unresolved.add(cid)
            adapter.wait_quiescent(project.run.settle_ms)
            outcome = classify(adapter.health())
            if outcome.ok:
                for cid in case.values:
                    c = ctls.get(cid)
                    if (c is None or cid in unresolved or cid in skipped
                            or any(not _active(case.state, s, feats) for s in c.scope)):
                        continue
                    try:
                        readback[cid] = adapter.get_control(_retarget(c, id_map[cid]) if cid in id_map else c)
                    except HarnessError:
                        unresolved.add(cid)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            outcome = classify(exc)
        res = _CaseResult(outcome, self.clock() - t0, readback, unresolved)
        if not outcome.ok:
            self.close()
        return res


def _run_build(project: Project, spec: AdapterSpec, cases: list[Case], id_map: dict[str, Any],
               clock: Callable[[], float], factory: Callable[[], Any] | None,
               b_domains: dict[str, Any] | None = None) -> list[_CaseResult]:
    side = _Side(project, spec, id_map, clock, factory, b_domains)
    try:
        return [side.run(c) for c in cases]
    finally:
        side.close()


def _close(a: Any, b: Any, tol: float, abs_tol: float = 1e-9) -> bool:
    """Spec 3.4: |a-b| <= max(abs_tol, rel_tol * max(|a|,|b|)); bools/ints/enums exact."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= max(abs_tol, tol * max(abs(a), abs(b)))
    if isinstance(a, str) and isinstance(b, str):
        return _norm_text(a) == _norm_text(b)
    return a == b


def _norm_text(t: str) -> str:
    import unicodedata
    return " ".join(unicodedata.normalize("NFKC", t).split())


# ------------------------------------------------------------------ statistics
def wilcoxon_signed_rank(diffs: Sequence[float]) -> float:
    """Two-sided p of the paired Wilcoxon signed-rank test (normal approximation with tie correction;
    zeros dropped). n < 6 returns 1.0 (no evidence)."""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n < 6:
        return 1.0
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    tie_term = 0.0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = r
        t = j - i + 1
        tie_term += t ** 3 - t
        i = j + 1
    w = sum(r for r, x in zip(ranks, d) if x > 0)
    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24 - tie_term / 48
    if var <= 0:
        return 1.0
    z = (abs(w - mean) - 0.5) / math.sqrt(var)
    return math.erfc(max(z, 0.0) / math.sqrt(2))


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return 0.0 if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


# ------------------------------------------------------------------ control mapping (U5 3.2)
@dataclass(frozen=True)
class MapEntry:
    b_selector: Selector | None
    score: float
    status: str                       # mapped | review | unmatched
    reasons: tuple[str, ...] = ()
    runner_up: float = 0.0
    b_name: str = ""
    b_domain: Any = None              # observed domain of the B element (suggested_domain), if any


MAP_ACCEPT, MAP_MARGIN, MAP_REVIEW = 0.80, 0.10, 0.60


def migrate_map(project: Project, info_a: Sequence[Any], info_b: Sequence[Any]) -> dict[str, MapEntry]:
    """PURE (Linux-testable): map each feature/control id of the project from build A to build B.

    Stages: hard type filter (equivalence classes allowed) -> exact unique auto_id+type (score 1.0) ->
    weighted fuzzy similarity (selectors.similarity) -> one-to-one assignment -> decision
    (mapped >= 0.80 with margin >= 0.10 to the runner-up; review 0.60..0.80 or small margin; else
    unmatched = cannot compare). Never mutates the project."""
    from . import selectors as S
    from .adapters import resolve as R
    a_items: list[tuple[str, Any]] = []
    for f in project.features:
        if f.kind != "action" or True:
            a_items.append((f.id, f.selector))
    for c in project.controls:
        a_items.append((c.id, c.selector))
    out: dict[str, MapEntry] = {}
    b_by_aid: dict[str, list[int]] = {}
    for i, e in enumerate(info_b):
        if e.selector.auto_id:
            b_by_aid.setdefault(e.selector.auto_id, []).append(i)
    taken: set[int] = set()
    pending: list[tuple[str, Any, Any]] = []          # (id, A ElementInfo|Selector, selector)
    for ident, sel in a_items:
        ra = R.resolve(sel, info_a)
        a_el = info_a[ra.index] if ra.found else None
        target = a_el if a_el is not None else sel
        ctype = (a_el.control_type if a_el is not None else sel.control_type)
        hits = [i for i in b_by_aid.get(sel.auto_id, []) if S.types_compatible(ctype, info_b[i].control_type)] \
            if sel.auto_id else []
        if len(hits) == 1 and hits[0] not in taken and info_b[hits[0]].control_type == (ctype or info_b[hits[0]].control_type):
            e = info_b[hits[0]]
            taken.add(hits[0])
            out[ident] = MapEntry(e.selector, 1.0, "mapped", ("unique AutomationId + control type",), 0.0,
                                  e.name, e.suggested_domain)
        else:
            pending.append((ident, target, sel))
    # fuzzy: score every pending A against every free B
    scores: dict[str, list[tuple[float, int]]] = {}
    for ident, target, sel in pending:
        row = []
        for i, e in enumerate(info_b):
            if i in taken:
                continue
            sc = S.similarity(target, e, a_tree=info_a, b_tree=info_b)
            if sc > 0:
                row.append((sc, i))
        row.sort(key=lambda t: (-t[0], t[1]))
        scores[ident] = row
    pairs = sorted(((row[0][0], ident, row[0][1]) for ident, row in scores.items() if row), key=lambda t: (-t[0], t[1]))
    assigned: dict[str, int] = {}
    used: set[int] = set()
    # greedy one-to-one by best score; a lost candidate falls back to its next best
    cursor = {ident: 0 for ident in scores}
    queue = list(pairs)
    while queue:
        sc, ident, bi = queue.pop(0)
        if bi in used:
            cursor[ident] += 1
            row = scores[ident]
            while cursor[ident] < len(row) and row[cursor[ident]][1] in used:
                cursor[ident] += 1
            if cursor[ident] < len(row):
                queue.append((row[cursor[ident]][0], ident, row[cursor[ident]][1]))
                queue.sort(key=lambda t: (-t[0], t[1]))
            continue
        used.add(bi)
        assigned[ident] = bi
    for ident, _t, sel in pending:
        row = scores.get(ident, [])
        if ident not in assigned or not row:
            out[ident] = MapEntry(None, row[0][0] if row else 0.0, "unmatched", ("no comparable control in B",))
            continue
        bi = assigned[ident]
        sc = next(s_ for s_, i in row if i == bi)
        alts = [s_ for s_, i in row if i != bi]
        runner = alts[0] if alts else 0.0
        margin = sc - runner
        e = info_b[bi]
        why = (f"similarity {sc:.2f}, margin {margin:.2f}",)
        if sc >= MAP_ACCEPT and margin >= MAP_MARGIN:
            status = "mapped"
        elif sc >= MAP_REVIEW:
            status = "review"
        else:
            status = "unmatched"
        out[ident] = MapEntry(e.selector if status != "unmatched" else None, round(sc, 4), status, why,
                              round(runner, 4), e.name, e.suggested_domain)
    return out


@dataclass(frozen=True)
class MappingRow:
    id: str
    a_name: str
    b_name: str
    score: float
    status: str
    reasons: tuple[str, ...]
    parts: dict = field(default_factory=dict)       # per-attribute similarity (for the score bar tooltip)


@dataclass
class MappingReview:
    """GUI data structure for the 'map review' screen: rows + removed/added lists + user decisions."""
    rows: list[MappingRow]
    removed: list[str]                  # in A, nothing comparable in B ("removed control")
    added: list[str]                    # B elements nobody mapped to ("added control", not fuzzed)
    entries: dict[str, MapEntry]
    confirmed: set[str] = field(default_factory=set)
    overrides: dict[str, Selector | None] = field(default_factory=dict)

    def confirm(self, ident: str) -> None:
        self.confirmed.add(ident)

    def override(self, ident: str, selector: Selector | None) -> None:
        self.overrides[ident] = selector
        self.confirmed.add(ident)

    def id_map(self, include_review: bool = False) -> dict[str, Any]:
        """id -> B Selector (mapped / confirmed / overridden) or None (cannot compare)."""
        m: dict[str, Any] = {}
        for ident, e in self.entries.items():
            if ident in self.overrides:
                m[ident] = self.overrides[ident]
            elif e.status == "mapped" or (e.status == "review" and (include_review or ident in self.confirmed)):
                m[ident] = e.b_selector
            else:
                m[ident] = None
        return m

    def b_domains(self) -> dict[str, Any]:
        return {i: e.b_domain for i, e in self.entries.items() if e.b_domain is not None and i in self.id_map()
                and self.id_map()[i] is not None}

    def to_dict(self) -> dict[str, Any]:
        return {"rows": [dict(id=r.id, a=r.a_name, b=r.b_name, score=r.score, status=r.status,
                              reasons=list(r.reasons)) for r in self.rows],
                "removed": list(self.removed), "added": list(self.added),
                "counts": {st: sum(1 for r in self.rows if r.status == st) for st in ("mapped", "review", "unmatched")}}


def review_map(project: Project, info_a: Sequence[Any], info_b: Sequence[Any]) -> MappingReview:
    from . import selectors as S
    from .adapters import resolve as R
    entries = migrate_map(project, info_a, info_b)
    rows = []
    used_b = set()
    names = {f.id: (f.name or f.id) for f in project.features}
    for ident, e in entries.items():
        ra = R.resolve(next((x.selector for x in (*project.features, *project.controls) if x.id == ident)), info_a)
        a_name = info_a[ra.index].name if ra.found else names.get(ident, ident)
        parts: dict = {}
        if e.b_selector is not None:
            rb = R.resolve(e.b_selector, info_b)
            if rb.found:
                used_b.add(rb.index)
                if ra.found:
                    parts = {k: v for k, v in S.similarity_parts(info_a[ra.index], info_b[rb.index],
                                                                 a_tree=info_a, b_tree=info_b).items() if v is not None}
        rows.append(MappingRow(ident, a_name, e.b_name, e.score, e.status, e.reasons, parts))
    removed = [r.id for r in rows if r.status == "unmatched"]
    added = [e.name for i, e in enumerate(info_b) if i not in used_b and e.control_type not in ("Window", "Pane", "Tab", "Text")]
    return MappingReview(rows, removed, added, entries)


# ------------------------------------------------------------------ report records
@dataclass(frozen=True)
class Divergence:
    id: str
    verdict: str          # regression | fixed | behaviour-change | perf-suspect | cannot-compare | noise
    oracle: str           # O1 liveness | O2 read-back | O6 timing
    control_id: str
    a_value: Any
    b_value: Any
    case: Case
    reproduced: tuple[int, int] = (0, 0)      # (replays reproducing it, replays)
    p_noise: float = 0.0                      # A/A false-positive rate of this oracle/element


_VERDICT = {"regression": ("regression", "O1"), "fix": ("fixed", "O1"), "value": ("behaviour-change", "O2"),
            "outcome": ("behaviour-change", "O1"), "timing": ("perf-suspect", "O6"),
            "incomparable": ("cannot-compare", "O2")}


@dataclass(frozen=True)
class Calibration:
    n: int
    noisy_controls: dict            # control id -> cases where A1 != A2
    outcome_flips: int
    fp_rate: dict                   # oracle -> d/n
    timing_ratio: float             # median(A2 latency)/median(A1 latency)
    perf_enabled: bool


def _compare(case: Case, a: _CaseResult, b: _CaseResult, tol: float, abs_tol: float, time_factor: float,
             time_floor_s: float, legacy_timing: bool) -> tuple[list[DiffItem], list[DiffItem], list[DiffItem],
                                                                  list[DiffItem], bool]:
    reg: list[DiffItem] = []
    fix: list[DiffItem] = []
    mis: list[DiffItem] = []
    inc: list[DiffItem] = []
    s = case.seq
    for tgt in sorted(a.unresolved | b.unresolved):
        side = "A" if tgt in a.unresolved and tgt not in b.unresolved else \
               "B" if tgt in b.unresolved and tgt not in a.unresolved else "A and B"
        inc.append(DiffItem("incomparable", s, tgt, detail=f"cannot resolve in {side}"))
    clean = False
    if a.outcome.ok and b.outcome.ok:
        clean = not (a.unresolved | b.unresolved)
        for cid in sorted(set(a.readback) & set(b.readback)):
            if not _close(a.readback[cid], b.readback[cid], tol, abs_tol):
                mis.append(DiffItem("value", s, cid, a.readback[cid], b.readback[cid],
                                    f"differs by more than {tol}"))
                clean = False
        ta, tb = a.elapsed_s, b.elapsed_s
        if legacy_timing and max(ta, tb) >= time_floor_s and min(ta, tb) * time_factor < max(ta, tb):
            mis.append(DiffItem("timing", s, "", round(ta, 4), round(tb, 4), f"ratio beyond x{time_factor}"))
            clean = False
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
    return reg, fix, mis, inc, clean


@dataclass(frozen=True)
class DiffReport:
    n_cases: int
    compared: int
    regressions: tuple[DiffItem, ...] = ()
    fixes: tuple[DiffItem, ...] = ()
    mismatches: tuple[DiffItem, ...] = ()
    incomparable: tuple[DiffItem, ...] = ()
    # --- additive (twin-build spec): all empty by default
    divergences: tuple[Divergence, ...] = ()        # reproduced, unmasked divergences (verdict table)
    suspected_noise: tuple[Divergence, ...] = ()    # `noise?` bucket: not reproduced k_confirm times
    noise: dict = field(default_factory=dict)       # A/A calibration summary + masks
    perf: dict = field(default_factory=dict)        # Wilcoxon result / disabled reason
    control_map: dict = field(default_factory=dict)
    order_log: tuple = ()                           # ("A"|"B", case seq) in execution order
    skipped_domain: int = 0

    @property
    def clean(self) -> bool:
        return not (self.regressions or self.fixes or self.mismatches or self.incomparable)

    def to_dict(self) -> dict[str, Any]:
        f = lambda xs: [dict(kind=i.kind, case=i.case_seq, target=i.target, a=i.a, b=i.b, detail=i.detail) for i in xs]
        d = {"n_cases": self.n_cases, "compared": self.compared, "regressions": f(self.regressions),
             "fixes": f(self.fixes), "mismatches": f(self.mismatches), "incomparable": f(self.incomparable)}
        if self.divergences or self.noise or self.perf or self.suspected_noise:
            d["divergences"] = [dict(id=x.id, verdict=x.verdict, oracle=x.oracle, control=x.control_id,
                                     a=x.a_value, b=x.b_value, reproduced=list(x.reproduced), p_noise=x.p_noise)
                                for x in self.divergences]
            d["noise"], d["perf"] = self.noise, self.perf
            d["suspected_noise"] = len(self.suspected_noise)
        return d

    def to_markdown(self) -> str:
        L = ["# Differential report (A = baseline build, B = candidate build)", "",
             f"- Cases: {self.n_cases}, fully comparable: {self.compared}",
             f"- Regressions (A ok, B fails): {len(self.regressions)}",
             f"- Fixes (A fails, B ok): {len(self.fixes)}",
             f"- Mismatches: {len(self.mismatches)}",
             f"- Cannot compare: {len(self.incomparable)}"]
        if self.noise.get("masked"):
            L.append(f"- Masked as nondeterministic (A/A): {', '.join(sorted(self.noise['masked']))}")
        if self.perf:
            L.append(f"- Timing oracle: {self.perf.get('message', '')}")
        L.append("")
        for title, items in (("Regressions", self.regressions), ("Fixes", self.fixes),
                             ("Mismatches", self.mismatches), ("Cannot compare", self.incomparable)):
            if items:
                L.append(f"## {title}")
                for i in items:
                    tgt = f" `{i.target}`" if i.target else ""
                    L.append(f"- case {i.case_seq}{tgt}: A={i.a!r} B={i.b!r} {i.detail}".rstrip())
                L.append("")
        return "\n".join(L)

    def to_events(self, run_id: str = "diff") -> list[ErrorEvent]:
        """ErrorEvent-compatible records for the reproduced divergences (agreeing cases are never stored)."""
        import datetime
        from .evidence import fingerprint
        out = []
        for d in self.divergences:
            if d.verdict in ("cannot-compare", "noise"):
                continue
            cat = {"regression": "assertion", "fixed": "assertion", "behaviour-change": "assertion",
                   "perf-suspect": "resource"}.get(d.verdict, "assertion")
            msg = f"A: {d.a_value!r}, B: {d.b_value!r}" if d.control_id else f"{d.verdict}: A {d.a_value}, B {d.b_value}"
            sig = fingerprint(cat, f"{d.verdict}|{d.control_id}|{d.oracle}")
            out.append(ErrorEvent(id=d.id, ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                  run_id=run_id, category=cat, signature=sig, message=msg, case=d.case, history=[],
                                  env={"twin": {"verdict": d.verdict, "oracle": d.oracle, "control": d.control_id,
                                                "reproduced": list(d.reproduced), "p_noise": d.p_noise}}))
        return out


# ------------------------------------------------------------------ A/A calibration (U5 3.5.1)
def calibrate(project: Project, spec_a: AdapterSpec, cases: list[Case], *, tolerance: float = 1e-6,
              abs_tol: float = 1e-9, clock: Callable[[], float] = time.monotonic,
              factory: Callable[[], Any] | None = None) -> Calibration:
    """Run the SAME cases on build A twice (two fresh launches). Controls/oracles that differ in > 0 cases
    are nondeterministic: their divergences get masked and the empirical false-positive rate d/n is
    reported next to every verdict class."""
    r1 = _run_build(project, spec_a, cases, {}, clock, factory)
    r2 = _run_build(project, spec_a, cases, {}, clock, factory)
    noisy: dict[str, int] = {}
    flips = 0
    for a, b in zip(r1, r2):
        if a.outcome.ok != b.outcome.ok or (not a.outcome.ok and a.outcome.category != b.outcome.category):
            flips += 1
            continue
        for cid in set(a.readback) & set(b.readback):
            if not _close(a.readback[cid], b.readback[cid], tolerance, abs_tol):
                noisy[cid] = noisy.get(cid, 0) + 1
    n = max(1, len(cases))
    m1, m2 = _median([x.elapsed_s for x in r1[5:]]), _median([x.elapsed_s for x in r2[5:]])
    ratio = (m2 / m1) if m1 > 0 else 1.0
    perf_ok = 1 / 1.2 <= ratio <= 1.2
    fp = {"O1": flips / n, "O2": (sum(noisy.values()) / n)}
    return Calibration(len(cases), noisy, flips, fp, round(ratio, 4), perf_ok)


def _perf_oracle(a_lat: list[float], b_lat: list[float], cal: Calibration | None, *, min_samples: int = 30,
                 discard: int = 5, min_ratio: float = 1.5, min_abs_s: float = 0.1, alpha: float = 0.01) -> dict[str, Any]:
    x, y = a_lat[discard:], b_lat[discard:]
    n = min(len(x), len(y))
    if cal is not None and not cal.perf_enabled:
        return {"enabled": False, "message": f"disabled: too noisy (A/A median ratio {cal.timing_ratio:.2f} outside 1/1.2..1.2)"}
    if n < min_samples:
        return {"enabled": False, "message": f"disabled: only {n} paired samples (< {min_samples})"}
    x, y = x[:n], y[:n]
    ma, mb = _median(x), _median(y)
    p = wilcoxon_signed_rank([b - a for a, b in zip(x, y)])
    ratio = (mb / ma) if ma > 0 else float("inf")
    flag = ratio >= min_ratio and (mb - ma) >= min_abs_s and p < alpha
    return {"enabled": True, "flagged": flag, "ratio": round(ratio, 3), "median_a": ma, "median_b": mb,
            "p": p, "samples": n, "message": ("perf-suspect: " if flag else "no slowdown: ")
            + f"median ratio {ratio:.2f}, p={p:.2g}, n={n}"}


# ------------------------------------------------------------------ main entry
def differential(project: Project, spec_a: AdapterSpec, spec_b: AdapterSpec, n_cases: int = 20,
                 seed: int = 1, tolerance: float = 1e-6, *, id_map: dict[str, Any] | None = None,
                 time_factor: float = 3.0, time_floor_s: float = 0.05,
                 cases: list[Case] | None = None, clock: Callable[[], float] = time.monotonic,
                 factory_a: Callable[[], Any] | None = None,
                 factory_b: Callable[[], Any] | None = None,
                 abs_tol: float = 1e-9, order: str = "sequential", calibrate_cases: int = 0,
                 k_confirm: int = 0, masks: Iterable[str] = (), wilcoxon: bool = False,
                 b_domains: dict[str, Any] | None = None, mapping: MappingReview | None = None,
                 store: Any = None, cancel: Callable[[], bool] | None = None,
                 progress: Callable[[str], None] | None = None) -> DiffReport:
    """Run identical cases on build A and B (never concurrently).

    order="sequential" (default, legacy): all of A then all of B. order="ABBA": lockstep per case, odd cases
    run B first (cancels drift). id_map maps ids of A to ids (str) or selectors (Selector) in B; None = missing.
    `mapping` (a reviewed MappingReview) supplies id_map and the B domains. `calibrate_cases` > 0 first runs
    that many cases on A twice and masks nondeterministic controls; `k_confirm` > 0 replays every divergence
    on fresh launches of both builds and keeps it only when reproduced >= ceil(2k/3) times (else `noise?`).
    `wilcoxon=True` replaces the legacy fixed ratio by the paired Wilcoxon timing oracle."""
    if mapping is not None:
        id_map = {**(id_map or {}), **mapping.id_map()}
        b_domains = {**(b_domains or {}), **mapping.b_domains()}
    cs = list(cases) if cases is not None else make_cases(project, n_cases, seed)
    masked = set(masks)
    noise: dict[str, Any] = {}
    cal: Calibration | None = None
    if calibrate_cases > 0:
        if progress:
            progress("A/A calibration")
        cal = calibrate(project, spec_a, cs[:calibrate_cases] if len(cs) >= calibrate_cases else cs,
                        tolerance=tolerance, abs_tol=abs_tol, clock=clock, factory=factory_a)
        masked |= set(cal.noisy_controls)
        noise = {"masked": sorted(masked), "fp_rate": cal.fp_rate, "noisy_controls": dict(cal.noisy_controls),
                 "outcome_flips": cal.outcome_flips, "timing_ratio": cal.timing_ratio, "n": cal.n}
    elif masked:
        noise = {"masked": sorted(masked)}
    side_a = _Side(project, spec_a, {}, clock, factory_a)
    side_b = _Side(project, spec_b, dict(id_map or {}), clock, factory_b, b_domains)
    log: list[tuple[str, int]] = []
    ra: list[_CaseResult] = []
    rb: list[_CaseResult] = []
    try:
        if order.upper() == "ABBA":
            for i, case in enumerate(cs):
                if cancel is not None and cancel():
                    cs = cs[:i]
                    break
                first, second = (side_a, side_b) if i % 2 == 0 else (side_b, side_a)
                r_first = first.run(case)
                log.append(("A" if first is side_a else "B", case.seq))
                r_second = second.run(case)
                log.append(("A" if second is side_a else "B", case.seq))
                (ra if first is side_a else rb).append(r_first)
                (ra if second is side_a else rb).append(r_second)
        else:
            for case in cs:
                ra.append(side_a.run(case))
                log.append(("A", case.seq))
            for case in cs:
                rb.append(side_b.run(case))
                log.append(("B", case.seq))
    finally:
        side_a.close()
        side_b.close()
    reg: list[DiffItem] = []
    fix: list[DiffItem] = []
    mis: list[DiffItem] = []
    inc: list[DiffItem] = []
    compared = 0
    for case, a, b in zip(cs, ra, rb):
        r_, f_, m_, i_, clean = _compare(case, a, b, tolerance, abs_tol, time_factor, time_floor_s, not wilcoxon)
        m_ = [m for m in m_ if m.target not in masked]
        if masked and not m_ and not (r_ or f_ or i_):
            clean = a.outcome.ok and b.outcome.ok
        reg += r_
        fix += f_
        mis += m_
        inc += i_
        compared += clean
    case_by = {c.seq: c for c in cs}
    divs: list[Divergence] = []
    suspect: list[Divergence] = []
    p_noise = lambda cid, orc: float((cal.fp_rate.get(orc, 0.0) if cal else 0.0))     # noqa: E731
    seen_ids = 0
    for item in [*reg, *fix, *mis]:
        if item.kind == "timing":
            continue
        verdict, oracle = _VERDICT[item.kind]
        repro = (0, 0)
        if k_confirm > 0:
            repro = _confirm(project, spec_a, spec_b, case_by[item.case_seq], item, k_confirm, id_map, b_domains,
                             clock, factory_a, factory_b, tolerance, abs_tol, masked)
        seen_ids += 1
        d = Divergence(f"d{seen_ids:04d}", verdict, oracle, item.target, item.a, item.b, case_by[item.case_seq],
                       repro, p_noise(item.target, oracle))
        need = math.ceil(2 * k_confirm / 3) if k_confirm > 0 else 0
        (divs if repro[0] >= need else suspect).append(d)
    perf: dict[str, Any] = {}
    if wilcoxon:
        perf = _perf_oracle([x.elapsed_s for x in ra], [x.elapsed_s for x in rb], cal)
    return DiffReport(len(cs), compared, tuple(reg), tuple(fix), tuple(mis), tuple(inc), tuple(divs),
                      tuple(suspect), noise, perf, dict(mapping.entries) if mapping is not None else {},
                      tuple(log), side_b.skipped_domain)


def _confirm(project: Project, spec_a: AdapterSpec, spec_b: AdapterSpec, case: Case, item: DiffItem, k: int,
             id_map: dict | None, b_domains: dict | None, clock: Callable[[], float],
             factory_a: Callable[[], Any] | None, factory_b: Callable[[], Any] | None,
             tol: float, abs_tol: float, masked: set[str]) -> tuple[int, int]:
    """Replay one case k times on fresh launches of both builds; count replays where the same divergence recurs."""
    hits = 0
    for _ in range(k):
        a = _run_build(project, spec_a, [case], {}, clock, factory_a)[0]
        b = _run_build(project, spec_b, [case], dict(id_map or {}), clock, factory_b, b_domains)[0]
        r_, f_, m_, _i, _c = _compare(case, a, b, tol, abs_tol, 3.0, 0.05, False)
        if any(x.kind == item.kind and x.target == item.target for x in (*r_, *f_, *m_)):
            hits += 1
    return hits, k
