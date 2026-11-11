"""DIAG: Dependency Inference. Observe-and-propose; proposals are never auto-applied."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from .types import ConstraintDef, Project, Provenance

# Observation = (assignment {feature: state}, flags {id: {'enabled': bool, 'visible': bool, 'state'?: str}})
Observation = tuple
OFF_STATES = frozenset({"off", "false", "0", "none", ""})


@dataclass(frozen=True)
class Proposal:
    kind: str                    # scope | requires | mutex
    subject: str                 # control id (scope), dependent feature (requires), "" for mutex
    features: tuple[str, ...]    # scope/requires: (needed feature,); mutex: the exclusive pair
    confidence: str              # verified | inferred
    evidence: int                # consistent observations
    contradictions: int = 0
    text: str = ""
    # --- additive (statistical inference): all default to "no statistics"
    effect: float = 0.0
    p: float = 1.0
    q: float = 1.0
    impact: dict = field(default_factory=dict)       # valid_states before/after if approved
    detail: dict = field(default_factory=dict)       # raw evidence (tables, trial counts)
    status: str = "pending"                          # pending | approved | rejected

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(f"{self.kind}|{self.subject}|{'+'.join(self.features)}".encode()).hexdigest()[:16]

    def evidence_strength(self) -> int:
        return self.evidence

    def apply(self, project: Project) -> Project:
        """Immutable update; the original project is untouched. Idempotent."""
        if self.kind == "enable_inverse":
            return project          # 'disabled when F is on' has no ControlDef/ConstraintDef encoding: report only
        if self.kind == "scope":
            out = []
            hit = False
            for c in project.controls:
                if c.id == self.subject:
                    hit = True
                    scope = tuple(c.scope) + tuple(f for f in self.features if f not in c.scope)
                    c = replace(c, scope=scope, provenance=Provenance("inferred", self.confidence, self.text))
                out.append(c)
            if not hit:
                raise ValueError(f"unknown control {self.subject!r}")
            return replace(project, controls=tuple(out))
        if self.kind == "requires":
            cd = ConstraintDef("requires", features=(self.subject,), needs=tuple(self.features))
        elif self.kind == "mutex":
            cd = ConstraintDef("mutex", features=tuple(sorted(self.features)))
        else:
            raise ValueError(f"unknown proposal kind {self.kind!r}")
        if cd in project.constraints:
            return project
        return replace(project, constraints=tuple(project.constraints) + (cd,))


def _is_active(state: str, default: str | None = None) -> bool:
    s = str(state).lower()
    if default is not None:
        return s != str(default).lower()
    return s not in OFF_STATES


def _ready(flag: dict[str, Any]) -> bool:
    return bool(flag.get("enabled", True)) and bool(flag.get("visible", True))


def infer_dependencies(observations: Iterable[Observation], project: Project | None = None,
                       min_verified: int = 3) -> list[Proposal]:
    obs = [(dict(a), dict(f)) for a, f in observations]
    # 'active' = not the feature's FIRST state (same as constraints/worker/scope; was: differs from default)
    defaults = {f.id: (f.states[0] if f.states else f.default_state) for f in project.features} if project else {}

    def active(fid: str, st: str) -> bool:
        return _is_active(st, defaults.get(fid))

    feature_ids = {k for a, _ in obs for k in a}
    if project:
        feature_ids |= {f.id for f in project.features}
    flag_ids = sorted({k for _, f in obs for k in f})
    out: list[Proposal] = []

    # ---- scope / requires: single-feature observations (one-at-a-time toggling) vs baseline
    for target in flag_ids:
        for feat in sorted(feature_ids):
            if feat == target:
                continue
            on_ready = on_notready = off_ready = off_notready = 0
            for assign, flags in obs:
                if target not in flags:
                    continue
                act = [k for k, v in assign.items() if active(k, v)]
                if act == [feat]:
                    if _ready(flags[target]):
                        on_ready += 1
                    else:
                        on_notready += 1
                elif not act:
                    if _ready(flags[target]):
                        off_ready += 1
                    else:
                        off_notready += 1
            # dependency: not ready when feature inactive, ready when active
            if on_ready and off_notready and not off_ready:
                support = on_ready + off_notready
                contra = on_notready
                conf = "verified" if (support >= min_verified and contra == 0) else "inferred"
                if target in feature_ids:
                    out.append(Proposal("requires", target, (feat,), conf, support, contra,
                                        f"{target} is only available when {feat} is active"))
                else:
                    out.append(Proposal("scope", target, (feat,), conf, support, contra,
                                        f"control {target} is only available when {feat} is active"))

    # ---- mutex: a feature we set reads back inactive when another is also set
    pair_votes: dict[tuple[str, str], list[int]] = {}
    for assign, flags in obs:
        act = [k for k, v in assign.items() if active(k, v)]
        if len(act) != 2:
            continue
        a, b = sorted(act)
        forced = [k for k in act if k in flags and "state" in flags[k]
                  and not active(k, flags[k]["state"])]
        ok = all(k in flags and "state" in flags[k] and active(k, flags[k]["state"]) for k in act)
        v = pair_votes.setdefault((a, b), [0, 0])
        if len(forced) == 1:
            v[0] += 1
        elif ok:
            v[1] += 1
    for (a, b), (yes, no) in sorted(pair_votes.items()):
        if yes:
            conf = "verified" if yes >= min_verified and no == 0 else "inferred"
            out.append(Proposal("mutex", "", (a, b), conf, yes, no,
                                f"enabling both {a} and {b} switches one of them off"))
    return out


# ------------------------------------------------------------------ observation gathering
def gather_observations(adapter: Any, project: Project,
                        probe: Callable[[Any, Project], dict[str, dict[str, Any]]],
                        pairs: bool = False, repeats: int = 1) -> list[Observation]:
    """Toggle features one at a time (optionally also pairs), probe flags, always restore defaults.

    `probe(adapter, project) -> {id: {'enabled', 'visible', ['state']}}` must only READ.
    """
    feats = [f for f in project.features if f.enabled and not f.dangerous and f.kind != "action"]
    settle = project.run.settle_ms

    def observe(assign: dict[str, str]) -> Observation:
        try:
            for fid, st in assign.items():
                adapter.set_feature_state(next(f for f in feats if f.id == fid), st)
            adapter.wait_quiescent(settle)
            return (dict(assign), probe(adapter, project))
        finally:
            adapter.reset(project)

    result: list[Observation] = []
    try:
        adapter.reset(project)
        for _ in range(max(1, repeats)):
            result.append((({}), probe(adapter, project)))
            for f in feats:
                for st in f.states:
                    if st != f.default_state:
                        result.append(observe({f.id: st}))
            if pairs:
                for i, fa in enumerate(feats):
                    for fb in feats[i + 1:]:
                        sa = next((s for s in fa.states if s != fa.default_state), None)
                        sb = next((s for s in fb.states if s != fb.default_state), None)
                        if sa is not None and sb is not None:
                            result.append(observe({fa.id: sa, fb.id: sb}))
    finally:
        adapter.reset(project)
    return result


# =====================================================================================
# Statistical inference (spec U4 3.3): interventions -> 2x2 tables -> Fisher + BH-FDR
# =====================================================================================
import hashlib
import json
import math
import random
import time
from dataclasses import field


@dataclass(frozen=True)
class InferConfig:
    min_per_side: int = 10           # contexts per side (feature active / inactive)
    q: float = 0.01                  # Benjamini-Hochberg FDR level
    min_effect: float = 0.95         # |P(enabled|on) - P(enabled|off)| needed for a proposal
    contexts: int | None = None      # K random contexts (None -> max(50, 10*|F|))
    max_seconds: float = 600.0
    watch_limit: int = 200
    min_backgrounds: int = 3         # distinct "other feature" backgrounds on each side
    conj_min_cell: int = 6           # contexts per (f1,f2) cell for conjunctions
    delay_steps: tuple[int, ...] = (1, 2, 4, 8, 16)   # read at cumulative settle multiples (delayed enable)
    trial_min: int = 10              # deterministic trials for mutex/requires (0 contradictions allowed)
    counter_probes: int = 3          # falsification attempts per proposed mutex/requires
    seed: int = 1


@dataclass(frozen=True)
class ElementObs:
    """One settled read of one element under one context (spec `Observation`; named ElementObs because the
    legacy tuple alias `Observation` is still public)."""
    context: dict
    element: str
    exists: bool = True
    visible: bool = True
    enabled: bool = True
    t_ms: float = 0.0               # latency until the value stopped changing
    noisy: bool = False             # reads never agreed: excluded from statistics


@dataclass(frozen=True)
class Trial:
    """Deterministic intervention for mutex/requires: what we asked and what the app reported back."""
    asked: dict                     # {feature: state} we tried to set (on top of the baseline)
    readback: dict                  # {feature: state} read after settling
    kind: str = "pair"              # pair (both asked at once, alternating order) | prereq (g set first, then f)
    subject: str = ""               # prereq trials: the feature whose stickiness is being tested


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p for [[a,b],[c,d]] (exact integer arithmetic; sums outcomes no likelier
    than the observed table)."""
    r1, r2, c1, n = a + b, c + d, a + c, a + b + c + d
    if n == 0:
        return 1.0
    den = math.comb(n, c1)
    obs = math.comb(r1, a) * math.comb(r2, c)
    tot = 0
    for x in range(max(0, c1 - r2), min(r1, c1) + 1):
        w = math.comb(r1, x) * math.comb(r2, c1 - x)
        if w <= obs:
            tot += w
    return min(1.0, tot / den)


