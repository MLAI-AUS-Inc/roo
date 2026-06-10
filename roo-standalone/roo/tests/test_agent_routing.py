import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
fake_executor_module = types.ModuleType("roo.skills.executor")
fake_executor_module.SkillExecutor = type("SkillExecutor", (), {})
fake_executor_module.SkillResult = type("SkillResult", (), {})
sys.modules.setdefault("roo.skills.executor", fake_executor_module)

from roo.agent import RooAgent
from roo.content_intent import parse_routing_intent
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
            [
                "points",
                "balance",
                "coworking",
                "book",
                "task",
                "tasks",
                "reward",
                "rewards",
                "topup",
                "top-up",
                "top up",
                "buy points",
                "buy roo points",
                "add points",
                "add roo points",
            ],
        ),
        _make_skill(
            "linear-meeting-actions",
            [
                "meeting actions",
                "meeting action items",
                "meeting notes to linear",
                "meeting summary to linear",
                "transcript to linear",
                "linear tasks from meeting",
                "create linear tickets from transcript",
                "extract action items",
                "sync meeting notes to linear",
            ],
        ),
        _make_skill(
            "github-integration",
            ["connect github", "github integration", "reconnect github", "github auth"],
        ),
        _make_skill(
            "luma-events",
            [
                "luma",
                "attendees",
                "guest list",
                "csv",
                "csv documents",
                "past csv documents",
                "mlai events",
            ],
        ),
        _make_skill(
            "mlai-data-query",
            [
                "data catalog",
                "database catalog",
                "query data",
                "vibe raising companies",
                "startup update drafts",
                "content factory jobs",
                "linear issues",
                "gmail messages",
            ],
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


def test_linear_meeting_tasks_route_to_linear_meeting_actions():
    agent = _make_agent()

    for text in [
        "turn this meeting summary into Linear tasks",
        "extract action items from this transcript and add them to Linear",
        "sync meeting notes to Linear project Alpha",
        "send this PDF to Linear as tasks",
        "create Linear issues from this image",
        "create a project update from this PDF",
        "summarize this meeting as a Linear project update",
        "do a project update in Linear",
        "create a to do item in the linear project 'venture studio' assign to Sonia",
        "create an issue in Linear project Venture Studio assigned to <@U123>",
        "add a Linear task to Venture Studio for Sonia",
    ]:
        skill = agent._select_skill_from_triggers(text)
        assert skill is not None
        assert skill.name == "linear-meeting-actions"


def test_linear_file_context_routes_short_attached_file_request():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers(
        "send this to Linear as tasks",
        has_file_context=True,
    )

    assert skill is not None
    assert skill.name == "linear-meeting-actions"


def test_linear_thread_reference_routes_when_thread_context_exists():
    agent = _make_agent()

    for text in [
        '@Roo add this to the Linear project called "BITGET EVENT"',
        "add this thread to Linear project BITGET EVENT",
        "put the above in Linear project called BITGET EVENT",
    ]:
        skill = agent._select_skill_from_triggers(
            text,
            has_thread_context=True,
        )
        assert skill is not None
        assert skill.name == "linear-meeting-actions"


def test_linear_thread_reference_does_not_route_without_context():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("add this to Linear")

    assert skill is None


def test_request_points_phrase_routes_to_points():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("request 5 points for helping at the event")

    assert skill is not None
    assert skill.name == "mlai-points"


def test_topup_phrases_route_to_points():
    agent = _make_agent()

    for text in [
        "/roo topup",
        "top up Roo Points",
        "buy 10 roo points",
        "add roo points",
        "I need more points",
    ]:
        skill = agent._select_skill_from_triggers(text)
        assert skill is not None
        assert skill.name == "mlai-points"


def test_coworking_booking_shortcuts_route_to_points():
    agent = _make_agent()

    for text in [
        "book me in",
        "book me in today",
        "check <@U123ABC> in today",
        "book <@U123ABC> in today",
        "also book <@U123ABC> in today",
    ]:
        skill = agent._select_skill_from_triggers(text)
        assert skill is not None
        assert skill.name == "mlai-points"


def test_luma_attendee_csv_phrase_routes_to_luma_events():
    agent = _make_agent()

    for text in [
        "can you give me past csv documents for the past 3 MLAI events",
        "give me a report for how many people registered for the april 29 event",
        "how many registrations were there for the 2026-04-29 event",
    ]:
        skill = agent._select_skill_from_triggers(text)
        assert skill is not None
        assert skill.name == "luma-events"


def test_coworking_report_wording_routes_to_points():
    agent = _make_agent()

    for text in [
        "coworking report last 3 months",
        "coworking summary last 6 months",
        "coworking overview from 2026-01-01 to 2026-03-31",
        "how many people used the coworking space this week",
        "how many people attended the office this week",
        "give me a report for how many people used the coworking space last week",
        "Roo how many peopel used the coworkign space last week and how does that usage compare to the week prior?",
        "which day was busiest for coworking last month",
        "how did coworking usage last week compare to the week prior",
        "show coworking trends for the last 3 months and recommendations",
    ]:
        skill = agent._select_skill_from_triggers(text)
        assert skill is not None
        assert skill.name == "mlai-points"


def test_data_catalog_request_routes_to_data_query():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("what data resources can Roo query?")

    assert skill is not None
    assert skill.name == "mlai-data-query"


def test_vibe_raising_data_request_routes_to_data_query():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("how many Vibe Raising companies do we have?")

    assert skill is not None
    assert skill.name == "mlai-data-query"


def test_content_factory_job_state_request_routes_to_data_query():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("which content factory jobs failed last week?")

    assert skill is not None
    assert skill.name == "mlai-data-query"


def test_startup_update_drafts_request_routes_to_data_query():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("show startup update drafts for my company")

    assert skill is not None
    assert skill.name == "mlai-data-query"


def test_synced_linear_issue_read_request_routes_to_data_query():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("show Linear issues synced for this startup")

    assert skill is not None
    assert skill.name == "mlai-data-query"


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


def test_natural_language_points_allowance_phrase_routes_to_points():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers(
        "increase the number of points <@U123ABC> can give out weekly to 48"
    )

    assert skill is not None
    assert skill.name == "mlai-points"


def test_revoke_points_admin_phrase_routes_to_points():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("remove <@U123ABC> as roo points admin")

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


def test_content_thread_context_keeps_scan_follow_up_on_content(monkeypatch):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "1772971600.288239",
        domain="woofya.com.au",
        workflow="scan",
    )

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for scan follow-up")

    monkeypatch.setattr("roo.agent.chat", fail_chat)

    skill = asyncio.run(agent._select_skill("scan it", [], "C123", "1772971600.288239"))

    assert skill is not None
    assert skill.name == "content-factory"


