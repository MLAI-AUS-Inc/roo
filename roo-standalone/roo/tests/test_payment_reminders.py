from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import httpx
import pytest

from roo.payment_reminders import (
    Builder, DeliveryRejected, ReceiptStore, ReminderConfig, render_messages, run_reminders,
)
from roo.payment_reminder_worker import ReminderAPI, main


LINEAR_ID = "00000000-0000-0000-0000-000000000001"
ORG_ID = "00000000-0000-0000-0000-000000000002"
BUILDER = Builder("U123", LINEAR_ID)
NOW = datetime(2026, 9, 10, 9, tzinfo=ZoneInfo("Australia/Melbourne"))


def issue(number=1, **updates):
    return {"id": f"id-{number}", "identifier": f"MLA-{number}",
            "title": f"Build feature {number}",
            "url": f"https://linear.app/mlai/issue/MLA-{number}/feature",
            "archivedAt": None, "assignee": {"id": LINEAR_ID},
            "state": {"name": "In Progress", "type": "started"}, **updates}


@pytest.fixture
def config(tmp_path):
    return ReminderConfig(True, date(2026, 9, 11), NOW.tzinfo, 9, "T123", ORG_ID,
                          (BUILDER,), tmp_path / "receipts")


@pytest.fixture
def api():
    result = Mock()
    result.open_issues.return_value = [issue()]
    result.open_dm.return_value = "D123"
    result.post_message.return_value = "123.456"
    return result


@pytest.mark.parametrize("stamp,expected", [
    ("2026-09-10T08:59:59+10:00", None),
    ("2026-09-10T09:00:00+10:00", "2026-09-11"),
    ("2026-09-10T23:59:59+10:00", "2026-09-11"),
    ("2026-09-11T00:00:00+10:00", None),
    ("2026-09-17T09:00:00+10:00", None),
    ("2026-09-24T09:00:00+10:00", "2026-09-25"),
    ("2026-09-03T09:00:00+10:00", None),
    ("2026-10-08T09:00:00+11:00", "2026-10-09"),
    ("2026-10-07T22:00:00+00:00", "2026-10-09"),
])
def test_delivery_window_and_dst(config, stamp, expected):
    due = config.due_friday(datetime.fromisoformat(stamp))
    assert (due.isoformat() if due else None) == expected


def test_disabled_and_outside_window_make_no_calls(config, api):
    for conf, stamp in [(replace(config, enabled=False), NOW), (config, NOW + timedelta(days=1))]:
        store = Mock()
        assert run_reminders(conf, api, store, stamp)["status"] == "not_due"
        assert not api.mock_calls
        assert not store.mock_calls


def valid_env():
    return {"PAYMENT_REMINDERS_ENABLED": "true", "PAYMENT_SLACK_TEAM_ID": "T123",
            "PAYMENT_LINEAR_ORGANIZATION_ID": ORG_ID,
            "PAYMENT_BUILDERS_JSON": json.dumps([BUILDER.__dict__])}


@pytest.mark.parametrize("updates", [
    {"PAYMENT_REMINDERS_ENABLED": "yes"}, {"PAYMENT_FIRST_FRIDAY": "2026-09-12"},
    {"PAYMENT_REMINDER_HOUR": "24"}, {"PAYMENT_BUILDERS_JSON": "{}"},
    {"PAYMENT_BUILDERS_JSON": "[]"}, {"PAYMENT_SLACK_TEAM_ID": "C123"},
    {"PAYMENT_LINEAR_ORGANIZATION_ID": "unknown"},
    {"PAYMENT_BUILDERS_JSON": json.dumps([BUILDER.__dict__, BUILDER.__dict__])},
    {"PAYMENT_BUILDERS_JSON": '[{"slack_user_id":"../oops","linear_user_id":"bad"}]'},
])
def test_invalid_config(updates):
    with pytest.raises((ValueError, TypeError)):
        ReminderConfig.from_env(valid_env() | updates)


def test_defaults_and_valid_config():
    assert not ReminderConfig.from_env({}).enabled
    config = ReminderConfig.from_env(valid_env())
    assert config.builders == (BUILDER,)
    assert config.first_payment_date == date(2026, 9, 11)


