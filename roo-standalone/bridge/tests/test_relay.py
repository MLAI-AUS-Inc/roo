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

    def __init__(self, names=None, fail=False, members=None):
        self._names = names or {}
        self._members = members or []
        self.posted = []
        self._ts = 1000.0
        self.fail = fail

    def users_info(self, user):
        for member in self._members:
            if member.get("id") == user:
                return {"ok": True, "user": member}
        if user in self._names:
            return {
                "ok": True,
                "user": {
                    "name": self._names[user],
                    "profile": {"display_name": self._names[user]},
                },
            }
        return {"ok": False}

    def users_list(self, limit=200, cursor=None):
        return {
            "ok": True,
            "members": self._members,
            "response_metadata": {"next_cursor": ""},
        }

    def chat_postMessage(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self._ts += 1
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"{self._ts:.6f}"}


def _store():
    d = tempfile.mkdtemp()
    return BridgeStore(os.path.join(d, "b.db"))


def _relay(store, remote_fail=False, mention_mode="plain", user_map=None):
    settings = BridgeSettings(
        MLAI_BOT_TOKEN="xoxb-x",
        MLAI_TEAM_ID="T_M",
        MLAI_BOT_USER_ID="B_MLAI",
        BRIDGE_MENTION_MODE=mention_mode,
    )
    mlai = FakeClient()
    remote = FakeClient(fail=remote_fail)
    pair = ResolvedPair(
        label="hex",
        remote_client=remote,
        remote_team="T_R",
        mlai_channel_id="C_M",
        remote_channel_id="C_R",
        remote_bot_user_id="B_REMOTE",
        mention_alias="hex",
        user_map=user_map or {},
    )
    relay = Relay(
        settings=settings,
        store=store,
        identity=IdentityResolver(),
        mlai_client=mlai,
        pairs=[pair],
    )
    return relay, mlai, remote, settings


def _member(user_id, name, email="", display_name=None, **extra):
    return {
        "id": user_id,
        "name": name,
        "real_name": display_name or name,
        "profile": {
            "display_name": display_name or name,
            "real_name": display_name or name,
            "email": email,
        },
        **extra,
    }


# --- loop guard: our own posts (incl. file uploads) never re-bridge ---------


def test_capture_drops_own_bot_posts_including_file_uploads():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    # A file upload the bridge made in the partner channel — NOT in the registry
    # (file-message ts isn't recorded), so it must be dropped by bot-id or the
    # photo loops forever.
    asyncio.run(
        relay.capture(
            "hex",
            True,
            {
                "ts": "900.0",
                "user": "B_REMOTE",
                "subtype": "file_share",
                "files": [{"name": "IMG_0516.jpg"}],
            },
        )
    )
    assert s.claim_due_inbound(10, time.time()) == []
    # A *different* bot (e.g. Jeanette) is still relayed.
    asyncio.run(
        relay.capture(
            "hex",
            True,
            {"ts": "901.0", "user": "U_OTHER", "bot_id": "B_J", "text": "hi"},
        )
    )
    assert len(s.claim_due_inbound(10, time.time())) == 1


# --- markup translation ---------------------------------------------------


def test_translate_labeled_mention_and_channel_and_special():
    r = IdentityResolver()
    out = r.translate(
        FakeClient(), "hey <@U123|alice> in <#C1|general> <!here>", team="T"
    )
    assert "@alice" in out and "#general" in out and "@here" in out
    assert "<@" not in out and "<#" not in out and "<!" not in out


def test_translate_links_and_preserve_slack_wire_escaping():
    r = IdentityResolver()
    out = r.translate(
        FakeClient(),
        "see <https://x.com|the site> &amp; email <mailto:a@example.com|a@example.com>",
        team="T",
    )
    assert "the site (https://x.com)" in out
    assert "&amp;" in out
    assert "email a@example.com" in out
    assert "mailto:" not in out


def test_escaped_user_markup_can_never_become_a_notification():
    r = IdentityResolver()
    r.index_workspace("T_R", [_member("URALICE", "alice", display_name="Alice")])
    out = r.translate(
        FakeClient(),
        "literal &lt;@URALICE&gt;",
        "T_M",
        dst_team="T_R",
        src_alias="mlai",
        dst_alias="hex",
        mention_mode="native",
    )
    assert out == "literal &lt;@URALICE&gt;"


def test_workspace_refresh_uses_all_cursor_pages():
    class PaginatedClient:
        def __init__(self):
            self.cursors = []

        def users_list(self, limit=200, cursor=None):
            self.cursors.append(cursor)
            if cursor is None:
                return {
                    "ok": True,
                    "members": [_member("UONE", "one", "one@example.com")],
                    "response_metadata": {"next_cursor": "page-2"},
                }
            return {
                "ok": True,
                "members": [_member("UTWO", "two", "two@example.com")],
                "response_metadata": {"next_cursor": ""},
            }

    client = PaginatedClient()
    directory = IdentityResolver().refresh_workspace(client, "T_R")

    assert client.cursors == [None, "page-2"]
    assert set(directory.by_id) == {"UONE", "UTWO"}


