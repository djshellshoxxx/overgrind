"""Valid feature-state space X: counting, ranking and lazy keyed-shuffle enumeration.

X = product of the states of enabled toggle/multistate features (action features are
events, not states; dangerous features are held at their default) minus the nogoods
from ``constraints`` and, optionally, minus the all-off state.

Counting and ranking never materialise X: features that appear in no nogood are free
digits of a mixed-radix number; features linked by nogoods form *components* whose
(small) valid sub-assignments are enumerated by pruned DFS (capped). X is then the
product of those digits, so ``unrank(i)`` is exact and O(#digits).  Visiting every
state exactly once per epoch in random order uses a keyed Feistel permutation with
cycle walking (``KeyedPermutation``): O(1) memory even for a 2^200 space.
"""
from __future__ import annotations

import random
from typing import Sequence

from .constraints import NogoodSet, compile_constraints
from .types import FeatureDef, Project

M64 = (1 << 64) - 1
_GOLD = 0x9E3779B97F4A7C15


def mix64(x: int) -> int:
    """splitmix64 finaliser."""
    x &= M64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & M64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & M64
    return x ^ (x >> 31)


def derive_seed(*parts: int) -> int:
    """Deterministic 64-bit seed from integers (order-sensitive)."""
    s = 0x1234567
    for p in parts:
        p = int(p)
        s = mix64(s + _GOLD + (p & M64))
        if p >> 64:
            s = mix64(s ^ (p >> 64))
    return s


def case_rng(*parts: int) -> random.Random:
    return random.Random(derive_seed(*parts))


class KeyedPermutation:
    """Bijection on [0, n) from a key: balanced Feistel network + cycle walking.

    O(1) memory, ``perm(i)`` costs a few hash rounds; any n >= 1 (bigints OK).
    """
    ROUNDS = 5

    def __init__(self, n: int, key: int):
        if n < 1:
            raise ValueError("n must be >= 1")
        self.n = n
        bits = max(2, (n - 1).bit_length())
        if bits % 2:
            bits += 1
        self.h = bits // 2
        self.mask = (1 << self.h) - 1
        self.keys = [derive_seed(key, r) for r in range(self.ROUNDS)]
        self._chunks = (self.h + 63) // 64

    def _f(self, r: int, k: int) -> int:
        if self._chunks == 1:
            return mix64(k ^ r) & self.mask
        s = k
        x = r
        while True:
            s = mix64(s ^ (x & M64))
            x >>= 64
            if not x:
                break
        out = 0
        for j in range(self._chunks):
            out |= mix64(s + j * _GOLD) << (64 * j)
        return out & self.mask

    def __call__(self, i: int) -> int:
        if not 0 <= i < self.n:
            raise IndexError(i)
        h, mask, n = self.h, self.mask, self.n
        x = i
        while True:
            left, right = x >> h, x & mask
            for k in self.keys:
                left, right = right, left ^ self._f(right, k)
            x = (left << h) | right
            if x < n:
                return x


def _enumerate_component(radix: Sequence[int], ngs: Sequence[Sequence[tuple[int, int]]],
                         cap: int, node_budget: int) -> list[tuple[int, ...]] | None:
    """Lexicographic list of valid assignments of one component, or None if > cap."""
    m = len(radix)
    check_at: list[list] = [[] for _ in range(m)]
    for ng in ngs:
        check_at[max(p for p, _ in ng)].append(tuple(ng))
    cur = [0] * m
    nxt = [0] * m
    out: list[tuple[int, ...]] = []
    k = 0
    nodes = 0
    while k >= 0:
        if k == m:
            out.append(tuple(cur))
            if len(out) > cap:
                return None
            k -= 1
            continue
        if nxt[k] >= radix[k]:
            nxt[k] = 0
            k -= 1
            continue
        s = nxt[k]
        nxt[k] += 1
        cur[k] = s
        nodes += 1
        if nodes > node_budget:
            return None
        if any(all(cur[p] == t for p, t in ng) for ng in check_at[k]):
            continue
        k += 1
    return out


