# Overgrind v0.2: Standalone continuous application GUI fuzzing
Status: **proposed specification**. Not implemented by the current v0.1 CLI.
Target: Windows 10/11 x64, packaged GUI executable, optionally installed or portable.

## Product intent and essential correction

A tester selects any Windows application, identifies its main features (by OS discovery, by clicking elements with an inspector, or manually), marks each feature's **default state** and capabilities, chooses individual GUI controls and allowed value subsets, then starts a randomized stress test. The **default stopping condition is manual stop (unbounded duration)**. Other stop conditions: fixed wall time, test-case budget, first error, number of unique errors, and optional coverage target. **Only failures/errors are persisted** in the default log; successful cases are counted live in the dashboard.

Current implementation (v0.1) provides a Python CLI testing F² ordered feature pairs with S samples and writes one JSONL record per case, with a maximum of 250,000 cases per invocation. v0.2 must remove that fixed-size campaign scheduling model for indefinite testing, expose a standalone Windows GUI, replace default success logging with error-only evidence and implement feature *state combination* coverage in addition to event sequences. Do not claim v0.2 is implemented until delivered and Windows-tested.

## Mathematical model and practical coverage guarantee

Definitions:
- F = set of user-selected and reachable main features, with their possible states.
- C = user-selected GUI parameters, each associated with zero or more features.
- D(c) = allowed domain for control c, after tester-defined filtering; types may include bool, enum, integer, float, bounded string, multi-select, vectors/XY, and time varying/action-based controls.
- E = constraints (dependency, exclusivity, conditional visibility, invalid state combinations).
- X = **valid configurations** of feature states under E. For n independent two-state features, |X| = 2^n, INCLUDING all-off; optional exclude-all-off yields 2^n - 1. n² covers only *ordered pairs of feature actions*, not all feature states. Both strategies remain separately selectable.
- For finite domains with unconstrained independent parameters, exhaustive control configurations = product over c of |D(c)|, multiplied by |X|. This grows astronomically. Real-valued domains are not enumerable in practical time.
- Random fuzzing runs indefinitely until stopped. After planned finite feature-state coverage has been completed, continue visiting configurations while sampling new control vectors, interaction orders, timings, and stateful action sequences. Do not promise every possible float/text/path is exhaustively visited.
- Fair scheduler: when feature-state combinations are finite and enumerable within configured capacity, ensure each valid combination is visited before starting a new shuffled sweep (where practical); track coverage progress. For enormous combinatorial sets use a **bounded-memory, coverage-driven scheduler** with declared coverage guarantees (e.g., pairwise/t-wise) rather than claiming exhaustive coverage.

Strategies:
1. **Every feature state** (default): cover every reachable feature configuration, subject to constraints, with adaptive control sampling; prioritize unvisited feature configurations; randomize order.
2. **Every ordered feature pair** (legacy): F² including A→A, A→B, B→A (sequences, not simultaneous on/off).
3. **t-wise parameter coverage**: pairwise (t=2), 3-wise, etc., using finite value buckets and constraints; optionally integrate Microsoft's PICT or a well-tested covering-array library.
4. **Full Cartesian product** (explicit opt-in and finite enumerable domains only): enumerate every allowed value per control and every reachable feature state. Show an estimated count and completion horizon; do not start when impossible/unbounded without explicit acknowledgement.
5. **Stateful chaos**: random interactions, enable/disable transitions, action order, variable changes while processing, dwell times and reset intervals; measure sequence/action coverage separately.
6. **Boundary/mutation**: min/max, near boundaries, 0, default, middle, common discrete values, prior crash value perturbations. Invalid/out-of-range values **opt-in**, never implied by the valid-domain fuzz setting.
7. **Adaptive mode**: favor rare feature combinations, unvisited parameter buckets, and cases resembling reproducible failures, with occasional uniform exploration to avoid starvation.

## User-facing Windows GUI: workflow

