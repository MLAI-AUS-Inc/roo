"""Exercise the real executor queue boundary and opt-in catalog, no model calls."""
from datetime import timedelta
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from roo.backend_identity import BackendActorContext, use_backend_actor_context
from roo.config import Settings
from roo.payment_reminders import ReceiptStore
from roo.router import build_tools
from roo.skills.loader import Skill, load_skill_from_directory
from tests.roo.skill_test_support import patch_skill_dependency
from tests.roo.test_studio_reports import NOW, queue, setup


def test_report_skill_is_opt_in_and_never_default_admin():
    base = {'_env_file': None, 'SLACK_BOT_TOKEN': 'synthetic', 'SLACK_SIGNING_SECRET': 'synthetic',
            'OPENAI_API_KEY': 'synthetic'}
    assert 'studio-hours' not in Settings(**base, STUDIO_REPORTS_ENABLED=False).enabled_skill_names
    assert 'studio-hours' in Settings(**base, STUDIO_REPORTS_ENABLED=True).enabled_skill_names
    assert 'studio-hours' not in Settings(**base, ROO_SURFACE='admin', ROO_ALLOWED_DM_USER_IDS='UADMIN',
                                        STUDIO_REPORTS_ENABLED=True).enabled_skill_names


def test_studio_hours_tool_declares_contributor_chart_options():
    path = Path(__file__).resolve().parents[2] / 'roo-standalone' / 'skills' / 'studio-hours'
    skill = load_skill_from_directory(path, import_client=False)
    tools, _ = build_tools([skill], channel_name=None)
    function = next(tool['function'] for tool in tools if tool['function']['name'] == 'studio-hours')
    properties = function['parameters']['properties']
    assert properties['chart']['enum'] == ['stacked', 'cumulative', 'both', 'monthly']
    assert properties['project']['type'] == 'string'
    assert function['parameters']['required'] == ['action']


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [
    {'action': 'summary'}, {'action': 'summary', 'month': 'recent', 'months': 3},
    {'action': 'summary', 'month': 'last_complete', 'months': 3},
    {'action': 'summary', 'month': 'all', 'client': 'Mark Ghiasy'},
    {'action': 'summary', 'month': 'recent', 'months': 3, 'client': 'mark ghiasy'},
    {'action': 'summary', 'month': 'all', 'client': 'Devashi Desai'},
    {'action': 'summary', 'month': 'all', 'client': 'Devarshi Desai'},
    {'action': 'summary', 'month': 'recent', 'months': 3, 'client': 'Devashi Desai'},
    {'action': 'summary', 'month': 'recent', 'months': 2, 'client': 'Devashi Desai',
     'project': 'Study Nash', 'chart': 'cumulative'},
    {'action': 'summary', 'month': 'all', 'client': 'Mark Ghiasy',
     'project': 'Master App', 'chart': 'both'},
    {'action': 'summary', 'chart': 'stacked'},
    {'action': 'summary', 'month': 'recent', 'months': 3, 'client': 'all'}])
async def test_real_executor_queues_authenticated_request_and_returns_text(tmp_path, monkeypatch, params):
    # Some legacy agent tests register a lightweight executor stub.
    current = sys.modules.get('roo.skills.executor')
    if current is not None and not getattr(current, '__file__', None):
        sys.modules.pop('roo.skills.executor')
    executor = importlib.import_module('roo.skills.executor')
    settings = SimpleNamespace(ROO_SURFACE='public', STUDIO_REPORTS_ENABLED=True,
                               STUDIO_REPORTS_SLACK_TEAM_ID='T123', STUDIO_REPORTS_QUEUE_DIR=str(tmp_path))
    patch_skill_dependency(monkeypatch, 'get_settings', lambda: settings)
    skill = Skill(name='studio-hours', description='', content='', path=Path('.'))
    context = BackendActorContext('T123', 'UMARK', 'C123', '1.0', 'Ev123')
    with use_backend_actor_context(context):
        result = await executor.SkillExecutor().execute(skill, 'show my Studio hours', 'UMARK',
            channel_id='C123', thread_ts='1.0', param_overrides=params)
    assert result.success and isinstance(result.message, str) and 'DM' in result.message
    request = ReceiptStore(tmp_path).read(next(tmp_path.glob('*.json')).stem)
    assert request['actor'] == 'UMARK'
    assert request['selector']['action'] == 'summary'
    assert request['selector']['months'] == params.get('months', 1)
    assert request['selector']['month'] not in {'recent', 'last_complete'}
    assert request['selector'].get('client') == params.get('client')
    assert request['selector'].get('project') == params.get('project')
    assert request['selector'].get('chart') == params.get('chart')
    # Backend/API callers without the verified Slack task context cannot queue.
    result = await executor.SkillExecutor().execute(skill, 'show Mark hours', 'UMARK',
        channel_id='C123', thread_ts='1.0', param_overrides={'action': 'detailed'})
    assert 'could not verify' in result.message
    assert len(list(tmp_path.glob('*.json'))) == 1