def benjamini_hochberg(pvals: list[float], q: float) -> list[float]:
    """BH adjusted q-values, same order as input."""
    m = len(pvals)
    if not m:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [1.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, pvals[i] * m / rank)
        adj[i] = prev
    return adj


def _first_state(project: Project | None, fid: str, features: dict) -> str:
    f = features.get(fid)
    return f.states[0] if f is not None and f.states else "off"


def _is_on(features: dict, fid: str, state: str) -> bool:
    """'Active' == not the feature's first state (same semantics as constraints/worker/scope)."""
    f = features.get(fid)
    if f is not None and f.states:
        return state != f.states[0]
    return str(state).lower() not in OFF_STATES


def fingerprint(kind: str, subject: str, obj: str) -> str:
    return hashlib.sha1(f"{kind}|{subject}|{obj}".encode()).hexdigest()[:16]


def _majority(reads: list[bool]) -> tuple[bool, bool]:
    """(value, noisy): majority; noisy when not unanimous."""
    t = sum(reads)
    return t * 2 > len(reads) or (t * 2 == len(reads) and reads[-1]), t not in (0, len(reads))


def _collapse(observations: Iterable[ElementObs]) -> dict[tuple, dict[str, Any]]:
    """Repeated reads of one (context, element) -> one settled value by majority vote (read-noise guard)."""
    groups: dict[tuple, list[ElementObs]] = {}
    for o in observations:
        key = (tuple(sorted(o.context.items())), o.element)
        groups.setdefault(key, []).append(o)
    out = {}
    for key, os_ in groups.items():
        if any(o.noisy for o in os_) and len(os_) == 1:
            continue
        reads = [o.enabled and o.exists and o.visible for o in os_ if not o.noisy]
        if not reads:
            continue
        val, disagree = _majority(reads)
        if disagree and len(reads) < 3:               # two reads that disagree: no evidence either way
            continue
        out[key] = {"enabled": val, "context": dict(key[0]), "t_ms": max(o.t_ms for o in os_)}
    return out


