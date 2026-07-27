import asyncio
import importlib
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)

backend_module = importlib.import_module("roo.clients.mlai_backend")
coworking_module = importlib.import_module("roo.coworking_booking_intents")
main_module = importlib.import_module("roo.main")
approval_module = importlib.import_module("roo.points_request_approval")
slack_client_module = importlib.import_module("roo.slack_client")
executor_module = importlib.import_module("roo.skills.executor")
SkillExecutor = executor_module.SkillExecutor


@pytest.fixture(autouse=True)
def clear_points_request_summary_cache():
    approval_module.clear_points_request_summaries()
    yield
    approval_module.clear_points_request_summaries()


class FakePointsClient:
    def __init__(self):
        self.created = None
        self.attached = None

    async def create_points_request(
        self,
        requester_slack_id,
        target_slack_id,
        points,
        reason,
        slack_channel_id=None,
        slack_thread_ts=None,
    ):
        self.created = {
            "requester_slack_id": requester_slack_id,
            "target_slack_id": target_slack_id,
            "points": points,
            "reason": reason,
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts,
        }
        return {"id": 42}

    async def attach_points_request_slack_summary(
        self,
        request_id,
        slack_channel_id,
        slack_thread_ts,
        slack_summary_message_ts,
    ):
        self.attached = {
            "request_id": request_id,
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts,
            "slack_summary_message_ts": slack_summary_message_ts,
        }
        return {"ok": True}


class FakeTopupPurchaseClient:
    def __init__(self, response=None):
        self.created = None
        self.response = response or {
            "id": "purchase-uuid",
            "frontend_checkout_page_url": "https://mlai.au/roo/topup/purchase-uuid",
        }

    async def create_points_purchase(
        self,
        slack_user_id,
        pack_id=None,
        points_amount=None,
        purchase_from=None,
    ):
        self.created = {
            "slack_user_id": slack_user_id,
            "pack_id": pack_id,
            "points_amount": points_amount,
            "purchase_from": purchase_from,
        }
        return self.response


class FakeTopupButtonsClient:
    def __init__(self, response=None):
        self.created = None
        self.response = response or {
            "checkout_request_id": "EvTopup123",
            "options": [
                {
                    "pack_id": "topup_5",
                    "points_amount": 10,
                    "amount_cents": 1999,
                    "currency": "aud",
                    "expires_at": "2026-07-28T10:00:00Z",
                    "checkout_session_url": (
                        "https://checkout.stripe.com/c/pay/cs_test_topup_5#checkout"
                    ),
                },
                {
                    "pack_id": "topup_10",
                    "points_amount": 20,
                    "amount_cents": 3699,
                    "currency": "aud",
                    "expires_at": "2026-07-28T10:00:00Z",
                    "checkout_session_url": (
                        "https://checkout.stripe.com/c/pay/cs_test_topup_10"
                    ),
                },
                {
                    "pack_id": "topup_25",
                    "points_amount": 50,
                    "amount_cents": 6399,
                    "currency": "aud",
                    "expires_at": "2026-07-28T10:00:00Z",
                    "checkout_session_url": (
                        "https://checkout.stripe.com/c/pay/cs_test_topup_25"
                    ),
                },
            ],
            "errors": [],
        }

    async def create_points_checkout_options(
        self,
        slack_user_id,
        *,
        checkout_request_id,
        pack_ids=None,
        purchase_from=None,
    ):
        self.created = {
            "slack_user_id": slack_user_id,
            "checkout_request_id": checkout_request_id,
            "pack_ids": pack_ids,
            "purchase_from": purchase_from,
        }
        if pack_ids is None:
            return self.response
        selected = [
            option
            for option in self.response["options"]
            if option["pack_id"] in pack_ids
        ]
        return {**self.response, "options": selected}


class FakeBalanceClient:
    def __init__(self, response):
        self.response = response

    async def get_balance(self, slack_user_id):
        return self.response


class FakeRewardsClient:
    def __init__(self, rewards, balance_response=None, balance_error=None):
        self.rewards = rewards
        self.balance_response = balance_response
        self.balance_error = balance_error
        self.list_rewards_user_id = None
        self.balance_user_id = None

    async def list_rewards(self, slack_user_id=None):
        self.list_rewards_user_id = slack_user_id
        return self.rewards

    async def get_balance(self, slack_user_id):
        self.balance_user_id = slack_user_id
        if self.balance_error:
            raise self.balance_error
        return self.balance_response or {"balance": 12, "lifetime_earned": 42}


class FailingTopupPurchaseClient(FakeTopupPurchaseClient):
    async def create_points_purchase(
        self,
        slack_user_id,
        pack_id=None,
        points_amount=None,
        purchase_from=None,
    ):
        self.created = {
            "slack_user_id": slack_user_id,
            "pack_id": pack_id,
            "points_amount": points_amount,
            "purchase_from": purchase_from,
        }
        request = httpx.Request(
            "POST",
            "https://backend.test/api/v1/points/purchases/",
        )
        response = httpx.Response(
            400,
            request=request,
            json={"error": "Top-up purchases are not available for this account."},
        )
        raise httpx.HTTPStatusError("bad request", request=request, response=response)


class BalanceCapTopupPurchaseClient(FakeTopupPurchaseClient):
    async def create_points_purchase(
        self,
        slack_user_id,
        pack_id=None,
        points_amount=None,
        purchase_from=None,
    ):
        request = httpx.Request(
            "POST",
            "https://backend.test/api/v1/points/purchases/",
        )
        response = httpx.Response(
            400,
            request=request,
            json={"error": "Top-up purchase would exceed the 100-point spendable balance cap"},
        )
        raise httpx.HTTPStatusError("bad request", request=request, response=response)


class FailingCreatePointsClient(FakePointsClient):
    async def create_points_request(
        self,
        requester_slack_id,
        target_slack_id,
        points,
        reason,
        slack_channel_id=None,
        slack_thread_ts=None,
    ):
        request = httpx.Request(
            "POST",
            "https://backend.test/api/v1/points/requests/",
        )
        response = httpx.Response(
            404,
            request=request,
            json={"detail": "Not Found"},
        )
        raise httpx.HTTPStatusError("not found", request=request, response=response)


class FailingAttachPointsClient(FakePointsClient):
    async def attach_points_request_slack_summary(
        self,
        request_id,
        slack_channel_id,
        slack_thread_ts,
        slack_summary_message_ts,
    ):
        self.attached = {
            "request_id": request_id,
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts,
            "slack_summary_message_ts": slack_summary_message_ts,
        }
        request = httpx.Request(
            "PATCH",
            f"https://backend.test/api/v1/points/requests/{request_id}/slack-summary/",
        )
        response = httpx.Response(
            404,
            request=request,
            json={"detail": "Not Found"},
        )
        raise httpx.HTTPStatusError("not found", request=request, response=response)


class FakePointsAdminClient:
    def __init__(self):
        self.promote_args = None
        self.revoke_args = None
        self.allowance_args = None

    def _clean_slack_id(self, user_id):
        return str(user_id).strip("<@>")

    async def get_admin_details(self, slack_user_id):
        return {"slack_user_id": slack_user_id, "role": "admin"}

    async def promote_points_admin(self, requester_slack_id, target_slack_id):
        self.promote_args = (requester_slack_id, target_slack_id)
        return {"target_slack_id": target_slack_id}

    async def set_points_admin_weekly_allowance(
        self,
        requester_slack_id,
        target_slack_id,
        weekly_allowance,
    ):
        self.allowance_args = (requester_slack_id, target_slack_id, weekly_allowance)
        return {
            "target_slack_id": target_slack_id,
            "weekly_allowance": weekly_allowance,
        }

    async def revoke_points_admin(self, requester_slack_id, target_slack_id):
        self.revoke_args = (requester_slack_id, target_slack_id)
        return {"target_slack_id": target_slack_id, "revoked": True}


class FakeAlreadyAdminClient(FakePointsAdminClient):
    async def promote_points_admin(self, requester_slack_id, target_slack_id):
        self.promote_args = (requester_slack_id, target_slack_id)
        return {"target_slack_id": target_slack_id, "already_admin": True}


class FakeMissingPointsAdminClient(FakePointsAdminClient):
    async def set_points_admin_weekly_allowance(
        self,
        requester_slack_id,
        target_slack_id,
        weekly_allowance,
    ):
        self.allowance_args = (requester_slack_id, target_slack_id, weekly_allowance)
        return {"error": "Not a points admin"}


class FakeAlreadyRevokedClient(FakePointsAdminClient):
    async def revoke_points_admin(self, requester_slack_id, target_slack_id):
        self.revoke_args = (requester_slack_id, target_slack_id)
        return {"target_slack_id": target_slack_id, "already_revoked": True}


class FakeAwardGuardClient(FakePointsAdminClient):
    def __init__(self):
        super().__init__()
        self.allowance_lookup_called = False
        self.award_called = False

    async def get_admin_allowance(self, requester_slack_id):
        self.allowance_lookup_called = True
        return {"remaining": 100, "allowance": 100}

    async def award_points(self, requester_slack_id, target_slack_id, points, reason):
        self.award_called = True
        return {"points_awarded": points, "new_balance": 999}


class FakePartnerRestrictedClient(FakeAwardGuardClient):
    def __init__(self):
        super().__init__()
        self.create_called = False
        self.approve_called = False

    async def get_admin_details(self, slack_user_id):
        return {"slack_user_id": slack_user_id, "role": "partner"}

    async def create_task(self, *args, **kwargs):
        self.create_called = True
        return {"id": 1}

    async def approve_task(self, *args, **kwargs):
        self.approve_called = True
        return {"points_awarded": 5}


