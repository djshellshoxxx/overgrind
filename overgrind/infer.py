"""DIAG: Dependency Inference. Observe-and-propose; proposals are never auto-applied."""
from __future__ import annotations

from dataclasses import dataclass, replace
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

    def apply(self, project: Project) -> Project:
        """Immutable update; the original project is untouched. Idempotent."""
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
    defaults = {f.id: f.default_state for f in project.features} if project else {}

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