1. **Target** panel: choose running process/window using PID/process picker/crosshair OR select an EXE to launch; require exact target selection and optionally narrow to owned windows. Show executable path, version, process architecture, PID, window title, application build hash, automation capabilities. Attach without source code when UIA supports it.
2. **Inspect**: attach and enumerate UIA elements via a read-only snapshot; navigate nested pages with controlled manual capture; show tree view, hover/crosshair target highlight, stable AutomationId/name/type/bounds, value, available UIA patterns, current/default/range as discoverable, access path (which tab/window). Do not silently accept guessed ranges. Ignore unrelated OS/process windows.
3. **Features**: tester may add a feature by picking an element or manually creating one. Give ID/name, UI location, feature kind (checkbox toggle, radio choice, tab view, action, mode, custom test bridge), default state (on/off/selection/value), available states, test inclusion, dependencies/conflicts, and optional activation/verify/reset actions. Explicitly distinguish (a) persistent enabled feature state from (b) invoked event/button, and (c) mutually exclusive selections.
4. **Controls**: tester may add a control by picking an element or manually creating it; associate with feature(s) or global; identify type, min/max/step/precision, allowed enum options, max string length/alphabet, original default, restricted fuzz subset, skip/required/read-only, allowed mutations, dependencies and baseline restore behavior. Show effective domain alongside original discovered domain. UIA inspection is read-only and incomplete for custom-drawn GUIs.
5. **Combination selection**: multi-select features and controls, include/exclude all-off, select pairwise/t-wise or full state enumeration, constrain combinations, choose whether defaults must be restored after each test or after an interval. Display number of valid finite feature configurations and bounds on estimated control combinations; preview selected test cases.
6. **Duration**: default **Until stopped**; optional N minutes/hours/days, N test cases, N unique errors, first error, or finite coverage completion. Pause/resume/stop must be responsive even if GUI interaction blocks (controller and worker processes separated). Automatic target restart after crash optional; maintain watchdog deadline per operation/case.
7. **Run**: live status displays only aggregate nonpersistent success stats (elapsed time, cases executed, cases/sec, unique errors, number of crashes, hangs, feature configuration coverage, estimated bucket coverage, current pair/configuration and process status). Keep an in-memory ring buffer of recent steps; avoid disk writes for successful test cases by default.
8. **Errors**: list sortable/filterable failures with time, unique fingerprint, category, occurrence count, and a review/replay action. Double-click opens the minimal evidence package with screenshot and available crash dump.
9. **Save project**: standalone application retains named project manifest with target identity, selectors, feature mappings, defaults, domain constraints, reset recipes, error rules and run configuration; export/import JSON for developer handoffs.

## Failure detection and errors-only capture

Separate GUI-driver errors from app-under-test failures:
- Target process died or exited unexpectedly (crash).
- Unhandled exception / Windows Error Reporting or crash-dump trigger.
- Application stopped responding (distinguish transient busy vs repeatable hang with threshold).
- GUI control unavailable or automation failed (harness error; do not misclassify as app defect).
- UIA state read-back mismatch or invariant/assertion violated (requires observation oracle).
- Unexpected modal dialog / runtime error dialog.
- Performance threshold violation (CPU, memory, handles, threads, sustained latency, launch hang). Unusual resource utilization is not automatically a defect unless threshold/oracle configured.
- For developer-instrumented builds: sanitizer failures, logged asserts, playback underruns, audio glitches, rendering errors, data mismatch and other app-specific diagnostic counters.
- Missing UI controls, undesired state persistence, or visual anomalies need explicit assertions/baselines; generic UIA cannot know intended output.

**No successful-case log records by default.** Maintain per-worker in-memory counters and a bounded last-N actions ring buffer. On failure, atomically persist an **error event** (JSON metadata and optional external evidence) containing:
- timestamp, run UUID, target exe path + hash + PID/build, OS/build, tester manifest hash;
- PRNG family/version/seed/counters or serialized state and exact applied feature state, control values and order, dwell timing, action sequence since last known reset/checkpoint;
- failure category, error text, observed expected vs actual when known, repro attempt, unique signature and count;
- screenshot (only if helpful), crash dump path (opt-in), relevant stderr/application log excerpts (opt-in), recent bounded actions and event sequence, accessibility tree excerpt (optional).
- Crash fingerprints deduplicated by reliable exception type/module/stack where available; do not collapse materially different crashes into one category.

