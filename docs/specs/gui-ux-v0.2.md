# Overgrind v0.2 GUI: UX specification
Owner: GUI. Code: `overgrind/gui/*` (entry `python -m overgrind.gui [--demo]`). Contracts: `overgrind/types.py`. Depends only on the public APIs in `architecture-v0.2.md`.

## Personas
- **Dana, QA tester (primary)**: runs the app by hand every day, no programming. Wants "point at things, press Start, tell me what broke". Never sees JSON, regexes or stack traces unless she asks.
- **Sam, QA lead**: reviews results, compares two builds, files issues. Wants a reproducible recipe and an honest statement of what was covered.
- **Priya, developer**: receives the issue text and the `.ovg.json` project; wants exact values, seed, steps.

## Guided flow (left-hand stepper, 7 pages; every page shows one "What to do now" line)
1. **Target**: pick a running window or browse an EXE (+ arguments). Shows path, 32/64-bit, PID, title.
2. **Inspect**: scan the app, search the element tree, read the detail card, add elements as features or controls.
3. **Features**: name each feature, set its states and default, mark dangerous ones, add rules between features.
4. **Controls**: choose the values to try; compare the app's original range with the range we will test.
5. **Plan**: strategy, how long, seed, reset policy; see the estimate and a 20-case preview; run the preflight check.
6. **Run**: Start / Pause / Stop, live numbers, Feature-State Atlas, recent actions.
7. **Errors**: browse failures, Reproduce and shrink, Copy as issue, Open folder.
Back / Next buttons sit under every page. Next is never blocked; problems are shown, not enforced, except Start (needs zero errors).

## Wireframes (1100x700; stepper 190px; content scrolls vertically only)
```
+-File Edit Tools Help-----------------------------------------------------+
| 1 Target   | Step 2 of 7  Inspect                                         |
| 2 Inspect* | What to do now: Click Scan, then pick an element on the left |
| 3 Features | [!] 2 problems: "Feature 'Dark mode' has no name" [Fix]      |
| 4 Controls | [Scan app] [Search...........]                               |
| 5 Plan     | +-Elements---------+ +-Details------------------------+    |
| 6 Run      | | v Main window    | | Dark mode  CheckBox            |    |
| 7 Errors   | |   [x] Dark mode  | | Suggests: toggle  (v) Verified |    |
|            | |   [ ] Volume     | | Stability  [#######--] 72 Good |    |
|            | +------------------+ | Why: ... How to fix: ...       |    |
|            |                      | Backups: 1) path 2) title+type |    |
|            |                      | [Add as feature][Add as control]|   |
|            |      [< Back]                              [Next >]     |
+ Status: Target: Demo app (ready)  |  Run: idle ------------------------+
```
```
Plan:  ( ) State coverage (recommended)   ( ) Ordered pairs (legacy)  ...cards
       How long?  (o) Until I stop it   ( ) For [10] minutes   ( ) [1000] cases
                  ( ) First error ( ) [3] different errors ( ) One full pass
       Estimate: "1,024 valid states; ~4.2e12 value combos - random sampling"
       [Preview 20 cases]   [Run preflight check]   Trust Ledger: ok/warn/fail rows
Run:   [> Start] [|| Pause] [[] Stop]   Elapsed 00:12  Cases 3,102 (258/s)
       Errors 2 unique  Crashes 1  Hangs 1      Atlas: rows=features, columns=states,
       cell = visit count (number + shading); ring = coverage; guarantee sentence verbatim
Errors: [filter] table(time|category icon|signature|count|reliability)  | detail pane
       [Reproduce and shrink] [Copy as issue] [Open folder]
```

