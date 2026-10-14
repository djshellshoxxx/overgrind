# B2 Streaming t-way Generation: constraint-aware, bounded-memory, never-ending covering stream
Status: proposed. Owner: `strategies/twise.py`, `constraints.py`, `states.py`, `scheduler.py` (ENGINE). Types: `CaseGenerator`, `Case`, `Coverage(tway_t, tway_total, tway_covered, buckets_*)`, `ConstraintDef`, `Domain`, `RunConfig(strategy="twise", strategy_options)`.

## 1. Problem & why it matters
Pairwise/t-way is the standard way to get high interaction coverage without the full product, but existing generators are *batch*: they compute one finite array. Overgrind runs until stopped, must start executing immediately, survive restarts, honour constraints, fit in bounded memory when `n` parameters x `v` values makes the tuple space huge, say honestly what is covered, and keep producing useful, diverse cases after the array is complete.

## 2. Prior art & what they do poorly
| Source | Approach | Notes / gap |
|---|---|---|
| AETG (Cohen, Dalal, Fredman, Patton, IEEE TSE 23(7) 1997) [V: bibliographic; S: described as greedy one-test-at-a-time] | greedy, builds each test from candidates maximizing new tuples | batch output; commonly cited as slower than IPO-style on large inputs [S] |
| IPO / IPOG / IPOG-F (Lei & Tai 1998; Lei, Kacker, Kuhn, Okun, Lawrence ECBS 2007; refinements Forbes et al. 2008) [V] | parameter-at-a-time horizontal+vertical growth; refinements ~5x faster (up to 280x) and ~5% smaller arrays [V: NIST JRES abstract] | needs whole array in memory, parameters must be known; vertical growth stage can need all uncovered tuples |
| Simulated annealing (Cohen, Colbourn, Ling; ISSRE'03; Discrete Math 2008) [V] | metaheuristic, strength 3, smaller arrays | slow; fixed size N must be guessed; metaheuristics usually smaller but greedy faster [S survey] |
| PICT [V: pict.md] | deterministic heuristic packing; result size varies ~5-10% by seed; `/b` runs N seeds keep smallest; constraints enforced during generation; seeding, sub-models (mixed strength), weights, negative values; `/o` order up to n | batch; no streaming/resume; no coverage accounting after run |
| NIST ACTS [V: NIST tools page] | IPOG family, t=1..6, constraints, variable strength | batch Java tool |
| Constrained CT survey (arXiv 1908.02480) [V] | three frameworks: one-test-at-a-time, parameter-at-a-time, whole array | none streaming w/ bounded memory found |
| Sarkar & Colbourn two-stage [S] | randomized time/memory-efficient first stage, then search for remainder | closest to bounded-memory; not streaming/resumable |
Effectiveness backdrop: NIST reports failures involve few conditions (1-6 way), 4-6-way coverage found all faults in studies, "pseudo-exhaustive, not a guarantee" [V: csrc.nist.gov interactions page]. Honest overlap: algorithms (greedy, IPO, constraints, mixed strength) are known; our contribution is the *streaming, checkpointable, bounded-memory, infeasible/unknown-accounting* packaging and its use as an infinite scheduler. We did not find a published streaming t-way generator with these properties (search negative; not proof).

## 3. Design
### 3.1 Parameters and tuples
Parameters `P_1..P_n`: every non-action feature (values = states) and every fuzzable control that the user marked "include in t-way" (values = `domain.buckets(domain, b)`, default `b=3`: low-boundary / default-or-mid / high-boundary; enum: up to 6 options else bucketed). `v_i` = value count. Tuple space `T = sum over t-subsets S of prod_{i in S} v_i`. Example unconstrained n=10, v=3, t=2: C(10,2)*9 = 405. Memory worst-case examples: n=500, v=3, t=3: 20,708,500 * 27 = 5.6e8 bits = 66.6 MiB; t=4 => C(500,4)=2.57e9 * 81 = 2.1e11 bits (~24 GiB): sampled mode mandatory.
Tuple id = `(rank_of_subset via combinadic, mixed-radix value index)`; bit index = `offset[subset] + valueindex`; `offset` table is O(C(n,t)) ints -> if `C(n,t) > 5e6` use hashed sampling (3.5) because even offsets exceed budget.

### 3.2 Constraints
`constraints.py` compiles `ConstraintDef` (mutex, requires, forbid) to nogoods (partial assignments that are invalid). `feasible(partial)->True|False|None` via unit propagation + bounded DFS (node budget `solver_nodes=2000`; `None` = unknown). Tuple classes: covered, uncovered, infeasible (False), unknown (None). Control `scope` is not a state constraint (it gates writes) so it does not remove tuples. A tuple is "infeasible" only when proved; budget exhaustion yields `unknown`, counted in the denominator (conservative) and reported.

### 3.3 One-test-at-a-time with tuple seeding (streaming core)
Per emitted case:
1. Pick seed tuple: advance a cursor over uncovered bits (word-level `bit_scan`; per-subset first-zero hints); skip classes infeasible; if `feasible(seed)` is False -> mark infeasible, continue; None -> mark unknown (retry later with larger budget).
2. Build `m` candidate tests (default 20; AETG-style [S]): fix seed values, then randomized order over remaining parameters; for each parameter pick the value (ties random) maximizing newly covered tuples w.r.t. already-assigned parameters, subject to `feasible(partial)`; if dead-end, backtrack within `bt=50` steps else discard candidate.
3. Choose candidate covering most uncovered tuples (>= 1 guaranteed by the seed); mark all tuples of the chosen test covered; emit `Case(state=..., values=..., tags=("twise:t=2","epoch:e"))`; controls not in t-way parameters are drawn by `domain.sample` (random in domain), so non-bucket parameters keep exploring.
Cost per test ~ `m * n * v * C(n-1,t-1)` bit lookups in the naive form; for n=40, v=3, t=3, m=20: 20*40*3*741 = 1.8M (too slow in CPython at ~1 test/s). Mitigations (in order): (a) evaluate gain only against *uncovered-neighbour lists* (precomputed lazily per parameter pair while coverage < 90%), (b) after 90% coverage switch to *seed-only* completion (m=4), (c) vectorize with `bytearray`/`int` bitsets, (d) cap `time_per_case_ms=50`, falling back to seed-plus-random fill. Target: >= 200 cases/s for n<=20,t=2,v<=4 in pure Python (checked by test); throughput matters little vs GUI latency (~10 cases/s) so generation is never the bottleneck at default sizes.
Streaming/prefix property: each emitted test is chosen greedily, so any prefix has near-best coverage-per-test for that length; consumers may stop at any time. Expected size bound (unconstrained, uniform v): greedy with expected gain >= `U*v^-t` per step needs at most `ceil(ln(T)/(-ln(1-v^-t)))+1` tests (Johnson/Lovasz-Stein style argument [K]); e.g. 405 tuples, v=3, t=2: <= 52; real greedy typically far fewer. Compare with PICT row counts offline when PICT available (CI optional); do not claim parity.

### 3.4 After coverage completes ("continues beyond coverage")
The stream never ends. Epoch `e` ends when all feasible tuples covered at least `k_e` times (k_0=1). Then in order: (1) `e+=1`, re-randomize the coverage order and **raise multiplicity** to `k=2` (every tuple twice, with a fresh seed so contexts differ) up to `k_max=3`; (2) track **(t+1)-way sampled coverage** with leftover memory and bias candidate choice by (t+1)-way gain (cheap proxy: random tie-breaks prefer unseen (t+1)-way hash bucket); (3) interleave per `scheduler` weights default 70% t-way stream / 15% boundary / 15% adaptive-or-random (`strategy_options["mix"]`), so continuing runs spend time on fresh value draws inside buckets (`domain.sample`) rather than replaying identical bucket vectors. Reported as `Coverage.epochs`, guarantee text "all feasible t-way bucket tuples covered >= k times".
When ledger shows no new tuples and user also `coverage_complete=True` stop condition => stop (RunConfig). `Coverage.tway_total/covered` count k=1 coverage only; multiplicity in guarantee text.

### 3.5 Bounded memory and sampled mode (honesty)
Budget `mem_mb` (default 64 MiB for the bitmap). Exact mode iff `T/8 <= budget`. Else **hash-sampled mode**: tuple tracked iff `H64(seed_l, tuple_key) < rho*2^64`, `rho = budget_bits/T`; seeds for generation use the *sampled* uncovered tuples only (cursor over a hashed subset, enumerated lazily by iterating subsets and testing the hash -- cost accepted). Coverage reported as `c/m` of the `m` sampled tuples with Wilson 95% CI; `Coverage.tway_total=None` and guarantee text states "estimated ... NOT a complete-coverage claim" (see U1). No claim of strength-t coverage is ever made in sampled mode. Generation still yields tests with high marginal coverage for the sampled tuples and random-fill elsewhere.
Memory beyond bitmap: O(n*m_candidates) per candidate; `checkpoint()` holds `{seed, emitted N, epoch, k, cursor, rho, rng state}`; bitmap reconstructed by **deterministic replay** of the N emitted cases when `N <= 200,000` (default) else written to a sidecar file `bitmap.zlib` beside the checkpoint (atomic) -- the dict then carries `bitmap_path`.

### 3.6 Infeasible-tuple accounting
Counters kept: `covered, uncovered, infeasible, unknown` and `skipped_seeds`. Each infeasible proof stores the minimal conflicting nogood (for UI "why": e.g. "A=on conflicts with B=on (mutex)"). Unknowns are retried with doubled `solver_nodes` at epoch boundaries. Denominator for percentages = covered + uncovered + unknown; infeasible reported separately (never silently dropped).

### 3.7 Options
`strategy_options`: `t` (default 2; allow 1-6, warn > 4), `buckets` (3), `mem_mb` (64), `candidates` (20), `solver_nodes` (2000), `k_max` (3), `mix` ({"twise":0.7,"boundary":0.15,"adaptive":0.15}), `strength_groups` (list of feature-id lists with own t, PICT sub-model-like [V]), `weights` (value -> positive int hint, PICT-like [V]), `seed_cases` (list of Cases to pre-cover, PICT `/e` like [V]).

## 4. Data / API contracts
- `strategies.twise.TwiseGenerator(project, seed, options)` implements `CaseGenerator`: `__iter__` infinite; `coverage()->Coverage` (tway_t, tway_total, tway_covered, buckets_total = sum v_i, buckets_visited, guarantee); `feedback(case, outcome)`: if `outcome.detail.get("applied") is False` the case's tuples are un-marked (not covered) so they get re-seeded; `checkpoint()/restore()` as above.
- `constraints.feasible(partial: dict[str,str]) -> bool | None`; `constraints.nogoods(project)`.
- `scheduler.estimate(project)` extended: `{"tway_tuples": T, "mode": "exact"|"sampled", "mem_mb": ..., "text": ...}` (adds keys; existing keys unchanged).
- Pure/no-IO: deterministic given `(project_hash, seed, options)`.

## 5. UX hooks
Strategy panel: t selector with live `T`, memory and "exact/sampled" badge before start; per-strength progress bars (t, t+1 sampled); infeasible list with reasons; "estimated cases to complete" from greedy bound (flagged estimate); pair-matrix heatmap shared with U1.

## 6. Acceptance tests
1. n=10, v=3, t=2, no constraints: every one of 405 tuples covered; case count <= 52 (bound) in 20 seeds; `tway_covered==405`.
2. t=3, n=8, v=2: all `C(8,3)*8=448` tuples covered; count <= `ceil(ln448/-ln(1-1/8))+1=47`.
3. Constraint `mutex(A,B)` and `requires(C->A)`: no emitted case violates (property test 10k cases); tuples {A=on,B=on} and {C=on,A=off} counted infeasible, and `tway_total` excludes them.
4. Solver budget 1 node on a constraint-rich model => `unknown>0` reported and included in denominator; doubling budget at epoch end reduces unknown.
5. Prefix property: coverage after N cases >= 0.9 * (coverage of N random uniform cases) + margin for N in {10,20,50}; and strictly more than random for 95% of seeds.
6. Determinism: same (project, seed) => identical first 5,000 cases; checkpoint at 777 + restore => identical continuation (replay path) and (sidecar path with forced small threshold).
7. Memory: n=60,t=2,v=3 run 200k cases: tracemalloc peak growth after warm-up < 5 MiB beyond bitmap.
8. Sampled mode forced (`mem_mb` tiny): `tway_total is None`, guarantee contains "NOT a complete-coverage claim", CI width shrinks with more tests; `Coverage` never claims completion.
9. Beyond coverage: after epoch 0 completes, next emitted cases cover tuples a second time (k=2) before epoch 2; mix yields boundary and random-sample cases in configured proportion (+-5%).
10. `feedback(applied=False)` re-queues the lost tuples (coverage decreases accordingly).
11. Throughput: >= 200 cases/s for n=20,t=2,v=3 in CPython 3.10+.
12. Mixed strength: group `{A,B,C}` with t=3 and rest t=2 covers all group triples and all pairs.
13. Cross-check vs reference: for n=4,v=2,t=2 exhaustive brute-force coverage check of every emitted prefix.

## 7. Metrics
Tests per full t-way coverage vs lower bound `v^t` and vs bound formula; coverage-vs-cases curve AUC; fraction infeasible/unknown; generation ms/case; bitmap MiB; solver nodes per case; share of cases adding new tuples; defects found per t-way tuple (does higher t pay off).

## 8. Risks
Bucket choice drives validity: coverage of *buckets* is not coverage of *values* (guarantee text says so); wrong constraints distort tuples (U4); bit-scan cursor in Python may be slow for 5e8 bits (word-level ints, chunked); greedy size may exceed PICT/ACTS by 10-50% (unmeasured; PICT itself varies 5-10% by seed [V]); sampled mode statistical claims depend on hash uniformity; checkpoint replay cost for long runs; large t explodes (warn).

## 9. Design deltas
1. `CaseGenerator.checkpoint()` "small JSON-able" is violated by big bitmaps -> sidecar path option.
2. `strategy_options` gains many keys (documented above) -- not enumerated in types.py (it is `dict[str, Any]`, compatible).
3. Bucket count is global (`buckets`) rather than per-`Domain`; `domain.buckets(domain, n)` already supports n.
4. `feedback()` needs `Outcome.detail["applied"]` (see U1); an un-applied case must un-cover its tuples.
5. Streaming generation continues past coverage via multiplicity epochs + mix; the continuous spec only says "continue visiting configurations while sampling new vectors".
6. `Coverage.tway_total` includes `unknown`; infeasible reported only in text.

## Sources
- https://github.com/microsoft/pict/blob/main/doc/pict.md ; https://csrc.nist.gov/Projects/Automated-Combinatorial-Testing-for-Software/Downloadable-Tools
- https://csrc.nist.gov/pubs/conference/2007/03/29/ipog-a-general-strategy-for-tway-software-testing/final ; https://www.nist.gov/publications/refining-parameter-order-strategy-constructing-covering-arrrays
- https://cse.unl.edu/~myra/papers/issre_03.pdf ; https://doi.org/10.1145/1273463.1273482 ; https://arxiv.org/pdf/1908.02480
- https://csrc.nist.gov/Projects/automated-combinatorial-testing-for-software/combinatorial-methods-in-testing/interactions-involved-in-software-failures
