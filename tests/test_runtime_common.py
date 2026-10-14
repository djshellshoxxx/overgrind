"""Shared builders for the runtime tests (no TestCase here)."""
from __future__ import annotations

from overgrind.types import (
    AdapterSpec, ControlDef, Domain, FeatureDef, Project, RunConfig, Selector, TargetRef,
)

MOCK = "overgrind.adapters.mock:MockAdapter"
SIMPLE_GEN = "overgrind.adapters.mock:simple_generator"


def make_project(n_features: int = 4, run: RunConfig | None = None, controls: bool = True,
                 dangerous: bool = False) -> Project:
    names = ("alpha", "beta", "gamma", "delta", "eps", "zeta", "eta", "theta")
    feats = [FeatureDef(id=f"cb_{c[0]}", name=f"Enable {c}",
                        selector=Selector(auto_id=f"cb_{c}", control_type="CheckBox"))
             for c in names[:n_features]]
    feats.append(FeatureDef(id="mode", kind="multistate", states=("Off", "Low", "High"),
                            default_state="Off", selector=Selector(auto_id="cmb_mode", control_type="ComboBox")))
    feats.append(FeatureDef(id="btn_apply", kind="action", states=(), default_state="",
                            selector=Selector(auto_id="btn_apply")))
    if dangerous:
        feats.append(FeatureDef(id="btn_delete", kind="action", states=(), default_state="",
                                dangerous=True, selector=Selector(auto_id="btn_delete")))
    ctrls = []
    if controls:
        ctrls = [
            ControlDef("volume", "integer", Selector(auto_id="sld_volume"), Domain("integer", 0, 100, 1), default=50),
            ControlDef("gain", "float", Selector(auto_id="sld_gain"), Domain("float", 0.0, 10.0, 0.1), default=1.0,
                       scope=("cb_a",)),
            ControlDef("name", "text", Selector(auto_id="edt_name"), Domain("text", max_length=8), default=""),
        ]
    return Project("t", TargetRef(kind="process", pid=None), tuple(feats), tuple(ctrls),
                   run=run or RunConfig(capture_screenshot=False, settle_ms=0, hang_threshold_ms=50))


def mock_spec(**kw) -> AdapterSpec:
    return AdapterSpec(MOCK, dict(kw))
