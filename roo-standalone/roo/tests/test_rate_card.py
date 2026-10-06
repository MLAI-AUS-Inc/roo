"""Rate-card display and smart-award behavior using external-boundary doubles."""

from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from roo.clients import mlai_backend as backend_module
from roo.skills import executor as executor_module


RATE_CARD_ROW = {
    "name": "Newsletter", "alias": "newsletter", "points": 5,
    "description": "Write a newsletter", "is_active": True,
}
FAILURE_MESSAGE = "I couldn't load the rate card just now. Please try again in a moment."


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

    async def execute(action="view_rate_card", **params):
        return await executor._execute_mlai_points(
            skill=SimpleNamespace(name="mlai-points"),
            text="award <@URECIPIENT> for Newsletter" if action in {"award", "award_points"} else "show rates",
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
    points_context.client.award_points.assert_not_awaited()
    points_context.client.link_slack_user.assert_not_awaited()
    points_context.client.get_user_by_slack_id.assert_not_awaited()
    points_context.send_dm.assert_not_called()


@pytest.mark.asyncio
async def test_smart_award_match_requires_confirmation(points_context):
    assert await points_context.execute("award_points", reason="Newsletter") == (
        "I found a match in the Rate Card: 'Newsletter' is worth 5 points. "
        "Should I award 5 points to <@URECIPIENT>? (You have 50 pts left this week.)"
    )
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
    points_context.client.award_points.assert_not_awaited()
    points_context.send_dm.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_amount_award_does_not_fetch_rate_card(points_context):
    result = await points_context.execute("award_points", points=5, reason="Newsletter")
    assert "Awarded 5 points" in result
    points_context.client.get_rate_card.assert_not_awaited()
    points_context.client.award_points.assert_awaited_once_with("UADMIN", "URECIPIENT", 5, "Newsletter")


@pytest.mark.asyncio
async def test_unrelated_points_failure_keeps_general_guard(points_context, monkeypatch):
    monkeypatch.setattr(points_context.executor, "_handle_points_action", AsyncMock(
        side_effect=backend_module.MLAIBackendUnavailableError("test error"),
    ))
    assert await points_context.execute("balance") == (
        "I couldn't reach the MLAI points backend just now, so I couldn't "
        "confirm that action. Please try again in a moment."
    )
