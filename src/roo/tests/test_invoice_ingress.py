"""Signed HTTP handoff and opt-in catalog behavior without live providers."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlencode

import pytest
import httpx
from fastapi.testclient import TestClient

from roo import main as runtime
from roo.config import Settings, get_settings
from roo.payment_reminders import ReceiptStore
from roo.tests.test_timesheet_ingress import headers
from roo.tests.test_invoice_worker import service, config, THURSDAY, FRIDAY, queue


@pytest.fixture
def app(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, OPENAI_API_KEY='synthetic',
        SLACK_BOT_TOKEN='synthetic', SLACK_SIGNING_SECRET='test-secret',
        SLACK_RECEIPTS_DB_PATH=str(tmp_path / 'receipts.db'),
        STUDIO_INVOICE_COMMANDS_ENABLED=True, STUDIO_INVOICE_QUEUE_DIR=str(tmp_path / 'queue'),
        TIMESHEET_COMMANDS_ENABLED=True, TIMESHEET_SLACK_TEAM_ID='T123',
        TIMESHEET_SLACK_BOT_USER_ID='UBOT', TIMESHEET_RECIPIENT_SLACK_ID='USAM',
        TIMESHEET_QUEUE_DIR=str(tmp_path / 'sam-queue'))
    runtime.app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(runtime, 'post_message', Mock())
    monkeypatch.setattr(runtime, 'post_ephemeral', Mock())
    monkeypatch.setattr(runtime, '_handle_app_mention_with_room_choice', AsyncMock(side_effect=AssertionError('LLM path')))
    yield TestClient(runtime.app), settings, tmp_path
    runtime.app.dependency_overrides.clear()


def event(**updates):
    return dict(type='event_callback', team_id='T123', event_id='Ev1', event={
        'type': 'app_mention', 'text': '<@UBOT> invoice', 'channel': 'C123',
        'user': 'UBUILDER', 'ts': '123.456', **updates})


async def post_with_ack(body):
    """Keep the request loop alive and drain acknowledgements before assertions."""
    tasks = []
    real_create_task = asyncio.create_task

    def track_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        tasks.append(task)
        return task

    with patch.object(runtime.asyncio, 'create_task', track_task):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.app),
                                   base_url='http://test') as client:
            response = await client.post('/slack/events', content=body, headers=headers(body))
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        return response


@pytest.mark.asyncio
async def test_ack_assertion_waits_for_background_transport(app, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def delayed_transport(function, *args, **kwargs):
        if function is not runtime.post_message:
            return await real_to_thread(function, *args, **kwargs)
        entered.set()
        await release.wait()
        return function(*args, **kwargs)

    monkeypatch.setattr(runtime.asyncio, 'to_thread', delayed_transport)
    body = json.dumps(event(channel='G123')).encode()
    request = asyncio.create_task(post_with_ack(body))
    await asyncio.wait_for(entered.wait(), timeout=1)
    release.set()
    response = await request
    assert response.status_code == 200
    runtime.post_message.assert_called_once_with(channel='G123',
        text='Your invoice request is queued. Roo will verify your builder mapping and DM the result to you.',
        thread_ts='123.456')


@pytest.mark.parametrize('channel,thread,expected', [
    ('C123', None, '123.456'), ('C123', '100.000001', '100.000001'),
    ('G123', None, '123.456'),
])
@pytest.mark.asyncio
async def test_invoice_queue_ack_is_public_in_originating_thread(app, channel, thread, expected):
    client, settings, root = app
    body = json.dumps(event(channel=channel, thread_ts=thread)).encode()
    assert (await post_with_ack(body)).status_code == 200
    runtime.post_message.assert_called_once_with(channel=channel,
        text='Your invoice request is queued. Roo will verify your builder mapping and DM the result to you.',
        thread_ts=expected)
    runtime.post_ephemeral.assert_not_called()


@pytest.mark.asyncio
async def test_invoice_queue_ack_deduplicates_signed_event_types(app):
    client, settings, root = app
    for kind in ('app_mention', 'message', 'app_mention'):
        body = json.dumps(event(type=kind)).encode()
        assert (await post_with_ack(body)).status_code == 200
    assert runtime.post_message.call_count == 1
    runtime.post_ephemeral.assert_not_called()


@pytest.mark.asyncio
async def test_invoice_dm_ack_stays_private_and_timesheet_ack_stays_ephemeral(app):
    client, settings, root = app
    body = json.dumps(event(type='message', channel_type='im', channel='D123', text='invoice')).encode()
    assert (await post_with_ack(body)).status_code == 200
    assert runtime.post_message.call_args.kwargs['channel'] == 'D123'
    assert 'thread_ts' not in runtime.post_message.call_args.kwargs
    runtime.post_message.reset_mock()
    body = json.dumps(event(text='<@UBOT> timesheet', user='USAM', ts='123.457')).encode()
    assert (await post_with_ack(body)).status_code == 200
    runtime.post_ephemeral.assert_called_once()
    runtime.post_message.assert_not_called()


@pytest.mark.asyncio
async def test_failed_public_ack_keeps_invoice_queued(app):
    client, settings, root = app
    runtime.post_message.side_effect = RuntimeError('Slack unavailable')
    body = json.dumps(event()).encode()
    assert (await post_with_ack(body)).status_code == 200
    assert json.loads(next((root / 'queue').glob('*.json')).read_text())['status'] == 'pending'


def test_invoice_slash_ack_remains_ephemeral_without_public_post(app):
    client, settings, root = app
    body = urlencode(dict(command='/roo', text='invoice', team_id='T123', user_id='UBUILDER',
        channel_id='C123', trigger_id='slash-public-check')).encode()
    response = client.post('/slack/commands', content=body,
        headers=headers(body, 'application/x-www-form-urlencoded'))
    assert response.status_code == 200 and response.json()['response_type'] == 'ephemeral'
    runtime.post_message.assert_not_called()
    runtime.post_ephemeral.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('channel,thread,stamp', [
    ('C123', 'invalid', '123.456'), ('C123', None, None), ('INVALID', None, '123.456'),
])
async def test_invoice_ack_never_posts_unthreaded_or_invalid_channel(app, channel, thread, stamp):
    await runtime._invoice_ack({'channel': channel, 'thread_ts': thread, 'ts': stamp}, 'Fixed acknowledgement')
    runtime.post_message.assert_not_called()
    runtime.post_ephemeral.assert_not_called()


def test_signed_mention_dm_and_slash_queue_metadata_only(app):
    client, settings, root = app
    for payload in [event(), event(type='message', channel_type='im', channel='D123', text='invoice latest', ts='123.457')]:
        body = json.dumps(payload).encode()
        assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    body = urlencode(dict(command='/roo', text='invoice 2026-10-23', team_id='T123', user_id='UBUILDER',
                          channel_id='C123', trigger_id='slash1')).encode()
    response = client.post('/slack/commands', content=body, headers=headers(body, 'application/x-www-form-urlencoded'))
    assert response.status_code == 200 and response.json()['response_type'] == 'ephemeral'
    requests = [json.loads(p.read_text()) for p in (root / 'queue').glob('*.json')]
    assert len(requests) == 3
    assert {r['selector'] for r in requests} == {'current', 'latest', '2026-10-23'}
    assert {r['actor'] for r in requests} == {'UBUILDER'}
    assert all(set(r) <= {'team', 'actor', 'channel', 'source', 'selector', 'requested_at', 'status', 'thread_ts'} for r in requests)
    assert not (root / 'sam-queue').exists()


def test_invalid_signature_never_queues_invoice(app):
    client, settings, root = app
    body = json.dumps(event()).encode()
    h = headers(body)
    h['X-Slack-Signature'] = 'v0=' + '0' * 64
    assert client.post('/slack/events', content=body, headers=h).status_code == 403
    assert not (root / 'queue').exists()
    runtime.post_message.assert_not_called()
    runtime.post_ephemeral.assert_not_called()


def test_invoice_disk_failure_releases_signed_lease_and_retry_deduplicates(app, monkeypatch):
    client, settings, root = app
    body = json.dumps(event()).encode()
    original = ReceiptStore.write
    monkeypatch.setattr(ReceiptStore, 'write', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 503
    runtime.post_message.assert_not_called()
    monkeypatch.setattr(ReceiptStore, 'write', original)
    for kind in ('app_mention', 'message', 'app_mention'):
        body = json.dumps(event(type=kind)).encode()
        assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    assert len(list((root / 'queue').glob('*.json'))) == 1


def test_invoice_is_disabled_on_admin_and_respects_deployment_context_gate(app, monkeypatch):
    client, settings, root = app
    settings.ROO_SURFACE = 'admin'
    body = json.dumps(event()).encode()
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    assert not (root / 'queue').exists()
    settings.ROO_SURFACE = 'public'
    runtime.post_message.reset_mock()
    monkeypatch.setattr(runtime, '_is_slack_context_allowed', lambda *a, **kw: False)
    body = json.dumps(event(ts='123.999')).encode()
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    assert not (root / 'queue').exists()
    runtime.post_message.assert_not_called()


def test_exact_parser_rejects_targets_and_rollout_periods(tmp_path):
    from roo.invoice_commands import enqueue, parse_command
    from roo.timesheets import TimesheetError
    assert parse_command('<@UBOT> invoice', 'UBOT') == 'current'
    assert parse_command('invoice latest', dm=True) == 'latest'
    assert parse_command('invoice', 'UBOT') is None
    settings = SimpleNamespace(ROO_SURFACE='public', STUDIO_INVOICE_COMMANDS_ENABLED=True,
        STUDIO_INVOICE_QUEUE_DIR=str(tmp_path / 'queue'), TIMESHEET_SLACK_TEAM_ID='T123',
        TIMESHEET_SLACK_BOT_USER_ID='UBOT', TIMESHEET_FIRST_CUTOFF='2026-09-11')
    for text in ('invoice UOTHER', 'invoice 2026-10-09', 'invoice 2026-10-30', 'invoice ../secret'):
        result = enqueue(settings, team='T123', actor='UBUILDER', channel='D123', source_id=text,
                         text=text, dm=True, now=THURSDAY)
        assert result['handled']
    assert not (tmp_path / 'queue').exists()


def test_opt_in_invoice_skill_loads_and_help_does_not_execute_model_parameters():
    from pathlib import Path
    from roo.skills.loader import load_skills
    settings = Settings(_env_file=None, STUDIO_INVOICE_COMMANDS_ENABLED=True)
    skills = load_skills(Path('skills'), allowed_names=settings.enabled_skill_names)
    assert 'studio-invoices' in {s.name for s in skills}
    assert 'studio-invoices' not in Settings(_env_file=None).enabled_skill_names


@pytest.mark.asyncio
async def test_invoice_help_ignores_model_supplied_owner_and_bank_fields(app):
    from pathlib import Path
    from roo.skills.loader import load_skills
    from roo.skills.executor import SkillExecutor
    client, settings, root = app
    skill = next(s for s in load_skills(Path('skills'), allowed_names=settings.enabled_skill_names)
                 if s.name == 'studio-invoices')
    result = await SkillExecutor().execute(skill, 'generate another member invoice', 'UBUILDER',
        channel_id='C123', param_overrides={'action': 'generate', 'actor': 'UOTHER', 'bsb': '123456'})
    assert 'invoice latest' in result.message
    assert '123456' not in result.message
    assert not (root / 'queue').exists()


def test_existing_worker_once_processes_invoice_queue_without_timesheet_schedule(config, monkeypatch):
    from roo import timesheet_worker
    s = service(config, scheduled=False)
    queue(s, selector='2026-10-23')
    monkeypatch.setattr(timesheet_worker.TimesheetConfig, 'from_env', lambda env: config)
    monkeypatch.setattr('roo.invoice_source.InvoiceSourceAPI', lambda *a, **kw: s.api)
    monkeypatch.setenv('STUDIO_INVOICES_ENABLED', 'true')
    monkeypatch.setenv('STUDIO_INVOICES_SCHEDULE_ENABLED', 'false')
    monkeypatch.setenv('STUDIO_INVOICE_QUEUE_DIR', str(s.queue.directory))
    class FrozenDateTime:
        @staticmethod
        def now(tz):
            return FRIDAY
    monkeypatch.setattr(timesheet_worker, 'datetime', FrozenDateTime)
    assert timesheet_worker.main(['--once']) == 0
    assert len(s.api.files) == 1 and s.api.files[0][0] == 'DUBUILDER'