def test_content_thread_context_keeps_publish_pr_follow_up_on_content(monkeypatch):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "1772971600.288239",
        domain="birdpsychology.com.au",
        workflow="write",
        active_job_id="job-content-123",
    )

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for publish-pr follow-up")

    monkeypatch.setattr("roo.agent.chat", fail_chat)

    skill = asyncio.run(
        agent._select_skill("publish this article as a PR", [], "C123", "1772971600.288239")
    )

    assert skill is not None
    assert skill.name == "content-factory"


@pytest.mark.parametrize(
    "text",
    [
        "publish this article as a PR",
        "publish this bundle as a PR",
        "turn this bundle into a PR",
        "open a draft PR for this bundle",
        "push this bundle to PR",
        "push this article to PR",
    ],
)
def test_publish_pr_aliases_route_to_content_factory_thread_follow_up(monkeypatch, text: str):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "1772971600.288239",
        domain="birdpsychology.com.au",
        workflow="write",
        active_job_id="job-content-123",
    )

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for publish-pr follow-up")

    monkeypatch.setattr("roo.agent.chat", fail_chat)

    skill = asyncio.run(agent._select_skill(text, [], "C123", "1772971600.288239"))

    assert skill is not None
    assert skill.name == "content-factory"


def test_scan_domain_phrase_routes_to_content_factory():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("scan the domain woofya.com.au")

    assert skill is not None
    assert skill.name == "content-factory"


def test_reconnect_to_github_for_domain_routes_to_github_integration():
    routing = parse_routing_intent("reconnect to github for mlai.au")

    assert routing == {
        "skill_name": "github-integration",
        "params": {
            "domain": "mlai.au",
            "action": "reconnect",
        },
    }


@pytest.mark.parametrize(
    "text",
    [
        "authenticate with github for mlai.au",
        "authenticate github for mlai.au",
        "connect github again for mlai.au",
    ],
)
def test_github_reconnect_aliases_route_to_github_integration(text: str):
    routing = parse_routing_intent(text)

    assert routing == {
        "skill_name": "github-integration",
        "params": {
            "domain": "mlai.au",
            "action": "reconnect",
        },
    }


def test_scan_repo_for_domain_phrase_routes_to_content_factory():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers("scan the repo for the domain woofya.com.au")

    assert skill is not None
    assert skill.name == "content-factory"


def test_scan_domain_phrase_skips_llm_router(monkeypatch):
    agent = _make_agent()

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for domain scan routing")

    monkeypatch.setattr("roo.agent.chat", fail_chat)

    skill = asyncio.run(agent._select_skill("scan the domain woofya.com.au", [], None, None))

    assert skill is not None
    assert skill.name == "content-factory"


