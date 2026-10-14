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

def _png(w: int = 64, h: int = 48, rgb: tuple[int, int, int] = (235, 235, 240)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


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
                 wedge_rate: float = 0.0, fake_pid: int | None = None) -> None:
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
        self._rates = bool(self.crash_rate or self.hang_rate or self.dialog_rate or self.wedge_rate)

    # ------------------------------------------------------------ lifecycle
    def attach(self, target: TargetRef | None = None) -> None:
        self._attached = True
        self._dead = self._hung = self._modal = ""
        self._states, self._values = {}, {}
        self._dirty = False

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
        self._states[feature.id] = state
        self._dirty = True

    def invoke(self, feature: FeatureDef) -> None:
        self._op("invoke", feature.id)
        if feature.dangerous:
            raise HarnessError("mock: refusing to invoke a dangerous feature")
        self._dirty = True

    def set_control(self, control: ControlDef, value: Value) -> None:
        self._op("control", control.id, value)
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
        self._states = {f.id: f.default_state for f in project.features if f.kind != "action"}
        self._values = {c.id: c.default for c in project.controls}
        self._dirty = False

    def wait_quiescent(self, timeout_ms: int = 0) -> None:
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