def test_extract_points_request_reason_supports_requesting_phrase():
    executor = SkillExecutor()

    reason = executor._extract_points_request_reason(
        "I'm requesting 6 points for volunteering at MedHack"
    )

    assert reason == "volunteering at MedHack"


def test_extract_points_request_reason_supports_award_me_phrase():
    executor = SkillExecutor()

    reason = executor._extract_points_request_reason(
        "please award me 6 roo points for volunteering at medhack"
    )

    assert reason == "volunteering at medhack"


@pytest.mark.asyncio
async def test_request_points_creates_request_and_suppresses_auto_post(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsClient()
    posted_messages = []

    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "222.333"},
    )
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="request_points",
        params={"points": 5, "reason": "helping at the event"},
        text="request 5 points for helping at the event",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert result["suppress_post"] is True
    assert result["data"]["request_id"] == 42
    assert client.created == {
        "requester_slack_id": "U123",
        "target_slack_id": "U123",
        "points": 5,
        "reason": "helping at the event",
        "slack_channel_id": "C123",
        "slack_thread_ts": "111.222",
    }
    assert client.attached == {
        "request_id": 42,
        "slack_channel_id": "C123",
        "slack_thread_ts": "111.222",
        "slack_summary_message_ts": "222.333",
    }
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert "Points Admins can approve this by reacting with a green tick" in posted_messages[0]["text"]
    assert posted_messages[0]["metadata"] == {
        "event_type": "roo_points_request",
        "event_payload": {
            "request_id": 42,
            "requester_slack_id": "U123",
            "target_slack_id": "U123",
            "points": 5,
            "reason": "helping at the event",
            "slack_thread_ts": "111.222",
        },
    }


@pytest.mark.asyncio
async def test_request_points_handles_im_requesting_phrase(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsClient()
    posted_messages = []

    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "222.333"},
    )
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="request_points",
        params={"action": "request", "points": 6, "reason": ""},
        text="I'm requesting 6 points for volunteering at MedHack",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert result["suppress_post"] is True
    assert client.created["points"] == 6
    assert client.created["reason"] == "volunteering at MedHack"
    assert "requested *6 points* for: volunteering at MedHack" in posted_messages[0]["text"]


@pytest.mark.asyncio
async def test_request_points_404_returns_integration_error(monkeypatch):
    executor = SkillExecutor()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=FailingCreatePointsClient(),
        action="request_points",
        params={"points": 12, "reason": "running the 21st x MLAI event"},
        text="I'm requesting 12 points for running the 21st x MLAI event",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "queue that points request for admin approval" in result
    assert "Double-check the ID or date" not in result


@pytest.mark.asyncio
async def test_request_points_attach_failure_keeps_slack_approval_active(monkeypatch):
    executor = SkillExecutor()
    posted_messages = []

    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "222.333"},
    )
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=FailingAttachPointsClient(),
        action="request_points",
        params={"points": 5, "reason": "helping at the event"},
        text="request 5 points for helping at the event",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert result["suppress_post"] is True
    assert result["data"]["request_id"] == 42
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert posted_messages[0]["metadata"]["event_payload"]["request_id"] == 42
    assert (
        approval_module.get_remembered_points_request_summary("C123", "222.333")["id"] == 42
    )


@pytest.mark.asyncio
async def test_award_me_phrase_is_converted_into_points_request(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsClient()
    posted_messages = []

    monkeypatch.setattr(
        executor_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "222.333"},
    )
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="award_points",
        params={"action": "award_points", "points": 6, "reason": ""},
        text="please award me 6 roo points for volunteering at medhack",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert result["suppress_post"] is True
    assert client.created["target_slack_id"] == "U123"
    assert client.created["reason"] == "volunteering at medhack"
    assert "requested *6 points* for: volunteering at medhack" in posted_messages[0]["text"]


@pytest.mark.asyncio
async def test_request_points_rejected_in_dm():
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=FakePointsClient(),
        action="request_points",
        params={"points": 5, "reason": "helping at the event"},
        text="request 5 points for helping at the event",
        user_id="U123",
        channel_id="D123",
        thread_ts="111.222",
        skill=None,
    )

    assert "shared channel or thread" in result


@pytest.mark.asyncio
async def test_balance_summary_includes_lifetime_purchased():
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=FakeBalanceClient(
            {
                "balance": 15,
                "lifetime_earned": 42,
                "lifetime_spent": 27,
                "lifetime_purchased": 10,
            }
        ),
        action="balance",
        params={},
        text="points",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "Current Balance:** 15 points" in result
    assert "Lifetime Earned:** 42 points" in result
    assert "Lifetime Spent:** 27 points" in result
    assert "Lifetime Purchased:** 10 points" in result


@pytest.mark.asyncio
async def test_list_rewards_response_includes_catalog_context():
    executor = SkillExecutor()
    client = FakeRewardsClient(
        [
            {
                "code": "WORKSHOP_FREE",
                "name": "Free Workshop Ticket",
                "description": "Events (Limited Stock)",
                "cost_points": 42,
                "fulfillment": "manual",
                "stock_remaining": 5,
                "can_afford": False,
                "user_balance": 12,
            },
            {
                "code": "STICKER",
                "name": "Sticker",
                "description": "Merch",
                "cost_points": 1,
                "fulfillment": "manual",
                "stock_remaining": None,
                "can_afford": True,
                "user_balance": 12,
            },
            {
                "code": "COWORKING_DAY",
                "name": "1 Day Hot-desk",
                "description": "Coworking",
                "cost_points": 4,
                "fulfillment": "auto",
                "stock_remaining": None,
                "can_afford": True,
                "user_balance": 12,
            },
        ]
    )

    result = await executor._handle_points_action(
        client=client,
        action="list_rewards",
        params={},
        text="rewards",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.list_rewards_user_id == "U123"
    assert client.balance_user_id == "U123"
    assert "Your balance: **12 points**" in result
    assert "Lifetime earned: **42 points**" in result
    assert "**Sticker** (`STICKER`) - 1 point" in result
    assert "Merch; admin approval; you can redeem this now" in result
    assert "**1 Day Hot-desk** (`COWORKING_DAY`) - 4 points" in result
    assert "Coworking; instant redemption; you can redeem this now" in result
    assert "**Free Workshop Ticket** (`WORKSHOP_FREE`) - 42 points" in result
    assert "Events (Limited Stock); 5 left; admin approval; need 30 more points" in result
    assert "SEO article generation costs 4 Roo Points" in result
    assert "variable Roo Points bid" in result
    assert "Bounties and paid work generally go to members with the highest lifetime earned Roo Points" in result
    assert "MLAI committee" in result
    assert "at least 100 lifetime earned Roo Points" in result
    assert "reward request <CODE>" in result


@pytest.mark.asyncio
async def test_list_rewards_still_renders_when_balance_lookup_fails():
    executor = SkillExecutor()
    client = FakeRewardsClient(
        [
            {
                "code": "EVENT_TICKET",
                "name": "Free Community Event Ticket",
                "description": "Events",
                "cost_points": 6,
                "fulfillment": "manual",
                "stock_remaining": None,
                "can_afford": True,
                "user_balance": 10,
            },
        ],
        balance_error=RuntimeError("balance timeout"),
    )

    result = await executor._handle_points_action(
        client=client,
        action="list_rewards",
        params={},
        text="rewards",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.balance_user_id == "U123"
    assert "Your balance: **10 points**" in result
    assert "Free Community Event Ticket" in result
    assert "SEO article generation costs 4 Roo Points" in result
    assert "Bounties and paid work" in result


@pytest.mark.asyncio
async def test_topup_points_disabled_returns_feature_message(monkeypatch):
    executor = SkillExecutor()
    client = FakeTopupPurchaseClient()

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(ROO_POINTS_TOPUP_ENABLED=False),
    )

    result = await executor._handle_points_action(
        client=client,
        action="topup_points",
        params={"points": 10},
        text="buy 10 roo points",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "not enabled yet" in result
    assert client.created is None


@pytest.mark.asyncio
async def test_topup_points_missing_pack_lists_fixed_packs(monkeypatch):
    executor = SkillExecutor()

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(ROO_POINTS_TOPUP_ENABLED=True),
    )

    result = await executor._handle_points_action(
        client=FakeTopupPurchaseClient(),
        action="topup_points",
        params={},
        text="top up Roo Points",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "Available Top-up Roo Points packs" in result
    assert "10 Top-up Roo Points - A$19.99" in result
    assert "20 Top-up Roo Points - A$36.99" in result
    assert "50 Top-up Roo Points - A$63.99" in result
    assert "price per point" not in result.lower()


@pytest.mark.asyncio
async def test_topup_points_missing_pack_posts_three_private_stripe_buttons(
    monkeypatch,
):
    executor = SkillExecutor()
    client = FakeTopupButtonsClient()
    delivered = {}

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            ROO_POINTS_TOPUP_ENABLED=True,
            ROO_POINTS_TOPUP_BUTTONS_ENABLED=True,
            roo_points_stripe_checkout_hosts={"checkout.stripe.com"},
        ),
    )
    monkeypatch.setattr(
        executor_module,
        "post_ephemeral",
        lambda **kwargs: delivered.update(kwargs) or {"ok": True},
    )

    result = await executor._handle_points_action(
        client=client,
        action="topup_points",
        params={},
        text="top up Roo Points",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
        request_id="EvTopup123",
    )

    assert result["suppress_post"] is True
    assert result["message"] == ""
    assert result["data"]["delivery"] == "ephemeral"
    assert result["data"]["pack_ids"] == ["topup_5", "topup_10", "topup_25"]
    assert client.created == {
        "slack_user_id": "U123",
        "checkout_request_id": "EvTopup123",
        "pack_ids": None,
        "purchase_from": {
            "source": "slack",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        },
    }
    assert delivered["channel"] == "C123"
    assert delivered["user"] == "U123"
    assert delivered["thread_ts"] == "111.222"
    buttons = delivered["blocks"][1]["elements"]
    assert [button["text"]["text"] for button in buttons] == [
        "10 points · A$19.99",
        "20 points · A$36.99",
        "50 points · A$63.99",
    ]
    assert all(
        button["url"].startswith("https://checkout.stripe.com/")
        for button in buttons
    )
    assert buttons[1]["style"] == "primary"
    assert "checkout.stripe.com" not in delivered["text"]


