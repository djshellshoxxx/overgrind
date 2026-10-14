"""Deterministic randomized coverage of all ordered feature pairs."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from random import Random
from typing import Any, Iterator

from .models import AppSpec
from .sampling import sample_value

MAX_CASES = 250_000


@dataclass(frozen=True)
class TestCase:
    index: int
    first: str
    second: str
    values: dict[str, Any]


def total_cases(spec: AppSpec, samples: int) -> int:
    if type(samples) is not int or samples < 1:
        raise ValueError("samples must be a positive integer")
    n = len(spec.features)
    total = n * n * samples
    if total > MAX_CASES:
        raise ValueError(f"{total} cases exceeds the {MAX_CASES} case per-run cap; split the campaign")
    return total


def generate_cases(spec: AppSpec, samples: int, seed: int) -> Iterator[TestCase]:
    """Shuffle each feature-pair/trial occurrence, then sample all controls.

    Every ordered pair, including (A, A) and (A, B)/(B, A), appears
    exactly the configured samples count. The same manifest and seed replay.
    """
    total = total_cases(spec, samples)
    rng = Random(seed)
    pairs = list(itertools.product((f.id for f in spec.features), repeat=2))
    jobs = list(range(total))
    rng.shuffle(jobs)
    for index, job in enumerate(jobs):
        pair = pairs[job % len(pairs)]
        values = {control.id: sample_value(control, rng) for control in spec.controls}
        yield TestCase(index, pair[0], pair[1], values)
