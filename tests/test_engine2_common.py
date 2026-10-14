"""Shared fixtures for the engine2 tests (states / constraints / strategies)."""
from overgrind.types import (ConstraintDef, ControlDef, Domain, FeatureDef, Project, RunConfig, Selector,
                             TargetRef)


def toggles(n, prefix="f"):
    return [FeatureDef(f"{prefix}{i}") for i in range(n)]


def multistate(fid, states=("off", "low", "high")):
    return FeatureDef(fid, kind="multistate", states=tuple(states))


def control(cid, kind="integer", scope=(), skip=False, **dom):
    if kind == "integer":
        dom = {"minimum": 0, "maximum": 100, **dom}
    return ControlDef(cid, kind, Selector(auto_id=cid), Domain(kind, **dom), scope=tuple(scope), skip=skip)


def project(features, controls=(), constraints=(), **run):
    return Project("t", TargetRef(), tuple(features), tuple(controls), tuple(constraints), RunConfig(**run))


def take(gen, n):
    out = []
    for c in gen:
        out.append(c)
        if len(out) >= n:
            break
    return out


def key(case):
    return tuple(sorted(case.state.items()))
