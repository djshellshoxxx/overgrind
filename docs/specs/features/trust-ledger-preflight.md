# U2 Trust Ledger and Preflight: provenance on every fact, dry-run before a long run, danger guard
Status: proposed. Owners: `project.py` (CORE: ledger fields/validation), new `preflight.py` + `danger.py` (propose RUNTIME), `adapters/uia.py`, GUI preflight dialog. Types: `Provenance`, `Selector`, `FeatureDef.dangerous`, `ControlDef.sensitive/skip/read_only`, `ElementInfo.confidence`, `HarnessError`, `TargetAdapter`.

## 1. Problem & why it matters
A multi-day unattended run built on a wrong fact (guessed range, selector that hits the wrong widget, reset that does not reset, a "Save" button treated as a toggle) wastes the run or, worse, damages data (Delete/Format/Send/Pay clicked thousands of times) and produces false defects. Black-box teaching ("point at it") yields facts of very different quality. Users need to see which facts are verified and have the tool *prove* the model works before committing hours.

## 2. Prior art & what they do poorly
| Tool | What exists | Gap |
|---|---|---|
| TestComplete Name Mapping / self-healing; Ranorex repository [V: SmartBear self-healing page; Ranorex dynamic-ID blog] | stored recognition criteria, heal at run time, suggest update after run | no per-fact confidence; no pre-run validation of whole model; no value-domain provenance |
| Inspect.exe (legacy [V]), Accessibility Insights [V], FlaUInspect | show properties/patterns | read-only viewers; no verdict whether a value is safe to write |
| FuzzWinForms [V: README listed in strategy doc], monkey tools | random input | no danger classification of what is clicked |
| UI Automation docs [V] | AutomationId optional, only sibling-unique, may change per release | tools do not surface this uncertainty |
Overlap: self-healing and object repositories exist (TestComplete, Ranorex) -- not claimed as novel. Candidate novelty: *fact-level* provenance + a mandatory run-gating preflight that exercises write/read-back/reset on the live target + keyword-and-context danger guard integrated with the scheduler. We found no tool doing all three; claim stays "not found together".

## 3. Design
### 3.1 Trust ledger
Every fact (selector, feature state set, control domain, default, read-only flag, constraint, scope) carries `Provenance(source, confidence, note)`. Ladder (higher = stronger):
| confidence | meaning | how earned | expires when |
|---|---|---|---|
| `verified` | observed live by Overgrind on this target build via two independent reads/writes | preflight pass (3.2) or successful runtime read-back | target exe hash/version changes -> demote to `inferred`; 30 days -> `inferred` (stale) |
| `inferred` | derived from UIA hints (ElementInfo.suggested_*) or approved inference (U4) | inspector, infer.py + human approval | re-verified |
| `manual` | typed by the tester | editor | never auto-changes; preflight may *contradict* it |
| `unknown` | no evidence | defaults | -- |
`note` carries a compact evidence trail (v1, no types change): `"ev:v1;t=2026-10-14T10:00Z;n=3;tier=1;rb=ok;build=<sha8>"`. Rendering parses `ev:` and shows human text. Facts never silently upgrade: only `preflight.apply(report)` or approved inference proposals rewrite provenance, and each rewrite is a project diff the user sees.
Run gating (`project.validate` + controller start): block start if any enabled control/feature has `confidence=="unknown"` unless acknowledged ("I know" checkbox stored in project `run`-adjacent metadata, delta); warn if `invalid_values=True` with any `manual`/`inferred` domain; warn when >30% of facts are `manual`.

