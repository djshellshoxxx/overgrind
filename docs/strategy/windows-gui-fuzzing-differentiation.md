# Overgrind: Universal Windows GUI scope and differentiation strategy
Status: Product strategy / candidate roadmap. This document does not claim features are implemented.
Applies to: any interactive Windows application for which a suitable observation and control mechanism can be constructed. **No assumption of source code access, JUCE, audio support, or developer instrumentation.**

## Core product identity

**Overgrind = autonomous, user-teachable, black-box Windows GUI fuzzing with systematic feature-state coverage and actionable bug reproduction.**

The core user journey:
1. Choose an EXE or running process (or a window), with user-selected test profile.
2. Inspect visible controls, use a crosshair to mark features, default states, and controls. Discovered ranges are suggestions with provenance and confidence; the user confirms unknown types and bounds.
3. Select feature combinations, control subsets and allowed parameter ranges, exclusions, dependencies, default state resets and duration. **Run until manually stopped** is default.
4. Execute constraint-valid feature states and sampled controls continuously, with adaptive exploration and optional targeted scenario sequences. Bounded scheduler must not grow linearly with run duration.
5. Record **only errors by default** (with a bounded RAM history and small resume checkpoints), separating failures of the app under test from failures of Overgrind's automation.
6. Reproduce/verify and reduce a failure to a compact recipe with seed, action history, control values, timing, target version and environment. Export to developer issue/report.

## Platform scope and truthful compatibility

- Primary: Windows 10/11 x64 interactive GUI applications. Test app architecture (x86/x64), framework and installed runtime must not dictate the fuzzing core.
- Technologies to *target* with adapters/feature tests: native Win32/MFC, WinForms, WPF, WinUI/UWP where accessible, Qt, Electron/Chromium embedded views, Java Swing/JavaFX, Delphi, custom canvas/GDI and other third-party frameworks. This is a compatibility *goal*, not a claim these are already supported.
- Access tiers: (1) UI Automation / MSAA native semantic controls; (2) Win32/message APIs where appropriate; (3) vision/crosshair/keyboard/mouse fallback; (4) optional developer test hooks for higher confidence/observability.
- UIA does not expose every custom control or true semantic range. Visual coordinate automation is brittle. A custom or privilege-isolated application may not be automatable without intervention.
- Testing an already compiled app without source code must remain the **primary default**. Optional developers' adapters add depth (internal assertions, crash dumps, structured test APIs), never become a mandatory prerequisite.
- Support multiple windows, dialogs, tab pages, modal stacks, dynamic controls, resizable layouts and hybrid native/web surfaces. Track exact process/window ownership and changing target identity. Never send events to another process after accidental focus loss.
- State security boundaries explicitly: UAC secure desktop, lock screen, other user sessions, protected processes, elevated apps, full-screen exclusive graphics and anti-automation interfaces are not promised to work unattended.
- Present compatibility as per-control status: auto-inspectable, auto-controllable, mapped manually, requires bridge, unsupported. Never say "100% GUI controls discovered."

## Existing capabilities in other projects: avoid false novelty claims

- SmartBear TestComplete: deep desktop object inspection, record/replay, data-driven testing, reporting and integrations. Overgrind cannot claim "no-code desktop automation" alone as a novelty.
- Ranorex Studio: broad Windows toolkit coverage, object repository, recording and reporting. Broad app compatibility alone is not novel.
- Squish: native/custom GUI support, scriptable stress/monkey testing, verification, CI. "Monkey testing Windows GUI apps" alone is not novel.
- FuzzWinForms: randomized mouse/keyboard actions, event logging, recording/replay and a replay minimizer. Replay and reduction alone are not novel.
- Microsoft PICT / NIST ACTS: systematic combinatorial test generation with constraints. Pairwise or t-wise alone is not novel.
- FlaUI / pywinauto: toolkits to manipulate Windows UI Automation. They are useful adapter foundations, not directly equivalent complete products.
- GUITAR: GUI model exploration and event-flow testing. State graph coverage alone is not novel.

**Potentially differentiating package**: fast end-user teaching + transparent constraint-driven combination engine + truly unattended multi-day fuzz campaigns + continuously improved coverage + low-noise error-only evidence + reproducible/minimized regressions from closed-source Windows apps.

## Differentiators, ranked by value and feasibility

| Priority | Candidate | User value | Engineering | Distinction |
| --- | --- | --- | --- | --- |
| P0 | Teach-by-pointing feature and control editor | Very high | High | Turns a real arbitrary GUI into a reusable typed fuzz model with defaults and constraints, rather than a recorded script |
| P0 | Reachable-state enumerator + continuously sampled control domains | Very high | High | Explicit all-reachable finite feature states first, then continuous parameter exploration with truthful coverage |
| P0 | Indefinite fault-tolerant watchdog and error-only collection | Very high | High | Practical "leave it running" bug discovery with bounded memory, app/harness separation and crash evidence |
| P1 | One-click reproducer and delta-debugging reducer | Very high | Very high | Produce the smallest reliable sequence AND parameter-domain subset, not merely a screen recording |
| P1 | Differential fuzzing of older/newer versions | Very high | Very high | Same saved test model and input vectors run on two builds; classify regressions vs improvements |
| P1 | Environment/temporal perturbation engine | High | High | Stress resize, focus, minimization, timing, DPI/theme changes and fast enable/disable during long-running operations |
| P1 | Coverage honesty/heatmap and adaptive scheduling | High | High | Quantify what was actually exercised: feature states, t-way buckets, transitions, dwell times, timeouts |
| P1 | Resource-trend/leak oracles | High | Medium | Per-feature-state baseline for memory, handle, GDI object, thread, CPU/IO growth; warnings require corroboration |
| P2 | Control relationship inference with uncertainty | High | Very high | Observe when controls appear/disappear or become enabled and propose dependencies for approval |
| P2 | Cross-application workflow/staged scenarios | Medium-high | Very high | Model interactions across two apps/OS dialogs; do not compromise core app-focused MVP |

