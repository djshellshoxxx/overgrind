"""twise: streaming, constraint-aware, bounded-memory t-way covering generator.

Factors = every driven feature (levels = its states) and every fuzzable control
(levels = ``domain.buckets``). A t-tuple is a choice of t factors and one level each.

* Universe size U = e_t(L_1..L_n) (elementary symmetric polynomial) is computed
  combinatorially, never by listing.
* Tuples are tracked in an exact bitset (combinadic subset rank x level code) when the
  code space fits ``strategy_options['tuple_cap']`` bits (default 8M = 1 MiB).
  Otherwise the generator runs in *sampled* mode and says so: nothing is tracked and no
  coverage is claimed.
* Rows are produced greedily (AETG/IPOG style): a not-yet-covered seed tuple is taken
  from a keyed-shuffled cursor, remaining factors get the level that covers the most
  uncovered tuples, honouring nogoods (the state part is always a valid state).
* Tuples whose feature part cannot be completed to a valid state are *infeasible*:
  counted separately (exact upfront scan when small, otherwise discovered lazily and
  flagged), excluded from the total.
* When a sweep reaches 100% of feasible tuples a new sweep starts (bitset cleared,
  fresh keyed shuffle, fresh random values inside buckets); it never stops.
"""
from __future__ import annotations

import base64
import itertools
import math
import random
import zlib
from bisect import bisect_right

from ..states import KeyedPermutation, derive_seed
from ..types import Coverage
from . import BaseGenerator

DEFAULT_TUPLE_CAP = 8_000_000
DEFAULT_SCAN_CAP = 100_000


def elementary(levels, k: int) -> list[int]:
    """e_0..e_k of the level counts: number of j-tuples over all factor subsets."""
    e = [1] + [0] * k
    for L in levels:
        for j in range(k, 0, -1):
            e[j] += e[j - 1] * L
    return e


def _pack(b: bytearray) -> str:
    return base64.b64encode(zlib.compress(bytes(b), 6)).decode("ascii")


def _unpack(s: str) -> bytearray:
    return bytearray(zlib.decompress(base64.b64decode(s)))


