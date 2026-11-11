"""ordered_pairs (legacy): A->B feature sequences over F x F, as unbounded repeating sweeps.

Every sweep visits all F^2 ordered pairs (A->A, A->B, B->A ...) exactly once in a fresh
keyed-shuffled order. No 250k cap: sweeps repeat forever with new control values.
The case's ``order`` is ``(A, B)``; ``state`` is a *valid* configuration with A and B
active when constraints allow (otherwise only A, otherwise the default state).
"""
from __future__ import annotations

from ..states import KeyedPermutation, derive_seed
from ..types import Coverage
from . import BaseGenerator


class Generator(BaseGenerator):
    name = "ordered_pairs"

    def __init__(self, project):
        super().__init__(project)
        self.items = [f.id for f in project.features if f.enabled and not f.dangerous]
        if not self.items:
            raise ValueError("ordered_pairs needs at least one enabled, non-dangerous feature")
        self.F = len(self.items)
        self.total = self.F * self.F
        self.sweep = 0
        self.index = 0
        self._perm = None
        self._perm_sweep = -1
        self._pos = {fid: k for k, fid in enumerate(self.space.ids)}

    def _activate(self, idx, fid, rng):
        k = self._pos.get(fid)
        if k is None or self.space.radix[k] < 2:
            return idx
        out = list(idx)
        out[k] = 1 + rng.randrange(self.space.radix[k] - 1)
        return out

    def _next_case(self):
        sp = self.space
        if self._perm is None or self._perm_sweep != self.sweep:
            self._perm = KeyedPermutation(self.total, derive_seed(self.seed, self.sweep, 0x9A125))
            self._perm_sweep = self.sweep
        sw, ix = self.sweep, self.index
        j = self._perm(ix)
        a, b = self.items[j // self.F], self.items[j % self.F]
        rng = self.rng(sw, ix)
        base = sp.default_index()
        if not sp.is_valid_idx(base):
            base = sp.sample(rng) if sp.size != 0 else base
        idx = base
        for cand in (self._activate(self._activate(base, a, rng), b, rng), self._activate(base, a, rng)):
            if sp.is_valid_idx(cand):
                idx = cand
                break
        values = self.plan.sample_all(rng) if self.plan.n else {}
        self.index += 1
        if self.index >= self.total:
            self.index = 0
            self.sweep += 1
        return self.make_case(idx, values, order=(a, b), tags=("pair", f"sweep:{sw + 1}"))

    def coverage(self) -> Coverage:
        sw, ix, tot = self.sweep, self.index, self.total
        g = (f"Ordered feature pairs A->B (incl. A->A): {tot} per sweep, {sw} full sweep{'s' if sw != 1 else ''} "
             f"completed (current {ix}/{tot}); tests sequences, not all feature-state combinations; "
             "control values sampled.")
        return Coverage(states_total=None, states_visited=0, epochs=sw, tway_t=2, tway_total=tot,
                        tway_covered=tot if sw >= 1 else ix, guarantee=g)

    def _ck_extra(self):
        return {"sweep": self.sweep, "index": self.index}

    def _ck_load(self, st):
        self.sweep = int(st.get("sweep", 0))
        self.index = int(st.get("index", 0))
        self._perm = None