def test_handle_mention_normalizes_slack_link_and_passes_scan_params(monkeypatch):
    agent = _make_agent()
    captured = {}

    class FakeExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message="ok",
                data=kwargs.get("param_overrides"),
                blocks=None,
                suppress_post=False,
            )

    agent.skill_executor = FakeExecutor()

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for Slack link scans")

    monkeypatch.setattr("roo.agent.chat", fail_chat)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> scan the repo for the domain <http://woofya.com.au|woofya.com.au>",
            user_id="U05QPB483K9",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result["skill_used"] == "content-factory"
    assert captured["text"] == "scan the repo for the domain woofya.com.au"
    assert captured["param_overrides"] == {
        "action": "scan",
        "domain": "woofya.com.au",
    }


def test_handle_mention_parses_delegated_scan_and_passes_identity_overrides(monkeypatch):
    agent = _make_agent()
    captured = {}

    class FakeExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message="ok",
                data=kwargs.get("param_overrides"),
                blocks=None,
                suppress_post=False,
            )

    agent.skill_executor = FakeExecutor()

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for delegated scans")

    monkeypatch.setattr("roo.agent.chat", fail_chat)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> scan the repo for the domain woofya.com.au as <@U0AQV5X9G0J>",
            user_id="U05QPB483K9",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result["skill_used"] == "content-factory"
    assert captured["text"] == "scan the repo for the domain woofya.com.au"
    assert captured["param_overrides"] == {
        "action": "scan",
        "domain": "woofya.com.au",
        "requested_by_slack_user_id": "U05QPB483K9",
        "effective_slack_user_id": "U0AQV5X9G0J",
    }


def test_handle_mention_rejects_unauthorized_delegation(monkeypatch):
    agent = _make_agent()

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for delegation denials")

    monkeypatch.setattr("roo.agent.chat", fail_chat)
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> scan mlai.au as <@U0AQV5X9G0J>",
            user_id="U123OTHER",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result == {
        "message": "Only <@U05QPB483K9> can run Content Factory as another user.",
        "skill_used": "content-factory",
        "data": None,
    }


def test_handle_mention_keeps_delegated_target_sticky_within_thread(monkeypatch):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "123.456",
        domain="studynash.co",
        workflow="scan",
        requested_by_slack_user_id="U05QPB483K9",
        effective_slack_user_id="U0AQV5X9G0J",
    )
    captured = {}

    class FakeExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message="ok",
                data=kwargs.get("param_overrides"),
                blocks=None,
                suppress_post=False,
            )

    agent.skill_executor = FakeExecutor()

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for sticky delegated follow-up")

    monkeypatch.setattr("roo.agent.chat", fail_chat)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> publish this article as a PR",
            user_id="U05QPB483K9",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result["skill_used"] == "content-factory"
    assert captured["param_overrides"] == {
        "action": "publish_pr",
        "domain": "studynash.co",
        "requested_by_slack_user_id": "U05QPB483K9",
        "effective_slack_user_id": "U0AQV5X9G0J",
    }


def test_handle_mention_passes_publish_pr_job_from_thread_context(monkeypatch):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "123.456",
        domain="birdpsychology.com.au",
        workflow="write",
        active_job_id="job-content-123",
    )
    captured = {}

    class FakeExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message="ok",
                data=kwargs.get("param_overrides"),
                blocks=None,
                suppress_post=False,
            )

    agent.skill_executor = FakeExecutor()

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for publish-pr follow-up")

    monkeypatch.setattr("roo.agent.chat", fail_chat)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text="<@U090FV0GTT4> publish this article as a PR",
            user_id="U05QPB483K9",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result["skill_used"] == "content-factory"
    assert captured["param_overrides"] == {
        "action": "publish_pr",
        "domain": "birdpsychology.com.au",
        "job_id": "job-content-123",
    }


@pytest.mark.parametrize(
    "text",
    [
        "<@U090FV0GTT4> publish this article as a PR",
        "<@U090FV0GTT4> push this bundle to PR",
    ],
)
def test_handle_mention_passes_publish_pr_job_from_thread_context_aliases(monkeypatch, text: str):
    agent = _make_agent()
    agent.remember_thread_context(
        "content-factory",
        "C123",
        "123.456",
        domain="birdpsychology.com.au",
        workflow="write",
        active_job_id="job-content-123",
    )
    captured = {}

    class FakeExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message="ok",
                data=kwargs.get("param_overrides"),
                blocks=None,
                suppress_post=False,
            )

    agent.skill_executor = FakeExecutor()

    async def fail_chat(*args, **kwargs):
        raise AssertionError("LLM router should not be needed for publish-pr follow-up")

    monkeypatch.setattr("roo.agent.chat", fail_chat)
    monkeypatch.setattr("roo.agent.get_thread_messages", lambda channel, thread_ts: [])
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "U090FV0GTT4")

    result = asyncio.run(
        agent.handle_mention(
            text=text,
            user_id="U05QPB483K9",
            channel_id="C123",
            thread_ts="123.456",
        )
    )

    assert result["skill_used"] == "content-factory"
    assert captured["param_overrides"] == {
        "action": "publish_pr",
        "domain": "birdpsychology.com.au",
        "job_id": "job-content-123",
    }


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
