"""Pure-logic tests for the bridge — no network, no Slack."""
import asyncio
import os
import tempfile
import time

from bridge.config import BridgeSettings
from bridge.identity import IdentityResolver
from bridge.relay import Relay
from bridge.store import BridgeStore


class FakeClient:
    """Minimal stand-in for a slack_sdk WebClient."""

    def __init__(self, names=None, fail=False):
        self._names = names or {}
        self.posted = []
        self._ts = 1000.0
        self.fail = fail

    def users_info(self, user):
        if user in self._names:
            return {
                "ok": True,
                "user": {"name": self._names[user], "profile": {"display_name": self._names[user]}},
            }
        return {"ok": False}

    def chat_postMessage(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self._ts += 1
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"{self._ts:.6f}"}


def _store():
    d = tempfile.mkdtemp()
    return BridgeStore(os.path.join(d, "b.db"))


def _relay(store, snc_fail=False):
    settings = BridgeSettings(
        MLAI_BOT_TOKEN="xoxb-x", MLAI_CHANNEL_ID="C_M", SNC_CHANNEL_ID="C_S",
        MLAI_TEAM_ID="T_M", SNC_TEAM_ID="T_S",
    )
    mlai = FakeClient()
    snc = FakeClient(fail=snc_fail)
    relay = Relay(
        settings=settings, store=store, identity=IdentityResolver(),
        mlai_client=mlai, snc_client=snc,
    )
    return relay, mlai, snc, settings


# --- markup translation ---------------------------------------------------

def test_translate_labeled_mention_and_channel_and_special():
    r = IdentityResolver()
    out = r.translate(FakeClient(), "hey <@U123|alice> in <#C1|general> <!here>", team="T")
    assert "@alice" in out and "#general" in out and "@here" in out
    assert "<@" not in out and "<#" not in out and "<!" not in out


def test_translate_unlabeled_mention_uses_lookup():
    r = IdentityResolver()
    out = r.translate(FakeClient({"U999": "bob"}), "yo <@U999>", team="T")
    assert "@bob" in out


def test_translate_links_and_unescape():
    r = IdentityResolver()
    out = r.translate(FakeClient(), "see <https://x.com|the site> &amp; go", team="T")
    assert "the site (https://x.com)" in out
    assert "&" in out and "&amp;" not in out


# --- store basics ---------------------------------------------------------

def test_loop_guard_registry():
    s = _store()
    assert not s.is_self_posted("T", "C", "1.1")
    s.record_posted("T", "C", "1.1")
    assert s.is_self_posted("T", "C", "1.1")


def test_message_map_roundtrip():
    s = _store()
    s.map_message("TA", "CA", "10.0", "TB", "CB", "20.0")
    assert s.dst_for("TA", "CA", "10.0") == ("TB", "CB", "20.0")
    assert s.dst_for("TA", "CA", "missing") is None


def test_kv_roundtrip():
    s = _store()
    assert s.get_kv("hwm") is None
    s.set_kv("hwm", "123.45")
    assert s.get_kv("hwm") == "123.45"


# --- capture (poller → queue) ---------------------------------------------

def test_capture_enqueues_and_dedups():
    s = _store()
    relay, mlai, snc, settings = _relay(s)
    asyncio.run(relay.capture_from_mlai({"ts": "111.1", "user": "U_ALICE", "text": "hello"}))
    due = s.claim_due_inbound(10, time.time())
    assert len(due) == 1 and due[0]["direction"] == "mlai_to_snc"
    # Idempotent: the same source ts is never enqueued twice.
    asyncio.run(relay.capture_from_mlai({"ts": "111.1", "user": "U_ALICE", "text": "hello"}))
    assert len(s.claim_due_inbound(10, time.time())) == 1


def test_capture_skips_self_posted_echo():
    s = _store()
    relay, mlai, snc, settings = _relay(s)
    s.record_posted("T_M", "C_M", "222.2")  # a message the bridge itself posted
    asyncio.run(relay.capture_from_mlai({"ts": "222.2", "user": "U_X", "text": "echo"}))
    assert s.claim_due_inbound(10, time.time()) == []


def test_capture_skips_non_content_subtype():
    s = _store()
    relay, mlai, snc, settings = _relay(s)
    asyncio.run(relay.capture_from_mlai({"ts": "333.3", "subtype": "channel_join", "user": "U_X"}))
    assert s.claim_due_inbound(10, time.time()) == []


# --- delivery (worker → other workspace) ----------------------------------

def test_deliver_posts_records_and_maps():
    s = _store()
    relay, mlai, snc, settings = _relay(s)
    asyncio.run(relay.capture_from_mlai({"ts": "111.1", "user": "U_ALICE", "text": "hello"}))
    row = s.claim_due_inbound(10, time.time())[0]
    asyncio.run(relay.deliver(row))
    # Posted into S&C with the author spoofed via chat:write.customize.
    assert len(snc.posted) == 1
    assert snc.posted[0]["text"] == "hello"
    assert snc.posted[0]["username"] == "U_ALICE"
    assert snc.posted[0]["channel"] == "C_S"
    # Mapped + registered (loop guard) + no longer pending.
    dst = s.dst_for("T_M", "C_M", "111.1")
    assert dst is not None and s.is_self_posted("T_S", "C_S", dst[2])
    assert s.claim_due_inbound(10, time.time()) == []


def test_deliver_retries_on_failure_instead_of_dropping():
    s = _store()
    relay, mlai, snc, settings = _relay(s, snc_fail=True)
    asyncio.run(relay.capture_from_mlai({"ts": "111.1", "user": "U_ALICE", "text": "hello"}))
    row = s.claim_due_inbound(10, time.time())[0]
    asyncio.run(relay.deliver(row))
    assert snc.posted == []
    # Not lost: backed off into the future, attempt_count incremented.
    assert s.claim_due_inbound(10, time.time()) == []
    later = s.claim_due_inbound(10, time.time() + 120)
    assert len(later) == 1 and later[0]["attempt_count"] == 1
