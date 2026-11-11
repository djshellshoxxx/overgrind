# Research: reliable crash / hang / dialog detection on Windows (no source access)

Evidence tags: **[V]** = claim confirmed from the primary page's content as returned by a search tool; **[S]** = secondary source (blog, forum, vendor); **[K]** = engineering knowledge not re-verified in this pass. Note: `learn.microsoft.com` was unreachable via direct fetch from the research sandbox (proxy 403), so Microsoft Learn pages were read only through search-engine summaries of the pages. Re-verify exact numbers before shipping.

## 1. Signal catalogue

| Failure | Primary signal | Secondary / corroboration | Pitfalls |
|---|---|---|---|
| Process death | `WaitForSingleObject(hProcess)` + `GetExitCodeProcess`; handle opened with `SYNCHRONIZE\|PROCESS_QUERY_LIMITED_INFORMATION` **at launch/attach** [K] | exit code NTSTATUS (0xC0000005 AV, 0xC00000FD stack overflow, 0xC0000409 /GS fail) [K]; Application log event 1000 "Application Error" [V: WER FAQ] | PID reuse: always pair PID with creation time [K]. Clean `exit(0)` after a fuzz action is still "unexpected exit" only if no Exit/Close feature was invoked. |
| Hang | `IsHungAppWindow(hwnd)`: system says unresponsive when the app is not waiting for input, not starting up, and has not called PeekMessage within an internal 5 s timeout; ghost windows return TRUE; docs say "not intended for general use", timeout subject to change [V] | `SendMessageTimeoutW(hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG\|SMTO_BLOCK, T, &r)`; 0 return = timeout/hung, check `GetLastError()==ERROR_TIMEOUT` [V partial, S] | `SMTO_ABORTIFHUNG` is documented as returning immediately if the receiving thread "appears hung", but practitioners report it is unreliable in some cases (hang begins after call started; suspended threads not reported) [S: classicvb.net, AutoIt/CodeProject threads]. Use two signals, never one. |
| Hang (tool-side) | Our own UIA/adapter call exceeds `op_timeout_ms` (worker killed by watchdog) | process still alive + `IsHungAppWindow` true | A UIA call blocking is evidence only of "target slow OR our driver stuck". UIA ConnectionTimeout default is 2 s (`IUIAutomation2::ConnectionTimeout`, ms) [V]; TransactionTimeout default not found in sources reviewed. |
| Error dialog | enumerate top-level + owned windows of target process tree (`EnumWindows`/`GetWindowThreadProcessId`, `GetLastActivePopup`); dialog class `#32770` [K] | `WerFault.exe` child/sibling process with `-p <pid>` [K]; Application log 1000/1002 [V for 1000] | Legit modal dialogs (Open/Save, About) are not failures. Need allow-list + keyword classifier (section 5). |
| Resource blow-up | `GetProcessMemoryInfo` (private bytes), `GetProcessHandleCount`, `GetGuiResources(GR_GDIOBJECTS/GR_USEROBJECTS)`, threads via toolhelp [K] | trend over feature states (strategy doc G) | Single sample is not a defect; require trend + corroboration. GDI default per-process quota 10,000 [K]. |

## 2. Hang decision procedure (adopted)

Probe every `probe_ms=250` while a case is in flight and between ops: `p1 = SendMessageTimeout(WM_NULL, SMTO_ABORTIFHUNG|SMTO_BLOCK, 500 ms)` on the target's top-level window (and each owned visible top-level window), `p2 = IsHungAppWindow`. `unresponsive = p1 failed OR p2`. Declare **hang** when `unresponsive` is continuously true for `RunConfig.hang_threshold_ms` (default 5000 -- matches OS notion [V]; tester may raise for slow apps). Declare **busy** (no defect) if it recovers before the threshold. Attach `detail={"unresponsive_ms", "cpu_delta_ms" (GetProcessTimes over window), "threads"}`; `cpu_delta_ms≈0` => likely deadlock/wait; high CPU => busy loop. Fingerprint hang by (category, normalized last-invoked control id) only -- we cannot know the hung code location without a dump.
Windows without message loops (console, service-like GUIs, windows owned by a different process e.g. WebView child) can yield false "hung"; treat target-process-tree windows only and require the main window class to be stable across probes.

## 3. UI Automation pitfalls (for `adapters/uia.py`)

