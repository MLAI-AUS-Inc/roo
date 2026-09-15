"""Signed HTTP entry points, including the durable receipt/queue handoff."""
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from roo import main as runtime
from roo.config import Settings, get_settings
from roo.payment_reminders import ReceiptStore


@pytest.fixture
def app(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, OPENAI_API_KEY='synthetic',
        SLACK_BOT_TOKEN='xoxb-synthetic', SLACK_SIGNING_SECRET='test-secret',
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / 'receipts.db'),
        TIMESHEET_COMMANDS_ENABLED=True, TIMESHEET_SLACK_TEAM_ID='T123',
        TIMESHEET_SLACK_BOT_USER_ID='UBOT', TIMESHEET_RECIPIENT_SLACK_ID='USAM',
        TIMESHEET_QUEUE_DIR=str(tmp_path / 'queue'))
    runtime.app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(runtime, '_timesheet_ack', AsyncMock())
    # An exact timesheet request must never enter inference or generic mention handling.
    monkeypatch.setattr(runtime, '_handle_app_mention_with_room_choice', AsyncMock(side_effect=AssertionError('LLM path')))
    yield TestClient(runtime.app), settings, tmp_path
    runtime.app.dependency_overrides.clear()


def headers(body, content_type='application/json'):
    stamp = str(int(time.time()))
    digest = hmac.new(b'test-secret', b'v0:' + stamp.encode() + b':' + body, hashlib.sha256).hexdigest()
    return {'X-Slack-Request-Timestamp': stamp, 'X-Slack-Signature': 'v0=' + digest, 'Content-Type': content_type}


def payload(**updates):
    return {'type': 'event_callback', 'team_id': 'T123', 'event_id': 'Ev1', 'event': {
        'type': 'app_mention', 'text': '<@UBOT> timesheet', 'channel': 'C123', 'user': 'USAM', 'ts': '123.456', **updates}}


def test_signed_mention_and_direct_message_enqueue_privately(app):
    client, settings, root = app
    for event in [payload(), payload(type='message', channel_type='im', text='timesheet current', channel='DSAM', ts='123.457')]:
        body = json.dumps(event).encode()
        assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    requests = [json.loads(x.read_text()) for x in (root / 'queue').glob('*.json')]
    assert {r['selector'] for r in requests} == {'latest', 'current'}
    assert {r['actor'] for r in requests} == {'USAM'}
    runtime._handle_app_mention_with_room_choice.assert_not_called()


def test_signature_rejection_never_enqueues(app):
    client, settings, root = app
    body = json.dumps(payload()).encode()
    h = headers(body)
    h['X-Slack-Signature'] = 'v0=' + '0' * 64
    assert client.post('/slack/events', content=body, headers=h).status_code == 403
    assert not (root / 'queue').exists()


def test_generic_receipt_exists_but_queue_write_failed_retry_recovers(app, monkeypatch):
    client, settings, root = app
    body = json.dumps(payload()).encode()
    h = headers(body)
    original = ReceiptStore.write
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(ReceiptStore, 'write', fail)
    assert client.post('/slack/events', content=body, headers=h).status_code == 503
    monkeypatch.setattr(ReceiptStore, 'write', original)
    assert client.post('/slack/events', content=body, headers=h).status_code == 200
    assert client.post('/slack/events', content=body, headers=h).status_code == 200
    assert len(list((root / 'queue').glob('*.json'))) == 1


def test_two_slack_event_types_for_same_message_share_request(app):
    client, settings, root = app
    for kind in ('message', 'app_mention'):
        body = json.dumps(payload(type=kind)).encode()
        assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    assert len(list((root / 'queue').glob('*.json'))) == 1


def test_slash_command_private_ack_and_duplicate(app):
    client, settings, root = app
    body = urlencode({'command': '/roo-dev', 'text': 'timesheet', 'user_id': 'USAM',
                      'team_id': 'T123', 'channel_id': 'C123', 'trigger_id': 'trigger-1'}).encode()
    h = headers(body, 'application/x-www-form-urlencoded')
    response = client.post('/slack/commands', content=body, headers=h)
    assert response.status_code == 200
    assert response.json()['response_type'] == 'ephemeral'
    assert client.post('/slack/commands', content=body, headers=h).status_code == 200
    assert len(list((root / 'queue').glob('*.json'))) == 1


def test_other_users_and_admin_surface_never_queue(app):
    client, settings, root = app
    body = json.dumps(payload(user='UOTHER')).encode()
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    settings.ROO_SURFACE = 'admin'
    body = json.dumps(payload()).encode()
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    assert not (root / 'queue').exists()


@pytest.mark.parametrize('event', [
    payload(type='message', channel_type='im', text='timesheet 2026-09-11', channel='DSAM'),
    payload(text='<@UBOT> timesheet 2026-09-11', thread_ts='100.000001'),
])
def test_signed_sam_request_delivers_summary_and_every_csv_only_to_his_dm(app, event):
    from roo.timesheet_worker import TimesheetService
    from roo.timesheets import TimesheetConfig
    from roo.tests.test_timesheets import api_for, item, ORG, USER, END
    client, settings, root = app
    cfg = TimesheetConfig.from_env({
        'TIMESHEETS_ENABLED': 'true', 'TIMESHEET_SLACK_TEAM_ID': 'T123',
        'TIMESHEET_RECIPIENT_SLACK_ID': 'USAM', 'TIMESHEET_LINEAR_ORGANIZATION_ID': ORG,
        'TIMESHEET_BUILDERS_JSON': json.dumps([{'linear_user_id': USER, 'slack_user_id': 'UBUILDER'}]),
        'TIMESHEET_DATA_DIR': str(root / 'data'), 'TIMESHEET_QUEUE_DIR': str(root / 'queue'),
    })
    body = json.dumps(event).encode()
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    queued = json.loads(next((root / 'queue').glob('*.json')).read_text())
    assert queued['thread_ts'] == event['event'].get('thread_ts', event['event']['ts'])
    api = api_for([item()])
    assert TimesheetService(cfg, api).commands(END)[0]['status'] == 'sent'
    api.open_dm.assert_called_once_with('USAM')
    assert api.post_message.call_args.args[0] == 'DSAM'
    assert api.upload_csv.call_count >= 2
    assert all(call.args[0] == 'DSAM' for call in api.upload_csv.call_args_list)


@pytest.mark.parametrize('event', [payload(user='UALAN', thread_ts='100.000001'),
    payload(user='UALAN', type='message', channel_type='im', text='timesheet', channel='DALAN')])
def test_signed_alan_dm_and_thread_requests_are_denied(app, event):
    client, settings, root = app
    body = json.dumps(event).encode()
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    assert not (root / 'queue').exists()
    runtime._handle_app_mention_with_room_choice.assert_not_called()


def test_timesheet_handoff_completes_event_lease_and_failure_releases_it(app, monkeypatch):
    from roo.slack_security import get_slack_receipt_store
    client, settings, root = app
    body = json.dumps(payload()).encode()
    fingerprint = runtime._retry_managed_slack_event_fingerprint(body, "unused")
    store = get_slack_receipt_store(settings.SLACK_RECEIPTS_DB_PATH)
    original = ReceiptStore.write
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(ReceiptStore, "write", fail)
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 503
    # A failed handoff releases ownership immediately, allowing Slack's retry.
    disposition, claim = store.claim_event(fingerprint)
    assert disposition == "claimed"
    store.release(fingerprint, claim_token=claim)
    monkeypatch.setattr(ReceiptStore, "write", original)
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    assert store.claim_event(fingerprint) == ("completed", None)
    assert len(list((root / 'queue').glob('*.json'))) == 1
