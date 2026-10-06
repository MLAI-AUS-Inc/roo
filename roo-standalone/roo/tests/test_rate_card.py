"""Rate-card display and smart-award behavior using external-boundary doubles."""

from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from roo.clients import mlai_backend as backend_module
from roo.skills import executor as executor_module
from roo.skills.loader import load_skill_from_directory


RATE_CARD_ROW = {
    "name": "Newsletter", "alias": "newsletter", "points": 5,
    "description": "Write a newsletter", "is_active": True,
}
RATE_CARD_CATALOG = [
    RATE_CARD_ROW,
    {
        "name": "Workshop assistant", "alias": "workshop", "points": 18,
        "description": "Prep and delivery", "is_active": True,
    },
    {
        "name": "Grant full application", "alias": "grant", "points": 60,
        "description": "Write a full grant", "is_active": True,
    },
    {
        "name": "Door shift", "alias": "door", "points": 12,
        "description": "Registration at the door", "is_active": True,
    },
    {
        "name": "Zebra herding", "alias": "zebra", "points": 9,
        "description": "Herd the zebras", "is_active": True,
    },
]
FAILURE_MESSAGE = "I couldn't load the rate card just now. Please try again in a moment."
CATALOG_HEADER = "📋 **Standard Point Rates:**"
ASK_FOR_WORK = "What work should I estimate Roo points for?"


@pytest.fixture
def points_context(monkeypatch):
    # These are the external read/write boundaries; exercise the real executor.
    client = SimpleNamespace(
        get_rate_card=AsyncMock(return_value=[RATE_CARD_ROW]),
        get_admin_details=AsyncMock(return_value={"role": "admin"}),
        get_admin_allowance=AsyncMock(return_value={"remaining": 50, "allowance": 100}),
        get_user_by_slack_id=AsyncMock(return_value=42),
        award_points=AsyncMock(return_value={"new_balance": 10}),
        link_slack_user=AsyncMock(),
    )
    # Bind the real ID normalizer to the client double.
    client._clean_slack_id = MethodType(backend_module.MLAIBackendClient._clean_slack_id, client)
    send_dm = Mock()
    monkeypatch.setattr(backend_module, "MLAIBackendClient", lambda **kwargs: client)
    monkeypatch.setattr(executor_module, "get_settings", lambda: SimpleNamespace(
        MLAI_BACKEND_URL="https://backend.test", ROO_API_KEY="roo-test-key",
        MLAI_API_KEY="legacy-test-key", INTERNAL_API_KEY="different-admin-test-key",
    ))
    monkeypatch.setattr(executor_module, "send_dm", send_dm)
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UBOT")
    monkeypatch.setattr(executor_module, "get_bot_user_id", lambda: "UBOT")
    for name in ("chat", "embed", "get_llm_client"):
        monkeypatch.setattr(executor_module, name, Mock(side_effect=AssertionError("No provider calls")))
    executor = executor_module.SkillExecutor()

    async def execute(action="view_rate_card", text=None, **params):
        return await executor._execute_mlai_points(
            skill=SimpleNamespace(name="mlai-points"),
            text=text if text is not None else ("award <@U012ABCDEF> for Newsletter" if action in {"award", "award_points"} else "show rates"),
            params={"action": action, **params}, user_id="UADMIN",
            channel_id="CTEST", thread_ts="111.222",
        )

    return SimpleNamespace(client=client, send_dm=send_dm, executor=executor, execute=execute)


@pytest.mark.asyncio
async def test_view_rate_card_populated_formats_existing_fields(points_context):
    assert await points_context.execute() == (
        "📋 **Standard Point Rates:**\n\n• **Newsletter** (5 pts) - Write a newsletter"
    )


@pytest.mark.asyncio
async def test_view_rate_card_empty_has_distinct_copy(points_context):
    points_context.client.get_rate_card.return_value = []
    assert await points_context.execute() == "No active point rates are configured."