### 3.2 Preflight (dry run) -- ordered checks, each `pass|warn|fail`
| id | check | method | fail/warn rule |
|---|---|---|---|
| P01 | target identity | adapter.attach; pid alive; exe hash computed; integrity level of target vs worker | fail if target elevated and we are not (UIPI) [K]; warn if x86/x64 mismatch with dump tooling |
| P02 | selector resolves | resolve each feature/control selector 3x; record matches per tier | fail if 0 or >1 candidates on final tier; warn if resolved only via tier >= 3 (fragile; see B1) |
| P03 | element belongs to target | owning pid in target process tree | fail otherwise (never act on other process) |
| P04 | type/pattern fit | control kind vs ElementInfo patterns (Toggle/RangeValue/Value/SelectionItem/ExpandCollapse/Invoke) | fail if required pattern absent |
| P05 | read baseline | `get_control` == declared default (type-coerced, tolerance for float 1e-9 rel) | warn on mismatch, offer "adopt observed as default" (-> `verified`) |
| P06 | write/read-back | for each non-readonly fuzzable control: write 3 values (boundary-low/mid/boundary-high of *fuzz* domain), read back each, restore default | fail if read-back never equals written (clamped/ignored); warn if differs by clamp -> propose narrowed domain |
| P07 | feature toggle round-trip | set each state, read back, return to default | fail if state does not stick |
| P08 | reset recipe | perturb 3 random controls+features, call `adapter.reset(project)`, verify all declared defaults restored; repeat twice | fail if any residue (reset incomplete) -- run cannot claim "independent cases" |
| P09 | dependency sanity | feature scope/`requires` violated by set? writes while prerequisites inactive produce read-back mismatch? | warn |
| P10 | timing | p50/p95 of op latency over >= 20 ops; `settle_ms` vs observed quiescence | warn if `op_timeout_ms < 5*p95`; suggest values |
| P11 | harness stability | re-resolve all selectors after window resize-to-same-size, tab round trip; target health ok throughout | warn on flips |
| P12 | danger scan | section 3.3 over all selected items and over *reachable neighbours* (siblings in same dialog) | `block` items -> fail unless excluded |
| P13 | file-dialog/modal containment | detect modal appearing during probes; apply dismiss policy | warn |
| P14 | evidence path | writable, free space >= 1 GiB, screenshot works | fail/warn |
**Preflight never invokes `action` features** (side effects) -- it checks resolution, `InvokePattern` presence and danger only; a feature may be flagged `safe_to_probe` by the user (delta) to include it. It restores baseline after every probe and performs `reset` at end; total budget 120 s default, 20 ops per control cap. Output `PreflightReport(items, can_start, summary, started, finished, target_hash)`; `can_start = no fail`. Report is stored (small JSON) in the project folder, not per success event.

### 3.3 Danger guard
Tiers: `block` (never automated unless user explicitly clears `FeatureDef.dangerous`), `warn` (allowed but shown, requires confirm at start), `disruptive` (kills/changes target session: Exit, Close, Quit, Restart, Shutdown, Log off).
Matching over normalized text of: element Name, AutomationId, HelpText, tooltip, parent group/dialog title, window title. Normalize: casefold, strip accelerator `&`/`_`, trailing `...`/`…`, NFKC, collapse whitespace; match whole words/prefix tokens (not substrings: "Savannah" must not match "save"), also camel/snake split of AutomationId (`btnSaveAs` -> save as).
Seed lexicon (English; extensible per-project and per-locale; localized seeds are **K**, review needed):
- block: save, save as, delete, remove, erase, wipe, format, purge, overwrite, replace all, send, submit, post, publish, pay, purchase, buy, order, checkout, transfer, withdraw, uninstall, install, upload, share, email, sign, confirm payment, empty, factory reset, clear all, revoke, terminate, kill, drop.
- disruptive: exit, quit, close, shutdown, restart, log off, sign out.
- warn: print, export, import, apply, update, reset, ok (inside a dialog whose title matches a block word), yes (in confirmation dialogs).
- de: speichern, loeschen, senden, bezahlen, kaufen, beenden; fr: enregistrer, supprimer, envoyer, payer, quitter; es: guardar, eliminar, enviar, pagar, salir; ja: 保存 削除 送信 支払; zh: 保存 删除 发送 支付 (K).
Context rules: a control inside a dialog whose title is block-matched inherits `warn`; file dialogs (`#32770` with Save/Open) are `block` for the Save button, fuzz strings are never typed into path fields unless the field is declared with a sandbox directory; Password edit (UIA `IsPassword`) forces `ControlDef.sensitive=True`. Runtime tripwire: before each invoke/toggle the adapter re-reads the element Name; if it now matches a block word that was not present at teach time (label changed, e.g. "Add" -> "Delete"), raise `HarnessError` (never a defect) and skip. `FeatureDef.dangerous=True` is set automatically on `block` hits at teach time (user can clear with a confirm that is logged in provenance note).
Environment safety banner: first run per project shows "Run on a disposable VM/profile with test data" and records acknowledgement; project may set `target.process_name` allow-list; input injection verifies foreground/owner pid before every mouse/keyboard op (fallback tier only).

