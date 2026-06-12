"""Routing eval harness.

Replays labelled routing cases (roo/routing_eval/cases/*.yaml) through Roo's
real routing code and scores the decisions.

Two modes:

- deterministic: replays ONLY the pre-LLM funnel (fast path, intent regexes,
  looks-like heuristics, keyword scoring) exactly in handle_mention order.
  Hermetic — no network, no API keys. A case that reaches the LLM router is
  scored as a "fallthrough", which is acceptable (the LLM gets to decide) but
  never as deterministically correct.

- full: same funnel, then the real LLM fallback (RooAgent._llm_select_skill)
  for cases that fell through. Needs an LLM API key (OPENAI_API_KEY /
  GOOGLE_API_KEY) in the environment or .env.

Verdicts per case:
- correct          decided skill == expected skill
- ok-fallthrough   undecided, and expected skill is "none" (good: the message
                   should reach general chat / the LLM rather than a skill)
- fallthrough      undecided, but a skill was expected (neutral in
                   deterministic mode; the LLM layer is responsible for it)
- misroute         decided skill != expected skill (always bad)

Gates (see roo/tests/test_routing_eval_gate.py):
- every case tagged `blessed` must be `correct` (and action-correct when both
  expected and predicted actions are present)
- no case tagged `misroute-guard` may be a `misroute`
- no per-case regression against the committed baseline.json
"""
from __future__ import annotations

import asyncio
import importlib
import io
import json
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = Path(__file__).resolve().parent / "cases"
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"

VERDICT_RANK = {"misroute": 0, "fallthrough": 1, "ok-fallthrough": 2, "correct": 3}


def _ensure_repo_on_path() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))


def _ensure_real_frontmatter():
    """Undo the lightweight `frontmatter` stub some unit tests install.

    roo/tests/test_agent_routing.py does sys.modules.setdefault("frontmatter",
    SimpleNamespace(...)) so it can import the agent without the dependency.
    When the eval runs in the same pytest session we need the real library to
    load SKILL.md files.
    """
    stub = sys.modules.get("frontmatter")
    if stub is not None and not getattr(stub, "__file__", None):
        sys.modules.pop("frontmatter")
    real = importlib.import_module("frontmatter")
    loader_mod = sys.modules.get("roo.skills.loader")
    if loader_mod is not None:
        loader_mod.frontmatter = real
    return real


@dataclass
class RoutingCase:
    id: str
    text: str
    expect_skill: Optional[str]  # None means "no skill / general chat"
    expect_action: Optional[str] = None
    expect_params: Dict[str, Any] = field(default_factory=dict)
    channel: Optional[str] = None
    thread: Dict[str, Any] = field(default_factory=dict)
    files: bool = False
    tags: List[str] = field(default_factory=list)


@dataclass
class Prediction:
    layer: str
    skill: Optional[str]
    action: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CaseResult:
    case: RoutingCase
    prediction: Prediction
    verdict: str
    action_verdict: Optional[str] = None  # "correct" | "wrong" | None (unchecked)


def load_cases(cases_dir: Path = CASES_DIR) -> List[RoutingCase]:
    cases: List[RoutingCase] = []
    seen_ids = set()
    for path in sorted(cases_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text()) or []
        if not isinstance(raw, list):
            raise ValueError(f"{path.name}: top level must be a list of cases")
        for entry in raw:
            case_id = entry.get("id")
            if not case_id:
                raise ValueError(f"{path.name}: case missing 'id': {entry}")
            if case_id in seen_ids:
                raise ValueError(f"duplicate case id: {case_id}")
            seen_ids.add(case_id)
            expect = entry.get("expect") or {}
            if "skill" not in expect:
                raise ValueError(f"{case_id}: expect.skill is required (use 'none')")
            expected_skill = expect["skill"]
            if expected_skill in (None, "none", "null"):
                expected_skill = None
            cases.append(
                RoutingCase(
                    id=case_id,
                    text=str(entry.get("text") or ""),
                    expect_skill=expected_skill,
                    expect_action=expect.get("action"),
                    expect_params=dict(expect.get("params_subset") or {}),
                    channel=entry.get("channel"),
                    thread=dict(entry.get("thread") or {}),
                    files=bool(entry.get("files")),
                    tags=list(entry.get("tags") or []),
                )
            )
    if not cases:
        raise ValueError(f"no cases found in {cases_dir}")
    return cases