@pytest.mark.asyncio
async def test_view_rate_card_failure_uses_existing_executor_guard(points_context):
    points_context.client.get_rate_card.side_effect = backend_module.MLAIBackendUnavailableError("test error")
    assert await points_context.execute() == FAILURE_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["award_points", "award"])
async def test_smart_award_failure_has_no_side_effects(points_context, action):
    points_context.client.get_rate_card.side_effect = backend_module.MLAIBackendUnavailableError("test error")
    assert await points_context.execute(action, reason="Newsletter") == FAILURE_MESSAGE
    points_context.client.get_rate_card.assert_awaited_once()
    points_context.client.award_points.assert_not_awaited()
    points_context.client.link_slack_user.assert_not_awaited()
    points_context.client.get_user_by_slack_id.assert_not_awaited()
    points_context.send_dm.assert_not_called()


@pytest.mark.asyncio
async def test_smart_award_match_requires_confirmation(points_context):
    assert await points_context.execute("award_points", reason="Newsletter") == (
        "I found a match in the Rate Card: 'Newsletter' is worth 5 points. "
        "Should I award 5 points to <@U012ABCDEF>? (You have 50 pts left this week.)"
    )
    points_context.client.get_rate_card.assert_awaited_once()
    points_context.client.award_points.assert_not_awaited()
    points_context.client.link_slack_user.assert_not_awaited()
    points_context.send_dm.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("card", [[], [{**RATE_CARD_ROW, "name": "Gardening", "description": "Gardening"}]])
async def test_smart_award_empty_or_no_match_keeps_manual_amount_prompt(points_context, card):
    points_context.client.get_rate_card.return_value = card
    assert await points_context.execute("award_points", reason="Newsletter") == (
        'How many points should I award? (e.g., "award @user 5 points")'
    )
    points_context.client.get_rate_card.assert_awaited_once()
    points_context.client.award_points.assert_not_awaited()
    points_context.send_dm.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_amount_award_does_not_fetch_rate_card(points_context):
    result = await points_context.execute("award_points", points=5, reason="Newsletter")
    assert "Awarded 5 points" in result
    points_context.client.get_rate_card.assert_not_awaited()
    points_context.client.award_points.assert_awaited_once_with("UADMIN", "U012ABCDEF", 5, "Newsletter")


