"""Real signed ingress, queue, report and threaded file delivery with fake Slack."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from dataclasses import replace

from fastapi.testclient import TestClient
import httpx
import pytest

from roo.timesheet_demo import (DemoAPI, create_app, DEV_APP, DEV_BOT, DEV_CHANNEL, DEV_TEAM, NOTICE)
from roo.timesheets import TimesheetError
from roo.timesheet_commands import enqueue
from roo.timesheet_worker import TimesheetService
from roo.tests.test_timesheets import config, api_for, item, settings, END


@pytest.fixture
def demo(tmp_path):
    calls = []
    counter = 0
    def handler(request):
        nonlocal counter
        body = json.loads(request.content) if request.content and request.headers.get('content-type', '').startswith('application/json') else {}
        calls.append((request.url.path, body))
        if request.url.path.endswith('auth.test'):
            data = {'ok': True, 'team_id': DEV_TEAM, 'user_id': DEV_BOT}
        elif request.url.path.endswith('users.info'):
            uid = request.url.params['user']
            data = {'ok': True, 'user': {'id': uid, 'team_id': DEV_TEAM, 'profile': {'api_app_id': DEV_APP}}}
        elif request.url.path.endswith('chat.postMessage'):
            assert body['channel'] == DEV_CHANNEL
            assert body['thread_ts'] == '123.000001'
            assert NOTICE in body['text']
            data = {'ok': True, 'channel': DEV_CHANNEL, 'ts': '124.000001'}
        elif request.url.path.endswith('files.getUploadURLExternal'):
            counter += 1
            data = {'ok': True, 'file_id': f'F{counter}', 'upload_url': 'https://files.slack.com/upload/test'}
        elif request.url.host == 'files.slack.com':
            assert 'authorization' not in request.headers
            return httpx.Response(200, text='OK')
        elif request.url.path.endswith('files.completeUploadExternal'):
            assert body['channel_id'] == DEV_CHANNEL
            assert body['thread_ts'] == '123.000001'
            data = {'ok': True, 'files': body['files']}
        else:
            pytest.fail('Unexpected API path: ' + request.url.path)
        return httpx.Response(200, json=data)
    api = DemoAPI(httpx.Client(transport=httpx.MockTransport(handler)), 'synthetic-token')
    env = {'ROO_ENVIRONMENT': 'development', 'TIMESHEET_DEMO_ENABLED': 'true',
           'SLACK_BOT_TOKEN': 'synthetic-token', 'SLACK_SIGNING_SECRET': 'test-secret',
           'TIMESHEET_DEMO_ALLOWED_SLACK_IDS': 'UALAN', 'TIMESHEET_DEMO_DATA_DIR': str(tmp_path / 'demo')}
    return env, api, calls


def send(app, text='timesheet', user='UALAN', channel=DEV_CHANNEL, message_ts='123.000002', valid=True):
    payload = {'type': 'event_callback', 'team_id': DEV_TEAM, 'api_app_id': DEV_APP,
               'event': {'type': 'app_mention', 'user': user, 'channel': channel,
                         'text': f'<@{DEV_BOT}> {text}', 'ts': message_ts, 'thread_ts': '123.000001'}}
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    signature = hmac.new(b'test-secret', b'v0:' + ts.encode() + b':' + body, hashlib.sha256).hexdigest()
    return TestClient(app).post('/slack/events', content=body, headers={
        'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': 'v0=' + (signature if valid else '0'*64)})


def test_signed_demo_pipeline_threaded_csvs_and_two_periods(demo):
    env, api, calls = demo
    app = create_app(env, api)
    assert send(app).status_code == 200
    assert send(app).status_code == 200  # duplicate Slack delivery
    assert app.state.service.commands(datetime.now(timezone.utc))[0]['status'] == 'sent'
    report = app.state.service.ledger.load()['periods']['2026-09-11']
    assert sum(r['units'] for r in report['rows']) / 4 == 8.25
    assert len(report['exceptions']) == 1
    assert len([x for x in calls if x[0].endswith('chat.postMessage')]) == 1
    assert len([x for x in calls if x[0].endswith('files.completeUploadExternal')]) == 3
    assert app.state.service.commands(datetime.now(timezone.utc)) == []
    env['TIMESHEET_DEMO_AS_OF'] = '2026-09-26T12:00:00+10:00'
    later = create_app(env, api)
    assert send(later, 'timesheet 2026-09-25', message_ts='125.000001').status_code == 200
    assert later.state.service.commands(datetime.now(timezone.utc))[0]['status'] == 'sent'
    report = later.state.service.ledger.load()['periods']['2026-09-25']
    assert sum(r['units'] for r in report['rows']) / 4 == 5
    assert not report['exceptions']


@pytest.mark.parametrize('change', [{'user': 'USTRANGER'}, {'channel': 'COTHER'}, {'valid': False}])
def test_demo_rejects_other_users_channels_and_invalid_signatures(demo, change):
    env, api, calls = demo
    app = create_app(env, api)
    response = send(app, **change)
    assert response.status_code == (403 if change.get('valid') is False else 200)
    assert app.state.service.commands(datetime.now(timezone.utc)) == []
    assert not calls


@pytest.mark.parametrize('key,value', [('ROO_ENVIRONMENT', 'production'), ('TIMESHEET_DEMO_ENABLED', 'false')])
def test_demo_cannot_run_as_production(demo, key, value):
    env, api, calls = demo
    env[key] = value
    with pytest.raises(TimesheetError, match='explicit_development'):
        create_app(env, api)
    assert not calls


def test_demo_rejects_real_source_and_reports(demo):
    env, api, calls = demo
    app = create_app(env, api)
    with pytest.raises(TimesheetError, match='linear_disabled'):
        api.linear('query { issues { nodes { id } } }')
    with pytest.raises(TimesheetError, match='invalid_demo_destination'):
        app.state.service.deliver_request({}, {}, 'test', END)
    assert not calls


def test_demo_health_verifies_development_identity(demo):
    env, api, calls = demo
    with TestClient(create_app(env, api)) as client:
        assert client.get('/healthz/ready').json()['mode'] == 'fake-timesheet-demo'
    assert any(x[0].endswith('auth.test') for x in calls)


@pytest.mark.parametrize('dm,channel,text', [(True, 'DALAN', 'timesheet'),
    (False, 'C123', '<@UBOT> timesheet')])
def test_production_rejects_alan_even_with_legacy_allowlist(config, dm, channel, text):
    cfg = replace(config, allowed_users=('USAM', 'UALAN'))
    public = settings(cfg)
    public.TIMESHEET_ALLOWED_SLACK_IDS = 'USAM,UALAN'
    result = enqueue(public, team='T123', actor='UALAN', channel=channel, source_id='123.456',
                     text=text, dm=dm, now=END, thread_ts='123.000001')
    assert 'not authorized' in result['text']
    assert not cfg.queue.exists()


def test_demo_supports_explicit_extra_tester_without_production_access(demo):
    env, api, calls = demo
    env['TIMESHEET_DEMO_ALLOWED_SLACK_IDS'] = 'UALAN,USAM'
    app = create_app(env, api)
    assert send(app, user='USAM').status_code == 200
    assert app.state.service.commands(datetime.now(timezone.utc))[0]['status'] == 'sent'
