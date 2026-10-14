"""Type-aware, seeded samples with boundary-biased numeric stress."""

from __future__ import annotations

from random import Random
from typing import Any

from .models import ControlSpec


def sample_value(control: ControlSpec, rng: Random) -> Any:
    """Generate a valid sample; numeric edge cases are deliberately oversampled."""
    kind = control.kind
    if kind == "boolean":
        return bool(rng.getrandbits(1))
    if kind == "choice":
        return rng.choice(control.options)
    if kind == "text":
        length = rng.randint(0, control.max_length)
        return "".join(rng.choices(control.alphabet, k=length))
    low, high = control.minimum, control.maximum
    if low == high:
        return low
    if kind == "integer":
        midpoint = (low + high) // 2
        candidates = [low, high, midpoint]
        if low <= 0 <= high:
            candidates.append(0)
        if rng.random() < 0.3:
            return rng.choice(candidates)
        return rng.randint(low, high)
    if kind == "float":
        midpoint = low / 2 + high / 2
        candidates = [low, high, midpoint]
        if low <= 0 <= high:
            candidates.append(0.0)
        if rng.random() < 0.3:
            return rng.choice(candidates)
        # Weighted interpolation avoids overflow in (high - low).
        t = rng.random()
        value = (1 - t) * low + t * high
        return max(low, min(high, value))
    raise ValueError(f"unsupported kind: {kind}")