- A hung or slow provider blocks the UIA client call until timeout; hung target => hung automation [S: UiPath forum, SO]. Therefore all UIA runs in the **worker process** with a watchdog deadline (already the architecture); kill+respawn worker on `op_timeout_ms`; the target is not killed for a worker stall.
- Never call UIA on the thread that owns your own UI; Microsoft documents conflicts causing "very slow performance or even ... stop responding", and says to run TreeWalker/FindAll/pattern calls on a separate thread [V: UI Automation Threading Issues]. (Overgrind GUI is a separate process from the worker, so satisfied by design.)
- Event subscriptions that may fire from your own UI must be created on a non-UI thread [V]. Overgrind v0.2 should avoid UIA event handlers and poll instead (fewer deadlock classes).
- `RuntimeId` is only unique at a point in time and may be reused; treat as opaque, compare only [V]. Never persist it (see B1).
- Caching (`CacheRequest`) cuts cross-process round trips; benefit is largest for WPF/custom server-side providers, smaller for default Win32 client-side providers [V]. Use `FindAllBuildCache` for `inspect()`.
- Reported (single-source, unverified) slowness/timeouts: `FindAll` in WebView/Edge subtrees while the tree rebuilds (COMException "Operation timed out") [S]. Policy: cap subtree size (default 5,000 elements / 4 s) and retry once.
- Elevation: a non-elevated client cannot drive elevated windows (UIPI) [K]; preflight (U2) compares integrity levels.

## 4. Dumps and tree kill

| Mechanism | Facts | Overgrind use |
|---|---|---|
| WER LocalDumps | `HKLM\SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps` (+ per-exe subkey); off by default; **admin needed**; `DumpFolder` REG_EXPAND_SZ default `%LOCALAPPDATA%\CrashDumps`; `DumpCount` default 10 (oldest replaced); `DumpType` 0 custom/1 mini (default)/2 full; `CustomDumpFlags` default 0x121; not collected if automatic debugging set; apps with custom crash handling unsupported [V] | Never edit HKLM silently. Offer "enable crash dumps for <exe>" button that needs elevation, writes per-exe key `DumpType=2`, `DumpCount` small, and records the change so it can be reverted. Collect new files newer than case start. |
| `MiniDumpWriteDump` | Microsoft says call from a separate process when possible, especially if the target is unstable; in-process loader deadlock risk; DbgHelp single-threaded [V]. | Watchdog (separate process) writes hang dumps via `MiniDumpWriteDump` to opened handle (`PROCESS_QUERY_INFORMATION\|PROCESS_VM_READ`); never inject into target. Match bitness (x86 target from x64 tool yields WOW64 caveats [K]). |
| ProcDump | `-h` dump on hung window (window not responding to messages >= 5 s, same definition as Task Manager; fixed, not configurable -- one AI-generated Q&A says so, unverified) [V/S]; `-e` unhandled exception, `-e 1` also first-chance; `-ma` full memory; default dump = threads+handles only [V]. `-w`/`-x` not confirmed in sources read. | Optional external dependency (not bundled; Sysinternals EULA). Detect `procdump64.exe` on PATH; use `-accepteula -ma -e -h` against pid if user opts in. |
| Job objects | `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` terminates all processes when last handle closes; nested jobs since Win8/Server 2012; kill-on-close applies to child jobs; breakaway flags (`BREAKAWAY_OK` needs `CREATE_BREAKAWAY_FROM_JOB`, `SILENT_BREAKAWAY_OK` does not); child is in every job of its parent's chain [V] | For **launched** targets: `CreateProcess(CREATE_SUSPENDED)` -> `AssignProcessToJobObject` -> `ResumeThread` (no escape window) [K]; job handle held by the watchdog so a controller crash also kills the tree. For **attached** targets: do not kill; offer "terminate tree" only with explicit user consent and PID+start-time check. |

Dumps contain memory (passwords, documents): opt-in (`capture_dump=False` default, matches RunConfig), size cap (default 512 MiB total per run, mini dump default), excluded from issue export unless ticked, listed in evidence manifest.

## 5. Dialog classification

1. Snapshot windows of target tree (visible, top-level or owned popup) before and after each op; new window = candidate.
2. Allow-list (from project, learned during preflight/baseline: dialogs that appear on intentional actions) -> expected, handled by dismiss policy (Esc / default button), not a failure.
3. Error signature = class `#32770` or UIA control type Window with button set subset of {OK, Cancel, Close, Abort, Retry, Ignore, Yes, No, Details} AND (title/text matches `error|exception|fatal|unhandled|assert|stopped working|not responding|runtime|crash|failed|violation|access violation|out of memory` case-insens, localized lists optional) -> `Outcome(category="dialog")`.
4. `WerFault.exe` for target pid => `crash` (process death follows) with `detail["wer"]=true`.
5. Unknown modal that blocks input > `hang_threshold_ms` -> `Health.status="modal"`, category `dialog` with low confidence; never counted as defect until the same dialog repeats >= 2 times with the same normalized text (flagged "unclassified dialog" in UI).

