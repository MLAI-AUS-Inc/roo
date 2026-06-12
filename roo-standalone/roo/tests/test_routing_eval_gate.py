"""Routing eval gate — hermetic (deterministic mode only, no LLM/network).

Since Phase 3 of the routing redesign deleted the regex/keyword funnel, the
only deterministic routing is the exact-match fast path; everything else is
the v2 tool-calling router (validated by `scripts/run_routing_eval.py
--mode v2`, which needs a live LLM key — run it after any SKILL.md, router,
or model change).

These gates protect what CI can check without a network:

1. every case tagged `fast-path` still routes via the fast path, correctly;
2. nothing is EVER deterministically misrouted (a tripwire against
   reintroducing keyword/regex capture);
3. no per-case regression against the committed baseline
   (roo/routing_eval/baseline.json) — run
   `scripts/run_routing_eval.py --write-baseline` when a change is
   intentional and commit the diff.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.routing_eval import runner


@pytest.fixture(scope="module")
def results():
    return runner.run_eval(mode="deterministic")


def test_fast_path_cases_route_deterministically_correct(results):
    failures = []
    checked = 0
    for result in results:
        if "fast-path" not in result.case.tags:
            continue
        checked += 1
        if result.verdict != "correct" or result.prediction.layer != "fast":
            failures.append(
                f"{result.case.id}: {result.verdict} via {result.prediction.layer} "
                f"(got={result.prediction.skill}, expected={result.case.expect_skill})"
            )
        elif result.action_verdict == "wrong":
            failures.append(
                f"{result.case.id}: action {result.prediction.action!r} != "
                f"expected {result.case.expect_action!r}"
            )
    assert checked > 0, "no fast-path cases found in the dataset"
    assert not failures, "fast-path regressions:\n" + "\n".join(failures)


def test_nothing_is_deterministically_misrouted(results):
    failures = [
        f"{result.case.id}: {result.prediction.layer} -> {result.prediction.skill} "
        f"(expected {result.case.expect_skill or 'none'}) {result.case.text[:60]!r}"
        for result in results
        if result.verdict == "misroute"
    ]
    assert not failures, (
        "deterministic misroutes — did someone reintroduce keyword/regex routing?\n"
        + "\n".join(failures)
    )


def test_no_regression_vs_committed_baseline(results):
    baseline = runner.load_baseline()
    if baseline is None:
        pytest.skip("no committed baseline yet (run scripts/run_routing_eval.py --write-baseline)")
    if baseline.get("mode") != "deterministic":
        pytest.skip(f"baseline is {baseline.get('mode')}-mode; gate compares deterministic runs")
    regressions, _improvements = runner.compare_to_baseline(results, baseline)
    assert not regressions, (
        "routing regressions vs baseline (if intentional, re-run "
        "scripts/run_routing_eval.py --write-baseline and commit the diff):\n"
        + "\n".join(regressions)
    )
