from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Generic, Literal, TypeVar

from openai import APITimeoutError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .llm import ToolCallParseError, get_llm_client

logger = logging.getLogger(__name__)

LINEAR_SKILL_MODEL = "gpt-6.2-sol"
LINEAR_PROJECT_REASONING_MODEL = "gpt-6.2-astra"

LinearReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]
LinearInferenceStage = Literal[
    "direct_issue",
    "meeting_actions",
    "contextual_issue",
    "project_reconciliation",
    "effort_sizing",
    "studio_effort",
]

_EFFORT_ORDER: tuple[LinearReasoningEffort, ...] = (
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
_EFFORT_TIMEOUT_SECONDS: dict[LinearReasoningEffort, float] = {
    "low": 30.0,
    "medium": 45.0,
    "high": 75.0,
    "xhigh": 120.0,
    "max": 180.0,
}
_STAGE_TIMEOUT_FLOORS: dict[
    LinearInferenceStage, dict[LinearReasoningEffort, float]
] = {
    # Meeting extraction returns a comparatively large structured payload. Give
    # ordinary and complex chunks enough time to finish before progressively
    # reducing their scope in the caller.
    "meeting_actions": {"medium": 60.0, "high": 90.0},
}


def _linear_timeout_seconds(
    stage: LinearInferenceStage,
    effort: LinearReasoningEffort,
) -> float:
    return max(
        _EFFORT_TIMEOUT_SECONDS[effort],
        _STAGE_TIMEOUT_FLOORS.get(stage, {}).get(effort, 0.0),
    )


_STAGE_MAX_OUTPUT_TOKENS: dict[LinearInferenceStage, int] = {
    "direct_issue": 4_000,
    "meeting_actions": 8_000,
    "contextual_issue": 2_500,
    "project_reconciliation": 8_000,
    "effort_sizing": 16_000,
    "studio_effort": 16_000,
}
_STUDIO_STRUCTURED_RECOVERY_MAX_OUTPUT_TOKENS = 24_000
_STAGE_BASE_EFFORT: dict[LinearInferenceStage, LinearReasoningEffort] = {
    "direct_issue": "low",
    "meeting_actions": "medium",
    "contextual_issue": "high",
    "project_reconciliation": "medium",
    "effort_sizing": "high",
    "studio_effort": "high",
}
_STAGE_MAX_INITIAL_EFFORT: dict[LinearInferenceStage, LinearReasoningEffort] = {
    "direct_issue": "high",
    "meeting_actions": "xhigh",
    "contextual_issue": "xhigh",
    "project_reconciliation": "high",
    "effort_sizing": "xhigh",
    "studio_effort": "xhigh",
}


class LinearCandidate(BaseModel):
    """Structured issue candidate shared by direct, meeting, and contextual flows."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=4000)
    work_status: Literal["open", "completed", "cancelled", "duplicate"] = "open"
    completed_work: str = Field(default="", max_length=4000)
    remaining_work: str = Field(default="", max_length=4000)
    available_artifacts: list[str] = Field(default_factory=list, max_length=20)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    owner_hint: str = Field(default="", max_length=240)
    project_hint: str = Field(default="", max_length=240)
    team_hint: str = Field(default="", max_length=240)
    due_expression: str | None = Field(default=None, max_length=240)
    due_date: str | None = Field(default=None, max_length=32)
    priority: int = Field(default=3, ge=0, le=4)
    evidence: str = Field(default="", max_length=1000)
    evidence_message_ts: str | None = Field(default=None, max_length=64)
    explicit_commitment: bool = True
    source_label: str = Field(default="", max_length=240)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class LinearDirectIssueBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[LinearCandidate] = Field(default_factory=list, max_length=30)


ProjectFactKind = Literal["progress", "requirement", "decision", "blocker", "next_step", "question", "health"]
ProjectHealth = Literal["onTrack", "atRisk", "offTrack"]


class LinearMeetingFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ProjectFactKind
    text: str = Field(min_length=1, max_length=500)
    evidence: str = Field(min_length=1, max_length=800)
    project_hint: str = Field(default="", max_length=240)
    health: ProjectHealth | None = None


class LinearMeetingCandidate(LinearCandidate):
    explicit_commitment: bool = False


class LinearMeetingActionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_items: list[LinearMeetingCandidate] = Field(default_factory=list, max_length=50)
    project_facts: list[LinearMeetingFact] = Field(default_factory=list, max_length=40)


_MEETING_QUOTE_TYPOGRAPHY = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def normalize_meeting_quote(text: str) -> str:
    """Compare verbatim quotes without requiring identical line wraps or smart quotes."""
    return " ".join(text.translate(_MEETING_QUOTE_TYPOGRAPHY).split())


def validate_meeting_fact_quotes(batch: LinearMeetingActionBatch, text: str) -> None:
    """Reject invented quotes while tolerating line wraps from PDFs and Slack."""
    source = normalize_meeting_quote(text)
    for finding in [*batch.action_items, *batch.project_facts]:
        quote = normalize_meeting_quote(finding.evidence)
        if not quote or quote not in source:
            raise LinearInferenceValidationError("Meeting finding is missing its original quote.")


class LinearContextualIssueResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue: LinearCandidate | None = None


@dataclass(frozen=True)
class LinearReasoningSignals:
    stage: LinearInferenceStage
    source_chars: int = 0
    source_count: int = 1
    batch_chunk_index: int = 1
    batch_chunk_count: int = 1
    recovery_depth: int = 0
    candidate_count: int = 1
    explicit_project: bool = True
    explicit_owner: bool = True
    ambiguity: bool = False
    partial_work: bool = False
    dependency_count: int = 0
    artifact_count: int = 0
    conflicting_context: bool = False
    allow_astra: bool = False


@dataclass(frozen=True)
class LinearReasoningDecision:
    stage: LinearInferenceStage
    effort: LinearReasoningEffort
    timeout_seconds: float
    complexity_score: int
    reasons: tuple[str, ...]
    retry: bool = False
    model: str = LINEAR_SKILL_MODEL


StructuredT = TypeVar("StructuredT", bound=BaseModel)


@dataclass(frozen=True)
class LinearInferenceResult(Generic[StructuredT]):
    value: StructuredT
    decision: LinearReasoningDecision
    attempts: int


class LinearInferenceValidationError(ValueError):
    """Structured output parsed but did not satisfy the workflow contract."""


class LinearInferenceTimeoutError(TimeoutError):
    """A bounded Linear inference call timed out before producing a result."""

    def __init__(
        self,
        *,
        decision: LinearReasoningDecision,
        signals: LinearReasoningSignals,
        attempt: int,
        duration_ms: int,
    ) -> None:
        self.decision = decision
        self.signals = signals
        self.attempt = attempt
        self.duration_ms = duration_ms
        super().__init__(
            "Linear inference timed out "
            f"(stage={signals.stage}, effort={decision.effort}, "
            f"attempt={attempt}, chunk={signals.batch_chunk_index}/"
            f"{signals.batch_chunk_count}, duration_ms={duration_ms})"
        )


def choose_linear_reasoning(signals: LinearReasoningSignals) -> LinearReasoningDecision:
    """Choose reasoning from inference difficulty, never from the XS-XL task label."""

    score = 0
    reasons: list[str] = []

    if signals.source_chars >= 20_000:
        score += 3
        reasons.append("very_long_context")
    elif signals.source_chars >= 8_000:
        score += 2
        reasons.append("long_context")
    elif signals.source_chars >= 3_000:
        score += 1
        reasons.append("moderate_context")

    if signals.source_count >= 4:
        score += 2
        reasons.append("many_sources")
    elif signals.source_count >= 2:
        score += 1
        reasons.append("multiple_sources")

    if signals.candidate_count >= 6:
        score += 2
        reasons.append("many_candidates")
    elif signals.candidate_count >= 2:
        score += 1
        reasons.append("multiple_candidates")

    if not signals.explicit_project:
        score += 1
        reasons.append("project_inference")
    if not signals.explicit_owner:
        score += 1
        reasons.append("owner_inference")
    if signals.ambiguity:
        score += 2
        reasons.append("ambiguous_scope")
    if signals.partial_work:
        score += 1
        reasons.append("remaining_work_reconstruction")
    if signals.dependency_count >= 2:
        score += 1
        reasons.append("multiple_dependencies")
    if signals.artifact_count >= 2:
        score += 1
        reasons.append("multiple_artifacts")
    if signals.conflicting_context:
        score += 3
        reasons.append("conflicting_context")

    bump = 2 if score >= 6 else 1 if score >= 3 else 0
    base_index = _EFFORT_ORDER.index(_STAGE_BASE_EFFORT[signals.stage])
    cap_index = _EFFORT_ORDER.index(_STAGE_MAX_INITIAL_EFFORT[signals.stage])
    effort = _EFFORT_ORDER[min(base_index + bump, cap_index)]
    timeout_seconds = _linear_timeout_seconds(signals.stage, effort)
    if signals.stage == "project_reconciliation" and (signals.candidate_count >= 25 or signals.source_chars >= 100_000):
        timeout_seconds = max(timeout_seconds, 180.0)
    return LinearReasoningDecision(
        stage=signals.stage,
        effort=effort,
        timeout_seconds=timeout_seconds,
        complexity_score=score,
        reasons=tuple(reasons) or ("stage_baseline",),
        model=(
            LINEAR_PROJECT_REASONING_MODEL
            if signals.stage == "project_reconciliation" and signals.allow_astra
            and (signals.conflicting_context or signals.ambiguity or signals.source_count > 1 or score >= 3)
            else LINEAR_SKILL_MODEL
        ),
    )


def escalate_linear_reasoning(
    decision: LinearReasoningDecision,
) -> LinearReasoningDecision:
    current_index = _EFFORT_ORDER.index(decision.effort)
    effort = _EFFORT_ORDER[min(current_index + 1, len(_EFFORT_ORDER) - 1)]
    return replace(
        decision,
        effort=effort,
        timeout_seconds=max(decision.timeout_seconds, _linear_timeout_seconds(decision.stage, effort)),
        reasons=(*decision.reasons, "structured_validation_retry"),
        retry=True,
    )


def recover_linear_structured_output(
    decision: LinearReasoningDecision,
) -> LinearReasoningDecision:
    """Trade reasoning depth for enough budget to emit the structured result."""

    current_index = _EFFORT_ORDER.index(decision.effort)
    effort = _EFFORT_ORDER[max(0, current_index - 1)]
    return replace(
        decision,
        effort=effort,
        timeout_seconds=max(
            decision.timeout_seconds,
            _linear_timeout_seconds(decision.stage, effort),
        ),
        reasons=(*decision.reasons, "structured_output_budget_recovery"),
        retry=True,
    )


def linear_safety_identifier(requester_id: str | None) -> str:
    digest = hashlib.sha256(str(requester_id or "unknown").encode("utf-8")).hexdigest()
    return f"roo-linear-{digest[:24]}"


def _log_linear_inference_event(
    level: int,
    event: str,
    *,
    decision: LinearReasoningDecision,
    signals: LinearReasoningSignals,
    attempt: int,
    max_output_tokens: int,
    duration_ms: int | None = None,
    error_type: str | None = None,
    error_diagnostics: dict[str, object] | None = None,
) -> None:
    payload = {
        "event": event,
        "stage": signals.stage,
        "model": decision.model,
        "reasoning_effort": decision.effort,
        "timeout_seconds": decision.timeout_seconds,
        "complexity_score": decision.complexity_score,
        "reasoning_reasons": list(decision.reasons),
        "attempt": attempt,
        "max_output_tokens": max_output_tokens,
        "source_chars": signals.source_chars,
        "source_count": signals.source_count,
        "batch_chunk_index": signals.batch_chunk_index,
        "batch_chunk_count": signals.batch_chunk_count,
        "recovery_depth": signals.recovery_depth,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if error_type:
        payload["error_type"] = error_type
    if error_diagnostics:
        safe_diagnostic_keys = {
            "status",
            "incomplete_reason",
            "error_code",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "output_item_types",
            "content_item_types",
        }
        payload["response_diagnostics"] = {
            key: value
            for key, value in error_diagnostics.items()
            if key in safe_diagnostic_keys
        }
    logger.log(
        level,
        "LINEAR_INFERENCE %s",
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
    )


async def run_linear_structured_inference(
    *,
    messages: list[dict[str, str]],
    response_format: type[StructuredT],
    signals: LinearReasoningSignals,
    safety_identifier: str | None = None,
    validator: Callable[[StructuredT], None] | None = None,
) -> LinearInferenceResult[StructuredT]:
    """Run the selected model with one bounded, failure-specific recovery attempt."""

    client = get_llm_client("openai")
    parse_method = getattr(client, "responses_parse", None)
    if parse_method is None:
        raise RuntimeError(
            "The OpenAI client does not support structured Responses output."
        )

    decision = choose_linear_reasoning(signals)
    retryable_errors = (
        LinearInferenceValidationError,
        ToolCallParseError,
        ValidationError,
    )
    last_error: Exception | None = None
    attempt_decision = decision
    attempt_max_output_tokens = _STAGE_MAX_OUTPUT_TOKENS[signals.stage]
    if signals.stage == "project_reconciliation" and signals.candidate_count >= 25:
        # Larger meetings must return a disposition for every candidate.
        attempt_max_output_tokens = 12_000
    attempt_messages = messages
    for attempt in range(2):
        started_at = time.monotonic()
        _log_linear_inference_event(
            logging.INFO,
            "started",
            decision=attempt_decision,
            signals=signals,
            attempt=attempt + 1,
            max_output_tokens=attempt_max_output_tokens,
        )
        try:
            parsed = await parse_method(
                attempt_messages,
                response_format,
                model=attempt_decision.model,
                reasoning_effort=attempt_decision.effort,
                timeout=attempt_decision.timeout_seconds,
                max_retries=0,
                max_output_tokens=attempt_max_output_tokens,
                safety_identifier=safety_identifier,
                store=False,
            )
            if not isinstance(parsed, response_format):
                parsed = response_format.model_validate(parsed)
            if validator is not None:
                validator(parsed)
            duration_ms = round((time.monotonic() - started_at) * 1000)
            _log_linear_inference_event(
                logging.INFO,
                "completed",
                decision=attempt_decision,
                signals=signals,
                attempt=attempt + 1,
                max_output_tokens=attempt_max_output_tokens,
                duration_ms=duration_ms,
            )
            return LinearInferenceResult(
                value=parsed,
                decision=attempt_decision,
                attempts=attempt + 1,
            )
        except APITimeoutError as exc:
            duration_ms = round((time.monotonic() - started_at) * 1000)
            _log_linear_inference_event(
                logging.WARNING,
                "timed_out",
                decision=attempt_decision,
                signals=signals,
                attempt=attempt + 1,
                max_output_tokens=attempt_max_output_tokens,
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
            )
            raise LinearInferenceTimeoutError(
                decision=attempt_decision,
                signals=signals,
                attempt=attempt + 1,
                duration_ms=duration_ms,
            ) from exc
        except retryable_errors as exc:
            last_error = exc
            duration_ms = round((time.monotonic() - started_at) * 1000)
            _log_linear_inference_event(
                logging.WARNING,
                "validation_retry" if attempt == 0 else "validation_failed",
                decision=attempt_decision,
                signals=signals,
                attempt=attempt + 1,
                max_output_tokens=attempt_max_output_tokens,
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
                error_diagnostics=getattr(exc, "diagnostics", None),
            )
            if attempt == 1:
                raise
            if signals.stage in {"meeting_actions", "project_reconciliation"} and isinstance(
                exc, LinearInferenceValidationError
            ):
                # These validators report a fixed contract violation without transcript or user data.
                attempt_messages = [
                    {"role": "system", "content": f"Correct this validation failure and return the full result. {exc}"},
                    *messages,
                ]
            if (
                signals.stage in {"effort_sizing", "studio_effort"}
                and isinstance(exc, ToolCallParseError)
                and str(exc) == "structured_response_missing"
            ):
                attempt_decision = recover_linear_structured_output(
                    attempt_decision
                )
                attempt_max_output_tokens = (
                    _STUDIO_STRUCTURED_RECOVERY_MAX_OUTPUT_TOKENS
                )
            else:
                attempt_decision = escalate_linear_reasoning(attempt_decision)

    assert last_error is not None
    raise last_error