@pytest.mark.asyncio
async def test_topup_points_explicit_pack_returns_one_button_in_dm(monkeypatch):
    executor = SkillExecutor()
    client = FakeTopupButtonsClient()

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            ROO_POINTS_TOPUP_ENABLED=True,
            ROO_POINTS_TOPUP_BUTTONS_ENABLED=True,
            roo_points_stripe_checkout_hosts={"checkout.stripe.com"},
        ),
    )
    monkeypatch.setattr(
        executor_module,
        "post_ephemeral",
        lambda **kwargs: pytest.fail("DM checkout must not use chat.postEphemeral"),
    )

    result = await executor._handle_points_action(
        client=client,
        action="topup_points",
        params={"points": 20},
        text="topup 20 points",
        user_id="U123",
        channel_id="D123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
        request_id="EvTopup20",
    )

    assert result["data"]["delivery"] == "direct_message"
    assert result["data"]["pack_ids"] == ["topup_10"]
    assert client.created["pack_ids"] == ["topup_10"]
    buttons = result["blocks"][1]["elements"]
    assert len(buttons) == 1
    assert buttons[0]["text"]["text"] == "20 points · A$36.99"
    assert "checkout.stripe.com" not in result["message"]


@pytest.mark.asyncio
async def test_topup_points_rejects_untrusted_checkout_url(monkeypatch):
    executor = SkillExecutor()
    client = FakeTopupButtonsClient(
        response={
            "checkout_request_id": "EvEvil",
            "options": [
                {
                    "pack_id": "topup_5",
                    "points_amount": 10,
                    "amount_cents": 1999,
                    "currency": "aud",
                    "checkout_session_url": "https://evil.example/checkout",
                }
            ],
            "errors": [],
        }
    )

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            ROO_POINTS_TOPUP_ENABLED=True,
            ROO_POINTS_TOPUP_BUTTONS_ENABLED=True,
            roo_points_stripe_checkout_hosts={"checkout.stripe.com"},
        ),
    )
    monkeypatch.setattr(
        executor_module,
        "post_ephemeral",
        lambda **kwargs: pytest.fail("Untrusted links must never be posted"),
    )

    result = await executor._handle_points_action(
        client=client,
        action="topup_points",
        params={},
        text="topup",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
        request_id="EvEvil",
    )

    assert "trusted Stripe Checkout links" in result
    assert "evil.example" not in result


