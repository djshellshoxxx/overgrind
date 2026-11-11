# U3 Auto-Reduce Reproducer: reliability classification, ddmin/HDD reduction, value shrinking, issue export
Status: proposed. Owner: `reduce.py` (DIAG) + `evidence.py`. Types: `ErrorEvent(.reliability, .reduced, .history, .case)`, `Case`, `Action`, `RunConfig.confirm_runs`, `Outcome`, `TargetAdapter`, `AdapterSpec`.

## 1. Problem & why it matters
A fuzz failure is `state vector + 40 control values + a 200-action ring`. Developers need "turn on A, set Width=0, press Apply -> crash", reproduced N/M times, with an honest statement of how reliable it is. Un-reduced, un-classified failures are ignored; flaky ones are mislabelled "fixed" after one lucky pass.

## 2. Prior art & what they do poorly
| Source | What it does | Gap |
|---|---|---|
| ddmin (Zeller & Hildebrandt) [V: TSE 2002 / ISSTA 2000 pdf] | 1-minimal failing input; O(n^2) worst case; full minimality needs 2^n-1 subsets | assumes deterministic test; text inputs |
| HDD (Misherghi & Su 2006), SimplyDroid, "Minimizing GUI event traces" (FSE'16), DetReduce [V: ICSE'18; abstracts] | structure-aware / nondeterminism-aware event-trace reduction for Android; DetReduce notes ddmin-style minimizers scale poorly past ~1,000 inputs and need cheap feasibility checks | Android-only, trace-only: do not reduce *parameter values* or the *feature-state vector*; no statistical reliability label; no export |
| FuzzWinForms [strategy doc] | replay minimizer for Windows Forms | event list only; framework-specific |
| Hypothesis/QuickCheck shrinking [K] | value shrinking toward simple | property-test domain, in-process |
Honest overlap: ddmin and GUI-trace reduction are established; Overgrind's addition is applying it at three levels at once (assignment set, order/history, values) on a black-box Windows target, with a binomial-bounded reliability verdict driving the number of retries, and a safe-by-construction store (original never mutated).

## 3. Design
### 3.1 Reliability classification (before reducing)
Replay the *failing case from a fresh target* (restart + `adapter.reset`, then re-apply `ErrorEvent.history` actions since last reset, then the failing case) `n` times; outcome per replay = `fail-same-signature` (success for us) | `pass` | `other-failure` (different signature, recorded separately) | `harness` (excluded, not counted, retried up to 2x). Original observation is **not** counted (selection bias: it was picked because it failed).
Let `r` = same-signature failures out of `n` valid replays; one-sided 95% exact (Clopper-Pearson) bounds; for r=0 or r=n they reduce to `1-0.05^(1/n)` and `0.05^(1/n)`:
| n | r=n -> reproduce prob p >= | r=0 -> p <= (exact) | rule-of-three 3/n |
|---|---|---|---|
| 2 | 0.224 | 0.776 | 1.5 (useless) |
| 3 | 0.368 | 0.632 | 1.0 |
| 5 | 0.549 | 0.451 | 0.60 |
| 10 | 0.741 | 0.259 | 0.30 |
| 20 | 0.861 | 0.139 | 0.15 |
| 59 | 0.951 | 0.050 | 0.051 |
(rule of three: 0 events in n trials => p < ~3/n at 95% [V: Int J Biostat 2022 "modified rule of three"; exact bound is `1-alpha^(1/n)`; n=10 gives 0.259 vs 0.30 [V]].)
Labels (`ErrorEvent.reliability`): **reliable** = r==n and n>=3 (p>=0.37) -- default plan runs n=5 (p>=0.55); **flaky** = 0<r<n, report `p_hat=r/n` with CP interval; **not_reproducible** = r==0 and n>=5 (p<=0.45 "may still exist at that rate"), n<5 stays **unconfirmed**. Extension rule: if r==0 and the failure category is `hang` or `resource`, extend to n=10; if the first `RunConfig.confirm_runs` replays disagree, extend to n=10 to pin `p_hat`. Hard caps: n<=10 by default, <=59 only when user clicks "Estimate rate"; wall budget 5 min.
Choosing replays to **rule out** a rate eps at 95%: `n = ceil(ln 0.05 / ln(1-eps))`: eps=0.5 -> 5, 0.2 -> 14, 0.1 -> 29, 0.05 -> 59, 0.01 -> 299. UI wording always "not reproduced in n tries; true rate likely < x%", never "fixed".
Replay isolation policy: crash/dialog/assertion => restart target per replay; hang => restart too; `reset_policy=never` runs (stateful chaos) replay the full ring from process start. Restart cost bounds `n`.

