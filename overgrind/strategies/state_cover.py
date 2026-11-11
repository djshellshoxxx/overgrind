"""state_cover (default): visit every valid feature state once per epoch, random order.

Each epoch is a fresh keyed permutation of the valid-state index space (seeded by
seed+epoch); nothing is materialised, so memory is O(1) whatever the space size.
Control values are drawn fresh (CORE ``domain.sample``) on every visit. If the
space exceeds ``strategy_options['budget']`` (default 1_000_000) the strategy
falls back to uniform valid sampling and reports ``states_total=None``.

Evidence (spec U1 3.2): ``feedback`` with ``Outcome.detail['applied'] is False`` un-counts the visit and
re-queues the state later in the same epoch; after ``MAX_FAILS`` failures the state is ``unreachable`` and is
excluded from the denominator (reported separately). ``detail['verified']`` marks read-back matches; an
Outcome without these keys is treated as applied.
"""
from __future__ import annotations

from array import array

from ..states import derive_seed, pow2_text
from ..types import Coverage
from . import BaseGenerator

DEFAULT_BUDGET = 4_194_304   # spec U1 3.3: enumerable up to 2^22 states (permutation is O(1) memory)
COUNT_CAP = 4096          # per-state visit vector is kept (and reported) up to this many states
MAX_FAILS = 3             # application failures before a state is declared unreachable
RETRY_CAP = 4096          # bounded re-queue / unreachable bookkeeping
INFLIGHT = 256
HIST_BINS = 9             # histogram bins 0..7 and '8 or more'