@pytest.mark.asyncio
async def test_topup_points_unsupported_pack_is_rejected(monkeypatch):
    executor = SkillExecutor()

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(ROO_POINTS_TOPUP_ENABLED=True),
    )

    result = await executor._handle_points_action(
        client=FakeTopupPurchaseClient(),
        action="topup_points",
        params={"points": 100},
        text="buy 100 roo points",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "10, 20, or 50 Top-up Roo Points" in result


@pytest.mark.asyncio
async def test_topup_points_valid_pack_creates_purchase(monkeypatch):
    executor = SkillExecutor()
    client = FakeTopupPurchaseClient()

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(ROO_POINTS_TOPUP_ENABLED=True),
    )

    result = await executor._handle_points_action(
        client=client,
        action="topup_points",
        params={},
        text="buy 10 roo points",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "https://mlai.au/roo/topup/purchase-uuid" in result
    assert "not money" in result
    assert "no cash value" in result
    assert "do not count toward lifetime earned contribution" in result
    assert client.created == {
        "slack_user_id": "U123",
        "pack_id": "topup_5",
        "points_amount": None,
        "purchase_from": {
            "source": "slack",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        },
    }


@pytest.mark.asyncio
async def test_topup_points_backend_error_is_friendly(monkeypatch):
    executor = SkillExecutor()

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(ROO_POINTS_TOPUP_ENABLED=True),
    )

    result = await executor._handle_points_action(
        client=FailingTopupPurchaseClient(),
        action="topup_points",
        params={"points": 10},
        text="buy 10 roo points",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "couldn't create that top-up checkout" in result
    assert "Top-up purchases are not available" in result


@pytest.mark.asyncio
async def test_topup_points_balance_cap_message_is_clear(monkeypatch):
    executor = SkillExecutor()

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(ROO_POINTS_TOPUP_ENABLED=True),
    )

    result = await executor._handle_points_action(
        client=BalanceCapTopupPurchaseClient(),
        action="topup_points",
        params={"points": 10},
        text="topup 10 points",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "heaps of Roo Points" in result
    assert "100-point spendable balance cap" in result
    assert "Use some points first" in result


@pytest.mark.asyncio
async def test_execute_mlai_points_prefers_roo_api_key(monkeypatch):
    executor = SkillExecutor()
    captured = {}

    async def fake_handle_points_action(
        self,
        client,
        action,
        params,
        text,
        user_id,
        channel_id,
        thread_ts,
        skill,
    ):
        captured["client"] = client
        captured["action"] = action
        return {"ok": True}

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="mlai-api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", InitCapturingBackendClient)
    monkeypatch.setattr(
        SkillExecutor,
        "_handle_points_action",
        fake_handle_points_action,
    )

    result = await executor._execute_mlai_points(
        skill=SimpleNamespace(name="mlai-points"),
        text="request 5 points for helping at the event",
        params={"action": "request_points", "points": 5, "reason": "helping at the event"},
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert result == {"ok": True}
    assert captured["action"] == "request_points"
    assert isinstance(captured["client"], InitCapturingBackendClient)
    assert InitCapturingBackendClient.last_init["kwargs"] == {
        "base_url": "https://backend.test",
        "api_key": "roo-api-key",
        "internal_api_key": "internal-key",
    }


@pytest.mark.asyncio
async def test_promote_points_admin_authorized(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()
    ensured_users = []

    async def fake_ensure_user_exists(user_id):
        ensured_users.append(user_id)

    monkeypatch.setattr(executor, "_ensure_user_exists", fake_ensure_user_exists)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="promote <@UTARGET> to roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert ensured_users == ["UTARGET"]
    assert client.promote_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
    )
    assert "<@UTARGET> is now a Roo Points Admin" in result


@pytest.mark.asyncio
async def test_promote_points_admin_denies_unauthorized_user(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()
    ensured_users = []

    async def fake_ensure_user_exists(user_id):
        ensured_users.append(user_id)

    monkeypatch.setattr(executor, "_ensure_user_exists", fake_ensure_user_exists)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="promote <@UTARGET> to roo points admin",
        user_id="UNAUTHORIZED",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "only <@U05QPB483K9> can manage Points Admin access and weekly allowances" in result
    assert client.promote_args is None
    assert ensured_users == []


@pytest.mark.asyncio
async def test_promote_points_admin_requires_exactly_one_tagged_user(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    missing_result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="promote sam to roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )
    multiple_result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="promote <@UONE> and <@UTWO> to roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "Tag exactly one user to promote them to Roo Points Admin." in missing_result
    assert "I found multiple mentions there" in multiple_result


@pytest.mark.asyncio
async def test_promote_points_admin_handles_already_admin_idempotently(monkeypatch):
    executor = SkillExecutor()
    client = FakeAlreadyAdminClient()

    async def fake_ensure_user_exists(_user_id):
        return None

    monkeypatch.setattr(executor, "_ensure_user_exists", fake_ensure_user_exists)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="promote_points_admin",
        params={},
        text="make <@UTARGET> a roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.promote_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
    )
    assert "<@UTARGET> is already a Roo Points Admin." == result


@pytest.mark.asyncio
async def test_revoke_points_admin_authorized(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="revoke_points_admin",
        params={},
        text="remove <@UTARGET> as roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.revoke_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
    )
    assert "✅ Removed Roo Points Admin access from <@UTARGET>." == result


@pytest.mark.asyncio
async def test_revoke_points_admin_denies_unauthorized_user(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="revoke_points_admin",
        params={},
        text="revoke <@UTARGET> as roo points admin",
        user_id="UNAUTHORIZED",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "only <@U05QPB483K9> can manage Points Admin access and weekly allowances" in result
    assert client.revoke_args is None


@pytest.mark.asyncio
async def test_revoke_points_admin_handles_already_revoked_idempotently(monkeypatch):
    executor = SkillExecutor()
    client = FakeAlreadyRevokedClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="revoke_points_admin",
        params={},
        text="revoke <@UTARGET> as roo points admin",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.revoke_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
    )
    assert "<@UTARGET> already doesn't have Roo Points Admin access." == result


@pytest.mark.asyncio
async def test_set_points_admin_allowance_authorized(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="change <@UTARGET> weekly points allowance to 150",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.allowance_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
        150,
    )
    assert "✅ Set <@UTARGET>'s weekly points allowance to 150 points." == result


@pytest.mark.asyncio
async def test_set_points_admin_allowance_denies_unauthorized_user(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="set <@UTARGET> weekly points allowance to 150",
        user_id="UNAUTHORIZED",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "only <@U05QPB483K9> can manage Points Admin access and weekly allowances" in result
    assert client.allowance_args is None


@pytest.mark.asyncio
async def test_set_points_admin_allowance_requires_numeric_positive_value(monkeypatch):
    executor = SkillExecutor()
    client = FakePointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    missing_result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="set <@UTARGET> weekly points allowance",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )
    invalid_result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="set <@UTARGET> weekly points allowance to 0",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "What weekly allowance should I set?" in missing_result
    assert "Weekly points allowance has to be a positive number." == invalid_result


@pytest.mark.asyncio
async def test_set_points_admin_allowance_surfaces_target_not_admin(monkeypatch):
    executor = SkillExecutor()
    client = FakeMissingPointsAdminClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="set_points_admin_allowance",
        params={},
        text="set <@UTARGET> weekly points allowance to 150",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.allowance_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
        150,
    )
    assert "<@UTARGET> isn't a Points Admin yet, so I can't update their allowance." == result


@pytest.mark.asyncio
async def test_award_flow_reroutes_management_phrase_to_allowance_update(monkeypatch):
    executor = SkillExecutor()
    client = FakeAwardGuardClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="award_points",
        params={"action": "award_points"},
        text="please increase the number of points <@UTARGET> can give out weekly to 48",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.allowance_args == (
        executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        "UTARGET",
        48,
    )
    assert client.allowance_lookup_called is False
    assert client.award_called is False
    assert "✅ Set <@UTARGET>'s weekly points allowance to 48 points." == result


@pytest.mark.asyncio
async def test_award_flow_reroutes_missing_tag_management_phrase_to_clarification(monkeypatch):
    executor = SkillExecutor()
    client = FakeAwardGuardClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="award_points",
        params={"action": "award_points"},
        text="please increase the number of points sam can give out weekly to 48",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert client.allowance_args is None
    assert client.allowance_lookup_called is False
    assert client.award_called is False
    assert "Tag exactly one user to change their weekly points allowance." == result


@pytest.mark.asyncio
async def test_partner_admin_cannot_award_points(monkeypatch):
    executor = SkillExecutor()
    client = FakePartnerRestrictedClient()

    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UBOT")

    result = await executor._handle_points_action(
        client=client,
        action="award_points",
        params={"points": 5, "reason": "helping out", "target_user": "UTARGET"},
        text="award <@UTARGET> 5 points for helping out",
        user_id="UPARTNER",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "partner admins can only generate coworking reports" in result
    assert client.allowance_lookup_called is False
    assert client.award_called is False


@pytest.mark.asyncio
async def test_partner_admin_cannot_create_or_approve_tasks():
    executor = SkillExecutor()
    create_client = FakePartnerRestrictedClient()
    approve_client = FakePartnerRestrictedClient()
    edit_client = FakePartnerRestrictedClient()
    cancel_client = FakePartnerRestrictedClient()

    create_result = await executor._handle_points_action(
        client=create_client,
        action="create_task",
        params={"title": "Partner task", "points": 5, "portfolio": "ops"},
        text="create task Partner task worth 5 points",
        user_id="UPARTNER",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )
    approve_result = await executor._handle_points_action(
        client=approve_client,
        action="approve_task",
        params={"task_id": 42},
        text="approve task 42",
        user_id="UPARTNER",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )
    edit_result = await executor._handle_points_action(
        client=edit_client,
        action="edit_task",
        params={"task_id": 42, "title": "Updated partner task"},
        text="edit task 42 title to Updated partner task",
        user_id="UPARTNER",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )
    cancel_result = await executor._handle_points_action(
        client=cancel_client,
        action="cancel_task",
        params={"task_id": 42},
        text="cancel task 42",
        user_id="UPARTNER",
        channel_id="C123",
        thread_ts="111.222",
        skill=None,
    )

    assert "partner admins can only generate coworking reports" in create_result
    assert "partner admins can only generate coworking reports" in approve_result
    assert "partner admins can only generate coworking reports" in edit_result
    assert "partner admins can only generate coworking reports" in cancel_result
    assert create_client.create_called is False
    assert approve_client.approve_called is False


class FakeApprovalClient:
    last_instance = None

    def __init__(self, *args, **kwargs):
        self.lookup_args = None
        self.approve_args = None
        FakeApprovalClient.last_instance = self

    async def get_points_request_by_slack_message(self, slack_channel_id, slack_message_ts):
        self.lookup_args = (slack_channel_id, slack_message_ts)
        return {
            "id": 7,
            "status": "pending",
            "requester_slack_id": "UREQUESTER",
            "target_slack_id": "UREQUESTER",
            "points": 5,
            "reason": "helping at the event",
            "slack_thread_ts": "111.222",
        }

    async def approve_points_request(self, request_id, admin_slack_id):
        self.approve_args = (request_id, admin_slack_id)
        return {"points_awarded": 5, "new_balance": 17}


class FailingApprovalClient(FakeApprovalClient):
    async def approve_points_request(self, request_id, admin_slack_id):
        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(403, request=request, json={"error": "Not a points admin"})
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)


class LookupMissApprovalClient(FakeApprovalClient):
    async def get_points_request_by_slack_message(self, slack_channel_id, slack_message_ts):
        self.lookup_args = (slack_channel_id, slack_message_ts)
        return None


class InitCapturingBackendClient:
    last_init = None

    def __init__(self, *args, **kwargs):
        InitCapturingBackendClient.last_init = {
            "args": args,
            "kwargs": kwargs,
        }


class RecordingAsyncClient:
    def __init__(self, *, status_code=200, json_data=None):
        self.status_code = status_code
        self.json_data = {} if json_data is None else json_data
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def _response(self, method, url, *, json=None, params=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return httpx.Response(
            self.status_code,
            request=httpx.Request(method, url),
            json=self.json_data,
        )

    async def request(self, method, url, **kwargs):
        return self._response(
            method,
            url,
            json=kwargs.get("json"),
            params=kwargs.get("params"),
            headers=kwargs.get("headers"),
            timeout=kwargs.get("timeout"),
        )

    async def post(self, url, *, json=None, headers=None, timeout=None):
        return self._response("POST", url, json=json, headers=headers, timeout=timeout)

    async def patch(self, url, *, json=None, headers=None, timeout=None):
        return self._response("PATCH", url, json=json, headers=headers, timeout=timeout)

    async def get(self, url, *, params=None, headers=None, timeout=None):
        return self._response("GET", url, params=params, headers=headers, timeout=timeout)


@pytest.mark.asyncio
async def test_reaction_approval_posts_confirmation(monkeypatch):
    posted_messages = []
    direct_messages = []

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeApprovalClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "333.444"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: direct_messages.append((user_id, text)),
    )

    await main_module._handle_reaction_added(
        {
            "user": "UADMIN",
            "reaction": "white_check_mark",
            "item": {"type": "message", "channel": "C123", "ts": "222.333"},
        }
    )

    assert FakeApprovalClient.last_instance.lookup_args == ("C123", "222.333")
    assert FakeApprovalClient.last_instance.approve_args == (7, "UADMIN")
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert "approved by <@UADMIN>" in posted_messages[0]["text"]
    assert "<@UREQUESTER> received 5 points" in posted_messages[0]["text"]
    assert direct_messages == []


@pytest.mark.asyncio
async def test_reaction_approval_failure_dms_reactor(monkeypatch):
    posted_messages = []
    direct_messages = []

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FailingApprovalClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "333.444"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: direct_messages.append((user_id, text)),
    )

    await main_module._handle_reaction_added(
        {
            "user": "UNOTADMIN",
            "reaction": "white_check_mark",
            "item": {"type": "message", "channel": "C123", "ts": "222.333"},
        }
    )

    assert posted_messages == []
    assert direct_messages
    assert direct_messages[0][0] == "UNOTADMIN"
    assert "Not a points admin" in direct_messages[0][1]


@pytest.mark.asyncio
async def test_reaction_approval_uses_cached_summary_mapping_when_backend_lookup_misses(monkeypatch):
    posted_messages = []
    direct_messages = []

    approval_module.remember_points_request_summary(
        "C123",
        "222.333",
        {
            "id": 9,
            "status": "pending",
            "requester_slack_id": "UREQUESTER",
            "target_slack_id": "UREQUESTER",
            "points": 18,
            "reason": "helping out at the openclaw event last night",
            "slack_thread_ts": "111.222",
        },
    )

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", LookupMissApprovalClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "333.444"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: direct_messages.append((user_id, text)),
    )

    await main_module._handle_reaction_added(
        {
            "user": "UADMIN",
            "reaction": "white_check_mark",
            "item": {"type": "message", "channel": "C123", "ts": "222.333"},
        }
    )

    assert LookupMissApprovalClient.last_instance.lookup_args == ("C123", "222.333")
    assert LookupMissApprovalClient.last_instance.approve_args == (9, "UADMIN")
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert "<@UREQUESTER> received 5 points" in posted_messages[0]["text"]
    assert approval_module.get_remembered_points_request_summary("C123", "222.333") is None
    assert direct_messages == []


