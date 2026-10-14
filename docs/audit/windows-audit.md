# Windows static audit (QA-WINDOWS)

Scope: adapters/uia.py, adapters/procwatch.py, tests/windows_smoke.py, fixtures/, packaging/, windows-build.yml.
Method: static review; pywinauto 0.6.8 sdist source read from PyPI; Qt `qtbase` source (6.5/6.8) read from GitHub.
learn.microsoft.com was not fetched; Win32 semantics below come from engineering knowledge + the research notes [K].
Nothing here has run on Windows.

## Defects fixed
1. **auto_id lookup always raised** (uia.py): pywinauto 0.6.8 `UIAElementInfo.descendants()` -> `build_condition(process, class_name, title, control_type, content_only)` has no `auto_id`; TypeError became HarnessError for every AutomationId selector. Now filtered client-side. Unit-test fake made strict to match.
2. Qt < 6.8 reports AutomationId as a dotted objectName path; suffix match added. Qt >= 6.8 returns `setAccessibleIdentifier` text verbatim (qtbase `accessibleId()`), which the fixture sets.
3. `ComboBoxWrapper.item_texts()` does not exist in the UIA backend (it is `texts()`); options were always empty.
4. ctypes: no argtypes/restype anywhere -> 64-bit HANDLE/HWND/HDC truncation. All entry points typed via lazy `_api()` (WinDLL use_last_error=True).
5. `SendMessageTimeoutW` used SMTO_ABORTIFHUNG only and treated any failure as hang; now ABORTIFHUNG|BLOCK, hang only if GetLastError in (0, ERROR_TIMEOUT).
6. `GetDIBits` was called with the bitmap still selected into the DC (invalid); fixed. Hung windows: PrintWindow would block, so a BitBlt screen copy is used instead.
7. Window/hang/modal checks looked only at the root pid; now the descendant process tree (Toolhelp snapshot). Launcher shims (venv python.exe) are adopted by first descendant owning a window.
8. Launch: pywinauto `Application.start` (no flags, no job). Replaced by `procwatch.launch` (CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW, NUL std handles, close_fds, best-effort KILL_ON_JOB_CLOSE job; `detach(kill=False)` clears the flag first so the target survives).
9. `kill_tree` snapshots descendants before taskkill /T (children are unfindable after the root dies), then TerminateProcess leftovers, then closes the job.
10. DPI: no process DPI awareness was set; `set_dpi_aware()` (PER_MONITOR_AWARE_V2 -> shcore -> SetProcessDPIAware) is called in `attach` before pywinauto import.
11. Stale-wrapper cache check swallowed COM errors (always "fresh"); now reads process_id directly.
12. Fixture: `segv` mode could not crash on Windows (ctypes converts access violations to OSError) -> RaiseException(0xC0000409); MessageBoxW now typed (HWND > 2^31 would overflow c_int).
13. Smoke test rewritten: Name fallback selectors, polling for Qt's lazy UIA tree, diag dump (UIA tree, tasklist, session/screen, screenshot), faulthandler hard timeout, leaked-process sweep, nonzero exit.
14. Packaging: `packaging/overgrind.spec` (collect_submodules overgrind/pywinauto, collect_all comtypes/win32com, win32 hidden imports; CLI excludes PySide6). Workflow: timeout-minutes 30, pip cache, UTF-8, pwsh exit-code checks, diagnostics artifact on failure, desktop probe step.

## Verified against pywinauto 0.6.8 source
`iface_{toggle,range_value,value,selection_item,expand_collapse,invoke,window}` (raise NoPatternInterfaceError -> None), `element_info.{automation_id,class_name,control_type,process_id,rectangle,handle,name}`, `ComboBoxWrapper.{select,selected_text,texts}`, `Desktop(...).windows(process=)`, `children()`, `descendants()`. pywinauto imports initialise COM as MTA (pythoncom probe) and requires pywin32 + comtypes; no DPI handling.

## Unverified / risks for first real Windows CI run
- Interactive desktop on windows-latest is unconfirmed; UIA may see nothing (check smoke-diag/*-env.txt: session id, screen size).
- Qt UIA pattern exposure (Toggle for QCheckBox, RangeValue for QSlider/QSpinBox, Invoke for QPushButton) is assumed from Qt's provider; Qt builds the UIA tree lazily, first queries may be empty.
- pywinauto 0.6.8 + comtypes latest + Python 3.12 compatibility not exercised; comtypes.gen wrapper generation in the frozen exe is the classic PyInstaller failure. The frozen smoke does not start a worker; an adapter string-import failure would only show at run time.
- Job assignment fails silently (taskkill fallback) if Python runs in a restrictive job; assignment happens after CreateProcess (no CREATE_SUSPENDED), so a very early grandchild may escape the job.
- UWP/ApplicationFrameHost windows are not in the target's process tree (not handled). Elevation check returns None (no error) when the target token cannot be opened.
- `is_alive` treats exit code 259 as alive; PID reuse is not guarded.
- WerFault "app stopped working" UI could keep a crashed fixture alive in `segv` mode (not used by the smoke).