### 3.2 Reduction oracle
`test(candidate) -> FAIL | PASS | UNRESOLVED` (ddmin's three outcomes [K]): FAIL = same signature (category + normalized key, see failure-detection research); PASS = ran clean; UNRESOLVED = harness error, different signature, constraint-invalid candidate, or element unavailable. UNRESOLVED is treated as PASS for deletion decisions (never accept a deletion we cannot justify) but logged.
Flaky targets: with estimate `p_hat` from 3.1, each *negative* candidate is retried `m = ceil(ln(beta)/ln(1-p_hat))` times (beta=0.05 false-negative per test; p_hat=0.5 -> 5; 0.2 -> 14; 0.1 -> 29), capped by `max_tries_per_test=10`; if cap binds, flag `reduced.partial=true, reason="low reproduction rate"`. FAIL on first success (no retries for positives). Positive-only evidence keeps the reduced case valid: final candidate must reproduce >= `final_confirm` times (default 3 of 5 if flaky; 5/5 if reliable) else fall back to the previous accepted candidate.

### 3.3 Three-level reduction (cheap first)
1. **Prefix/suffix cut**: drop history after the failure point and before the last reset (ring begins mid-state: restore to state at ring start is unknown => prefer last `reset` action; if none, mark `reduced.assumes_unknown_prestate=true`).
2. **Assignment-set ddmin** (HDD-like hierarchy): the failing `Case` is a set of absolute assignments `{feature->state, control->value}`; deleting an element = revert it to its default (`FeatureDef.default_state`, `ControlDef.default`), which is always representable and mostly constraint-valid (invalid by `ConstraintDef` => candidate UNRESOLVED, kept). Levels: (a) feature groups sharing a scope -> (b) individual features -> (c) controls; ddmin per level, granularity starts at n=2, doubles; complexity worst-case O(n^2) tests [V], typical far fewer; |set| is ~tens so replays ~ 50-300.
3. **Order/history ddmin** (only when `reset_policy != every_case` or `Case.order` matters): ddmin over `ErrorEvent.history` entries of kind `state|control|invoke`; keep `reset|wait|restart` structural markers unless they are themselves candidates; remove `wait` shrink dwell (`Case.dwell_ms` toward 0 by halving, then compare).
Budget: `max_replays=400`, `max_seconds=600`; each replay's cost estimated from first replay time `t0`; plan printed ("est. 6 min") before start; user can cancel; partial result is valid and saved at every accepted step (atomic).
n=200 ring with worst-case O(n^2)=40,000 replays is not feasible; steps 1-2 cut n to tens first. (DetReduce reports ddmin-style GUI reducers do not scale past ~1,000 inputs [V].)

### 3.4 Value shrinking (after set reduction; remaining non-default controls only)
Goal "simplest value that still fails", order of attempts: default -> domain boundary nearest default -> `0`/`min` -> bisect toward default.
- bool/choice: default or first option; integer/float: try default, `min`, `max`, `0`, then binary search on the interval between failing value and default (stop at interval width <= step or 10 iterations); floats: round to fewer significant digits (17->6->3->1), snap to step grid; strict monotonicity is not assumed -- each probe is a full test.
- text: ddmin over characters, then replace chars by `"a"`/`"0"` class representatives, then shorten to boundary lengths (0, 1, max_length, max_length+1 only if `invalid_values`). `sensitive` controls: shrink locally, export as `<redacted len=N class=alnum>` with a note; reproduction file ships with `"sensitive_values_local": true`.
Time cap per control 60 s; all probes counted in `max_replays`.

### 3.5 Safety / non-destructiveness
Never mutate the original: output goes to `EvidenceStore` sidecar `<id>.reduced.json` (atomic temp+rename) and sets `ErrorEvent.reduced` view on load; original JSON and attachments untouched (strategy doc D). Reducer respects danger guard (U2): candidates are subsets of already-executed actions so no new risk, but value shrinking never introduces values outside `fuzz_domain` unless the original had them. Replays run with the same `RunConfig` as the failing run except `capture_dump=False` unless requested. Different-signature failures found during reduction are saved as *new* events (bonus finds) not discarded.

### 3.6 Export
`export_issue(event, fmt)`: Markdown (GitHub-ready): title `[category] <control> -> <signature8>`; environment (OS build, target exe path basename + SHA-256 + version, Overgrind version, project hash); **Reliability sentence** ("Reproduced 5/5 from a fresh launch (p >= 0.55 at 95%)" / "Reproduced 2/10 (20%, 95% CI 3-56%)" / "Not reproduced in 5 tries; seen once"); minimal steps numbered in human words ("1. Set Feature 'Dither' = on ..."); values table (original vs reduced); expected/actual; attachments list; `overgrind repro <file>.json` command; JSON machine form (versioned, includes selectors, seed, Case, actions, reduction stats). Redaction pass for `sensitive` controls and normalized paths. No screenshots/dumps embedded by default.

## 4. Data / API contracts
- `reduce.Reducer(project, adapter_spec).reduce(event)->ErrorEvent` (as in architecture). Options object `ReduceOptions(replays=5, max_replays=400, max_seconds=600, beta=0.05, final_confirm=3, restart_per_replay=True)`.
- `reduce.classify_reliability(project, adapter_spec, event, n=5)->ReliabilityResult(label, n, r, p_lo, p_hi, other_signatures:list)`; label values exactly `ErrorEvent.reliability` strings.
- `ErrorEvent.reduced` schema v1: `{"version":1,"case":{state,values,order,dwell_ms},"actions":[...],"removed":{"features":[..],"controls":[..],"actions":int},"values_shrunk":{ctrl:{from,to}},"replays":int,"seconds":float,"partial":bool,"reason":str,"final_confirm":{"n":int,"r":int},"signature":str,"base_event_hash":str,"redactions":[...]}`.
- Reliability stats persisted in `ErrorEvent.env["reliability"]={"n","r","p_lo","p_hi","level":0.95}` (no new field).
- `evidence.EvidenceStore.write_reduced(id, dict)` atomic; `export_issue(event, "md"|"json")->str`.

## 5. UX hooks
Error list column "Reliability" (chip + n/r); details pane "Reproduce" (spinner, ETA, cancel), "Reduce" progress "removed 31 of 38 settings"; diff view original vs reduced; "Copy issue" / "Save .json"; "Estimate rate (59 tries)" button; warning chip "flaky: reduction may be incomplete".

## 6. Acceptance tests (MockAdapter deterministic crash rules)
1. Mock crashes iff `A=on and W==0`; Case has 12 features, 20 controls non-default => reduced to exactly {A=on, W=0}; replays <= 150; others reverted to defaults.
2. Same, but crash also needs `B=on` within history order A then B => history ddmin yields [A, B] in that order.
3. Crash probability 0.5 per replay: 200 trials of classification => label `flaky` in >= 95% (never `reliable` with n=5 more than 3.2% of runs [0.5^5=0.031]); crash prob 1.0 => `reliable`; crash prob 0 => `not_reproducible` after n=5.
4. Flaky reduction p=0.2: each negative retried 14x (assert call counts), final result still reproduces >= 3/5.
5. Value shrink: crash iff `x >= 37` (integer 0..100, default 10, failing 91) => shrinks to 37 within <= 12 probes; text crash iff len>=5 with failing 80-char string => length 5, chars simplified.
6. UNRESOLVED handling: candidate violating `mutex` is never accepted; harness errors do not shrink the case.
7. Original `ErrorEvent` file bytes unchanged after reduction (hash compare); `.reduced.json` written atomically (kill mid-write leaves no partial file).
8. Budget: with `max_replays=10` returns `partial=True` and a valid still-failing case.
9. Different-signature failure during reduction stored as a new event with `tags` back-reference.
10. Export: markdown contains reliability sentence, SHA-256 of target, numbered steps; sensitive values absent (grep test).
11. Determinism: same seed/event => identical reduced case (ddmin partition order fixed).
12. Cancel at any point leaves last accepted candidate saved.

## 7. Metrics
Reduction ratio (settings removed / non-default settings), replays per reduction, seconds, % events reliable/flaky/not_reproducible, % reductions partial, time-to-first-export; developer feedback "repro worked first try".

## 8. Risks
Restart-per-replay cost makes flaky hang reduction slow; state leakage between replays via files/registry/settings makes "flaky" artifacts (mitigate: restart + reset + optionally clean profile dir); signature too strict => reduction aborts at step 0; too loose => reduces to a different bug (mitigate: full key match + other-signature capture); over-trust in "reliable" with n=3; replaying destructive paths (guard by U2).

## 9. Design deltas
1. `RunConfig.confirm_runs` default 2 is too low for the label "reliable" (p>=0.22 only); recommend default 3 minimum, planned replays 5 (treat field as the *minimum*).
2. Reliability numbers have no home in `ErrorEvent` -> `env["reliability"]`; propose first-class `reliability_stats: dict`.
3. `ErrorEvent.reduced` stored in a sidecar file, not rewritten in the primary event JSON (original immutability).
4. Reduction operates on absolute assignments (revert-to-default), which requires `Case.state/values` to be complete and defaults known; `Action` history is used only for stateful runs.
5. Needs adapter capability `restart()` (fresh launch) in `TargetAdapter` (currently only `detach(kill)`+`attach`): composition suffices, but cost model assumes it.

## Sources
- https://www.st.cs.uni-saarland.de/papers/tse2002/tse2002.pdf ; https://www.st.cs.uni-saarland.de/publications/files/hildebrandt-issta-2000.pdf
- https://wenyuwang.web.illinois.edu/pub/icse18-detreduce.pdf ; SimplyDroid (ASE 2017) https://scholars.cityu.edu.hk/en/publications/simplydroid-efficient-event-sequence-simplification-for-android-a/
- https://www.degruyterbrill.com/document/doi/10.1515/ijb-2022-0061/html (rule of three); Clopper-Pearson exact bounds [K]
- Probabilistic delta debugging / HDD original paper: not retrieved (only mentioned in secondary sources).