@pytest.mark.asyncio
async def test_unrelated_points_failure_keeps_general_guard(points_context, monkeypatch):
    monkeypatch.setattr(points_context.executor, "_handle_points_action", AsyncMock(
        side_effect=backend_module.MLAIBackendUnavailableError("test error"),
    ))
    assert await points_context.execute("balance") == (
        "I couldn't reach the MLAI points backend just now, so I couldn't "
        "confirm that action. Please try again in a moment."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("points", [5, 12])
async def test_textual_amount_after_numeric_slack_id_is_used(points_context, points):
    result = await points_context.execute(
        "award_points", text=f"award <@U012ABCDEF> {points} points for Newsletter",
        reason="Newsletter",
    )
    assert f"Awarded {points} points" in result
    points_context.client.get_rate_card.assert_not_awaited()
    points_context.client.award_points.assert_awaited_once_with(
        "UADMIN", "U012ABCDEF", points, "Newsletter",
    )


def test_points_skill_routes_estimates_away_from_the_catalog():
    skill = load_skill_from_directory(
        Path(__file__).resolve().parents[2] / "skills" / "mlai_points"
    )
    examples = {
        (row.get("text"), row.get("action"))
        for row in skill.routing.get("examples", [])
    }
    negatives = {
        (row.get("text"), row.get("instead"))
        for row in skill.routing.get("negative_examples", [])
    }
    estimate = next(action for action in skill.actions if action["name"] == "estimate_points")

    assert ("estimate how many roo points for adding MFA after signup", "estimate_points") in examples
    assert ("how many points is this task worth", "estimate_points") in examples
    assert ("show the rate card", "view_rate_card") in examples
    assert ("show the standard point rates", "view_rate_card") in examples
    assert ("all the ways to earn", "view_rate_card") in examples
    assert ("estimate points for new work", "estimate_points") in negatives
    assert "task_description" in estimate["params"]


def _assert_newsletter_recommendation(result: str):
    assert "I'd recommend **5 points** for this." in result
    assert "• **Newsletter** (5 pts) - Write a newsletter" in result
    assert "That matches the 'Newsletter' rate, which is worth 5 points." in result
    assert CATALOG_HEADER not in result
    assert "Grant full application" not in result
    assert "Workshop assistant" not in result
    assert result.count("• **") == 3


@pytest.mark.asyncio
async def test_estimate_points_recommends_without_full_catalog(points_context):
    points_context.client.get_rate_card.return_value = RATE_CARD_CATALOG
    result = await points_context.execute(
        "estimate_points",
        text="estimate how many roo points for Newsletter",
        task_description="Newsletter",
    )
    _assert_newsletter_recommendation(result)
    points_context.client.get_rate_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_estimate_points_missing_description_asks_for_the_work(points_context):
    assert await points_context.execute(
        "estimate_points",
        text="how many points is this task worth",
    ) == ASK_FOR_WORK
    points_context.client.get_rate_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_estimate_points_empty_card_stays_one_line(points_context):
    points_context.client.get_rate_card.return_value = []
    result = await points_context.execute(
        "estimate_points",
        text="estimate how many roo points for adding MFA after signup",
    )
    assert result == "No active point rates are configured."
    assert CATALOG_HEADER not in result
    assert "\n" not in result


@pytest.mark.asyncio
async def test_estimate_points_backend_failure_stays_one_line(points_context):
    points_context.client.get_rate_card.side_effect = backend_module.MLAIBackendUnavailableError("test error")
    result = await points_context.execute(
        "estimate_points",
        text="estimate how many roo points for adding MFA after signup",
    )
    assert result == FAILURE_MESSAGE
    assert CATALOG_HEADER not in result
    assert "\n" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "estimate how many roo points for Newsletter",
    "how many roo points for Newsletter",
    "how many points is Newsletter worth",
    'estimate how many points for this "Newsletter"',
])
async def test_view_rate_card_estimate_wording_recommends_instead(points_context, text):
    points_context.client.get_rate_card.return_value = RATE_CARD_CATALOG
    result = await points_context.execute("view_rate_card", text=text)
    _assert_newsletter_recommendation(result)


@pytest.mark.asyncio
async def test_view_rate_card_quoted_task_does_not_dump_the_catalog(points_context):
    points_context.client.get_rate_card.return_value = RATE_CARD_CATALOG
    text = (
        "estimate how many points for this "
        '"I need a very intuitive and clean UX flow after a user signs up, '
        "where they can add their phone number to get a 2fa code, add an "
        'authenticator app and/or add a passkey."'
    )
    result = await points_context.execute("view_rate_card", text=text)
    assert "I'd recommend **" in result
    assert "Closest matches:" in result
    assert CATALOG_HEADER not in result
    assert result.count("• **") <= 3
    shown = [row["name"] for row in RATE_CARD_CATALOG if row["name"] in result]
    assert len(shown) <= 3


@pytest.mark.asyncio
async def test_view_rate_card_worth_question_without_work_asks(points_context):
    result = await points_context.execute(
        "view_rate_card",
        text="how many points is this task worth",
    )
    assert result == ASK_FOR_WORK
    assert CATALOG_HEADER not in result
    points_context.client.get_rate_card.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "show the rate card",
    "list the standard point rates",
    "all the ways to earn",
    "show the rate card and estimate how many roo points for Newsletter",
])
async def test_explicit_rate_card_requests_still_dump_the_catalog(points_context, text):
    points_context.client.get_rate_card.return_value = RATE_CARD_CATALOG
    result = await points_context.execute("view_rate_card", text=text)
    assert result.startswith(CATALOG_HEADER)
    for row in RATE_CARD_CATALOG:
        assert f"**{row['name']}** ({row['points']} pts) - {row['description']}" in result
    assert "I'd recommend" not in result
