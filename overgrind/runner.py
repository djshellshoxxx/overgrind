"""Execution boundary and append-only evidence logging."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

from .models import AppSpec, ControlSpec, FeatureSpec
from .planning import generate_cases, total_cases


class GuiAdapter(Protocol):
    def set_control(self, control: ControlSpec, value: Any) -> None: ...
    def activate_feature(self, feature: FeatureSpec) -> None: ...
    def healthy(self) -> bool: ...


def execute(
    spec: AppSpec,
    adapter: GuiAdapter,
    output: str | Path,
    *,
    samples: int = 1,
    seed: int = 1,
    stop_on_failure: bool = False,
    settle_ms: int = 0,
    only_index: int | None = None,
) -> dict[str, int]:
    """Run all planned cases against an adapter and store JSONL evidence."""
    if type(settle_ms) is not int or settle_ms < 0:
        raise ValueError("settle_ms must be a nonnegative integer")
    count = total_cases(spec, samples)
    if only_index is not None and (type(only_index) is not int or not 0 <= only_index < count):
        raise ValueError(f"only_index must be between 0 and {count - 1}")
    feature_index = {feature.id: feature for feature in spec.features}
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"passed": 0, "failed": 0}
    # Refuse to overwrite previous evidence.
    with path.open("x", encoding="utf-8") as stream:
        for case in generate_cases(spec, samples=samples, seed=seed):
            if only_index is not None and case.index != only_index:
                continue
            start = time.monotonic()
            status, error = "passed", None
            applied_controls: list[str] = []
            try:
                prepare = getattr(adapter, "prepare_case", None)
                if callable(prepare):
                    prepare(spec.features)
                adapter.activate_feature(feature_index[case.first])
                adapter.activate_feature(feature_index[case.second])
                active = {case.first, case.second}
                for control in spec.controls:
                    if set(control.requires_features).issubset(active):
                        adapter.set_control(control, case.values[control.id])
                        applied_controls.append(control.id)
                if settle_ms:
                    time.sleep(settle_ms / 1000)
                if not adapter.healthy():
                    raise RuntimeError("application window is missing or unhealthy")
            except Exception as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"
            record = {
                "index": case.index,
                "seed": seed,
                "pair": [case.first, case.second],
                "values": case.values,
                "applied_controls": applied_controls,
                "status": status,
                "error": error,
                "duration_ms": round((time.monotonic() - start) * 1000, 3),
            }
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            counts[status] += 1
            if status == "failed" and stop_on_failure:
                break
    return counts
