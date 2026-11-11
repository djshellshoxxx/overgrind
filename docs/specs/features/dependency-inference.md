# U4 Dependency Inference: propose enabled / visible / mutex / requires relations from UIA observations
Status: proposed. Owner: `infer.py` (DIAG); reads via `TargetAdapter`; output consumed by `project.py`/GUI. Types: `ConstraintDef(kind mutex|requires|forbid)`, `ControlDef.scope`, `FeatureDef`, `ElementInfo`, `Provenance`, `HarnessError`, `Coverage`.

## 1. Problem & why it matters
Testers must hand-declare "control X only matters when feature Y is on", "A and B are exclusive". Missing constraints => invalid states are executed (harness errors, wasted budget, wrong coverage denominators). Wrong constraints => valid states silently excluded (lost coverage). The tool can *observe* these relations on the live app, but only reliable evidence (interventions with controls) and a human gate make that safe.

## 2. Prior art & what they do poorly
| Source | What | Gap |
|---|---|---|
| GUI ripping / GUITAR event-flow graph [V: Memon WCRE'03 abstract; arXiv 1702.06968 overview: not every widget is actionable at all times] | extracts widgets/events and which events are enabled after which | model is for sequence testing; no statistics, no confidence, no human-approval workflow, no constraint output for state-vector coverage |
| Event-dependency graphs from bytecode (arXiv 1205.4928) [V] | data dependencies between handlers | needs source/bytecode |
| Yuan & Memon, execution-feedback relationships between events (IST 2010) [V: abstract only] | run-time effects of events on other events | event-ordering, not enabled/visible/mutex of settings; full text not read |
| NIST ACTS / PICT constraints [V] | accept hand-written constraints | no discovery |
| Daikon-style invariant inference [K] | likely invariants from traces | code-level |
Honest overlap: GUI ripping already infers enabledness structure; **we found no tool that proposes state-vector constraints (mutex/requires/forbid) for black-box GUIs from controlled interventions with significance control and per-proposal approval** -- stated as "not found", not "novel".

## 3. Design
### 3.1 Relations
| id | Meaning | Evidence |
|---|---|---|
| `enable(c|F)` | control/element `c` is enabled (UIA `IsEnabled`) iff predicate F over features holds | contrast of c.enabled across feature interventions |
| `visible(c|F)` | element exists/`IsOffscreen=False` iff F | same with existence |
| `mutex(f,g)` | setting f active forces g inactive (radio-like) | after set f, read-back g changes from active to inactive in all trials where g was active |
| `requires(f -> g)` | f cannot be activated unless g active | attempt set f with g inactive: read-back f != intended (or f disabled) in all trials; with g active f sticks |
| `forbid(assign)` | joint assignment cannot be reached | write-ignored / auto-correct observed |
Output mapping: `enable/visible(c|F)` -> `ControlDef.scope` (all features in a conjunctive F must be active; non-conjunctive F reported as "complex" with no auto-mapping); `mutex/requires/forbid` -> `ConstraintDef`.

### 3.2 Observation protocol (active, causal)
Passive logs from a fuzz run are confounded (many factors change at once, async UI updates). Use **interventions** from a known baseline (`adapter.reset`):
1. Candidate set: features F (toggle/multistate, not `dangerous`, enabled) and UI elements E (selected controls + optional "watch list" from `inspect()` capped at 200).
2. OFAT sweep: for each f and each non-default state s: set f=s from baseline, `wait_quiescent`, read for every e in E: `exists`, `IsOffscreen`, `IsEnabled` (+ current value of features' read-back for mutex). Restore. Reads repeated after `2*settle_ms`; a disagreeing pair marks the cell `noisy` and excludes it.
3. Randomized context phase (interaction effects): `K` random contexts = random assignment to all features (valid per current constraints or not -- invalid contexts are skipped when known invalid); apply, read E, record `(context, e props)`. Default `K=max(50, 10*|F|)`; time cap 10 min; cost = K * (apply + |E| reads), UIA reads batched via cache request.
4. Probe for `requires/forbid`: for each pair (f,g) with suspicious OFAT behaviour only (f set had no effect with g at default), try f with g active vs inactive, 10 trials each.
Never touch `dangerous` features, never invoke actions; everything restored; abort on target failure (a crash is saved as a normal ErrorEvent).

### 3.3 Statistics and thresholds
For each (e, f) build 2x2 table `{f active/inactive} x {e enabled/disabled}` over contexts where other features are randomized. Test: Fisher's exact (one-table), two-sided; multiple comparisons across all (e,f) pairs controlled with Benjamini-Hochberg FDR `q=0.01`. Perfect separation arithmetic: with `a` observations per side, one-sided p = 1/C(2a,a): a=5 -> 0.0040, a=6 -> 0.0011, a=8 -> 7.8e-5, a=10 -> 5.4e-6; two-sided doubles. With m=360 tests (12 features x 30 elements) BH needs the best p <= q/m = 2.8e-5, which only a>=10 meets (a=8 gives 1.6e-4 two-sided and passes only if >=6 relations are significant together) -- hence the default of 10. 
**Minimum observation counts (defaults)**: >= 10 contexts per side (f active and f inactive), >= 3 of each side drawn from distinct "other-feature" backgrounds; relation reported only if `q<=0.01` AND `effect = P(e enabled|f on) - P(e enabled|f off)` has `|effect|>=0.95` (near-deterministic UI logic; partial effects are shown as "weak association" and never proposed). Conjunctive predicates (e enabled iff f1 AND f2): after univariate pass, test pairs among features with `0.3<|effect|<0.95` using 2x2x2 tables, needs >= 6 contexts per cell (24+), else "insufficient data". Mutex/requires use deterministic evidence: >= 10/10 consistent trials, 0 contradictions (a single contradiction after settle retest => reject, UI is not rule-governed).
**Confidence label** attached on approval: `inferred`; raw evidence stored in `Provenance.note="ev:v1;infer=enable;n=16;eff=1.00;q=2e-4;..."` (U2 encoding). Never `verified` until a later U2 preflight/run reproduces it.

### 3.3b False-positive control and the asymmetry
- FP (spurious constraint) removes valid states => silent coverage loss; FN (missed constraint) => invalid states executed => harness errors detectable at runtime. Therefore default **precision over recall**; thresholds above; every proposal shows *impact*: `valid_states before -> after` via `scheduler.estimate` (e.g. "removes 37% of 1,024 states").
- Falsification probes: before presenting a `mutex/requires/forbid`, run 3 extra attempts designed to violate it from different backgrounds (counter-example search); any violation rejects.
- Timing confound: delayed enable (spinner) -> re-read at 3 delays (settle, 2x, 4x); store `latency_ms` and propose `settle_ms` if larger than configured.
- Dynamic UI (controls recreated => selector churn): resolve each e per read by selector (B1); unresolved counts as `absent`, which is only treated as `visible=false` evidence when the container/window is confirmed present.
- Data-dependent enabledness (e enabled iff a text field non-empty): shows as weak association with a control, not a feature -> reported "depends on value of <control>" (not proposed; out of scope v1).
- Rejected proposals fingerprinted `sha1(kind|subject|object)` in the project's `inference_rejected` list (delta: stored in `Provenance.note` of the feature or sidecar `infer.json`) and not re-proposed unless evidence strength doubles.

## 4. Data / API contracts
- `infer.infer_dependencies(project, adapter, *, config=InferConfig(), progress=None)->list[Proposal]`.
- `InferConfig(min_per_side=10, q=0.01, min_effect=0.95, contexts=None, max_seconds=600, watch_limit=200)`.
- `Proposal(kind, subject, object, constraint: ConstraintDef|None, scope_add: tuple[str,...]|None, control_id: str|None, n:int, effect:float, p:float, q:float, impact:dict, evidence:dict, status:"pending"|"approved"|"rejected")` dataclass in infer.py (delta: not in types.py).
- `infer.apply(project, proposals)->Project` applies only `approved` ones; sets provenance `Provenance("inferred","inferred", note)`; result goes through `project.validate`.
- `Observation(context:dict[str,str], element:str, exists:bool, visible:bool, enabled:bool, t_ms:float)` for tests/offline analysis: `infer.analyze(observations, features)->list[Proposal]` is pure (no adapter) so it is unit-testable on Linux.

## 5. UX hooks
Project -> "Discover dependencies" wizard: shows plan (#applies, ETA), progress, then a review table: relation in words ("'Gamma' slider is enabled only when 'HDR' is on"), evidence (16 trials, 16/16), q-value, impact on state count, three buttons (Approve, Reject, Test again). Approved items appear with `inferred` badge in the project; can be undone. Banner: "Proposals are hypotheses; approving a wrong one hides states from testing".

## 6. Acceptance tests (MockAdapter with planted rules; seeds 0..19)
1. Planted: 12 features, 30 controls, 5 planted `enable` rules and 1 `mutex`, 1 `requires`; default config => recall >= 0.9 of planted, **zero** false positives in all 20 seeds.
2. Null model: 30 controls with random-independent enabledness (p=0.5 per control) => 0 proposals in 100 seeds (FDR control).
3. 5% read noise (random flip of IsEnabled reads): proposals either survive via retest or are dropped; FP count 0 in 50 seeds; recall >= 0.7.
4. Delayed enable (300 ms) with `settle_ms=25` => marked `noisy` until delays tested; after 4x settle correct relation found; suggestion `settle_ms>=...`.
5. Min counts: with only 5 contexts per side no proposal is emitted ("insufficient data"), with 10 emitted.
6. Conjunction: `c enabled iff f1 and f2` => pair relation found only with >=24 contexts; univariate FP none.
7. `dangerous` feature never toggled (Mock call log).
8. Baseline restored after phase, after exception, and after cancel.
9. Approval path: unapproved proposals never change `Project`; approved `mutex` reduces `valid_states` exactly as computed in `impact`.
10. Rejected proposal not re-proposed on re-run; re-proposed when evidence doubled.
11. Counter-example probe: planted *violable* rule (holds 9/10) is rejected.
12. Pure `analyze()` on recorded observation JSON reproduces identical proposals (determinism).

## 7. Metrics
Proposals per project; approval rate; post-approval harness-error rate change; % states excluded by approved relations; FDR audit (rejected after later contradiction); cost (applies, minutes).

## 8. Risks
Applications with non-deterministic or stateful enabling (license dialogs, async loading) => noisy; many controls hidden behind tabs need navigation actions not modelled (observations see them as absent => spurious `visible` relation with the tab feature: mitigate by treating tab/page features as containers and reporting "belongs to page"); Fisher tests assume independent contexts; read cost on huge trees; UIA `IsEnabled` is provider-reported and can lie for custom-drawn controls [K]; users approving without reading.

## 9. Design deltas
1. `ConstraintDef` cannot express "disabled unless" for *controls* -> mapped to `ControlDef.scope` (conjunctive only); disjunctive predicates unsupported.
2. `Proposal`, `InferConfig`, `Observation` live in `infer.py`; rejected-list storage needs a project/sidecar field.
3. Needs adapter read of `IsEnabled/IsOffscreen/exists` for arbitrary selectors: `TargetAdapter` has only `inspect()` (whole tree, heavy) -> propose optional `probe(selectors)->dict[str, ElementProps]` using batched cache requests; fallback = filtered `inspect()`.
4. Minimum counts (>=10 per side, q=0.01, |effect|>=0.95) are initial defaults to be calibrated on real apps; none of them comes from published GUI-specific guidance.

## Sources
- https://www.cs.umd.edu/~atif/pubs/MemonWCRE2003-abstract.html ; https://arxiv.org/abs/1702.06968 ; https://arxiv.org/abs/1205.4928 ; https://www.cs.umd.edu/~atif/papers/YuanMemonIST2010.pdf
- https://csrc.nist.gov/Projects/Automated-Combinatorial-Testing-for-Software/Downloadable-Tools ; https://github.com/microsoft/pict/blob/main/doc/pict.md
- Fisher exact test, Benjamini-Hochberg: standard statistics [K].
