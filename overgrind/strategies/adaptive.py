"""adaptive: favour rarely visited states/buckets and recently failing neighbourhoods.

Visit counts live in a count-min sketch (fixed size, bounded memory, approximate).
Each case is one of: uniform exploration (>= 15%, so nothing starves), a mutation of a
recently failing case (``feedback``), or the rarest of K random candidates.
"""
from __future__ import annotations

import base64
import zlib
from array import array

from .. import domain as D
from ..states import M64, _GOLD, mix64
from ..types import Coverage, Outcome
from . import BaseGenerator

DEPTH = 3
WIDTH = 4096
HOT_CAP = 32


class Generator(BaseGenerator):
    name = "adaptive"

    def __init__(self, project):
        super().__init__(project)
        self.eps = min(0.5, max(0.10, float(self.opts.get("explore", 0.15))))
        self.hot_p = float(self.opts.get("failure_focus", 0.2))
        self.k = max(2, int(self.opts.get("candidates", 8)))
        self.sketch = array("H", [0]) * (DEPTH * WIDTH)
        self.hot: list[dict] = []
        self.n_uniform = self.n_hot = self.n_rare = 0

    # ---------------------------------------------------------- sketch
    @staticmethod
    def _key(kind: int, *parts: int) -> int:
        h = mix64(kind + 1)
        for p in parts:
            h = mix64(h ^ ((p + 1) * _GOLD & M64))
        return h

    def _est(self, key: int) -> int:
        return min(self.sketch[j * WIDTH + mix64(key + j * _GOLD) % WIDTH] for j in range(DEPTH))

    def _bump(self, key: int) -> None:
        sk = self.sketch
        for j in range(DEPTH):
            i = j * WIDTH + mix64(key + j * _GOLD) % WIDTH
            if sk[i] < 65535:
                sk[i] += 1

    def _state_key(self, idx) -> int:
        return self._key(1, *idx)

    def _bucket_of(self, c: int, v) -> int:
        for b, (_, lo, hi) in enumerate(self.plan.buckets()[c]):
            try:
                if lo == hi == v or (self.plan.domains[c].kind in ("integer", "float") and lo <= v <= hi):
                    return b
                if self.plan.domains[c].kind == "text" and lo <= len(v) <= hi:
                    return b
            except TypeError:
                continue
        return 0

    def _keys(self, idx, values):
        ks = [self._state_key(idx)]
        for c, cid in enumerate(self.plan.ids):
            ks.append(self._key(2, c, self._bucket_of(c, values[cid])))
        return ks

    # ---------------------------------------------------------- stream
    def _draw(self, rng):
        idx = self.space.sample(rng)
        return idx, self.plan.sample_all(rng)

    def _next_case(self):
        sp, plan = self.space, self.plan
        rng = self.rng(self.seq)
        r = rng.random()
        if r < self.eps:
            idx, values = self._draw(rng)
            tags = ("adaptive", "explore")
            self.n_uniform += 1
        elif self.hot and r < self.eps + self.hot_p:
            h = rng.choice(self.hot)
            idx = list(h["idx"])
            movable = [k for k in range(sp.n) if sp.radix[k] >= 2]
            if movable and rng.random() < 0.6:
                k = rng.choice(movable)
                cand = list(idx)
                cand[k] = rng.randrange(sp.radix[k])
                if sp.is_valid_idx(cand):
                    idx = cand
            values = dict(h["values"])
            for c, cid in enumerate(plan.ids):
                if cid not in values or rng.random() < 0.3:
                    values[cid] = D.sample(plan.domains[c], rng)
            tags = ("adaptive", "neighbour")
            self.n_hot += 1
        else:
            best = None
            for _ in range(self.k):
                idx_c, vals_c = self._draw(rng)
                ks = self._keys(idx_c, vals_c)
                score = self._est(ks[0]) * 4 + sum(self._est(x) for x in ks[1:])
                if best is None or score < best[0]:
                    best = (score, idx_c, vals_c)
            _, idx, values = best
            tags = ("adaptive", "rare")
            self.n_rare += 1
        for key in self._keys(idx, values):
            self._bump(key)
        return self.make_case(idx, values, tags=tags)

    def feedback(self, case, outcome: Outcome) -> None:
        if outcome.ok:
            return
        try:
            idx = self.space.to_index(case.state)
        except (KeyError, ValueError):
            return
        self.hot.append({"idx": idx, "values": dict(case.values)})
        if len(self.hot) > HOT_CAP:
            self.hot.pop(0)

    def coverage(self) -> Coverage:
        g = (f"Adaptive (count-min sketch, approximate): {self.n_rare} rarity-driven, {self.n_hot} failure-neighbour "
             f"and {self.n_uniform} uniform-exploration cases ({self.hot.__len__()} hot cases tracked); "
             "no exhaustive coverage guarantee.")
        return Coverage(guarantee=g)

    # ---------------------------------------------------------- checkpoint
    def _ck_extra(self):
        return {"sketch": base64.b64encode(zlib.compress(self.sketch.tobytes(), 6)).decode("ascii"),
                "hot": self.hot, "counts": [self.n_uniform, self.n_hot, self.n_rare]}

    def _ck_load(self, st):
        if "sketch" in st:
            a = array("H")
            a.frombytes(zlib.decompress(base64.b64decode(st["sketch"])))
            self.sketch = a
        self.hot = [{"idx": list(h["idx"]), "values": dict(h["values"])} for h in st.get("hot", [])]
        self.n_uniform, self.n_hot, self.n_rare = (int(x) for x in st.get("counts", [0, 0, 0]))
