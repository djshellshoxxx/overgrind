"""Smart selectors (spec B1): explained fragility score, shared similarity, bounded self-healing.

Pure Python on `ElementInfo`/`Selector` (no pywinauto, no Qt). `similarity()` is the ONE implementation
shared by selector healing (`adapters/resolve.py`) and the twin-build control mapping (`diffrun.py`).

Heal policy: accept a candidate only when its score >= `HEAL_THRESHOLD` AND the margin to the runner-up
>= `HEAL_MARGIN`; otherwise refuse (HarnessError at the resolve layer). The thresholds and weights are
provisional (spec deltas), not literature values.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .types import ElementInfo, HarnessError, Selector

HEAL_THRESHOLD = 0.85
HEAL_MARGIN = 0.15
HEAL_MAX_NODES = 2000

# attribute weights (spec U5 3.2 / B1 3.4); unavailable attributes are dropped and the rest renormalised
WEIGHTS = {"auto_id": 0.25, "title": 0.25, "path": 0.15, "class_name": 0.05,
           "position": 0.15, "neighbour": 0.10, "patterns": 0.05}

EQUIVALENT_TYPES = (frozenset({"Edit", "Document"}), frozenset({"Button", "SplitButton"}))

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT = re.compile(r"[^0-9A-Za-zÀ-￿]+")


# ------------------------------------------------------------------ string similarity
def tokens(text: str) -> list[str]:
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = _CAMEL.sub(" ", s)
    return [t for t in _SPLIT.split(s.casefold()) if t]


def normalize_title(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text or "")).replace("&", "").replace("…", "")
    s = re.sub(r"\.\.\.$", "", s.strip())
    s = re.sub(r"\d+", "", s)
    return " ".join(s.casefold().split())


def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0 if a else 0.0
    if not a or not b:
        return 0.0
    rng = max(len(a), len(b)) // 2 - 1
    rng = max(rng, 0)
    am, bm = [False] * len(a), [False] * len(b)
    m = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - rng), min(len(b), i + rng + 1)):
            if not bm[j] and b[j] == ch:
                am[i] = bm[j] = True
                m += 1
                break
    if not m:
        return 0.0
    k = t = 0
    for i in range(len(a)):
        if am[i]:
            while not bm[k]:
                k += 1
            if a[i] != b[k]:
                t += 1
            k += 1
    jaro = (m / len(a) + m / len(b) + (m - t / 2) / m) / 3
    p = 0
    for x, y in zip(a, b):
        if x != y or p == 4:
            break
        p += 1
    return jaro + p * 0.1 * (1 - jaro)


def _jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _id_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    ta, tb = tokens(a), tokens(b)
    return max(jaro_winkler(" ".join(ta), " ".join(tb)), _jaccard(ta, tb))


def _title_sim(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if na == nb:
        return 1.0 if na else 0.0
    return max(_jaccard(na.split(), nb.split()), jaro_winkler(na, nb))


def _path_sim(a: Sequence[str], b: Sequence[str]) -> float:
    if not a and not b:
        return 1.0
    n = 0
    for x, y in zip(reversed(list(a)), reversed(list(b))):
        if x != y:
            break
        n += 1
    return n / max(len(a), len(b))


def types_compatible(a: str, b: str) -> bool:
    if a == b or not a or not b:
        return True
    return any(a in g and b in g for g in EQUIVALENT_TYPES)


# ------------------------------------------------------------------ similarity
def _geometry(info: ElementInfo, tree: Sequence[ElementInfo] | None) -> tuple[float, float, float, float] | None:
    """Bounds as fractions of the root window; None when unknown."""
    if not tree or info.bounds == (0, 0, 0, 0):
        return None
    root = tree[0]
    rx, ry, rw, rh = root.bounds
    if rw <= 0 or rh <= 0:
        return None
    x, y, w, h = info.bounds
    return ((x - rx) / rw, (y - ry) / rh, w / rw, h / rh)


def _neighbour(info: ElementInfo, tree: Sequence[ElementInfo] | None) -> str | None:
    """Name of the preceding sibling (label-for-edit pattern)."""
    if not tree or info.parent is None:
        return None
    try:
        me = next(i for i, e in enumerate(tree) if e is info)
    except StopIteration:
        return None
    for j in range(me - 1, -1, -1):
        if tree[j].parent == info.parent:
            return tree[j].name or None
        if j == info.parent:
            break
    return None


def similarity_parts(a: ElementInfo | Selector, b: ElementInfo, *, a_tree: Sequence[ElementInfo] | None = None,
                     b_tree: Sequence[ElementInfo] | None = None) -> dict[str, float | None]:
    """Per-attribute similarity in [0,1]; None = attribute unavailable on either side."""
    if isinstance(a, ElementInfo):
        sa, ctype, name, ai = a.selector, a.control_type, a.name, a
    else:
        sa, ctype, name, ai = a, a.control_type, a.title, None
    sb = b.selector
    t_a, t_b = (name or sa.title), (b.name or sb.title)
    parts: dict[str, float | None] = {}
    parts["auto_id"] = _id_sim(sa.auto_id, sb.auto_id) if sa.auto_id and sb.auto_id else None
    parts["title"] = _title_sim(t_a, t_b) if t_a and t_b else None
    parts["path"] = _path_sim(sa.path, sb.path) if sa.path or sb.path else None
    parts["class_name"] = (1.0 if sa.class_name == sb.class_name else 0.0) if sa.class_name and sb.class_name else None
    ga = _geometry(ai, a_tree) if ai is not None else None
    gb = _geometry(b, b_tree)
    parts["position"] = max(0.0, 1.0 - sum(abs(x - y) for x, y in zip(ga, gb)) / 2) if ga and gb else None
    na, nb = (_neighbour(ai, a_tree) if ai is not None else None), _neighbour(b, b_tree)
    parts["neighbour"] = _title_sim(na, nb) if na and nb else None
    parts["patterns"] = _jaccard(ai.patterns, b.patterns) if ai is not None and ai.patterns and b.patterns else None
    return parts


def similarity(a: ElementInfo | Selector, b: ElementInfo, *, a_tree: Sequence[ElementInfo] | None = None,
               b_tree: Sequence[ElementInfo] | None = None) -> float:
    """Weighted attribute similarity in [0,1]; 0 when control types are incompatible."""
    ctype = a.control_type
    if not types_compatible(ctype, b.control_type):
        return 0.0
    parts = similarity_parts(a, b, a_tree=a_tree, b_tree=b_tree)
    tot = sum(WEIGHTS[k] for k, v in parts.items() if v is not None)
    if tot <= 0:
        return 0.0
    return sum(WEIGHTS[k] * v for k, v in parts.items() if v is not None) / tot


# ------------------------------------------------------------------ healing
@dataclass(frozen=True)
class HealResult:
    index: int | None
    score: float = 0.0
    margin: float = 0.0
    runner_up: float = 0.0
    ok: bool = False
    reason: str = ""


def heal(target: ElementInfo | Selector, elements: Sequence[ElementInfo], *,
         threshold: float = HEAL_THRESHOLD, margin: float = HEAL_MARGIN,
         accept: Callable[[ElementInfo], bool] | None = None,
         taught_tree: Sequence[ElementInfo] | None = None) -> HealResult:
    """Best candidate under the margin rule; never guesses. `accept` is the owner-process filter."""
    best = second = 0.0
    bi: int | None = None
    ctype = target.control_type
    for i, e in enumerate(list(elements)[:HEAL_MAX_NODES]):
        if ctype and e.control_type != ctype:                    # hard filter: type must be EQUAL
            continue
        if accept is not None and not accept(e):
            continue
        s = similarity(target, e, a_tree=taught_tree, b_tree=elements)
        if s > best:
            best, second, bi = s, best, i
        elif s > second:
            second = s
    if bi is None:
        return HealResult(None, reason="no candidate of the same control type")
    m = best - second
    if best < threshold:
        return HealResult(None, best, m, second, False, f"best candidate scores {best:.2f} < {threshold:.2f}")
    if m < margin:
        return HealResult(None, best, m, second, False,
                          f"ambiguous: best {best:.2f}, runner-up {second:.2f} (margin {m:.2f} < {margin:.2f})")
    return HealResult(bi, best, m, second, True, f"healed with score {best:.2f}, margin {m:.2f}")


def write_plausible(kind: str, value: Any) -> bool:
    """Pre-write check for healed targets: the current value must parse as the control's domain kind."""
    if kind == "boolean":
        return isinstance(value, bool) or str(value).strip().lower() in ("true", "false", "on", "off", "0", "1")
    if kind in ("integer", "float"):
        try:
            float(value)
            return not isinstance(value, bool)
        except (TypeError, ValueError):
            return False
    if kind in ("text", "choice"):
        return value is None or isinstance(value, str)
    return True


