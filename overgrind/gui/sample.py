"""Sample data: the --demo project and a throwaway inspect tree (used when no adapter is reachable)."""
from __future__ import annotations

from ..types import (
    AdapterSpec, ConstraintDef, ControlDef, Domain, ElementInfo, FeatureDef, Project, Provenance,
    RunConfig, Selector, TargetRef,
)

DEMO_ADAPTER = AdapterSpec("overgrind.adapters.mock:MockAdapter", {
    "crash_rate": 0.001, "hang_rate": 0.001, "seed": 7, "latency_ms": 1.5,
    "bad_combo": {"cb_alpha": "on", "cb_gamma": "on", "cmb_mode": "High"}})


def _sel(auto_id: str, title: str, ctype: str) -> Selector:
    return Selector(auto_id=auto_id, title=title, control_type=ctype, path=("Demo App", "Settings"))


def _static_demo() -> Project:
    prov = Provenance("uia", "verified", "")
    feats = (
        FeatureDef("dark_mode", "Dark mode", "toggle", ("off", "on"), "off", _sel("darkMode", "Dark mode", "CheckBox"), provenance=prov),
        FeatureDef("notifications", "Notifications", "toggle", ("off", "on"), "on", _sel("notify", "Notifications", "CheckBox"), provenance=prov),
        FeatureDef("sync", "Cloud sync", "toggle", ("off", "on"), "off", _sel("sync", "Cloud sync", "CheckBox"), provenance=prov),
        FeatureDef("offline", "Offline mode", "toggle", ("off", "on"), "off", _sel("offline", "Offline mode", "CheckBox"), provenance=Provenance("manual", "manual", "")),
        FeatureDef("quality", "Video quality", "multistate", ("low", "medium", "high"), "medium", _sel("quality", "Video quality", "ComboBox"), provenance=Provenance("uia", "inferred", "")),
        FeatureDef("export", "Export report", "action", (), "", _sel("exportBtn", "Export report", "Button"), dangerous=False, provenance=prov),
    )
    ctrls = (
        ControlDef("volume", "integer", _sel("volume", "Volume", "Slider"), Domain("integer", 0, 100, 1),
                   Domain("integer", 10, 90, 1), 50, provenance=prov),
        ControlDef("brightness", "float", _sel("brightness", "Brightness", "Spinner"), Domain("float", 0.0, 1.0, 0.05), None, 0.5, scope=("dark_mode",), provenance=prov),
        ControlDef("username", "text", _sel("user", "User name", "Edit"), Domain("text", max_length=20), None, "", sensitive=True, provenance=Provenance("manual", "manual", "")),
    )
    cons = (ConstraintDef("mutex", features=("sync", "offline")),)
    return Project(name="Demo project (sample app)", target=TargetRef(kind="process", process_name="demo_app.exe", title_regex="Demo App"),
                   features=feats, controls=ctrls, constraints=cons, run=RunConfig(), uuid="demo")


def sample_elements() -> list[ElementInfo]:
    rows = [
        (0, None, "Demo App", "Window", Selector(title="Demo App", control_type="Window"), "", None, "unknown"),
        (1, 0, "Settings", "Pane", Selector(auto_id="settings", control_type="Pane", path=("Demo App",)), "", None, "unknown"),
        (2, 1, "Dark mode", "CheckBox", _sel("darkMode", "Dark mode", "CheckBox"), "boolean", Domain("boolean"), "verified"),
        (2, 1, "Volume", "Slider", _sel("volume", "Volume", "Slider"), "integer", Domain("integer", 0, 100, 1), "verified"),
        (2, 1, "Quality", "ComboBox", Selector(title="Quality", control_type="ComboBox", path=("Demo App", "Settings")), "choice", Domain("choice", options=("low", "medium", "high")), "inferred"),
        (2, 1, "OK", "Button", Selector(control_type="Button", path=("Demo App", "Settings"), index=2), "action", None, "unknown"),
    ]
    out = []
    for i, (d, parent, name, ct, sel, kind, dom, conf) in enumerate(rows):
        out.append(ElementInfo(sel, name, ct, d, parent, (10, 10 + 20 * i, 200, 20), None, (), kind, dom, conf))
    return out


def demo_project() -> Project:
    """The --demo project, built from MockAdapter's own inspect tree so selectors always match."""
    try:
        from ..adapters.mock import MockAdapter
        els = {e.selector.auto_id: e for e in MockAdapter().inspect() if e.selector.auto_id}
    except Exception:
        return _static_demo()
    prov = Provenance("uia", "verified", "sample")
    feats = [FeatureDef(a, els[a].name, "toggle", ("off", "on"), "off", els[a].selector, provenance=prov)
             for a in ("cb_alpha", "cb_beta", "cb_gamma", "cb_delta")]
    feats.append(FeatureDef("cmb_mode", "Mode", "multistate", ("Off", "Low", "High"), "Off", els["cmb_mode"].selector, provenance=prov))
    feats.append(FeatureDef("btn_apply", "Apply", "action", (), "", els["btn_apply"].selector, provenance=Provenance("uia", "inferred", "sample")))
    feats.append(FeatureDef("btn_delete", "Delete all data", "action", (), "", els["btn_delete"].selector, dangerous=True, enabled=False,
                            provenance=Provenance("uia", "inferred", "sample")))
    ctrls = []
    for a, default, fz, scope in (("sld_volume", 50, Domain("integer", 10, 90, 1), ()), ("sld_gain", 1.0, None, ("cb_alpha",)),
                                  ("spn_count", 3, None, ()), ("edt_name", "", None, ())):
        e = els[a]
        ctrls.append(ControlDef(a, e.suggested_domain.kind, e.selector, e.suggested_domain, fz, default, scope=scope,
                                sensitive=(a == "edt_name"), provenance=Provenance("uia", e.confidence, "sample")))
    cons = (ConstraintDef("mutex", features=("cb_beta", "cb_delta")),)
    return Project(name="Demo project (mock app)", target=TargetRef(kind="process", process_name="mock_app.exe", title_regex="Mock App"),
                   features=tuple(feats), controls=tuple(ctrls), constraints=cons,
                   run=RunConfig(restart_on_crash=True), uuid="demo")
