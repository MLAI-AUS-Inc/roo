"""Offline Slack award reporting; backend and delivery boundaries are faked."""
from types import SimpleNamespace

import httpx
import pytest

from roo.clients.mlai_backend import MLAIBackendUnavailableError
from roo.skills.executor import SkillExecutor
from roo.skills.executors import mlai_points


ALLOWANCE_ERROR = "Award exceeds the administrator's remaining weekly allowance"


def http_error(status=400, *, payload=None, body=None):
    request = httpx.Request("POST", "https://backend.invalid/admin/award/?secret=hidden")
    response = httpx.Response(status, request=request, **(
        {"content": body} if body is not None else {"json": payload}
    ))
    return httpx.HTTPStatusError("private transport diagnostics", request=request, response=response)


def confirmation(*, replayed=False):
    return {"success": True, "new_balance": 88, "ledger_id": 42,
            "created": not replayed, "replayed": replayed}


class AwardBackend:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    async def get_admin_details(self, slack_user_id):
        return {"slack_user_id": slack_user_id, "role": "admin"}

    async def get_user_by_slack_id(self, slack_user_id):
        return "existing-user"

    async def get_admin_allowance(self, slack_user_id):
        raise AssertionError("An idempotent replay must reach the backend's atomic gate")

    async def award_points(self, actor, target, points, reason, **kwargs):
        self.calls.append((actor, target, points, reason, kwargs))
        outcome = self.outcomes[target]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def delivery(monkeypatch):
    messages = []
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(mlai_points, "send_dm", lambda user, text: messages.append((user, text)))
    return messages


async def award(client):
    mentions = " ".join(f"<@{target}>" for target in client.outcomes)
    return await SkillExecutor()._handle_points_action(
        client=client, action="award_points", params={"points": 30, "reason": "mentoring"},
        text=f"<@UROO> give 30 points to {mentions} for mentoring",
        user_id="UADMIN", channel_id="CAWARDS", thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"), request_id="award-event-1",
    )


@pytest.mark.asyncio
async def test_allowance_rejection_has_no_success_claim_or_dm(delivery):
    result = await award(AwardBackend({"UTARGET": http_error(payload={"error": ALLOWANCE_ERROR})}))
    assert "No points were awarded" in result
    assert ALLOWANCE_ERROR in result
    assert "Awarded" not in result and "🎉" not in result
    assert "private transport" not in result and "https://" not in result
    assert delivery == []


@pytest.mark.asyncio
async def test_all_success_reports_only_confirmed_recipients(delivery):
    result = await award(AwardBackend({"UONE": confirmation(), "UTWO": confirmation()}))
    assert "Awarded 30 points each" in result
    assert "✅ <@UONE>: points awarded" in result
    assert "✅ <@UTWO>: points awarded" in result
    assert "88" not in result
    assert {user for user, _ in delivery} == {"UONE", "UTWO"}


@pytest.mark.asyncio
async def test_partial_success_does_not_claim_everyone_was_awarded(delivery):
    result = await award(AwardBackend({
        "UONE": confirmation(), "UTWO": http_error(payload={"error": ALLOWANCE_ERROR}),
    }))
    assert "Partially completed" in result
    assert "30 points" in result and "1 of 2" in result
    assert "✅ <@UONE>: points awarded" in result
    assert f"❌ <@UTWO>: {ALLOWANCE_ERROR}" in result
    assert "points each!" not in result
    assert [user for user, _ in delivery] == ["UONE"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    http_error(body="private plain-text diagnostic secret=hidden"),
    http_error(body="<html>private proxy diagnostic</html>"),
    http_error(payload={"error": {"token": "hidden"}}),
    http_error(payload={"error": "See https://backend.invalid/?secret=hidden"}),
    http_error(payload={"error": "private diagnostic secret=hidden"}),
    http_error(payload={"error": "<!channel> private diagnostic"}),
    http_error(payload=["private diagnostic"]),
])
async def test_unknown_rejections_use_safe_fallback(error, delivery):
    result = await award(AwardBackend({"UTARGET": error}))
    assert "No points were awarded" in result
    assert "rejected" in result.lower()
    assert all(value not in result for value in ("hidden", "private", "https://", "<!channel>", "<html>"))
    assert delivery == []


@pytest.mark.asyncio
async def test_balance_detail_is_redacted_from_public_rejection(delivery):
    result = await award(AwardBackend({"UTARGET": http_error(payload={"error": "Insufficient balance: 88 < 100 required"})}))
    assert "not enough Roo Points" in result
    assert "88" not in result and "100" not in result
    assert delivery == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    httpx.ReadTimeout("private timeout diagnostic"),
    http_error(503, body="private upstream diagnostic"),
    http_error(408, payload={"error": "private timeout diagnostic"}),
    MLAIBackendUnavailableError("private backend diagnostic"),
    RuntimeError("private unexpected diagnostic secret=hidden"),
    ValueError("private JSON decoding diagnostic"),
])
async def test_uncertain_outcome_never_claims_success_or_definite_failure(error, delivery):
    result = await award(AwardBackend({"UTARGET": error}))
    assert "couldn't confirm" in result.lower()
    assert "Check the points history before sending a new award" in result
    assert "No points were awarded" not in result and "Awarded" not in result
    assert "❌" not in result and "⚠️ <@UTARGET>" in result
    assert "private" not in result and "hidden" not in result
    assert delivery == []


@pytest.mark.asyncio
async def test_mixed_confirmed_and_unconfirmed_outcomes_are_distinct(delivery):
    result = await award(AwardBackend({
        "UONE": confirmation(), "UTWO": httpx.ReadTimeout("unknown"),
        "UTHREE": http_error(payload={"error": ALLOWANCE_ERROR}),
    }))
    assert "Partially completed" in result and "1 of 3" in result
    assert "✅ <@UONE>" in result
    assert "⚠️ <@UTWO>" in result
    assert "❌ <@UTHREE>" in result
    assert "Check the points history" in result
    assert [user for user, _ in delivery] == ["UONE"]


@pytest.mark.asyncio
async def test_replay_keeps_operation_identity_and_suppresses_repeat_dm(delivery):
    client = AwardBackend({"UTARGET": confirmation()})
    first = await award(client)
    client.outcomes["UTARGET"] = confirmation(replayed=True)
    second = await award(client)
    assert "Awarded 30 points to <@UTARGET>" in first
    assert first == second
    assert len(delivery) == 1
    assert client.calls[0] == client.calls[1]
    assert client.calls[0][:4] == ("UADMIN", "UTARGET", 30, "mentoring")
    assert client.calls[0][4]["idempotency_key"].startswith("roo-slack-award:")


@pytest.mark.asyncio
async def test_dm_failure_does_not_change_confirmed_award(monkeypatch, delivery):
    def fail_dm(*args):
        raise RuntimeError("private notification diagnostic")

    monkeypatch.setattr(mlai_points, "send_dm", fail_dm)
    result = await award(AwardBackend({"UTARGET": confirmation()}))
    assert "Awarded 30 points to <@UTARGET>" in result
    assert "private" not in result and "❌" not in result