If successes are not persisted, deterministic replay requires saving the full recent sequence or a checkpoint sufficient to reconstruct state, plus the exact failing inputs. Seed alone is insufficient for nondeterministic timing/concurrent GUI events. Storage retention / dump size limits; sensible redaction of passwords and fields tagged sensitive.

## Standalone Windows architecture

Preserve the proven Python core and current adapter protocol where possible; avoid a premature wholesale rewrite. Proposed packaging:
- Windows GUI: PySide6 (Qt widgets) or another proven native-capable toolkit. Bundle Python interpreter and dependencies using PyInstaller/Nuitka into an installer or self-contained folder/executable. No Python install should be required by end users.
- Core model: typed, versioned immutable project manifests; migrations, validation, original vs limited ranges, separate feature states vs events.
- Discovery plugins: Windows UIA (current pywinauto) + alternate Win32 API; optional image-based element targeting for GUIs lacking accessibility metadata (fragile, opt-in); adapter contract for custom drawn JUCE controls and developer-provided control registry.
- Windows controller process: GUI config, session management, dashboard, pause/stop/cancel, error event subscriptions, resource budgets, target PID/window identity. **Separate worker process** makes it possible to detect and terminate a stuck UIA call, then restart only the worker.
- Persistent campaign scheduler: bounded memory; supports unbounded runtime, seed checkpoints, valid configuration enumeration when small, covering arrays or lazy state-space sampling when large; no global 250,000-case limit for indefinite mode.
- Target adapter: discover, set state, get state, apply control, activate action, reset, wait for quiescence, health check, screenshot; capabilities discovered at runtime rather than assuming generic toggles are safe.
- Watchdog: process tree tracking; responsive stop; per-action/case deadlines; optional target restart or restore checkpoint; optional native ProcDump integration and WER crash reports.
- Diagnostic sinks: errors-only atomic event files and attachments; in-memory metrics; option to export report when run concludes, explicitly not silent per-success logging.
- Packaging & CI: Python unit tests on platforms already tested + dedicated Windows GUI smoke tests using a bundled synthetic target app (standard UIA fields, custom controls, intentional crashes/hangs); build and release a Windows x64 package.

Avoid requiring administrator privileges unless the target or requested diagnostic requires them. Interactive desktop sessions generally needed for visible GUI automation. Cross-process interactions may fail across elevation/integrity boundaries or when desktop is locked. Do not promise reliable mouse-driven testing in a locked headless Windows session; use an active interactive VM desktop or app-specific non-GUI harness for those cases.

## Acceptance requirements

**P0 behavior** (required before v0.2 standalone alpha):
- P0-01: UI starts and can attach to a chosen Windows process or EXE with no local Python installation required.
- P0-02: User can pick feature controls via inspection and manually specify feature identity, default state and behavior.
- P0-03: User can pick and configure control types, defaults and restricted domains; save/load named projects; invalid constraints are rejected before a run.
- P0-04: Unbounded duration is the default; explicit time-limited operation accurately terminates on configured wall time; pause/stop responsive on worker timeout.
- P0-05: User can select only specific features and specific controls, associate controls to prerequisites and block impossible states.
- P0-06: Test **all valid finite feature on/off states** (not merely F² pairs) when the set fits enumerator budget; case order randomized; no all-on-only assumption.
- P0-07: A bounded-memory strategy continuously draws control values; it handles never-ending runs without growing memory or stored-success events linearly.
- P0-08: Record only errors by default; include effective values/feature state/sequence and enough evidence to reproduce failing cases.
- P0-09: Distinguish app failures vs automation errors; detect process death, clearly thresholded hangs and explicit assertion failures.
- P0-10: Restore configured baseline/defaults to support independent cases; define optional stateful-continuous chaos mode.
- P0-11: Existing v0.1 CLI behavior and tests remain passing; versioned manifests remain migratable.

