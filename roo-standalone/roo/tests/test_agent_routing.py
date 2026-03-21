import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
fake_executor_module = types.ModuleType("roo.skills.executor")
fake_executor_module.SkillExecutor = type("SkillExecutor", (), {})
fake_executor_module.SkillResult = type("SkillResult", (), {})
sys.modules.setdefault("roo.skills.executor", fake_executor_module)

from roo.agent import RooAgent
from roo.skills.loader import Skill


def _make_skill(name: str, trigger_keywords: list[str]) -> Skill:
    return Skill(
        name=name,
        description=name,
        content="",
        path=Path("."),
        trigger_keywords=trigger_keywords,
    )


def _make_agent() -> RooAgent:
    agent = object.__new__(RooAgent)
    agent.skills = [
        _make_skill(
            "content-factory",
            [
                "write me an article",
                "write an article",
                "write article",
                "research the best article",
                "blog post",
                "target keyword",
            ],
        ),
        _make_skill(
            "mlai-points",
            ["points", "balance", "coworking", "book", "task", "tasks", "reward", "rewards"],
        ),
    ]
    agent.skill_executor = SimpleNamespace()
    agent._thread_skill_context = {}
    return agent


def test_article_request_beats_points_task_trigger():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers(
        "write me an article about 'How to build an ai agent harness for long-running specific tasks'"
    )

    assert skill is not None
    assert skill.name == "content-factory"


def test_points_request_still_routes_to_points():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("create a task called 'Fix docs' worth 5 points")

    assert skill is not None
    assert skill.name == "mlai-points"


def test_request_points_phrase_routes_to_points():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("request 5 points for helping at the event")

    assert skill is not None
    assert skill.name == "mlai-points"


def test_promote_points_admin_phrase_routes_to_points():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("promote <@U123ABC> to roo points admin")

    assert skill is not None
    assert skill.name == "mlai-points"


def test_points_allowance_phrase_routes_to_points():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("change <@U123ABC> weekly points allowance to 150")

    assert skill is not None
    assert skill.name == "mlai-points"


def test_content_thread_context_keeps_follow_up_on_content(monkeypatch):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "1772971600.288239",
        domain="mlai.au",
        workflow="research",
    )

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for sticky content follow-up")

    monkeypatch.setattr("roo.agent.chat", fail_chat)

    skill = asyncio.run(
        agent._select_skill("write it for my domain", [], "C123", "1772971600.288239")
    )

    assert skill is not None
    assert skill.name == "content-factory"


def test_llm_router_uses_gpt_5_4(monkeypatch):
    agent = _make_agent()
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(content="content-factory")

    monkeypatch.setattr("roo.agent.chat", fake_chat)
    monkeypatch.setattr("roo.agent.get_settings", lambda: SimpleNamespace(ROUTER_MODEL="gpt-5.4"))

    skill = asyncio.run(agent._select_skill("help me decide what to do next", [], None, None))

    assert skill is not None
    assert skill.name == "content-factory"
    assert captured["model"] == "gpt-5.4"
    assert captured["reasoning_effort"] == "low"
