"""Pure-logic tests for the bridge — no network, no Slack."""
import asyncio
import os
import tempfile
import time

from bridge.config import BridgeSettings
from bridge.identity import IdentityResolver
from bridge.relay import Relay, ResolvedPair
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


def _relay(store, remote_fail=False):
    settings = BridgeSettings(MLAI_BOT_TOKEN="xoxb-x", MLAI_TEAM_ID="T_M")
    mlai = FakeClient()
    remote = FakeClient(fail=remote_fail)
    pair = ResolvedPair(
        label="hex", remote_client=remote, remote_team="T_R",
        mlai_channel_id="C_M", remote_channel_id="C_R",
    )
    relay = Relay(
        settings=settings, store=store, identity=IdentityResolver(),
        mlai_client=mlai, pairs=[pair],
    )
    return relay, mlai, remote, settings


# --- markup translation ---------------------------------------------------

def test_translate_labeled_mention_and_channel_and_special():
    r = IdentityResolver()
    out = r.translate(FakeClient(), "hey <@U123|alice> in <#C1|general> <!here>", team="T")
    assert "@alice" in out and "#general" in out and "@here" in out
    assert "<@" not in out and "<#" not in out and "<!" not in out


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
    # reverse lookup (needed to thread replies on received parents)
    assert s.src_for("TB", "CB", "20.0") == ("TA", "CA", "10.0")
    assert s.src_for("TB", "CB", "missing") is None


# --- capture (poller → queue) ---------------------------------------------

def test_capture_to_remote_enqueues_with_pair_direction():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    asyncio.run(relay.capture("hex", False, {"ts": "111.1", "user": "U_ALICE", "text": "hi"}))
    due = s.claim_due_inbound(10, time.time())
    assert len(due) == 1 and due[0]["direction"] == "hex:to_remote"
    # Idempotent on the source coordinates.
    asyncio.run(relay.capture("hex", False, {"ts": "111.1", "user": "U_ALICE", "text": "hi"}))
    assert len(s.claim_due_inbound(10, time.time())) == 1


def test_capture_to_mlai_uses_remote_team_and_channel():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    asyncio.run(relay.capture("hex", True, {"ts": "222.2", "user": "U_BOB", "text": "yo"}))
    due = s.claim_due_inbound(10, time.time())
    assert len(due) == 1 and due[0]["direction"] == "hex:to_mlai"
    assert due[0]["src_team"] == "T_R" and due[0]["src_channel"] == "C_R"


def test_capture_skips_self_posted_echo():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    s.record_posted("T_M", "C_M", "333.3")  # a message the bridge itself posted
    asyncio.run(relay.capture("hex", False, {"ts": "333.3", "user": "U_X", "text": "echo"}))
    assert s.claim_due_inbound(10, time.time()) == []


def test_capture_skips_non_content_subtype():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    asyncio.run(relay.capture("hex", False, {"ts": "444.4", "subtype": "channel_join", "user": "U"}))
    assert s.claim_due_inbound(10, time.time()) == []


# --- delivery (worker → destination) --------------------------------------

def test_deliver_to_remote_posts_records_and_maps():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    asyncio.run(relay.capture("hex", False, {"ts": "111.1", "user": "U_ALICE", "text": "hello"}))
    row = s.claim_due_inbound(10, time.time())[0]
    asyncio.run(relay.deliver(row))
    # Posted into the partner channel, author spoofed via chat:write.customize.
    assert len(remote.posted) == 1
    assert remote.posted[0]["text"] == "hello"
    assert remote.posted[0]["username"] == "U_ALICE"
    assert remote.posted[0]["channel"] == "C_R"
    dst = s.dst_for("T_M", "C_M", "111.1")
    assert dst is not None and s.is_self_posted("T_R", "C_R", dst[2])
    assert s.claim_due_inbound(10, time.time()) == []


def test_deliver_to_mlai_routes_to_hub():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    asyncio.run(relay.capture("hex", True, {"ts": "222.2", "user": "U_BOB", "text": "inbound"}))
    row = s.claim_due_inbound(10, time.time())[0]
    asyncio.run(relay.deliver(row))
    assert len(mlai.posted) == 1 and mlai.posted[0]["channel"] == "C_M"


def test_deliver_retries_on_failure_instead_of_dropping():
    s = _store()
    relay, mlai, remote, settings = _relay(s, remote_fail=True)
    asyncio.run(relay.capture("hex", False, {"ts": "111.1", "user": "U_ALICE", "text": "hello"}))
    row = s.claim_due_inbound(10, time.time())[0]
    asyncio.run(relay.deliver(row))
    assert remote.posted == []
    # Not lost: backed off into the future, attempt_count incremented.
    assert s.claim_due_inbound(10, time.time()) == []
    later = s.claim_due_inbound(10, time.time() + 120)
    assert len(later) == 1 and later[0]["attempt_count"] == 1


def test_reply_threads_under_received_parent_via_reverse_map():
    # An MLAI message was already mirrored to the partner (parent 100 -> 200).
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    s.map_message("T_M", "C_M", "100.0", "T_R", "C_R", "200.0")
    # A reply now lands in the partner channel, on the *received* parent (200).
    asyncio.run(relay.capture("hex", True, {"ts": "205.0", "user": "U_HEX", "text": "looks good", "thread_ts": "200.0"}))
    row = s.claim_due_inbound(10, time.time())[0]
    asyncio.run(relay.deliver(row))
    # It must land in MLAI threaded under the original parent (100), via the reverse map.
    assert len(mlai.posted) == 1
    assert mlai.posted[0]["channel"] == "C_M"
    assert mlai.posted[0]["thread_ts"] == "100.0"