## Empty and error states (always say what to do, never just "empty")
- No target: "Pick the window you want to test, or browse to an EXE." Inspect/Run show the same hint.
- Nothing scanned: "Press Scan app. We only read the screen; nothing is clicked."
- No features: "Add at least one feature (Inspect > Add as feature, or Add manually)."
- Invalid project: banner lists each `validate()` message in plain words; `warning:` messages are amber and do not block Start; others are red and block Start. Each has a **Fix** link that jumps to the page.
- Run problems: harness errors are shown as "Overgrind could not drive the app (not an app bug)".
- No errors yet: "No failures so far. Failures appear here the moment they happen."
- Missing backend/adapter (e.g. UIA on Linux): explain and suggest `--demo`.
- Closing while running: confirm, then stop the worker cleanly. No other dialogs during a run.

## Accessibility
- Everything reachable by Tab; stepper is a list (Up/Down, Ctrl+1..7). Visible focus ring (Qt default kept).
- Status is never colour alone: confidence badges and ledger rows carry an icon shape AND a word (Verified, Inferred, Manual, Unknown; OK, Warning, Fail).
- Atlas cells print their count; the Atlas has an accessible description summarising coverage.
- Contrast: semantic colours are chosen separately for light and dark palettes (>= 4.5:1 text).
- Follows OS light/dark palette; DPI-aware (Qt high-DPI scaling, sizes in font-metric units); min size 900x600, no horizontal scrolling at 1100x700.

## Keyboard map
Ctrl+N New, Ctrl+O Open, Ctrl+S Save, Ctrl+Shift+S Save As, Ctrl+Z / Ctrl+Y undo / redo, Ctrl+1..7 jump to page, Alt+Left / Alt+Right Back / Next, Ctrl+F focus search, Ctrl+R preflight, F5 Start, F6 Pause/Resume, Shift+F5 Stop, F1 shortcuts, Ctrl+Q quit.

## Copy tone
Plain, calm, second person. Short sentences. Say what happened, why, and what to do. Avoid: selector, UIA, domain, fingerprint, ring buffer (use element address, value range, error signature, recent steps). Never blame the user. Never claim more than we measured: "covered 380 of 1,024 states", not "mostly covered". Locator warnings always give the reason and the fix ("This button has no stable ID, so it is found by its position. Ask the developer to add an AutomationId.").

## Architecture notes
`ProjectModel` (QObject) owns the Project, path, dirty flag and undo stack; every edit is `dataclasses.replace` through `ProjectModel.edit(label, fn)`. Pages observe `changed`. All long calls (scan, estimate, preview, preflight, reduce, differential, start/stop) run on `QThread` workers; the run page only polls `RunController.stats()` from a 400 ms `QTimer`. Backend access is centralised in `gui/backend.py` (lazy imports, tolerant result normalisation).
Atlas data note: `RunStats.coverage` has no per-state counts, so cell counts are tallied from the polled action tail ("sampled"); totals and ring come from `Coverage`.
Stop conditions are not a field of `Project`, so the GUI stores them as `run.strategy_options['stop']` (a `StopConditions` dict, saved with the project) and passes `stop=` to `RunController`.

## QA-GUI amendments (v0.2 hardening)
- **Start gate (Trust Ledger)**: a preflight with any Fail row, while it still describes the current project, makes Start ask for the project name to be typed. The same typed confirmation covers enabled dangerous features and controls of unknown type. It is asked once per project version.
- **Validation banner**: shows this page's problems plus the first blocking problem from another page with a Fix link; validate() texts are rewritten in plain words using feature names.
- **Plan** keeps the estimate and [Preview] [Run preflight check] in a footer that is always visible; results scroll into view.
- **Errors**: actions sit above the detail text; Reproduce shows replay progress and a Cancel that keeps the best result; double-click opens the evidence folder; the list is a lazy model (5,000 rows stay responsive, reloads insert only new rows).
- **Closing during a run** stops the controller on a background thread, then closes. Unexpected exceptions are logged to `overgrind-gui.log` and shown calmly (never silently).
- **Undo**: typing in one box is one undo step; undoing back to the saved state clears the dirty flag.
- **Atlas**: cells wrap for many states, red outline plus "!" marks states where an error happened, hover tooltips; uses exact per-state counts when `Coverage` provides them, otherwise says "sampled".
