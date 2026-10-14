"""Scheduler facade: ``make_generator``, ``preview``, ``estimate``."""
from __future__ import annotations

import itertools
from typing import Any

from . import domain as D
from .states import StateSpace, pow2_text
from .strategies import strategy_class
from .types import Case, CaseGenerator, Project


def make_generator(project: Project, checkpoint: dict[str, Any] | None = None) -> CaseGenerator:
    """Build the generator for ``project.run.strategy`` (ValueError if unknown),
    optionally resuming from a ``checkpoint()`` dict."""
    gen = strategy_class(project.run.strategy)(project)
    if checkpoint:
        gen.restore(checkpoint)
    return gen


def preview(project: Project, n: int) -> list[Case]:
    """First ``n`` cases of a fresh generator. Pure: touches no live generator state."""
    return list(itertools.islice(make_generator(project), max(0, int(n))))


def estimate(project: Project) -> dict[str, Any]:
    """Report (never refuse): valid state count, exact Cartesian size or None, text."""
    space = StateSpace(project)
    valid = space.size
    combos: int | None = 1
    for c in project.controls:
        if c.skip or c.read_only:
            continue
        k = D.count(D.effective(c))
        if k is None:
            combos = None
            break
        combos *= k
    cartesian = None if (combos is None or valid is None) else valid * combos
    if valid is None:
        st = "uncountable feature states (large constraint components)"
    else:
        st = f"{pow2_text(valid)} states"
        if valid != space.raw_size and space.raw_size:
            st += f" (of {pow2_text(space.raw_size)} before constraints)"
    if combos is None:
        vc = "continuous/unbounded values (not enumerable)"
    elif combos == 1:
        vc = "no value combos"
    else:
        vc = f"~{combos:.3g} value combos" if combos >= 10 ** 6 else f"{combos} value combos"
    return {"valid_states": valid, "cartesian": cartesian, "text": f"{st} x {vc}"}