def build_agent():
    """Build a RooAgent with the real skill catalog but no Slack/executor wiring."""
    _ensure_repo_on_path()
    _ensure_real_frontmatter()
    from roo.agent import RooAgent
    from roo.skills.loader import load_skills

    agent = object.__new__(RooAgent)
    with redirect_stdout(io.StringIO()):
        agent.skills = load_skills(REPO_ROOT / "skills")
    agent.skill_executor = None
    agent._thread_skill_context = {}
    if not agent.skills:
        raise RuntimeError(f"no skills loaded from {REPO_ROOT / 'skills'}")
    return agent


def validate_cases(cases: List[RoutingCase], agent) -> None:
    known = {skill.name for skill in agent.skills}
    for case in cases:
        if case.expect_skill is not None and case.expect_skill not in known:
            raise ValueError(
                f"{case.id}: expected skill '{case.expect_skill}' is not a loaded skill "
                f"(known: {sorted(known)})"
            )


def _clean(text: str) -> str:
    """Mirror RooAgent._clean_mention minus the bot-mention strip."""
    from roo.content_intent import normalize_slack_text

    return " ".join(normalize_slack_text(text).split()).strip()


def _thread_context(case: RoutingCase) -> Optional[Dict[str, Any]]:
    if not case.thread:
        return None
    return {
        "skill_name": case.thread.get("last_skill"),
        "domain": case.thread.get("domain"),
        "workflow": case.thread.get("workflow"),
        "active_job_id": case.thread.get("job_id"),
        "updated_at": datetime.now(timezone.utc),
    }


def predict_deterministic(agent, case: RoutingCase) -> Prediction:
    """Replay the pre-LLM funnel in RooAgent.handle_mention order."""
    from roo.content_intent import parse_routing_intent

    clean = _clean(case.text)
    thread_context = _thread_context(case)

    # 1. fast path (exact commands)
    fast_action = agent._match_fast_path(clean)
    if fast_action:
        return Prediction(layer="fast", skill="mlai-points", action=fast_action)

    # 2. intent regexes (content_intent.parse_routing_intent)
    intent = parse_routing_intent(
        clean,
        thread_skill_name=(thread_context or {}).get("skill_name"),
        thread_domain=(thread_context or {}).get("domain"),
        thread_job_id=(thread_context or {}).get("active_job_id"),
    )
    if intent:
        params = dict(intent.get("params") or {})
        return Prediction(
            layer="intent-regex",
            skill=intent["skill_name"],
            action=params.get("action"),
            params=params,
        )

    # 3. looks-like heuristics, thread follow-up, keyword scoring
    skill, layer = agent._select_skill_from_triggers_detail(
        clean,
        thread_context,
        has_file_context=case.files,
        channel_name=case.channel,
    )
    if skill:
        return Prediction(layer=layer or "triggers", skill=skill.name)

    return Prediction(layer="llm-fallthrough", skill=None)


async def _predict_full_async(agent, case: RoutingCase, deterministic: Prediction) -> Prediction:
    if deterministic.skill is not None:
        return deterministic
    clean = _clean(case.text)
    skill = await agent._llm_select_skill(
        clean,
        [],
        channel_name=case.channel,
        thread_context=_thread_context(case),
    )
    if skill:
        return Prediction(layer="llm", skill=skill.name)
    return Prediction(layer="llm-none", skill=None)


