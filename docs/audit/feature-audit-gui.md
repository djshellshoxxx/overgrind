# GUI feature audit (QA-GUI, v0.2)

Method: the real app was driven offscreen (`--demo` flow, scripted, screenshots at 1100x700 and 900x600, light and dark), plus tests in `tests/test_gui_qa.py`. Status: I = implemented, P = partial, M = missing.

## UX spec (gui-ux-v0.2.md)
| Requirement | Status | Evidence / gap |
|---|---|---|
| 7-page stepper, Back/Next, "What to do now" on each page | I | stepper list, PageBase hint; nav labels name the next page |
| Next never blocked; Start blocked on errors | I | `test_gui_run::test_start_blocked_without_features`; Start re-validates synchronously (`test_start_rechecks_validation_synchronously`) |
| Empty states (no target, no scan, no features, no errors) | I | each page has text with next action |
| Invalid-project banner, amber warnings vs red blockers, Fix link | I (fixed) | was hiding other-page blockers and using jargon; now plain words + Fix link |
| Run problems shown as "not an app bug" | I | `friendly_reason` (now single line) |
| Missing backend explains and suggests `--demo` | I | `BackendMissing` messages |
| Close while running: confirm, stop cleanly | I (fixed) | stop was on the GUI thread; now async (`test_close_while_running_does_not_block_the_ui_thread`) |
| Keyboard map (Ctrl+N/O/S/Shift+S/Z/Y/1-7/F/R, F5/F6/Shift+F5, F1, Alt+arrows) | I | menu actions; Run actions now enable/disable with state |
| Contrast, icon+word status, Atlas accessible description | I | badges/ledger carry icon and word |
| Min size 900x600, no horizontal scroll | I (fixed) | overflow with wide fonts on Controls/Plan; `test_no_horizontal_overflow_at_min_size_with_wide_fonts` |
| DPI aware | I | PassThrough rounding; checked at 1.25 |

## Product spec "User-facing Windows GUI: workflow"
| Step | Status | Evidence / gap |
|---|---|---|
| 1 Target: process picker or EXE, path, bits, PID, title | P | picker + EXE + card done; no crosshair picker, no version/build hash/automation capabilities, no "owned windows" narrowing |
| 2 Inspect: tree, search, detail, patterns, bounds, access path | P | all present; no hover/crosshair highlight in the target; no manual capture of nested pages |
| 3 Features: name, kind, states, default, include, dangerous, constraints | P | activation/verify/reset actions and UI location not editable; persistent-vs-event-vs-exclusive shown via Kind |
| 4 Controls: original vs restricted domain, skip/read-only, scope | I | editor now clamps to original range (`test_numeric_editor_cannot_leave_the_original_range_or_invert`); step/precision shown; no "allowed mutations" |
| 5 Combination selection: strategy, all-off, reset policy, counts, preview | I | estimate + 20-case preview |
| 6 Duration options | I | until stopped / minutes / hours / cases / unique errors / first error / full pass; no auto-restart watchdog setting beyond restart-on-crash |
| 7 Run: live aggregate stats, Atlas | I | tiles, rings, recent steps; atlas counts are sampled until Coverage exposes exact counts |
| 8 Errors: sortable/filterable, double-click evidence | I (fixed) | double-click opens evidence folder; screenshot thumbnail click opens it |
| 9 Save project, export/import JSON | I | Save/Open/recent/Import v0.1; unicode round trip tested |

## Feature UX hooks
| Hook | Status | Evidence / gap |
|---|---|---|
| U1 Atlas grid, colour by count, red outline for errors, guarantee verbatim, copy report | P | grid + outline + guarantee + Copy coverage report done. Missing: Hilbert view (>4096 states), pair matrix, timeline, "why not 100%" list, unreachable hatching, CSV export |
| U2 Trust Ledger table with icon+word, fix text | P | table done. Missing: row click highlight overlay, fix buttons (mark dangerous / adopt default / narrow domain), inspector evidence trail |
| U2 Start disabled on fail; typed project name override; danger review | I (new) | `PreflightGate` tests; danger list comes from validate warnings; no lexicon match text of its own |
| U2 inspector badges V/I/M/? | I | Verified/Inferred/Manual/Unknown badge in tree, tables, detail |
| U3 Reliability chip, Reproduce spinner/progress/cancel, copy issue, save | P | chip + progress (replay count) + Cancel + Copy + Save-as-file. Missing: diff original vs reduced, "Estimate rate (59 tries)", per-step "removed 31 of 38" |
| U4 Suggest dependencies review | P | list with accept/reject-all and evidence count. Missing: per-row Reject/Test again, q-value, impact on state count; banner "proposals are hypotheses" added only in spec text |
| U5 Compare builds | P | A/B picker, run, grouped verdicts. Missing: map review table, calibration progress, reliability chips, side-by-side values, report export |
| B1 stability meter, ranked fallbacks, WHY text | I | meter + reason + fix hints + ranked backups with scores; badge A-D letters not used (0-100 + word) |

Counts: UX spec 11 rows (11 I). Workflow 9 steps (5 I, 4 P, 0 M). Hooks 8 rows (3 I, 5 P, 0 M).

## Bugs found and fixed (all with tests)
Dirty flag stayed set after undo to saved state; one undo step per keystroke; run start left session stuck in "starting" when evidence dir unwritable; close during "starting" leaked a running controller; closeEvent stopped the controller on the GUI thread; stale debounced validation let F5 start an invalid project; markup in element names/choice options rendered as HTML; Controls editor let ranges exceed the original or invert (blocking Start with misleading copy); status bar/long names could widen the window; Windows window list lacked ctypes argtypes/restype (64-bit handle truncation), used 1024-char path buffer, listed cloaked/owned/tool/shell windows and fell back to fake demo windows on error; recent list broke on commas, slash variants and vanished files; invalid default file names; quoted EXE paths; backslashes eaten from titles; errors page rebuilt every row on each reload (1.8 s for 5,000) and hid its action buttons below the fold; Atlas clipped many states and was wiped when returning to the Run page; background-task failures without handler were silent; PyInstaller could not see dynamically imported backend modules; wide-font overflow on Controls/Plan.

## Backend requests
- `Coverage`: per-state visit counts, e.g. `state_visits: dict[str, dict[str, int]]` (GUI reads `state_visits`, `visits` or `visit_counts`).
- `Reducer`: cancel flag and progress callback (GUI currently cancels by jumping the injected clock and reads `.replays`).
- `strategies/__init__.py`, `worker.py`, `reduce.py`, `diffrun.py` import modules by name; add static imports for PyInstaller (the GUI backend now lists them behind `if False:`).
- Preflight/validate: structured fix actions per ledger row (for fix buttons) and an acknowledgement field for unknown controls.
- MockAdapter screenshot must be a valid PNG (error detail shows a grey box otherwise).
