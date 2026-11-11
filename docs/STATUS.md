# Overgrind v0.2 status / handoff

Branch `claude/stoic-cerf-9pxib7` (draft PR #4). Last verified: 458 unit tests pass on Linux (offscreen Qt). Windows CI: backend, runtime and UIA-logic tests passed on a real Windows runner; two GUI layout tests failed there and were fixed afterwards. On a real Windows runner desktop (commit 185f577) the full UIA smoke test PASSES: all 9 tests, including the end-to-end controller + worker + watchdog + UIA run that detects a crash, a hang and a modal dialog from a hidden feature combination, dedups them and continues. Still unproven: real third-party apps, elevated targets, multi-monitor/DPI, ProcDump dumps. One GUI layout test (Target page overflow with a wide fallback font, offscreen Windows) still fails; its next failure message lists the widest widgets.

## Built
Core (project v2 model, migration, validation, selector scoring), engine (lazy state enumeration, state_cover / ordered_pairs / streaming t-wise / boundary / adaptive / chaos, honest coverage), diagnostics (errors-only evidence, dedup, redaction, auto-reduce, differential, dependency inference), runtime (controller + spawned worker + watchdog, preflight, mock/UIA adapters, synthetic fixture app, `overgrind v2` CLI), PySide6 GUI (7-page guided flow, Atlas, ledger, reduce, compare), PyInstaller spec and Windows CI.
Audits: `docs/audit/feature-audit-backend.md`, `feature-audit-gui.md`, `windows-audit.md`. Specs: `docs/specs/`, research: `docs/research/`.

## Known gaps (see audits for acceptance-test level detail)
- Never run on a real Windows desktop with a real target app: UIA adapter, ProcDump hook, PrintWindow screenshots, pick-on-screen overlay, process list.
- Unimplemented spec items: sampled-mode confidence intervals and sidecar bitsets (U1/B2), preflight check P09, atomic kill-mid-write reducer test, assertion-oracle API, P1-05 framework bridges, image-based targeting, CI-agent mode.
- Chaos/adaptive streams are not exactly reproduced after a watchdog respawn; screenshots are not masked for sensitive fields.
- Ubuntu CI abort (`none_dealloc: deallocating None`) root-caused: PySide6 6.12.0 leaks `None` references on Python < 3.12 (QTreeWidgetItem.isHidden/setHidden/setToolTip/...). 6.8-6.11 are clean. `pyproject.toml` now pins `PySide6-Essentials>=6.6,<6.12`; verified all 92 GUI tests pass on Python 3.10 + PySide6 6.11.3. Re-check the next CI run.
- Not code-signed; expect SmartScreen warnings.