async def _predict_v2_async(agent, case: RoutingCase) -> Prediction:
    """Router v2: fast path stays in front; everything else is one tool call."""
    from roo import router as router_v2

    clean = _clean(case.text)
    fast_action = agent._match_fast_path(clean)
    if fast_action:
        return Prediction(layer="fast", skill="mlai-points", action=fast_action)

    decision = await router_v2.route(
        clean,
        skills=agent.skills,
        channel_name=case.channel,
        thread_history=[],
        thread_hint=_thread_context(case),
        file_names=["attached-file"] if case.files else None,
    )
    if decision.skill:
        return Prediction(
            layer="v2", skill=decision.skill, action=decision.action, params=decision.params
        )
    if decision.is_clarification:
        return Prediction(layer="v2-clarify", skill=None)
    if decision.source == "error":
        return Prediction(layer="v2-error", skill=None)
    return Prediction(layer="v2-chat", skill=None)


def score(case: RoutingCase, prediction: Prediction) -> CaseResult:
    if prediction.skill is None:
        verdict = "ok-fallthrough" if case.expect_skill is None else "fallthrough"
    elif case.expect_skill is None:
        verdict = "misroute"
    elif prediction.skill == case.expect_skill:
        verdict = "correct"
    else:
        verdict = "misroute"

    action_verdict = None
    if verdict == "correct" and case.expect_action and prediction.action:
        action_verdict = "correct" if prediction.action == case.expect_action else "wrong"

    # params subset check folds into the action verdict reporting
    if verdict == "correct" and case.expect_params and prediction.params:
        for key, value in case.expect_params.items():
            if key in prediction.params and str(prediction.params[key]) != str(value):
                action_verdict = "wrong"

    return CaseResult(case=case, prediction=prediction, verdict=verdict, action_verdict=action_verdict)


def run_eval(mode: str = "deterministic", tag_filter: Optional[str] = None) -> List[CaseResult]:
    agent = build_agent()
    cases = load_cases()
    validate_cases(cases, agent)
    if tag_filter:
        cases = [case for case in cases if tag_filter in case.tags]
        if not cases:
            raise ValueError(f"no cases match tag '{tag_filter}'")

    results: List[CaseResult] = []
    if mode == "deterministic":
        for case in cases:
            results.append(score(case, predict_deterministic(agent, case)))
        return results

    if mode in ("full", "v2"):
        import os

        os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-routing-eval-dummy")
        os.environ.setdefault("SLACK_SIGNING_SECRET", "routing-eval-dummy")

        async def _run() -> List[CaseResult]:
            collected: List[CaseResult] = []
            for case in cases:
                if mode == "v2":
                    prediction = await _predict_v2_async(agent, case)
                else:
                    deterministic = predict_deterministic(agent, case)
                    prediction = await _predict_full_async(agent, case, deterministic)
                collected.append(score(case, prediction))
            return collected

        return asyncio.run(_run())

    raise ValueError(f"unknown mode: {mode}")


def summarize(results: List[CaseResult]) -> Dict[str, Any]:
    counts: Dict[str, int] = {key: 0 for key in VERDICT_RANK}
    by_skill: Dict[str, Dict[str, int]] = {}
    action_checked = 0
    action_correct = 0
    for result in results:
        counts[result.verdict] += 1
        expected = result.case.expect_skill or "none"
        bucket = by_skill.setdefault(expected, {key: 0 for key in VERDICT_RANK})
        bucket[result.verdict] += 1
        if result.action_verdict is not None:
            action_checked += 1
            if result.action_verdict == "correct":
                action_correct += 1
    return {
        "total": len(results),
        "counts": counts,
        "by_expected_skill": by_skill,
        "action": {"checked": action_checked, "correct": action_correct},
    }


