# Overgrind v0.2 status / handoff

Branch `claude/stoic-cerf-9pxib7` (draft PR #4). Last verified: 458 unit tests pass on Linux (offscreen Qt). Windows CI: backend, runtime and UIA-logic tests passed on a real Windows runner; two GUI layout tests failed there and were fixed afterwards. The UIA adapter, ctypes process code and the Windows smoke test (fixture app driven through UIA) have NOT yet been proven on a real desktop: check the latest *Windows build* run (step "Windows smoke", artifact `smoke-diagnostics`).

## Built
Core (project v2 model, migration, validation, selector scoring), engine (lazy state enumeration, state_cover / ordered_pairs / streaming t-wise / boundary / adaptive / chaos, honest coverage), diagnostics (errors-only evidence, dedup, redaction, auto-reduce, differential, dependency inference), runtime (controller + spawned worker + watchdog, preflight, mock/UIA adapters, synthetic fixture app, `overgrind v2` CLI), PySide6 GUI (7-page guided flow, Atlas, ledger, reduce, compare), PyInstaller spec and Windows CI.
Audits: `docs/audit/feature-audit-backend.md`, `feature-audit-gui.md`, `windows-audit.md`. Specs: `docs/specs/`, research: `docs/research/`.

## Known gaps (see audits for acceptance-test level detail)
- Never run on a real Windows desktop with a real target app: UIA adapter, ProcDump hook, PrintWindow screenshots, pick-on-screen overlay, process list.
- Unimplemented spec items: sampled-mode confidence intervals and sidecar bitsets (U1/B2), preflight check P09, atomic kill-mid-write reducer test, assertion-oracle API, P1-05 framework bridges, image-based targeting, CI-agent mode.
- Chaos/adaptive streams are not exactly reproduced after a watchdog respawn; screenshots are not masked for sensitive fields.
- Ubuntu CI crashed once (native Qt abort in the Inspect page 5,000-element filter test); defensive fixes landed but are unconfirmed.
- Not code-signed; expect SmartScreen warnings.
