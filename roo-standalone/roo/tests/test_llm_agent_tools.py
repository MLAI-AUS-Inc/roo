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
    ))

    assert result.content == "Heart rate 128."
    assert result.usage == {"prompt_tokens": 30, "completion_tokens": 10}
    assert result.tool_calls == [{"name": "get_observations", "arguments": {}}]
    assert executed == [("get_observations", {})]
    assert fake.calls[0]["tool_choice"] == "auto"
    assert fake.calls[0]["parallel_tool_calls"] is False
    assert reasoning in fake.calls[1]["input"]
    assert {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": '{"heart_rate": 128}',
    } in fake.calls[1]["input"]
