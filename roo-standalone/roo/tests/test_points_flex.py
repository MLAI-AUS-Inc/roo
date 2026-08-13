import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-points-flex-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "points-flex-signing-test")
os.environ.setdefault("OPENAI_API_KEY", "points-flex-openai-test")

from roo import agent as agent_module
from roo import main as main_module
from roo import points_flex as flex_module
from roo import slack_client as slack_client_module
from roo.agent import RooAgent
from roo.points_flex import (
    POINTS_FLEX_ACTION_ID,
    POINTS_FLEX_DELETE_ACTION_ID,
    PointsFlexShareStore,
    PointsFlexTokenError,
    issue_points_flex_confirmation,
    issue_points_flex_deletion,
    parse_lifetime_earned,
    verify_points_flex_confirmation,
    verify_points_flex_deletion,
)
from roo.skills import executor as executor_module
from roo.skills.executor import SkillExecutor
from roo.slack_security import SlackRequestReceiptStore


SIGNING_SECRET = "points-flex-test-secret"


@pytest.fixture(autouse=True)
def clear_flex_store_cache():
    flex_module.get_points_flex_store.cache_clear()
    yield
    flex_module.get_points_flex_store.cache_clear()


def _settings(tmp_path):
    return SimpleNamespace(
        SLACK_SIGNING_SECRET=SIGNING_SECRET,
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / "slack-state.db"),
        MLAI_BACKEND_URL="https://backend.test",
        MLAI_API_KEY="api-key",
        ROO_API_KEY="roo-key",
        INTERNAL_API_KEY="internal-key",
        ROO_SURFACE="public",
        ROO_UNIFIED_ADMIN_ROUTING_ENABLED=False,
    )


def _issue(*, user="UVERIFIED", channel="CPOINTS", thread="111.222", now=None):
    return issue_points_flex_confirmation(
        signing_secret=SIGNING_SECRET,
        slack_user_id=user,
        channel_id=channel,
        thread_ts=thread,
        now=time.time() if now is None else now,
    )


def _response_body(response):
    return json.loads(response.body.decode("utf-8"))


def test_confirmation_token_is_bound_and_contains_no_points_data():
    token, expected = _issue(now=1_000)

    decoded = verify_points_flex_confirmation(
        token,
        signing_secret=SIGNING_SECRET,
        now=1_001,
    )

    assert decoded == expected
    payload_segment = token.split(".", 1)[0]
    payload = json.loads(flex_module._urlsafe_decode(payload_segment))
    assert set(payload) == {"c", "exp", "iat", "rid", "t", "u", "v"}
    assert payload["u"] == "UVERIFIED"
    assert payload["c"] == "CPOINTS"
    assert payload["t"] == "111.222"
    assert payload["v"] == 2


def test_confirmation_token_rejects_tampering_and_expiry():
    token, _ = _issue(now=1_000)
    payload, signature = token.split(".", 1)
    tampered_payload = ("A" if payload[0] != "A" else "B") + payload[1:]

    with pytest.raises(PointsFlexTokenError, match="invalid_token"):
        verify_points_flex_confirmation(
            f"{tampered_payload}.{signature}",
            signing_secret=SIGNING_SECRET,
            now=1_001,
        )

    with pytest.raises(PointsFlexTokenError, match="expired_token"):
        verify_points_flex_confirmation(
            token,
            signing_secret=SIGNING_SECRET,
            now=1_600,
        )


def test_pre_deploy_v1_confirmation_remains_valid_during_rollout():
    payload = {
        "c": "CPOINTS",
        "exp": 1_600,
        "iat": 1_000,
        "rid": "03ab2fb5-3b6a-4708-85cf-f214abb999f0",
        "u": "UVERIFIED",
        "v": 1,
    }
    encoded = flex_module._urlsafe_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = flex_module.hmac.new(
        flex_module._signing_key(SIGNING_SECRET),
        encoded.encode("ascii"),
        flex_module.hashlib.sha256,
    ).digest()
    token = f"{encoded}.{flex_module._urlsafe_encode(signature)}"

    confirmation = verify_points_flex_confirmation(
        token,
        signing_secret=SIGNING_SECRET,
        now=1_001,
    )

    assert confirmation.thread_ts is None
    assert confirmation.channel_id == "CPOINTS"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (142, 142), ("142", 142), ("+142", 142)],
)
def test_lifetime_earned_parser_accepts_only_integral_nonnegative_values(value, expected):
    assert parse_lifetime_earned({"lifetime_earned": value}) == expected


