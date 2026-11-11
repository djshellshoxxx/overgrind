"""Command-line entrypoint for Windows GUI inspection and fuzz campaigns."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import AppSpec
from .planning import generate_cases, total_cases
from .runner import execute


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="overgrind")
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Read currently exposed Windows UIA controls")
    inspect.add_argument("--title-regex", required=True)
    inspect.add_argument("--output", type=Path)

    plan = sub.add_parser("plan", help="Print a deterministic randomized test preview")
    plan.add_argument("--config", type=Path, required=True)
    plan.add_argument("--samples", type=int, default=1)
    plan.add_argument("--seed", type=int, default=1)
    plan.add_argument("--limit", type=int, default=10)

    run = sub.add_parser("run", help="Run GUI stress tests using Windows UI Automation")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--samples", type=int, default=1)
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--settle-ms", type=int, default=0)
    run.add_argument("--case-index", type=int, default=None,
                     help="Replay just this zero-based index from the seeded plan")
    run.add_argument("--stop-on-failure", action="store_true")
    run.add_argument("--execute", action="store_true",
                     help="Required to allow changing the application")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw[:1] == ["v2"]:
        from .cli2 import main as v2_main
        return v2_main(raw[1:])
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            from .windows_uia import WindowsUIA
            catalog = WindowsUIA(args.title_regex).inspect()
            output = json.dumps(catalog, indent=2, ensure_ascii=False)
            if args.output:
                args.output.write_text(output + "\n", encoding="utf-8")
                print(f"Catalog written to {args.output}")
            else:
                print(output)
            return 0

        spec = AppSpec.from_file(args.config)
        total = total_cases(spec, args.samples)
        if args.command == "plan":
            if args.limit < 0:
                raise ValueError("limit must be nonnegative")
            print(json.dumps({
                "features": len(spec.features),
                "controls": len(spec.controls),
                "ordered_feature_pairs": len(spec.features) ** 2,
                "total_cases": total,
                "seed": args.seed,
                "preview": [
                    {"index": c.index, "pair": [c.first, c.second], "values": c.values}
                    for _, c in zip(range(args.limit),
                                    generate_cases(spec, args.samples, args.seed))
                ],
            }, indent=2))
            return 0

        if not args.execute:
            parser_error = "run requires --execute; inspect and plan are read-only"
            raise ValueError(parser_error)
        from .windows_uia import WindowsUIA
        adapter = WindowsUIA(spec.title_regex)
        counts = execute(
            spec, adapter, args.report, samples=args.samples, seed=args.seed,
            stop_on_failure=args.stop_on_failure, settle_ms=args.settle_ms,
            only_index=args.case_index,
        )
        print(json.dumps({"cases": sum(counts.values()), **counts,
                          "report": str(args.report), "seed": args.seed}))
        return 1 if counts["failed"] else 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"Overgrind: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
