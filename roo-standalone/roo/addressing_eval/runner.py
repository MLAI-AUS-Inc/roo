"""Run labelled Slack addressedness cases without executing Roo skills."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import yaml

from ..addressing import decide_addressing


DEFAULT_CASES_PATH = Path(__file__).with_name("cases.yaml")


async def run_addressing_eval(
    *,
    cases_path: Path = DEFAULT_CASES_PATH,
    model: Optional[str] = None,
    min_confidence: float = 0.90,
) -> dict:
    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or []
    results = []
    for index, case in enumerate(cases):
        decision = await decide_addressing(
            text=str(case.get("text") or ""),
            user_id=str(case.get("user_id") or "USAM"),
            bot_user_id=str(case.get("bot_user_id") or "UROO"),
            history=case.get("history") or [],
            current_message_ts=str(case.get("message_ts") or f"eval-{index}"),
            candidate_reason=str(case.get("candidate_reason") or "eval_candidate"),
            explicit_mention=bool(case.get("explicit_mention")),
            min_implicit_confidence=min_confidence,
            indirect_mention_confidence=min_confidence,
            model=model,
        )
        actual = "respond" if decision.should_respond else "ignore"
        expected = str(case.get("expect") or "")
        results.append(
            {
                "id": case.get("id") or f"case-{index}",
                "expected": expected,
                "actual": actual,
                "passed": actual == expected,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "source": decision.source,
            }
        )
    passed = sum(1 for result in results if result["passed"])
    return {
        "summary": {"passed": passed, "total": len(results)},
        "results": results,
    }


def format_eval_report(report: dict) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False)
