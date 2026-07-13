"""Unit tests for the bounded Responses API ward-agent loop."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.llm import OpenAIClient


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
