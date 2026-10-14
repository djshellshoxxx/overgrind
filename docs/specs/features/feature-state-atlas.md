# U1 Feature-State Atlas: an honest coverage ledger with guarantee statements
Status: proposed. Owner module: `strategies/*`, `states.py`, `scheduler.py` (ENGINE) produce; `gui/*` renders. Types: `Coverage`, `Case`, `RunStats`, `CaseGenerator`.

## 1. Problem & why it matters
Fuzzers report "N cases run". Testers need to know *what was exercised*: which feature-state configurations, which t-way interactions, how many sweeps, and -- critically -- what may NOT be claimed. Overclaiming ("all combinations tested") is the most common credibility failure of combinatorial tools; underreporting hides blind spots. The atlas is a bounded-memory ledger of visited feature states and t-way tuples with a machine-generated guarantee sentence whose wording is determined by the evidence, not by marketing.

## 2. Prior art & what they do poorly
| Tool / source | What it does | Gap |
|---|---|---|
| NIST ACTS (generator) and Combinatorial Coverage Measurement (CCM): t-way coverage of an *existing* test set; total-variable-value configuration coverage; (t+1)-way side-coverage [V: csrc.nist.gov coverage-measurement page; Kuhn et al. ICST-W 2013] | The measurement math we reuse | Offline, batch, file-based; no notion of live runs, epochs, constraint-aware denominators over a running target, applied vs verified, or resume |
| PICT / ACTS generators [V: github.com/microsoft/pict doc] | Emit a finite suite covering t-way | Suite size fixed up front; no ledger once executed; nothing about what failed to apply |
| GUITAR event-flow graph [V: Memon WCRE'03] | Event coverage of a ripped GUI model | Event/sequence coverage, not feature-state-vector coverage; no honesty text |
| Monkey/FuzzWinForms/TestComplete/Squish | action counts | no coverage model of settings |
**Honest overlap statement**: t-way coverage measurement and constraint-aware generation exist (NIST). What we did not find in any tool reviewed: a *live*, resumable ledger over a black-box GUI target that (a) counts a tuple only when the state was **applied and read back**, (b) separates *infeasible* (constraint-excluded) from *uncovered* from *unknown*, (c) has explicit **epoch** semantics for unbounded runs, (d) emits a guarantee sentence from a fixed grammar. Novelty claim is the combination, and it must be stated as "we found no tool that..." not "no tool exists".

## 3. Design
### 3.1 Entities
- *Parameter* = feature (non-action) with `states`, or a control bucket from `domain.buckets(domain, n)`. *Valid state* = assignment to all features satisfying `ConstraintDef`s. `N_valid` from `scheduler.estimate(project)["valid_states"]` (None if > enumerate budget).
- *Tuple* = t-subset of parameters + value for each. *Feasible* iff some valid state extends it (solver/propagation). Classes: `covered`, `uncovered`, `infeasible` (proved), `unknown` (solver budget hit).

### 3.2 Three evidence levels per observation (the honesty core)
| Level | Definition | Counts toward |
|---|---|---|
| `attempted` | case handed to adapter | cases/sec only |
| `applied` | every `set_feature_state` returned without error | `states_visited` only when no read-back is configured/possible |
| `verified` | read-back (`get_control` / state selector) equals intended after `settle_ms` | `states_visited` (preferred), tway |
A case whose apply raised `HarnessError` is **not** counted (state unknown). A case that crashed/hung the target *after* apply **is** counted visited (the state was reached) and tagged `errored`. `Coverage.states_visited` counts verified if the project has read-back for >= 50% of features, else applied; guarantee text names which (`"applied (read-back available for 3/7 features)"`).

### 3.3 Epoch semantics
- Enumerable (`N_valid <= 4,194,304` default, bitset <= 512 KiB): epoch = one pass visiting each valid state exactly once in an order given by a keyed pseudo-random permutation of `[0,N)` (e.g. 4-round Feistel / cycle-walking on unrank index, key=(seed, epoch)) -> O(1) memory, no repeat inside an epoch [K: format-preserving permutation]. `states_visited` = distinct visited **in the current epoch**; at `states_visited == N_valid` -> `epochs += 1`, counter resets, `ever_visited` bitset retained. Interrupted visit (case errored harness) is retried later in the same epoch (max 3 retries then marked `unreachable` and excluded from denominator with a count shown).
- Not enumerable: `states_total=None`; ledger switches to *sampled mode* (3.5). `epochs` stays 0; never print "epoch complete".
- Project hash change (`project.project_hash`) => ledger invalidated (new run id); resume from checkpoint only if hash equal.
- Pause time irrelevant; epoch is count-based, not time-based.

### 3.4 t-way ledger
Per parameter-subset bitmap indexed by combinadic rank x mixed-radix value index (see B1/B2 spec `streaming-twise-generation.md` for generation). Memory `T` bits where `T = sum_over_subsets prod(v_i)`; default budget `mem_mb=64`. Exact mode iff `T <= 8*mem_mb*2^20`. `Coverage.tway_total = feasible + unknown`, `tway_covered` = covered; infeasible counted only in guarantee text. Also report (t+1)-way *side coverage* sample when budget remains [V: NIST notes lower-strength full coverage yields partial higher-strength coverage].

### 3.5 Sampled mode (cannot enumerate)
Deterministic hash sampling: tuple in ledger iff `H(seed_ledger, tupleid) < rho`, `rho = min(1, budget_bits / T)`. Coverage fraction estimated as `c/m` over the `m` sampled tuples with Wilson 95% interval; reported as `~X% (95% CI a-b, m=...)`. For *unseen state mass* use Good-Turing: `P(next state is new) ~ n1/n` where `n1` = states seen exactly once in a reservoir sketch (bounded, 1M entries, hash-set eviction by smallest hash = bottom-k) [K: Good 1953]. Never print an absolute "covered" for sampled mode.

### 3.6 Guarantee grammar (generated by `Coverage.guarantee`)
```
G := SCOPE " " CLAIM [" " CAVEAT]*
SCOPE  := "Feature states:" | "t=<t> interactions:"
CLAIM exact+complete  := "all <N> valid configurations visited in epoch <e> (<level>)."
CLAIM exact+partial   := "<k> of <N> valid configurations visited this epoch (<p>%); <e> full epochs completed."
CLAIM tway exact      := "<c> of <F> feasible <t>-way value tuples covered (<p>%); <I> proved infeasible by constraints; <U> undecided."
CLAIM sampled         := "estimated <p>% (95% CI a-b) of <t>-way tuples from a <m>-tuple hash sample; NOT a complete-coverage claim."
CAVEAT := "Controls: only <b> value buckets per control were explored; continuous values not exhaustively covered."
        | "Order/sequence effects and timing were not covered by this metric."
        | "<x> cases failed to apply and are excluded."
```
May claim: "every *valid* configuration of the selected two-state/multistate features, over the *selected* features, was applied (and verified) at least once". May NOT claim: coverage of values within unbounded domains; coverage of unselected features/controls; absence of defects; coverage of states hidden behind dynamic UI not modelled; order-independent behaviour. `invalid_values=True` runs are accounted separately and never raise `states_visited`.

### 3.7 Visualization (GUI hooks)
- N_valid <= 4096: grid, 1 cell/state, colour: grey unvisited, blue visited, saturating with visit count (cap 8), red outline if an error occurred in it, hatched if `unreachable`.
- 4096 < N_valid <= 4M: Hilbert-curve image up to 2048x2048 (1 pixel/state, 2 px beyond 1M), rendered from the bitset on a timer (<=1 Hz); hover maps pixel->state via unrank.
- t-way: n x n pair-matrix heatmap (cell = fraction of feasible value pairs covered, diagonal = single-feature state coverage); click cell -> list of missing tuples (<= 200 shown, "export all").
- Timeline: coverage % vs cases (sampled every 1 s, bounded ring 3600) with epoch markers; flat-line for >10 min annotated "saturated".
- Always show the guarantee sentence verbatim above the picture; copy-to-report button.

## 4. Data / API contracts
- `Coverage` (types.py) used as is; `guarantee` filled by `strategies.guarantee_text(ledger, project)`. Delta: `evidence_level`, `unreachable`, `infeasible`, `unknown`, `errored` counts do not exist in `Coverage` -> carried in `CaseGenerator.checkpoint()["ledger"]` and in `guarantee` text; GUI reads via `scheduler.ledger_snapshot(gen)->dict` (new, ENGINE).
- `CaseGenerator.feedback(case, outcome)`: `outcome.detail["applied"]: bool`, `["verified"]: bool` (RUNTIME sets) -> ledger decides counting. Outcome without these keys => treated `applied`.
- Checkpoint: epoch, ledger counters, permutation key, bitset (zlib+base64 when <= 256 KiB; else sidecar file path `checkpoint_dir/ledger.bin` written atomically) -- keeps "small JSON-able state" true.
- Pure functions (testable without Windows): `Ledger.observe(case, level)`, `Ledger.snapshot()->Coverage`, `unrank(i)`, `epoch_permutation(key,N)`.

## 5. UX hooks
Run tab "Coverage" panel: guarantee sentence, epoch counter, progress ring, matrix/Hilbert view, "why not 100%?" list (infeasible, unreachable, failed-to-apply with counts); project editor shows live `N_valid`, before/after effect of each constraint; export "coverage report" (markdown + CSV of uncovered tuples).

## 6. Acceptance tests
1. 3 independent toggles, include_all_off=True: exactly 8 states, no repeat within epoch 0, after 8 cases `epochs==1`, second epoch a different order (seed-dependent) yet again 8 distinct.
2. `include_all_off=False`: 7 states; denominator 7.
3. mutex(A,B): 3 of 4 combos valid; both-on never produced; `states_total==3`.
4. 20 toggles (1,048,576 states): permutation property -- first 10,000 outputs distinct; bitset memory <= 200 KiB (tracemalloc).
5. HarnessError during apply: case not counted; state re-queued; after 3 failures `unreachable` shown and removed from denominator, guarantee mentions count.
6. Read-back mismatch case counts `applied` not `verified`; guarantee text names the level.
7. Pairwise ledger: 4 params x 3 buckets: T=54 tuples; all-coverage via exhaustive 81 states gives 54/54; with `requires` making 6 tuples infeasible: `tway_total==48`, text says "6 proved infeasible".
8. Sampled mode forced (mem_mb=0.001): text contains "NOT a complete-coverage claim"; never contains "all".
9. Guarantee grammar fuzz: for 500 random ledger states the text never contains "all" / "100%" unless exact+complete.
10. Checkpoint round-trip at case 5,000 reproduces identical next 1,000 cases and identical `Coverage`.
11. Project hash change => restore refuses, new ledger.
12. 10 million `observe` calls: RSS growth < 5 MiB.

## 7. Metrics
Distinct valid states per hour; epochs/day; time-to-first-complete-epoch; fraction of tuples `unknown` (solver budget health); % cases excluded as failed-to-apply (harness quality); ledger memory; ledger bytes in checkpoint.

## 8. Risks
- Constraint model wrong (U4) => denominator wrong; mitigated by showing constraint effects and "unreachable" accounting.
- Read-back unavailable for many apps => most coverage is `applied` only; text must say so.
- Users read the sentence as a defect-free guarantee; mitigation: fixed caveat line, and no green "100%" badge for sampled mode.
- Feistel/unrank bugs silently skip states; mitigated by exhaustive tests on N<=2^16.
- Good-Turing estimate misleads under non-uniform sampling (adaptive strategy); disable estimate when strategy is `adaptive`/`chaos`.

## 9. Design deltas
1. `Coverage` lacks fields for evidence level / infeasible / unknown / unreachable -> carry in `guarantee` + checkpoint; propose adding `extra: dict` (defaulted) in types.py.
2. `Outcome.detail` keys `applied`/`verified` become part of the RUNTIME->ENGINE contract.
3. Checkpoint may reference a sidecar file (breaks "pure small JSON" for > 4M states or big t-way bitmaps).
4. `states_visited` is per-epoch (resets), differs from continuous-gui spec wording "track coverage progress"; lifetime stat kept as `ever_visited` in snapshot.
5. Bucket count for coverage claims is a project-level setting (`strategy_options["buckets"]`), not per-control.

## Sources
- https://csrc.nist.gov/Projects/automated-combinatorial-testing-for-software/combinatorial-coverage-measurement/coverage-measurement
- https://csrc.nist.gov/Projects/automated-combinatorial-testing-for-software/combinatorial-methods-in-testing/interactions-involved-in-software-failures
- https://github.com/microsoft/pict/blob/main/doc/pict.md
- https://www.cs.umd.edu/~atif/pubs/MemonWCRE2003-abstract.html
