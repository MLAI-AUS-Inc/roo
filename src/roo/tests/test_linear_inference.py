from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx
import pytest
from openai import APITimeoutError
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import roo.linear_inference as inference_module
from roo.linear_inference import (
    LINEAR_SKILL_MODEL,
    LinearInferenceTimeoutError,
    LinearReasoningSignals,
    choose_linear_reasoning,
    run_linear_structured_inference,
)
from roo.llm import ToolCallParseError


class ExampleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1)


@pytest.mark.parametrize(
    ("signals", "expected_effort"),
    [
        (
            LinearReasoningSignals(
                stage="direct_issue",
                source_chars=240,
                explicit_project=True,
                explicit_owner=True,
            ),
            "low",
        ),
        (
            LinearReasoningSignals(
                stage="meeting_actions",
                source_chars=2_500,
                explicit_project=True,
                explicit_owner=True,
            ),
            "medium",
        ),
        (
            LinearReasoningSignals(
                stage="contextual_issue",
                source_chars=10_000,
                explicit_project=False,
                explicit_owner=False,
                ambiguity=True,
            ),
            "xhigh",
        ),
        (
            LinearReasoningSignals(
                stage="studio_effort",
                source_chars=4_000,
                source_count=4,
                candidate_count=2,
                partial_work=True,
                dependency_count=2,
                artifact_count=2,
            ),
            "xhigh",
        ),
    ],
)
def test_reasoning_policy_varies_by_inference_difficulty(
    signals,
    expected_effort,
):
    decision = choose_linear_reasoning(signals)

    assert decision.effort == expected_effort
    assert decision.effort != "max"


def test_meeting_reasoning_uses_extraction_specific_timeout_floors():
    ordinary = choose_linear_reasoning(
        LinearReasoningSignals(
            stage="meeting_actions",
            source_chars=2_500,
            explicit_project=True,
            explicit_owner=True,
        )
    )
    complex_chunk = choose_linear_reasoning(
        LinearReasoningSignals(
            stage="meeting_actions",
            source_chars=4_000,
            explicit_project=False,
            explicit_owner=False,
        )
    )

    assert (ordinary.effort, ordinary.timeout_seconds) == ("medium", 60.0)
    assert (complex_chunk.effort, complex_chunk.timeout_seconds) == ("high", 90.0)


@pytest.mark.asyncio
async def test_linear_gateway_always_uses_exact_sol_model(monkeypatch):
    providers = []
    calls = []

    class FakeClient:
        async def responses_parse(self, messages, response_format, **kwargs):
            calls.append(kwargs)
            return ExampleResult(value="ok")

    def fake_get_client(provider):
        providers.append(provider)
        return FakeClient()

    monkeypatch.setattr(inference_module, "get_llm_client", fake_get_client)

    result = await run_linear_structured_inference(
        messages=[{"role": "user", "content": "Create a clear task."}],
        response_format=ExampleResult,
        signals=LinearReasoningSignals(
            stage="direct_issue",
            source_chars=20,
            explicit_project=True,
            explicit_owner=True,
        ),
        safety_identifier="roo-linear-test",
    )

    assert result.value.value == "ok"
    assert providers == ["openai"]
    assert calls[0]["model"] == LINEAR_SKILL_MODEL == "gpt-5.6-sol"
    assert calls[0]["reasoning_effort"] == "low"
    assert calls[0]["timeout"] == 30.0
    assert calls[0]["max_retries"] == 0
    assert calls[0]["max_output_tokens"] == 4_000
    assert calls[0]["store"] is False


