# Fixtures

## `synthetic_app.py` - PySide6 target with deliberate defects

A small Qt widgets app that Overgrind drives through UI Automation. Windows CI uses it to prove
end to end that crashes, hangs and modal dialogs are detected, deduplicated and survived.

| Widget (AutomationId) | Notes |
|---|---|
| `cb_a` .. `cb_d` | checkboxes; `cb_c` / `cb_d` are mutually exclusive (the app unchecks the other) |
| `cmb_mode` | combo: Off / Low / High |
| `sld_volume` 0..100, `sld_gain` 0..10, `spn_count` 0..50 | RangeValue controls |
| `edt_name` | line edit, max 20 chars (Value pattern) |
| `btn_apply` | harmless; `btn_delete` is a decoy "dangerous" button (mark the feature `dangerous`) |
| `btn_crash`, `btn_hang`, `btn_modal` | fault on click (30 ms after the click so the UIA call returns) |

Deliberate faults (command line, or env `OVERGRIND_BUG="SPEC;SPEC"`):

```
--crash-after-ms N  --hang-after-ms N  --modal-after-ms N  --quit-after-ms N
--crash-mode exit|segv                       exit = os._exit(3); segv = null dereference
--bug combo:ab[:crash|hang|modal]            faults when checkboxes a AND b are both checked
--bug value:volume>=90[:crash|hang|modal]    faults when volume/gain/count satisfies the test
```

Run headless on Linux (no UIA, only to check the app itself): `QT_QPA_PLATFORM=offscreen python fixtures/synthetic_app.py --quit-after-ms 500`.

## How CI uses it (Windows runner, interactive desktop)

`pip install pywinauto PySide6-Essentials` then

```
python -m unittest tests.windows_smoke -v
```

`tests/windows_smoke.py` is skipped on non-Windows. It launches the app through `UiaAdapter`
(`TargetRef(kind="exe", exe_path=sys.executable, args=("fixtures/synthetic_app.py", ...))`) and checks:

1. `inspect()` finds the checkboxes/sliders with `verified` confidence and no side effects.
2. Toggle / RangeValue / combo writes read back; `reset()` restores defaults.
3. `OVERGRIND_BUG=combo:ab` + a `RunController` run (`restart_on_crash=True`) yields exactly ONE
   deduplicated `crash` event, the run keeps going after restarting the target.
4. `--bug combo:ab:hang` yields a `hang`; `--bug combo:ab:modal` yields a `dialog` event.
5. A wedged worker is killed by the watchdog and the run continues.

Notes: the desktop must be unlocked and interactive; run the target and Overgrind at the same
integrity level (UIPI). Frozen (PyInstaller) builds must call `multiprocessing.freeze_support()`
first thing in their entry point because workers are `spawn`ed.