@dataclass(frozen=True)
class SelectorUpdate:
    old: Selector
    new: Selector
    score: float
    margin: float
    note: str = ""        # provenance note ("ev:v1;heal=1;score=..;margin=..")


def propose_update(old: Selector, healed: ElementInfo, score: float, margin: float) -> SelectorUpdate:
    return SelectorUpdate(old, healed.selector, round(score, 3), round(margin, 3),
                          f"ev:v1;heal=1;score={score:.2f};margin={margin:.2f}")


def resolve(elements: Sequence[ElementInfo], sel: Selector, *, allow_heal: bool = True,
            heal_threshold: float = HEAL_THRESHOLD, heal_margin: float = HEAL_MARGIN,
            dangerous: bool = False, accept: Callable[[ElementInfo], bool] | None = None) -> Any:
    """Tiered resolve with explicit bounded healing. Raises HarnessError when it cannot be sure."""
    from .adapters import resolve as R
    return R.resolve_heal(sel, elements, allow_heal=allow_heal and not dangerous,
                          threshold=heal_threshold, margin=heal_margin, accept=accept)


# ------------------------------------------------------------------ fragility explanations
@dataclass(frozen=True)
class Reason:
    code: str
    severity: int          # penalty points
    text: str
    fix: str


_FIX_ID = "ask developers to set AutomationProperties.AutomationId / objectName to a stable name"
FRAGILITY: dict[str, tuple[int, str, str]] = {
    "F01": (35, "No AutomationId: falls back to Name, which changes with language/labels", _FIX_ID),
    "F02": (30, "AutomationId looks auto-generated; it changes between builds", _FIX_ID),
    "F03": (25, "Several siblings share this AutomationId", "pick a parent container or add a longer path"),
    "F04": (15, "Empty title", "use the neighbouring label or an AutomationId"),
    "F05": (25, "Name follows displayed data (digits, dates, units or the current value)",
            "select by AutomationId or by the neighbouring label instead"),
    "F06": (10, "Language dependent text without an AutomationId", _FIX_ID),
    "F07": (10, "Deep or generic layout path: layout dependent", "choose a nearer named container"),
    "F08": (30, "Position among identical siblings (index) is used", "avoid index; add an AutomationId or label"),
    "F09": (5, "Generic or hashed class name", "combine with title/AutomationId"),
    "F10": (20, "Element lives in a virtualised/recycled container: items are recreated on scroll",
            "select the container and use its value patterns"),
    "F11": (30, "Resolves with more than one match at the best tier", "make it unique with a path or AutomationId"),
    "F12": (15, "Owning window title contains a document name/counter", "start the path below the window"),
}
_GENERIC_CLASS = {"edit", "static", "button", "pane", "group", "text"}
_GUID = re.compile(r"^\{?[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}?$")
_HASHY = re.compile(r"[0-9a-f]{8,}", re.IGNORECASE)
_ITEMNUM = re.compile(r"^item\s*\d+$", re.IGNORECASE)
_WINFORMS = re.compile(r"^WindowsForms10\.", re.IGNORECASE)
_DATA_TITLE = re.compile(r"\d|%|\b(?:px|kb|mb|gb|ms|sec|min)\b", re.IGNORECASE)