def report(results: List[CaseResult], *, verbose: bool = False) -> str:
    summary = summarize(results)
    lines: List[str] = []
    counts = summary["counts"]
    decided = counts["correct"] + counts["misroute"]
    lines.append(
        f"cases={summary['total']}  correct={counts['correct']}  "
        f"misroute={counts['misroute']}  fallthrough={counts['fallthrough']}  "
        f"ok-fallthrough={counts['ok-fallthrough']}"
    )
    if decided:
        lines.append(
            f"decided-accuracy={counts['correct']}/{decided} "
            f"({100.0 * counts['correct'] / decided:.1f}%)"
        )
    overall = counts["correct"] + counts["ok-fallthrough"]
    lines.append(
        f"overall-accuracy={overall}/{summary['total']} "
        f"({100.0 * overall / summary['total']:.1f}%)"
        "  [correct + acceptable-none; the headline number for v2/full modes]"
    )
    action = summary["action"]
    if action["checked"]:
        lines.append(f"action-accuracy={action['correct']}/{action['checked']}")

    misroutes = [result for result in results if result.verdict == "misroute"]
    if misroutes:
        lines.append("\nMISROUTES (deterministically wrong):")
        for result in misroutes:
            lines.append(
                f"  {result.case.id:<38} {result.prediction.layer:<18} "
                f"-> {result.prediction.skill or 'none':<24} expected {result.case.expect_skill or 'none':<22} "
                f"{result.case.text[:60]!r}"
            )

    action_wrong = [result for result in results if result.action_verdict == "wrong"]
    if action_wrong:
        lines.append("\nACTION MISMATCHES (right skill, wrong action/params):")
        for result in action_wrong:
            lines.append(
                f"  {result.case.id:<38} got {result.prediction.action or result.prediction.params} "
                f"expected {result.case.expect_action or result.case.expect_params}"
            )

    if verbose:
        fallthroughs = [result for result in results if result.verdict == "fallthrough"]
        if fallthroughs:
            lines.append("\nFALLTHROUGHS (reach the LLM router; skill expected):")
            for result in fallthroughs:
                lines.append(
                    f"  {result.case.id:<38} expected {result.case.expect_skill:<22} "
                    f"{result.case.text[:60]!r}"
                )

    lines.append("\nPer expected skill (correct/misroute/fallthrough/ok):")
    for skill_name in sorted(summary["by_expected_skill"]):
        bucket = summary["by_expected_skill"][skill_name]
        lines.append(
            f"  {skill_name:<24} {bucket['correct']:>3} / {bucket['misroute']:>3} "
            f"/ {bucket['fallthrough']:>3} / {bucket['ok-fallthrough']:>3}"
        )
    return "\n".join(lines)


def to_baseline(results: List[CaseResult], mode: str) -> Dict[str, Any]:
    return {
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": summarize(results)["counts"],
        "action": summarize(results)["action"],
        "cases": {
            result.case.id: {
                "verdict": result.verdict,
                "layer": result.prediction.layer,
                "skill": result.prediction.skill,
                "action": result.prediction.action,
            }
            for result in results
        },
    }


def load_baseline(path: Path = BASELINE_PATH) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def compare_to_baseline(
    results: List[CaseResult], baseline: Dict[str, Any]
) -> Tuple[List[str], List[str]]:
    """Return (regressions, improvements) as human-readable strings."""
    regressions: List[str] = []
    improvements: List[str] = []
    baseline_cases = baseline.get("cases", {})
    for result in results:
        previous = baseline_cases.get(result.case.id)
        if not previous:
            continue  # new case — no baseline yet
        old_rank = VERDICT_RANK.get(previous.get("verdict"), 0)
        new_rank = VERDICT_RANK.get(result.verdict, 0)
        if new_rank < old_rank:
            regressions.append(
                f"{result.case.id}: {previous.get('verdict')} -> {result.verdict} "
                f"(now {result.prediction.layer} -> {result.prediction.skill})"
            )
        elif new_rank > old_rank:
            improvements.append(f"{result.case.id}: {previous.get('verdict')} -> {result.verdict}")
    return regressions, improvements