def analyze(observations: Iterable[ElementObs], features: Iterable[FeatureDef],
            config: InferConfig = InferConfig(), project: Project | None = None,
            rejected: dict[str, int] | None = None) -> list[Proposal]:
    """PURE: observations -> proposals (deterministic; no adapter). enable(c|F) relations only; mutex /
    requires come from `analyze_trials`. Precision over recall: all thresholds must hold."""
    feats = {f.id: f for f in features if f.kind != "action"}
    cells = _collapse(observations)
    by_el: dict[str, list[dict]] = {}
    for (_, el), rec in cells.items():
        by_el.setdefault(el, []).append(rec)
    tests: list[dict] = []
    for el in sorted(by_el):
        recs = by_el[el]
        for fid in sorted(feats):
            tab = [[0, 0], [0, 0]]            # [on/off][enabled/disabled]
            bgs = [set(), set()]
            for r in recs:
                st = r["context"].get(fid)
                if st is None:
                    continue
                side = 0 if _is_on(feats, fid, st) else 1
                tab[side][0 if r["enabled"] else 1] += 1
                bgs[side].add(tuple(sorted((k, v) for k, v in r["context"].items() if k != fid)))
            n_on, n_off = sum(tab[0]), sum(tab[1])
            if min(n_on, n_off) < config.min_per_side or min(len(bgs[0]), len(bgs[1])) < config.min_backgrounds:
                continue
            p_on, p_off = tab[0][0] / n_on, tab[1][0] / n_off
            tests.append({"el": el, "f": fid, "tab": tab, "effect": p_on - p_off,
                          "p": fisher_exact(tab[0][0], tab[0][1], tab[1][0], tab[1][1]), "n": n_on + n_off})
    # ---- conjunctions among partially-explained features
    for el in sorted(by_el):
        weak = [t["f"] for t in tests if t["el"] == el and 0.3 < abs(t["effect"]) < config.min_effect]
        strong = {t["f"] for t in tests if t["el"] == el and abs(t["effect"]) >= config.min_effect}
        if strong:
            continue
        for i, f1 in enumerate(sorted(set(weak))):
            for f2 in sorted(set(weak))[i + 1:]:
                cell = {(a, b): [0, 0] for a in (0, 1) for b in (0, 1)}
                for r in by_el[el]:
                    s1, s2 = r["context"].get(f1), r["context"].get(f2)
                    if s1 is None or s2 is None:
                        continue
                    k = (int(_is_on(feats, f1, s1)), int(_is_on(feats, f2, s2)))
                    cell[k][0 if r["enabled"] else 1] += 1
                if min(sum(v) for v in cell.values()) < config.conj_min_cell:
                    continue
                both, rest = cell[(1, 1)], [sum(cell[k][0] for k in cell if k != (1, 1)),
                                           sum(cell[k][1] for k in cell if k != (1, 1))]
                p_both = both[0] / sum(both)
                p_rest = rest[0] / max(1, sum(rest))
                if p_both >= config.min_effect and p_rest <= 1 - config.min_effect:
                    tests.append({"el": el, "f": (f1, f2), "tab": [both, rest], "effect": p_both - p_rest,
                                  "p": fisher_exact(both[0], both[1], rest[0], rest[1]),
                                  "n": sum(both) + sum(rest)})
    qv = benjamini_hochberg([t["p"] for t in tests], config.q)
    props: list[Proposal] = []
    for t, q in zip(tests, qv):
        if q > config.q or abs(t["effect"]) < config.min_effect:
            continue
        fs = t["f"] if isinstance(t["f"], tuple) else (t["f"],)
        if t["effect"] > 0:
            kind, text = "scope", f"control {t['el']} is only enabled when {' and '.join(fs)} {'are' if len(fs) > 1 else 'is'} active"
            conf = "inferred"
        else:
            kind, text = "enable_inverse", f"control {t['el']} is disabled when {fs[0]} is active (not expressible as a scope; report only)"
            conf = "inferred"
        pr = Proposal(kind, t["el"], tuple(fs), conf, t["n"], 0, text, effect=round(t["effect"], 4),
                      p=t["p"], q=q, detail={"table": t["tab"], "relation": "enable"})
        fp = pr.fingerprint
        if rejected and fp in rejected and t["n"] < 2 * rejected[fp]:
            continue
        props.append(pr)
    return sorted(props, key=lambda p: (p.kind, p.subject, p.features))


