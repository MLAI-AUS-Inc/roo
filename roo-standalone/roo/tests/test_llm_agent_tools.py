"""Unit tests for the bounded Responses API ward-agent loop."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.llm import OpenAIClient, ToolCallParseError


def _run(coro):
    return asyncio.run(coro)


class FakeResponses:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_agent_with_tools_executes_and_returns_final_text():
    reasoning = SimpleNamespace(type="reasoning")
    tool_call = SimpleNamespace(
        type="function_call",
        name="get_observations",
        arguments="{}",
        call_id="call-1",
    )
    first = SimpleNamespace(
        output=[reasoning, tool_call],
        output_text="",
        model="gpt-5.6-terra",
        usage=SimpleNamespace(input_tokens=12, output_tokens=4),
    )
    second = SimpleNamespace(
        output=[SimpleNamespace(type="message")],
        output_text="Heart rate 128.",
        model="gpt-5.6-terra",
        usage=SimpleNamespace(input_tokens=18, output_tokens=6),
    )
    client = OpenAIClient.__new__(OpenAIClient)
    client.model = "gpt-5.6-terra"
    fake = FakeResponses([first, second])
    client.client = SimpleNamespace(responses=fake)
    executed = []

    result = _run(client.agent_with_tools(
        [
            {"role": "system", "content": "Use exact tool data."},
            {"role": "user", "content": "Observations please."},
        ],
        [{"type": "function", "name": "get_observations"}],
        lambda name, arguments: executed.append((name, arguments)) or {"heart_rate": 128},
        reasoning_effort="low",
        safety_identifier="health-hack-test",
    ))

    assert result.content == "Heart rate 128."
    assert result.usage == {"prompt_tokens": 30, "completion_tokens": 10}
    assert result.tool_calls == [{"name": "get_observations", "arguments": {}}]
    assert executed == [("get_observations", {})]
    assert fake.calls[0]["tool_choice"] == "auto"
    assert fake.calls[0]["parallel_tool_calls"] is False
    assert fake.calls[0]["safety_identifier"] == "health-hack-test"
    assert fake.calls[0]["max_output_tokens"] == 700
    assert reasoning in fake.calls[1]["input"]
    assert {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": '{"heart_rate": 128}',
    } in fake.calls[1]["input"]


def test_openai_request_client_applies_bounded_timeout_and_zero_retries():
    class RootClient:
        def __init__(self):
            self.calls = []

        def with_options(self, **kwargs):
            self.calls.append(kwargs)
            return "bounded-client"

    client = OpenAIClient.__new__(OpenAIClient)
    root = RootClient()
    client.client = root

    selected = client._request_client({"timeout": 20})

    assert selected == "bounded-client"
    assert root.calls == [{"timeout": 20.0, "max_retries": 0}]


def test_responses_parse_forwards_reasoning_and_output_bounds():
    class ParsedResult(BaseModel):
        value: str

    class FakeParsedResponses:
        def __init__(self):
            self.calls = []

        async def parse(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(output_parsed=ParsedResult(value="ok"))

    client = OpenAIClient.__new__(OpenAIClient)
    client.model = "gpt-5.6-sol"
    responses = FakeParsedResponses()
    client.client = SimpleNamespace(responses=responses)

    result = _run(
        client.responses_parse(
            [
                {"role": "system", "content": "Return structured output."},
                {"role": "user", "content": "Create the task."},
            ],
            ParsedResult,
            model="gpt-5.6-sol",
            reasoning_effort="high",
            max_output_tokens=3_000,
            store=False,
        )
    )

    assert result.value == "ok"
    assert responses.calls[0]["model"] == "gpt-5.6-sol"
    assert responses.calls[0]["reasoning"] == {"effort": "high"}
    assert responses.calls[0]["max_output_tokens"] == 3_000
    assert responses.calls[0]["store"] is False


def test_reasoning_chat_uses_completion_bound_and_safety_identifier():
    class FakeCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Hello"))],
                model="gpt-5.6-terra",
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2),
            )

    client = OpenAIClient.__new__(OpenAIClient)
    client.model = "gpt-5.6-terra"
    completions = FakeCompletions()
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = _run(client.chat(
        [{"role": "user", "content": "Hello"}],
        model="gpt-5.6-terra",
        max_tokens=500,
        safety_identifier="health-hack-test",
    ))

    assert result.content == "Hello"
    assert completions.calls == [{
        "model": "gpt-5.6-terra",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_completion_tokens": 500,
        "n": 1,
        "safety_identifier": "health-hack-test",
    }]


def test_missing_tool_exception_never_contains_raw_model_content():
    raw_secret = "RAW_MODEL_CONTENT_MUST_NOT_APPEAR"

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=raw_secret,
                    tool_calls=[],
                ))],
                model="gpt-5.6-terra",
            )

    client = OpenAIClient.__new__(OpenAIClient)
    client.model = "gpt-5.6-terra"
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    with pytest.raises(ToolCallParseError) as captured:
        _run(client.chat_tools(
            [{"role": "user", "content": "hello"}],
            [{"type": "function", "name": "safe_tool"}],
        ))
    assert str(captured.value) == "model_tool_call_missing"
    assert raw_secret not in str(captured.value)


def test_malformed_tool_exception_never_contains_raw_arguments_or_name():
    raw_secret = "RAW_ARGUMENT_MUST_NOT_APPEAR"
    call = SimpleNamespace(
        type="function_call",
        name=f"unsafe-{raw_secret}",
        arguments='{"value":"' + raw_secret,
        call_id="call-unsafe",
    )
    response = SimpleNamespace(
        output=[call],
        output_text="",
        model="gpt-5.6-terra",
        usage=None,
    )
    client = OpenAIClient.__new__(OpenAIClient)
    client.model = "gpt-5.6-terra"
    client.client = SimpleNamespace(responses=FakeResponses([response]))

    with pytest.raises(ToolCallParseError) as captured:
        _run(client.agent_with_tools(
            [{"role": "user", "content": "hello"}],
            [{"type": "function", "name": "safe_tool"}],
            lambda name, arguments: {},
        ))
    assert str(captured.value) == "ward_tool_arguments_invalid_json"
    assert raw_secret not in str(captured.value)
