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
  fresh keyed shuffle, fresh random values inside buckets); it never stops. Sweep k covers every tuple
  for the k-th time (multiplicity epochs). After the first full sweep the stream is mixed
  (``mix``, default 70% t-way / 15% boundary / 15% adaptive) so fresh values keep being explored.
* ``strength_groups`` give factor groups their own (higher) strength: extra layers of tuples that are
  seeded first and counted separately. ``solver_nodes`` bounds the feasibility search; tuples whose
  feasibility could not be decided stay in the denominator as *unknown* and are retried with a doubled
  budget at each sweep end. ``feedback`` with ``detail['applied'] is False`` un-covers the tuples that
  case newly covered and re-queues them (bounded retries, then *unreachable*, excluded and counted).
"""
from __future__ import annotations

import base64
import itertools
import math
import random
import zlib
from bisect import bisect_right

from ..states import KeyedPermutation, derive_seed
from ..types import Case, Coverage
from . import BaseGenerator, strategy_class

DEFAULT_TUPLE_CAP = 8_000_000
DEFAULT_SCAN_CAP = 100_000
MAX_TUPLE_FAILS = 3
QUEUE_CAP = 100_000
INFLIGHT = 32
NODES_CAP = 1 << 22
DEFAULT_MIX = {"twise": 0.7, "boundary": 0.15, "adaptive": 0.15}


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


class GroupLayer:
    """Extra tuples of strength ``t`` over a fixed factor group (bitset indexed like the main ledger)."""

    def __init__(self, pos: list[int], levels: list[int], t: int, key: int):
        self.pos, self.lv, self.t = pos, levels, t          # pos: global factor positions (sorted)
        m = len(pos)
        self.Lmax = max(levels)
        self.Lt = self.Lmax ** t
        self.ctab = [[0] * m] + [[math.comb(p, j) for p in range(m)] for j in range(1, t + 1)]
        self.W = math.comb(m, t) * self.Lt
        self.universe = elementary(levels, t)[t]
        self.cov = bytearray((self.W >> 3) + 1)
        self.key = key
        self.perm = KeyedPermutation(self.W, key)
        self.cursor = 0
        self.covered = 0
        self.infeasible = 0
        self.unknown = 0
        self.skipped: set[int] = set()      # dead/unknown codes (kept out of the covered count)
        self.requeue: list[int] = []

    def total(self) -> int:
        return self.universe - self.infeasible

    def complete(self) -> bool:
        return (self.cursor >= self.W and not self.requeue) or self.covered + self.unknown >= self.total()

    def _bit(self, c: int) -> bool:
        return bool(self.cov[c >> 3] & (1 << (c & 7)))

    def set_(self, c: int) -> None:
        self.cov[c >> 3] |= 1 << (c & 7)

    def code_of(self, combo: tuple[int, ...], row: list[int]) -> int:
        rank = lc = 0
        for j, q in enumerate(combo):
            rank += self.ctab[j + 1][q]
            lc += row[self.pos[q]] * self.Lmax ** j
        return rank * self.Lt + lc

    def decode(self, code: int):
        rank, lc = divmod(code, self.Lt)
        loc = [0] * self.t
        for j in range(self.t, 0, -1):
            p = bisect_right(self.ctab[j], rank) - 1
            rank -= self.ctab[j][p]
            loc[j - 1] = p
        out = []
        for q in loc:
            lc, l = divmod(lc, self.Lmax)
            if l >= self.lv[q]:
                return None
            out.append((self.pos[q], l))
        return tuple(out)

    def mark(self, row: list[int]) -> list[int]:
        new = []
        for combo in itertools.combinations(range(len(self.pos)), self.t):
            c = self.code_of(combo, row)
            if self._bit(c):
                continue
            self.set_(c)
            if c in self.skipped:
                continue
            self.covered += 1
            new.append(c)
        return new

    def uncover(self, codes) -> None:
        for c in codes:
            if self._bit(c) and c not in self.skipped:
                self.cov[c >> 3] &= ~(1 << (c & 7)) & 0xFF
                self.covered -= 1
                if len(self.requeue) < QUEUE_CAP:
                    self.requeue.append(c)

    def reset(self) -> None:
        self.cov = bytearray(len(self.cov))
        self.covered = 0
        self.cursor = 0
        self.requeue = []
        for c in self.skipped:
            self.set_(c)


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
        self.solver_nodes = int(self.opts.get("solver_nodes", 0))       # 0 = legacy budgets (20k scan / 100k pick)
        self.scan_nodes = self.solver_nodes or 20_000
        self.pick_nodes = self.solver_nodes or 100_000
        self.k_max = max(1, int(self.opts.get("k_max", 3)))
        mx = self.opts.get("mix", DEFAULT_MIX)
        mx = {k: max(0.0, float(mx.get(k, 0.0))) for k in ("twise", "boundary", "adaptive")}
        tot = sum(mx.values())
        if tot <= 0 or mx["twise"] <= 0:
            raise ValueError("mix needs a positive 'twise' share")
        self.mix = {k: v / tot for k, v in mx.items()}
        nb = int(self.opts.get("buckets", 3))
        if not 1 <= nb <= 16:
            raise ValueError("buckets must be 1..16")
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
        self.unk: dict[tuple, int] = {}          # feature-part tuples whose feasibility is undecided (-> multiplicity)
        self.unk_n = 0
        self.dead: set[tuple] = set()
        self.full_passes = 0                     # passes that completed with nothing undecided / unreachable
        self.requeue: list[int] = []
        self.tfail: dict[int, int] = {}
        self.unreach: set[int] = set()
        self.n_failed = 0
        self.n_requeued = 0
        self._inflight: dict[int, tuple] = {}
        self.groups: list[GroupLayer] = []
        self._sub: dict[str, object] = {}
        self._sub_ck: dict[str, dict] = {}
        self.mix_counts = {"twise": 0, "boundary": 0, "adaptive": 0}
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
            self._init_groups()
        else:
            self.cov = bytearray()

    @property
    def unresolved(self) -> int:
        return self.unk_n

    def _factor_pos(self, fid: str) -> int:
        sp = self.space
        if fid in sp.ids:
            k = sp.ids.index(fid)
            if k in self.fk:
                return self.fk.index(k)
        elif fid in self.plan.ids:
            c = self.plan.ids.index(fid)
            if c in self.cf:
                return self.nf + self.cf.index(c)
        else:
            raise ValueError(f"strength_groups: unknown factor {fid!r}")
        return -1                                           # single-level factor: nothing to combine

    def _init_groups(self) -> None:
        raw = self.opts.get("strength_groups") or []
        for gi, item in enumerate(raw):
            if isinstance(item, dict):
                ids = item.get("factors") or item.get("ids") or item.get("features") or []
                tg = int(item.get("t", self.t + 1))
            else:
                ids, tg = list(item), self.t + 1
            pos = sorted({q for q in (self._factor_pos(f) for f in ids) if q >= 0})
            tg = min(tg, len(pos))
            if tg > 4:
                raise ValueError("strength_groups: t must be <= 4")
            if tg <= self.t or tg < 1:
                continue                                   # already implied by the global strength
            lay = GroupLayer(pos, [self.levels[q] for q in pos], tg, derive_seed(self.seed, gi, 0x6A09))
            if lay.W > self.tuple_cap:
                raise ValueError("strength_groups: group tuple space exceeds tuple_cap")
            self.groups.append(lay)

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
        r = self.space.complete(partial, rng, budget=self.scan_nodes)
        if r is False:
            if tup not in self.unk:
                self.unk[tup] = self._tuple_dead_count(len(tup))
                self.unk_n += self.unk[tup]
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
    def _retry_unknown(self) -> bool:
        """Sweep end: re-decide undecided tuples with a doubled node budget (spec B2 3.6)."""
        self.pick_nodes = min(self.pick_nodes * 2, NODES_CAP)
        self.scan_nodes = min(self.scan_nodes * 2, NODES_CAP)
        rng = random.Random(derive_seed(self.seed, self.sweep, 0x4E7))
        resolved = False
        for fp in list(self.unk):
            r = self.space.complete({self.fk[p]: l for p, l in fp}, rng, budget=self.pick_nodes)
            if r is False:
                continue
            self.unk_n -= self.unk.pop(fp)
            if r is None:
                m = self._tuple_dead_count(len(fp))
                self.dead.add(fp)
                self._lazy_dead.append([list(x) for x in fp])
                self.infeasible += m
            resolved = True
        if resolved:
            self.cursor = 0
        return resolved

    def _target(self) -> int:
        return self._feasible_total() - self.unk_n - len(self.unreach)

    def _group_seed(self, rng):
        sp = self.space
        for gi, lay in enumerate(self.groups):
            while True:
                if lay.requeue:
                    code = lay.requeue.pop(0)
                elif lay.cursor < lay.W and not lay.complete():
                    code = lay.perm(lay.cursor)
                    lay.cursor += 1
                else:
                    break
                if lay._bit(code):
                    continue
                tup = lay.decode(code)
                if tup is None:
                    lay.set_(code)
                    continue
                fp = tuple((p, l) for p, l in tup if p < self.nf)
                full = None
                if fp and self.constrained:
                    full = sp.complete({self.fk[p]: l for p, l in fp}, rng, budget=self.pick_nodes)
                    if not full:
                        lay.skipped.add(code)
                        lay.set_(code)
                        if full is False:
                            lay.unknown += 1
                        else:
                            lay.infeasible += 1
                        continue
                return tup, code, full
        return None

    def _pick_seed(self, rng):
        sp = self.space
        rolled = 0
        while True:
            if self._perm is None or self._perm_sweep != self.sweep:
                self._perm = KeyedPermutation(self.W, derive_seed(self.seed, self.sweep, 0x7157))
                self._perm_sweep = self.sweep
            if self.requeue:
                code = self.requeue.pop(0)
                if self._bit(code):
                    continue
            else:
                if self.cursor >= self.W or (self.scan_complete and self.covered >= self._target()):
                    if self.unk and self._retry_unknown():
                        continue
                    rolled += 1
                    if rolled > 2:                          # nothing seedable (everything unreachable/undecided)
                        return (), 0, None
                    self.full_passes += int(self.unk_n == 0 and not self.unreach and self.scan_complete
                                            and all(g.unknown == 0 for g in self.groups))
                    self.sweeps_done += 1
                    self.sweep += 1
                    self.cases_in_sweep = 0
                    self.cursor = 0
                    self.covered = 0
                    self.cov = bytearray(len(self.cov))
                    self._perm = None
                    self._inflight.clear()
                    for g in self.groups:
                        g.reset()
                    continue
                code = self._perm(self.cursor)
                self.cursor += 1
                if self._bit(code):
                    continue
            tup = self._decode(code)
            if tup is None or (self.unreach and code in self.unreach):
                self._set(code)
                continue
            fp = tuple(x for x in tup if x[0] < self.nf)
            if fp in self.dead:
                self._set(code)
                continue
            if fp in self.unk:
                continue
            full = None
            if fp and self.constrained:
                full = sp.complete({self.fk[p]: l for p, l in fp}, rng, budget=self.pick_nodes)
                if not full:
                    m = self._tuple_dead_count(len(fp))
                    if full is False:
                        self.unk[fp] = m
                        self.unk_n += m
                    else:
                        self.dead.add(fp)
                        self._lazy_dead.append([list(x) for x in fp])
                        self.infeasible += m
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

    def _mark_row(self, row: list[int]) -> list[int]:
        """Mark every t-tuple of the row; returns the codes this row NEWLY covered (for un-covering)."""
        ctab, pw, Lt, cov = self.ctab, self.pw, self.Lt, self.cov
        unreach = self.unreach
        new = []
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
                if unreach and code in unreach:
                    continue
                new.append(code)
        self.covered += len(new)
        return new

    # ------------------------------------------------------------ mix (after first full sweep)
    def _sub_gen(self, name: str):
        g = self._sub.get(name)
        if g is None:
            g = strategy_class(name)(self.project)
            g.seed = derive_seed(self.seed, 0xB0, 1 if name == "boundary" else 2)
            if name in self._sub_ck:
                g.restore(self._sub_ck[name])
            self._sub[name] = g
        return g

    def _mix_case(self) -> Case | None:
        if self.sweeps_done < 1 or not self.exact or self.mix["twise"] >= 1.0:
            return None
        u = self.rng(self.seq, 0x31).random()
        if u < self.mix["twise"]:
            self.mix_counts["twise"] += 1
            return None
        name = "boundary" if u < self.mix["twise"] + self.mix["boundary"] else "adaptive"
        if self.mix[name] <= 0:
            name = "adaptive" if name == "boundary" else "boundary"
        sub = self._sub_gen(name)
        c = next(sub)
        self.mix_counts[name] += 1
        return Case(seq=self.seq, state=c.state, values=c.values, order=c.order, dwell_ms=c.dwell_ms,
                    tags=(f"twise:{self.t}", f"mix:{name}", f"sweep:{self.sweep + 1}"))

    # ------------------------------------------------------------ stream
    def _next_case(self):
        mc = self._mix_case()
        if mc is not None:
            return mc
        sp, plan = self.space, self.plan
        sw, ci = self.sweep, self.cases_in_sweep
        rng = self.rng(sw, ci, 0x7)
        bks = plan.buckets() if plan.n else []
        cpos = {c: self.nf + q for q, c in enumerate(self.cf)}
        if self.exact:
            seed = self._group_seed(rng) if self.groups else None
            if seed is None:
                seed = self._pick_seed(rng)
            tup, _, full = seed
            sw, ci = self.sweep, self.cases_in_sweep       # may have rolled into a new sweep
            rng = self.rng(sw, ci, 0x8)
            row, idx = self._build_row(tup, full, rng)
            new = self._mark_row(row)
            gnew = [g.mark(row) for g in self.groups]
            self._inflight[self.seq] = (self.sweep, new, gnew)
            if len(self._inflight) > INFLIGHT:
                self._inflight.pop(next(iter(self._inflight)))
            tags = (f"twise:{self.t}", f"sweep:{sw + 1}")
        else:
            idx = sp.sample(rng)
            row = None
            tags = (f"twise:{self.t}", "sampled")
        values = {}
        for c in range(plan.n):
            bk = bks[c]
            if row is not None and c in cpos:
                bucket = bk[row[cpos[c]]]
            elif row is None:
                bucket = bk[rng.randrange(len(bk))]
            else:
                bucket = bk[0]
            values[plan.ids[c]] = plan.sample_bucket(c, bucket, rng)
        self.cases_in_sweep += 1
        return self.make_case(idx, values, tags=tags)

    # ------------------------------------------------------------ evidence (spec B2 section 4, U1 3.2)
    def feedback(self, case, outcome) -> None:
        if "mix:adaptive" in case.tags:
            sub = self._sub.get("adaptive")
            if sub is not None:
                sub.feedback(case, outcome)
            return
        info = self._inflight.pop(case.seq, None)
        det = outcome.detail or {}
        if det.get("applied", True) is not False:
            return
        self.n_failed += 1
        if info is None or info[0] != self.sweep or not self.exact:
            return
        _, new, gnew = info
        for code in new:
            if not self._bit(code) or code in self.unreach:
                continue
            self.cov[code >> 3] &= ~(1 << (code & 7)) & 0xFF
            self.covered -= 1
            k = self.tfail.get(code, 0) + 1
            if len(self.tfail) < QUEUE_CAP or code in self.tfail:
                self.tfail[code] = k
            if k >= MAX_TUPLE_FAILS and len(self.unreach) < QUEUE_CAP:
                self.unreach.add(code)
                self._set(code)                           # skipped from now on, never counted covered
            elif len(self.requeue) < QUEUE_CAP:
                self.requeue.append(code)
                self.n_requeued += 1
        for g, codes in zip(self.groups, gnew):
            g.uncover(codes)

    # ------------------------------------------------------------ coverage
    def _total(self) -> int:
        """Denominator: feasible + unknown (spec 3.4); unreachable tuples are excluded and counted apart."""
        return self._feasible_total() - len(self.unreach)

    def progress(self) -> dict:
        """Exact t-way progress (exact mode) for UIs/tests."""
        total = self._total() if self.exact else self.universe
        covered = self.covered if self.exact else 0
        pct = 100.0 * covered / total if self.exact and total else (100.0 if self.exact else 0.0)
        return {"mode": self.mode, "t": self.t, "universe": self.universe, "infeasible": self.infeasible,
                "total": total, "covered": covered, "percent": pct, "sweeps_done": self.sweeps_done,
                "infeasible_exact": self.scan_complete, "unresolved": self.unk_n, "unknown": self.unk_n,
                "unreachable": len(self.unreach), "requeued": self.n_requeued, "failed_to_apply": self.n_failed,
                "full_passes": self.full_passes, "k_max": self.k_max, "solver_nodes": self.pick_nodes,
                "mix": dict(self.mix), "mix_counts": dict(self.mix_counts),
                "groups": [{"factors": list(g.pos), "t": g.t, "total": g.total(), "covered": g.covered,
                            "infeasible": g.infeasible, "unknown": g.unknown} for g in self.groups]}

    def why_not_complete(self) -> tuple[str, ...]:
        if not self.exact:
            return (f"{self.t}-way tuple space too large to track within tuple_cap; sampled mode tracks nothing",)
        why = []
        left = self._total() - self.covered
        if left > 0:
            why.append(f"{left} feasible tuples not yet covered in this sweep")
        if self.unk_n:
            why.append(f"{self.unk_n} tuples undecided by the solver (node budget {self.pick_nodes}); counted in the total")
        if self.unreach:
            why.append(f"{len(self.unreach)} tuples unreachable (application failed {MAX_TUPLE_FAILS} times); excluded")
        if not self.scan_complete:
            why.append("infeasible tuples are only discovered lazily; the infeasible count is a lower bound")
        if self.n_failed:
            why.append(f"{self.n_failed} cases failed to apply and were not counted")
        for g in self.groups:
            if g.covered < g.total():
                why.append(f"group {g.t}-way: {g.total() - g.covered} tuples not yet covered")
        return tuple(why)

    def ledger(self) -> dict:
        pr = self.progress()
        pr["why_not_complete"] = list(self.why_not_complete())
        pr["infeasible_sample"] = [[[self.space.ids[self.fk[p]], self.space.states[self.fk[p]][l]] for p, l in fp]
                                   for fp in itertools.islice(sorted(self.dead), 10)]
        return pr

    def coverage(self) -> Coverage:
        nb = sum(len(b) for b in (self.plan.buckets() if self.plan.n else []))
        pr = self.progress()
        t, n = self.t, self.n
        note = "" if self.t == self.t_req else f" (requested t={self.t_req}, only {n} factors with >1 level)"
        pre = f"t={t} interactions:"
        if n == 0 or self.universe == 0:
            g = f"{pre} nothing to combine: fewer than one factor with more than one level; random valid cases only."
            return Coverage(tway_t=t, tway_total=0, guarantee=g, buckets_total=nb)
        if not self.exact:
            g = (f"{pre} SAMPLED mode: the {t}-way tuple universe {self.universe} needs {self.W} tracked codes "
                 f"(> tuple_cap {self.tuple_cap}); nothing is tracked, cases are random valid states with random "
                 f"buckets. NOT a complete-coverage claim: no {t}-way coverage is claimed{note}.")
            return Coverage(tway_t=t, tway_total=None, tway_covered=0, buckets_total=nb, guarantee=g,
                            why_not_complete=self.why_not_complete())
        c, F, I, U = pr["covered"], pr["total"], self.infeasible, self.unk_n
        inf_txt = f"{I} proved infeasible by constraints" if self.scan_complete else \
            f"{I} proved infeasible by constraints (lower bound: feasibility scan incomplete, found lazily so far)"
        pct = int(1000.0 * c / F) / 10.0 if F else 0.0
        if self.full_passes >= 1:
            k = self.full_passes
            g = (f"{pre} all {F} feasible {t}-way value tuples covered {k} time{'s' if k != 1 else ''} "
                 f"(current pass {c} of {F}, {min(pct, 99.9) if c < F else pct}%); {inf_txt}; {U} undecided.")
        else:
            g = (f"{pre} {c} of {F} feasible {t}-way value tuples covered ({min(pct, 99.9) if c < F else pct}%); "
                 f"{inf_txt}; {U} undecided.")
            if self.sweeps_done:
                g += (f" {self.sweeps_done} pass{'es' if self.sweeps_done != 1 else ''} ended with undecided or "
                      "unreachable tuples excluded.")
        for gp in pr["groups"]:
            done = gp["covered"] >= gp["total"] and not gp["unknown"]
            g += (f" Group {len(gp['factors'])} factors t={gp['t']}: {gp['covered']} of {gp['total']} tuples covered"
                  f"{' (complete)' if done else ''}.")
        if self.plan.n:
            g += (f" Controls: only {self.plan.nb} value buckets per control were explored; "
                  "continuous values not exhaustively covered.")
        g += " Order/sequence effects and timing were not covered by this metric."
        if self.n_failed:
            g += f" {self.n_failed} cases failed to apply and are excluded."
        if self.unreach:
            g += f" {len(self.unreach)} tuples unreachable after {MAX_TUPLE_FAILS} failed applications are excluded."
        g += note
        return Coverage(states_total=None, tway_t=t, tway_total=F, tway_covered=c,
                        epochs=self.sweeps_done, buckets_total=nb,
                        buckets_visited=nb if self.sweeps_done >= 1 else 0, guarantee=g,
                        infeasible=I, unknown=U, unreachable=len(self.unreach), failed_to_apply=self.n_failed,
                        why_not_complete=self.why_not_complete())

    # ------------------------------------------------------------ checkpoint
    def _ck_extra(self):
        d = {"sweep": self.sweep, "cases_in_sweep": self.cases_in_sweep, "sweeps_done": self.sweeps_done,
             "cursor": self.cursor, "covered": self.covered, "mode": self.mode,
             "infeasible": self.infeasible, "unresolved": self.unk_n, "lazy_dead": self._lazy_dead,
             "full_passes": self.full_passes, "nodes": [self.scan_nodes, self.pick_nodes],
             "unk": [[[list(x) for x in fp], m] for fp, m in self.unk.items()],
             "requeue": list(self.requeue), "tfail": [[c, k] for c, k in sorted(self.tfail.items())],
             "unreach": sorted(self.unreach), "n_failed": self.n_failed, "n_requeued": self.n_requeued,
             "mix_counts": dict(self.mix_counts)}
        if self.exact:
            d["bits"] = _pack(self.cov)
            d["groups"] = [{"bits": _pack(g.cov), "cursor": g.cursor, "covered": g.covered,
                            "infeasible": g.infeasible, "unknown": g.unknown, "skipped": sorted(g.skipped),
                            "requeue": list(g.requeue)} for g in self.groups]
        subs = {n: g.checkpoint() for n, g in self._sub.items()}
        for n, ck in self._sub_ck.items():
            subs.setdefault(n, ck)
        d["subs"] = subs
        return d

    def _ck_load(self, st):
        self.sweep = int(st.get("sweep", 0))
        self.cases_in_sweep = int(st.get("cases_in_sweep", 0))
        self.sweeps_done = int(st.get("sweeps_done", 0))
        self.cursor = int(st.get("cursor", 0))
        self.covered = int(st.get("covered", 0))
        self.full_passes = int(st.get("full_passes", self.sweeps_done))
        self._perm = None
        self._inflight = {}
        self._sub = {}
        self._sub_ck = dict(st.get("subs", {}))
        self.mix_counts = {k: int(v) for k, v in st.get("mix_counts", self.mix_counts).items()}
        self.n_failed = int(st.get("n_failed", 0))
        self.n_requeued = int(st.get("n_requeued", 0))
        self.requeue = [int(x) for x in st.get("requeue", [])]
        self.tfail = {int(c): int(k) for c, k in st.get("tfail", [])}
        self.unreach = {int(x) for x in st.get("unreach", [])}
        if "nodes" in st:
            self.scan_nodes, self.pick_nodes = (int(x) for x in st["nodes"])
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
            if "unk" in st:                                  # the saved list is authoritative (retries resolve some)
                self.unk = {tuple(tuple(x) for x in fp): int(m) for fp, m in st["unk"]}
                self.unk_n = sum(self.unk.values())
            for g, gs in zip(self.groups, st.get("groups", [])):
                g.cov = _unpack(gs["bits"])
                g.cursor, g.covered = int(gs["cursor"]), int(gs["covered"])
                g.infeasible, g.unknown = int(gs["infeasible"]), int(gs["unknown"])
                g.skipped = {int(x) for x in gs["skipped"]}
                g.requeue = [int(x) for x in gs["requeue"]]
