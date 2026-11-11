"""Shared fakes for DIAG tests (no dependency on RUNTIME's MockAdapter)."""
from overgrind.types import (
    Action, AdapterSpec, ControlDef, Domain, FeatureDef, Health, HarnessError, Project,
    Selector, TargetFailure, TargetRef,
)


def make_project(sensitive_pw=True):
    feats = tuple(FeatureDef(id=i, default_state="off") for i in ("A", "B", "C", "bug"))
    ctls = (
        ControlDef("vol", "integer", Selector(auto_id="vol"), Domain("integer", 0, 100), default=0),
        ControlDef("gain", "float", Selector(auto_id="gain"), Domain("float", 0.0, 1.0), default=0.0),
        ControlDef("name", "text", Selector(auto_id="name"), Domain("text", max_length=20), default=""),
        ControlDef("pw", "text", Selector(auto_id="pw"), Domain("text"), default="", sensitive=sensitive_pw),
    )
    return Project(name="p", target=TargetRef(), features=feats, controls=ctls)


class FakeAdapter:
    """rule: 'none' | 'value' (vol>=10 crashes) | 'seq' (A on then B on crashes) | 'bug' (invoke bug)
    | 'flaky' (every 2nd failing replay crashes) | 'drop:<ctl>' (get_control missing)."""
    _count = 0

    def __init__(self, rule="none", offset=0.0, fail_on_gain=None, missing=(), delay=0.0, clock=None):
        self.rule, self.offset, self.fail_on_gain = rule, offset, fail_on_gain
        self.missing, self.delay, self.clock = set(missing), delay, clock
        self.st, self.vals, self.dead = {}, {}, None
        self.trail = []

    def attach(self, target): pass
    def detach(self, kill=False): pass
    def inspect(self): return []
    def reset(self, project):
        self.st, self.vals, self.trail, self.dead = {}, {}, [], None
    def wait_quiescent(self, timeout_ms): pass
    def screenshot(self, path): return False
    def pid(self): return 1234
    def health(self):
        return Health("dead", self.dead) if self.dead else Health()

    def _crash(self, msg):
        self.dead = msg

    def set_feature_state(self, feature, state):
        if feature.id in self.missing: raise HarnessError("no such element")
        self.st[feature.id] = state
        self.trail.append((feature.id, state))
        if self.rule == "seq" and self.trail[-2:] == [("A", "on"), ("B", "on")]:
            self._crash("access violation at 0x7ff61234 in pid 5012")

    def invoke(self, feature):
        if self.rule == "bug" and feature.id == "bug":
            self._crash("boom at 0xDEADBEEF pid 99")

    def set_control(self, control, value):
        if control.id in self.missing: raise HarnessError("no such element")
        self.vals[control.id] = value
        if self.rule == "value" and control.id == "vol" and value >= 10:
            self._crash("overflow handling vol")
        if self.rule == "flaky" and control.id == "vol" and value >= 10:
            FakeAdapter._count += 1
            if FakeAdapter._count % 2 == 0:
                self._crash("overflow handling vol")
        if self.fail_on_gain is not None and control.id == "gain" and value > self.fail_on_gain:
            self._crash("gain crash")

    def get_control(self, control):
        if control.id in self.missing: raise HarnessError("gone")
        v = self.vals.get(control.id, control.default)
        return v + self.offset if isinstance(v, float) else v


SPEC = lambda **kw: AdapterSpec("test_diag_common:FakeAdapter", kw)
