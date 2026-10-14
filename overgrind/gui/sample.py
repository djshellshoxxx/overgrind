"""Sample data: the --demo project and a throwaway inspect tree (used when no adapter is reachable)."""
from __future__ import annotations

from ..types import (
    AdapterSpec, ConstraintDef, ControlDef, Domain, ElementInfo, FeatureDef, Project, Provenance,
    RunConfig, Selector, TargetRef,
)

DEMO_ADAPTER = AdapterSpec("overgrind.adapters.mock:MockAdapter", {"crash_rate": 0.002, "hang_rate": 0.001, "seed": 7})


def _sel(auto_id: str, title: str, ctype: str) -> Selector:
    return Selector(auto_id=auto_id, title=title, control_type=ctype, path=("Demo App", "Settings"))


def demo_project() -> Project:
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