@pytest.mark.asyncio
async def test_linear_gateway_retries_once_and_only_retry_reaches_max(monkeypatch):
    calls = []

    class FakeClient:
        async def responses_parse(self, messages, response_format, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {}
            return ExampleResult(value="recovered")

    monkeypatch.setattr(
        inference_module,
        "get_llm_client",
        lambda provider: FakeClient(),
    )

    result = await run_linear_structured_inference(
        messages=[{"role": "user", "content": "Infer the contextual task."}],
        response_format=ExampleResult,
        signals=LinearReasoningSignals(
            stage="contextual_issue",
            source_chars=10_000,
            explicit_project=False,
            explicit_owner=False,
            ambiguity=True,
        ),
    )

    assert result.value.value == "recovered"
    assert result.attempts == 2
    assert [call["reasoning_effort"] for call in calls] == ["xhigh", "max"]
    assert result.decision.retry is True


@pytest.mark.asyncio
async def test_studio_missing_output_retries_with_less_reasoning_and_more_headroom(
    monkeypatch,
    caplog,
):
    calls = []

    class FakeClient:
        async def responses_parse(self, messages, response_format, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ToolCallParseError(
                    "structured_response_missing",
                    diagnostics={
                        "status": "incomplete",
                        "incomplete_reason": "max_output_tokens",
                        "output_tokens": 16_000,
                        "reasoning_tokens": 15_900,
                    },
                )
            return ExampleResult(value="recovered")

    monkeypatch.setattr(
        inference_module,
        "get_llm_client",
        lambda provider: FakeClient(),
    )

    with caplog.at_level(logging.INFO):
        result = await run_linear_structured_inference(
            messages=[{"role": "user", "content": "Size these Studio tasks."}],
            response_format=ExampleResult,
            signals=LinearReasoningSignals(
                stage="studio_effort",
                source_chars=40_000,
                source_count=45,
                candidate_count=3,
                ambiguity=True,
            ),
        )

    assert result.value.value == "recovered"
    assert result.attempts == 2
    assert [call["model"] for call in calls] == [
        "gpt-5.6-sol",
        "gpt-5.6-sol",
    ]
    assert [call["reasoning_effort"] for call in calls] == ["xhigh", "high"]
    assert [call["max_output_tokens"] for call in calls] == [16_000, 24_000]
    assert [call["timeout"] for call in calls] == [120.0, 120.0]
    assert result.decision.retry is True
    assert "structured_output_budget_recovery" in result.decision.reasons
    assert '"incomplete_reason":"max_output_tokens"' in caplog.text
    assert '"reasoning_tokens":15900' in caplog.text


@pytest.mark.asyncio
async def test_linear_gateway_surfaces_timeout_without_validation_escalation(
    monkeypatch,
    caplog,
):
    calls = []

    class FakeClient:
        async def responses_parse(self, messages, response_format, **kwargs):
            calls.append(kwargs)
            raise APITimeoutError(
                request=httpx.Request("POST", "https://api.openai.test/v1/responses")
            )

    monkeypatch.setattr(
        inference_module,
        "get_llm_client",
        lambda provider: FakeClient(),
    )

    signals = LinearReasoningSignals(
        stage="meeting_actions",
        source_chars=9_000,
        source_count=1,
        batch_chunk_index=2,
        batch_chunk_count=5,
        explicit_project=True,
        explicit_owner=True,
    )
    with (
        caplog.at_level(logging.INFO),
        pytest.raises(LinearInferenceTimeoutError) as error,
    ):
        await run_linear_structured_inference(
            messages=[{"role": "user", "content": "Extract the action items."}],
            response_format=ExampleResult,
            signals=signals,
        )

    assert len(calls) == 1
    assert calls[0]["model"] == LINEAR_SKILL_MODEL == "gpt-5.6-sol"
    assert calls[0]["reasoning_effort"] == "medium"
    assert calls[0]["timeout"] == 60.0
    assert calls[0]["max_retries"] == 0
    assert error.value.signals == signals
    assert error.value.attempt == 1
    assert '"event":"started"' in caplog.text
    assert '"event":"timed_out"' in caplog.text
    assert '"batch_chunk_index":2' in caplog.text
    assert '"batch_chunk_count":5' in caplog.text
    assert '"error_type":"APITimeoutError"' in caplog.text
