# B1 Smart Inspector and Selectors: stability scoring, ranked fallbacks, self-healing, "why is this fragile"
Status: proposed. Owners: new `selectors.py` (propose RUNTIME; pure logic + UIA-agnostic), `adapters/uia.py` (tree access), `gui/*` inspector. Types: `Selector`, `ElementInfo`, `Provenance`, `HarnessError`, `TargetAdapter.inspect`.

## 1. Problem & why it matters
Every later feature (reset, dependency inference, differential twin, 3-day runs) rests on selectors that keep resolving to the *same* element. Inspectors show properties but not "will this still work tomorrow?", and self-healing in commercial tools can silently bind to the wrong control, turning a harness problem into a fake defect or, with write operations, into data damage.

## 2. Prior art & what they do poorly
| Tool | Facts | Gap |
|---|---|---|
| Inspect.exe [V: MS Learn] | SDK-only, legacy ("use Accessibility Insights"), shows UIA+MSAA properties/patterns in tree + data view | no selector, no stability judgement |
| Accessibility Insights for Windows [V] | modern UIA tree + property/pattern view, pause option, accessibility scanning | accessibility-lens, not automation-lens |
| FlaUInspect [K] | open-source UIA tree browser for FlaUI users | no scoring [K] |
| TestComplete Name Mapping [V] | stored criteria; self-healing checks same hierarchy level, with Extended Find deeper; after run suggests updating criteria | heal decision not explained, no threshold shown to user |
| Ranorex [V] | self-healing on by default since 10.1.1; robust RanoreXPath mode; healed steps show **Warning**; global path weight rules | weights manual |
| Squish object map [K, not verified in this pass] | real-name properties with wildcards | -- |
| Similo / VON Similo [V] | weighted multi-attribute web element matching beats single locators (11% vs 27% failures on 48 sites; 94.7% vs 83.8%) | web DOM; no desktop UIA |
UIA facts [V: MS Learn AutomationId/RuntimeId]: AutomationId unique only among siblings, not guaranteed stable across releases/builds, optional, but localization-independent; RuntimeId is opaque, reusable. Overlap: object spy + repository + healing are standard (not novel). Differentiators: *explained* stability score from live probes, bounded/explicit healing with margin rule that **refuses to guess**, selector-level provenance feeding U2.

## 3. Design
### 3.1 What makes a UIA selector fragile (heuristics, each with reason code)
| code | condition | penalty | explanation shown |
|---|---|---|---|
| F01 | no `auto_id` | -35 | "No AutomationId: falls back to Name which changes with language/labels" |
| F02 | `auto_id` looks generated: all digits, GUID, `\w*[0-9a-f]{8,}`, Win32 numeric control id (`^\d+$`), WinForms `WindowsForms10.*.app.0.<hash>` class [K], `Item \d+` | -30 | "Looks auto-generated; changes between builds" |
| F03 | `auto_id` non-unique among siblings (dup count d) | -25 | "N siblings share this id" |
| F04 | `title` empty | -15 | |
| F05 | `title` has digits/dates/units/`%`/length>40 or equals current `value` (value-as-name: edits/labels) | -25 | "Name follows displayed data" |
| F06 | non-ASCII/localizable text without auto_id | -10 | "Language dependent" |
| F07 | `path` depth > 6 or any ancestor segment generic (Pane/Group without id) | -10 | "Layout dependent" |
| F08 | `index` used | -30 | "Position among identical siblings" |
| F09 | `class_name` generic ("Edit","Static") or contains hash | -5 | |
| F10 | element in virtualized/recycled container (ListItem inside DataGrid with virtualization patterns) | -20 | "Items are recreated on scroll" |
| F11 | resolves with >1 match at best tier | -30 | |
| F12 | owning window title contains document name/counter (path uses window title) | -15 | "Window title changes with the open file" |
Base 100, subtract penalties (floor 0), bonus +5 each for: auto_id unique in whole window; `control_type` matches expected for the declared `ControlDef.kind`. Grade A >= 85, B 70-84, C 50-69, D < 50.
**Framework hints [K]**: WinForms AutomationId = control `Name` (stable if designer names kept), Win32 = numeric dialog control id (stable within a build, weak across), WPF = `AutomationProperties.AutomationId`/`x:Name`, Qt = `objectName` when Qt UIA provider exposes it, Electron/Chromium = DOM-derived, often unstable. Surface as hints, not scores.

