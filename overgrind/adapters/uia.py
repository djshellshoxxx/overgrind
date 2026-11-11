"""UiaAdapter: the v2 Windows UI Automation target adapter (pywinauto, backend='uia').

Windows only; every Windows/pywinauto import is lazy so the module imports on Linux and the
pure logic can be unit-tested with fake wrappers.

Safety rules enforced here:
* `inspect()` is read-only (no clicks, no expand/collapse unless `read_combo_options=True`).
* Every wrapper is checked to belong to the attached PID before anything is sent to it; the
  keyboard fallback additionally verifies the foreground window belongs to that PID.
* Features flagged `dangerous` are never touched.
* Elevation mismatch (target elevated, we are not) raises a clear HarnessError at attach.
* Patterns first (Toggle, RangeValue, Value, Selection, ExpandCollapse, Invoke); keyboard input
  only when the needed pattern is missing.
"""
from __future__ import annotations

import subprocess
import sys
import time
from typing import Any

from ..types import (
    ControlDef, Domain, ElementInfo, FeatureDef, HarnessError, Health, Project, Selector,
    TargetRef, Value,
)
from . import procwatch, resolve

MAX_ELEMENTS = 3000
MAX_DEPTH = 14
_ON = ("on", "true", "1", "checked", "yes")
_KEY_SPECIAL = "{}+^%~()[]"


def _escape_keys(text: str) -> str:
    return "".join("{" + ch + "}" if ch in _KEY_SPECIAL else ch for ch in text)


def _pattern(w: Any, name: str) -> Any:
    """UIA pattern interface of a pywinauto wrapper, or None if unsupported."""
    try:
        return getattr(w, "iface_" + name)
    except Exception:
        return None


def _info(w: Any) -> Any:
    return getattr(w, "element_info", None)


def _attr(obj: Any, name: str, default: Any = "") -> Any:
    try:
        v = getattr(obj, name)
        v = v() if callable(v) else v
        return default if v is None else v
    except Exception:
        return default


def _num(x: Any) -> int | float:
    f = float(x)
    return int(f) if f == int(f) and abs(f) < 1e15 else f