## 4. Data / API contracts
- `preflight.run_preflight(project, adapter, *, budget_s=120, progress=None) -> PreflightReport`; `preflight.apply(project, report) -> Project` (returns new frozen `Project` with upgraded provenance/defaults the user accepted).
- `PreflightItem(id:str, level:str, subject:str, message:str, fix:str, evidence:dict)`; `PreflightReport` dataclass in `preflight.py` (not in types.py; delta).
- `danger.classify(texts: list[str]) -> DangerFinding(tier, matched, source_text)`; `danger.scan_elements(list[ElementInfo]) -> dict[int, DangerFinding]`.
- `project.validate` gains rules: unknown-confidence gate; dangerous feature enabled without override => error.
- Evidence trail encoding in `Provenance.note` (`ev:v1;k=v;...`); parser `provenance_info(note)->dict`.

## 5. UX hooks
Preflight dialog: table with traffic lights, click row -> highlights element in target (overlay) and shows fix button ("mark dangerous", "adopt observed default", "narrow domain to 0..99"). Inspector badges (V/I/M/?) per fact; hover shows evidence trail. "Start" disabled while any fail; override requires typing the project name for unknown/dangerous. Danger review screen before first run lists every `block`/`warn` hit with the text that triggered it.

## 6. Acceptance tests (MockAdapter + scripted variants)
1. Mock with a control whose adapter ignores writes => P06 fail, `can_start False`.
2. Mock with clamped writes (max 100 declared 200) => warn + proposal domain 0..100, provenance becomes `verified` only after user applies.
3. Reset that forgets one control => P08 fail naming the control.
4. Two elements match one selector => P02 fail with both candidates listed; selector resolved only via index => warn.
5. Danger lexicon: "&Save As...", "Delete all", "btnPayNow", "Savannah" (no match), "Remove from cart" (block), "Löschen" (block), "Exit" (disruptive); property test: 1,000 random benign words never match.
6. Tripwire: label changes "Add"->"Delete" at runtime => HarnessError, zero invokes, counter `danger_skips==1`.
7. Preflight does not call `invoke` on action features (Mock records calls) unless `safe_to_probe`.
8. Baseline restored after preflight even when P06 raises midway (try/finally; Mock state equals defaults).
9. Exe hash change => all `verified` demoted to `inferred` on load; run blocked until re-preflight or acknowledgement.
10. Password-type element auto `sensitive=True`; its values absent from exported evidence.
11. Preflight budget 120 s exceeded => partial report, `can_start False`, message "incomplete".
12. Start with an `unknown` control without acknowledgement => `project.validate` error; with acknowledgement => allowed and logged.

## 7. Metrics
% facts by confidence; preflight pass rate on first try; time to preflight; harness-error rate in first hour of runs after pass vs skipped preflight (should drop); danger hits per project; tripwire trips; false-positive danger hits reported by users.

## 8. Risks
Lexicon false negatives give a false sense of safety -- UI states it is a heuristic and recommends a VM. False positives annoy: per-project allow override with log. Preflight itself writes to the target (mitigated: baseline restore, P06 only on declared fuzz domains, no actions). Localized lists unreviewed. Verified-after-write can be wrong if the app reflects the write in the UI but fails to commit (needs a semantic oracle).

## 9. Design deltas
1. `Provenance` has no evidence/timestamp fields -> encode in `note`; propose `Provenance.evidence: tuple[str,...]` and `checked_at: str`.
2. `ControlDef` lacks `dangerous`/`safe_to_probe`; `FeatureDef` lacks `safe_to_probe` -> use `skip=True` + note, or add fields.
3. New modules `preflight.py`, `danger.py` not in the ownership table (propose RUNTIME owns; `project.validate` calls danger).
4. "Unknown-acknowledged" flag needs a home (propose `RunConfig.ack_unknown: bool`).
5. `TargetAdapter` needs optional `element_text(selector)->str` (tripwire re-read) and `owner_pid(selector)->int|None`; adapters lacking them degrade to warn.

## Sources
- https://support.smartbear.com/testcomplete/docs/testing-with/running/self-healing-tests.html
- https://www.ranorex.com/blog/automated-testing-and-dynamic-ids/
- https://learn.microsoft.com/en-us/windows/win32/winauto/inspect-objects ; https://accessibilityinsights.io/docs/windows/getstarted/inspect/
- https://learn.microsoft.com/en-us/uwp/api/windows.ui.xaml.automation.automationproperties.automationid
- UIPI/integrity levels and `IsPassword`: engineering knowledge [K], not re-verified.