class StateSpace:
    """Valid feature-state space of a project (see module docstring)."""

    def __init__(self, project: Project, comp_cap: int = 100_000, node_budget: int = 3_000_000,
                 include_all_off: bool | None = None):
        feats: Sequence[FeatureDef] = project.features
        self.include_all_off = project.run.include_all_off if include_all_off is None else include_all_off
        driven = [f for f in feats if f.enabled and not f.dangerous and f.kind in ("toggle", "multistate")
                  and len(f.states) >= 1]
        self.features: tuple[FeatureDef, ...] = tuple(driven)
        self.ids: tuple[str, ...] = tuple(f.id for f in driven)
        self.states: tuple[tuple[str, ...], ...] = tuple(tuple(f.states) for f in driven)
        self.radix: tuple[int, ...] = tuple(len(s) for s in self.states)
        self.n = len(driven)
        self.held: dict[str, str] = {f.id: f.default_state for f in feats
                                     if f.enabled and f.dangerous and f.kind in ("toggle", "multistate")}
        self._index = {fid: k for k, fid in enumerate(self.ids)}
        self.nogoods: NogoodSet = compile_constraints(feats, project.constraints, driven=self.ids)
        self.raw_size = 1
        for r in self.radix:
            self.raw_size *= r
        self._build(comp_cap, node_budget)

    # ------------------------------------------------------------ construction
    def _build(self, comp_cap: int, node_budget: int) -> None:
        idx_ngs = [tuple((self._index[f], self.states[self._index[f]].index(s)) for f, s in ng)
                   for ng in self.nogoods.nogoods]
        self.idx_nogoods = idx_ngs
        parent = list(range(self.n))

        def find(a: int) -> int:
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for ng in idx_ngs:
            for (a, _), (b, _) in zip(ng, ng[1:]):
                parent[find(a)] = find(b)
        involved = {f for ng in idx_ngs for f, _ in ng}
        comps: dict[int, list[int]] = {}
        for f in sorted(involved):
            comps.setdefault(find(f), []).append(f)
        self.free: list[int] = [f for f in range(self.n) if f not in involved]
        self.components: list[tuple[list[int], list[tuple[int, ...]]]] = []
        self.countable = not self.nogoods.impossible
        total_budget = node_budget
        for feats_ in comps.values():
            pos = {f: p for p, f in enumerate(feats_)}
            ngs = [[(pos[f], s) for f, s in ng] for ng in idx_ngs if ng[0][0] in pos]
            lst = _enumerate_component([self.radix[f] for f in feats_], ngs, comp_cap, total_budget)
            if lst is None:
                self.countable = False
                self.components.append((feats_, []))
                continue
            self.components.append((feats_, lst))
        # digits: radices of free features then component list sizes
        self._digits: list[tuple] = []
        for f in self.free:
            self._digits.append(("f", f, self.radix[f]))
        for feats_, lst in self.components:
            self._digits.append(("c", feats_, lst, len(lst)))
        self.all_off_valid = False
        self._offset = 0
        if not self.countable:
            self.valid_raw = None
            self.size = None
            self.all_off_valid = None
            return
        n_valid = 1
        for d in self._digits:
            n_valid *= d[2] if d[0] == "f" else d[3]
        self.valid_raw = n_valid
        self.all_off_valid = (n_valid > 0 and all(
            bool(lst) and lst[0] == (0,) * len(f) for f, lst in self.components))
        if self.n == 0:
            self.all_off_valid = True
        if not self.include_all_off and self.all_off_valid and self.n > 0:
            self._offset = 1
        self.size = n_valid - self._offset

    @property
    def unsatisfiable(self) -> bool:
        """True iff provably zero valid states (undecided uncountable spaces report False)."""
        if self.nogoods.impossible:
            return True
        if self.size is not None:
            return self.size == 0
        return False

    # ------------------------------------------------------------ rank / sample
    def unrank(self, i: int) -> list[int]:
        """The i-th valid state (0 <= i < size) as a list of state indices."""
        if self.size is None:
            raise ValueError("space is not countable")
        if not 0 <= i < self.size:
            raise IndexError(i)
        i += self._offset
        out = [0] * self.n
        for d in self._digits:
            if d[0] == "f":
                i, v = divmod(i, d[2])
                out[d[1]] = v
            else:
                i, v = divmod(i, d[3])
                for f, s in zip(d[1], d[2][v]):
                    out[f] = s
        return out

    def is_valid_idx(self, idx: Sequence[int]) -> bool:
        if self.nogoods.impossible:
            return False
        for ng in self.idx_nogoods:
            if all(idx[f] == s for f, s in ng):
                return False
        if not self.include_all_off and self.n and not any(idx):
            return False
        return True

    def sample(self, rng: random.Random) -> list[int]:
        """Uniform valid state (exact when countable, rejection otherwise)."""
        if self.size is not None:
            if self.size == 0:
                raise ValueError("no valid feature states")
            return self.unrank(rng.randrange(self.size))
        if self.nogoods.impossible:
            raise ValueError("no valid feature states")
        radix = self.radix
        for _ in range(5000):
            idx = [rng.randrange(r) for r in radix]
            if self.is_valid_idx(idx):
                return idx
        got = self.complete({}, rng)
        if not got:
            raise ValueError("no valid feature states found")
        return got

    def complete(self, partial: dict[int, int], rng: random.Random, budget: int = 200_000):
        """Valid full state extending ``partial`` {feature index: state index}.
        Returns list, None (proven impossible) or False (search budget exhausted)."""
        fixed = {self.ids[f]: self.states[f][s] for f, s in partial.items()}
        accept = None
        if not self.include_all_off and self.n:
            accept = lambda a: any(a[i] != self.states[k][0] for k, i in enumerate(self.ids))
        r = self.nogoods.find_valid(fixed, rng, budget, features=self.ids, accept=accept)
        if not r:
            return r
        return [self.states[k].index(r[i]) for k, i in enumerate(self.ids)]

    def feasible(self, partial: dict[int, int], rng: random.Random | None = None, budget: int = 200_000):
        """True / False(proven infeasible) / None (unknown)."""
        r = self.complete(partial, rng or random.Random(0), budget)
        if r is False:
            return None
        return r is not None

    # ------------------------------------------------------------ conversion
    def state_dict(self, idx: Sequence[int]) -> dict[str, str]:
        ids, sts = self.ids, self.states
        d = {ids[k]: sts[k][idx[k]] for k in range(self.n)}
        if self.held:
            d.update(self.held)
        return d

    def to_index(self, state: dict[str, str]) -> list[int]:
        return [self.states[k].index(state[fid]) for k, fid in enumerate(self.ids)]

    def default_index(self) -> list[int]:
        out = []
        for f in self.features:
            out.append(f.states.index(f.default_state) if f.default_state in f.states else 0)
        return out

    def is_active(self, k: int, s: int) -> bool:
        return s != 0

    def permutation(self, key: int) -> KeyedPermutation:
        if not self.size:
            raise ValueError("space is empty or not countable")
        return KeyedPermutation(self.size, key)

    def describe(self) -> str:
        if self.size is None:
            return "uncountable (constraint components too large)"
        return str(self.size)


def pow2_text(n: int | None) -> str:
    if n is None:
        return "?"
    if n > 1 and n & (n - 1) == 0:
        return f"2^{n.bit_length() - 1}"
    return f"{n:,}" if n < 10 ** 15 else f"{n:.3e}"