def test_full_list_and_copy():
    issues = [issue(i) for i in range(1, 501)]
    messages = render_messages(issues, first_payment=True)
    text = "\n".join(messages)
    assert "First payments are this Friday!" in text
    assert "Please get all your hours in" in text
    assert "Friday by 12pm (noon)" in text
    assert "500" in messages[0]
    assert len(messages) > 1
    assert all(len(message) <= 3800 for message in messages)
    for item in issues:
        assert text.count(f"|{item['identifier']}>") == 1
        assert item["title"] in text
    assert "First payments" not in render_messages([issue()], first_payment=False)[0]
    assert "Friday by 12pm (noon)" in render_messages([issue()], first_payment=False)[0]
    assert render_messages([], first_payment=True) == []


def test_escaping_and_extreme_title():
    messages = render_messages([issue(title="<!channel> & <@U123> " + "x" * 10000)], first_payment=True)
    assert all(len(message) <= 3800 for message in messages)
    text = "".join(messages)
    assert "<!channel>" not in text
    assert "&lt;!channel&gt;" in text
    assert text.count("x") == 10000
    assert text.count("|MLA-1>") == 1


@pytest.mark.parametrize("url", ["https://evil.example/issue/1", "http://linear.app/issue/1",
                                 "https://linear.app/issue/1|<!channel>"])
def test_unsafe_links_fail_before_delivery(url):
    with pytest.raises(ValueError):
        render_messages([issue(url=url)], first_payment=True)


def test_success_receipts_survive_restart(config, api):
    first = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)
    second = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)
    assert first["builders"][0]["status"] == second["builders"][0]["status"] == "sent"
    api.open_issues.assert_called_once_with(LINEAR_ID)
    api.post_message.assert_called_once()


def test_empty_builder_not_messaged(config, api):
    api.open_issues.return_value = []
    assert run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)["builders"][0]["status"] == "empty"
    api.open_dm.assert_not_called()
    api.post_message.assert_not_called()


def test_dry_run_has_no_writes_or_dm(config, api):
    store = Mock()
    result = run_reminders(config, api, store, NOW, dry_run=True)
    assert result["builders"][0]["messages"]
    assert not store.mock_calls
    api.open_dm.assert_not_called()
    api.post_message.assert_not_called()


def test_concurrent_builder_is_skipped(config, api):
    key = "T123_2026-09-11_U123"
    store = ReceiptStore(config.receipts_dir)
    with store.locked(key) as acquired:
        assert acquired
        result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)
        assert result["builders"][0]["status"] == "busy"
    api.post_message.assert_not_called()


def test_uncertain_send_is_not_retried(config, api):
    api.post_message.side_effect = httpx.ReadTimeout("timeout")
    store = ReceiptStore(config.receipts_dir)
    assert run_reminders(config, api, store, NOW)["builders"][0]["status"] == "error"
    assert run_reminders(config, api, store, NOW)["builders"][0]["status"] == "needs_review"
    api.post_message.assert_called_once()


def test_crash_after_slack_accepts_requires_review(config, api):
    class CrashAfterSend(ReceiptStore):
        def write(self, key, receipt):
            if any(p["status"] == "sent" for p in receipt["parts"]):
                raise OSError("disk failed after send")
            super().write(key, receipt)
    run_reminders(config, api, CrashAfterSend(config.receipts_dir), NOW)
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)
    assert result["builders"][0]["status"] == "needs_review"
    api.post_message.assert_called_once()


def test_partial_delivery_rate_limit_resumes_unsent_parts(config, api):
    api.open_issues.return_value = [issue(i) for i in range(100)]
    messages = render_messages(api.open_issues.return_value, first_payment=True)
    responses = iter(["1.0", DeliveryRejected("ratelimited", 120)])
    def send(*args):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value
    api.post_message.side_effect = send
    store = ReceiptStore(config.receipts_dir)
    run_reminders(config, api, store, NOW)
    assert api.post_message.call_count == 2
    run_reminders(config, api, store, NOW + timedelta(seconds=60))
    assert api.post_message.call_count == 2
    api.post_message.side_effect = None
    result = run_reminders(config, api, store, NOW + timedelta(seconds=120))
    assert result["builders"][0]["status"] == "sent"
    assert api.post_message.call_count == len(messages) + 1
    assert sum(call.args[1] == messages[0] for call in api.post_message.call_args_list) == 1