@pytest.mark.asyncio
async def test_reaction_approval_uses_message_metadata_for_heavy_check_mark(monkeypatch):
    posted_messages = []
    direct_messages = []

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            INTERNAL_API_KEY="internal-key",
            ROO_API_KEY="roo-api-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", LookupMissApprovalClient)
    monkeypatch.setattr(
        main_module,
        "get_message",
        lambda channel, message_ts: {
            "metadata": {
                "event_type": "roo_points_request",
                "event_payload": {
                    "request_id": 12,
                    "requester_slack_id": "UREQUESTER",
                    "target_slack_id": "UREQUESTER",
                    "points": 18,
                    "reason": "helping out at the openclaw event last night",
                    "slack_thread_ts": "111.222",
                },
            }
        },
    )
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "333.444"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: direct_messages.append((user_id, text)),
    )

    await main_module._handle_reaction_added(
        {
            "user": "UADMIN",
            "reaction": "heavy_check_mark",
            "item": {"type": "message", "channel": "C123", "ts": "222.333"},
        }
    )

    assert LookupMissApprovalClient.last_instance.lookup_args == ("C123", "222.333")
    assert LookupMissApprovalClient.last_instance.approve_args == (12, "UADMIN")
    assert posted_messages[0]["thread_ts"] == "111.222"
    assert "<@UREQUESTER> received 5 points" in posted_messages[0]["text"]
    assert direct_messages == []


@pytest.mark.asyncio
async def test_reaction_approval_ignores_other_emoji(monkeypatch):
    posted_messages = []
    direct_messages = []

    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeApprovalClient)
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: posted_messages.append(kwargs) or {"ts": "333.444"},
    )
    monkeypatch.setattr(
        main_module,
        "send_dm",
        lambda user_id, text, **kwargs: direct_messages.append((user_id, text)),
    )

    await main_module._handle_reaction_added(
        {
            "user": "UADMIN",
            "reaction": "thumbsup",
            "item": {"type": "message", "channel": "C123", "ts": "222.333"},
        }
    )

    assert posted_messages == []
    assert direct_messages == []


def test_linear_meeting_file_request_helper_allows_short_attached_file_prompt():
    assert main_module._looks_like_linear_meeting_file_request(
        "send this to Linear as tasks",
        has_files=True,
    )
    assert not main_module._looks_like_linear_meeting_file_request(
        "send this to Linear as tasks",
        has_files=False,
    )


@pytest.mark.asyncio
async def test_slack_events_start_here_message_triggers_intro_handler(monkeypatch):
    handled_events = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task

    async def fake_handle_start_here_intro(event):
        handled_events.append(event)

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module, "_handle_start_here_intro", fake_handle_start_here_intro)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UINTRO",
            "channel": "CSTART",
            "ts": "111.222",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    await asyncio.gather(*scheduled_tasks)
    assert handled_events == [payload["event"]]


@pytest.mark.asyncio
async def test_slack_events_ignores_start_here_thread_reply(monkeypatch):
    scheduled = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append("called")
        return None

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UINTRO",
            "channel": "CSTART",
            "ts": "111.222",
            "thread_ts": "111.222",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    assert scheduled == []


@pytest.mark.asyncio
async def test_slack_events_non_start_here_message_does_not_trigger_intro_handler(monkeypatch):
    scheduled = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append("called")
        return None

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UOTHER",
            "channel": "COTHER",
            "ts": "111.222",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    assert scheduled == []


@pytest.mark.asyncio
async def test_slack_events_bot_message_does_not_trigger_intro_handler(monkeypatch):
    scheduled = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append("called")
        return None

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UBOT",
            "channel": "CSTART",
            "ts": "111.222",
            "bot_id": "B123",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    assert scheduled == []


@pytest.mark.asyncio
async def test_slack_events_message_subtype_does_not_trigger_intro_handler(monkeypatch):
    scheduled = []

    def fake_create_task(coro):
        coro.close()
        scheduled.append("called")
        return None

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "user": "UINTRO",
            "channel": "CSTART",
            "ts": "111.222",
            "subtype": "message_changed",
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    assert scheduled == []


@pytest.mark.asyncio
async def test_slack_events_start_here_message_edit_rechecks_intro(monkeypatch):
    handled_events = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task

    async def fake_handle_start_here_intro(event):
        handled_events.append(event)

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(slack_client_module, "get_channel_id", lambda name: "CSTART")
    monkeypatch.setattr(main_module, "_handle_start_here_intro", fake_handle_start_here_intro)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "event": {
            "type": "message",
            "subtype": "message_changed",
            "channel": "CSTART",
            "message": {
                "type": "message",
                "user": "UINTRO",
                "channel": "CSTART",
                "ts": "111.222",
                "text": "Hi, I'm Sam and my startup helps founders test ideas.",
            },
        }
    }

    class FakeRequest:
        async def json(self):
            return payload

    response = await main_module.slack_events(FakeRequest())

    assert response.status_code == 200
    await asyncio.gather(*scheduled_tasks)
    assert handled_events == [payload["event"]]


@pytest.mark.asyncio
async def test_slack_events_dedupes_retried_app_mention(monkeypatch):
    handled_events = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task
    main_module._recent_app_mention_events.clear()

    async def fake_handle_mention(event):
        handled_events.append(event)

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(main_module, "_handle_mention", fake_handle_mention)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    payload = {
        "team_id": "T123",
        "event": {
            "type": "app_mention",
            "user": "U123",
            "channel": "C123",
            "ts": "111.222",
            "text": "<@UROO> book me in today",
        },
    }

    class FakeRequest:
        async def json(self):
            return payload

    response_1 = await main_module.slack_events(FakeRequest())
    response_2 = await main_module.slack_events(FakeRequest())

    assert response_1.status_code == 200
    assert response_2.status_code == 200
    await asyncio.gather(*scheduled_tasks)
    assert handled_events == [payload["event"]]
    main_module._recent_app_mention_events.clear()


@pytest.mark.asyncio
async def test_slack_events_allows_new_app_mention_timestamp(monkeypatch):
    handled_events = []
    scheduled_tasks = []
    real_create_task = asyncio.create_task
    main_module._recent_app_mention_events.clear()

    async def fake_handle_mention(event):
        handled_events.append(event)

    def fake_create_task(coro):
        task = real_create_task(coro)
        scheduled_tasks.append(task)
        return task

    monkeypatch.setattr(main_module, "_handle_mention", fake_handle_mention)
    monkeypatch.setattr(main_module.asyncio, "create_task", fake_create_task)

    first_event = {
        "type": "app_mention",
        "user": "U123",
        "channel": "C123",
        "ts": "111.222",
        "text": "<@UROO> book me in today",
    }
    second_event = dict(first_event, ts="111.333")

    class FakeRequest:
        def __init__(self, event):
            self._payload = {"team_id": "T123", "event": event}

        async def json(self):
            return self._payload

    response_1 = await main_module.slack_events(FakeRequest(first_event))
    response_2 = await main_module.slack_events(FakeRequest(second_event))

    assert response_1.status_code == 200
    assert response_2.status_code == 200
    await asyncio.gather(*scheduled_tasks)
    assert handled_events == [first_event, second_event]
    main_module._recent_app_mention_events.clear()


def test_app_mention_dedupe_logs_ttl_expiry(capsys):
    main_module._recent_app_mention_events.clear()
    event = {
        "type": "app_mention",
        "user": "U123",
        "channel": "C123",
        "ts": "111.222",
    }
    payload = {"team_id": "T123", "event": event}

    assert main_module._mark_app_mention_event_seen(payload, event, now=1000.0)
    assert main_module._mark_app_mention_event_seen(payload, dict(event, ts="111.333"), now=2000.0)

    captured = capsys.readouterr()
    assert "Slack app_mention dedupe TTL expired count=1" in captured.out
    main_module._recent_app_mention_events.clear()


@pytest.mark.asyncio
async def test_handle_start_here_intro_delegates_to_skill(monkeypatch):
    handled = []

    async def fake_handle(event):
        handled.append(event)
        return {"status": "awarded"}

    monkeypatch.setattr(main_module, "handle_start_here_intro", fake_handle)
    event = {
        "type": "message",
        "user": "UINTRO",
        "channel": "CSTART",
        "ts": "111.222",
        "text": "Hi, I'm Sam and my startup helps founders test ideas.",
    }

    result = await main_module._handle_start_here_intro(event)

    assert result == {"status": "awarded"}
    assert handled == [event]


@pytest.mark.asyncio
async def test_backend_client_create_points_request_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"id": 42})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.create_points_request(
        requester_slack_id="UREQUESTER",
        target_slack_id="<@UTARGET>",
        points=12,
        reason="running the 21st x MLAI event",
        slack_channel_id="C123",
        slack_thread_ts="111.222",
    )

    assert result == {"id": 42}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "POST"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/requests/"
    assert recorder.calls[0]["json"] == {
        "requester_slack_id": "UREQUESTER",
        "target_slack_id": "UTARGET",
        "points": 12,
        "reason": "running the 21st x MLAI event",
        "slack_channel_id": "C123",
        "slack_thread_ts": "111.222",
    }
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "api-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 10.0