class Generator(BaseGenerator):
    name = "twise"

    def __init__(self, project):
        super().__init__(project)
        sp, plan = self.space, self.plan
        want = int(self.opts.get("t", 2))
        if not 1 <= want <= 4:
            raise ValueError("twise supports t = 1..4")
        self.t_req = want
        self.tuple_cap = int(self.opts.get("tuple_cap", DEFAULT_TUPLE_CAP))
        self.scan_cap = int(self.opts.get("scan_cap", DEFAULT_SCAN_CAP))
        self.gain_cap = int(self.opts.get("gain_cap", 64))
        # ---- factors: features first, then controls
        self.fk: list[int] = [k for k in range(sp.n) if sp.radix[k] >= 2]          # driven idx per position
        self.nf = len(self.fk)
        bks = plan.buckets() if plan.n else []
        self.cf: list[int] = [ci for ci in range(plan.n) if len(bks[ci]) >= 2]      # control idx per position
        self.levels: list[int] = [sp.radix[k] for k in self.fk] + [len(bks[ci]) for ci in self.cf]
        self.n = len(self.levels)
        self.t = min(want, self.n)
        self.constrained = bool(sp.idx_nogoods) or (not sp.include_all_off and sp.n > 0)
        self.nogoods_by_lit: dict[tuple[int, int], list] = {}
        for ng in sp.idx_nogoods:
            for lit in ng:
                self.nogoods_by_lit.setdefault(lit, []).append(ng)
        self.involved = {f for ng in sp.idx_nogoods for f, _ in ng}
        # ---- universe
        t = self.t
        self.universe = elementary(self.levels, t)[t] if t else 0
        self.Lmax = max(self.levels, default=1)
        self.Lt = self.Lmax ** t
        self.W = math.comb(self.n, t) * self.Lt if t else 0
        self.exact = t > 0 and self.universe > 0 and self.W <= self.tuple_cap
        self.mode = "exact" if self.exact else "sampled"
        # ---- state
        self.sweep = 0
        self.cases_in_sweep = 0
        self.sweeps_done = 0
        self.cursor = 0
        self.covered = 0
        self.unresolved = 0
        self.dead: set[tuple] = set()
        self.infeasible = 0
        self.scan_complete = True
        self._lazy_dead: list[list] = []
        self._perm = None
        self._perm_sweep = -1
        if self.exact:
            self.cov = bytearray((self.W >> 3) + 1)
            self.ctab = [[0] * self.n] + [[math.comb(p, j) for p in range(self.n)] for j in range(1, t + 1)]
            self.pw = [self.Lmax ** j for j in range(t + 1)]
            self._ctrl_e = elementary(self.levels[self.nf:], t)
            self._scan_infeasible()
        else:
            self.cov = bytearray()

    # ------------------------------------------------------------ infeasibility
    def _tuple_dead_count(self, j: int) -> int:
        return self._ctrl_e[self.t - j]

    def _scan_infeasible(self) -> None:
        sp = self.space
        if not self.constrained or self.nf == 0:
            return
        if not sp.idx_nogoods and sum(1 for r in sp.radix if r >= 2) > self.t:
            return                                   # all-off exclusion alone cannot kill a tuple
        rng = random.Random(derive_seed(self.seed, 0x5CA11))
        budget = self.scan_cap
        flevels = self.levels[:self.nf]
        for j in range(1, self.t + 1):
            size = elementary(flevels, j)[j]
            if size > budget:
                self.scan_complete = False
                break
            budget -= size
            for combo in itertools.combinations(range(self.nf), j):
                for lv in itertools.product(*[range(self.levels[p]) for p in combo]):
                    tup = tuple(zip(combo, lv))
                    if self._tuple_infeasible(tup, rng):
                        self.dead.add(tup)
                        self.infeasible += self._tuple_dead_count(j)

    def _tuple_infeasible(self, tup: tuple, rng) -> bool:
        if len(tup) >= 2:
            for i in range(len(tup)):
                if tup[:i] + tup[i + 1:] in self.dead:
                    return True
        partial = {self.fk[p]: l for p, l in tup}
        for ng in self.space.idx_nogoods:
            if all(partial.get(f) == s for f, s in ng):
                return True
        if not (self.involved & partial.keys()) and sum(1 for r in self.space.radix if r >= 2) > len(partial):
            return False
        r = self.space.complete(partial, rng, budget=20_000)
        if r is False:
            self.unresolved += 1
            return False
        return r is None

    # ------------------------------------------------------------ tuple codes
    def _code(self, positions, levels_by_pos) -> int:
        rank = 0
        lc = 0
        ctab, pw = self.ctab, self.pw
        for j, p in enumerate(positions):
            rank += ctab[j + 1][p]
            lc += levels_by_pos[p] * pw[j]
        return rank * self.Lt + lc

    def _decode(self, code: int):
        rank, lc = divmod(code, self.Lt)
        pos = [0] * self.t
        for j in range(self.t, 0, -1):
            p = bisect_right(self.ctab[j], rank) - 1
            rank -= self.ctab[j][p]
            pos[j - 1] = p
        out = []
        for j, p in enumerate(pos):
            lc, l = divmod(lc, self.Lmax)
            if l >= self.levels[p]:
                return None
            out.append((p, l))
        return tuple(out)

    def _bit(self, code: int) -> bool:
        return bool(self.cov[code >> 3] & (1 << (code & 7)))

    def _set(self, code: int) -> bool:
        b, m = code >> 3, 1 << (code & 7)
        if self.cov[b] & m:
            return False
        self.cov[b] |= m
        return True

    def _feasible_total(self) -> int:
        return self.universe - self.infeasible

    # ------------------------------------------------------------ seed + row
    def _pick_seed(self, rng):
        sp = self.space
        while True:
            if self._perm is None or self._perm_sweep != self.sweep:
                self._perm = KeyedPermutation(self.W, derive_seed(self.seed, self.sweep, 0x7157))
                self._perm_sweep = self.sweep
            if self.cursor >= self.W or (self.scan_complete and self.covered >= self._feasible_total()):
                self.sweeps_done += 1
                self.sweep += 1
                self.cases_in_sweep = 0
                self.cursor = 0
                self.covered = 0
                self.cov = bytearray(len(self.cov))
                self._perm = None
                continue
            code = self._perm(self.cursor)
            self.cursor += 1
            if self._bit(code):
                continue
            tup = self._decode(code)
            if tup is None:
                self._set(code)
                continue
            fp = tuple(x for x in tup if x[0] < self.nf)
            if fp in self.dead:
                self._set(code)
                continue
            full = None
            if fp and self.constrained:
                full = sp.complete({self.fk[p]: l for p, l in fp}, rng, budget=100_000)
                if not full:
                    if full is False:
                        self.unresolved += 1
                    self.dead.add(fp)
                    self._lazy_dead.append([list(x) for x in fp])
                    self.infeasible += self._tuple_dead_count(len(fp))
                    self._set(code)
                    continue
            return tup, code, full

    def _gain(self, p: int, lv: int, fixed: list[int], row: list[int], rng) -> int:
        t = self.t
        if t == 1:
            return 0 if self._bit(self._code((p,), {p: lv})) else 1
        k = t - 1
        m = len(fixed)
        if m < k:
            return 0
        if math.comb(m, k) <= self.gain_cap:
            combos = itertools.combinations(fixed, k)
        else:
            combos = (tuple(sorted(rng.sample(fixed, k))) for _ in range(self.gain_cap))
        g = 0
        old = row[p]
        row[p] = lv
        for combo in combos:
            pos = sorted(combo + (p,))
            if not self._bit(self._code(pos, row)):
                g += 1
        row[p] = old
        return g

    def _choose(self, p: int, fixed: list[int], row: list[int], rng, allowed=None) -> int | None:
        L = self.levels[p]
        order = list(range(L))
        rng.shuffle(order)
        scored = [(self._gain(p, lv, fixed, row, rng), lv) for lv in order]
        scored.sort(key=lambda x: -x[0])                 # stable: ties keep shuffled order
        for _, lv in scored:
            if allowed is None or allowed(lv):
                return lv
        return None

    def _build_row(self, tup, full, rng):
        sp = self.space
        n, nf = self.n, self.nf
        row = [-1] * n
        for p, l in tup:
            row[p] = l
        fixed = sorted(p for p, _ in tup)
        fp_partial = {self.fk[p]: l for p, l in tup if p < nf}
        assigned = dict(fp_partial)
        order = [p for p in range(nf) if row[p] < 0]
        rng.shuffle(order)
        dead_end = False
        for p in order:
            k = self.fk[p]

            def allowed(lv, k=k):
                for ng in self.nogoods_by_lit.get((k, lv), ()):
                    if all(f == k or assigned.get(f) == s for f, s in ng):
                        return False
                return True

            lv = self._choose(p, fixed, row, rng, allowed)
            if lv is None:
                dead_end = True
                break
            row[p] = lv
            assigned[k] = lv
            fixed.append(p)
            fixed.sort()
        idx = [0] * sp.n
        if not dead_end:
            for p in range(nf):
                idx[self.fk[p]] = row[p]
        if dead_end or (self.constrained and not sp.is_valid_idx(idx)):
            idx = full if full else sp.complete(fp_partial, rng)
            if not idx:
                idx = sp.sample(rng)
            for p in range(nf):
                row[p] = idx[self.fk[p]]
            fixed = sorted(set(fixed) | set(range(nf)))
        order = [p for p in range(nf, n) if row[p] < 0]
        rng.shuffle(order)
        for p in order:
            row[p] = self._choose(p, fixed, row, rng)
            fixed.append(p)
            fixed.sort()
        return row, idx

    def _mark_row(self, row: list[int]) -> None:
        ctab, pw, Lt, cov = self.ctab, self.pw, self.Lt, self.cov
        new = 0
        for combo in itertools.combinations(range(self.n), self.t):
            rank = 0
            lc = 0
            for j, p in enumerate(combo):
                rank += ctab[j + 1][p]
                lc += row[p] * pw[j]
            code = rank * Lt + lc
            b, m = code >> 3, 1 << (code & 7)
            if not cov[b] & m:
                cov[b] |= m
                new += 1
        self.covered += new

    # ------------------------------------------------------------ stream
    def _next_case(self):
        sp, plan = self.space, self.plan
        sw, ci = self.sweep, self.cases_in_sweep
        rng = self.rng(sw, ci, 0x7)
        bks = plan.buckets() if plan.n else []
        cpos = {c: self.nf + q for q, c in enumerate(self.cf)}
        if self.exact:
            tup, _, full = self._pick_seed(rng)
            sw, ci = self.sweep, self.cases_in_sweep       # may have rolled into a new sweep
            rng = self.rng(sw, ci, 0x8)
            row, idx = self._build_row(tup, full, rng)
            self._mark_row(row)
            tags = (f"twise:{self.t}", f"sweep:{sw + 1}")
        else:
            idx = sp.sample(rng)
            row = None
            tags = (f"twise:{self.t}", "sampled")
        values = {}
        for c in range(plan.n):
            if row is not None and c in cpos:
                values[plan.ids[c]] = plan.sample_bucket(c, bks[c][row[cpos[c]]], rng)
            elif bks and len(bks[c]) >= 1 and row is None:
                values[plan.ids[c]] = plan.sample_bucket(c, bks[c][rng.randrange(len(bks[c]))], rng)
            elif bks:
                values[plan.ids[c]] = plan.sample_bucket(c, bks[c][0], rng)
            else:
                values[plan.ids[c]] = None
        self.cases_in_sweep += 1
        return self.make_case(idx, values, tags=tags)

    # ------------------------------------------------------------ coverage
    def progress(self) -> dict:
        """Exact t-way progress (exact mode) for UIs/tests."""
        total = self._feasible_total() if self.exact else self.universe
        covered = self.covered if self.exact else 0
        pct = 100.0 * covered / total if self.exact and total else (100.0 if self.exact else 0.0)
        return {"mode": self.mode, "t": self.t, "universe": self.universe, "infeasible": self.infeasible,
                "total": total, "covered": covered, "percent": pct, "sweeps_done": self.sweeps_done,
                "infeasible_exact": self.scan_complete, "unresolved": self.unresolved}

    def coverage(self) -> Coverage:
        nb = sum(len(b) for b in (self.plan.buckets() if self.plan.n else []))
        pr = self.progress()
        t, n = self.t, self.n
        note = "" if self.t == self.t_req else f" (requested t={self.t_req}, only {n} factors with >1 level)"
        if n == 0 or self.universe == 0:
            g = "Nothing to combine: fewer than one factor with more than one level; random valid cases only."
            return Coverage(tway_t=t, tway_total=0, guarantee=g, buckets_total=nb)
        if not self.exact:
            g = (f"SAMPLED mode: {t}-way tuple universe {self.universe} needs {self.W} tracked codes "
                 f"(> tuple_cap {self.tuple_cap}); nothing tracked, cases are random valid states with random "
                 f"buckets. No {t}-way coverage is claimed{note}.")
            return Coverage(tway_t=t, tway_total=self.universe, tway_covered=0, buckets_total=nb, guarantee=g)
        extra = ""
        if not self.scan_complete:
            extra = f" Infeasible count is a lower bound (feasibility scan incomplete, found lazily so far)."
        if self.unresolved:
            extra += f" {self.unresolved} tuples had undecided feasibility (search budget)."
        inf = f"{self.infeasible} infeasible tuple{'s' if self.infeasible != 1 else ''} excluded by constraints"
        if self.sweeps_done == 0:
            g = (f"{t}-way progress: {pr['covered']}/{pr['total']} feasible tuples covered "
                 f"({pr['percent']:.1f}%); {inf}; control values drawn inside buckets, not exhaustive.{extra}{note}")
        else:
            g = (f"All {pr['total']} feasible {t}-way tuples covered {self.sweeps_done} time"
                 f"{'s' if self.sweeps_done != 1 else ''} (sweep {self.sweep + 1}: {pr['percent']:.1f}%); {inf}; "
                 f"control values drawn fresh inside buckets, not exhaustive.{extra}{note}")
        return Coverage(states_total=None, tway_t=t, tway_total=pr["total"], tway_covered=pr["covered"],
                        epochs=self.sweeps_done, buckets_total=nb,
                        buckets_visited=nb if self.sweeps_done >= 1 else 0, guarantee=g)

    # ------------------------------------------------------------ checkpoint
    def _ck_extra(self):
        d = {"sweep": self.sweep, "cases_in_sweep": self.cases_in_sweep, "sweeps_done": self.sweeps_done,
             "cursor": self.cursor, "covered": self.covered, "mode": self.mode,
             "infeasible": self.infeasible, "unresolved": self.unresolved, "lazy_dead": self._lazy_dead}
        if self.exact:
            d["bits"] = _pack(self.cov)
        return d

    def _ck_load(self, st):
        self.sweep = int(st.get("sweep", 0))
        self.cases_in_sweep = int(st.get("cases_in_sweep", 0))
        self.sweeps_done = int(st.get("sweeps_done", 0))
        self.cursor = int(st.get("cursor", 0))
        self.covered = int(st.get("covered", 0))
        self._perm = None
        if self.exact and "bits" in st:
            self.cov = _unpack(st["bits"])
            # base infeasibility came from the deterministic scan in __init__; add lazy finds
            self._lazy_dead = []
            for fp in st.get("lazy_dead", []):
                key = tuple(tuple(x) for x in fp)
                if key not in self.dead:
                    self.dead.add(key)
                    self.infeasible += self._tuple_dead_count(len(key))
                self._lazy_dead.append(fp)
            self.unresolved = int(st.get("unresolved", self.unresolved))