def test_permanent_rejection_requires_review(config, api):
    api.post_message.side_effect = DeliveryRejected("missing_scope")
    store = ReceiptStore(config.receipts_dir)
    assert run_reminders(config, api, store, NOW)["builders"][0]["status"] == "needs_review"
    run_reminders(config, api, store, NOW)
    api.post_message.assert_called_once()


def test_expiring_window_stops_before_next_message(config, api):
    api.open_issues.return_value = [issue(i) for i in range(100)]
    times = iter([NOW, NOW + timedelta(days=1)])
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW, clock=lambda: next(times))
    assert result["builders"][0]["status"] == "pending"
    api.post_message.assert_called_once()


def test_one_failed_builder_does_not_block_others(config, api):
    second = Builder("U456", "00000000-0000-0000-0000-000000000003")
    api.open_issues.side_effect = [ValueError("unavailable"), [issue(2)]]
    result = run_reminders(replace(config, builders=(BUILDER, second)), api, ReceiptStore(config.receipts_dir), NOW)
    assert [b["status"] for b in result["builders"]] == ["error", "sent"]


def test_mapping_change_does_not_reuse_saved_tasks(config, api):
    api.post_message.side_effect = DeliveryRejected("ratelimited", 120)
    store = ReceiptStore(config.receipts_dir)
    run_reminders(config, api, store, NOW)
    changed = replace(config, builders=(Builder("U123", ORG_ID),))
    assert run_reminders(changed, api, store, NOW)["builders"][0]["status"] == "error"
    api.post_message.assert_called_once()


def make_api(handler):
    return ReminderAPI(httpx.Client(transport=httpx.MockTransport(handler)),
                       linear_key="test-read-key", slack_token="test-bot-token")


def page(nodes, more=False, cursor=None):
    return {"data": {"issues": {"nodes": nodes,
            "pageInfo": {"hasNextPage": more, "endCursor": cursor}}}}


def test_paginated_linear_read_includes_all_open_categories():
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["variables"]["after"] is None:
            return httpx.Response(200, json=page([
                issue(1), issue(2, state={"name": "Someday", "type": "backlog"}),
                issue(3, state={"name": "Shipped", "type": "completed"}),
                issue(4, state={"name": "Won't do", "type": "canceled"}),
                issue(5, archivedAt="2026-01-01"),
            ], True, "cursor-1"))
        return httpx.Response(200, json=page([issue(6), issue(1)]))
    result = make_api(handler).open_issues(LINEAR_ID)
    assert [i["identifier"] for i in result] == ["MLA-1", "MLA-2", "MLA-6"]
    assert len(requests) == 2
    assert requests[1]["variables"]["after"] == "cursor-1"
    assert "includeArchived: false" in requests[0]["query"]
    assert "mutation" not in requests[0]["query"]


@pytest.mark.parametrize("body", [
    {"errors": [{"message": "failed"}], "data": page([issue()])["data"]},
    page([issue(assignee={"id": ORG_ID})]),
    page([issue()], True, None),
    {"data": {"issues": {"nodes": [], "pageInfo": {}}}},
])
def test_partial_or_mismatched_linear_response_fails(body):
    with pytest.raises(ValueError):
        make_api(lambda _: httpx.Response(200, json=body)).open_issues(LINEAR_ID)


def test_cursor_loop_is_an_error():
    with pytest.raises(ValueError):
        make_api(lambda _: httpx.Response(200, json=page([issue()], True, "same"))).open_issues(LINEAR_ID)


def test_workspace_mismatch(config):
    api = make_api(lambda _: httpx.Response(200, json={"ok": True, "team_id": "TOTHER"}))
    with pytest.raises(ValueError, match="Slack workspace"):
        api.verify_workspace(config)


def test_linear_organization_mismatch(config):
    def handler(request):
        body = {"ok": True, "team_id": "T123"} if request.url.host == "slack.com" else {"data": {"organization": {"id": "other"}}}
        return httpx.Response(200, json=body)
    with pytest.raises(ValueError, match="Linear organization"):
        make_api(handler).verify_workspace(config)