def analyze_trials(trials: Iterable[Trial], features: Iterable[FeatureDef],
                   config: InferConfig = InferConfig(), rejected: dict[str, int] | None = None) -> list[Proposal]:
    """PURE: deterministic relations from interventions. A relation needs >= `trial_min` consistent trials
    and ZERO contradictions (a single counter-example rejects it: the UI is not rule-governed)."""
    feats = {f.id: f for f in features if f.kind != "action"}
    mutex: dict[tuple[str, str], list[int]] = {}
    req: dict[tuple[str, str], list[int]] = {}      # (f, g): [blocked_without_g, stuck_without_g, stuck_with_g, blocked_with_g]
    solo: dict[str, list[int]] = {}                 # f alone: [stuck, blocked]; mutex needs both members "free"
    for tr in trials:
        asked_on = [k for k, v in tr.asked.items() if k in feats and _is_on(feats, k, v)]
        if len(asked_on) == 1:
            f1 = asked_on[0]
            solo.setdefault(f1, [0, 0])[0 if (f1 in tr.readback and _is_on(feats, f1, tr.readback[f1])) else 1] += 1
        if len(asked_on) == 2 and tr.kind == "prereq":
            f1 = tr.subject
            g1 = next((x for x in asked_on if x != f1), "")
            w = req.setdefault((f1, g1), [0, 0, 0, 0])
            w[2 if (f1 in tr.readback and _is_on(feats, f1, tr.readback[f1])) else 3] += 1
        elif len(asked_on) == 2:
            a, b = sorted(asked_on)
            kept = [k for k in (a, b) if k in tr.readback and _is_on(feats, k, tr.readback[k])]
            v = mutex.setdefault((a, b), [0, 0])
            v[0 if len(kept) < 2 else 1] += 1
        elif len(asked_on) == 1:
            f = asked_on[0]
            stuck = f in tr.readback and _is_on(feats, f, tr.readback[f])
            for g, st in tr.asked.items():
                if g == f or g not in feats:
                    continue
                v = req.setdefault((f, g), [0, 0, 0, 0])
                if not _is_on(feats, g, st):
                    v[1 if stuck else 0] += 1
                else:
                    v[2 if stuck else 3] += 1
    out: list[Proposal] = []
    for (a, b), (yes, no) in sorted(mutex.items()):
        free = all(solo.get(x, [0, 1])[1] == 0 and solo.get(x, [0, 0])[0] >= 3 for x in (a, b))
        if yes >= config.trial_min and no == 0 and free:
            out.append(Proposal("mutex", "", (a, b), "inferred", yes, 0,
                                f"enabling both {a} and {b} switches one of them off", effect=1.0,
                                p=2.0 ** -yes, q=2.0 ** -yes, detail={"trials": yes}))
    for (f, g), (blocked, stuck_wo, stuck_w, blocked_w) in sorted(req.items()):
        if blocked >= config.trial_min and stuck_wo == 0 and blocked_w == 0 and stuck_w >= 3:
            out.append(Proposal("requires", f, (g,), "inferred", blocked + stuck_w, 0,
                                f"{f} cannot be activated unless {g} is active", effect=1.0,
                                p=2.0 ** -blocked, q=2.0 ** -blocked,
                                detail={"blocked_without": blocked, "stuck_with": stuck_w}))
    if rejected:
        out = [p for p in out if p.fingerprint not in rejected or p.evidence_strength() >= 2 * rejected[p.fingerprint]]
    return out


