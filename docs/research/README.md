# Overgrind research index (2026-10-14)

Method note: web research via search-tool summaries; direct fetch of `learn.microsoft.com`, `pyinstaller.org`, `docs.python.org` was blocked in the research sandbox, so Microsoft/PyInstaller claims are tagged **[V]** (confirmed in page content as summarized by search), **[S]** (secondary), **[K]** (engineering knowledge, not re-verified). Every spec lists its own unverifiable points.

## Index
| Doc | Topic |
|---|---|
| [windows-failure-detection.md](windows-failure-detection.md) | crash / hang / dialog detection, UIA pitfalls, dumps, job objects, crash fingerprinting |
| [packaging-windows.md](packaging-windows.md) | PyInstaller vs Nuitka, onedir/onefile, multiprocessing freeze, CI desktop, signing, installers |
| [../specs/features/feature-state-atlas.md](../specs/features/feature-state-atlas.md) | U1 coverage ledger + guarantee grammar |
| [../specs/features/trust-ledger-preflight.md](../specs/features/trust-ledger-preflight.md) | U2 provenance, preflight, danger guard |
| [../specs/features/auto-reduce-reproducer.md](../specs/features/auto-reduce-reproducer.md) | U3 reliability stats, ddmin/HDD, shrinking, export |
| [../specs/features/dependency-inference.md](../specs/features/dependency-inference.md) | U4 enabled/visible/mutex inference with FDR control |
| [../specs/features/differential-twin-build.md](../specs/features/differential-twin-build.md) | U5 two-build lockstep, control-map migration, noise |
| [../specs/features/smart-inspector-selectors.md](../specs/features/smart-inspector-selectors.md) | B1 stability scoring, tiers, bounded self-healing |
| [../specs/features/streaming-twise-generation.md](../specs/features/streaming-twise-generation.md) | B2 streaming constrained t-way generator |

## Ten most important findings
1. **Use two independent hang signals.** `IsHungAppWindow` is OS-defined at 5 s but documented "not for general use"/subject to change; `SendMessageTimeout(WM_NULL, SMTO_ABORTIFHUNG)` is reported unreliable in edge cases. Declare hang only after both agree for `hang_threshold_ms`.
2. **Dump from a separate process.** Microsoft says call `MiniDumpWriteDump` out-of-process; WER LocalDumps needs admin/HKLM and is off by default, so crash dumps must be an explicit, reversible opt-in.
3. **Job objects with KILL_ON_JOB_CLOSE (nested OK since Win8)** are the right tree-kill; assign at `CREATE_SUSPENDED`; never kill an attached process without consent.
4. **UIA calls block on hung targets** (ConnectionTimeout default 2 s); run all UIA in the killable worker process (already in the architecture) and never on the owner's UI thread.
5. **AutomationId is only sibling-unique, optional, not guaranteed stable across builds; RuntimeId is reusable** -- never persist RuntimeId; score selector stability and refuse to guess when healing (accept only with high score *and* margin).
6. **Crash bucketing fails by spray (build timestamps), offset drift, and over-coarse frames** (WER/Socorro experience); fingerprint = category + exception code + module + module-relative offset, with normalized text, never fuzz values.
7. **Reliability needs statistics:** 2 replays prove almost nothing (all-fail => p>=0.22 at 95%); "reliable" needs >=3 (5 planned), "not reproduced" needs >=5 and is a bound (p<0.45), ruling out 5% needs 59 clean runs. Existing `confirm_runs=2` is too low.
8. **No existing tool found combines** live resumable state-coverage ledger + infeasible/unknown accounting + generated guarantee text; NIST CCM/ACTS/PICT cover measurement/generation in batch. Overlap must be stated; "we found none", not "none exists".
9. **t-way tuple bitmaps explode:** n=500,v=3,t=3 is ~67 MiB, t=4 ~24 GiB -> a hard memory budget with hash-sampled mode and CI-based, non-completeness wording is mandatory.
10. **Packaging: PyInstaller onedir + zip/Inno Setup first; fix the frozen multiprocessing traps** (freeze_support first, no Qt import in worker, hidden-import the `AdapterSpec` factory modules, comtypes gen cache). Signing: Azure Artifact Signing is $9.99/mo (eligibility unverified), EV no longer gives instant SmartScreen reputation, certs max 460 days from 2026-03-01; **GitHub-hosted `windows-latest` interactive-desktop availability is unverified** -- probe it in CI and keep SendInput tests on self-hosted VMs.

## Claims not verified (consolidated)
- Interactive desktop/session and screen size on GitHub-hosted Windows runners; `windows-latest` exact current image (sources conflict on 2025 vs 2026 migration dates).
- Artifact Signing individual-developer eligibility; EV/SmartScreen change month (Mar vs Aug 2024).
- PyInstaller/Nuitka Windows start-up and AV false-positive rates (only single-author Linux benchmark and vendor claims found).
- ProcDump `-w`/`-x` semantics; TransactionTimeout default; Squish object-map behavior; FlaUInspect feature list.
- Original HDD and probabilistic delta debugging papers; ISSTA'07 constraint-handling details; IPOG complexity.
- All similarity weights, heal/map thresholds, FDR/minimum-count defaults and perf-test thresholds are provisional design values, not literature values.
