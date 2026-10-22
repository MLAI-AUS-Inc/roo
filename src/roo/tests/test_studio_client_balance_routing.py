"""Hermetic balance skill routing and authenticated queue boundary checks."""
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from pydantic import ValidationError
import pytest

from roo.backend_identity import BackendActorContext, use_backend_actor_context
from roo.config import Settings
from roo.payment_reminders import ReceiptStore
from roo.skills.loader import Skill, load_skill_from_directory
from roo.studio_report_commands import enqueue
from roo.timesheets import fingerprint

NOW = datetime(2026, 10, 11, 4, tzinfo=timezone.utc)
BASE_SETTINGS = {'_env_file': None, 'SLACK_BOT_TOKEN': 'synthetic',
                 'SLACK_SIGNING_SECRET': 'synthetic', 'OPENAI_API_KEY': 'synthetic'}


@pytest.fixture
def setup(tmp_path):
    settings = SimpleNamespace(ROO_SURFACE='public', STUDIO_REPORTS_ENABLED=True,
        STUDIO_CLIENT_BALANCE_ENABLED=True, STUDIO_REPORTS_SLACK_TEAM_ID='T123',
        STUDIO_REPORTS_QUEUE_DIR=str(tmp_path))
    return SimpleNamespace(settings=settings, store=ReceiptStore(tmp_path), directory=tmp_path)


def queue(setup, params=None, *, kind='client_balance', event='Ev123', actor='UMARK', now=NOW):
    context = BackendActorContext('T123', actor, 'C123', '1.0', event)
    return enqueue(setup.settings, context, params or {}, report_kind=kind,
        user_id=actor, channel_id='C123', thread_ts='1.0', now=now)


def requests(setup):
    return [setup.store.read(path.stem) for path in setup.directory.glob('*.json')]


@pytest.mark.parametrize('hours,balance', [(False, False), (True, False), (False, True), (True, True)])
def test_balance_skill_is_independently_opt_in(hours, balance):
    configured = Settings(**BASE_SETTINGS, STUDIO_REPORTS_ENABLED=hours,
                          STUDIO_CLIENT_BALANCE_ENABLED=balance)
    assert ('studio-hours' in configured.enabled_skill_names) is hours
    assert ('studio-client-balance' in configured.enabled_skill_names) is balance


def test_allowlist_cannot_bypass_balance_flag_or_public_surface():
    with pytest.raises(ValidationError, match='STUDIO_CLIENT_BALANCE_ENABLED'):
        Settings(**BASE_SETTINGS, ROO_ENABLED_SKILLS='studio-client-balance')
    with pytest.raises(ValidationError, match='only on Public Roo'):
        Settings(**BASE_SETTINGS, ROO_SURFACE='admin', ROO_ALLOWED_DM_USER_IDS='UADMIN',
                 STUDIO_CLIENT_BALANCE_ENABLED=True, ROO_ENABLED_SKILLS='studio-client-balance')
    allowed = Settings(**BASE_SETTINGS, STUDIO_CLIENT_BALANCE_ENABLED=True,
                       ROO_ENABLED_SKILLS='studio-client-balance')
    assert allowed.enabled_skill_names == frozenset({'studio-client-balance'})