@pytest.mark.asyncio
async def test_backend_client_create_points_purchase_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(
        json_data={
            "id": "purchase-uuid",
            "frontend_checkout_page_url": "https://mlai.au/roo/topup/purchase-uuid",
        }
    )
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.create_points_purchase(
        slack_user_id="<@U123>",
        pack_id="topup_10",
        purchase_from={
            "source": "slack",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        },
    )

    assert result == {
        "id": "purchase-uuid",
        "frontend_checkout_page_url": "https://mlai.au/roo/topup/purchase-uuid",
    }
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "POST"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/purchases/"
    assert recorder.calls[0]["json"] == {
        "slack_user_id": "U123",
        "pack_id": "topup_10",
        "purchase_from": {
            "source": "slack",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        },
    }
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "api-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 10.0


@pytest.mark.asyncio
async def test_backend_client_create_checkout_options_uses_canonical_endpoint(
    monkeypatch,
):
    response_payload = {
        "checkout_request_id": "EvTopup123",
        "options": [
            {
                "pack_id": "topup_10",
                "points_amount": 20,
                "amount_cents": 3699,
                "currency": "aud",
                "checkout_session_url": (
                    "https://checkout.stripe.com/c/pay/cs_test_topup_10"
                ),
            }
        ],
        "errors": [],
    }
    recorder = RecordingAsyncClient(json_data=response_payload)
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.create_points_checkout_options(
        slack_user_id="<@U123>",
        checkout_request_id="EvTopup123",
        pack_ids=["topup_10"],
        purchase_from={
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        },
    )

    assert result == response_payload
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "POST"
    assert (
        recorder.calls[0]["url"]
        == "https://backend.test/api/v1/points/purchases/checkout-options/"
    )
    assert recorder.calls[0]["json"] == {
        "slack_user_id": "U123",
        "checkout_request_id": "EvTopup123",
        "pack_ids": ["topup_10"],
        "purchase_from": {
            "source": "slack",
            "slack_channel_id": "C123",
            "slack_thread_ts": "111.222",
        },
    }
    assert recorder.calls[0]["timeout"] == 30.0


@pytest.mark.asyncio
async def test_backend_client_attach_points_request_summary_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"ok": True})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.attach_points_request_slack_summary(
        request_id=42,
        slack_channel_id="C123",
        slack_thread_ts="111.222",
        slack_summary_message_ts="222.333",
    )

    assert result == {"ok": True}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "PATCH"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/requests/42/slack-summary/"
    assert recorder.calls[0]["json"] == {
        "slack_channel_id": "C123",
        "slack_summary_message_ts": "222.333",
        "slack_thread_ts": "111.222",
    }
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "internal-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 10.0


@pytest.mark.asyncio
async def test_backend_client_lookup_points_request_by_slack_message_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"id": 42, "status": "pending"})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.get_points_request_by_slack_message(
        slack_channel_id="C123",
        slack_message_ts="222.333",
    )

    assert result == {"id": 42, "status": "pending"}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "GET"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/requests/by-slack-message/"
    assert recorder.calls[0]["json"] is None
    assert recorder.calls[0]["params"] == {
        "slack_channel_id": "C123",
        "slack_message_ts": "222.333",
    }
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "internal-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 10.0


@pytest.mark.asyncio
async def test_backend_client_approve_points_request_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"points_awarded": 12, "new_balance": 17})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.approve_points_request(
        request_id=42,
        admin_slack_id="UADMIN",
    )

    assert result == {"points_awarded": 12, "new_balance": 17}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "POST"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/requests/42/approve/"
    assert recorder.calls[0]["json"] == {"admin_slack_id": "UADMIN"}
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "internal-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 15.0


@pytest.mark.asyncio
async def test_backend_client_award_first_channel_post_uses_canonical_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"awarded": True, "new_balance": 4, "points_awarded": 4})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.award_first_channel_post(
        slack_user_id="<@UINTRO>",
        channel_id="CSTART",
    )

    assert result == {"awarded": True, "new_balance": 4, "points_awarded": 4}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "POST"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/activity/first-post-award/"
    assert recorder.calls[0]["json"] == {
        "slack_user_id": "UINTRO",
        "channel_id": "CSTART",
    }
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "internal-key"
    assert recorder.calls[0]["headers"]["X-Request-ID"].startswith("roo-")
    assert recorder.calls[0]["timeout"] == 15.0


@pytest.mark.asyncio
async def test_backend_client_system_award_points_uses_system_endpoint(monkeypatch):
    recorder = RecordingAsyncClient(json_data={"points_awarded": 12, "new_balance": 17})
    monkeypatch.setattr(backend_module.httpx, "AsyncClient", lambda: recorder)

    client = backend_module.MLAIBackendClient(
        base_url="https://backend.test",
        api_key="api-key",
        internal_api_key="internal-key",
    )

    result = await client.system_award_points(
        admin_slack_id="UROOBOT",
        target_slack_id="<@UTARGET>",
        points=12,
        reason="System award",
        idempotency_key="link_love:CBOOST:111.000:UTARGET",
    )

    assert result == {"points_awarded": 12, "new_balance": 17}
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["method"] == "POST"
    assert recorder.calls[0]["url"] == "https://backend.test/api/v1/points/system/award/"
    assert recorder.calls[0]["json"] == {
        "created_by_slack_id": "UROOBOT",
        "target_slack_id": "UTARGET",
        "points": 12,
        "reason": "System award",
        "idempotency_key": "link_love:CBOOST:111.000:UTARGET",
    }
    assert recorder.calls[0]["params"] is None
    assert recorder.calls[0]["headers"]["Content-Type"] == "application/json"
    assert recorder.calls[0]["headers"]["X-API-Key"] == "internal-key"
    assert recorder.calls[0]["timeout"] == 15.0


@pytest.mark.asyncio
async def test_execute_mlai_points_returns_backend_unavailable_message(monkeypatch):
    executor = SkillExecutor()
    skill = SimpleNamespace(name="mlai-points")

    async def fake_handle_points_action(**kwargs):
        raise backend_module.MLAIBackendUnavailableError("backend unavailable")

    monkeypatch.setattr(
        executor,
        "_handle_points_action",
        fake_handle_points_action,
    )
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )

    result = await executor._execute_mlai_points(
        skill=skill,
        text="book coworking today",
        params={"action": "book_coworking", "date": "today"},
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "couldn't confirm whether your coworking booking went through" in result.lower()


@pytest.mark.asyncio
async def test_book_coworking_still_succeeds_when_balance_refresh_times_out(tmp_path):
    monkeypatch = pytest.MonkeyPatch()
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")

    class FakeCoworkingClient:
        async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
            return {"points_cost": 4}

        async def get_balance(self, slack_user_id):
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")

    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: __import__("datetime").date(2026, 4, 9))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    try:
        result = await executor._handle_points_action(
            client=FakeCoworkingClient(),
            action="book_coworking",
            params={"date": "2026-04-09"},
            text="book coworking today",
            user_id="U123",
            channel_id="C123",
            thread_ts="111.222",
            skill=SimpleNamespace(name="mlai-points"),
        )
    finally:
        monkeypatch.undo()

    assert "Booked you in for **2026-04-09**" in result
    assert "Balance remaining" not in result


