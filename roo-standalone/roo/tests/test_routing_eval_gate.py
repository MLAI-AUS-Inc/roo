"""Routing eval gate — hermetic (deterministic mode only, no LLM/network).

This replaces phrase-by-phrase routing assertions with dataset-level gates:

1. every case tagged `blessed` must be routed deterministically AND correctly
   (these mirror the behaviours the old unit tests asserted one-by-one);
2. no case tagged `misroute-guard` may be deterministically misrouted
   (falling through to the LLM router is fine — deciding wrongly without it
   is not);
3. no per-case regression against the committed baseline
   (roo/routing_eval/baseline.json) — run
   `scripts/run_routing_eval.py --write-baseline` when a change is intentional
   and include the diff in the PR.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.routing_eval import runner


@pytest.fixture(scope="module")
def results():
    return runner.run_eval(mode="deterministic")


def test_blessed_cases_route_deterministically_correct(results):
    failures = []
    for result in results:
        if "blessed" not in result.case.tags:
            continue
        if result.verdict != "correct":
            failures.append(
                f"{result.case.id}: {result.verdict} "
                f"(layer={result.prediction.layer}, got={result.prediction.skill}, "
                f"expected={result.case.expect_skill})"
            )
        elif result.action_verdict == "wrong":
            failures.append(
                f"{result.case.id}: action {result.prediction.action!r} != "
                f"expected {result.case.expect_action!r}"
            )
    assert not failures, "blessed routing regressions:\n" + "\n".join(failures)


def test_no_guarded_case_is_deterministically_misrouted(results):
    failures = [
        f"{result.case.id}: {result.prediction.layer} -> {result.prediction.skill} "
        f"(expected {result.case.expect_skill or 'none'}) {result.case.text[:60]!r}"
        for result in results
        if "misroute-guard" in result.case.tags and result.verdict == "misroute"
    ]
    assert not failures, "guarded cases misrouted deterministically:\n" + "\n".join(failures)


def test_no_regression_vs_committed_baseline(results):
    baseline = runner.load_baseline()
    if baseline is None:
        pytest.skip("no committed baseline yet (run scripts/run_routing_eval.py --write-baseline)")
    regressions, _improvements = runner.compare_to_baseline(results, baseline)
    assert not regressions, (
        "routing regressions vs baseline (if intentional, re-run "
        "scripts/run_routing_eval.py --write-baseline and commit the diff):\n"
        + "\n".join(regressions)
    )
