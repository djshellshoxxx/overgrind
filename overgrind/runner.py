"""Execution boundary and append-only evidence logging."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

from .models import AppSpec, ControlSpec, FeatureSpec
from .planning import generate_cases


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
) -> dict[str, int]:
    """Run all planned cases against an adapter and store JSONL evidence."""
    if type(settle_ms) is not int or settle_ms < 0:
        raise ValueError("settle_ms must be a nonnegative integer")
    feature_index = {feature.id: feature for feature in spec.features}
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"passed": 0, "failed": 0}
    # Refuse to overwrite previous evidence.
    with path.open("x", encoding="utf-8") as stream:
        for case in generate_cases(spec, samples=samples, seed=seed):
            start = time.monotonic()
            status, error = "passed", None
            try:
                prepare = getattr(adapter, "prepare_case", None)
                if callable(prepare):
                    prepare(spec.features)
                for control in spec.controls:
                    adapter.set_control(control, case.values[control.id])
                adapter.activate_feature(feature_index[case.first])
                adapter.activate_feature(feature_index[case.second])
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