### Competitive guardrails

- Never market UIA-based inspection or object spies as a new invention.
- Never claim exhaustive coverage over real-valued or unbounded domains.
- Never claim any GUI is automatable without demonstrating required driver capabilities.
- Never call every assertion mismatch an application defect. Separate **target failures**, **oracle uncertainties**, and **harness errors**, with configurable confirmation runs.
- Randomness is not sufficient. Coverage scheduling, replay, minimization and a reasonable test oracle create value.
- "Error-only logging" is a UX/storage default, not exclusive intellectual property: other tools may filter logs or capture failure artifacts.
- Every differentiator needs cross-framework reproducible fixtures, not one successful run on a developer's app.

## Distinctive features to design carefully

### A. Teach-by-pointing, with confidence and provenance
UI shows element under cursor; captured identity and path; control type; current/default value when readable; proposed min/max/step or enum. Confidence: verified/readable, inferred, manually entered, unknown. User can mark global vs feature-scoped controls, dangerous actions to exclude, paired constraints, baseline restoring procedure. A dry-run validates selectors and resets.

### B. Stateful combination engine
Model each feature as off/on, multi-state, exclusive mode or event/action. Distinguish a feature configuration from an action sequence. Calculate valid configurations after constraints; perform a shuffled coverage epoch when manageable; use t-wise/covering arrays with explicit limits otherwise. Control sampling draws from restricted domains with boundary, default, random and mutation strategies. A saved project documents exactly what coverage guarantees apply.

### C. Always-on resilience
Separate GUI/controller, automation worker and target process; watchdog covers worker deadlocks and target hangs, crash detection and controlled restart. Keep minimal persistent checkpoints and counters; successes are not logged as events. Errors include a bounded sequence history, exact inputs, failure signatures, snapshots and optional dump; deduplicate repeated errors. Pauses/stops are responsive.

### D. Reproducibility and minimization
On failure: attempt replay in freshly reset environment, estimate reproducibility, simplify action history using chunk deletion and simplify control values toward defaults/boundaries; verify failure signature still matches. Export machine-readable testcase and human-readable explanation. Distinguish flaky, reliable and non-reproducible failures. No minimizer should accidentally overwrite the original evidence.

### E. Differential testing
Two app builds/processes, matched control maps with schema migration; same feature-state/configuration vectors, controls, and timing constraints. Compare outcomes (crash/success, numeric/status values, UI tree properties, elapsed time within tolerance). Screenshots are optional visual evidence, not the sole oracle. Report "cannot compare" where controls no longer map reliably.

### F. Temporal/environment fuzzing
Timing shifts, rapid toggles, app focus changes, window resize, maximize/minimize, changing monitor geometry, theme/DPI/locale (when supported in isolated environment), menus/dialogs, sleep/recovery events (explicit opt-in). These are separate strategies: desktop settings should not be modified without a user-defined test profile.

### G. Observability without source code
Windows process liveness, window responsiveness, memory/CPU/handles/GDI objects, WER/process dumps and visible error dialogs. Internal app assertions, log hooks and semantic output validators are optional developer enhancements. Use per-app guardrails to avoid treating slow expected operations as hangs.

## Concrete staged roadmap

**Stage 1, runnable standalone alpha:** package a self-contained Windows GUI with target picker, UIA inspection and manual feature/control mapping, defaults and allowed ranges, all reachable finite feature-state combinations with adaptive random values, unrestricted-duration default and time limit, meaningful start/pause/stop, errors-only evidence, target/harness failure discrimination, crash/watchdog, save/load profiles, and synthetic Win32/WPF test fixtures. Make performance of long runs bounded-memory. Release actual Windows executable and run interactive smoke tests.

**Stage 2, high-value differentiators:** robust one-click deterministic replay, actual bug reduction, auto-recovery and crash dump capture, t-wise/constraint scheduler, per-control provenance, coverage view, issue exports, resource-trend monitors and temporal stress.

**Stage 3, flagship features:** dual-build differential fuzzing, inferred feature dependencies, optional advanced visual recognition for inaccessible GUIs, app adapters, cross-process scenarios, persistent fuzz knowledge model.

## Suggested implementation boundaries
- Core: application/feature/parameter schemas, state constraints, sampling, scheduling, campaign state, retention/coverage.
- Automation: ITargetAdapter with enumerate/read/set/invoke/wait/reset/capabilities; adapters for UIA, Win32 and optional visual or developer-specific mechanisms.
- Observers: target process watcher; screen/UIA oracle; performance sampler; app-specific oracles; error classifier.
- Evidence: errors-only event sink; dedup/fingerprint; ring buffer; repro compiler; bundle export.
- Platform: Windows x64 self-contained GUI and worker processes; versioned project manifests.
- Test coverage: unit/property-based tests for generator and constraints; fixture apps spanning Win32, WinForms, WPF, Qt, Electron and at least one inaccessible custom-painted GUI for fallback validation.

## References
- https://smartbear.com/product/testcomplete/features/
- https://www.ranorex.com/windows-desktop-test-automation/
- https://www.qt.io/quality-assurance/squish/platform-automated-windows-gui-testing
- https://doc.qt.io/squish/how-to-use-the-qt-api.html
- https://github.com/SJMakin/FuzzWinForms
- https://github.com/microsoft/pict
- https://csrc.nist.gov/Projects/Automated-Combinatorial-Testing-for-Software/Downloadable-Tools
- https://github.com/FlaUI/FlaUI