@pytest.mark.parametrize('followup, expected', [
    ({'action': 'detailed'}, {'client': 'Mark', 'project': 'Master App', 'chart': 'stacked'}),
    ({'month': 'previous', 'chart': 'cumulative'},
     {'client': 'Mark', 'project': 'Master App', 'chart': 'cumulative'}),
    ({'action': 'detailed', 'project': 'Cybertest'},
     {'client': 'Mark', 'project': 'Cybertest', 'chart': 'stacked'}),
    ({'action': 'detailed', 'project': 'all'}, {'client': 'Mark', 'chart': 'stacked'}),
    ({'action': 'detailed', 'client': 'self'}, {'client': 'self', 'chart': 'stacked'}),
])
def test_scoped_contributor_chart_followup(setup, followup, expected):
    queue(setup, params={'month': 'recent', 'months': 2, 'client': 'Mark',
                         'project': 'Master App', 'chart': 'stacked'})
    queue(setup, actor='UOTHER', event='other', now=NOW + timedelta(seconds=1),
          params={'project': 'Other project', 'chart': 'both'})
    assert 'DM' in queue(setup, params=followup, event='followup', now=NOW + timedelta(seconds=2))
    requests = [setup.service.queue.read(p.stem) for p in setup.config.queue.glob('*.json')]
    last = max(requests, key=lambda item: item['requested_at'])
    assert last['selector'] == {'action': followup.get('action', 'summary'),
                                'month': '2026-08', 'months': 2, **expected}


@pytest.mark.parametrize('params, prompt', [
    ({'project': ['Master App', 'Cybertest']}, 'one project name'),
    ({'project': ''}, 'one project name'),
    ({'chart': 'payroll'}, 'stacked hours chart'),
])
def test_invalid_chart_request_does_not_enter_queue(setup, params, prompt):
    assert prompt in queue(setup, params=params)
    assert not list(setup.config.queue.glob('*.json'))


@pytest.mark.asyncio
async def test_worker_breakdown_dm_queues_monthly_chart_without_placeholder(setup, monkeypatch):
    from roo.skills.contracts import SkillRequest
    from roo.skills.executors.studio_hours import StudioHoursExecutor

    patch_skill_dependency(monkeypatch, 'get_settings', lambda: setup.settings)
    context = BackendActorContext('T123', 'UMARK', 'DUMARK', '123.45', 'EvBreakdown')
    text = 'Give me the worker breakdown for who has been doing hours on the master app project over the last 3 months'
    req = SkillRequest(skill=None, text=text, user_id='UMARK', channel_id='DUMARK', thread_ts='123.45',
                       params={'month': 'current', 'months': 3, 'project': 'master app', 'action': 'summary'})
    with use_backend_actor_context(context):
        for _ in range(2):
            assert (await StudioHoursExecutor().execute(req))['suppress_post']
    saved, = [setup.service.queue.read(p.stem) for p in setup.config.queue.glob('*.json')]
    from roo.studio_reports import month_offset, TZ
    from roo.timesheets import timestamp
    current = timestamp(saved['requested_at']).astimezone(TZ).strftime('%Y-%m')
    assert saved['selector'] == {'action': 'summary', 'month': month_offset(current, -2).strftime('%Y-%m'),
                                 'months': 3, 'chart': 'monthly', 'project': 'master app'}
    assert saved['actor'] == 'UMARK' and saved['thread_ts'] == '123.45'


def test_worker_breakdown_preserves_explicit_ranges_and_other_views():
    from roo.studio_report_commands import hours_params
    original = {'month': '2026-08', 'months': 5, 'project': 'Master App'}
    assert hours_params('worker breakdown last 3 months from August to December 2026', original) == {
        **original, 'chart': 'monthly'}
    assert hours_params('hours by person over the last three full months', {'chart': 'cumulative'}) == {
        'chart': 'cumulative', 'month': 'last_complete', 'months': 3}
    assert hours_params('worker breakdown per project', {'chart': 'stacked'}) == {'chart': 'stacked'}
    assert hours_params('worker breakdown in text only', {}) == {}
    assert hours_params('stacked bars and cumulative lines by person', {'chart': 'both'}) == {'chart': 'both'}
    assert hours_params('hours by person on Master App', {'project': 'Master App', 'chart': 'both'})['chart'] == 'monthly'