@pytest.mark.asyncio
async def test_book_coworking_defaults_missing_date_to_today(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")

    class FakeCoworkingClient:
        def __init__(self):
            self.book_args = None
            self.balance_args = None

        async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
            self.book_args = (slack_user_id, booking_date, slack_channel_id)
            return {"points_cost": 1}

        async def get_balance(self, slack_user_id):
            self.balance_args = slack_user_id
            return {"balance": 9}

    client = FakeCoworkingClient()
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    result = await executor._handle_points_action(
        client=client,
        action="book_coworking",
        params={},
        text="book me in",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    intent = store.get_by_key("coworking:U123:2026-05-04")
    assert "Booked you in for **2026-05-04**" in result
    assert client.book_args == ("U123", "2026-05-04", "C123")
    assert client.balance_args == "U123"
    assert intent["requested_by_slack_id"] == "U123"


class FakeAdminCheckinCoworkingClient:
    def __init__(
        self,
        *,
        role="admin",
        book_exception=None,
        balance=14,
    ):
        self.role = role
        self.book_exception = book_exception
        self.balance = balance
        self.admin_checks = []
        self.book_args = None
        self.balance_args = None

    async def get_admin_details(self, slack_user_id):
        self.admin_checks.append(slack_user_id)
        if not self.role:
            return None
        return {"slack_user_id": slack_user_id, "role": self.role}

    async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
        self.book_args = (slack_user_id, booking_date, slack_channel_id)
        if self.book_exception:
            raise self.book_exception
        return {"points_cost": 1}

    async def get_balance(self, slack_user_id):
        self.balance_args = slack_user_id
        return {"balance": self.balance}


@pytest.mark.asyncio
async def test_admin_checkin_books_target_for_today(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")
    client = FakeAdminCheckinCoworkingClient(balance=13)
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UROO")

    result = await executor._handle_points_action(
        client=client,
        action="admin_checkin_coworking",
        params={},
        text="check <@UTARGET> in today",
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    intent = store.get_by_key("coworking:UTARGET:2026-05-04")
    assert "Checked <@UTARGET> in for **2026-05-04**" in result
    assert "Their balance: 13 pts" in result
    assert client.book_args == ("UTARGET", "2026-05-04", "C123")
    assert client.balance_args == "UTARGET"
    assert intent["requested_by_slack_id"] == "UADMIN"


@pytest.mark.asyncio
async def test_admin_checkin_book_mention_phrase_books_target_for_today(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")
    client = FakeAdminCheckinCoworkingClient(balance=13)
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UROO")

    action = executor._resolve_routed_points_action(
        {"action": "book_coworking", "target_users": ["<@UTARGET>"]},
        "also book <@UTARGET> in today",
    )
    result = await executor._handle_points_action(
        client=client,
        action=action,
        params={"action": "book_coworking", "target_users": ["<@UTARGET>"]},
        text="also book <@UTARGET> in today",
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    intent = store.get_by_key("coworking:UTARGET:2026-05-04")
    assert action == "admin_checkin_coworking"
    assert "Checked <@UTARGET> in for **2026-05-04**" in result
    assert client.book_args == ("UTARGET", "2026-05-04", "C123")
    assert client.balance_args == "UTARGET"
    assert intent["requested_by_slack_id"] == "UADMIN"


@pytest.mark.asyncio
async def test_admin_checkin_honors_explicit_date(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")
    client = FakeAdminCheckinCoworkingClient()
    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UROO")

    result = await executor._handle_points_action(
        client=client,
        action="admin_checkin_coworking",
        params={"date": "2026-06-01"},
        text="check <@UTARGET> in 2026-06-01",
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "Checked <@UTARGET> in for **2026-06-01**" in result
    assert client.book_args == ("UTARGET", "2026-06-01", "C123")


@pytest.mark.asyncio
async def test_admin_checkin_denies_partner_without_booking(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")
    client = FakeAdminCheckinCoworkingClient(role="partner")
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UROO")

    result = await executor._handle_points_action(
        client=client,
        action="admin_checkin_coworking",
        params={},
        text="book <@UTARGET> in today",
        user_id="UPARTNER",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "partner admins can only generate coworking reports" in result
    assert client.book_args is None
    assert store.counts_by_status() == {}


@pytest.mark.asyncio
async def test_admin_checkin_requires_exactly_one_target(monkeypatch):
    executor = SkillExecutor()
    client = FakeAdminCheckinCoworkingClient()
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UROO")

    missing_result = await executor._handle_points_action(
        client=client,
        action="admin_checkin_coworking",
        params={},
        text="check in today",
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )
    multiple_result = await executor._handle_points_action(
        client=client,
        action="admin_checkin_coworking",
        params={},
        text="book <@UONE> and <@UTWO> in today",
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "Mention exactly one user" in missing_result
    assert "Tag exactly one user" in multiple_result
    assert client.book_args is None


@pytest.mark.asyncio
async def test_book_coworking_with_target_mention_refuses_self_booking(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")
    client = FakeAdminCheckinCoworkingClient()
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UROO")

    result = await executor._handle_points_action(
        client=client,
        action="book_coworking",
        params={"action": "book_coworking", "target_users": ["<@UTARGET>"]},
        text="book <@UTARGET> in today",
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "I saw a tagged user" in result
    assert client.book_args is None
    assert store.counts_by_status() == {}


@pytest.mark.asyncio
async def test_admin_checkin_insufficient_balance_message_names_target(tmp_path, monkeypatch):
    request = httpx.Request("POST", "https://backend.test/api/v1/points/coworking/book/")
    response = httpx.Response(
        400,
        request=request,
        json={"detail": "Insufficient balance: need 1 point"},
    )
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")
    client = FakeAdminCheckinCoworkingClient(
        book_exception=httpx.HTTPStatusError("bad request", request=request, response=response),
        balance=0,
    )
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)
    monkeypatch.setattr(slack_client_module, "get_bot_user_id", lambda: "UROO")

    result = await executor._handle_points_action(
        client=client,
        action="admin_checkin_coworking",
        params={},
        text="check <@UTARGET> in today",
        user_id="UADMIN",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    intent = store.get_by_key("coworking:UTARGET:2026-05-04")
    assert "I couldn't check <@UTARGET> in" in result
    assert "Their current balance is **0 points**" in result
    assert "UADMIN" not in result
    assert intent["status"] == "blocked"


def make_coworking_report(start_date: str, end_date: str, counts: list[int], unique_users: int = 2) -> dict:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    range_days = (end - start).days + 1
    daily = []
    for offset in range(range_days):
        daily_date = start + timedelta(days=offset)
        daily.append({
            "date": daily_date.isoformat(),
            "booked_users": counts[offset] if offset < len(counts) else 0,
        })

    total = sum(row["booked_users"] for row in daily)
    active_days = sum(1 for row in daily if row["booked_users"] > 0)
    max_count = max((row["booked_users"] for row in daily), default=0)
    busiest_days = [
        {"date": row["date"], "booked_users": row["booked_users"]}
        for row in daily
        if max_count > 0 and row["booked_users"] == max_count
    ]
    weekly = [{
        "week_start": start_date,
        "booked_user_days": total,
        "active_days": active_days,
    }]
    monthly = [{
        "month": start_date[:7],
        "booked_user_days": total,
        "active_days": active_days,
    }]
    return {
        "range": {
            "start_date": start_date,
            "end_date": end_date,
            "source": "active_coworking_bookings",
        },
        "totals": {
            "booked_user_days": total,
            "unique_users": unique_users,
            "active_days": active_days,
            "range_days": range_days,
            "average_per_day": round(total / range_days, 2),
            "busiest_days": busiest_days,
        },
        "monthly": monthly,
        "weekly": weekly,
        "daily": daily,
    }


class FakeCoworkingReportClient:
    def __init__(self, admin_slack_ids=None, admin_roles=None, reports_by_range=None):
        self.calls = []
        self.admin_checks = []
        self.reports_by_range = reports_by_range or {}
        self.admin_roles = {
            slack_id: "admin"
            for slack_id in (admin_slack_ids or [executor_module.POINTS_SUPER_ADMIN_SLACK_ID])
        }
        self.admin_roles.update(admin_roles or {})

    async def get_admin_details(self, slack_user_id):
        self.admin_checks.append(slack_user_id)
        role = self.admin_roles.get(slack_user_id)
        if not role:
            return None
        return {"slack_user_id": slack_user_id, "role": role}

    async def get_coworking_report(self, slack_user_id, start_date, end_date):
        self.calls.append((slack_user_id, start_date, end_date))
        return self.reports_by_range.get(
            (start_date, end_date),
            make_coworking_report(start_date, end_date, [2, 1], unique_users=2),
        )


@pytest.mark.asyncio
async def test_coworking_report_exact_range_formats_slack_report():
    client = FakeCoworkingReportClient()
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="coworking report from 2026-01-01 to 2026-01-31",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-01-01", "2026-01-31")
    ]
    assert "*Coworking usage*" in result
    assert "Source: Active coworking bookings (not door check-ins)" in result
    assert "Booked user-days: 3" in result
    assert "Unique users: 2" in result
    assert "*Monthly*" in result
    assert "*Weekly*" in result
    assert "*Daily*" not in result
    assert "2026-01" in result
    assert "2026-01-01" in result
    assert "```" in result


@pytest.mark.asyncio
async def test_coworking_report_allows_points_admin():
    client = FakeCoworkingReportClient(admin_slack_ids=["UPOINTSADMIN"])
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
        text="coworking report 2026-01-01 2026-01-31",
        user_id="UPOINTSADMIN",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.admin_checks == ["UPOINTSADMIN"]
    assert client.calls == [("UPOINTSADMIN", "2026-01-01", "2026-01-31")]
    assert "Source: Active coworking bookings" in result


@pytest.mark.asyncio
async def test_coworking_report_allows_partner_admin():
    client = FakeCoworkingReportClient(admin_roles={"UPARTNER": "partner"})
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
        text="coworking report 2026-01-01 2026-01-31",
        user_id="UPARTNER",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.admin_checks == ["UPARTNER"]
    assert client.calls == [("UPARTNER", "2026-01-01", "2026-01-31")]
    assert "Source: Active coworking bookings" in result


@pytest.mark.asyncio
async def test_coworking_report_denies_non_points_admin_without_report_call():
    client = FakeCoworkingReportClient()
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={"start_date": "2026-01-01", "end_date": "2026-01-31"},
        text="coworking report 2026-01-01 2026-01-31",
        user_id="UOTHER",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "need to be a Points Admin to generate coworking reports" in result
    assert client.admin_checks == ["UOTHER"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_coworking_report_last_three_months_uses_current_date(monkeypatch):
    client = FakeCoworkingReportClient()
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 4, 28))

    await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="coworking report last 3 months",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-01-29", "2026-04-28")
    ]


@pytest.mark.asyncio
async def test_coworking_report_this_week_uses_monday_through_today(monkeypatch):
    client = FakeCoworkingReportClient()
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 4, 28))

    await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="how many people used the coworking space this week",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-04-27", "2026-04-28")
    ]


@pytest.mark.asyncio
async def test_coworking_report_last_week_uses_previous_sunday_through_saturday(monkeypatch):
    client = FakeCoworkingReportClient()
    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 4, 28))

    await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="give me a report for how many people used the coworking space last week",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-04-19", "2026-04-25")
    ]


@pytest.mark.asyncio
async def test_coworking_report_last_week_compares_week_prior(monkeypatch):
    async def fail_chat(*args, **kwargs):
        raise RuntimeError("no llm in deterministic comparison test")

    monkeypatch.setattr(executor_module, "chat", fail_chat)
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    client = FakeCoworkingReportClient(
        reports_by_range={
            ("2026-04-26", "2026-05-02"): make_coworking_report(
                "2026-04-26",
                "2026-05-02",
                [0, 1, 4, 6, 7, 8, 2],
                unique_users=19,
            ),
            ("2026-04-19", "2026-04-25"): make_coworking_report(
                "2026-04-19",
                "2026-04-25",
                [0, 0, 3, 4, 5, 4, 3],
                unique_users=14,
            ),
        }
    )
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="how many people used the coworking space last week and how does that compare to the week prior?",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-04-26", "2026-05-02"),
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-04-19", "2026-04-25"),
    ]
    assert "Last week had 28 booked user-days" in result
    assert "up 9 (+47.4%)" in result
    assert "Week prior: 2026-04-19 to 2026-04-25" in result


@pytest.mark.asyncio
async def test_coworking_report_previous_period_uses_same_length_range(monkeypatch):
    async def fail_chat(*args, **kwargs):
        raise RuntimeError("no llm in deterministic comparison test")

    monkeypatch.setattr(executor_module, "chat", fail_chat)
    client = FakeCoworkingReportClient(
        reports_by_range={
            ("2026-01-08", "2026-01-14"): make_coworking_report(
                "2026-01-08",
                "2026-01-14",
                [2, 2, 2, 2, 2, 2, 2],
            ),
            ("2026-01-01", "2026-01-07"): make_coworking_report(
                "2026-01-01",
                "2026-01-07",
                [1, 1, 1, 1, 1, 1, 1],
            ),
        }
    )
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="coworking report from 2026-01-08 to 2026-01-14 compared with the previous period",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-01-08", "2026-01-14"),
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-01-01", "2026-01-07"),
    ]
    assert "up 7 (+100.0%)" in result


@pytest.mark.asyncio
async def test_coworking_report_busiest_day_last_month(monkeypatch):
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    counts = [0] * 30
    counts[14] = 8
    client = FakeCoworkingReportClient(
        reports_by_range={
            ("2026-04-01", "2026-04-30"): make_coworking_report(
                "2026-04-01",
                "2026-04-30",
                counts,
            ),
        }
    )
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="which day was busiest for coworking last month?",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.calls == [
        (executor_module.POINTS_SUPER_ADMIN_SLACK_ID, "2026-04-01", "2026-04-30")
    ]
    assert "Last month's busiest day was 2026-04-15 (8)" in result


@pytest.mark.asyncio
async def test_coworking_report_trends_and_recommendations_use_gpt54(monkeypatch):
    captured = {}

    async def fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            content=(
                "🏢 *Coworking usage*\n"
                "Range: 2026-01-29 to 2026-04-28\n"
                "Source: Active coworking bookings (not door check-ins)\n\n"
                "*Interpretation*\n"
                "Usage is concentrated mid-week."
            )
        )

    monkeypatch.setattr(executor_module, "chat", fake_chat)
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 4, 28))
    client = FakeCoworkingReportClient(
        reports_by_range={
            ("2026-01-29", "2026-04-28"): make_coworking_report(
                "2026-01-29",
                "2026-04-28",
                [1] * 90,
            ),
        }
    )
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="coworking report last 3 months with any trends or recommendations",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert captured["kwargs"]["model"] == "gpt-5.4"
    assert "active coworking bookings" in captured["messages"][1]["content"]
    assert "*Interpretation*" in result
    assert "Usage is concentrated mid-week." in result