class Generator(BaseGenerator):
    name = "state_cover"

    def __init__(self, project):
        super().__init__(project)
        self.budget = int(self.opts.get("budget", DEFAULT_BUDGET))
        n = self.space.size
        self.epoch_mode = n is not None and n <= self.budget
        self.n = n
        self.epoch = 0           # 0-based; completed epochs == self.epoch
        self.index = 0           # next position in this epoch's permutation
        self.visited_cnt = 0     # distinct states counted this epoch (== index unless failures/retries)
        self.unverified_cnt = 0  # of those, visits whose read-back did not verify (this epoch)
        self.retry: list[int] = []            # ranks to revisit this epoch (application failed)
        self.fails: dict[int, int] = {}       # rank -> failures so far (bounded)
        self.unreach: set[int] = set()
        self.n_failed = 0        # lifetime cases not applied
        self.n_ver = 0
        self.n_fb = 0            # lifetime feedback calls seen (0 => evidence only 'attempted')
        self.rb_seen = 0         # max features with read-back seen in one case
        self.rb_feats = 0
        self._inflight: dict[int, tuple[int, int, int]] = {}     # seq -> (epoch, rank, from_retry)
        self._perm = None
        self._perm_epoch = -1
        self.sampled = 0
        self._counts = array("B", bytes(n)) if self.epoch_mode and n <= COUNT_CAP else None

    # ------------------------------------------------------------ helpers
    def _target(self) -> int:
        return self.n - len(self.unreach)

    def _next_rank(self) -> tuple[int, int, int]:
        """(rank, position-or--1, epoch) of the next state to visit; retries come first."""
        sp = self.space
        if self._perm is None or self._perm_epoch != self.epoch:
            self._perm = sp.permutation(derive_seed(self.seed, self.epoch, 0x57A7E))
            self._perm_epoch = self.epoch
        if self.retry:
            return self.retry.pop(0), -1, self.epoch
        while self.index < self.n:
            r = self._perm(self.index)
            self.index += 1
            if r in self.unreach:
                continue
            return r, self.index - 1, self.epoch
        # permutation exhausted but epoch not complete: nothing left (only reachable via bookkeeping drift)
        return self._perm(0), 0, self.epoch

    # ------------------------------------------------------------ stream
    def _next_case(self):
        sp = self.space
        if self.epoch_mode:
            ep = self.epoch
            rank, ix, _ = self._next_rank()
            idx = sp.unrank(rank)
            if self._counts is not None and self._counts[rank] < 255:
                self._counts[rank] += 1
            rng = (self.rng(ep, ix) if ix >= 0 else self.rng(ep, self.index, rank, 1)) if self.plan.n else None
            values = self.plan.sample_all(rng) if rng is not None else {}
            tags = (f"epoch:{ep + 1}",) if ix >= 0 else (f"epoch:{ep + 1}", "retry")
            self.visited_cnt += 1
            self._inflight[self.seq] = (ep, rank, 1 if ix < 0 else 0)
            if len(self._inflight) > INFLIGHT:
                self._inflight.pop(next(iter(self._inflight)))
            if self.visited_cnt >= self._target() and not self.retry:
                self.index = 0
                self.visited_cnt = 0
                self.unverified_cnt = 0
                self.epoch += 1
            return self.make_case(idx, values, tags=tags)
        rng = self.rng(self.sampled, 1)
        idx = sp.sample(rng)
        values = self.plan.sample_all(rng)
        self.sampled += 1
        return self.make_case(idx, values, tags=("sampled",))

    # ------------------------------------------------------------ evidence
    def feedback(self, case, outcome) -> None:
        if not self.epoch_mode:
            return
        det = outcome.detail or {}
        info = self._inflight.pop(case.seq, None)
        self.n_fb += 1
        if det.get("features"):
            if int(det.get("readback", 0)) >= self.rb_seen:
                self.rb_seen, self.rb_feats = int(det.get("readback", 0)), int(det["features"])
        if info is None:
            return
        ep, rank, _ = info
        if det.get("applied", True) is not False:
            if det.get("verified") is True:
                self.n_ver += 1
            elif ep == self.epoch:
                self.unverified_cnt += 1
            return
        # ---- application failed: not counted, re-queued, bounded budget
        self.n_failed += 1
        reopen = ep == self.epoch - 1 and self.index == 0 and self.visited_cnt == 0 and not self.retry
        if ep != self.epoch and not reopen:
            return
        if reopen:                               # the last case of the just-closed epoch was lost: epoch is not done
            self.epoch -= 1
            self._perm = None
            self.index = self.n
            self.visited_cnt = self._target()
        if self._counts is not None and self._counts[rank] > 0:
            self._counts[rank] -= 1
        self.visited_cnt = max(0, self.visited_cnt - 1)
        k = self.fails.get(rank, 0) + 1
        if len(self.fails) < RETRY_CAP or rank in self.fails:
            self.fails[rank] = k
        if k >= MAX_FAILS:
            self.unreach.add(rank)
            if self.visited_cnt >= self._target() and not self.retry:   # excluded state may complete the epoch
                self.index, self.visited_cnt, self.unverified_cnt = 0, 0, 0
                self.epoch += 1
        elif len(self.retry) < RETRY_CAP:
            self.retry.append(rank)

    def evidence_level(self) -> str:
        if not self.n_fb:
            return "attempted"
        return "verified" if self.unverified_cnt == 0 and self.n_ver else "applied"

    def _level_text(self) -> str:
        lv = self.evidence_level()
        if lv == "applied" and self.rb_feats:
            return f"applied (read-back available for {self.rb_seen}/{self.rb_feats} features)"
        if lv == "applied":
            return "applied (no read-back available)"
        if lv == "attempted":
            return "attempted: application not confirmed"
        return lv

    def _caveats(self) -> str:
        out = []
        if self.plan.n:
            out.append(f"Controls: only {self.plan.nb} value buckets per control were explored; "
                       "continuous values not exhaustively covered.")
        out.append("Order/sequence effects and timing were not covered by this metric.")
        if self.n_failed:
            out.append(f"{self.n_failed} cases failed to apply and are excluded.")
        if self.unreach:
            out.append(f"{len(self.unreach)} states unreachable (application failed {MAX_FAILS} times) "
                       "are excluded from the count.")
        if self.unverified_cnt and self.rb_feats:
            out.append(f"{self.unverified_cnt} visits this epoch were not confirmed by read-back.")
        return " ".join(out)

    def why_not_complete(self) -> tuple[str, ...]:
        why = []
        if not self.epoch_mode:
            why.append("state space too large to enumerate within the budget; sampled only")
            return tuple(why)
        left = self._target() - self.visited_cnt
        if left > 0:
            why.append(f"{left} valid states not yet visited this epoch")
        if self.retry:
            why.append(f"{len(self.retry)} states re-queued after failing to apply")
        if self.unreach:
            why.append(f"{len(self.unreach)} states unreachable (excluded from the denominator)")
        if self.n_failed:
            why.append(f"{self.n_failed} cases failed to apply (harness errors)")
        if self.evidence_level() == "attempted":
            why.append("application not confirmed: the runtime reported no applied/verified evidence")
        elif self.evidence_level() == "applied":
            why.append("states are applied, not verified by read-back")
        return tuple(why)

    def ledger(self) -> dict:
        """JSON-able Atlas data (spec U1 section 5): counts plus a bounded sample of unvisited states."""
        d = {"mode": "enumerated" if self.epoch_mode else "sampled", "epoch": self.epoch, "total": self.n,
             "visited": self.visited_cnt if self.epoch_mode else 0, "unreachable": len(self.unreach),
             "failed_to_apply": self.n_failed, "unverified": self.unverified_cnt,
             "evidence": self.evidence_level(), "retry_queue": len(self.retry),
             "why_not_complete": list(self.why_not_complete()), "unvisited_sample": []}
        if self.epoch_mode:
            sp, sample = self.space, []
            for r in self.retry[:5]:
                sample.append(sp.state_dict(sp.unrank(r)))
            if True:
                perm = sp.permutation(derive_seed(self.seed, self.epoch, 0x57A7E))
                j = self.index
                while j < self.n and len(sample) < 10:
                    r = perm(j)
                    j += 1
                    if r not in self.unreach:
                        sample.append(sp.state_dict(sp.unrank(r)))
            d["unvisited_sample"] = sample
        return d

    # ------------------------------------------------------------ coverage
    def coverage(self) -> Coverage:
        if self.epoch_mode:
            n, ep = self.n, self.epoch
            tgt, vis = self._target(), self.visited_cnt
            lv = self.evidence_level()
            if vis == 0 and ep >= 1:     # epoch just rolled over: the finished epoch is what we can claim
                vis_show, claim_complete = tgt, True
            else:
                vis_show, claim_complete = vis, False
            if claim_complete and not self.unreach and lv != "attempted":
                g = f"Feature states: all {n} valid configurations visited in epoch {ep} ({self._level_text()}). "
            elif claim_complete and lv != "attempted":
                g = (f"Feature states: {tgt} of {n} valid configurations visited in epoch {ep} ({self._level_text()}); "
                     f"{len(self.unreach)} unreachable. ")
            elif claim_complete:
                g = (f"Feature states: {tgt} of {n} valid configurations handed to the target in epoch {ep} "
                     f"({self._level_text()}). ")
            else:
                pct = int(100 * vis_show / tgt * 10) / 10 if tgt else 0.0
                if pct >= 100.0:
                    pct = 99.9
                g = (f"Feature states: {vis_show} of {n} valid configurations visited this epoch ({pct}%); "
                     f"{ep} full epochs completed ({self._level_text()}). ")
            g += self._caveats()
            return Coverage(states_total=n, states_visited=vis_show, epochs=ep, guarantee=g, evidence=lv,
                            state_counts=tuple(self._counts) if self._counts is not None else (),
                            visit_hist=self._hist(n, ep, vis), unreachable=len(self.unreach),
                            failed_to_apply=self.n_failed, unverified=self.unverified_cnt,
                            why_not_complete=self.why_not_complete())
        size = self.space.size
        txt = (f"about {pow2_text(size)} valid feature states" if size is not None
               else "an uncountable number of valid feature states")
        g = (f"Feature states: space too large to enumerate within budget {self.budget} ({txt}): {self.sampled} cases "
             "drawn uniformly from valid states; no coverage guarantee (NOT a complete-coverage claim), "
             "control values sampled. " + self._caveats())
        return Coverage(states_total=None, states_visited=0, epochs=0, guarantee=g, evidence=self.evidence_level(),
                        failed_to_apply=self.n_failed, why_not_complete=self.why_not_complete())

    @staticmethod
    def _hist(n: int, ep: int, ix: int) -> tuple[int, ...]:
        """Visit-count histogram, exact and O(1): ``ix`` states were visited ep+1 times, the rest ep times."""
        h = [0] * HIST_BINS
        h[min(ep + 1, HIST_BINS - 1)] += ix
        h[min(ep, HIST_BINS - 1)] += n - ix
        return tuple(h)

    # ------------------------------------------------------------ checkpoint
    def _ck_extra(self):
        return {"epoch": self.epoch, "index": self.index, "sampled": self.sampled, "visited": self.visited_cnt,
                "unverified": self.unverified_cnt, "retry": list(self.retry),
                "fails": [[r, k] for r, k in sorted(self.fails.items())], "unreach": sorted(self.unreach),
                "n_failed": self.n_failed, "n_fb": self.n_fb, "n_ver": self.n_ver, "rb": [self.rb_seen, self.rb_feats]}

    def _ck_load(self, st):
        self.epoch = int(st.get("epoch", 0))
        self.index = int(st.get("index", 0))
        self.sampled = int(st.get("sampled", 0))
        self.visited_cnt = int(st.get("visited", self.index))
        self.unverified_cnt = int(st.get("unverified", 0))
        self.retry = [int(x) for x in st.get("retry", [])]
        self.fails = {int(r): int(k) for r, k in st.get("fails", [])}
        self.unreach = {int(x) for x in st.get("unreach", [])}
        self.n_failed = int(st.get("n_failed", 0))
        self.n_fb = int(st.get("n_fb", 0))
        self.n_ver = int(st.get("n_ver", 0))
        self.rb_seen, self.rb_feats = (int(x) for x in st.get("rb", [0, 0]))
        self._inflight = {}
        self._perm = None
        if self._counts is not None:           # derived state: every rank ep times, +1 for this epoch's prefix
            ep, ix = self.epoch, self.index
            self._counts = array("B", bytes([min(ep, 255)]) * self.n)
            if ix:
                perm = self.space.permutation(derive_seed(self.seed, ep, 0x57A7E))
                for j in range(ix):
                    r = perm(j)
                    if self._counts[r] < 255:
                        self._counts[r] += 1
            for r in self.retry:                # queued states were un-counted when they failed
                if self._counts[r] > 0:
                    self._counts[r] -= 1
