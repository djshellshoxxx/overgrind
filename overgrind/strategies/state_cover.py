"""state_cover (default): visit every valid feature state once per epoch, random order.

Each epoch is a fresh keyed permutation of the valid-state index space (seeded by
seed+epoch); nothing is materialised, so memory is O(1) whatever the space size.
Control values are drawn fresh (CORE ``domain.sample``) on every visit. If the
space exceeds ``strategy_options['budget']`` (default 1_000_000) the strategy
falls back to uniform valid sampling and reports ``states_total=None``.
"""
from __future__ import annotations

from ..states import derive_seed, pow2_text
from ..types import Coverage
from . import BaseGenerator

DEFAULT_BUDGET = 1_000_000


class Generator(BaseGenerator):
    name = "state_cover"

    def __init__(self, project):
        super().__init__(project)
        self.budget = int(self.opts.get("budget", DEFAULT_BUDGET))
        n = self.space.size
        self.epoch_mode = n is not None and n <= self.budget
        self.n = n
        self.epoch = 0           # 0-based; completed epochs == self.epoch
        self.index = 0
        self._perm = None
        self._perm_epoch = -1
        self.sampled = 0

    # ------------------------------------------------------------ stream
    def _next_case(self):
        sp = self.space
        if self.epoch_mode:
            if self._perm is None or self._perm_epoch != self.epoch:
                self._perm = sp.permutation(derive_seed(self.seed, self.epoch, 0x57A7E))
                self._perm_epoch = self.epoch
            ep, ix = self.epoch, self.index
            idx = sp.unrank(self._perm(ix))
            rng = self.rng(ep, ix) if self.plan.n else None
            values = self.plan.sample_all(rng) if rng is not None else {}
            tags = (f"epoch:{ep + 1}",)
            self.index += 1
            if self.index >= self.n:
                self.index = 0
                self.epoch += 1
            return self.make_case(idx, values, tags=tags)
        rng = self.rng(self.sampled, 1)
        idx = sp.sample(rng)
        values = self.plan.sample_all(rng)
        self.sampled += 1
        return self.make_case(idx, values, tags=("sampled",))

    # ------------------------------------------------------------ coverage
    def coverage(self) -> Coverage:
        if self.epoch_mode:
            n, ep, ix = self.n, self.epoch, self.index
            visited = n if ep >= 1 else ix
            if ep == 0:
                g = (f"No full epoch yet: {ix} of {n} valid feature states visited (each at most once so far); "
                     "control values sampled per visit, not exhaustive.")
            else:
                g = (f"All {n} valid feature states visited {ep} time{'s' if ep != 1 else ''}"
                     f" (epoch {ep + 1}: {ix}/{n}); control values sampled, not exhaustive.")
            return Coverage(states_total=n, states_visited=visited, epochs=ep, guarantee=g)
        size = self.space.size
        txt = (f"about {pow2_text(size)} valid feature states" if size is not None
               else "an uncountable number of valid feature states")
        g = (f"Space too large to enumerate within budget {self.budget} ({txt}): {self.sampled} cases drawn "
             "uniformly from valid states; no coverage guarantee, control values sampled.")
        return Coverage(states_total=None, states_visited=0, epochs=0, guarantee=g)

    # ------------------------------------------------------------ checkpoint
    def _ck_extra(self):
        return {"epoch": self.epoch, "index": self.index, "sampled": self.sampled}

    def _ck_load(self, st):
        self.epoch = int(st.get("epoch", 0))
        self.index = int(st.get("index", 0))
        self.sampled = int(st.get("sampled", 0))
        self._perm = None
