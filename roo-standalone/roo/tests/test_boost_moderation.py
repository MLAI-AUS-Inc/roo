import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import boost_moderation as module
from roo import link_love
from roo.boost_moderation import (
    BoostPostAdmissionStore,
    boost_reward_admission_decision,
    boost_reward_root_poster,
    handle_boost_recheck_reaction,
    handle_boost_root_edit,
    handle_boost_root_post,
    process_due_boost_restores_once,
)
from roo.slack_moderation import ModeratorDeleteResult


def moderation_settings(**overrides):
    values = {
        "BOOST_POST_MODERATION_ENABLED": True,
        "BOOST_POST_AUTO_DELETE_ENABLED": False,
        "BOOST_LINK_LOVE_CHANNEL_ID": "CBOOST123",
        "BOOST_POST_ENFORCEMENT_CUTOFF_TS": "1700000000.000000",
        "BOOST_POST_DECISION_TIMEOUT_SECONDS": 30.0,
        "BOOST_POST_MAX_RETRY_ATTEMPTS": 5,
        "BOOST_POST_RETRY_POLL_SECONDS": 15.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def root_event(**overrides):
    values = {
        "type": "message",
        "channel": "CBOOST123",
        "ts": "1800000000.123456",
        "user": "UPOSTER1",
        "text": "Please boost https://www.linkedin.com/posts/example-123",
    }
    values.update(overrides)
    return values


class ApprovedClient:
    def __init__(self):
        self.calls = []

    async def admit_boost_post(self, **payload):
        self.calls.append(payload)
        return {
            "status": "approved",
            "admission_id": "admission-1",
            "base_cost_points": 8,
            "charged_points": 4,
            "discount_applied": True,
            "new_balance": 16,
        }


class InsufficientClient:
    def __init__(self, *, balance=3, required=8):
        self.balance = balance
        self.required = required
        self.calls = []

    async def admit_boost_post(self, **payload):
        self.calls.append(payload)
        return {
            "status": "insufficient_points",
            "code": "insufficient_points",
            "message": f"Needs {self.required} points but balance is {self.balance}",
            "base_cost_points": 8,
            "charged_points": self.required,
            "discount_applied": self.required == 4,
            "balance_before": self.balance,
            "new_balance": self.balance,
        }


class RecheckApprovedClient:
    def __init__(self):
        self.calls = []

    async def admit_boost_post(self, **payload):
        self.calls.append(payload)
        return {
            "status": "approved",
            "admission_id": "admission-1",
            "base_cost_points": 8,
            "charged_points": 8,
            "discount_applied": False,
            "new_balance": 4,
        }


def successful_delete(**kwargs):
    return ModeratorDeleteResult(
        True,
        "deleted",
        kwargs["channel_id"],
        kwargs["message_ts"],
    )


def test_boost_url_accepts_any_http_or_https_domain() -> None:
    assert link_love.extract_boost_url(
        "Share https://example.com/product?id=12&utm_source=slack#pricing"
    ) == "https://example.com/product?id=12&utm_source=slack#pricing"
    assert link_love.extract_boost_url("Read http://news.example.org/story/") == (
        "http://news.example.org/story"
    )
    assert link_love.extract_boost_url("No campaign link here") is None


def test_invalid_post_notice_explains_internal_validation_and_retry() -> None:
    notice = module._rejection_notice(
        {
            "poster_slack_id": "UPOSTER1",
            "status": "rejected_invalid",
            "rejection_reason": "Missing required fields: social_post_url",
        }
    )

    assert "required Slack information was missing or invalid" in notice
    assert "No Roo points were charged" in notice
    assert "Any website or social link is allowed" in notice
    assert "create a new top-level post" in notice


def test_insufficient_points_notice_shows_price_balance_and_discount() -> None:
    notice = module._rejection_notice(
        {
            "poster_slack_id": "UPOSTER1",
            "status": "rejected_insufficient_points",
            "backend_result_json": json.dumps(
                {
                    "charged_points": 4,
                    "new_balance": 3,
                    "discount_applied": True,
                }
            ),
        }
    )

    assert "costs 4 Roo points" in notice
    assert "currently have 3" in notice
    assert "50% Australian-startup monthly-update discount" in notice
    assert "engaging with other founders' posts" in notice
    assert "DMing me `topup`" in notice
    assert "react to this Roo guidance reply with ✅ or ✔️" in notice
    assert "DM me `points`" in notice
    assert "what's my points balance?" in notice


def test_unlinked_member_notice_explains_how_to_fix_account_link() -> None:
    notice = module._rejection_notice(
        {
            "poster_slack_id": "UPOSTER1",
            "status": "rejected_member_unlinked",
        }
    )

    assert "can't match your Slack profile to a Roo Points account" in notice
    assert "what's my points balance?" in notice
    assert "ask an MLAI admin to link your Slack profile" in notice


@pytest.mark.asyncio
async def test_root_admission_is_durable_idempotent_and_posts_discount_notice(
    tmp_path, monkeypatch
):
    settings = moderation_settings()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")
    client = ApprovedClient()
    notices = []

    first = await handle_boost_root_post(
        root_event(),
        workspace_id="TTEAM123",
        store=store,
        client=client,
        post_message_fn=lambda **kwargs: notices.append(kwargs),
    )
    duplicate = await handle_boost_root_post(
        root_event(),
        workspace_id="TTEAM123",
        store=store,
        client=client,
        post_message_fn=lambda **kwargs: notices.append(kwargs),
    )

    assert first["status"] == "approved"
    assert duplicate["status"] == "not_due"
    assert len(client.calls) == 1
    assert client.calls[0]["submission_key"] == (
        "boost-post:TTEAM123:CBOOST123:1800000000.123456"
    )
    admission = store.get("CBOOST123", "1800000000.123456")
    assert admission["charged_points"] == 4
    assert admission["discount_applied"] == 1
    assert len(notices) == 1
    assert "monthly-update discount" in notices[0]["text"]


@pytest.mark.asyncio
async def test_root_without_a_link_is_still_eligible_for_points_admission(
    tmp_path, monkeypatch
):
    settings = moderation_settings()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")
    client = ApprovedClient()

    result = await handle_boost_root_post(
        root_event(text="Please help boost my startup update"),
        workspace_id="TTEAM123",
        store=store,
        client=client,
        post_message_fn=lambda **kwargs: None,
    )

    assert result["status"] == "approved"
    assert client.calls[0]["social_post_url"] == ""


@pytest.mark.asyncio
async def test_insufficient_points_is_committed_before_root_is_deleted(tmp_path, monkeypatch):
    settings = moderation_settings(BOOST_POST_AUTO_DELETE_ENABLED=True)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")

    status_seen_by_delete = []
    notices = []

    def delete_fn(**kwargs):
        status_seen_by_delete.append(
            store.get(kwargs["channel_id"], kwargs["message_ts"])["status"]
        )
        return ModeratorDeleteResult(
            True, "deleted", kwargs["channel_id"], kwargs["message_ts"]
        )

    result = await handle_boost_root_post(
        root_event(),
        workspace_id="TTEAM123",
        store=store,
        client=InsufficientClient(),
        post_message_fn=lambda **kwargs: notices.append(kwargs) or {"ts": "1800000001.000100"},
        send_dm_fn=lambda *args, **kwargs: None,
        delete_fn=delete_fn,
    )

    assert status_seen_by_delete == ["rejected_insufficient_points"]
    assert result["status"] == "deleted"
    assert store.get("CBOOST123", "1800000000.123456")["decision_message_ts"] == (
        "1800000001.000100"
    )
    assert boost_reward_admission_decision(
        "CBOOST123", "1800000000.123456", store=store
    ) == "rejected"


@pytest.mark.asyncio
async def test_founder_check_reaction_rechecks_charges_and_restores_post(
    tmp_path,
    monkeypatch,
):
    settings = moderation_settings(BOOST_POST_AUTO_DELETE_ENABLED=True)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")
    posted = []

    def post_message(**kwargs):
        posted.append(kwargs)
        if kwargs.get("thread_ts"):
            return {"ts": "1800000001.000100"}
        return {"ts": "1800000002.000200"}

    initial = await handle_boost_root_post(
        root_event(),
        workspace_id="TTEAM123",
        store=store,
        client=InsufficientClient(),
        post_message_fn=post_message,
        send_dm_fn=lambda *args, **kwargs: None,
        delete_fn=successful_delete,
    )
    client = RecheckApprovedClient()

    restored = await handle_boost_recheck_reaction(
        {
            "user": "UPOSTER1",
            "reaction": "white_check_mark",
            "item": {
                "type": "message",
                "channel": "CBOOST123",
                "ts": "1800000001.000100",
            },
        },
        store=store,
        client=client,
        post_message_fn=post_message,
    )
    duplicate = await handle_boost_recheck_reaction(
        {
            "user": "UPOSTER1",
            "reaction": "heavy_check_mark",
            "item": {
                "type": "message",
                "channel": "CBOOST123",
                "ts": "1800000001.000100",
            },
        },
        store=store,
        client=client,
        post_message_fn=post_message,
    )

    assert initial["status"] == "deleted"
    assert restored["status"] == "restored"
    assert duplicate["status"] == "already_restored"
    assert len(client.calls) == 1
    assert client.calls[0]["recheck_insufficient_points"] is True
    assert posted[-1].get("thread_ts") is None
    assert "Restored for <@UPOSTER1>" in posted[-1]["text"]
    assert root_event()["text"] in posted[-1]["text"]
    source = store.get("CBOOST123", "1800000000.123456")
    alias = store.get("CBOOST123", "1800000002.000200")
    assert source["restored_message_ts"] == "1800000002.000200"
    assert alias["status"] == "approved"
    assert alias["poster_slack_id"] == "UPOSTER1"
    assert boost_reward_admission_decision(
        "CBOOST123", "1800000002.000200", store=store
    ) == "approved"
    assert boost_reward_root_poster(
        "CBOOST123", "1800000002.000200", store=store
    ) == "UPOSTER1"

    monkeypatch.setattr(module, "get_boost_post_store", lambda: store)

    class AwardClient:
        def __init__(self):
            self.calls = []

        async def system_award_points(self, **kwargs):
            self.calls.append(kwargs)
            return {"points_awarded": 1, "new_balance": 1}

    award_client = AwardClient()

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("The founder's own reply must not reach classification")

    self_reply = await link_love.handle_link_love_reply(
        {
            "type": "message",
            "channel": "CBOOST123",
            "thread_ts": "1800000002.000200",
            "ts": "1800000003.000300",
            "user": "UPOSTER1",
            "text": "Liked and commented",
        },
        store=link_love.LinkLoveAwardStore(tmp_path / "awards.db"),
        client=award_client,
        get_root_message=lambda channel, ts: {
            "user": "UROO",
            "text": posted[-1]["text"],
        },
        llm_chat=fail_if_called,
        bot_user_id="UROO",
        notification_delay_seconds=0,
    )
    assert self_reply == {"status": "ignored", "reason": "root_author_reply"}
    assert award_client.calls == []


@pytest.mark.asyncio
async def test_check_reaction_from_someone_else_does_not_recheck(tmp_path, monkeypatch):
    settings = moderation_settings(BOOST_POST_AUTO_DELETE_ENABLED=True)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")
    await handle_boost_root_post(
        root_event(),
        workspace_id="TTEAM123",
        store=store,
        client=InsufficientClient(),
        post_message_fn=lambda **kwargs: {"ts": "1800000001.000100"},
        send_dm_fn=lambda *args, **kwargs: None,
        delete_fn=successful_delete,
    )
    client = RecheckApprovedClient()

    result = await handle_boost_recheck_reaction(
        {
            "user": "UOTHER1",
            "reaction": "white_check_mark",
            "item": {
                "type": "message",
                "channel": "CBOOST123",
                "ts": "1800000001.000100",
            },
        },
        store=store,
        client=client,
    )

    assert result["handled"] is True
    assert result["status"] == "ignored_not_poster"
    assert client.calls == []


@pytest.mark.asyncio
async def test_still_insufficient_recheck_keeps_post_removed_and_explains_next_step(
    tmp_path,
    monkeypatch,
):
    settings = moderation_settings(BOOST_POST_AUTO_DELETE_ENABLED=True)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")
    posted = []
    await handle_boost_root_post(
        root_event(),
        workspace_id="TTEAM123",
        store=store,
        client=InsufficientClient(balance=3),
        post_message_fn=lambda **kwargs: posted.append(kwargs) or {"ts": "1800000001.000100"},
        send_dm_fn=lambda *args, **kwargs: None,
        delete_fn=successful_delete,
    )
    client = InsufficientClient(balance=6)

    result = await handle_boost_recheck_reaction(
        {
            "user": "UPOSTER1",
            "reaction": "white_check_mark",
            "item": {
                "type": "message",
                "channel": "CBOOST123",
                "ts": "1800000001.000100",
            },
        },
        store=store,
        client=client,
        post_message_fn=lambda **kwargs: posted.append(kwargs) or {"ts": "1800000001.000200"},
    )

    assert result["status"] == "still_insufficient"
    assert store.get("CBOOST123", "1800000000.123456")["status"] == "deleted"
    assert "currently have 6" in posted[-1]["text"]
    assert "react ✅ again" in posted[-1]["text"]


@pytest.mark.asyncio
async def test_restore_failure_is_durable_and_retry_worker_completes_it(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(module, "_now", lambda: clock[0])
    settings = moderation_settings(BOOST_POST_AUTO_DELETE_ENABLED=True)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")
    await handle_boost_root_post(
        root_event(),
        workspace_id="TTEAM123",
        store=store,
        client=InsufficientClient(),
        post_message_fn=lambda **kwargs: {"ts": "1800000001.000100"},
        send_dm_fn=lambda *args, **kwargs: None,
        delete_fn=successful_delete,
    )

    def fail_restore(**kwargs):
        if not kwargs.get("thread_ts"):
            raise RuntimeError("Slack unavailable")
        return {"ts": "1800000001.000100"}

    first = await handle_boost_recheck_reaction(
        {
            "user": "UPOSTER1",
            "reaction": "white_check_mark",
            "item": {
                "type": "message",
                "channel": "CBOOST123",
                "ts": "1800000001.000100",
            },
        },
        store=store,
        client=RecheckApprovedClient(),
        post_message_fn=fail_restore,
    )
    clock[0] = 1031.0
    retried = await process_due_boost_restores_once(
        store=store,
        post_message_fn=lambda **kwargs: {"ts": "1800000002.000200"},
    )

    assert first["status"] == "restore_pending"
    assert retried[0]["status"] == "restored"
    assert store.get("CBOOST123", "1800000000.123456")["restored_message_ts"] == (
        "1800000002.000200"
    )


@pytest.mark.asyncio
async def test_ambiguous_backend_failure_retries_without_deleting(tmp_path, monkeypatch):
    settings = moderation_settings(BOOST_POST_AUTO_DELETE_ENABLED=True)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")

    class TimeoutClient:
        async def admit_boost_post(self, **payload):
            request = httpx.Request("POST", "https://backend.test/admit")
            raise httpx.ReadTimeout("timed out", request=request)

    deletes = []
    result = await handle_boost_root_post(
        root_event(),
        workspace_id="TTEAM123",
        store=store,
        client=TimeoutClient(),
        delete_fn=lambda **kwargs: deletes.append(kwargs),
    )

    assert result["status"] == "retry"
    assert deletes == []
    assert boost_reward_admission_decision(
        "CBOOST123", "1800000000.123456", store=store
    ) == "pending"


def test_reward_decision_preserves_pre_cutoff_roots(tmp_path, monkeypatch):
    settings = moderation_settings()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")

    assert boost_reward_admission_decision(
        "CBOOST123", "1600000000.000001", store=store
    ) == "legacy"
    assert boost_reward_admission_decision(
        "CBOOST123", "1800000000.000001", store=store
    ) == "pending"


@pytest.mark.asyncio
async def test_approved_root_can_change_to_any_link_without_another_charge(
    tmp_path, monkeypatch
):
    settings = moderation_settings(BOOST_POST_AUTO_DELETE_ENABLED=True)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    store = BoostPostAdmissionStore(tmp_path / "admissions.db")
    await handle_boost_root_post(
        root_event(),
        workspace_id="TTEAM123",
        store=store,
        client=ApprovedClient(),
        post_message_fn=lambda **kwargs: None,
    )
    deleted = []

    result = await handle_boost_root_edit(
        {
            "channel": "CBOOST123",
            "message": {
                "ts": "1800000000.123456",
                "text": "Swap https://example.com/products/different-456",
            },
        },
        store=store,
        post_message_fn=lambda **kwargs: None,
        send_dm_fn=lambda *args, **kwargs: None,
        delete_fn=lambda **kwargs: (
            deleted.append(kwargs)
            or ModeratorDeleteResult(
                True, "deleted", kwargs["channel_id"], kwargs["message_ts"]
            )
        ),
    )

    assert result["status"] == "updated"
    assert deleted == []
    admission = store.get("CBOOST123", "1800000000.123456")
    assert admission["status"] == "approved"
    assert admission["social_post_url"] == "https://example.com/products/different-456"


@pytest.mark.asyncio
async def test_link_love_never_settles_after_paid_root_is_removed(tmp_path, monkeypatch):
    settings = moderation_settings()
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    admission_store = BoostPostAdmissionStore(tmp_path / "admissions.db")
    monkeypatch.setattr(module, "get_boost_post_store", lambda: admission_store)
    award_store = link_love.LinkLoveAwardStore(tmp_path / "awards.db")
    reply = {
        "type": "message",
        "channel": "CBOOST123",
        "thread_ts": "1800000000.123456",
        "ts": "1800000001.123456",
        "user": "UHELPER1",
        "text": "Liked and commented",
    }

    pending = await link_love.handle_link_love_reply(reply, store=award_store)
    assert pending == {"status": "ignored", "reason": "boost_admission_pending"}

    admission = admission_store.record_root(
        workspace_id="TTEAM123",
        channel_id="CBOOST123",
        root_message_ts="1800000000.123456",
        poster_slack_id="UPOSTER1",
        root_text="Boost https://www.linkedin.com/posts/example-123",
        social_post_url="https://www.linkedin.com/posts/example-123",
    )
    admission_store.claim_one(int(admission["id"]), owner="test")
    admission_store.mark_approved(
        int(admission["id"]),
        {
            "status": "approved",
            "admission_id": "admission-1",
            "base_cost_points": 8,
            "charged_points": 8,
            "discount_applied": False,
            "new_balance": 10,
        },
    )

    created, award = award_store.create_award(
        channel_id="CBOOST123",
        root_message_ts="1800000000.123456",
        slack_user_id="UHELPER1",
        root_author_slack_id="UPOSTER1",
        source_reply_message_ts="1800000001.123456",
    )
    assert created is True

    admission_store.mark_removed("CBOOST123", "1800000000.123456")

    class NoAwardClient:
        async def system_award_points(self, **kwargs):
            raise AssertionError("No helper points may escape an unapproved root")

    processed = await link_love.process_link_love_award(
        award,
        store=award_store,
        client=NoAwardClient(),
        bot_user_id="UROO",
    )

    assert processed["status"] == "blocked_admission"
    assert processed["award"]["status"] == "blocked"