### 3.2 Empirical probe (beats heuristics)
`probe(selector, element)` performs, read-only: (1) re-resolve 3x (time-spaced 200 ms), (2) after window resize to same size and back, (3) after navigating away and back (only if the user opted in and the page feature has a declared activation), (4) optional after target restart (preflight, once). Fraction `s` of resolutions returning the same element (fingerprint = control_type + bounds-relative-to-parent within 3 px + auto_id/title) -> `empirical = 100*s`. Final `score = round(0.5*heuristic + 0.5*empirical)` when probes ran, else heuristic with flag `unprobed`. Any single failed re-resolution caps grade at C.

### 3.3 Ranked fallbacks (derived from the single frozen `Selector`)
Tiers = attribute subsets, tried in order; stop at the first tier yielding exactly 1 candidate whose `control_type` matches:
1. `auto_id` + `control_type` (window-scoped)  2. `path` + `auto_id`  3. `path` + `title` + `control_type`  4. `title` + `control_type` + `class_name`  5. `path` + `control_type` + `index`  6. (opt-in) relative geometry: nearest `control_type` within 8% of window size of the taught bounds fraction + neighbour label.
`types.py` `Selector` stays unchanged: the tier list is a pure function `tiers(selector)`; the *taught* geometry/neighbour label needed for tier 6 is stored in `Provenance.note` (`geo=x,y,w,h;lbl=...`) -- delta.
`resolve(root, selector)->Resolution(element, tier:int, candidates:int, score:float, healed:bool, reason:str)`; non-heal resolution never allows `candidates!=1`: >1 => `HarnessError("ambiguous selector")` with candidate list.

### 3.4 Self-healing (explicit, bounded, never silent)
Trigger: tiers 1-5 all fail (0 candidates) or ambiguous. Procedure: enumerate candidates under the taught window (cap 2,000 nodes), score each with the shared similarity (same function and weights as U5 3.2: auto_id .25, title .25, path .15, class .05, relative position .15, neighbour label .10, patterns .05), hard filter `control_type` equal and owner pid in target tree.
**Accept** iff best >= 0.85 AND margin to second >= 0.15. **Otherwise** raise `HarnessError("cannot resolve")` -- never guess (continuous-gui spec: never accept guesses silently). For *write* operations (set_control/invoke) additionally require pre-write plausibility: current value parses as the control's `Domain.kind`; post-write read-back succeeds; any failure => revert + HarnessError + `unhealable` mark. Healing never applies to `dangerous` features (U2).
Healed resolutions: counted (`heals`, per selector), logged at run level (not per case), shown in dashboard as warning chip, and emitted as a **selector update proposal** (new `Selector` + score + diff) -- applied to the project only by user approval (and then `Provenance` -> `inferred` with `ev:` trail). Three consecutive heals to the same candidate => auto-pause suggestion "UI changed, update selectors". Heal rate > 5% of resolutions => run flagged `degraded` in report.
Distinct from defects: all failures here are harness category; a run never converts unresolved selectors into target failures unless target liveness probes agree (failure-detection research).

### 3.5 Inspector behaviour
Read-only snapshot via cache request; tree with search; crosshair mode (UIA `ElementFromPoint` on mouse position, highlight rectangle overlay in a separate topmost transparent window; no focus steal); property grid (AutomationId, Name, ControlType, ClassName, FrameworkId, IsEnabled, IsOffscreen, bounds, patterns, value, HelpText, `IsPassword`); never shows/stores RuntimeId (not persistent) except for in-session identity; shows tier table: strategy -> match count -> latency; "Why fragile?" panel lists triggered F-codes with the plain explanation and a fix suggestion (ask devs to set `AutomationProperties.AutomationId`/`objectName`; choose parent container; use label neighbour; avoid index); "Alternatives" lists better ancestors/descendants with higher score; copy selector JSON; "Pin as feature/control" (creates `FeatureDef`/`ControlDef` with `Provenance("uia","inferred")` and suggested domain from patterns: RangeValue -> numeric min/max/step; Toggle -> boolean; Selection/ExpandCollapse+items -> choice; Value -> text).