@pytest.mark.parametrize("value", [None, True, -1, 1.5, "1.5", "001"])
def test_lifetime_earned_parser_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="invalid lifetime_earned"):
        parse_lifetime_earned({"lifetime_earned": value})


def test_flex_store_claim_is_atomic_across_store_instances(tmp_path):
    _, confirmation = _issue(now=1_000)
    database_path = tmp_path / "flex.db"

    def claim(index):
        store = PointsFlexShareStore(database_path)
        return store.claim(
            confirmation,
            owner=f"worker-{index}",
            now=1_001,
        )["state"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(claim, range(8)))

    assert states.count("claimed") == 1
    assert states.count("processing") == 7


def test_flex_store_release_allows_retry_and_shared_state_is_final(tmp_path):
    _, confirmation = _issue(now=1_000)
    store = PointsFlexShareStore(tmp_path / "flex.db")

    assert store.claim(confirmation, owner="worker-1", now=1_001)["state"] == "claimed"
    released = store.release(
        confirmation.request_id,
        owner="worker-1",
        error_code="slack_post_failed",
        now=1_002,
    )
    assert released["status"] == "pending"
    assert released["last_error_code"] == "slack_post_failed"

    assert store.claim(confirmation, owner="worker-2", now=1_003)["state"] == "claimed"
    shared = store.mark_shared(
        confirmation.request_id,
        owner="worker-2",
        message_ts="123.456",
        now=1_004,
    )
    assert shared["status"] == "shared"
    assert store.claim(confirmation, owner="worker-3", now=1_005)["state"] == "shared"


def test_flex_store_never_reclaims_ambiguous_stale_processing_state(tmp_path):
    _, confirmation = _issue(now=1_000)
    store = PointsFlexShareStore(tmp_path / "flex.db")

    assert store.claim(
        confirmation,
        owner="crashed-worker",
        now=1_001,
        lease_seconds=1,
    )["state"] == "claimed"

    assert store.claim(
        confirmation,
        owner="retry-worker",
        now=1_003,
    )["state"] == "processing"


def test_flex_and_slack_receipts_can_claim_the_shared_database_concurrently(tmp_path):
    _, confirmation = _issue(now=1_000)
    database_path = tmp_path / "shared-slack-state.db"

    with ThreadPoolExecutor(max_workers=2) as pool:
        flex_future = pool.submit(
            PointsFlexShareStore(database_path).claim,
            confirmation,
            owner="flex-worker",
            now=1_001,
        )
        receipt_future = pool.submit(
            SlackRequestReceiptStore(database_path).claim,
            "a" * 64,
            now=1_001,
            ttl_seconds=600,
        )

    assert flex_future.result()["state"] == "claimed"
    assert receipt_future.result() is True


class FlexBalanceClient:
    def __init__(self, lifetime_earned=142):
        self.lifetime_earned = lifetime_earned
        self.balance_users = []

    async def get_balance(self, slack_user_id):
        self.balance_users.append(slack_user_id)
        return {
            "balance": 37,
            "lifetime_earned": self.lifetime_earned,
            "lifetime_spent": 105,
            "lifetime_purchased": 12,
        }