def impact(project: Project, proposal: Proposal) -> dict[str, Any]:
    """valid_states before -> after if `proposal` were approved (via the real StateSpace)."""
    from .states import StateSpace
    before = StateSpace(project).size
    try:
        after_p = replace(proposal, status="approved").apply(project)
        after = StateSpace(after_p).size
    except Exception:           # noqa: BLE001
        after = before
    pct = None if not before or after is None else round(100.0 * (before - after) / before, 1)
    return {"valid_before": before, "valid_after": after, "removed_pct": pct}


def apply(project: Project, proposals: Iterable[Proposal]) -> Project:
    """Only `approved` proposals touch the project; every result passes project.validate (errors raise)."""
    out = project
    for pr in proposals:
        if pr.status != "approved" or pr.kind == "enable_inverse":
            continue
        out = pr.apply(out)
    if out is not project:
        from . import project as P
        errs = [e for e in P.validate(out) if not e.startswith("warning:")]
        if errs:
            raise ValueError("approved proposals produce an invalid project: " + "; ".join(errs[:3]))
    return out


def save_rejected(path: str, rejected: dict[str, int]) -> None:
    from .evidence import atomic_write_text
    from pathlib import Path
    atomic_write_text(Path(path), json.dumps(rejected, sort_keys=True))