def test_native_structured_mention_maps_by_exact_email():
    r = IdentityResolver()
    r.index_workspace(
        "T_M", [_member("UMALICE", "alice", "Alice@Example.com", "Alice")]
    )
    r.index_workspace(
        "T_R", [_member("URALICE", "alice.hex", "alice@example.com", "Alice")]
    )

    out = r.translate(
        FakeClient(),
        "hello <@UMALICE>",
        "T_M",
        dst_team="T_R",
        src_alias="mlai",
        dst_alias="hex",
        mention_mode="native",
    )

    assert out == "hello <@URALICE>"


def test_manual_mapping_precedes_email_and_reverse_map_is_safe():
    r = IdentityResolver()
    r.index_workspace("T_M", [_member("UMSOURCE", "source", "source@example.com")])
    r.index_workspace(
        "T_R",
        [
            _member("UREMAIL", "email-match", "source@example.com"),
            _member("UROVERRIDE", "override", "other@example.com"),
        ],
    )

    out = r.translate(
        FakeClient(),
        "<@UMSOURCE>",
        "T_M",
        dst_team="T_R",
        src_alias="mlai",
        dst_alias="hex",
        user_map={"UMSOURCE": "UROVERRIDE"},
        mention_mode="native",
    )
    pair = ResolvedPair(
        "hex",
        FakeClient(),
        "T_R",
        "C_M",
        "C_R",
        user_map={
            "UMONE": "URDUP",
            "UMTWO": "URDUP",
            "UMTHREE": "UROK",
        },
    )

    assert out == "<@UROVERRIDE>"
    assert pair.reverse_user_map() == {"UROK": "UMTHREE"}


def test_unmatched_structured_mention_is_labeled_but_does_not_ping():
    r = IdentityResolver()
    r.index_workspace(
        "T_M", [_member("UMALICE", "alice", "alice@mlai.example", "Alice")]
    )
    r.index_workspace("T_R", [_member("URBOB", "bob", "bob@hex.example", "Bob")])

    out = r.translate(
        FakeClient(),
        "ask <@UMALICE>",
        "T_M",
        dst_team="T_R",
        src_alias="mlai",
        dst_alias="hex",
        mention_mode="native",
    )

    assert out == "ask @Alice (MLAI)"
    assert "<@" not in out


def test_explicit_destination_only_handle_becomes_native_mention():
    r = IdentityResolver()
    r.index_workspace(
        "T_R", [_member("URALICE", "alice", "alice@hex.example", "Alice Smith")]
    )

    out = r.translate(
        FakeClient(),
        "please ask @hex:alice and @hex:alice-smith",
        "T_M",
        dst_team="T_R",
        src_alias="mlai",
        dst_alias="hex",
        mention_mode="native",
    )

    assert out == "please ask <@URALICE> and <@URALICE>"


def test_ambiguous_or_inactive_explicit_handles_never_ping():
    r = IdentityResolver()
    r.index_workspace(
        "T_R",
        [
            _member("URONE", "alex-one", display_name="Alex Smith"),
            _member("URTWO", "alex-two", display_name="Alex Smith"),
            _member("URGONE", "gone", deleted=True),
            _member("URBOT", "helper", is_bot=True),
        ],
    )

    out = r.translate(
        FakeClient(),
        "@hex:alex-smith @hex:gone @hex:helper",
        "T_M",
        dst_team="T_R",
        src_alias="mlai",
        dst_alias="hex",
        mention_mode="native",
    )

    assert out == "@hex:alex-smith @hex:gone @hex:helper"
    assert "<@" not in out


def test_explicit_mentions_are_not_parsed_inside_code_or_links():
    r = IdentityResolver()
    r.index_workspace("T_R", [_member("URALICE", "alice", display_name="Alice")])

    out = r.translate(
        FakeClient(),
        "`@hex:alice` ```\n@hex:alice\n``` <https://x.test/@hex:alice|@hex:alice> @hex:alice",
        "T_M",
        dst_team="T_R",
        src_alias="mlai",
        dst_alias="hex",
        mention_mode="native",
    )

    assert out.count("<@URALICE>") == 1
    assert "`@hex:alice`" in out
    assert "https://x.test/@hex:alice" in out


def test_observe_mode_resolves_without_emitting_native_markup():
    r = IdentityResolver()
    r.index_workspace("T_R", [_member("URALICE", "alice", display_name="Alice")])

    out = r.translate(
        FakeClient(),
        "hi @hex:alice",
        "T_M",
        dst_team="T_R",
        src_alias="mlai",
        dst_alias="hex",
        mention_mode="observe",
    )

    assert out == "hi @Alice (HEX)"
    assert r.health()["mention_counts"]["explicit_resolved"] == 1