@pytest.mark.asyncio
async def test_flex_preview_is_private_and_uses_only_verified_actor(monkeypatch, tmp_path):
    client = FlexBalanceClient()
    previews = []
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: _settings(tmp_path),
    )
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(
        executor_module,
        "post_ephemeral",
        lambda **kwargs: previews.append(kwargs) or {"ok": True},
    )

    result = await SkillExecutor()._handle_points_action(
        client=client,
        action="flex_points",
        params={"target_slack_id": "UFORGED"},
        text="flex my points",
        user_id="UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.balance_users == ["UVERIFIED"]
    assert result["message"] == ""
    assert result["suppress_post"] is True
    assert previews[0]["user"] == "UVERIFIED"
    assert previews[0]["channel"] == "CPOINTS"
    assert previews[0]["thread_ts"] == "111.222"
    section_text = previews[0]["blocks"][0]["text"]["text"]
    context_text = previews[0]["blocks"][1]["elements"][0]["text"]
    assert "142 Roo Points" in section_text
    assert "37" not in section_text
    assert "105" not in section_text
    assert "12" not in section_text
    assert "balance, purchases, spending, and history stay private" in context_text
    button = previews[0]["blocks"][2]["elements"][0]
    assert button["action_id"] == POINTS_FLEX_ACTION_ID
    confirmation = verify_points_flex_confirmation(
        button["value"],
        signing_secret=SIGNING_SECRET,
    )
    assert confirmation.slack_user_id == "UVERIFIED"
    assert confirmation.channel_id == "CPOINTS"
    assert confirmation.thread_ts == "111.222"


@pytest.mark.asyncio
async def test_flex_rejects_tagged_targets_before_balance_lookup(monkeypatch):
    client = FlexBalanceClient()
    notices = []
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(
        executor_module,
        "post_ephemeral",
        lambda **kwargs: notices.append(kwargs) or {"ok": True},
    )

    result = await SkillExecutor()._handle_points_action(
        client=client,
        action="flex_points",
        params={},
        text="<@UROO> flex <@UTARGET>'s points",
        user_id="UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.balance_users == []
    assert result["suppress_post"] is True
    assert result["data"]["target_rejected"] is True
    assert "only flex your own" in notices[0]["text"].lower()


@pytest.mark.asyncio
async def test_flex_in_dm_redirects_to_destination_channel_without_lookup(monkeypatch):
    client = FlexBalanceClient()
    monkeypatch.setattr(
        executor_module,
        "post_ephemeral",
        lambda **kwargs: pytest.fail("no ephemeral message expected in a DM"),
    )

    result = await SkillExecutor()._handle_points_action(
        client=client,
        action="flex_points",
        params={},
        text="flex my points",
        user_id="UVERIFIED",
        channel_id="DPRIVATE",
        thread_ts=None,
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert client.balance_users == []
    assert "shared channel" in result
    assert "lifetime-earned" in result


@pytest.mark.asyncio
async def test_preview_failure_never_posts_points_and_sends_generic_dm(monkeypatch, tmp_path):
    direct_messages = []
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: _settings(tmp_path),
    )
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(
        executor_module,
        "post_ephemeral",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Slack unavailable")),
    )
    monkeypatch.setattr(
        executor_module,
        "send_dm",
        lambda user_id, text, **kwargs: direct_messages.append((user_id, text)) or {"ok": True},
    )

    result = await SkillExecutor()._handle_points_action(
        client=FlexBalanceClient(),
        action="flex_points",
        params={},
        text="flex my points",
        user_id="UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert result["message"] == ""
    assert result["suppress_post"] is True
    assert result["data"]["preview_delivered"] is False
    assert direct_messages[0][0] == "UVERIFIED"
    assert "142" not in direct_messages[0][1]
    assert "37" not in direct_messages[0][1]


class ConfirmationBackendClient:
    balance_users = []
    payload = {"balance": 9, "lifetime_earned": 155, "lifetime_spent": 44}
    failures_remaining = 0

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs

    async def get_balance(self, slack_user_id):
        type(self).balance_users.append(slack_user_id)
        if type(self).failures_remaining:
            type(self).failures_remaining -= 1
            raise RuntimeError("backend unavailable")
        return dict(type(self).payload)


@pytest.fixture
def confirmation_backend(monkeypatch):
    ConfirmationBackendClient.balance_users = []
    ConfirmationBackendClient.failures_remaining = 0
    ConfirmationBackendClient.payload = {
        "balance": 9,
        "lifetime_earned": 155,
        "lifetime_spent": 44,
        "lifetime_purchased": 20,
    }
    monkeypatch.setattr(
        "roo.clients.mlai_backend.MLAIBackendClient",
        ConfirmationBackendClient,
    )
    return ConfirmationBackendClient


@pytest.mark.asyncio
async def test_confirmation_refetches_total_and_posts_in_request_thread_once(
    monkeypatch,
    tmp_path,
    confirmation_backend,
):
    settings = _settings(tmp_path)
    token, confirmation = _issue()
    public_posts = []
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: public_posts.append(kwargs) or {"ok": True, "ts": "123.456"},
    )

    first = await main_module._handle_points_flex_confirmation_action(
        settings=settings,
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )
    second = await main_module._handle_points_flex_confirmation_action(
        settings=settings,
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )

    assert confirmation_backend.balance_users == ["UVERIFIED"]
    assert public_posts == [
        {
            "channel": "CPOINTS",
            "text": "<@UVERIFIED> has earned *155 Roo Points* through MLAI contributions.",
            "thread_ts": "111.222",
            "client_msg_id": confirmation.request_id,
        }
    ]
    assert "9" not in public_posts[0]["text"]
    assert "44" not in public_posts[0]["text"]
    assert "20" not in public_posts[0]["text"]
    assert "Shared" in _response_body(first)["text"]
    assert "already shared" in _response_body(second)["text"]
    record = flex_module.get_points_flex_store(
        settings.SLACK_RECEIPTS_DB_PATH
    ).get(confirmation.request_id)
    assert record["status"] == "shared"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "channel", "message_fragment"),
    [
        ("UATTACKER", "CPOINTS", "Only the member"),
        ("UVERIFIED", "COTHER", "only works in the channel"),
    ],
)
async def test_confirmation_rejects_actor_or_channel_mismatch_before_lookup(
    monkeypatch,
    tmp_path,
    confirmation_backend,
    actor,
    channel,
    message_fragment,
):
    token, _ = _issue()
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: pytest.fail("no public post expected"),
    )

    response = await main_module._handle_points_flex_confirmation_action(
        settings=_settings(tmp_path),
        action_value=token,
        verified_user_id=actor,
        verified_channel_id=channel,
    )

    assert confirmation_backend.balance_users == []
    assert message_fragment in _response_body(response)["text"]


