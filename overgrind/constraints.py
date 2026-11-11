"""Constraint compiler: mutex / requires / forbid  ->  nogoods (invalid partial assignments).

A *nogood* is a partial assignment ``{feature: state}``; any full assignment that
contains every literal of a nogood is invalid. "Active" means ``state != states[0]``.

Features that are not part of the driven state space (disabled, dangerous, action)
are *fixed*: they are held at their default state, so literals on them are
simplified away at compile time (a literal that can never match drops the nogood,
a literal that always matches is removed; an empty nogood makes the set
unsatisfiable).
"""
from __future__ import annotations

import itertools
import random
from typing import Iterable, Mapping, Sequence

from .types import ConstraintDef, FeatureDef

Nogood = tuple[tuple[str, str], ...]          # sorted ((feature, state), ...)


def _is_active(f: FeatureDef, state: str) -> bool:
    return bool(f.states) and state != f.states[0]


def _active_states(f: FeatureDef) -> tuple[str, ...]:
    return tuple(f.states[1:])


class NogoodSet:
    """Compiled constraints with a fast validity check and a small exact solver."""

    def __init__(self, nogoods: Iterable[Nogood], domains: Mapping[str, Sequence[str]],
                 impossible: bool = False):
        self.domains: dict[str, tuple[str, ...]] = {k: tuple(v) for k, v in domains.items()}
        self.impossible = impossible              # an empty nogood was produced
        uniq = sorted(set(nogoods), key=lambda n: (len(n), n))
        self.nogoods: list[Nogood] = self._minimise(uniq)
        self._by_lit: dict[tuple[str, str], list[Nogood]] = {}
        for ng in self.nogoods:
            for lit in ng:
                self._by_lit.setdefault(lit, []).append(ng)
        self.involved: frozenset[str] = frozenset(f for ng in self.nogoods for f, _ in ng)

    @staticmethod
    def _minimise(sorted_ngs: list[Nogood]) -> list[Nogood]:
        if len(sorted_ngs) > 3000:
            return sorted_ngs
        kept: list[tuple[Nogood, frozenset]] = []
        for ng in sorted_ngs:
            s = frozenset(ng)
            if not any(k <= s for _, k in kept):
                kept.append((ng, s))
        return [ng for ng, _ in kept]

    def __len__(self) -> int:
        return len(self.nogoods)

    # ------------------------------------------------------------ checks
    def violated(self, assignment: Mapping[str, str]) -> Nogood | None:
        """First nogood fully contained in the (possibly partial) assignment."""
        if self.impossible:
            return ()
        for lit in assignment.items():
            for ng in self._by_lit.get(lit, ()):
                if all(assignment.get(f) == s for f, s in ng):
                    return ng
        return None

    def is_valid(self, assignment: Mapping[str, str]) -> bool:
        """True iff no nogood is contained in the assignment."""
        return not self.impossible and self.violated(assignment) is None

    # ------------------------------------------------------------ solver
    def find_valid(self, fixed: Mapping[str, str] | None = None, rng: random.Random | None = None,
                   budget: int = 200_000, features: Sequence[str] | None = None,
                   accept=None) -> dict[str, str] | None | bool:
        """Find a full valid assignment extending ``fixed`` (backtracking).

        Returns the assignment, ``None`` if proven none exists, ``False`` if the node
        budget ran out (unknown). ``accept(assignment)`` may veto complete assignments.
        """
        if self.impossible:
            return None
        names = list(features if features is not None else self.domains)
        fixed = dict(fixed or {})
        cur: dict[str, str] = {}
        for f, s in fixed.items():
            if f in self.domains and s not in self.domains[f]:
                return None
            cur[f] = s
        if self.violated(cur) is not None:
            return None
        rng = rng or random.Random(0)
        todo = [n for n in names if n not in cur]
        todo.sort(key=lambda n: (n not in self.involved, rng.random()))   # constrained first
        nodes = [0]

        def rec(i: int) -> dict[str, str] | None | bool:
            if i == len(todo):
                if accept is not None and not accept(cur):
                    return None
                return dict(cur)
            f = todo[i]
            opts = list(self.domains[f])
            rng.shuffle(opts)
            unknown = False
            for s in opts:
                nodes[0] += 1
                if nodes[0] > budget:
                    return False
                cur[f] = s
                ok = True
                for ng in self._by_lit.get((f, s), ()):
                    if all(cur.get(g) == t for g, t in ng):
                        ok = False
                        break
                if ok:
                    r = rec(i + 1)
                    if r:
                        return r
                    if r is False:
                        unknown = True
                        del cur[f]
                        return False
                del cur[f]
            return False if unknown else None

        return rec(0)

    def unsatisfiable(self, budget: int = 200_000, accept=None) -> bool | None:
        """True if provably zero valid states, False if one exists, None if undecided."""
        r = self.find_valid(budget=budget, accept=accept)
        if r is False:
            return None
        return r is None


def compile_constraints(features: Sequence[FeatureDef], constraints: Sequence[ConstraintDef],
                        driven: Iterable[str] | None = None) -> NogoodSet:
    """Compile ``constraints`` over ``features``.

    ``driven`` = ids of features that vary (default: every enabled toggle/multistate
    feature). All other known features are fixed at ``default_state``. Unknown ids
    are ignored (project validation reports them).
    """
    by_id = {f.id: f for f in features}
    if driven is None:
        driven = [f.id for f in features if f.enabled and f.kind in ("toggle", "multistate")]
    driven_set = set(driven)
    domains = {i: by_id[i].states for i in driven_set if i in by_id}
    fixed = {f.id: f.default_state for f in features if f.id not in driven_set}
    out: list[Nogood] = []
    impossible = False

    def add(literals: Iterable[tuple[str, str]]) -> None:
        nonlocal impossible
        lits: dict[str, str] = {}
        for f, s in literals:
            if f in lits and lits[f] != s:
                return                                  # contradictory -> can never match
            lits[f] = s
        keep: list[tuple[str, str]] = []
        for f, s in lits.items():
            if f in driven_set:
                if s not in by_id[f].states:
                    return                              # unknown state -> never matches
                keep.append((f, s))
            elif f in fixed:
                if fixed[f] != s:
                    return                              # fixed feature never in that state
            else:
                return                                  # unknown feature
        if not keep:
            impossible = True
            return
        out.append(tuple(sorted(keep)))

    def lit_states(fid: str, active: bool) -> tuple[str, ...] | None:
        f = by_id.get(fid)
        if f is None:
            return None
        return _active_states(f) if active else (f.states[0],)

    for c in constraints:
        if c.kind == "mutex":
            fs = [x for x in dict.fromkeys(c.features) if x in by_id]
            for a, b in itertools.combinations(fs, 2):
                for sa in lit_states(a, True):
                    for sb in lit_states(b, True):
                        add([(a, sa), (b, sb)])
                # fixed-but-active features are handled by simplification in add()
        elif c.kind == "requires":
            for f in c.features:
                if f not in by_id:
                    continue
                for n in c.needs:
                    if n not in by_id or n == f:
                        continue
                    for sf in lit_states(f, True):
                        for sn in lit_states(n, False):
                            add([(f, sf), (n, sn)])
        elif c.kind == "forbid":
            if c.assignment and all(k in by_id for k in c.assignment):
                add(list(c.assignment.items()))
        # unknown constraint kinds are ignored here (project.validate reports them)
    # fixed features can be "active" under a mutex/requires: add() already evaluated
    # them via their default state because lit_states yields concrete literal states.
    return NogoodSet(out, domains, impossible)
