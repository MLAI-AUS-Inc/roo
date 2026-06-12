"""Catalog hygiene gate — the SKILL.md files ARE the router; lint them in CI.

Hermetic: loads the real skills/*/SKILL.md and the eval dataset, no LLM calls.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.routing_eval import runner
from roo import router


def _skills():
    return runner.build_agent().skills


def test_catalog_lints_clean():
    problems = router.lint_catalog(_skills())
    assert not problems, "SKILL.md catalog problems:\n" + "\n".join(problems)


def test_catalog_stays_within_token_budget():
    """The whole tool catalog rides on every routed message — keep it bounded."""
    tools, _ = router.build_tools(_skills(), channel_name=None)
    import json

    approx_tokens = len(json.dumps(tools)) / 4
    assert approx_tokens < 6000, (
        f"tool catalog ≈{approx_tokens:.0f} tokens (budget 6000) — trim SKILL.md "
        "routing descriptions/examples"
    )


def test_eval_case_actions_exist_in_skill_manifests():
    """Cross-check the eval dataset against the SKILL.md action enums.

    Catches drift in both directions: a case expecting an action a skill no
    longer declares, or a renamed action leaving stale expectations behind.
    """
    skills_by_name = {skill.name: skill for skill in _skills()}
    failures = []
    for case in runner.load_cases():
        if not case.expect_skill or not case.expect_action:
            continue
        skill = skills_by_name.get(case.expect_skill)
        if skill is None:
            failures.append(f"{case.id}: unknown skill {case.expect_skill}")
            continue
        action_names = skill.action_names()
        if action_names and case.expect_action not in action_names:
            failures.append(
                f"{case.id}: expected action {case.expect_action!r} not declared by "
                f"{case.expect_skill} (has {action_names})"
            )
    assert not failures, "dataset/manifest drift:\n" + "\n".join(failures)