class UiaAdapter:
    def __init__(self, launch_timeout_s: float = 30.0, read_combo_options: bool = False,
                 backend: str = "uia") -> None:
        self.launch_timeout_s = launch_timeout_s
        self.read_combo_options = read_combo_options
        self.backend = backend
        self._target: TargetRef | None = None
        self._pid: int | None = None
        self._root_pid: int | None = None     # launched process (may differ from _pid for launcher shims)
        self._win: Any = None
        self._cache: dict[Selector, Any] = {}
        self._elements: list[ElementInfo] = []
        self._wrappers: list[Any] = []

    # ------------------------------------------------------------ attach / detach
    def _require_windows(self) -> None:
        if sys.platform != "win32":
            raise HarnessError("UiaAdapter requires Windows (use adapters.mock:MockAdapter elsewhere)")

    def attach(self, target: TargetRef) -> None:
        self._require_windows()
        if self._pid and self._target == target and procwatch.is_alive(self._pid) and self._win is not None:
            return
        # DPI awareness must be set BEFORE pywinauto/comtypes read any geometry, otherwise UIA
        # rectangles and screenshots are virtualised (wrong) on scaled displays. COM itself is
        # initialised by pywinauto's import (MTA via pythoncom; comtypes follows sys.coinit_flags)
        # on the worker's main thread, which is the only thread that calls into it.
        procwatch.set_dpi_aware()
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise HarnessError("pywinauto is not installed (pip install overgrind[windows])") from exc
        self._target, self._cache = target, {}
        try:
            if target.kind == "exe" and target.exe_path:
                # Own launcher (not pywinauto's Application.start): new process group, no inherited
                # handles, kill-on-close Job Object; see procwatch.launch. The process id is all
                # pywinauto needs afterwards (Desktop(...).windows(process=pid)).
                self._pid = procwatch.launch([target.exe_path, *target.args])
                self._root_pid = self._pid
            elif target.pid:
                self._pid = self._root_pid = int(target.pid)
            else:
                rx = target.title_regex or target.process_name
                if not rx:
                    raise HarnessError("target has no exe, pid, process name or title regex")
                w = Desktop(backend=self.backend).window(title_re=rx).wrapper_object() \
                    if target.title_regex else None
                if w is not None:
                    self._pid = int(_attr(_info(w), "process_id", 0)) or None
                else:
                    self._pid = self._pid_by_name(target.process_name)
        except HarnessError:
            raise
        except Exception as exc:
            raise HarnessError(f"cannot attach to target: {type(exc).__name__}: {exc}") from exc
        if not self._pid:
            raise HarnessError("could not determine the target process id")
        self._check_elevation()
        self._win = self._wait_main_window(Desktop)

    @staticmethod
    def _pid_by_name(name: str) -> int | None:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) > 1 and parts[0].strip('"').lower() == name.lower():
                return int(parts[1].strip('"'))
        return None

    def _check_elevation(self) -> None:
        ours, theirs = procwatch.is_elevated(), procwatch.is_elevated(self._pid)
        if theirs and not ours:
            raise HarnessError(
                f"target (pid {self._pid}) runs elevated but Overgrind does not; Windows blocks UI "
                "automation across this integrity boundary (UIPI). Restart Overgrind as "
                "administrator, or start the target without elevation.")

    def _wait_main_window(self, Desktop: Any) -> Any:
        deadline = time.monotonic() + self.launch_timeout_s
        rx = self._target.title_regex if self._target else ""
        while time.monotonic() < deadline:
            if not procwatch.is_alive(self._pid):
                raise HarnessError(f"target process {self._pid} exited during launch")
            try:
                wins = Desktop(backend=self.backend).windows(process=self._pid)
                if not wins and self._root_pid == self._pid:
                    # Launcher shims (venv python.exe, Electron, wrappers) own no window themselves;
                    # adopt the first descendant process that does and treat it as the target pid.
                    for child in procwatch.descendant_pids(self._pid):
                        wins = Desktop(backend=self.backend).windows(process=child)
                        if wins:
                            self._pid = child
                            break
            except Exception:
                wins = []
            wins = [w for w in wins if _attr(w, "is_visible", True)]
            if rx:
                import re
                wins = [w for w in wins if re.search(rx, _attr(w, "window_text", "") or "")] or wins
            if wins:
                return wins[0]
            time.sleep(0.2)
        raise HarnessError(f"no window for pid {self._pid} within {self.launch_timeout_s:g}s")

    def detach(self, kill: bool = False) -> None:
        pid, root = self._pid, self._root_pid or self._pid
        if kill and root:
            procwatch.kill_tree(root)
        elif root:
            procwatch.release_job(root)       # leave the target running even if we die later
        self._win, self._cache, self._pid, self._root_pid = None, {}, None, None
        self._elements, self._wrappers = [], []

    def pid(self) -> int | None:
        return self._pid

    def _hwnd(self) -> int | None:
        h = _attr(_info(self._win), "handle", None) if self._win is not None else None
        return int(h) if h else None

    # ------------------------------------------------------------ element location
    def _assert_pid(self, w: Any) -> None:
        pid = _attr(_info(w), "process_id", None)
        if pid is not None and self._pid is not None and int(pid) != int(self._pid):
            raise HarnessError(f"refusing to send input to pid {pid}: not the attached target ({self._pid})")

    def _alive_or_raise(self) -> None:
        if self._win is None or not self._pid:
            raise HarnessError("not attached")
        if not procwatch.is_alive(self._pid):
            raise HarnessError(f"target process {self._pid} is gone")

    def _find(self, sel: Selector) -> Any:
        self._alive_or_raise()
        w = self._cache.get(sel)
        if w is not None:
            try:
                # direct (non-swallowing) read: raises if the UIA element went stale
                if int(w.element_info.process_id) == self._pid:
                    return w
            except Exception:
                pass
            self._cache.pop(sel, None)
        w = self._find_fresh(sel)
        self._assert_pid(w)
        self._cache[sel] = w
        return w

    def _descendants(self, **crit: Any) -> list[Any]:
        """Descendants matching title/control_type/class_name (server-side UIA conditions) and
        auto_id (client-side: pywinauto 0.6.8's UIA descendants() has NO auto_id keyword and raises
        TypeError for it)."""
        crit = {k: v for k, v in crit.items() if v not in ("", None)}
        auto_id = crit.pop("auto_id", None)
        try:
            hits = list(self._win.descendants(**crit))
        except Exception as exc:
            raise HarnessError(f"UI Automation query failed: {type(exc).__name__}: {exc}") from exc
        if auto_id is not None:
            # Qt < 6.8 reports a dotted path ("main_window.cb_a") as AutomationId: accept the suffix.
            hits = [h for h in hits
                    if (lambda a: a == auto_id or a.endswith("." + auto_id))(str(_attr(_info(h), "automation_id", "")))]
        return hits

    def _find_fresh(self, sel: Selector) -> Any:
        tiers = []
        if sel.auto_id:
            tiers.append(dict(auto_id=sel.auto_id, control_type=sel.control_type))
        if sel.title and sel.control_type:
            tiers.append(dict(title=sel.title, control_type=sel.control_type))
        for crit in tiers:
            hits = self._descendants(**crit)
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1 and sel.index is not None and sel.index < len(hits):
                return hits[sel.index]
        # self-healing: snapshot + ordered tiers (path, class+index)
        infos, wrappers = self._walk()
        res = resolve.resolve(sel, infos)
        if res.found:
            return wrappers[res.index]
        raise HarnessError("element not found / ambiguous: " + resolve.explain(sel, infos))

    def selector_enabled(self, sel: Selector) -> bool | None:
        try:
            return bool(self._find(sel).is_enabled())
        except HarnessError:
            raise
        except Exception:
            return None

    # ------------------------------------------------------------ inspect (read-only)
    def _walk(self) -> tuple[list[ElementInfo], list[Any]]:
        self._alive_or_raise()
        infos: list[ElementInfo] = []
        wrappers: list[Any] = []
        stack: list[tuple[Any, int, int | None, tuple[str, ...]]] = [(self._win, 0, None, ())]
        # iterative pre-order DFS (children pushed reversed so document order is preserved)
        while stack and len(infos) < MAX_ELEMENTS:
            w, depth, parent, path = stack.pop()
            idx = len(infos)
            me = self._element_info(w, depth, parent, path)
            infos.append(me)
            wrappers.append(w)
            if depth >= MAX_DEPTH:
                continue
            try:
                kids = w.children()
            except Exception:
                kids = []
            child_path = path + (me.name or me.selector.auto_id,)
            for k in reversed(kids):
                stack.append((k, depth + 1, idx, child_path))
        self._elements, self._wrappers = infos, wrappers
        return infos, wrappers

    def inspect(self) -> list[ElementInfo]:
        return list(self._walk()[0])

    def _patterns(self, w: Any) -> tuple[str, ...]:
        names = (("Toggle", "toggle"), ("RangeValue", "range_value"), ("Value", "value"),
                 ("Selection", "selection"), ("SelectionItem", "selection_item"),
                 ("ExpandCollapse", "expand_collapse"), ("Invoke", "invoke"), ("Window", "window"))
        return tuple(label for label, attr in names if _pattern(w, attr) is not None)

    def _element_info(self, w: Any, depth: int, parent: int | None, path: tuple[str, ...]) -> ElementInfo:
        ei = _info(w)
        name = str(_attr(ei, "name", ""))
        ctype = str(_attr(ei, "control_type", ""))
        sel = Selector(auto_id=str(_attr(ei, "automation_id", "") or _attr(ei, "auto_id", "")),
                       title=name, control_type=ctype, class_name=str(_attr(ei, "class_name", "")),
                       path=path)
        rect = _attr(ei, "rectangle", None)
        bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)) if rect is not None else (0, 0, 0, 0)
        pats = self._patterns(w)
        kind, dom, conf, value = self._suggest(w, ctype, pats)
        return ElementInfo(sel, name, ctype, depth, parent, bounds, value, pats, kind, dom, conf)

    def _suggest(self, w: Any, ctype: str, pats: tuple[str, ...]) -> tuple[str, Domain | None, str, Value]:
        """(kind, domain, confidence, value). 'verified' only when Toggle/RangeValue/ExpandCollapse
        data was actually read from the element."""
        try:
            if "Toggle" in pats:
                st = int(_pattern(w, "toggle").CurrentToggleState)
                return "boolean", Domain("boolean"), "verified", st == 1
            if "RangeValue" in pats:
                rv = _pattern(w, "range_value")
                lo, hi, cur = rv.CurrentMinimum, rv.CurrentMaximum, rv.CurrentValue
                step = rv.CurrentSmallChange or None
                vals = [x for x in (lo, hi, cur, step) if x is not None]
                integral = all(float(x) == int(float(x)) for x in vals)
                dom = Domain("integer" if integral else "float", _num(lo) if integral else float(lo),
                             _num(hi) if integral else float(hi), (_num(step) if integral else float(step)) if step else None)
                return dom.kind, dom, "verified", _num(cur) if integral else float(cur)
            if "ExpandCollapse" in pats and ctype == "ComboBox":
                state = _pattern(w, "expand_collapse").CurrentExpandCollapseState
                cur = self._value_text(w)
                opts: tuple[str, ...] = ()
                if self.read_combo_options:
                    try:
                        opts = tuple(str(t) for t in w.texts())   # UIA ComboBoxWrapper has texts(), not item_texts()
                    except Exception:
                        opts = ()
                _ = state  # state was read: pattern is live
                return "choice", Domain("choice", options=opts), "verified" if opts else "inferred", cur
            if "Value" in pats and ctype in ("Edit", "Document", "Text"):
                return "text", Domain("text"), "inferred", self._value_text(w)
            if ctype in ("RadioButton", "TabItem") and "SelectionItem" in pats:
                sel = bool(_pattern(w, "selection_item").CurrentIsSelected)
                return "boolean", Domain("boolean"), "inferred", sel
            if ctype in ("Button", "Hyperlink", "MenuItem", "SplitButton") or "Invoke" in pats:
                return "action", None, "inferred", None
        except Exception:
            return "", None, "unknown", None
        return "", None, "unknown", None

    @staticmethod
    def _value_text(w: Any) -> str:
        v = _pattern(w, "value")
        try:
            if v is not None:
                return str(v.CurrentValue)
            return str(w.window_text())
        except Exception:
            return ""

    # ------------------------------------------------------------ writes
    def _toggle_state(self, w: Any) -> bool | None:
        t = _pattern(w, "toggle")
        if t is not None:
            return int(t.CurrentToggleState) == 1
        si = _pattern(w, "selection_item")
        if si is not None:
            return bool(si.CurrentIsSelected)
        return None

    def _set_toggle(self, w: Any, want_on: bool) -> None:
        self._assert_pid(w)
        t = _pattern(w, "toggle")
        if t is not None:
            for _ in range(3):                       # tri-state boxes need up to 2 toggles
                cur = int(t.CurrentToggleState) == 1
                if cur == want_on:
                    return
                t.Toggle()
            if (int(t.CurrentToggleState) == 1) != want_on:
                raise HarnessError("toggle read-back mismatch")
            return
        si = _pattern(w, "selection_item")
        if si is not None:
            if want_on:
                si.Select()
                if not si.CurrentIsSelected:
                    raise HarnessError("selection read-back mismatch")
                return
            if not si.CurrentIsSelected:
                return
            raise HarnessError("cannot deselect a radio/tab style element; add per-state selectors")
        raise HarnessError("element supports neither Toggle nor SelectionItem")

    @staticmethod
    def _is_on_state(feature: FeatureDef, state: str) -> bool:
        if len(feature.states) >= 2:
            return state == feature.states[1]
        return str(state).strip().lower() in _ON

    def set_feature_state(self, feature: FeatureDef, state: str) -> None:
        if feature.dangerous:
            raise HarnessError(f"feature {feature.id!r} is flagged dangerous; refusing to touch it")
        if feature.state_selectors and state in feature.state_selectors:
            w = self._find(feature.state_selectors[state])
            self._set_toggle(w, True)
            return
        w = self._find(feature.selector)
        if feature.kind == "toggle":
            self._set_toggle(w, self._is_on_state(feature, state))
        else:
            self._select_text(w, state)

    def get_feature_state(self, feature: FeatureDef) -> str:
        if feature.state_selectors:
            for st, sel in feature.state_selectors.items():
                if self._toggle_state(self._find(sel)):
                    return st
            return feature.default_state
        w = self._find(feature.selector)
        if feature.kind == "toggle":
            on = self._toggle_state(w)
            if on is None:
                raise HarnessError("cannot read toggle state")
            return feature.states[1] if on else feature.states[0]
        return self._value_text(w)

    def invoke(self, feature: FeatureDef) -> None:
        if feature.dangerous:
            raise HarnessError(f"feature {feature.id!r} is flagged dangerous; refusing to invoke it")
        w = self._find(feature.selector)
        inv = _pattern(w, "invoke")
        if inv is not None:
            inv.Invoke()
            return
        self._keyboard(w, "{ENTER}")

    def _select_text(self, w: Any, text: str) -> None:
        self._assert_pid(w)
        try:
            w.select(text)
        except Exception as exc:
            # list-style containers: select the matching child item via SelectionItem
            for k in self._safe_children(w):
                if str(_attr(_info(k), "name", "")) == text and _pattern(k, "selection_item") is not None:
                    self._assert_pid(k)
                    _pattern(k, "selection_item").Select()
                    return
            raise HarnessError(f"cannot select {text!r}: {type(exc).__name__}: {exc}") from exc
        got = self._read_choice(w)
        if got is not None and got != text:
            raise HarnessError(f"selection read-back mismatch (wanted {text!r}, got {got!r})")

    @staticmethod
    def _safe_children(w: Any) -> list[Any]:
        try:
            return list(w.children())
        except Exception:
            return []

    def _read_choice(self, w: Any) -> str | None:
        try:
            return str(w.selected_text())
        except Exception:
            v = _pattern(w, "value")
            try:
                return str(v.CurrentValue) if v is not None else None
            except Exception:
                return None

    def _keyboard(self, w: Any, keys: str) -> None:
        """Fallback when no pattern exists. Never types into another process."""
        self._assert_pid(w)
        try:
            w.set_focus()
        except Exception as exc:
            raise HarnessError(f"cannot focus element: {exc}") from exc
        fg = procwatch.foreground_pid()
        if fg is not None and fg != self._pid:
            raise HarnessError(f"foreground window belongs to pid {fg}, not the target ({self._pid}); "
                               "refusing to send keys")
        w.type_keys(keys, with_spaces=True, set_foreground=False)

    def set_control(self, control: ControlDef, value: Value) -> None:
        w = self._find(control.selector)
        self._assert_pid(w)
        if not _attr(w, "is_enabled", True):
            raise HarnessError(f"control {control.id!r} is disabled")
        k = control.kind
        if k == "boolean":
            self._set_toggle(w, bool(value))
        elif k in ("integer", "float"):
            self._set_number(w, control, value)
        elif k == "text":
            self._set_text(w, str(value))
        elif k == "choice":
            self._select_text(w, str(value))
        else:
            raise HarnessError(f"unsupported control kind {k!r}")

    def _set_number(self, w: Any, control: ControlDef, value: Value) -> None:
        rv = _pattern(w, "range_value")
        if rv is not None:
            rv.SetValue(float(value))
            got = float(rv.CurrentValue)
            tol = max(float(control.domain.step or 0) / 2.0, 1e-9 * max(1.0, abs(float(value))))
            if abs(got - float(value)) > tol:
                raise HarnessError(f"RangeValue read-back mismatch (set {value}, read {got})")
            return
        if _pattern(w, "value") is not None:
            self._set_text(w, str(value))
            return
        self._keyboard(w, "^a" + _escape_keys(str(value)) + "{TAB}")

    def _set_text(self, w: Any, text: str) -> None:
        v = _pattern(w, "value")
        if v is not None:
            if v.CurrentIsReadOnly:
                raise HarnessError("value is read-only")
            v.SetValue(text)
            if str(v.CurrentValue) != text:
                raise HarnessError("Value read-back mismatch")
            return
        self._keyboard(w, "^a" + (_escape_keys(text) if text else "{DELETE}"))

    def get_control(self, control: ControlDef) -> Value:
        w = self._find(control.selector)
        k = control.kind
        if k == "boolean":
            on = self._toggle_state(w)
            if on is None:
                raise HarnessError("cannot read toggle state")
            return on
        if k in ("integer", "float"):
            rv = _pattern(w, "range_value")
            raw = rv.CurrentValue if rv is not None else self._value_text(w)
            try:
                return _num(raw) if k == "integer" else float(raw)
            except (TypeError, ValueError) as exc:
                raise HarnessError(f"cannot parse numeric value {raw!r}") from exc
        if k == "choice":
            return self._read_choice(w) or ""
        return self._value_text(w)

    # ------------------------------------------------------------ reset / health
    def reset(self, project: Project) -> None:
        self._alive_or_raise()
        self.dismiss_modals()
        for f in project.features:
            if f.kind == "action" or f.dangerous or not f.enabled:
                continue
            self.set_feature_state(f, f.default_state)
        for c in project.controls:
            if c.skip or c.read_only or c.default is None:
                continue
            w = self._find(c.selector)
            if not _attr(w, "is_enabled", True):
                continue          # prerequisite feature off: nothing to restore
            self.set_control(c, c.default)

    def dismiss_modals(self) -> int:
        """Close modal dialogs owned by the target via WM_CLOSE-equivalent (Window pattern)."""
        n = 0
        for hwnd, _title in procwatch.modal_dialogs(self._pid or 0, self._hwnd()):
            try:
                from pywinauto import Desktop
                w = Desktop(backend=self.backend).window(handle=hwnd).wrapper_object()
                self._assert_pid(w)
                w.close()
                n += 1
            except Exception:
                continue
        return n

    def wait_quiescent(self, timeout_ms: int = 1000) -> None:
        deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
        calm = 0
        while time.monotonic() < deadline and calm < 2:
            if not procwatch.is_alive(self._pid):
                return
            hwnd = self._hwnd()
            hung = procwatch.is_hung(hwnd, timeout_ms=100) if hwnd else False
            calm = 0 if hung else calm + 1
            time.sleep(0.01)

    def health(self) -> Health:
        pid = self._pid
        if not pid or self._win is None:
            return Health("missing", "not attached")
        if not procwatch.is_alive(pid):
            code = procwatch.exit_code(pid)
            return Health("dead", f"process exited (code {code})" if code is not None else "process exited")
        hwnd = self._hwnd()
        if procwatch.is_hung(pid, pid=True, timeout_ms=500):
            return Health("hung", "window is not responding (SendMessageTimeout/IsHungAppWindow)")
        dialogs = procwatch.modal_dialogs(pid, hwnd)
        if dialogs:
            return Health("modal", f"modal dialog: {dialogs[0][1] or '(untitled)'}")
        if hwnd and not procwatch.window_exists(hwnd):
            return Health("missing", "main window no longer exists")
        return Health("ok")

    def screenshot(self, path: str) -> bool:
        if self._win is None:
            return False
        try:
            self._win.capture_as_image().save(path)       # needs Pillow
            return True
        except Exception:
            hwnd = self._hwnd()
            return bool(hwnd) and procwatch.print_window_bmp(hwnd, path)