**P1** (next):
- P1-01: Auto-restart/recovery, snapshots and crash dump evidence, de-duplication and minimization.
- P1-02: Pairwise/3-wise value coverage using constraints and finite parameter bucketing (e.g. PICT), with coverage accounting.
- P1-03: Resource trend metrics, leak detection heuristics and configurable health oracles.
- P1-04: Repro one-click and reduced reproducer export; confirm failures after restart.
- P1-05: Custom-drawn JUCE/Qt input bridges and test-only diagnostics.
- P1-06: Configurable random event order, inter-event timing, audio playback changes and multi-minute stress holds.

**P2**: adaptive coverage heuristics, runtime-guided fuzz feedback, distributed workers, CI-agent mode, rich reports and app-specific plugins.

## Test plan / fixtures

- 3 independent toggles: enumerate exactly 8 valid on/off states (or 7 excluding all-off), no repeats until a new epoch under explicit exhaustive-feature-state mode.
- 10 independent toggles: cover all 1,024 states, with shuffled order and stable coverage counts.
- Exclusion A vs B: invalid both-enabled state must never be executed; count only valid states in denominator.
- Nonbinary feature (Off, Low, High): correct 3-state enumeration; mutually-exclusive modes never simultaneously active.
- Linked controls: control only changed if prerequisites hold; global controls remain eligible.
- Integer, decimal, booleans, enums, text, including zero-length and boundary values: respect restricted domain; default values recoverable.
- Unbounded run: simulated worker executes millions of cases with bounded memory, zero success records and clean cancellation.
- Timed run: timer uses monotonic clock; resumes after pause with clear policy for pause time inclusion.
- Crash/hang target fixture: distinguish worker failure and target failure; emit one structured error evidence package per incident; optional target restart and continued run.
- Repro: recorded failing-case action list and checkpoint reproduce deterministic fixture failure.
- Inspector: read-only; UIA snapshot does not modify target. Never accept a guessed type/range silently.
- Windows smoke tests: standard WinForms/WPF test app, a custom-drawn synthetic surface, scaled display, DPI changes, multiple monitors, elevation differences, window title changes, locked desktop behavior.
- Exit and unattended runs: when process/window target changes, stop or reattach only according to explicit user policy; never accidentally inject events into another process.

## Suggested implementation sequence and modular boundaries

1. Write manifest v2 schema and migrations, feature state configuration and constraints with unit tests.
2. Replace finite F²-only planner with strategy interface and bounded-memory indefinite scheduler, timer and stop/pause contracts. Keep legacy ordered-pair strategy.
3. Add minimal errors-only evidence collector, action ring buffer, category classifier, and deterministic replay test fixture.
4. Implement target process watchdog/case timeout using child worker process and synthetic crash/hang app.
5. Build Windows UI: target picker, inspector, feature/default state editor, control/range editor, strategy/duration configuration, dashboard and error viewer.
6. Package and smoke-test self-contained Windows executable in GitHub Actions and interactive Windows test VM.
7. Add higher-order combinatorial sampling, adaptive fuzzing, oracles, performance collection and custom-drawn controls.

Make interfaces stable: TargetAdapter, FeatureStateModel, ControlDomain, ConstraintEngine, CaseGenerator, RunController, ErrorOracle, EvidenceSink, ProjectStore. No input provider should depend directly on UI toolkit, and scheduler must not depend on Windows UI Automation. Avoid global mutable state and ensure cancellation/baseline reset are explicit contracts.

## Similar products and references

- Win32/UIA access: Microsoft UI Automation docs and FlaUI, pywinauto. These offer control/interaction primitives, not this particular full campaign scheduler.
- Configurable GUI test suites: SmartBear TestComplete; Ranorex Studio; Qt Squish.
- Windows GUI monkey fuzzer: SJMakin/FuzzWinForms (random inputs, capture/replay/minimization, event logs).
- State-driven GUI exploration: GUITAR GUI ripper and event-flow graph generation.
- Combination generation: microsoft/pict and NIST ACTS.
- Crash and hang evidence: Microsoft Sysinternals ProcDump.

This document records **required future behavior**. Current Overgrind does not have a standalone GUI, unlimited run duration or errors-only logging. Those must be delivered and tested separately.