def looks_generated(auto_id: str) -> bool:
    a = auto_id or ""
    return bool(a and (a.isdigit() or _GUID.match(a) or _WINFORMS.match(a) or _ITEMNUM.match(a)
                       or (_HASHY.search(a) and any(ch.isdigit() for ch in _HASHY.search(a).group(0)))))


@dataclass(frozen=True)
class TierResult:
    tier: str
    matches: int


@dataclass(frozen=True)
class SelectorReport:
    score: int
    grade: str
    reasons: list[Reason] = field(default_factory=list)
    tiers: list[TierResult] = field(default_factory=list)
    empirical: float | None = None


def grade_of(score: int) -> str:
    return "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D"


def _r(code: str) -> Reason:
    p, t, f = FRAGILITY[code]
    return Reason(code, p, t, f)


def score(sel: Selector, element: ElementInfo | None = None, tree: Sequence[ElementInfo] | None = None, *,
          probe: Callable[[Selector], float] | None = None, expected_type: str = "") -> SelectorReport:
    """Heuristic stability 0..100 with F-code reasons; `probe(sel)->fraction 0..1` blends the empirical score."""
    reasons: list[Reason] = []
    base = 100
    if not sel.auto_id:
        reasons.append(_r("F01"))
    elif looks_generated(sel.auto_id):
        reasons.append(_r("F02"))
    tiers: list[TierResult] = []
    if tree is not None:
        from .adapters import resolve as R
        for t in R.TIERS:
            if R._applies(t, sel):
                tiers.append(TierResult(t, sum(R._tier_match(t, sel, e) for e in tree)))
        if sel.auto_id and tiers and tiers[0].tier == "auto_id" and tiers[0].matches > 1:
            reasons.append(_r("F03"))
        r = R.resolve(sel, tree)
        if r.ambiguous and not (sel.index is not None):
            reasons.append(_r("F11"))
    title = sel.title or (element.name if element else "")
    if not title:
        reasons.append(_r("F04"))
    else:
        val = element.value if element is not None else None
        if (_DATA_TITLE.search(title) or len(title) > 40 or (val is not None and str(val) == title)):
            reasons.append(_r("F05"))
        if not sel.auto_id and any(ord(ch) > 127 for ch in title):
            reasons.append(_r("F06"))
    if len(sel.path) > 6 or any(not seg for seg in sel.path):
        reasons.append(_r("F07"))
    if sel.index is not None:
        reasons.append(_r("F08"))
    if sel.class_name and (sel.class_name.lower() in _GENERIC_CLASS or _HASHY.search(sel.class_name)):
        reasons.append(_r("F09"))
    if element is not None and element.control_type in ("ListItem", "DataItem") and any(
            "virtual" in p.lower() or p == "ItemContainer" for p in element.patterns):
        reasons.append(_r("F10"))
    if sel.path and re.search(r"\.\w{1,5}\b|\b\d+\b|untitled|document", sel.path[0], re.IGNORECASE):
        reasons.append(_r("F12"))
    total = base - sum(x.severity for x in reasons)
    if tree is not None and sel.auto_id and tiers and tiers[0].matches == 1:
        total += 5
    if expected_type and (sel.control_type == expected_type
                          or (element is not None and element.control_type == expected_type)):
        total += 5
    total = max(0, min(100, total))
    emp = None
    if probe is not None:
        emp = max(0.0, min(1.0, float(probe(sel)))) * 100
        total = round(0.5 * total + 0.5 * emp)
    sc = int(total)
    g = grade_of(sc)
    if emp is not None and emp < 100 and g in ("A", "B"):
        g = "C"                                  # any failed re-resolution caps the grade at C
    return SelectorReport(sc, g, reasons, tiers, emp)


def score_all(infos: Sequence[ElementInfo]) -> list[SelectorReport]:
    return [score(e.selector, e, infos) for e in infos]


def why_fragile(report: SelectorReport) -> list[str]:
    """Plain-language lines for the 'Why fragile?' panel (text + fix per triggered code)."""
    return [f"{r.code}: {r.text}. Fix: {r.fix}" for r in report.reasons]