def test_real_skill_exposes_only_safe_selectors_and_distinct_actions():
    # Legacy agent tests sometimes register a frontmatter stub.
    from roo.routing_eval.runner import _ensure_real_frontmatter
    _ensure_real_frontmatter()
    from roo import router
    skill = load_skill_from_directory(Path(__file__).resolve().parents[2] / 'skills' / 'studio-client-balance')
    assert skill.requires_auth
    assert not router.lint_catalog([skill])
    tool = router.build_tools([skill])[0][0]['function']
    assert set(tool['parameters']['properties']) == {'action', 'month', 'months', 'client'}
    assert tool['parameters']['properties']['action']['enum'] == ['summary', 'detailed']
    decision = router._validate_tool_call(skill.name, {'action': 'summary', 'client': 'Mark Ghiasy',
        'actor': 'UOTHER', 'source_path': '/private/ledger', 'project_ids': ['secret'],
        'amount': 999999, 'hour_units': 999999, 'allocation_rate_cents_per_hour': 1,
        'report_kind': 'hours'}, {skill.name: skill}, strict_action=True)
    assert decision.params == {'client': 'Mark Ghiasy'}


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [
    {'action': 'summary'},
    {'action': 'summary', 'month': 'recent', 'months': 3, 'client': 'Mark Ghiasy'},
    {'action': 'detailed', 'month': 'last_complete', 'months': 3, 'client': 'self'},
])
async def test_real_executor_queues_fixed_kind_and_authenticated_actor(setup, monkeypatch, params):
    current = sys.modules.get('roo.skills.executor')
    if current is not None and not getattr(current, '__file__', None):
        sys.modules.pop('roo.skills.executor')
    executor = importlib.import_module('roo.skills.executor')
    monkeypatch.setattr(executor, 'get_settings', lambda: setup.settings)
    skill = Skill(name='studio-client-balance', description='', content='', path=Path('.'))
    context = BackendActorContext('T123', 'UMARK', 'C123', '1.0', 'Ev123')
    malicious = {**params, 'actor': 'UOTHER', 'project_ids': ['secret'], 'source_path': '/tmp/private',
                 'amount': 999999, 'hour_units': 999999, 'allocation_rate_cents_per_hour': 1,
                 'grant': True, 'report_kind': 'hours'}
    with use_backend_actor_context(context):
        result = await executor.SkillExecutor().execute(skill, 'show used hours and purchased-hour credits', 'UMARK',
            channel_id='C123', thread_ts='1.0', param_overrides=malicious)
    assert result.success and 'Studio hours and purchased-hours report' in result.message and 'DM' in result.message
    assert result.data == params
    request, = requests(setup)
    assert request['actor'] == 'UMARK' and request['report_kind'] == 'client_balance'
    assert request['selector']['action'] == params['action']
    assert request['selector'].get('client') == params.get('client')
    assert set(request['selector']) <= {'action', 'month', 'months', 'client', 'project_to_date'}
    assert not any(value in json.dumps(request) for value in ['UOTHER', 'source_path', '999999', '/tmp/private'])
    if 'month' not in params:
        assert request['selector'] == {'action': 'summary', 'month': 'all', 'months': 1}
    result = await executor.SkillExecutor().execute(skill, 'show report', 'UMARK', channel_id='C123', thread_ts='1.0')
    assert 'could not verify' in result.message
    assert len(requests(setup)) == 1


@pytest.mark.parametrize('context', [
    None,
    BackendActorContext('TOTHER', 'UMARK', 'C123', '1.0', 'Ev123'),
    BackendActorContext('T123', 'UOTHER', 'C123', '1.0', 'Ev123'),
    BackendActorContext('T123', 'UMARK', 'COTHER', '1.0', 'Ev123'),
    BackendActorContext('T123', 'UMARK', 'C123', '2.0', 'Ev123'),
    BackendActorContext('T123', 'UMARK', 'C123', '1.0', ''),
])
def test_signed_context_mismatch_cannot_queue_balance_request(setup, context):
    result = enqueue(setup.settings, context, {'client': 'Mark Ghiasy'}, report_kind='client_balance',
                     user_id='UMARK', channel_id='C123', thread_ts='1.0', now=NOW)
    assert 'could not verify' in result
    assert requests(setup) == []


def test_enqueue_flag_gates_are_independent_and_public_only(setup):
    setup.settings.STUDIO_REPORTS_ENABLED = False
    assert 'DM' in queue(setup)
    assert 'not enabled' in queue(setup, kind='hours', event='hours')
    setup.settings.STUDIO_CLIENT_BALANCE_ENABLED = False
    assert 'not enabled' in queue(setup, event='balance-disabled')
    setup.settings.STUDIO_CLIENT_BALANCE_ENABLED = True
    setup.settings.ROO_SURFACE = 'admin'
    assert 'not enabled' in queue(setup, event='admin')
    assert len(requests(setup)) == 1


