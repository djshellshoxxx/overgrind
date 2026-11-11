"""chaos: stateful random walk over features and controls.

Random single-feature transitions (always to a valid state), shuffled action order,
dwell-time jitter, a few control mutations per step and an occasional full reset
(tag ``reset``). Makes no epoch/coverage claims.
"""
from __future__ import annotations

from .. import domain as D
from ..types import Coverage
from . import BaseGenerator


class Generator(BaseGenerator):
    name = "chaos"

    def __init__(self, project):
        super().__init__(project)
        self.actions = [f.id for f in project.features if f.enabled and not f.dangerous and f.kind == "action"]
        self.movable = [k for k in range(self.space.n) if self.space.radix[k] >= 2]
        interval = max(2, int(self.cfg.reset_interval or 50))
        self.reset_p = float(self.opts.get("reset_rate", 1.0 / interval))
        self.cur: list[int] | None = None
        self.vals: dict = {}
        self.steps = 0
        self.resets = 0

    def _baseline(self, rng):
        sp = self.space
        idx = sp.default_index()
        if not sp.is_valid_idx(idx):
            idx = sp.sample(rng)
        vals = {}
        for c, dom, cid in zip(self.plan.controls, self.plan.domains, self.plan.ids):
            vals[cid] = c.default if c.default is not None and D.contains(dom, c.default) else D.sample(dom, rng)
        return idx, vals

    def _next_case(self):
        sp, plan = self.space, self.plan
        rng = self.rng(self.seq)
        tags = ["chaos"]
        order: list[str] = []
        if self.cur is None or rng.random() < self.reset_p:
            self.cur, self.vals = self._baseline(rng)
            tags.append("reset")
            self.resets += 1
            order = list(sp.ids)
        else:
            changed = None
            if self.movable:
                for _ in range(8):
                    k = rng.choice(self.movable)
                    s = rng.randrange(sp.radix[k] - 1)
                    s += s >= self.cur[k]
                    cand = list(self.cur)
                    cand[k] = s
                    if sp.is_valid_idx(cand):
                        self.cur = cand
                        changed = sp.ids[k]
                        break
            acts = [a for a in self.actions if rng.random() < 0.5]
            order = ([changed] if changed else []) + acts
            rng.shuffle(order)
            for _ in range(rng.randint(0, min(2, plan.n)) if plan.n else 0):
                c = rng.randrange(plan.n)
                self.vals[plan.ids[c]] = D.sample(plan.domains[c], rng)
        r = rng.random()
        dwell = rng.randint(500, 2000) if r < 0.05 else min(2000, int(rng.expovariate(1 / 80.0)))
        self.steps += 1
        return self.make_case(self.cur, dict(self.vals), order=order or sp.ids, dwell_ms=dwell, tags=tags)

    def coverage(self) -> Coverage:
        g = (f"Stateful random walk: {self.steps} steps, {self.resets} resets; no epoch or coverage "
             "guarantee is claimed.")
        return Coverage(guarantee=g)

    def _ck_extra(self):
        return {"cur": self.cur, "vals": self.vals, "steps": self.steps, "resets": self.resets}

    def _ck_load(self, st):
        self.cur = list(st["cur"]) if st.get("cur") is not None else None
        self.vals = dict(st.get("vals", {}))
        self.steps = int(st.get("steps", 0))
        self.resets = int(st.get("resets", 0))
