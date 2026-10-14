"""Optional Windows UI Automation driver (requires pywinauto on Windows).

The backend discovers exposed UIA controls; a custom-drawn GUI can hide its
parameters from UI Automation. Such parameters require application-specific
adapters or automation hooks.
"""

from __future__ import annotations

import sys
from typing import Any

from .models import AppSpec, ControlSpec, FeatureSpec


class WindowsUIA:
    """Attach to an existing top-level window; never launch applications."""

    def __init__(self, title_regex: str):
        if sys.platform != "win32":
            raise RuntimeError("WindowsUIA requires Windows")
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise RuntimeError("Install Overgrind with the windows extra") from exc
        self.window = Desktop(backend="uia").window(title_re=title_regex)
        self.window.wait("exists ready", timeout=10)

    def _find(self, selector: dict[str, str]):
        criteria: dict[str, Any] = {}
        if "auto_id" in selector:
            criteria["auto_id"] = selector["auto_id"]
        if "title" in selector:
            criteria["title"] = selector["title"]
        if "control_type" in selector:
            criteria["control_type"] = selector["control_type"]
        wrapper = self.window.child_window(**criteria)
        wrapper.wait("exists enabled visible", timeout=5)
        return wrapper.wrapper_object()

    @staticmethod
    def _set_toggle(wrapper: Any, desired: bool) -> None:
        current = wrapper.get_toggle_state()
        if bool(current) != desired:
            wrapper.toggle()
        if bool(wrapper.get_toggle_state()) != desired:
            raise RuntimeError("toggle did not reach the requested state")

    def set_control(self, control: ControlSpec, value: Any) -> None:
        wrapper = self._find(control.selector)
        if control.kind in ("integer", "float"):
            try:
                pattern = wrapper.iface_range_value
                if pattern is None:
                    raise AttributeError("no RangeValue pattern")
                pattern.SetValue(float(value))
            except (AttributeError, TypeError):
                # Spin edits may lack a RangeValue provider.
                if wrapper.element_info.control_type == "Edit":
                    wrapper.set_edit_text(str(value))
                else:
                    raise
        elif control.kind == "boolean":
            self._set_toggle(wrapper, value)
        elif control.kind == "choice":
            wrapper.select(value)
        elif control.kind == "text":
            wrapper.set_edit_text(value)
        else:
            raise ValueError(f"unsupported control kind: {control.kind}")

    def activate_feature(self, feature: FeatureSpec) -> None:
        wrapper = self._find(feature.selector)
        if feature.mode == "enable":
            self._set_toggle(wrapper, True)
        elif feature.mode == "invoke":
            if hasattr(wrapper, "invoke"):
                wrapper.invoke()
            else:
                wrapper.click_input()
        else:
            raise ValueError(f"unsupported activation mode: {feature.mode}")

    def prepare_case(self, features: tuple[FeatureSpec, ...]) -> None:
        """Turn off independently enabled features before each new pair.

        Invoke-type actions cannot be automatically undone. Give those
        features an app-specific reset hook for isolated pair testing.
        """
        for feature in features:
            if feature.mode == "enable":
                self._set_toggle(self._find(feature.selector), False)

    def healthy(self) -> bool:
        return bool(self.window.exists(timeout=0))

    def inspect(self) -> dict[str, Any]:
        """Return candidates without clicking, toggling, or editing controls."""
        results: dict[str, Any] = {
            "window_title": self.window.window_text(),
            "controls": [],
            "feature_candidates": [],
            "notes": [
                "Only currently exposed UI Automation elements are discoverable.",
                "Review every selector, proposed type, range and feature action.",
                "Unbounded edits require explicit max_length before execution."
            ],
        }
        used: set[tuple[str, str]] = set()
        for wrapper in self.window.descendants():
            try:
                control_type = wrapper.element_info.control_type
                title = wrapper.window_text()
                auto_id = wrapper.automation_id()
                if not auto_id and not title:
                    continue
                selector = {"auto_id": auto_id} if auto_id else {"title": title}
                selector["control_type"] = control_type
                key = (auto_id or title, control_type)
                if key in used:
                    # Ambiguous selectors are not automation-safe.
                    continue
                used.add(key)
                record: dict[str, Any] = {
                    "name": title, "selector": selector,
                    "type": control_type, "requires_review": True,
                }
                if control_type in ("Button", "TabItem", "MenuItem"):
                    record["suggested_mode"] = "invoke"
                    results["feature_candidates"].append(record)
                elif control_type in ("CheckBox", "RadioButton"):
                    record["kind"] = "boolean"
                    record["suggested_feature_mode"] = "enable"
                    results["controls"].append(record)
                elif control_type == "ComboBox":
                    record["kind"] = "choice"
                    try:
                        record["options"] = list(dict.fromkeys(
                            t for t in wrapper.texts() if t))
                    except Exception:
                        record["options"] = []
                    results["controls"].append(record)
                elif control_type in ("Slider", "Spinner"):
                    record["kind"] = "float"
                    try:
                        pattern = wrapper.iface_range_value
                        record["minimum"] = float(pattern.CurrentMinimum)
                        record["maximum"] = float(pattern.CurrentMaximum)
                    except Exception:
                        record["minimum"] = None
                        record["maximum"] = None
                    results["controls"].append(record)
                elif control_type == "Edit":
                    record["kind"] = "text"
                    record["max_length"] = None
                    results["controls"].append(record)
            except Exception as exc:
                results.setdefault("skipped", []).append(type(exc).__name__)
        return results
