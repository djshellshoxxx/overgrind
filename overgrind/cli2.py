"""`python -m overgrind v2 <cmd>`: headless v0.2 commands. JSON on stdout, diagnostics on stderr.

Exit codes: 0 clean, 1 errors found (or regression/failed preflight), 2 usage / unusable setup.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import multiprocessing
import sys
from pathlib import Path
from typing import Any

from .types import AdapterSpec, StopConditions

DEFAULT_ADAPTER = "overgrind.adapters.uia:UiaAdapter"


class UsageError(Exception):
    pass


def _dump(obj: Any) -> None:
    print(json.dumps(obj, indent=None, default=_default, ensure_ascii=False))


def _default(o: Any) -> Any:
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _spec(factory: str, kwargs: str | None) -> AdapterSpec:
    try:
        kw = json.loads(kwargs) if kwargs else {}
    except ValueError as exc:
        raise UsageError(f"--adapter-kwargs is not valid JSON: {exc}") from exc
    if ":" not in factory:
        raise UsageError(f"adapter must look like 'package.module:callable', got {factory!r}")
    return AdapterSpec(factory, kw)


def _load(path: str):
    from . import project as P
    try:
        p = P.load(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UsageError(f"cannot load project {path}: {exc}") from exc
    problems = P.validate(p)
    if problems:
        raise UsageError("invalid project:\n  " + "\n  ".join(problems))
    return p


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="overgrind v2")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def proj(sp: argparse.ArgumentParser, adapter: bool = False) -> None:
        sp.add_argument("--project", required=True, metavar="p.json")
        if adapter:
            sp.add_argument("--adapter", default=DEFAULT_ADAPTER, metavar="module:callable")
            sp.add_argument("--adapter-kwargs", metavar="JSON")

    r = sub.add_parser("run", help="run a fuzz campaign (until stopped by default)")
    proj(r, True)
    r.add_argument("--max-seconds", type=float)
    r.add_argument("--max-cases", type=int)
    r.add_argument("--first-error", action="store_true")
    r.add_argument("--max-unique-errors", type=int)
    r.add_argument("--evidence", default="overgrind-evidence")
    r.add_argument("--resume", action="store_true", help="continue from evidence/checkpoint.json")
    r.add_argument("--generator", metavar="module:callable", help=argparse.SUPPRESS)

    pf = sub.add_parser("preflight", help="Trust Ledger dry run (writes only default values)")
    proj(pf, True)
    pf.add_argument("--in-process", action="store_true", help=argparse.SUPPRESS)
    pf.add_argument("--text", action="store_true", help="human-readable summary instead of JSON")

    er = sub.add_parser("errors", help="list/show persisted errors")
    es = er.add_subparsers(dest="what", required=True)
    for name in ("list", "show"):
        s = es.add_parser(name)
        s.add_argument("--evidence", default="overgrind-evidence")
        if name == "show":
            s.add_argument("event_id")

    rd = sub.add_parser("reduce", help="minimise a stored failing case")
    rd.add_argument("event_id")
    proj(rd, True)
    rd.add_argument("--evidence", default="overgrind-evidence")

    df = sub.add_parser("diff", help="differential run of two builds")
    proj(df)
    df.add_argument("--adapter-a", required=True, metavar="module:callable")
    df.add_argument("--adapter-b", required=True, metavar="module:callable")
    df.add_argument("--kwargs-a", metavar="JSON")
    df.add_argument("--kwargs-b", metavar="JSON")
    df.add_argument("--cases", type=int, default=20)

    es_ = sub.add_parser("estimate", help="valid states / cartesian size")
    proj(es_)
    pv = sub.add_parser("preview", help="first N generated cases")
    proj(pv)
    pv.add_argument("-n", type=int, default=10)

    mg = sub.add_parser("migrate", help="convert a v1 config to a v2 project")
    mg.add_argument("src")
    mg.add_argument("dst")
    return ap


# ------------------------------------------------------------------ commands
def cmd_run(a: argparse.Namespace) -> int:
    from .controller import RunController
    p = _load(a.project)
    sc = StopConditions(until_stopped=a.max_seconds is None and a.max_cases is None,
                        max_seconds=a.max_seconds, max_cases=a.max_cases,
                        first_error=a.first_error, max_unique_errors=a.max_unique_errors)
    ctl = RunController(p, _spec(a.adapter, a.adapter_kwargs), a.evidence, stop=sc, resume=a.resume,
                        generator_factory=a.generator)
    ctl.on_error(lambda ev: print(f"error {ev.id} {ev.category}: {ev.message[:120]}", file=sys.stderr))
    ctl.start()
    try:
        while not ctl.join(0.25):
            pass
    except KeyboardInterrupt:
        ctl.stop()
    st = ctl.stats()
    _dump({k: v for k, v in dataclasses.asdict(st).items() if k != "recent"} | {"evidence": a.evidence})
    if st.stop_reason.startswith(("automation unusable", "controller error", "worker crashed")):
        print(f"overgrind: {st.stop_reason}", file=sys.stderr)
        return 2
    return 1 if st.errors_unique else 0


def cmd_preflight(a: argparse.Namespace) -> int:
    from .preflight import preflight
    p = _load(a.project)
    rep = preflight(p, _spec(a.adapter, a.adapter_kwargs), isolated=not a.in_process)
    if a.text:
        print(rep.summary())
    else:
        _dump(rep.to_dict())
    return 1 if rep.verdict == "fail" else 0


def cmd_errors(a: argparse.Namespace) -> int:
    from . import evidence
    store = evidence.EvidenceStore(a.evidence)
    if a.what == "list":
        rows = [{"id": e.id, "ts": e.ts, "category": e.category, "signature": e.signature,
                 "count": e.count, "reliability": e.reliability, "message": e.message[:160]}
                for e in store.list()]
        _dump(rows)
        return 1 if rows else 0
    try:
        ev = store.load(a.event_id)
    except (KeyError, OSError, ValueError) as exc:
        raise UsageError(f"no such error {a.event_id!r}: {exc}") from exc
    _dump(evidence.event_to_dict(ev))
    return 1


def cmd_reduce(a: argparse.Namespace) -> int:
    from . import evidence
    from .reduce import Reducer
    p = _load(a.project)
    store = evidence.EvidenceStore(a.evidence)
    try:
        ev = store.load(a.event_id)
    except (KeyError, OSError, ValueError) as exc:
        raise UsageError(f"no such error {a.event_id!r}: {exc}") from exc
    red = Reducer(p, _spec(a.adapter, a.adapter_kwargs))
    try:
        out = red.reduce(ev)
    finally:
        red.close()
    if out.reduced:
        store.write_reduced(out, out.reduced)
    _dump({"id": out.id, "reliability": out.reliability, "reduced": out.reduced})
    return 1


def cmd_diff(a: argparse.Namespace) -> int:
    from .diffrun import differential
    p = _load(a.project)
    rep = differential(p, _spec(a.adapter_a, a.kwargs_a), _spec(a.adapter_b, a.kwargs_b), n_cases=a.cases)
    _dump(rep.to_dict())
    return 0 if rep.clean else 1


def cmd_estimate(a: argparse.Namespace) -> int:
    from . import scheduler
    _dump(scheduler.estimate(_load(a.project)))
    return 0


def cmd_preview(a: argparse.Namespace) -> int:
    from . import scheduler
    _dump([dataclasses.asdict(c) for c in scheduler.preview(_load(a.project), a.n)])
    return 0


def cmd_migrate(a: argparse.Namespace) -> int:
    from . import project as P
    try:
        data = json.loads(Path(a.src).read_text(encoding="utf-8"))
        p = P.migrate_v1(data)
        P.save(p, a.dst)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UsageError(f"migration failed: {exc}") from exc
    problems = P.validate(p)
    _dump({"written": a.dst, "features": len(p.features), "controls": len(p.controls), "problems": problems})
    return 0


COMMANDS = {"run": cmd_run, "preflight": cmd_preflight, "errors": cmd_errors, "reduce": cmd_reduce,
            "diff": cmd_diff, "estimate": cmd_estimate, "preview": cmd_preview, "migrate": cmd_migrate}


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:           # argparse: 0 for --help, 2 for usage errors
        return int(exc.code or 0)
    try:
        return COMMANDS[args.cmd](args)
    except UsageError as exc:
        print(f"overgrind: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"overgrind: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
