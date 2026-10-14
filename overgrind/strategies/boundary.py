"""boundary: boundary values (min/max/near/default/mid) crossed with random valid states.

Every case focuses one control (round-robin) and cycles it through all of its boundary
values; the other controls get a boundary value or an ordinary sample at random.
Out-of-domain probes are produced only when ``RunConfig.invalid_values`` is set and
are tagged ``invalid``.
"""
from __future__ import annotations

from .. import domain as D
from ..types import Coverage
from . import BaseGenerator


class Generator(BaseGenerator):
    name = "boundary"

    def __init__(self, project):
        super().__init__(project)
        self.visited: set[tuple[int, int]] = set()       # bounded by total boundary values
        self.invalid_p = float(self.opts.get("invalid_rate", 0.15))

    def _next_case(self):
        sp, plan = self.space, self.plan
        k = self.seq
        rng = self.rng(k)
        idx = sp.sample(rng)
        values = {}
        tags = ["boundary"]
        if plan.n:
            bl = plan.boundary()
            focus = k % plan.n
            for c in range(plan.n):
                if not bl[c]:
                    values[plan.ids[c]] = D.sample(plan.domains[c], rng)
                    continue
                if c == focus:
                    bi = (k // plan.n) % len(bl[c])
                elif rng.random() < 0.5:
                    bi = rng.randrange(len(bl[c]))
                else:
                    values[plan.ids[c]] = D.sample(plan.domains[c], rng)
                    continue
                values[plan.ids[c]] = bl[c][bi]
                self.visited.add((c, bi))
            if self.cfg.invalid_values and rng.random() < self.invalid_p:
                c = rng.randrange(plan.n)
                fn = getattr(D, "invalid_values", None)
                bad = fn(plan.domains[c], rng) if fn else []
                if bad:
                    values[plan.ids[c]] = rng.choice(bad)
                    tags.append("invalid")
        return self.make_case(idx, values, tags=tags)

    def coverage(self) -> Coverage:
        plan = self.plan
        total = sum(len(b) for b in plan.boundary()) if plan.n else 0
        inv = "on" if self.cfg.invalid_values else "off"
        g = (f"Boundary values: {len(self.visited)} of {total} (control, boundary value) pairs seen; feature states "
             f"drawn uniformly from valid states (no state coverage claimed); invalid-value probing {inv}.")
        return Coverage(buckets_total=total, buckets_visited=len(self.visited), guarantee=g)

    def _ck_extra(self):
        return {"visited": sorted([c, b] for c, b in self.visited)}

    def _ck_load(self, st):
        self.visited = {(int(c), int(b)) for c, b in st.get("visited", [])}
