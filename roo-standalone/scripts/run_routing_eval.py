#!/usr/bin/env python3
"""Run the routing eval.

Usage (from roo-standalone/):
    .venv/bin/python scripts/run_routing_eval.py                      # deterministic, report
    .venv/bin/python scripts/run_routing_eval.py --verbose            # + fallthrough list
    .venv/bin/python scripts/run_routing_eval.py --check              # fail on baseline regression
    .venv/bin/python scripts/run_routing_eval.py --write-baseline     # record current results
    .venv/bin/python scripts/run_routing_eval.py --mode v2            # the real router (live LLM)
    .venv/bin/python scripts/run_routing_eval.py --filter blessed     # only cases with a tag
    .venv/bin/python scripts/run_routing_eval.py --json out.json      # machine-readable results

Modes:
  deterministic (default)  hermetic; fast-path-only (the regex funnel was
                           deleted in Phase 3 — everything else is the router)
  v2                       fast path + the LLM tool-calling router for every
                           case (needs OPENAI_API_KEY; Slack vars are dummied)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roo.routing_eval import runner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["deterministic", "v2"], default="deterministic")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="parallel LLM calls in v2 mode (default 8)",
    )
    parser.add_argument("--filter", dest="tag_filter", help="only run cases with this tag")
    parser.add_argument("--verbose", action="store_true", help="also list fallthrough cases")
    parser.add_argument("--json", dest="json_path", help="write per-case results to a JSON file")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=f"write results to {runner.BASELINE_PATH.name} (commit this)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero on any per-case regression vs the committed baseline",
    )
    args = parser.parse_args()

    results = runner.run_eval(
        mode=args.mode, tag_filter=args.tag_filter, concurrency=args.concurrency
    )
    print(runner.report(results, verbose=args.verbose))

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(runner.to_baseline(results, args.mode), indent=2))
        print(f"\nwrote {args.json_path}")

    if args.write_baseline:
        runner.BASELINE_PATH.write_text(json.dumps(runner.to_baseline(results, args.mode), indent=2) + "\n")
        print(f"\nwrote baseline: {runner.BASELINE_PATH}")
        return 0

    baseline = runner.load_baseline()
    if baseline is not None and baseline.get("mode") != args.mode:
        print(
            f"\n(baseline is {baseline.get('mode')}-mode; skipping comparison for a "
            f"{args.mode} run — improvements/regressions across modes are not meaningful)"
        )
        baseline = None
    if baseline is not None and not args.tag_filter:
        regressions, improvements = runner.compare_to_baseline(results, baseline)
        if improvements:
            print(f"\nimprovements vs baseline ({len(improvements)}):")
            for line in improvements:
                print(f"  {line}")
        if regressions:
            print(f"\nREGRESSIONS vs baseline ({len(regressions)}):")
            for line in regressions:
                print(f"  {line}")
            if args.check:
                return 1
        elif args.check:
            print("\nno regressions vs baseline ✓")

    return 0


if __name__ == "__main__":
    sys.exit(main())