@pytest.mark.parametrize("change", [{"deleted": True}, {"is_bot": True}, {"team_id": "TOTHER"}])
def test_inactive_or_wrong_workspace_builder(change):
    user = {"id": "U123", "team_id": "T123", **change}
    with pytest.raises(ValueError):
        make_api(lambda _: httpx.Response(200, json={"ok": True, "user": user})).verify_builder(BUILDER, "T123")


def test_slack_payload_and_confirmation():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "channel": "D123", "ts": "1.2"})
    assert make_api(handler).post_message("D123", "tasks") == "1.2"
    assert requests == [{"channel": "D123", "text": "tasks", "parse": "none",
                         "unfurl_links": False, "unfurl_media": False}]


def test_slack_rate_limit_and_ambiguous_success():
    with pytest.raises(DeliveryRejected) as error:
        make_api(lambda _: httpx.Response(429, headers={"Retry-After": "123"})).post_message("D123", "tasks")
    assert error.value.retry_after == 123
    with pytest.raises(ValueError):
        make_api(lambda _: httpx.Response(200, json={"ok": True})).post_message("D123", "tasks")


def test_disabled_cli_does_not_need_credentials(monkeypatch, capsys):
    monkeypatch.setenv("PAYMENT_REMINDERS_ENABLED", "false")
    assert main(["--once"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"


def test_integrated_tick_uses_private_dm_and_all_pages(config):
    posts = []
    def handler(request):
        payload = dict(request.url.params) if request.method == "GET" else json.loads(request.content)
        if request.url.host == "api.linear.app":
            assert request.headers["Authorization"] == "test-read-key"
            query = payload["query"]
            if "PaymentReminderOrganization" in query:
                return httpx.Response(200, json={"data": {"organization": {"id": ORG_ID}}})
            if "PaymentReminderUser(" in query:
                return httpx.Response(200, json={"data": {"user": {"id": LINEAR_ID, "active": True}}})
            after = payload["variables"]["after"]
            return httpx.Response(200, json=page([issue(2)] if after else [issue(1)], not after, "next"))
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "auth.test":
            body = {"team_id": "T123"}
        elif method == "users.info":
            assert request.method == "GET"
            assert payload == {"user": "U123"}
            body = {"user": {"id": "U123", "team_id": "T123", "deleted": False}}
        elif method == "conversations.open":
            assert payload == {"users": "U123"}
            body = {"channel": {"id": "D123"}}
        elif method == "chat.postMessage":
            posts.append(payload)
            body = {"channel": "D123", "ts": "1.2"}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"ok": True, **body})
    result = run_reminders(config, make_api(handler), ReceiptStore(config.receipts_dir), NOW)
    assert result["builders"][0]["status"] == "sent"
    assert len(posts) == 1
    assert "|MLA-1>" in posts[0]["text"] and "|MLA-2>" in posts[0]["text"]
    assert posts[0]["channel"] == "D123"


def test_different_fortnight_sends_again_without_first_copy(config, api):
    store = ReceiptStore(config.receipts_dir)
    run_reminders(config, api, store, NOW)
    run_reminders(config, api, store, NOW + timedelta(days=14))
    assert api.post_message.call_count == 2
    assert "First payments" not in api.post_message.call_args.args[1]


def test_pre_send_disk_failure_never_posts(config, api):
    store = ReceiptStore(config.receipts_dir)
    store.write = Mock(side_effect=OSError("disk full"))
    assert run_reminders(config, api, store, NOW)["builders"][0]["status"] == "error"
    api.post_message.assert_not_called()


def test_account_check_rate_limit_stops_tick_with_retry_delay(config, api):
    api.verify_builder.side_effect = DeliveryRejected("ratelimited", 180)
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)
    assert result["builders"][0]["retry_after"] == 180
    api.post_message.assert_not_called()


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_unsent_transport_failure_retries_after_restart(config, api, error_type):
    attempts = []
    accepted = []
    def handler(request):
        attempts.append(json.loads(request.content))
        if len(attempts) == 1:
            raise error_type("failure before sending", request=request)
        accepted.append(attempts[-1])
        return httpx.Response(200, json={"ok": True, "channel": "D123", "ts": "1.2"})
    api.post_message = make_api(handler).post_message
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)
    assert result["builders"][0]["status"] == "pending"
    receipt = ReceiptStore(config.receipts_dir).read("T123_2026-09-11_U123")
    assert receipt["parts"][0]["status"] == "pending"
    assert receipt["parts"][0]["retry_at"] == (NOW + timedelta(seconds=60)).timestamp()
    run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW + timedelta(seconds=59))
    assert len(attempts) == 1
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW + timedelta(seconds=60))
    assert result["builders"][0]["status"] == "sent"
    run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW + timedelta(seconds=120))
    assert len(attempts) == 2
    assert len(accepted) == 1
    assert attempts[0] == attempts[1]