@pytest.mark.asyncio
async def test_coworking_report_gpt_failure_falls_back_to_deterministic_summary(monkeypatch):
    async def fail_chat(*args, **kwargs):
        raise RuntimeError("gpt unavailable")

    monkeypatch.setattr(executor_module, "chat", fail_chat)
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 4, 28))
    client = FakeCoworkingReportClient(
        reports_by_range={
            ("2026-01-29", "2026-04-28"): make_coworking_report(
                "2026-01-29",
                "2026-04-28",
                [1] * 90,
            ),
        }
    )
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="coworking_report",
        params={},
        text="coworking report last 3 months with any trends",
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "*Coworking usage*" in result
    assert "Booked user-days: 90" in result
    assert "*Interpretation*" in result


def test_extract_http_error_detail_suppresses_html_500_body():
    executor = SkillExecutor()
    request = httpx.Request("GET", "https://backend.test/api/v1/points/coworking/report/")
    response = httpx.Response(
        500,
        request=request,
        text="<!doctype html><html><body><h1>Server Error (500)</h1></body></html>",
    )
    exc = httpx.HTTPStatusError("server error", request=request, response=response)

    detail = executor._extract_http_error_detail(exc)

    assert "<!doctype html>" not in detail
    assert "temporarily unavailable" in detail


@pytest.mark.asyncio
async def test_execute_mlai_points_html_500_uses_backend_unavailable_message(monkeypatch):
    executor = SkillExecutor()
    skill = SimpleNamespace(name="mlai-points")

    async def fake_handle_points_action(**kwargs):
        request = httpx.Request(
            "GET",
            "https://backend.test/api/v1/points/coworking/report/",
        )
        response = httpx.Response(
            500,
            request=request,
            text="<!doctype html><html><body><h1>Server Error (500)</h1></body></html>",
        )
        raise httpx.HTTPStatusError("server error", request=request, response=response)

    monkeypatch.setattr(executor, "_handle_points_action", fake_handle_points_action)
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )

    result = await executor._execute_mlai_points(
        skill=skill,
        text="how many people used the coworking space this week",
        params={"action": "coworking_report"},
        user_id=executor_module.POINTS_SUPER_ADMIN_SLACK_ID,
        channel_id="C123",
        thread_ts="111.222",
    )

    assert "<!doctype html>" not in result
    assert "couldn't reach the MLAI points backend" in result


@pytest.mark.asyncio
async def test_book_coworking_persists_intent_and_queues_backend_timeout(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")

    class TimeoutCoworkingClient:
        async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
            raise backend_module.MLAIBackendUnavailableError("backend unavailable")

    executor = SkillExecutor()
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    result = await executor._handle_points_action(
        client=TimeoutCoworkingClient(),
        action="book_coworking",
        params={"date": "2026-04-22"},
        text="book coworking today",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    intent = store.get_by_key("coworking:U123:2026-04-22")
    assert "queued it and will keep retrying automatically" in result
    assert intent["status"] == "pending_retry"
    assert intent["slack_user_id"] == "U123"
    assert intent["booking_date"] == "2026-04-22"
    assert intent["channel_id"] == "C123"
    assert intent["thread_ts"] == "111.222"
    assert "backend unavailable" in intent["last_error"]


@pytest.mark.asyncio
async def test_dependency_health_check_reports_degraded_backend(monkeypatch):
    class FakeBackendClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_backend_readiness(self):
            return {"status": "ok"}

        async def get_points_health(self):
            raise backend_module.MLAIBackendUnavailableError("points probe failed")

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-api-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeBackendClient)
    main_module.app.state.startup_complete = True

    result = await main_module.dependency_health_check()

    assert result["status"] == "degraded"
    assert result["dependencies"]["mlai_backend"]["status"] == "degraded"
    assert result["dependencies"]["mlai_backend"]["readiness"]["status"] == "ok"
    assert "points_error" in result["dependencies"]["mlai_backend"]


@pytest.mark.asyncio
async def test_check_coworking_passes_slack_user_id_for_per_user_pricing():
    class FakeAvailabilityClient:
        def __init__(self):
            self.call = None

        async def check_coworking(self, check_date=None, days=7, slack_user_id=None):
            self.call = (check_date, days, slack_user_id)
            return [
                {
                    "date": "2026-05-04",
                    "available_slots": 5,
                    "cost_points": 4,
                    "is_bookable": True,
                }
            ]

    client = FakeAvailabilityClient()
    executor = SkillExecutor()

    result = await executor._handle_points_action(
        client=client,
        action="check_coworking",
        params={"date": "2026-05-04", "days": 7},
        text="coworking availability",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    # The user's slack id is forwarded so the backend can quote the discounted price.
    assert client.call == ("2026-05-04", 7, "U123")
    assert "4 pt" in result


@pytest.mark.asyncio
async def test_book_coworking_nudges_founder_when_charged_standard_price(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")

    class FakeCoworkingClient:
        async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
            return {"points_cost": 8, "monthly_update_discount_applied": False}

        async def get_balance(self, slack_user_id):
            return {"balance": 12}

    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    result = await executor._handle_points_action(
        client=FakeCoworkingClient(),
        action="book_coworking",
        params={"date": "2026-05-04"},
        text="book coworking today",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "Booked you in for **2026-05-04**" in result
    assert "Cost: 8 points" in result
    assert "submit a monthly update" in result
    assert "https://mlai.au/platform/login?app=founder-tools&next=/founder-tools" in result


@pytest.mark.asyncio
async def test_book_coworking_omits_nudge_when_discount_applied(tmp_path, monkeypatch):
    store = coworking_module.CoworkingBookingIntentStore(tmp_path / "intents.db")

    class FakeCoworkingClient:
        async def book_coworking(self, slack_user_id, booking_date, slack_channel_id=None):
            return {"points_cost": 4, "monthly_update_discount_applied": True}

        async def get_balance(self, slack_user_id):
            return {"balance": 12}

    executor = SkillExecutor()
    monkeypatch.setattr("roo.utils.get_current_date", lambda: date(2026, 5, 4))
    monkeypatch.setattr(executor_module, "get_coworking_intent_store", lambda: store)

    result = await executor._handle_points_action(
        client=FakeCoworkingClient(),
        action="book_coworking",
        params={"date": "2026-05-04"},
        text="book coworking today",
        user_id="U123",
        channel_id="C123",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "Booked you in for **2026-05-04**" in result
    assert "Cost: 4 points" in result
    assert "submit a monthly update" not in result
    assert "founder-tools" not in result
