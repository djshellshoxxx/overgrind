"""Case-generation strategies (all implement ``types.CaseGenerator``).

Shared plumbing lives here: ``BaseGenerator`` (iteration, seq, checkpoint envelope)
and ``ControlPlan`` (which controls are fuzzed, value sampling, buckets, scope).
Per-case randomness is derived from ``(seed, epoch/sweep, index)`` so a checkpoint is
a handful of integers and restore reproduces the uninterrupted stream exactly.
"""
from __future__ import annotations

import importlib
import random
from typing import Any

from .. import domain as D
from ..states import StateSpace, case_rng
from ..types import Case, Coverage, Outcome, Project, Value

STRATEGY_NAMES = ("state_cover", "ordered_pairs", "twise", "boundary", "adaptive", "chaos")
_MODULES = {n: f"{__name__}.{n}" for n in STRATEGY_NAMES}


def strategy_class(name: str):
    """Resolve a strategy name to its generator class (ValueError if unknown)."""
    if name not in _MODULES:
        raise ValueError(f"unknown strategy {name!r}; choose one of {', '.join(STRATEGY_NAMES)}")
    return importlib.import_module(_MODULES[name]).Generator


class ControlPlan:
    """Fuzzable controls (not skipped, not read-only) and helpers to draw values."""

    def __init__(self, project: Project, default_buckets: int = 3):
        self.controls = [c for c in project.controls if not c.skip and not c.read_only]
        self.domains = [D.effective(c) for c in self.controls]
        self.ids = [c.id for c in self.controls]
        self.n = len(self.controls)
        self.nb = default_buckets
        self._buckets: list[list[tuple]] | None = None
        self._boundary: list[list[Value]] | None = None
        self.scope = [tuple(c.scope) for c in self.controls]
        feats = {f.id: f for f in project.features}
        self._fixed_active: dict[str, bool] = {}
        for fid, f in feats.items():
            self._fixed_active[fid] = bool(f.states) and f.default_state != f.states[0]
            if f.kind == "action":
                self._fixed_active[fid] = True

    # ------------------------------------------------------------ buckets
    def buckets(self) -> list[list[tuple]]:
        if self._buckets is None:
            self._buckets = [D.buckets(d, self.nb) for d in self.domains]
        return self._buckets

    def boundary(self) -> list[list[Value]]:
        if self._boundary is None:
            self._boundary = [D.boundary_values(d) for d in self.domains]
        return self._boundary

    # ------------------------------------------------------------ values
    def sample_all(self, rng: random.Random) -> dict[str, Value]:
        return {i: D.sample(d, rng) for i, d in zip(self.ids, self.domains)}

    def sample_bucket(self, ci: int, bucket: tuple, rng: random.Random) -> Value:
        dom = self.domains[ci]
        fn = getattr(D, "sample_bucket", None)
        if fn is not None:
            try:
                return fn(dom, bucket, rng)
            except Exception:
                pass
        return D.sample(dom, rng)

    def in_scope(self, ci: int, state: dict[str, str], states_of: dict[str, tuple[str, ...]]) -> bool:
        """Scope semantics: every scope feature must be active (state != states[0])."""
        for fid in self.scope[ci]:
            if fid in state:
                st = states_of.get(fid)
                if st is not None and state[fid] == st[0]:
                    return False
            elif not self._fixed_active.get(fid, True):
                return False
        return True


class BaseGenerator:
    """Iterator protocol + checkpoint envelope. Subclasses implement ``_next_case``."""
    name = "base"
    needs_states = True

    def __init__(self, project: Project):
        self.project = project
        self.cfg = project.run
        self.seed = int(self.cfg.seed)
        self.opts: dict[str, Any] = dict(self.cfg.strategy_options or {})
        self.space = StateSpace(project)
        self.plan = ControlPlan(project, int(self.opts.get("buckets", 3)))
        self.states_of = {i: s for i, s in zip(self.space.ids, self.space.states)}
        self.seq = 0
        if self.needs_states and self.space.unsatisfiable:
            raise ValueError("constraints are unsatisfiable: zero valid feature states")

    # CaseGenerator protocol ------------------------------------------------
    def __iter__(self):
        return self

    def __next__(self) -> Case:
        case = self._next_case()
        self.seq += 1
        return case

    def _next_case(self) -> Case:           # pragma: no cover - abstract
        raise NotImplementedError

    def coverage(self) -> Coverage:          # pragma: no cover - abstract
        raise NotImplementedError

    def feedback(self, case: Case, outcome: Outcome) -> None:
        return None

    def checkpoint(self) -> dict[str, Any]:
        d = {"v": 1, "strategy": self.name, "seed": self.seed, "seq": self.seq}
        d.update(self._ck_extra())
        return d

    def restore(self, state: dict[str, Any]) -> None:
        if state.get("strategy") != self.name:
            raise ValueError(f"checkpoint is for strategy {state.get('strategy')!r}, not {self.name!r}")
        self.seed = int(state.get("seed", self.seed))
        self.seq = int(state.get("seq", 0))
        self._ck_load(state)

    def _ck_extra(self) -> dict[str, Any]:
        return {}

    def _ck_load(self, state: dict[str, Any]) -> None:
        return None

    # helpers -------------------------------------------------------------
    def rng(self, *parts: int) -> random.Random:
        return case_rng(self.seed, *parts)

    def make_case(self, idx, values, order=(), dwell_ms=0, tags=()) -> Case:
        return Case(seq=self.seq, state=self.space.state_dict(idx), values=values,
                    order=tuple(order) or self.space.ids, dwell_ms=dwell_ms, tags=tuple(tags))
