"""Self-healing locator logic, independent of Windows.

Given a `Selector` and a flat list of `ElementInfo` (a UIA snapshot), resolve the element through
ordered tiers, most stable first, and report which tier matched and whether it was ambiguous:

    auto_id  ->  path+type  ->  title+type  ->  class+index

A tier that matches several elements narrows the candidate pool; later tiers refine it. `explain`
produces the human-readable stability explanation shown by the inspector / preflight.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..types import ElementInfo, Selector

TIERS = ("auto_id", "path+type", "title+type", "class+index")


@dataclass(frozen=True)
class Resolution:
    index: int | None                    # index into the element list, None = unresolved
    tier: str = ""                       # tier that decided ("" if unresolved)
    ambiguous: bool = False              # more than one element matched at some tier
    candidates: tuple[int, ...] = ()     # remaining candidates when unresolved/ambiguous
    tried: tuple[tuple[str, int], ...] = ()   # (tier, matches within the pool)
    note: str = ""

    @property
    def found(self) -> bool:
        return self.index is not None


def _title(e: ElementInfo) -> str:
    return e.name or e.selector.title


def _applies(tier: str, sel: Selector) -> bool:
    if tier == "auto_id":
        return bool(sel.auto_id)
    if tier == "path+type":
        return bool(sel.path)
    if tier == "title+type":
        return bool(sel.title)
    return bool(sel.class_name) or (bool(sel.control_type) and sel.index is not None)


def _tier_match(tier: str, sel: Selector, e: ElementInfo) -> bool:
    es = e.selector
    if tier == "auto_id":
        # a different control type behind the same id (Button -> CheckBox) is a different control: never bind to it
        return es.auto_id == sel.auto_id and ((not sel.control_type) or not e.control_type
                                              or e.control_type == sel.control_type)
    if tier == "path+type":
        if tuple(es.path) != tuple(sel.path):
            return False
        if sel.control_type and e.control_type != sel.control_type:
            return False
        return (not sel.title) or _title(e) == sel.title
    if tier == "title+type":
        if _title(e) != sel.title:
            return False
        return (not sel.control_type) or e.control_type == sel.control_type
    if sel.class_name and es.class_name != sel.class_name:
        return False
    return (not sel.control_type) or e.control_type == sel.control_type


def resolve(selector: Selector, elements: Sequence[ElementInfo]) -> Resolution:
    pool = list(range(len(elements)))
    tried: list[tuple[str, int]] = []
    ambiguous = False
    for tier in TIERS:
        if not _applies(tier, selector):
            continue
        matches = [i for i in pool if _tier_match(tier, selector, elements[i])]
        tried.append((tier, len(matches)))
        if len(matches) == 1:
            return Resolution(matches[0], tier, ambiguous, (), tuple(tried), _healed_note(tier, tried))
        if len(matches) > 1:
            ambiguous = True
            pool = matches          # narrow; later tiers refine
        # 0 matches: keep the pool, a lower tier may still find it (self-healing)
    if ambiguous:
        if selector.index is not None and 0 <= selector.index < len(pool):
            return Resolution(pool[selector.index], tried[-1][0], True, tuple(pool), tuple(tried),
                              "ambiguous; disambiguated by index")
        return Resolution(None, "", True, tuple(pool), tuple(tried),
                          f"ambiguous: {len(pool)} elements match; add path or index")
    return Resolution(None, "", False, (), tuple(tried), "no element matches any tier")


def _healed_note(tier: str, tried: list[tuple[str, int]]) -> str:
    if len(tried) > 1 or tier != "auto_id":
        misses = [t for t, n in tried if n == 0]
        if misses:
            return f"resolved via {tier} after {', '.join(misses)} failed (selector is drifting)"
    return f"resolved via {tier}"


def stability_score(selector: Selector, elements: Sequence[ElementInfo] | None = None) -> tuple[int, str]:
    """0..100 + reason. Mirrors project.selector_score but also looks at observed uniqueness."""
    if selector.auto_id:
        base, why = 90, "AutomationId is the most stable locator"
    elif selector.path and selector.control_type:
        base, why = 70, "window path + control type; breaks if the layout changes"
    elif selector.title and selector.control_type:
        base, why = 55, "title + control type; breaks if the label is renamed or localised"
    elif selector.class_name:
        base, why = 30, "class name (+index) only; breaks when siblings change"
    else:
        base, why = 10, "positional or empty selector"
    if elements is not None:
        r = resolve(selector, elements)
        if not r.found and r.ambiguous:
            return max(0, base - 25), why + "; matches several elements (ambiguous)"
        if not r.found:
            return 0, why + "; currently unresolved"
        if r.ambiguous:
            base = max(0, base - 25)
            why += "; matches several elements"
    return base, why


def explain(selector: Selector, elements: Sequence[ElementInfo]) -> str:
    r = resolve(selector, elements)
    tiers = ", ".join(f"{t}:{n}" for t, n in r.tried) or "no applicable tier"
    score, why = stability_score(selector, elements)
    state = (f"resolved to #{r.index} via {r.tier}" if r.found else r.note)
    return f"{state} [{tiers}] stability {score}/100 ({why})"


def suggest_selector(element: ElementInfo, elements: Sequence[ElementInfo]) -> Selector:
    """Most stable selector for `element` that resolves uniquely in `elements`."""
    s = element.selector
    cands = []
    if s.auto_id:
        cands.append(Selector(auto_id=s.auto_id, control_type=element.control_type))
    if s.path:
        cands.append(Selector(path=s.path, control_type=element.control_type, title=_title(element)))
    if _title(element):
        cands.append(Selector(title=_title(element), control_type=element.control_type))
    cands.append(Selector(class_name=s.class_name, control_type=element.control_type))
    me = elements.index(element) if element in elements else None
    for c in cands:
        r = resolve(c, elements)
        if r.found and (me is None or r.index == me) and not r.ambiguous:
            return c
    same = [i for i, e in enumerate(elements)
            if e.control_type == element.control_type and e.selector.class_name == s.class_name]
    idx = same.index(me) if me in same else s.index
    return Selector(class_name=s.class_name, control_type=element.control_type, index=idx)
