"""Signed HTTP handoff and opt-in catalog behavior without live providers."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
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
    monkeypatch.setattr(runtime, '_timesheet_ack', AsyncMock())
    monkeypatch.setattr(runtime, '_handle_app_mention_with_room_choice', AsyncMock(side_effect=AssertionError('LLM path')))
    yield TestClient(runtime.app), settings, tmp_path
    runtime.app.dependency_overrides.clear()


def event(**updates):
    return dict(type='event_callback', team_id='T123', event_id='Ev1', event={
        'type': 'app_mention', 'text': '<@UBOT> invoice', 'channel': 'C123',
        'user': 'UBUILDER', 'ts': '123.456', **updates})


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


def test_invoice_disk_failure_releases_signed_lease_and_retry_deduplicates(app, monkeypatch):
    client, settings, root = app
    body = json.dumps(event()).encode()
    original = ReceiptStore.write
    monkeypatch.setattr(ReceiptStore, 'write', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 503
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
    monkeypatch.setattr(runtime, '_is_slack_context_allowed', lambda *a, **kw: False)
    body = json.dumps(event(ts='123.999')).encode()
    assert client.post('/slack/events', content=body, headers=headers(body)).status_code == 200
    assert not (root / 'queue').exists()


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
    monkeypatch.setattr(timesheet_worker, 'ReportAPI', lambda *a, **kw: s.api)
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