def test_legacy_hours_key_and_shape_preserved_with_distinct_balance_receipt(setup):
    assert 'DM' in queue(setup, kind='hours')
    old_key = fingerprint({'team': 'T123', 'actor': 'UMARK', 'channel': 'C123', 'source': 'Ev123'})
    hours = setup.store.read(old_key)
    assert hours and 'report_kind' not in hours
    assert hours['selector'] == {'action': 'summary', 'month': '2026-10', 'months': 1}
    assert 'DM' in queue(setup)
    assert 'already' in queue(setup, {'client': 'Other'})
    assert 'already' in queue(setup, kind='hours')
    assert len(requests(setup)) == 2
    balance = next(request for request in requests(setup) if request.get('report_kind'))
    assert 'client' not in balance['selector']


def test_detail_followups_inherit_only_same_kind_actor_and_team(setup):
    queue(setup, {'month': '2026-06', 'months': 3, 'client': 'Mark Ghiasy'})
    queue(setup, {'month': '2026-09', 'client': 'Hours client'}, kind='hours', event='hours', now=NOW + timedelta(seconds=1))
    queue(setup, {'month': '2026-08', 'client': 'Other'}, actor='UOTHER', event='other', now=NOW + timedelta(seconds=2))
    queue(setup, {'action': 'detailed'}, event='detail', now=NOW + timedelta(seconds=3))
    detail = next(request for request in requests(setup) if request['selector']['action'] == 'detailed')
    assert detail['selector'] == {'action': 'detailed', 'month': '2026-06', 'months': 3, 'client': 'Mark Ghiasy'}
    queue(setup, {'action': 'detailed'}, kind='hours', event='hours-detail', now=NOW + timedelta(seconds=4))
    hours_detail = next(request for request in requests(setup)
        if request.get('report_kind', 'hours') == 'hours' and request['selector']['action'] == 'detailed')
    assert hours_detail['selector'] == {'action': 'detailed', 'month': '2026-09', 'months': 1, 'client': 'Hours client'}


@pytest.mark.parametrize('action', [{'action': 'detailed'}, {'month': 'previous'}])
def test_balance_without_prior_finance_history_defaults_to_all_even_after_hours(setup, action):
    queue(setup, {'month': '2026-08', 'client': 'Hours client'}, kind='hours')
    queue(setup, action, event='balance', now=NOW + timedelta(seconds=1))
    balance = next(request for request in requests(setup) if request.get('report_kind') == 'client_balance')
    assert balance['selector']['month'] == 'all'
    assert 'client' not in balance['selector']


def test_explicit_period_and_self_override_finance_followup(setup):
    queue(setup, {'month': '2026-06', 'months': 3, 'client': 'Mark Ghiasy'})
    queue(setup, {'action': 'detailed', 'month': 'last', 'client': 'self'}, event='detail', now=NOW + timedelta(seconds=1))
    detail = next(request for request in requests(setup) if request['selector']['action'] == 'detailed')
    assert detail['selector'] == {'action': 'detailed', 'month': '2026-09', 'months': 1, 'client': 'self'}


@pytest.mark.parametrize('params', [
    {'action': 'write_payment'}, {'month': '2026-11'}, {'month': '2026-08', 'months': 13},
    {'month': ['2026-08']}, {'action': {'summary': True}}, {'client': '\n'},
])
def test_invalid_request_returns_finance_specific_validation_without_queuing(setup, params):
    assert 'Studio hours and purchased-hours report' in queue(setup, params)
    assert requests(setup) == []


def test_unknown_report_kind_fails_closed(setup):
    assert 'could not verify' in queue(setup, kind='accounting_write')
    assert requests(setup) == []
