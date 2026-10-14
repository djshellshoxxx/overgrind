"""Validated, adapter-independent manifest contracts."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

KINDS = frozenset(("integer", "float", "boolean", "choice", "text"))
DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-"


def _selector(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("selector must be a mapping")
    if not (value.get("auto_id") or value.get("title")):
        raise ValueError("selector needs an auto_id or title")
    if any(not isinstance(v, str) or not v for v in value.values()):
        raise ValueError("selector fields must be nonempty strings")
    if set(value) - {"auto_id", "title", "control_type"}:
        raise ValueError("unknown selector keys")
    return dict(value)


def _id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("id must be a nonempty string")
    return value


@dataclass(frozen=True)
class FeatureSpec:
    id: str
    selector: dict[str, str]
    mode: str = "invoke"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FeatureSpec":
        mode = data.get("mode", "invoke")
        if mode not in ("invoke", "enable"):
            raise ValueError("feature mode must be invoke or enable")
        return cls(id=_id(data.get("id")), selector=_selector(data.get("selector")), mode=mode)


@dataclass(frozen=True)
class ControlSpec:
    id: str
    kind: str
    selector: dict[str, str]
    minimum: float | int | None = None
    maximum: float | int | None = None
    options: tuple[str, ...] = field(default_factory=tuple)
    max_length: int | None = None
    alphabet: str = DEFAULT_ALPHABET
    requires_features: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ControlSpec":
        kind = data.get("kind")
        if kind not in KINDS:
            raise ValueError(f"invalid control kind: {kind!r}")
        ident = _id(data.get("id"))
        selector = _selector(data.get("selector"))
        minimum, maximum = data.get("minimum"), data.get("maximum")
        options = data.get("options", ())
        max_length = data.get("max_length")
        alphabet = data.get("alphabet", DEFAULT_ALPHABET)
        requires = data.get("requires_features", [])
        if (not isinstance(requires, list)
                or any(not isinstance(item, str) or not item for item in requires)
                or len(requires) != len(set(requires))):
            raise ValueError(f"{ident}: requires_features must be a unique list of feature ids")
        if kind in ("integer", "float"):
            valid = lambda x: isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x)
            if not valid(minimum) or not valid(maximum) or minimum > maximum:
                raise ValueError(f"{ident}: finite minimum <= maximum required")
            if kind == "integer" and (not isinstance(minimum, int) or not isinstance(maximum, int)):
                raise ValueError(f"{ident}: integer bounds must be integers")
        elif minimum is not None or maximum is not None:
            raise ValueError(f"{ident}: range only applies to numeric controls")
        if kind == "choice":
            if (not isinstance(options, (list, tuple)) or not options
                    or any(not isinstance(v, str) for v in options)
                    or len(set(options)) != len(options)):
                raise ValueError(f"{ident}: choice options must be unique strings")
        elif options:
            raise ValueError(f"{ident}: options only apply to choice controls")
        if kind == "text":
            if type(max_length) is not int or not 0 <= max_length <= 4096:
                raise ValueError(f"{ident}: max_length must be between 0 and 4096")
            if not isinstance(alphabet, str) or not alphabet:
                raise ValueError(f"{ident}: nonempty alphabet required")
        elif max_length is not None:
            raise ValueError(f"{ident}: max_length only applies to text")
        return cls(ident, kind, selector, minimum, maximum,
                   tuple(options), max_length, alphabet, tuple(requires))


@dataclass(frozen=True)
class AppSpec:
    title_regex: str
    features: tuple[FeatureSpec, ...]
    controls: tuple[ControlSpec, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AppSpec":
        if not isinstance(data, dict):
            raise ValueError("manifest must be an object")
        title = data.get("title_regex")
        if not isinstance(title, str) or not title:
            raise ValueError("title_regex must be nonempty")
        try:
            re.compile(title)
        except re.error as exc:
            raise ValueError(f"invalid title_regex: {exc}") from exc
        raw_features, raw_controls = data.get("features", []), data.get("controls", [])
        if not isinstance(raw_features, list) or not isinstance(raw_controls, list):
            raise ValueError("features and controls must be lists")
        features = tuple(FeatureSpec.from_dict(entry) for entry in raw_features)
        controls = tuple(ControlSpec.from_dict(entry) for entry in raw_controls)
        if not features:
            raise ValueError("at least one main feature is required")
        for label, items in (("features", features), ("controls", controls)):
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate ids in {label}")
        feature_ids = {feature.id for feature in features}
        for control in controls:
            if not set(control.requires_features).issubset(feature_ids):
                raise ValueError(f"{control.id}: unknown feature dependency")
        return cls(title, features, controls)

    @classmethod
    def from_file(cls, path: str | Path) -> "AppSpec":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