def load_rejected(path: str) -> dict[str, int]:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return {str(k): int(v) for k, v in d.items()}
    except (OSError, ValueError, AttributeError):
        return {}


# ------------------------------------------------------------------ active gathering (adapter)
def default_probe(adapter: Any, project: Project) -> dict[str, dict[str, bool]]:
    """Read {control id: {exists, visible, enabled}} through the optional `probe(selectors)` hook, else
    `selector_enabled` per control; adapters with neither return {} (inference then has no evidence)."""
    sels = {c.id: c.selector for c in project.controls if not c.skip}
    fn = getattr(adapter, "probe", None)
    if fn is not None:
        return fn(sels)
    fe = getattr(adapter, "selector_enabled", None)
    if fe is None:
        return {}
    return {k: {"exists": True, "visible": True, "enabled": bool(fe(s))} for k, s in sels.items()}


def gather(adapter: Any, project: Project, config: InferConfig = InferConfig(), *,
           probe: Callable[[Any, Project], dict] | None = None, progress: Callable[[str], None] | None = None,
           cancel: Callable[[], bool] | None = None, clock: Callable[[], float] = time.monotonic
           ) -> tuple[list[ElementObs], list[Trial], dict[str, Any]]:
    """Active interventions from a known baseline. Never touches `dangerous`/action features; the baseline is
    restored in `finally` (exception and cancel included). Returns (observations, trials, info) where info has
    `settle_suggestion_ms`, `noisy_cells`, `contexts`."""
    probe = probe or default_probe
    feats = [f for f in project.features if f.enabled and not f.dangerous and f.kind != "action"]
    fmap = {f.id: f for f in feats}
    ng = None
    try:
        from .constraints import compile_constraints
        ng = compile_constraints(project.features, project.constraints) if project.constraints else None
    except Exception:           # noqa: BLE001
        pass
    settle = max(1, project.run.settle_ms)
    k = config.contexts or max(50, 10 * len(feats))
    rng = random.Random(config.seed)
    deadline = clock() + config.max_seconds
    obs: list[ElementObs] = []
    trials: list[Trial] = []
    info: dict[str, Any] = {"settle_suggestion_ms": None, "noisy_cells": 0, "contexts": 0}
    getter = getattr(adapter, "get_feature_state", None)

    def stop() -> bool:
        return (cancel is not None and cancel()) or clock() > deadline

    def apply_ctx(assign: dict[str, str]) -> dict[str, str]:
        adapter.reset(project)
        for fid, st in assign.items():
            adapter.set_feature_state(fmap[fid], st)
        if getter is None:
            return dict(assign)
        return {fid: getter(fmap[fid]) for fid in assign}

    def read_all(ctx: dict[str, str]) -> None:
        reads: dict[str, list[bool]] = {}
        times: list[float] = []
        elapsed = 0
        done = 0
        for i, mult in enumerate(config.delay_steps):
            wait = settle * mult - elapsed
            adapter.wait_quiescent(max(0, wait))
            elapsed += max(0, wait)
            times.append(elapsed)
            snap = probe(adapter, project)
            for el, props in snap.items():
                reads.setdefault(el, []).append(bool(props.get("enabled", True) and props.get("exists", True)
                                                     and props.get("visible", True)))
            done = i + 1
            if done >= 3 and all(len(set(r[-3:])) == 1 for r in reads.values()):
                break
        for el, r in reads.items():
            last = r[-3:] if len(r) >= 3 else r
            if len(r) < 3:
                obs.append(ElementObs(dict(ctx), el, True, True, r[-1], times[-1], True))
                info["noisy_cells"] += 1
                continue
            val, noisy = _majority(last)
            first_ok = next((times[i] for i in range(len(r)) if all(x == val for x in r[i:])), times[-1])
            if noisy and len(set(last)) > 1 and r[-1] != r[-2]:
                info["noisy_cells"] += 1
            if first_ok > settle:
                info["settle_suggestion_ms"] = max(info["settle_suggestion_ms"] or 0, int(first_ok))
            obs.append(ElementObs(dict(ctx), el, True, True, val, first_ok, False))

    try:
        adapter.reset(project)
        read_all({f.id: f.default_state for f in feats})
        for f in feats:                                   # OFAT sweep
            for st in f.states:
                if st == f.default_state or stop():
                    continue
                ctx = {g.id: g.default_state for g in feats}
                ctx[f.id] = st
                apply_ctx({f.id: st})
                read_all(ctx)
        n = 0
        tries = 0
        while n < k and tries < 20 * k and not stop():   # randomised context phase
            tries += 1
            ctx = {f.id: f.states[rng.randrange(len(f.states))] for f in feats}
            if ng is not None and not ng.is_valid({**{x.id: x.default_state for x in project.features}, **ctx}):
                continue
            n += 1
            apply_ctx({a: s for a, s in ctx.items() if s != fmap[a].default_state})
            read_all(ctx)
            if progress is not None and n % 10 == 0:
                progress(f"context {n}/{k}")
        info["contexts"] = n
        # deterministic probes for mutex / requires: pair attempts and prerequisite attempts
        on_state = {f.id: next((s for s in f.states if s != f.states[0]), None) for f in feats}
        for i, fa in enumerate(feats):
            for fb in feats[i + 1:]:
                if stop() or on_state[fa.id] is None or on_state[fb.id] is None:
                    continue
                for rep in range(config.trial_min + config.counter_probes):
                    if rep % 2 == 0:
                        a, b = fa, fb
                    else:
                        a, b = fb, fa
                    asked = {a.id: on_state[a.id], b.id: on_state[b.id]}
                    rb = apply_ctx(asked)
                    adapter.wait_quiescent(settle)
                    trials.append(Trial(asked, dict(getter and {x: getter(fmap[x]) for x in asked} or rb)))
        for fa in feats:                                   # f alone, with and without each g
            if on_state[fa.id] is None:
                continue
            for fb in feats:
                if fb is fa or stop() or on_state[fb.id] is None:
                    continue
                for rep in range(config.trial_min):
                    for g_on in (False, True):
                        asked = {fb.id: on_state[fb.id]} if g_on else {}
                        asked[fa.id] = on_state[fa.id]
                        apply_ctx(asked)
                        adapter.wait_quiescent(settle)
                        rb = {x: getter(fmap[x]) for x in asked} if getter else dict(asked)
                        key = dict(asked)
                        if not g_on:
                            key[fb.id] = fb.states[0]
                        trials.append(Trial(key, rb, "prereq" if g_on else "pair", fa.id))
    finally:
        try:
            adapter.reset(project)
        except Exception:       # noqa: BLE001
            pass
    return obs, trials, info


def infer_statistical(project: Project, adapter: Any, config: InferConfig = InferConfig(), *,
                      progress: Callable[[str], None] | None = None, cancel: Callable[[], bool] | None = None,
                      rejected: dict[str, int] | None = None, probe: Callable | None = None,
                      clock: Callable[[], float] = time.monotonic) -> list[Proposal]:
    """Spec entry point: gather + analyze + impact. Proposals are 'pending' until the human approves."""
    obs, trials, info = gather(adapter, project, config, probe=probe, progress=progress, cancel=cancel, clock=clock)
    props = analyze(obs, project.features, config, project, rejected) + \
        analyze_trials(trials, project.features, config, rejected)
    out = []
    for p in props:
        ev = dict(p.detail)
        if info.get("settle_suggestion_ms"):
            ev["suggest_settle_ms"] = info["settle_suggestion_ms"]
        out.append(replace(p, impact=impact(project, p), detail=ev))
    return out
