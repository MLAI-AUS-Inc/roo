"""Exercise the real executor queue boundary and opt-in catalog, no model calls."""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from roo.backend_identity import BackendActorContext, use_backend_actor_context
from roo.config import Settings
from roo.payment_reminders import ReceiptStore
from roo.skills.loader import Skill


def test_report_skill_is_opt_in_and_never_default_admin():
    base = {'_env_file': None, 'SLACK_BOT_TOKEN': 'synthetic', 'SLACK_SIGNING_SECRET': 'synthetic',
            'OPENAI_API_KEY': 'synthetic'}
    assert 'studio-hours' not in Settings(**base, STUDIO_REPORTS_ENABLED=False).enabled_skill_names
    assert 'studio-hours' in Settings(**base, STUDIO_REPORTS_ENABLED=True).enabled_skill_names
    assert 'studio-hours' not in Settings(**base, ROO_SURFACE='admin', ROO_ALLOWED_DM_USER_IDS='UADMIN',
                                        STUDIO_REPORTS_ENABLED=True).enabled_skill_names


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [
    {'action': 'summary'}, {'action': 'summary', 'month': 'recent', 'months': 3},
    {'action': 'summary', 'month': 'last_complete', 'months': 3}])
async def test_real_executor_queues_authenticated_request_and_returns_text(tmp_path, monkeypatch, params):
    # Some legacy agent tests register a lightweight executor stub.
    current = sys.modules.get('roo.skills.executor')
    if current is not None and not getattr(current, '__file__', None):
        sys.modules.pop('roo.skills.executor')
    executor = importlib.import_module('roo.skills.executor')
    settings = SimpleNamespace(ROO_SURFACE='public', STUDIO_REPORTS_ENABLED=True,
                               STUDIO_REPORTS_SLACK_TEAM_ID='T123', STUDIO_REPORTS_QUEUE_DIR=str(tmp_path))
    monkeypatch.setattr(executor, 'get_settings', lambda: settings)
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
    # Backend/API callers without the verified Slack task context cannot queue.
    result = await executor.SkillExecutor().execute(skill, 'show Mark hours', 'UMARK',
        channel_id='C123', thread_ts='1.0', param_overrides={'action': 'detailed'})
    assert 'could not verify' in result.message
    assert len(list(tmp_path.glob('*.json'))) == 1
