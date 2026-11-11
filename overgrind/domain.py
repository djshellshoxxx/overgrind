"""Domain algebra: sampling, boundaries, buckets, containment, clamping, counting.

Pure functions, deterministic given a ``random.Random``. Numeric domains are
step-aware: with ``step`` (integers default to 1) valid values are
``minimum + k*step``. Float domains without a step are continuous.
"""
from __future__ import annotations

import math
from random import Random
from typing import NamedTuple

from .types import ControlDef, Domain, Value

DEFAULT_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-"
DEFAULT_TEXT_MAX = 64          # used only when a text domain lacks max_length
KINDS = ("boolean", "integer", "float", "choice", "text")
_MAX_STEPS = 2 ** 53
_EDGE_BIAS = 0.3               # probability of picking a boundary value


class _Grid(NamedTuple):
    lo: float | int
    hi: float | int
    step: float | int
    n: int                      # number of steps; values are lo + k*step, k in 0..n
    integer: bool = False

    def at(self, k: int) -> Value:
        if self.integer:
            return self.lo + self.step * k
        return float(min(self.hi, self.lo + self.step * k))


def _is_num(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _bounds(d: Domain) -> tuple[float | int, float | int]:
    lo, hi = d.minimum, d.maximum
    if lo is None or hi is None:
        raise ValueError("numeric domain needs minimum and maximum")
    if not (_is_num(lo) and _is_num(hi)) or not (math.isfinite(lo) and math.isfinite(hi)):
        raise ValueError("numeric bounds must be finite numbers")
    if d.kind == "float":
        lo, hi = float(lo), float(hi)     # exact int bounds beyond 2**53 must not out-range the floats drawn
    if lo > hi:
        raise ValueError("minimum > maximum")
    return lo, hi


def _grid(d: Domain) -> _Grid | None:
    """Value lattice, or None for a continuous float domain."""
    lo, hi = _bounds(d)
    step = d.step
    if step is not None and (not _is_num(step) or not math.isfinite(step) or step <= 0):
        raise ValueError("step must be a positive finite number")
    if d.kind == "integer":
        lo, hi = math.ceil(lo), math.floor(hi)
        if lo > hi:
            raise ValueError("no integer in range")
        if step is None:
            step = 1
        if step != int(step):
            raise ValueError("integer step must be integral")
        step = int(step)
        return _Grid(lo, hi, step, (hi - lo) // step, True)
    if step is None:
        return None
    span = hi / step - lo / step
    n = _MAX_STEPS if not math.isfinite(span) else min(_MAX_STEPS, int(math.floor(span + 1e-9)))
    return _Grid(lo, hi, step, max(0, n))


def _max_len(d: Domain) -> int:
    return DEFAULT_TEXT_MAX if d.max_length is None else d.max_length


def _alpha(d: Domain) -> str:
    return d.alphabet or DEFAULT_ALPHABET


def effective(control: ControlDef) -> Domain:
    """The domain actually fuzzed: tester-restricted subset or the original."""
    return control.fuzz_domain if control.fuzz_domain is not None else control.domain


# ---------------------------------------------------------------- containment
def contains(domain: Domain, value: Value, strict_step: bool = True) -> bool:
    """True if value is a valid member. Text alphabet is checked only if set."""
    k = domain.kind
    try:
        if k == "boolean":
            return isinstance(value, bool)
        if k == "choice":
            return isinstance(value, str) and value in domain.options
        if k == "text":
            if not isinstance(value, str):
                return False
            if domain.max_length is not None and len(value) > domain.max_length:
                return False
            return not domain.alphabet or all(c in domain.alphabet for c in value)
        if k not in ("integer", "float") or not _is_num(value) or not math.isfinite(value):
            return False
        if k == "integer" and not isinstance(value, int):
            return False
        g = _grid(domain)
        lo, hi = (g.lo, g.hi) if g else _bounds(domain)
        if value < lo or value > hi:
            return False
        if g and strict_step:
            q = (value - g.lo) / g.step
            return abs(q - round(q)) <= 1e-9 * max(1.0, abs(q))
        return True
    except (ValueError, OverflowError):
        return False


def subset_reasons(inner: Domain, outer: Domain) -> list[str]:
    """Why inner is not a subset of outer (empty list = subset)."""
    if inner.kind != outer.kind:
        return [f"kind {inner.kind!r} != {outer.kind!r}"]
    k, out = inner.kind, []
    if k in ("integer", "float"):
        try:
            ilo, ihi = _bounds(inner)
            olo, ohi = _bounds(outer)
        except ValueError as exc:
            return [str(exc)]
        if ilo < olo:
            out.append(f"minimum {ilo} below {olo}")
        if ihi > ohi:
            out.append(f"maximum {ihi} above {ohi}")
        ostep = outer.step if outer.step is not None else (1 if k == "integer" else None)
        istep = inner.step if inner.step is not None else (1 if k == "integer" else None)
        if ostep is not None:
            if istep is None:
                out.append(f"continuous but outer has step {ostep}")
            else:
                q = istep / ostep
                if abs(q - round(q)) > 1e-9 * max(1.0, abs(q)) or round(q) < 1:
                    out.append(f"step {istep} not a multiple of {ostep}")
                p = (ilo - olo) / ostep
                if abs(p - round(p)) > 1e-9 * max(1.0, abs(p)):
                    out.append(f"minimum {ilo} off the outer step grid")
    elif k == "choice":
        extra = [o for o in inner.options if o not in outer.options]
        if extra:
            out.append(f"options not in outer: {extra}")
    elif k == "text":
        if outer.max_length is not None and (inner.max_length is None or inner.max_length > outer.max_length):
            out.append(f"max_length {inner.max_length} exceeds {outer.max_length}")
        if outer.alphabet:
            extra = sorted(set(_alpha(inner)) - set(outer.alphabet))
            if extra:
                out.append(f"alphabet has characters outside outer: {''.join(extra)!r}")
    elif k != "boolean":
        out.append(f"unknown kind {k!r}")
    return out


def subset_of(inner: Domain, outer: Domain) -> bool:
    """Every value valid in inner is valid in outer."""
    return not subset_reasons(inner, outer)


# ---------------------------------------------------------------- boundaries
def _dedupe(values: list) -> list:
    out: list = []
    for v in values:
        if not any(type(v) is type(w) and v == w for w in out):
            out.append(v)
    return out


def boundary_values(domain: Domain) -> list[Value]:
    """Edge cases, all valid: min, max, min+step, max-step, 0, mid / options / text extremes."""
    k = domain.kind
    if k == "boolean":
        return [False, True]
    if k == "choice":
        return list(domain.options)
    if k == "text":
        ch, n = _alpha(domain)[0], _max_len(domain)
        vals: list = [""]
        if n >= 1:
            vals.append(ch)
        if n >= 2:
            vals.append(ch * (n - 1))
        if n >= 1:
            vals.append(ch * n)
        return _dedupe(vals)
    if k not in ("integer", "float"):
        raise ValueError(f"unsupported kind: {k}")
    g = _grid(domain)
    if g:
        vals = [g.at(0), g.at(min(1, g.n)), g.at(g.n // 2), g.at(max(0, g.n - 1)), g.at(g.n)]
    else:
        lo, hi = _bounds(domain)
        delta = hi / 1000 - lo / 1000
        vals = [lo, hi, lo / 2 + hi / 2]
        if delta > 0:
            vals += [lo + delta, hi - delta]
    vals.append(0 if k == "integer" else 0.0)
    if k == "float":
        vals = [float(v) for v in vals]
    vals = [v for v in vals if contains(domain, v)]
    return sorted(_dedupe(vals))


# ---------------------------------------------------------------- sampling
def sample(domain: Domain, rng: Random) -> Value:
    """Valid random value; numeric boundaries are oversampled (30%), text lengths 0..max_length."""
    k = domain.kind
    if k == "boolean":
        return bool(rng.getrandbits(1))
    if k == "choice":
        if not domain.options:
            raise ValueError("choice domain has no options")
        return rng.choice(domain.options)
    if k == "text":
        n = _max_len(domain)
        r = rng.random()
        length = 0 if r < 0.1 else n if r < 0.2 else rng.randint(0, n)
        return "".join(rng.choices(_alpha(domain), k=length))
    if k not in ("integer", "float"):
        raise ValueError(f"unsupported kind: {k}")
    g = _grid(domain)
    if g is None:
        lo, hi = _bounds(domain)
        if lo == hi:
            return float(lo)
        if rng.random() < _EDGE_BIAS:
            return rng.choice(boundary_values(domain))
        t = rng.random()
        return float(max(lo, min(hi, (1 - t) * lo + t * hi)))   # no (hi-lo) overflow
    if g.n == 0:
        return g.at(0)
    if rng.random() < _EDGE_BIAS:
        return rng.choice(boundary_values(domain))
    return g.at(rng.randint(0, g.n))


def clamp(domain: Domain, value: Value) -> Value:
    """Nearest valid value (numeric: snapped to range and step; text: truncated/filtered)."""
    k = domain.kind
    if k == "boolean":
        return bool(value)
    if k == "choice":
        if isinstance(value, str) and value in domain.options:
            return value
        return domain.options[0] if domain.options else value
    if k == "text":
        s = value if isinstance(value, str) else str(value)
        if domain.alphabet:
            s = "".join(c for c in s if c in domain.alphabet)
        return s[:domain.max_length] if domain.max_length is not None else s
    g = _grid(domain)
    if isinstance(value, bool) or not _is_num(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = math.nan
    if isinstance(value, float) and math.isnan(value):
        value = g.lo if g else _bounds(domain)[0]
    if g is None:
        lo, hi = _bounds(domain)
        return float(max(lo, min(hi, value)))
    if isinstance(value, float):
        if math.isinf(value):
            return g.at(g.n if value > 0 else 0)
        if k == "integer":
            value = round(value)
    try:
        q, r = divmod(value - g.lo, g.step)
        idx = int(q + (1 if 2 * r >= g.step else 0))
    except (OverflowError, ValueError):     # step vastly smaller than the span (or inf/nan arithmetic)
        idx = 0 if not value > g.lo else g.n
    return g.at(max(0, min(g.n, idx)))


# ---------------------------------------------------------------- buckets
def _fmt(v: Value) -> str:
    return f"{v:.6g}" if isinstance(v, float) else str(v)


def buckets(domain: Domain, n: int) -> list[tuple]:
    """Coverage buckets as (label, lo, hi), closed ranges that together cover the domain.

    numeric: n equal ranges plus dedicated min/max/zero buckets; choice: one per option
    (lo == hi == option); boolean: false/true; text: lengths empty/short/max (lo, hi = lengths).
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    k = domain.kind
    if k == "boolean":
        return [("false", False, False), ("true", True, True)]
    if k == "choice":
        return [(o, o, o) for o in dict.fromkeys(domain.options)]
    if k == "text":
        m = _max_len(domain)
        out = [("empty", 0, 0)]
        if m >= 2:
            out.append(("short", 1, m - 1))
        if m >= 1:
            out.append(("max", m, m))
        return out
    if k not in ("integer", "float"):
        raise ValueError(f"unsupported kind: {k}")
    g = _grid(domain)
    out = []
    if g is None:
        lo, hi = _bounds(domain)
        lo, hi = float(lo), float(hi)
        if lo == hi:
            return [("fixed", lo, hi)]
        edges = [lo + 0.0] + [(1 - i / n) * lo + (i / n) * hi for i in range(1, n)] + [hi]
        for a, b in zip(edges, edges[1:]):
            out.append((f"{_fmt(a)}..{_fmt(b)}", a, b))
        vmin, vmax = lo, hi
        zero_ok = lo < 0 < hi
    else:
        count = g.n + 1
        parts = min(n, count)
        for i in range(parts):
            a, b = g.at(i * count // parts), g.at((i + 1) * count // parts - 1)
            out.append((_fmt(a) if a == b else f"{_fmt(a)}..{_fmt(b)}", a, b))
        vmin, vmax = g.at(0), g.at(g.n)
        zero_ok = vmin < 0 < vmax and contains(domain, 0 if k == "integer" else 0.0)
    singles = {(b[1], b[2]) for b in out}
    extra = []
    if (vmin, vmin) not in singles:
        extra.append(("min", vmin, vmin))
    if zero_ok:
        z = 0 if k == "integer" else 0.0
        if (z, z) not in singles:
            extra.append(("zero", z, z))
    if (vmax, vmax) not in singles and vmax != vmin:
        extra.append(("max", vmax, vmax))
    return extra[:1] + out + extra[1:]


def sample_bucket(domain: Domain, bucket: tuple, rng: Random) -> Value:
    """A valid value inside one bucket returned by buckets()."""
    _, lo, hi = bucket
    k = domain.kind
    if k in ("boolean", "choice"):
        return lo
    if k == "text":
        return "".join(rng.choices(_alpha(domain), k=rng.randint(lo, hi)))
    g = _grid(domain)
    if g is None:
        t = rng.random()
        return float(max(lo, min(hi, (1 - t) * lo + t * hi)))
    a = max(0, math.ceil((lo - g.lo) / g.step - 1e-9))
    b = min(g.n, math.floor((hi - g.lo) / g.step + 1e-9))
    return g.at(rng.randint(a, b) if a <= b else min(max(a, 0), g.n))


# ---------------------------------------------------------------- invalid / count
def invalid_values(domain: Domain, rng: Random) -> list[Value]:
    """Out-of-domain probes (never NaN/inf). Opt-in: callers must ask explicitly."""
    k = domain.kind
    cand: list = []
    if k in ("integer", "float"):
        g = _grid(domain)
        lo, hi = (g.lo, g.hi) if g else _bounds(domain)
        unit = g.step if g else 1.0
        cand += [lo - unit, hi + unit, lo - unit * rng.randint(2, 1000), hi + unit * rng.randint(2, 1000)]
        if k == "integer":
            cand += [-2 ** 31 - 1, 2 ** 31, -2 ** 63 - 1, 2 ** 63]
        elif g and g.n >= 1:
            cand.append(lo + g.step / 2)
        if k == "integer" and g and g.step > 1 and g.n >= 1:
            cand.append(lo + 1)
        if k == "float":
            cand = [float(c) for c in cand]
    elif k == "text":
        m = _max_len(domain)
        ch = _alpha(domain)[0]
        cand += [ch * (m + 1), ch * (2 * m + 1)]
        if domain.alphabet:
            cand.append("é☃")
    elif k == "choice":
        cand += ["", "__not_an_option__"]
        if domain.options:
            cand.append(domain.options[0] + "x")
    elif k != "boolean":
        raise ValueError(f"unsupported kind: {k}")
    return [v for v in _dedupe(cand) if not contains(domain, v) and (not isinstance(v, float) or math.isfinite(v))]


def count(domain: Domain) -> int | None:
    """Number of distinct valid values; None for continuous or astronomically large (text) domains."""
    k = domain.kind
    if k == "boolean":
        return 2
    if k == "choice":
        return len(set(domain.options))
    if k == "text":
        return 1 if domain.max_length == 0 else None
    if k not in ("integer", "float"):
        raise ValueError(f"unsupported kind: {k}")
    g = _grid(domain)
    return None if g is None else g.n + 1