@pytest.mark.parametrize("error_type", [
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError,
    httpx.WriteError, httpx.RemoteProtocolError,
])
def test_uncertain_transport_failure_still_requires_review(config, api, error_type):
    attempts = []
    def handler(request):
        attempts.append(request)
        raise error_type("may have reached Slack", request=request)
    api.post_message = make_api(handler).post_message
    first = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)
    assert first["builders"][0]["status"] == "error"
    second = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW + timedelta(minutes=5))
    assert second["builders"][0]["status"] == "needs_review"
    assert len(attempts) == 1


def test_partial_send_connect_failure_then_uncertain_retry(config, api):
    api.open_issues.return_value = [issue(i) for i in range(100)]
    messages = render_messages(api.open_issues.return_value, first_payment=True)
    attempts = []
    def handler(request):
        attempts.append(json.loads(request.content)["text"])
        if len(attempts) == 2:
            raise httpx.ConnectError("connection unavailable", request=request)
        if len(attempts) == 3:
            raise httpx.ReadTimeout("response lost", request=request)
        return httpx.Response(200, json={"ok": True, "channel": "D123", "ts": "1.2"})
    api.post_message = make_api(handler).post_message
    assert run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)["builders"][0]["status"] == "pending"
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW + timedelta(minutes=1))
    assert result["builders"][0]["status"] == "error"
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW + timedelta(minutes=2))
    assert result["builders"][0]["status"] == "needs_review"
    assert attempts == [messages[0], messages[1], messages[1]]
    receipt = ReceiptStore(config.receipts_dir).read("T123_2026-09-11_U123")
    assert receipt["parts"][0]["status"] == "sent"
    assert receipt["parts"][1]["status"] == "sending"


def test_unsent_retry_does_not_send_after_thursday(config, api):
    attempts = []
    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("connection unavailable", request=request)
    api.post_message = make_api(handler).post_message
    late = NOW.replace(hour=23, minute=59, second=30)
    assert run_reminders(config, api, ReceiptStore(config.receipts_dir), late)["builders"][0]["status"] == "pending"
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), late + timedelta(seconds=60))
    assert result["status"] == "not_due"
    assert len(attempts) == 1


def test_unsent_retry_rechecks_account_before_delivery(config, api):
    attempts = []
    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection unavailable", request=request)
        return httpx.Response(200, json={"ok": True, "channel": "D123", "ts": "1.2"})
    api.post_message = make_api(handler).post_message
    run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW)
    api.verify_builder.side_effect = ValueError("builder account deactivated")
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW + timedelta(minutes=1))
    assert result["builders"][0]["status"] == "error"
    assert len(attempts) == 1
    api.verify_builder.side_effect = None
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW + timedelta(minutes=2))
    assert result["builders"][0]["status"] == "sent"
    assert len(attempts) == 2


def test_failed_retry_persistence_does_not_guess_sending_outcome(config, api):
    attempts = []
    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("connection unavailable", request=request)
    api.post_message = make_api(handler).post_message
    class FailedRetryWrite(ReceiptStore):
        def write(self, key, receipt):
            if any("retry_at" in part for part in receipt["parts"]):
                raise OSError("disk failed before retry state persisted")
            super().write(key, receipt)
    result = run_reminders(config, api, FailedRetryWrite(config.receipts_dir), NOW)
    assert result["builders"][0]["status"] == "error"
    result = run_reminders(config, api, ReceiptStore(config.receipts_dir), NOW + timedelta(minutes=1))
    assert result["builders"][0]["status"] == "needs_review"
    assert len(attempts) == 1