## 6. Crash bucketing / fingerprint practice

- WER buckets on client: app name/version/build date, module name/version/build date, exception code, offset (+ up to 10 generic params) [V: WER FAQ]. Known failure modes [S/V: Old New Thing, MS blogs]: **bucket spray** (module timestamp changes per recompile), **offset drift** without symbols, **coarse buckets** (every exception blamed on same catch frame), **failure shift** when bucketing logic changes.
- Socorro: signature = short string from the crashing thread's stack after ordered rules; too coarse merges unrelated crashes, too fine leaves tiny groups; rules revised regularly; frame fallback order file:line > offset (no module) > module+offset [V: socorro docs, siggen]. Chromium-style: top symbol + short hash of call stack is readable but collides across versions; pure stack hash is precise but brittle to small stack variance [S: chromium.org crash-reports page].
- Crashpad: out-of-process handler; client ID vs crash ID to count affected users per signature [V partial].

**Overgrind fingerprint (v1 black-box)**: `sha1(category | key)[:16]`, where key is the first available tier:
1. crash with dump/ProcDump/event-log data: `exception_code | module_basename.lower() | module_offset_hex` (offset, not address, so ASLR-stable; module version excluded so one bug across builds still groups within a build and can be re-grouped by `env`) -- optionally top-3 app-module frames if a minidump parser is present.
2. crash without details: `exit_code | last_invoked_control_id`.
3. hang: `last_invoked_control_id | window_class`.
4. dialog: `normalize(title) | normalize(text)`.
5. assertion/read-back: `control_id | kind (mismatch) `. 6. harness: `exception class | selector tier` (never mixed with target buckets).
Fuzz *values* never enter the key (they would split one bug into thousands of buckets); keep them in the event body.
**Normalization of volatile text** (applied in order): casefold; strip `(not responding)` suffix and trailing app-name segments; `0x[0-9a-f]{4,}`->`0xADDR`; GUIDs->`GUID`; `\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?`->`TS`; `\b\d+\b`->`N`; `[A-Za-z]:\\Users\\[^\\]+`->`%USERPROFILE%`; temp/random file names `tmp[0-9a-z]{4,}`->`TMP`; PIDs/TIDs; collapse whitespace; cap at 256 chars. Quoted substrings that equal a fuzz value of a `sensitive` control are redacted first.
Merge policy: same fingerprint => `EvidenceStore.bump`; never merge across categories; cross-build re-bucketing is a diffrun concern (U5).

## 7. Harness vs target discriminator (decision table)

| Observation | Verdict |
|---|---|
| Process exited, exit code NTSTATUS error or WerFault seen | target crash |
| Process exited 0, we invoked a feature named Exit/Quit/Close | expected (not a defect), restart per policy |
| Process alive, WM_NULL fails + `IsHungAppWindow` for >= threshold | target hang |
| Our UIA call timed out but WM_NULL probe answers within 500 ms | harness (UIA provider slow) -> `HarnessError` |
| Selector unresolved, window present and responsive | harness (or UI changed) |
| Selector unresolved and window gone | target crash/exit -> recheck liveness first |
| Write succeeded, read-back differs | oracle uncertainty: re-read after `2*settle_ms`; then `assertion` category with `confidence=low` unless a control `verify` oracle is configured |

## References
- https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-ishungappwindow
- https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw
- https://learn.microsoft.com/en-us/windows/win32/wer/collecting-user-mode-dumps
- https://learn.microsoft.com/en-us/windows/win32/api/minidumpapiset/nf-minidumpapiset-minidumpwritedump
- https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects , .../nested-jobs
- https://learn.microsoft.com/en-us/sysinternals/downloads/procdump
- https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nn-uiautomationclient-iuiautomation2
- https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/ui-automation-threading-issues
- https://learn.microsoft.com/en-us/archive/blogs/wer/faq ; https://devblogs.microsoft.com/oldnewthing/20200121-00/?p=103351
- https://socorro.readthedocs.io/en/latest/signaturegeneration.html ; https://chromium.googlesource.com/crashpad/crashpad/+/HEAD/doc/overview_design.md
- https://classicvb.net/articles/ap200009.asp (SMTO_ABORTIFHUNG reliability, secondary)
