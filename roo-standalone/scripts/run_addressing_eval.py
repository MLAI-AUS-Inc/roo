#!/usr/bin/env python3
"""CLI for the live addressedness evaluation set."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roo.addressing_eval.runner import format_eval_report, run_addressing_eval


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--min-confidence", type=float, default=0.90)
    args = parser.parse_args()

    kwargs = {
        "model": args.model,
        "min_confidence": args.min_confidence,
    }
    if args.cases:
        kwargs["cases_path"] = args.cases
    report = asyncio.run(run_addressing_eval(**kwargs))
    print(format_eval_report(report))
    return 0 if report["summary"]["passed"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