def test_mass_mentions_remain_inert_in_native_mode():
    r = IdentityResolver()
    r.index_workspace("T_R", [])
    out = r.translate(
        FakeClient(),
        "<!here> <!channel> <!everyone> <!subteam^S123|@admins>",
        "T_M",
        dst_team="T_R",
        src_alias="mlai",
        dst_alias="hex",
        mention_mode="native",
    )
    assert out == "@here @channel @everyone @admins"


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
    asyncio.run(
        relay.capture("hex", False, {"ts": "111.1", "user": "U_ALICE", "text": "hi"})
    )
    due = s.claim_due_inbound(10, time.time())
    assert len(due) == 1 and due[0]["direction"] == "hex:to_remote"
    # Idempotent on the source coordinates.
    asyncio.run(
        relay.capture("hex", False, {"ts": "111.1", "user": "U_ALICE", "text": "hi"})
    )
    assert len(s.claim_due_inbound(10, time.time())) == 1


def test_capture_to_mlai_uses_remote_team_and_channel():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    asyncio.run(
        relay.capture("hex", True, {"ts": "222.2", "user": "U_BOB", "text": "yo"})
    )
    due = s.claim_due_inbound(10, time.time())
    assert len(due) == 1 and due[0]["direction"] == "hex:to_mlai"
    assert due[0]["src_team"] == "T_R" and due[0]["src_channel"] == "C_R"


def test_capture_skips_self_posted_echo():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    s.record_posted("T_M", "C_M", "333.3")  # a message the bridge itself posted
    asyncio.run(
        relay.capture("hex", False, {"ts": "333.3", "user": "U_X", "text": "echo"})
    )
    assert s.claim_due_inbound(10, time.time()) == []


def test_capture_skips_non_content_subtype():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    asyncio.run(
        relay.capture(
            "hex", False, {"ts": "444.4", "subtype": "channel_join", "user": "U"}
        )
    )
    assert s.claim_due_inbound(10, time.time()) == []


# --- delivery (worker → destination) --------------------------------------


def test_deliver_to_remote_posts_records_and_maps():
    s = _store()
    relay, mlai, remote, settings = _relay(s)
    asyncio.run(
        relay.capture("hex", False, {"ts": "111.1", "user": "U_ALICE", "text": "hello"})
    )
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
    asyncio.run(
        relay.capture("hex", True, {"ts": "222.2", "user": "U_BOB", "text": "inbound"})
    )
    row = s.claim_due_inbound(10, time.time())[0]
    asyncio.run(relay.deliver(row))
    assert len(mlai.posted) == 1 and mlai.posted[0]["channel"] == "C_M"


def test_delivery_wires_native_mentions_in_both_directions():
    s = _store()
    relay, mlai, remote, settings = _relay(s, mention_mode="native")
    relay.identity.index_workspace(
        "T_M",
        [
            _member("UMAUTHOR", "mlai-author", "author@mlai.example", "MLAI Author"),
            _member("UMSAM", "sam", "sam@mlai.example", "Sam"),
            _member("UMALICE", "alice", "alice@example.com", "Alice"),
        ],
    )
    relay.identity.index_workspace(
        "T_R",
        [
            _member("URAUTHOR", "hex-author", "author@hex.example", "HEX Author"),
            _member("URALICE", "alice-hex", "alice@example.com", "Alice"),
        ],
    )

    asyncio.run(
        relay.capture(
            "hex",
            False,
            {
                "ts": "230.1",
                "user": "UMAUTHOR",
                "text": "hello <@UMALICE> and @hex:alice-hex",
            },
        )
    )
    asyncio.run(relay.deliver(s.claim_due_inbound(10, time.time())[0]))
    asyncio.run(
        relay.capture(
            "hex",
            True,
            {
                "ts": "230.2",
                "user": "URAUTHOR",
                "text": "please ask @mlai:sam",
            },
        )
    )
    asyncio.run(relay.deliver(s.claim_due_inbound(10, time.time())[0]))

    assert remote.posted[0]["text"] == "hello <@URALICE> and <@URALICE>"
    assert mlai.posted[0]["text"] == "please ask <@UMSAM>"


def test_deliver_retries_on_failure_instead_of_dropping():
    s = _store()
    relay, mlai, remote, settings = _relay(s, remote_fail=True)
    asyncio.run(
        relay.capture("hex", False, {"ts": "111.1", "user": "U_ALICE", "text": "hello"})
    )
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
    asyncio.run(
        relay.capture(
            "hex",
            True,
            {
                "ts": "205.0",
                "user": "U_HEX",
                "text": "looks good",
                "thread_ts": "200.0",
            },
        )
    )
    row = s.claim_due_inbound(10, time.time())[0]
    asyncio.run(relay.deliver(row))
    # It must land in MLAI threaded under the original parent (100), via the reverse map.
    assert len(mlai.posted) == 1
    assert mlai.posted[0]["channel"] == "C_M"
    assert mlai.posted[0]["thread_ts"] == "100.0"
