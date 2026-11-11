"""Deterministic in-memory target for tests, the GUI inspector demo and CI on Linux.

`MockAdapter` implements the whole `TargetAdapter` protocol. Failures are pure functions of
(seed, applied feature state, applied control values), so replay and reduction reproduce them:

* `bad_combo`   - dict feature -> state; the target crashes whenever ALL listed states are applied
                  (monotone, so ddmin can shrink a failing case down to exactly this combo).
* `crash_rate`, `hang_rate`, `dialog_rate`, `wedge_rate` - hash-based, evaluated once per case in
                  `wait_quiescent` (the end of a case). `wedge_rate` makes the adapter block forever
                  (simulates a stuck UIA call that only the watchdog can resolve).
* `harness_rate` - an operation raises HarnessError (automation noise, never an app defect).

`simple_generator` is a tiny deterministic, checkpointable CaseGenerator used when the real
scheduler is not wanted (tests, `generator_factory="overgrind.adapters.mock:simple_generator"`).
"""
from __future__ import annotations

import struct
import time
import zlib
from typing import Any, Iterator

from ..types import (
    Case, ControlDef, Coverage, Domain, ElementInfo, FeatureDef, HarnessError, Health,
    Outcome, Project, Selector, TargetRef, Value,
)

def _png(w: int = 160, h: int = 120, rgb: tuple[int, int, int] = (235, 235, 240)) -> bytes:
    """A small, strictly valid 8-bit RGB PNG (signature, IHDR, one IDAT, IEND, correct CRCs), QImage-free.

    Light background with a darker 2 px frame and a diagonal so a viewer can tell it rendered."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    dark = (90, 100, 130)
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            frame = x < 2 or y < 2 or x >= w - 2 or y >= h - 2
            diag = abs(x * h - y * w) < w            # thin diagonal
            row += bytes(dark if (frame or diag) else rgb)
        rows.append(b"\x00" + bytes(row))         # filter type 0 (None) per scanline
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


_PNG = _png()
_OFF = ("off", "false", "0", "none", "")


def _u(*parts: Any) -> float:
    """Deterministic uniform [0,1) from the parts."""
    h = zlib.crc32("|".join(map(str, parts)).encode("utf-8")) & 0xFFFFFFFF
    h = (h * 2654435761 + 0x9E3779B9) & 0xFFFFFFFF
    return h / 4294967296.0


class MockAdapter:
    def __init__(self, crash_rate: float = 0.0, hang_rate: float = 0.0,
                 bad_combo: dict[str, str] | None = None, dialog_rate: float = 0.0,
                 harness_rate: float = 0.0, seed: int = 0, latency_ms: float = 0.0,
                 wedge_rate: float = 0.0, fake_pid: int | None = None,
                 enable_rules: dict[str, list[str]] | None = None,
                 disable_when: dict[str, list[str]] | None = None,
                 mutex_groups: list[list[str]] | None = None,
                 requires_rules: dict[str, list[str]] | None = None,
                 mutex_leak: float = 0.0, read_noise: float = 0.0, enable_delay_ms: float = 0.0,
                 clamp: dict[str, tuple[float, float]] | None = None,
                 ignore_writes: list[str] | None = None, forget_on_reset: list[str] | None = None,
                 element_labels: dict[str, str] | None = None, password_controls: list[str] | None = None) -> None:
        self.crash_rate, self.hang_rate = float(crash_rate), float(hang_rate)
        self.dialog_rate, self.harness_rate = float(dialog_rate), float(harness_rate)
        self.wedge_rate = float(wedge_rate)
        self.bad_combo = dict(bad_combo) if bad_combo else None
        self.seed, self.latency_ms = int(seed), float(latency_ms)
        self._fake_pid = fake_pid
        self._states: dict[str, str] = {}
        self._values: dict[str, Value] = {}
        self._attached = False
        self._dead = ""
        self._hung = ""
        self._modal = ""
        self._dirty = False
        # --- additive planted behaviour for U2/U4 tests (all default off; nothing changes for old callers)
        self.enable_rules = {k: list(v) for k, v in (enable_rules or {}).items()}      # control -> features ALL active
        self.disable_when = {k: list(v) for k, v in (disable_when or {}).items()}      # control disabled when ANY active
        self.mutex_groups = [list(g) for g in (mutex_groups or [])]
        self.requires_rules = {k: list(v) for k, v in (requires_rules or {}).items()}
        self.mutex_leak, self.read_noise, self.enable_delay_ms = float(mutex_leak), float(read_noise), float(enable_delay_ms)
        self.clamp = {k: (float(a), float(b)) for k, (a, b) in (clamp or {}).items()}
        self.ignore_writes, self.forget_on_reset = set(ignore_writes or ()), set(forget_on_reset or ())
        self.element_labels = dict(element_labels or {})        # feature id -> label read by element_text()
        self.password_controls = set(password_controls or ())
        self.calls: list[tuple] = []                            # (op, target[, value]) log for tests
        self._project: Project | None = None
        self._vt = 0.0                                          # virtual ms advanced by wait_quiescent
        self._hist: list[tuple[float, dict[str, str]]] = []
        self._reads = 0
        self._sets = 0
        self._rates = bool(self.crash_rate or self.hang_rate or self.dialog_rate or self.wedge_rate)

    # ------------------------------------------------------------ lifecycle
    def attach(self, target: TargetRef | None = None) -> None:
        self._attached = True
        self._dead = self._hung = self._modal = ""
        self._states, self._values = {}, {}
        self._dirty = False
        self._hist = []

    def detach(self, kill: bool = False) -> None:
        self._attached = False
        if kill:
            self._dead = self._hung = self._modal = ""

    def pid(self) -> int | None:
        return self._fake_pid if self._attached else None

    # ------------------------------------------------------------ helpers
    def _op(self, kind: str, target: str, value: Any = None) -> None:
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)
        if not self._attached:
            raise HarnessError("mock: not attached")
        if self._dead:
            raise HarnessError("mock: target process is gone")
        if self._hung:
            raise HarnessError("mock: operation timed out (target not responding)")
        if self.harness_rate:
            if _u(self.seed, "h", kind, target, value, sorted(self._states.items())) < self.harness_rate:
                raise HarnessError(f"mock: automation failed on {target}")

    def _evaluate(self, final: bool) -> None:
        if self._dead or self._hung:
            return
        if self.bad_combo and all(self._states.get(k) == v for k, v in self.bad_combo.items()):
            self._dead = "mock crash: bad_combo"
            return
        if final and self._rates:
            u = _u(self.seed, "c", sorted(self._states.items()), sorted(self._values.items()))
            lim = self.crash_rate
            if u < lim:
                self._dead = "mock crash: access violation"
                return
            lim += self.hang_rate
            if u < lim:
                self._hung = "mock hang: not responding"
                return
            lim += self.dialog_rate
            if u < lim:
                self._modal = "mock modal: unexpected error dialog"
                return
            lim += self.wedge_rate
            if u < lim:
                while True:      # stuck driver call; only the watchdog gets us out
                    time.sleep(3600)

    # ------------------------------------------------------------ TargetAdapter
    def set_feature_state(self, feature: FeatureDef, state: str) -> None:
        self._op("state", feature.id, state)
        self._log(("state", feature.id, state))
        if self.requires_rules or self.mutex_groups:
            if not self._apply_state_rules(feature, state):
                return
        self._states[feature.id] = state
        self._snap()
        self._dirty = True

    def _log(self, entry: tuple) -> None:
        self.calls.append(entry)
        if len(self.calls) > 4000:          # bounded memory for long runs
            del self.calls[:2000]

    def _is_active(self, fid: str, states: dict[str, str] | None = None) -> bool:
        st = self._states if states is None else states
        f = next((x for x in (self._project.features if self._project else ()) if x.id == fid), None)
        cur = st.get(fid, f.default_state if f else "off")
        first = f.states[0] if f and f.states else "off"
        return cur != first

    def _apply_state_rules(self, feature: FeatureDef, state: str) -> bool:
        """Planted requires/mutex behaviour (U4 tests). False = the write is ignored by the 'app'."""
        active = state != (feature.states[0] if feature.states else "off")
        if active:
            for need in self.requires_rules.get(feature.id, ()):
                if not self._is_active(need):
                    return False
            self._sets += 1
            leak = self.mutex_leak and _u(self.seed, "leak", self._sets, feature.id) < self.mutex_leak
            if not leak:
                for grp in self.mutex_groups:
                    if feature.id in grp:
                        for other in grp:
                            if other != feature.id and self._is_active(other):
                                first = next((x.states[0] for x in self._project.features if x.id == other), "off") \
                                    if self._project else "off"
                                self._states[other] = first
        return True

    def _snap(self) -> None:
        self._hist.append((self._vt, dict(self._states)))
        if len(self._hist) > 64:
            del self._hist[:-64]

    def invoke(self, feature: FeatureDef) -> None:
        self._log(("invoke", feature.id))
        self._op("invoke", feature.id)
        if feature.dangerous:
            raise HarnessError("mock: refusing to invoke a dangerous feature")
        self._dirty = True

    def set_control(self, control: ControlDef, value: Value) -> None:
        self._op("control", control.id, value)
        self._log(("control", control.id, value))
        if control.id in self.ignore_writes:
            return
        if control.id in self.clamp and isinstance(value, (int, float)) and not isinstance(value, bool):
            lo, hi = self.clamp[control.id]
            value = type(value)(min(max(value, lo), hi)) if isinstance(value, int) else min(max(value, lo), hi)
        self._values[control.id] = value
        self._dirty = True

    def get_control(self, control: ControlDef) -> Value:
        if not self._attached or self._dead:
            raise HarnessError("mock: target not available")
        return self._values.get(control.id, control.default)

    def get_feature_state(self, feature: FeatureDef) -> str:
        if not self._attached or self._dead:
            raise HarnessError("mock: target not available")
        return self._states.get(feature.id, feature.default_state)

    def reset(self, project: Project) -> None:
        if not self._attached:
            raise HarnessError("mock: not attached")
        if self._dead or self._hung:
            raise HarnessError("mock: cannot reset a dead or hung target")
        self._modal = ""
        self._project = project
        self._log(("reset", "*"))
        keep = {k: self._values[k] for k in self.forget_on_reset if k in self._values}
        self._states = {f.id: f.default_state for f in project.features if f.kind != "action"}
        self._values = {c.id: c.default for c in project.controls}
        self._values.update(keep)                  # planted bug: reset forgets these controls
        self._snap()
        self._dirty = False

    def wait_quiescent(self, timeout_ms: int = 0) -> None:
        self._vt += max(0, timeout_ms)
        if self._dirty or self._rates:
            self._evaluate(final=True)
            self._dirty = False

    def health(self) -> Health:
        if not self._attached:
            return Health("missing", "mock: not attached")
        if self._dirty:
            self._evaluate(final=False)
        if self._dead:
            return Health("dead", self._dead)
        if self._hung:
            return Health("hung", self._hung)
        if self._modal:
            return Health("modal", self._modal)
        return Health("ok")

    # ------------------------------------------------------------ optional protocol extensions (additive)
    def _states_at(self, vt: float) -> dict[str, str]:
        snap: dict[str, str] = {}
        for t, st in self._hist:
            if t <= vt:
                snap = st
        return snap

    def _enabled_for(self, cid: str) -> bool:
        states = self._states_at(self._vt - self.enable_delay_ms) if self.enable_delay_ms else self._states
        if cid in self.enable_rules and not all(self._is_active(f, states) for f in self.enable_rules[cid]):
            return False
        if cid in self.disable_when and any(self._is_active(f, states) for f in self.disable_when[cid]):
            return False
        return True

    def probe(self, selectors: dict[str, Selector]) -> dict[str, dict[str, bool]]:
        """OPTIONAL: batched read-only element props {key: {exists, visible, enabled}} (spec U4 delta 3)."""
        if not self._attached or self._dead:
            raise HarnessError("mock: target not available")
        by_aid = {c.selector.auto_id: c.id for c in (self._project.controls if self._project else ())}
        out: dict[str, dict[str, bool]] = {}
        for key, sel in selectors.items():
            cid = by_aid.get(sel.auto_id, key)
            en = self._enabled_for(cid)
            if self.read_noise:
                self._reads += 1
                if _u(self.seed, "n", self._reads, key) < self.read_noise:
                    en = not en
            out[key] = {"exists": True, "visible": True, "enabled": en}
        return out

    def selector_enabled(self, selector: Selector) -> bool | None:
        by_aid = {c.selector.auto_id: c.id for c in (self._project.controls if self._project else ())}
        cid = by_aid.get(selector.auto_id)
        return self._enabled_for(cid) if cid is not None else True

    def element_text(self, selector: Selector) -> str:
        """OPTIONAL (tripwire): current label of the element; tests plant label changes via element_labels."""
        for f in (self._project.features if self._project else ()):
            if f.selector == selector and f.id in self.element_labels:
                return self.element_labels[f.id]
        return selector.title or ""

    def screenshot(self, path: str) -> bool:
        try:
            with open(path, "wb") as fh:
                fh.write(_PNG)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------ inspector tree
    def inspect(self) -> list[ElementInfo]:
        out: list[ElementInfo] = []

        def add(name, ctype, aid="", parent=None, depth=1, value=None, patterns=(), kind="",
                dom=None, conf="unknown", cls=""):
            names, p = [], parent
            while p is not None:
                names.append(out[p].name)
                p = out[p].parent
            path = tuple(reversed(names))
            sel = Selector(auto_id=aid, title=name, control_type=ctype, class_name=cls, path=path)
            out.append(ElementInfo(sel, name, ctype, depth, parent, (0, 0, 100, 20), value,
                                   tuple(patterns), kind, dom, conf))
            return len(out) - 1

        w = add("Mock App", "Window", "main", None, 0, cls="MockWindow")
        tab = add("Main", "Tab", "tabs", w, 1, patterns=("Selection",), kind="choice",
                  dom=Domain("choice", options=("General", "Advanced")), conf="inferred")
        add("General", "TabItem", "tab_general", tab, 2, patterns=("SelectionItem",))
        add("Advanced", "TabItem", "tab_advanced", tab, 2, patterns=("SelectionItem",))
        for key in ("Alpha", "Beta", "Gamma", "Delta"):
            add(f"Enable {key}", "CheckBox", f"cb_{key.lower()}", tab, 2,
                self._states.get(f"cb_{key.lower()}", "off") == "on", ("Toggle",),
                "boolean", Domain("boolean"), "verified", "CheckBox")
        add("Mode", "ComboBox", "cmb_mode", tab, 2, "Off", ("ExpandCollapse", "Value"),
            "choice", Domain("choice", options=("Off", "Low", "High")), "verified", "ComboBox")
        add("Volume", "Slider", "sld_volume", tab, 2, 50, ("RangeValue",), "integer",
            Domain("integer", 0, 100, 1), "verified", "Slider")
        add("Gain", "Slider", "sld_gain", tab, 2, 1.0, ("RangeValue",), "float",
            Domain("float", 0.0, 10.0, 0.1), "verified", "Slider")
        add("Count", "Spinner", "spn_count", tab, 2, 3, ("RangeValue",), "integer",
            Domain("integer", 0, 50, 1), "verified", "SpinBox")
        add("Name", "Edit", "edt_name", tab, 2, "", ("Value",), "text",
            Domain("text", max_length=20), "inferred", "LineEdit")
        add("Notes", "Edit", "edt_notes", tab, 2, "", ("Value",), "text",
            Domain("text", max_length=200), "inferred", "LineEdit")
        for name, aid in (("Apply", "btn_apply"), ("Reset", "btn_reset"), ("Crash now", "btn_crash"),
                          ("Hang now", "btn_hang"), ("Delete all data", "btn_delete")):
            add(name, "Button", aid, w, 1, None, ("Invoke",), "action", None, "inferred", "Button")
        add("Status", "Text", "lbl_status", w, 1, "ready", (), "", None, "unknown", "Label")
        return out


# ---------------------------------------------------------------------------- generator
_M64 = (1 << 64) - 1


class _Rng:
    """splitmix64: tiny state, fast, trivially checkpointable."""

    def __init__(self, state: int) -> None:
        self.s = state & _M64

    def next(self) -> int:
        self.s = (self.s + 0x9E3779B97F4A7C15) & _M64
        z = self.s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M64
        return z ^ (z >> 31)

    def below(self, n: int) -> int:
        return self.next() % n if n > 0 else 0

    def unit(self) -> float:
        return (self.next() >> 11) / float(1 << 53)


class SimpleGenerator:
    """Uniform random cases, checkpointable as {"seq": n}. NOT coverage driven."""

    def __init__(self, project: Project, checkpoint: dict[str, Any] | None = None,
                 epoch_size: int = 1000, max_cases: int | None = None) -> None:
        self.project, self.epoch_size, self.max_cases = project, max(1, epoch_size), max_cases
        self.seq = int((checkpoint or {}).get("seq", 0))
        self._states = [f for f in project.features if f.kind != "action" and f.enabled]
        self._actions = [f for f in project.features
                         if f.kind == "action" and f.enabled and not f.dangerous]
        self._controls = [c for c in project.controls if not (c.skip or c.read_only)]

    def __iter__(self) -> Iterator[Case]:
        return self

    def __next__(self) -> Case:
        if self.max_cases is not None and self.seq >= self.max_cases:
            raise StopIteration
        rng = _Rng((self.project.run.seed * 1000003) ^ (self.seq * 0x9E3779B1) ^ 0x5DEECE66D)
        self.seq += 1
        state = {f.id: f.states[rng.below(len(f.states))] if f.states else f.default_state
                 for f in self._states}
        values = {c.id: self._value(c, rng) for c in self._controls}
        order = [f.id for f in self._states] + [a.id for a in self._actions if rng.below(4) == 0]
        for i in range(len(order) - 1, 0, -1):
            j = rng.below(i + 1)
            order[i], order[j] = order[j], order[i]
        return Case(self.seq, state, values, tuple(order), 0, ())

    @staticmethod
    def _value(c: ControlDef, rng: _Rng) -> Value:
        d = c.fuzz_domain or c.domain
        if d.kind == "boolean":
            return bool(rng.below(2))
        if d.kind == "choice":
            return d.options[rng.below(len(d.options))] if d.options else c.default
        if d.kind == "text":
            return "".join(chr(97 + rng.below(26)) for _ in range(rng.below((d.max_length or 8) + 1)))
        lo = 0 if d.minimum is None else d.minimum
        hi = 100 if d.maximum is None else d.maximum
        if d.kind == "integer":
            return int(lo) + rng.below(int(hi) - int(lo) + 1)
        return float(lo) + rng.unit() * (float(hi) - float(lo))

    def coverage(self) -> Coverage:
        return Coverage(None, 0, self.seq // self.epoch_size, guarantee="random sampling; no coverage guarantee")

    def feedback(self, case: Case, outcome: Outcome) -> None:
        pass

    def checkpoint(self) -> dict[str, Any]:
        return {"seq": self.seq}

    def restore(self, state: dict[str, Any]) -> None:
        self.seq = int(state.get("seq", 0))


def simple_generator(project: Project, checkpoint: dict[str, Any] | None = None, **kw: Any) -> SimpleGenerator:
    return SimpleGenerator(project, checkpoint, **kw)
