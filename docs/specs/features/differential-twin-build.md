# U5 Differential Twin Build: same vectors on two builds, control-map migration, oracle classes, noise-aware tolerance
Status: proposed. Owner: `diffrun.py` (DIAG), shares the similarity scorer with B1 (`selectors.py`). Types: `Project`, `Selector`, `Case`, `AdapterSpec`, `TargetAdapter`, `Outcome`, `ErrorEvent`, `RunConfig`, `Health`.

## 1. Problem & why it matters
For closed-source Windows apps the only oracle available without source is "the previous build". Release-to-release regressions (new crash, a control that stopped sticking, a calculation that changed, a 3x slowdown) are found by running the same inputs on both builds. Two things make this hard in GUIs: the control map moves between builds (IDs, titles, layout change) and GUIs are noisy (timers, clocks, async loading, latency jitter). A naive diff drowns in false positives.

## 2. Prior art & what they do poorly
| Source | What | Gap |
|---|---|---|
| Widget matching across versions (Molnar, arXiv 1702.06968) [V: heuristic process for matching widgets across versions of an app] | reuse GUI tests on new version | desktop test reuse, not fuzz-vector lockstep; no oracle/noise model |
| GUI test repair via embeddings, 65% match-rate threshold in a thesis [S: ICST'23 listing, polito thesis]; Android->OpenHarmony migration study (arXiv 2607.11245) [V abstract]: text/position/image signals needed, dynamic widgets slow matching | matching quality evidence | mobile; no differential oracle |
| Similo / VON Similo [V: arXiv 2208.00677, 2301.03863]: weighted multi-attribute similarity; Similo failed to locate 91/801 (11%) vs 214 (27%) baseline across 48 sites; VON Similo 94.7% vs 83.8% accuracy | evidence that weighted similarity beats single-attribute locators | web only |
| RippleGUItester (arXiv 2603.03121) [V abstract]: change-aware exploratory testing, screenshot-based LLM classification of differences | visual oracle | not control-level; model-dependent |
| Differential testing in general (McKeeman 1998) [K] | cross-implementation oracle | not GUI-specific |
No Windows-UIA two-build lockstep fuzzer was found in the sources read (search found none; absence of evidence). Honest claim: "we found none". What we add: (a) control-map migration with `cannot-compare` as first-class outcome, (b) an **A/A noise calibration** that auto-demotes noisy oracles, (c) replay-confirmed divergences (U3).

## 3. Design
### 3.1 Roles and execution
Build A (baseline, project was taught on it) and build B (candidate). Each runs in its own **worker process + target process** (two `AdapterSpec`s). Same machine, separate working dirs; warn if both builds share settings stores (`%APPDATA%\<vendor>`, registry): cross-talk is a major confounder -> default recommendation: separate Windows user profile or VM snapshot per build; preflight (U2) runs on both and compares baseline reads.
Lockstep per case `c`: order alternates ABBA across cases (A,B / B,A) to cancel drift/thermal/cache bias; run sequentially, not simultaneously (CPU contention would skew timing; default `parallel=False`). Both reset between cases (`reset_policy` from project). Case vectors come from `scheduler.make_generator(project)`; B receives the mapped case (3.2); unmappable parts dropped and recorded.

### 3.2 Control-map migration (A -> B)
Input: A-side `ElementInfo` snapshot (from `inspect()` at teach time, stored in project cache) or live A; B live `inspect()`. Output `ControlMap: {id -> MapEntry(b_selector, score, status, reasons)}`.
1. Hard filter: `control_type` equal (or in equivalence classes {Edit,Document}, {Button,SplitButton}, {CheckBox,Button+Toggle}, {ComboBox,List} configurable) and same process.
2. Exact stage: unique `auto_id` (+ type) match => score 1.0 `mapped`.
3. Fuzzy stage for the rest, score = weighted sum, attributes normalized to [0,1]:
| attr | weight | similarity |
|---|---|---|
| auto_id | 0.25 | 1 if equal; else normalized Jaro-Winkler on token-split (camel/snake) forms; 0 if either empty (weight redistributed) |
| title (Name) | 0.25 | casefold, strip `&`, `...`, digits; token Jaccard + Jaro-Winkler max |
| path | 0.15 | longest common suffix of ancestor-title/id list / max len |
| class_name | 0.05 | equal=1 (framework ids with hashed suffix are stripped, B1) |
| relative position | 0.15 | normalized bounds inside parent window (x,y,w,h as fractions), 1 - L1 distance/2 |
| neighbour label | 0.10 | similarity of preceding sibling label (label-for-edit pattern) |
| patterns/value kind | 0.05 | Jaccard of supported patterns |
Weights are **initial** (borrowed in spirit from Similo's weighted-attribute idea [V]); calibrate on fixtures (see 6.2) before trusting.
4. Assignment: Hungarian (or greedy mutual-best) one-to-one so two A controls never map to one B element.
5. Decision: `mapped` if score >= 0.80 and margin to runner-up >= 0.10; `review` if 0.60 <= score < 0.80 or margin small (excluded from comparison until user confirms); `unmatched` < 0.60 -> **cannot compare** (A-only/B-only lists shown as "removed/added control"). Feature states: map per-state selectors for radio/tab features identically.
6. Domain check after mapping: probe B read domain (RangeValue min/max, combo options); mismatch => map but intersect domains for shared vectors (`fuzz_domain_B = fuzz_domain ∩ observed`), values outside are skipped and counted `skipped_domain`.
Stored as sidecar `twin-map.json` (versioned, hash of both builds) -- never mutates the project.

### 3.3 Oracle classes (each independently switchable, each carries confidence)
| class | compares | verdicts |
|---|---|---|
| O1 liveness | health outcome per case (ok/crash/hang/dialog) | `regression` (A ok, B fails), `fixed` (A fails, B ok), `both-fail-same` / `both-fail-diff` (signatures via fingerprint) |
| O2 write/read-back | `get_control` after `set_control` (normalized) | `readback-diff` (e.g. B clamps differently) |
| O3 UI-state parity | enabled/visible/exists of mapped controls after the case | `ui-state-diff` |
| O4 observed outputs | user-designated read-only elements (labels, status bar, result fields) text/value | `output-diff` (strongest semantic oracle; opt-in per element) |
| O5 dialogs | normalized title+text of dialogs raised | `dialog-diff` |
| O6 timing | per-operation and per-case latency | `perf-suspect` only (never "defect") |
Screenshots are attached as evidence only, never an oracle (strategy doc).

### 3.4 Tolerance and normalization
Integers/bools/enums exact. Floats: equal if `|a-b| <= max(abs_tol, rel_tol*max(|a|,|b|))` with `rel_tol=1e-6`, `abs_tol=1e-9` defaults (per-element override). Text: Unicode NFKC, casefold optional, whitespace collapse, strip thousands separators/locale decimals when both parse as numbers, ISO dates/times and `\d{1,2}:\d{2}` clocks masked, version strings of the product masked. Lists compared as ordered unless element marked `unordered`.

### 3.5 Noise handling (the credibility mechanism)
1. **A/A calibration**: before the A/B campaign, run the *same* cases (default 200 or 5 min) on A twice (two fresh launches). Any oracle/element that differs A1 vs A2 in > 0 cases is `nondeterministic`: its divergences are suppressed (auto-masked), listed in the report, and offered as "mask / designate as noise". Gives an empirical per-oracle false-positive rate `fp = d/200` reported next to every verdict class.
2. **Settle and re-read**: output compared after `wait_quiescent` + re-read after `2*settle_ms`; take the later value; disagreement => `unstable-read` (not diff).
3. **Confirm divergences**: every candidate divergence is replayed `k=3` times on *both* builds from fresh launches (U3 machinery); reported only if reproduced >= 2/3 on the side that differs and not on the other; else `noise?` bucket (counted, hidden by default).
4. **Timing (O6)**: collect >= 30 paired per-case latency samples per operation class; use the paired Wilcoxon signed-rank test; flag only if median(B)/median(A) >= 1.5 AND absolute median diff >= 100 ms AND p < 0.01 after A/A-calibrated variance check (A1 vs A2 median ratio must be within 1.2 else perf oracle disabled as "too noisy"). First `N=5` cases after launch discarded (cold start/JIT/cache). Report as `perf-suspect` with effect size and sample count. (Statistical choices are standard practice [K], not from GUI literature.)
5. Environment parity checks: same OS, DPI, display scale, locale, theme, monitor; mismatch => refuse or warn.

### 3.6 Verdict table
`regression` (O1 B-only failure, reproduced), `fixed`, `behaviour-change` (O2/O3/O4/O5 reproducible diff on mapped control; severity needs human), `perf-suspect`, `cannot-compare` (unmapped/unreadable), `noise` (suppressed). Each divergence is an `ErrorEvent`-compatible record: `category` = `regression|assertion|resource|dialog` (O1/O2-O5/O6/O5), `message` "A: 42, B: 41", `env["twin"]` with build hashes and mapping scores.

## 4. Data / API contracts
- `diffrun.differential(project, specA: AdapterSpec, specB: AdapterSpec, *, options=DiffOptions(), progress=None)->DiffReport` (name fixed in architecture).
- `DiffOptions(cases=None, stop=StopConditions(), calibrate_cases=200, k_confirm=3, oracles=("O1","O2","O3","O4","O5","O6"), rel_tol=1e-6, abs_tol=1e-9, order="ABBA", masks=())`.
- `DiffReport(control_map: dict[str,MapEntry], divergences: list[Divergence], noise: dict, stats: dict, env: dict)`; `Divergence(id, verdict, oracle, control_id, a_value, b_value, case: Case, reproduced: tuple[int,int], p_noise: float)` -- dataclasses in `diffrun.py` (not types.py; delta).
- Evidence: divergences persisted as ErrorEvents via `EvidenceStore` (errors-only principle: agreeing cases not stored).
- `diffrun.migrate_map(project, infoA, infoB)->dict[str,MapEntry]` pure, Linux-testable.

## 5. UX hooks
"Compare builds" wizard: pick A and B exes; map review screen (table A control -> B control, score bar, thumbnail highlight on both windows, accept/override); calibration progress ("A vs A: 3 noisy elements masked"); results grouped by verdict with reliability chips (U3); side-by-side values; "export regression report".

## 6. Acceptance tests (two MockAdapters; B = A with planted changes)
1. Rename 30% of AutomationIds and titles slightly ("Width" -> "Width (px)") => >= 95% mapped correctly, 0 wrong mappings with status `mapped`; ambiguous duplicates -> `review`.
2. Fuzzy calibration fixture of 200 labelled controls: precision of `mapped` >= 0.98; recall >= 0.85 (weights tuned on separate fold).
3. Removed control => `unmatched`, vectors skip it, report lists it; added control not fuzzed.
4. Planted regression: B crashes iff `A=on & W==0` => O1 `regression` reproduced 3/3, reduced via U3 to that pair.
5. Planted B clamping (max 100 vs 200) => `readback-diff` on the exact control; domain intersection prevents false alarms for values <= 100.
6. Nondeterministic label (random clock text) => A/A calibration masks it; zero divergences emitted for it; appears in `noise`.
7. Timing: B delays 2x on one op class (+150 ms) with 20% jitter => `perf-suspect` flagged with >= 30 samples; with jitter exceeding 1.2 A/A ratio => perf oracle disabled message.
8. Float tolerance: 0.1+0.2 vs 0.3 equal; 1.0 vs 1.0001 differ with rel_tol 1e-6.
9. ABBA ordering: Mock logs show alternating order.
10. Confirmation: transient one-time divergence (1 of 3 replays) lands in `noise?`.
11. Same build on both sides (A vs A): zero divergences over 1,000 cases (FP sanity).
12. Agreeing cases write nothing to disk.

## 7. Metrics
Map coverage (% mapped/review/unmatched); A/A noise rate per oracle; divergences per 1k cases by verdict; confirmation rate; precision from user triage; time per case pair; % perf oracle enabled.

## 8. Risks
Shared user data between builds; localized/redesigned UI breaks mapping wholesale (report "cannot compare" rather than guess); semantic differences that are intended (needs triage, label `behaviour-change` not `bug`); layout changes with unchanged AutomationIds mislead position weights; installing two versions side by side may be impossible (shared COM registration) => VM snapshots; timing noise from AV/updates; output oracles require user designation.

## 9. Design deltas
1. New dataclasses (`DiffReport`, `Divergence`, `ControlMap`, `DiffOptions`) live in `diffrun.py`; `ErrorEvent.category` vocabulary (`crash|hang|dialog|assertion|resource|harness`) lacks `regression|perf-suspect`; map them to `assertion`/`resource` with `env["twin"]["verdict"]`.
2. Project must retain the A-side `ElementInfo` snapshot (inspect cache) to migrate later; propose `project` sidecar `inspect-cache.json`.
3. `Case` application on B needs per-build `AdapterSpec`; `RunController` is single-target -> `differential()` drives two controllers/workers itself.
4. Weights/thresholds (0.80/0.10/0.60; wilcoxon 1.5x/100 ms) are provisional defaults, not literature values.
5. Shares similarity code with B1 (`selectors.similarity`) -- one implementation.

## Sources
- https://arxiv.org/abs/1702.06968 ; https://arxiv.org/pdf/2607.11245 ; https://arxiv.org/abs/2208.00677 ; https://arxiv.org/abs/2301.03863 ; https://arxiv.org/pdf/2603.03121
- https://conf.researchr.org/details/icst-2023/icst-2023-previous-editions/6/Repairing-Fragile-GUI-Test-Cases-Using-Word-and-Layout-Embedding ; https://webthesis.biblio.polito.it/14470