@pytest.mark.asyncio
async def test_backend_failure_releases_confirmation_for_safe_retry(
    monkeypatch,
    tmp_path,
    confirmation_backend,
):
    confirmation_backend.failures_remaining = 1
    token, _ = _issue()
    public_posts = []
    monkeypatch.setattr(
        main_module,
        "post_message",
        lambda **kwargs: public_posts.append(kwargs) or {"ok": True, "ts": "123.456"},
    )

    failed = await main_module._handle_points_flex_confirmation_action(
        settings=_settings(tmp_path),
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )
    retried = await main_module._handle_points_flex_confirmation_action(
        settings=_settings(tmp_path),
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )

    assert "nothing was posted" in _response_body(failed)["text"]
    assert _response_body(failed)["replace_original"] is False
    assert "Shared" in _response_body(retried)["text"]
    assert _response_body(retried)["replace_original"] is True
    assert len(public_posts) == 1


@pytest.mark.asyncio
async def test_slack_post_failure_releases_confirmation_for_safe_retry(
    monkeypatch,
    tmp_path,
    confirmation_backend,
):
    token, _ = _issue()
    attempts = []

    def post_message(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            return {"ok": False, "error": "temporary_error"}
        return {"ok": True, "ts": "123.456"}

    monkeypatch.setattr(main_module, "post_message", post_message)

    failed = await main_module._handle_points_flex_confirmation_action(
        settings=_settings(tmp_path),
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )
    retried = await main_module._handle_points_flex_confirmation_action(
        settings=_settings(tmp_path),
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )

    assert "nothing was shared" in _response_body(failed)["text"]
    assert _response_body(failed)["replace_original"] is False
    assert "Shared" in _response_body(retried)["text"]
    assert len(attempts) == 2
    assert attempts[0]["client_msg_id"] == attempts[1]["client_msg_id"]


@pytest.mark.asyncio
async def test_fast_path_routes_flex_with_verified_channel_context(monkeypatch):
    calls = []
    agent = object.__new__(RooAgent)

    async def execute(user_id, action, **kwargs):
        calls.append((user_id, action, kwargs))
        return {"message": "", "suppress_post": True}

    monkeypatch.setattr(agent, "_execute_fast_points", execute)

    result = await agent._try_fast_path(
        "flex my points",
        "UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
    )

    assert agent._match_fast_path("flex my points") == "flex_points"
    assert agent._match_fast_path("points flex") == "flex_points"
    assert calls == [
        (
            "UVERIFIED",
            "flex_points",
            {"channel_id": "CPOINTS", "thread_ts": "111.222"},
        )
    ]
    assert result["suppress_post"] is True


@pytest.mark.asyncio
async def test_fast_executor_invokes_flex_action_without_model_parameters(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client"] = kwargs

    class FakeSkill:
        name = "mlai-points"

        @staticmethod
        def get_client_class(name):
            assert name == "MLAIBackendClient"
            return FakeClient

    class FakeExecutor:
        async def _handle_points_action(self, **kwargs):
            captured["handler"] = kwargs
            return {"message": "", "suppress_post": True}

    agent = object.__new__(RooAgent)
    agent.skills = [FakeSkill()]
    agent.skill_executor = FakeExecutor()
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: SimpleNamespace(
            MLAI_BACKEND_URL="https://backend.test",
            MLAI_API_KEY="api-key",
            ROO_API_KEY="roo-key",
            INTERNAL_API_KEY="internal-key",
        ),
    )

    result = await agent._execute_fast_points(
        "UVERIFIED",
        "flex_points",
        channel_id="CPOINTS",
        thread_ts="111.222",
    )

    assert captured["handler"]["action"] == "flex_points"
    assert captured["handler"]["params"] == {}
    assert captured["handler"]["user_id"] == "UVERIFIED"
    assert captured["handler"]["channel_id"] == "CPOINTS"
    assert result["message"] == ""
    assert result["suppress_post"] is True


@pytest.mark.asyncio
async def test_slack_action_endpoint_routes_verified_identity_to_flex_handler(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    captured = []

    async def handle(**kwargs):
        captured.append(kwargs)
        return main_module._points_flex_action_response("handled")

    monkeypatch.setattr(main_module, "_handle_points_flex_confirmation_action", handle)
    payload = {
        "type": "block_actions",
        "user": {"id": "UVERIFIED"},
        "channel": {"id": "CPOINTS"},
        "actions": [{"action_id": POINTS_FLEX_ACTION_ID, "value": "signed-token"}],
    }

    class FakeRequest:
        state = SimpleNamespace(
            slack_duplicate=False,
            roo_settings=settings,
        )

        async def form(self):
            return {"payload": json.dumps(payload)}

    response = await main_module.slack_actions(FakeRequest(), _verified=True)

    assert _response_body(response)["text"] == "handled"
    assert captured == [
        {
            "settings": settings,
            "action_value": "signed-token",
            "verified_user_id": "UVERIFIED",
            "verified_channel_id": "CPOINTS",
        }
    ]


def _seed_shared_flex(
    store,
    *,
    user="UVERIFIED",
    channel="CPOINTS",
    thread="111.222",
    message_ts="222.333",
    now=None,
):
    base_time = time.time() if now is None else float(now)
    _, confirmation = _issue(
        user=user,
        channel=channel,
        thread=thread,
        now=base_time,
    )
    assert store.claim(
        confirmation,
        owner="share-worker",
        now=base_time + 1,
    )["state"] == "claimed"
    return store.mark_shared(
        confirmation.request_id,
        owner="share-worker",
        message_ts=message_ts,
        now=base_time + 2,
    )


def test_delete_token_is_exact_record_bound_and_contains_no_message_timestamp():
    _, confirmation = _issue(now=1_000)
    token = issue_points_flex_deletion(
        signing_secret=SIGNING_SECRET,
        request_id=confirmation.request_id,
        slack_user_id="UVERIFIED",
        channel_id="CPOINTS",
        now=1_010,
    )

    deletion = verify_points_flex_deletion(
        token,
        signing_secret=SIGNING_SECRET,
        now=1_011,
    )
    payload = json.loads(flex_module._urlsafe_decode(token.split(".", 1)[0]))

    assert deletion.request_id == confirmation.request_id
    assert deletion.slack_user_id == "UVERIFIED"
    assert deletion.channel_id == "CPOINTS"
    assert set(payload) == {"c", "exp", "iat", "rid", "u", "v"}
    assert "ts" not in payload
    assert "message_ts" not in payload


def test_delete_token_rejects_share_token_tampering_and_expiry():
    _, confirmation = _issue(now=1_000)
    delete_token = issue_points_flex_deletion(
        signing_secret=SIGNING_SECRET,
        request_id=confirmation.request_id,
        slack_user_id="UVERIFIED",
        channel_id="CPOINTS",
        now=1_010,
    )
    share_token, _ = _issue(now=1_010)

    with pytest.raises(PointsFlexTokenError, match="invalid_token"):
        verify_points_flex_deletion(
            share_token,
            signing_secret=SIGNING_SECRET,
            now=1_011,
        )
    with pytest.raises(PointsFlexTokenError, match="expired_token"):
        verify_points_flex_deletion(
            delete_token,
            signing_secret=SIGNING_SECRET,
            now=1_610,
        )


def test_live_flex_database_schema_is_upgraded_without_losing_shared_rows(tmp_path):
    database_path = tmp_path / "live-flex.db"
    request_id = "03ab2fb5-3b6a-4708-85cf-f214abb999f0"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE points_flex_shares (
                request_id TEXT PRIMARY KEY,
                slack_user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL,
                locked_by TEXT,
                locked_until REAL,
                message_ts TEXT,
                last_error_code TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                shared_at REAL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO points_flex_shares (
                request_id, slack_user_id, channel_id, expires_at, status,
                message_ts, created_at, updated_at, shared_at
            ) VALUES (?, 'UVERIFIED', 'CPOINTS', 1600, 'shared', '222.333', 1000, 1002, 1002)
            """,
            (request_id,),
        )

    store = PointsFlexShareStore(database_path)
    records = store.list_shared(slack_user_id="UVERIFIED", channel_id="CPOINTS")

    assert [record["request_id"] for record in records] == [request_id]
    assert records[0]["thread_ts"] is None


def test_delete_lookup_is_owner_and_channel_scoped_and_newest_first(tmp_path):
    store = PointsFlexShareStore(tmp_path / "flex.db")
    older = _seed_shared_flex(store, message_ts="200.001", now=1_000)
    newer = _seed_shared_flex(store, message_ts="300.001", now=1_100)
    _seed_shared_flex(store, user="UOTHER", message_ts="400.001", now=1_200)
    _seed_shared_flex(store, channel="COTHER", message_ts="500.001", now=1_300)

    records = store.list_shared(slack_user_id="UVERIFIED", channel_id="CPOINTS")

    assert [record["request_id"] for record in records] == [
        newer["request_id"],
        older["request_id"],
    ]


def test_store_rejects_signed_delete_for_a_different_record_owner(tmp_path):
    store = PointsFlexShareStore(tmp_path / "flex.db")
    record = _seed_shared_flex(store, user="UOWNER")
    token = issue_points_flex_deletion(
        signing_secret=SIGNING_SECRET,
        request_id=record["request_id"],
        slack_user_id="UATTACKER",
        channel_id="CPOINTS",
        now=1_010,
    )
    deletion = verify_points_flex_deletion(
        token,
        signing_secret=SIGNING_SECRET,
        now=1_011,
    )

    assert store.claim_delete(deletion, owner="attacker", now=1_012) == {
        "state": "not_found",
        "record": None,
    }
    assert store.get(record["request_id"])["status"] == "shared"


def test_delete_claim_is_atomic_and_replay_is_idempotent(tmp_path):
    database_path = tmp_path / "flex.db"
    store = PointsFlexShareStore(database_path)
    record = _seed_shared_flex(store, now=1_000)
    token = issue_points_flex_deletion(
        signing_secret=SIGNING_SECRET,
        request_id=record["request_id"],
        slack_user_id="UVERIFIED",
        channel_id="CPOINTS",
        now=1_010,
    )
    deletion = verify_points_flex_deletion(
        token,
        signing_secret=SIGNING_SECRET,
        now=1_011,
    )

    def claim(index):
        return PointsFlexShareStore(database_path).claim_delete(
            deletion,
            owner=f"delete-{index}",
            now=1_012,
        )["state"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(claim, range(8)))

    assert states.count("claimed") == 1
    assert states.count("deleting") == 7
    winning_owner = f"delete-{states.index('claimed')}"
    store.mark_deleted(record["request_id"], owner=winning_owner, now=1_013)
    assert store.claim_delete(deletion, owner="replay", now=1_014)["state"] == "deleted"


def test_stale_delete_can_recover_and_failed_delete_can_retry(tmp_path):
    store = PointsFlexShareStore(tmp_path / "flex.db")
    record = _seed_shared_flex(store, now=1_000)
    token = issue_points_flex_deletion(
        signing_secret=SIGNING_SECRET,
        request_id=record["request_id"],
        slack_user_id="UVERIFIED",
        channel_id="CPOINTS",
        now=1_010,
    )
    deletion = verify_points_flex_deletion(token, signing_secret=SIGNING_SECRET, now=1_011)

    assert store.claim_delete(
        deletion,
        owner="crashed",
        now=1_012,
        lease_seconds=1,
    )["state"] == "claimed"
    assert store.claim_delete(
        deletion,
        owner="recovery",
        now=1_014,
    )["state"] == "claimed"
    released = store.release_delete(
        record["request_id"],
        owner="recovery",
        error_code="slack_delete_failed",
        now=1_015,
    )
    assert released["status"] == "shared"
    assert store.claim_delete(
        deletion,
        owner="retry",
        now=1_016,
    )["state"] == "claimed"


@pytest.mark.asyncio
async def test_delete_preview_is_private_and_lists_only_verified_actors_flexes(
    monkeypatch,
    tmp_path,
):
    settings = _settings(tmp_path)
    store = flex_module.get_points_flex_store(settings.SLACK_RECEIPTS_DB_PATH)
    first = _seed_shared_flex(store, message_ts="200.001")
    second = _seed_shared_flex(store, message_ts="300.001", now=time.time() + 5)
    _seed_shared_flex(store, user="UOTHER", message_ts="400.001", now=time.time() + 10)
    previews = []
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings)
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(
        executor_module,
        "post_ephemeral",
        lambda **kwargs: previews.append(kwargs) or {"ok": True},
    )

    result = await SkillExecutor()._handle_points_action(
        client=FlexBalanceClient(),
        action="delete_flex",
        params={"message_ts": "999.999", "target_slack_id": "UOTHER"},
        text="delete my flex",
        user_id="UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="555.666",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert result["suppress_post"] is True
    assert result["data"]["flex_count"] == 2
    assert previews[0]["user"] == "UVERIFIED"
    assert previews[0]["thread_ts"] == "555.666"
    buttons = [block["accessory"] for block in previews[0]["blocks"] if "accessory" in block]
    assert len(buttons) == 2
    assert all(button["action_id"] == POINTS_FLEX_DELETE_ACTION_ID for button in buttons)
    deletions = [
        verify_points_flex_deletion(
            button["value"],
            signing_secret=SIGNING_SECRET,
        )
        for button in buttons
    ]
    assert {item.request_id for item in deletions} == {
        first["request_id"],
        second["request_id"],
    }
    assert all(item.slack_user_id == "UVERIFIED" for item in deletions)


@pytest.mark.asyncio
async def test_delete_request_rejects_tagged_target_before_lookup(monkeypatch, tmp_path):
    notices = []
    monkeypatch.setattr(executor_module, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("roo.slack_client.get_bot_user_id", lambda: "UROO")
    monkeypatch.setattr(
        executor_module,
        "post_ephemeral",
        lambda **kwargs: notices.append(kwargs) or {"ok": True},
    )

    result = await SkillExecutor()._handle_points_action(
        client=FlexBalanceClient(),
        action="delete_flex",
        params={},
        text="<@UROO> delete <@UOTHER>'s flex",
        user_id="UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert result["data"]["target_rejected"] is True
    assert "only delete your own" in notices[0]["text"].lower()


@pytest.mark.asyncio
async def test_delete_in_dm_requires_original_shared_channel():
    result = await SkillExecutor()._handle_points_action(
        client=FlexBalanceClient(),
        action="delete_flex",
        params={},
        text="delete my flex",
        user_id="UVERIFIED",
        channel_id="DPRIVATE",
        thread_ts="111.222",
        skill=SimpleNamespace(name="mlai-points"),
    )

    assert "shared channel" in result
    assert "only to you" in result


@pytest.mark.asyncio
async def test_delete_action_uses_only_stored_target_and_is_idempotent(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    store = flex_module.get_points_flex_store(settings.SLACK_RECEIPTS_DB_PATH)
    record = _seed_shared_flex(store, message_ts="222.333")
    _seed_shared_flex(store, message_ts="999.999", now=time.time() + 5)
    token = issue_points_flex_deletion(
        signing_secret=SIGNING_SECRET,
        request_id=record["request_id"],
        slack_user_id="UVERIFIED",
        channel_id="CPOINTS",
    )
    deletes = []
    monkeypatch.setattr(
        main_module,
        "delete_message",
        lambda **kwargs: deletes.append(kwargs) or {"ok": True},
    )

    first = await main_module._handle_points_flex_delete_action(
        settings=settings,
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )
    replay = await main_module._handle_points_flex_delete_action(
        settings=settings,
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )

    assert deletes == [{"channel": "CPOINTS", "message_ts": "222.333"}]
    assert _response_body(first)["text"] == "Deleted your Roo Points flex."
    assert "already deleted" in _response_body(replay)["text"]
    assert store.get(record["request_id"])["status"] == "deleted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "channel", "message_fragment"),
    [
        ("UATTACKER", "CPOINTS", "Only the member"),
        ("UVERIFIED", "COTHER", "only works in the channel"),
    ],
)
async def test_delete_action_rejects_actor_and_channel_mismatch(
    monkeypatch,
    tmp_path,
    actor,
    channel,
    message_fragment,
):
    settings = _settings(tmp_path)
    store = flex_module.get_points_flex_store(settings.SLACK_RECEIPTS_DB_PATH)
    record = _seed_shared_flex(store)
    token = issue_points_flex_deletion(
        signing_secret=SIGNING_SECRET,
        request_id=record["request_id"],
        slack_user_id="UVERIFIED",
        channel_id="CPOINTS",
    )
    monkeypatch.setattr(
        main_module,
        "delete_message",
        lambda **kwargs: pytest.fail("no Slack deletion expected"),
    )

    response = await main_module._handle_points_flex_delete_action(
        settings=settings,
        action_value=token,
        verified_user_id=actor,
        verified_channel_id=channel,
    )

    assert message_fragment in _response_body(response)["text"]
    assert store.get(record["request_id"])["status"] == "shared"


@pytest.mark.asyncio
async def test_manually_removed_flex_is_treated_as_idempotent_success(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    store = flex_module.get_points_flex_store(settings.SLACK_RECEIPTS_DB_PATH)
    record = _seed_shared_flex(store)
    token = issue_points_flex_deletion(
        signing_secret=SIGNING_SECRET,
        request_id=record["request_id"],
        slack_user_id="UVERIFIED",
        channel_id="CPOINTS",
    )
    monkeypatch.setattr(
        main_module,
        "delete_message",
        lambda **kwargs: {"ok": False, "error": "message_not_found"},
    )

    response = await main_module._handle_points_flex_delete_action(
        settings=settings,
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )

    assert "already gone" in _response_body(response)["text"]
    assert store.get(record["request_id"])["status"] == "deleted"


@pytest.mark.asyncio
async def test_slack_delete_failure_releases_record_for_retry(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    store = flex_module.get_points_flex_store(settings.SLACK_RECEIPTS_DB_PATH)
    record = _seed_shared_flex(store)
    token = issue_points_flex_deletion(
        signing_secret=SIGNING_SECRET,
        request_id=record["request_id"],
        slack_user_id="UVERIFIED",
        channel_id="CPOINTS",
    )
    attempts = []

    def delete_message(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            return {"ok": False, "error": "ratelimited"}
        return {"ok": True}

    monkeypatch.setattr(main_module, "delete_message", delete_message)
    failed = await main_module._handle_points_flex_delete_action(
        settings=settings,
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )
    retried = await main_module._handle_points_flex_delete_action(
        settings=settings,
        action_value=token,
        verified_user_id="UVERIFIED",
        verified_channel_id="CPOINTS",
    )

    assert _response_body(failed)["replace_original"] is False
    assert _response_body(retried)["text"] == "Deleted your Roo Points flex."
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_fast_path_routes_delete_with_verified_context(monkeypatch):
    calls = []
    agent = object.__new__(RooAgent)

    async def execute(user_id, action, **kwargs):
        calls.append((user_id, action, kwargs))
        return {"message": "", "suppress_post": True}

    monkeypatch.setattr(agent, "_execute_fast_points", execute)
    result = await agent._try_fast_path(
        "delete my flex",
        "UVERIFIED",
        channel_id="CPOINTS",
        thread_ts="111.222",
    )

    assert agent._match_fast_path("remove my points flex") == "delete_flex"
    assert agent._match_fast_path("delete my latest flex") == "delete_flex"
    assert calls == [
        (
            "UVERIFIED",
            "delete_flex",
            {"channel_id": "CPOINTS", "thread_ts": "111.222"},
        )
    ]
    assert result["suppress_post"] is True


@pytest.mark.asyncio
async def test_slack_action_endpoint_routes_verified_identity_to_delete_handler(
    monkeypatch,
    tmp_path,
):
    settings = _settings(tmp_path)
    captured = []

    async def handle(**kwargs):
        captured.append(kwargs)
        return main_module._points_flex_action_response("deleted")

    monkeypatch.setattr(main_module, "_handle_points_flex_delete_action", handle)
    payload = {
        "type": "block_actions",
        "user": {"id": "UVERIFIED"},
        "channel": {"id": "CPOINTS"},
        "actions": [
            {"action_id": POINTS_FLEX_DELETE_ACTION_ID, "value": "signed-delete-token"}
        ],
    }

    class FakeRequest:
        state = SimpleNamespace(slack_duplicate=False, roo_settings=settings)

        async def form(self):
            return {"payload": json.dumps(payload)}

    response = await main_module.slack_actions(FakeRequest(), _verified=True)

    assert _response_body(response)["text"] == "deleted"
    assert captured == [
        {
            "settings": settings,
            "action_value": "signed-delete-token",
            "verified_user_id": "UVERIFIED",
            "verified_channel_id": "CPOINTS",
        }
    ]


def test_delete_message_uses_roos_normal_bot_client(monkeypatch):
    calls = []

    class BotClient:
        def chat_delete(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    monkeypatch.setattr(slack_client_module, "get_slack_client", lambda: BotClient())

    response = slack_client_module.delete_message("CPOINTS", "222.333")

    assert response == {"ok": True}
    assert calls == [{"channel": "CPOINTS", "ts": "222.333"}]