## 4. Data / API contracts
- `selectors.tiers(sel)->list[TierSpec]`; `selectors.score(sel, element, tree, *, probe=None)->SelectorReport(score:int, grade:str, reasons:list[Reason(code,severity,text,fix)], tiers:list[TierResult], empirical:float|None)`; `selectors.resolve(tree|root, sel, *, allow_heal=True, heal_threshold=0.85, heal_margin=0.15)->Resolution`; `selectors.similarity(a: ElementInfo|Selector, b: ElementInfo)->float` (shared with U5); `selectors.propose_update(res)->SelectorUpdate`.
- Parallel list aligned to `TargetAdapter.inspect()` results: `score_all(infos)->list[SelectorReport]`.
- UIA tree access stays inside `adapters/uia.py`; `selectors.py` operates on `ElementInfo`-like dicts so it is unit-testable on Linux.

## 5. UX hooks
Badge (A-D) on every tree row and in the feature/control editors; traffic-light "Selector health" in preflight (U2 P02/P11); heal counter in dashboard; "Review selector changes" dialog (before/after, score, approve/reject).

## 6. Acceptance tests (synthetic ElementInfo trees)
1. Element with unique non-generated `auto_id` + type => grade A (>= 85); no id + Name "Item 3" at index 2 => grade D with F01+F05+F08 listed.
2. Reason texts non-empty and each code has a fix string (table test).
3. Generated-id detector: `"1042"`, GUID, `"WindowsForms10.EDIT.app.0.141b42a_r6_ad1"` flagged; `"btnApply"`, `"widthEdit"` not.
4. Heal: rename `auto_id` btnApply->btnApplyNow, same title/position => healed with score >= 0.85, margin >= 0.15, flagged, proposal generated, project unchanged.
5. Two near-identical candidates (margin 0.05) => `HarnessError`, no heal.
6. Control type changed (Button->CheckBox) => never healed.
7. Candidate in a different process => never healed.
8. Write path: healed target whose value fails to parse => revert + error + no write.
9. Dangerous feature never healed.
10. Ambiguity without heal (2 matches at tier 3) => `HarnessError` listing both.
11. Probe: element recreated with different bounds after resize => empirical < 100, grade capped at C.
12. Inspector read-only: Mock adapter call log contains no set/invoke during inspect/probe.
13. Performance: scoring 2,000 elements < 1 s in pure Python.
14. `selectors.py` imports without pywinauto/PySide6 (import hygiene test).

## 7. Metrics
Selector grade distribution per project; % resolutions at tier 1; heals per 1,000 resolutions; wrong-heal rate (audit on fixtures: target 0); time-to-resolve p95; fraction of harness errors due to selectors; user "fixed selector after explanation" rate.

## 8. Risks
Weights/thresholds are provisional, not validated on desktop UIA corpora (Similo evidence is web-only [V]); probes can perturb UI (resize/navigation) -> opt-in; frameworks (Electron) with dynamically generated ids score low although workable; healing may mask real regressions (mitigated by proposals + counters + never-silent); `ElementFromPoint` on elevated windows fails; DPI-virtualized coordinates for crosshair on mixed-DPI monitors need per-monitor awareness [K].

## 9. Design deltas
1. `Selector` has no stability score and no geometry/neighbour fields -> keep frozen; scores computed on demand; taught geometry/label kept in `Provenance.note`; propose optional `ElementInfo.stability:int=-1`.
2. New module `selectors.py` (not in ownership table); `similarity()` shared with `diffrun.py`.
3. Documented "ordering auto_id > path > title+type > index" in `Selector` docstring becomes tier list 1-6 (adds class_name tier and geometry fallback).
4. `TargetAdapter` needs `resolve_info(selector)->ElementInfo|None` and a tree-walk entry for heal candidates (`inspect()` is whole-tree; acceptable but slow).
5. Heal thresholds (0.85/0.15) and penalty table are initial values pending fixture calibration.

## Sources
- https://learn.microsoft.com/en-us/uwp/api/windows.ui.xaml.automation.automationproperties.automationid ; https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-getruntimeid
- https://learn.microsoft.com/en-us/windows/win32/winauto/inspect-objects ; https://accessibilityinsights.io/docs/windows/getstarted/inspect/ ; FlaUInspect repo (not re-fetched): https://github.com/FlaUI/FlaUInspect
- https://support.smartbear.com/testcomplete/docs/testing-with/running/self-healing-tests.html ; https://www.ranorex.com/blog/automated-testing-and-dynamic-ids/ ; https://support.ranorex.com/hc/en-us/articles/38028419260433
- https://arxiv.org/abs/2208.00677 ; https://arxiv.org/abs/2301.03863
