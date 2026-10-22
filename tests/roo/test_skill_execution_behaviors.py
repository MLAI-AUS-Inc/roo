"""Regression coverage for skills previously covered mainly by routing tests.

These tests exercise the public executor API and also run against the pre-class
snapshot, using fake model/backend boundaries. No database or network needed.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from roo.skills.executor import SkillExecutor
from roo.skills.loader import Skill
from roo.clients.mlai_backend import MLAIBackendUnavailableError


def skill(name, path=Path('.'), **kwargs):
    return Skill(name, 'Test capability', 'Use only supplied facts.', path, **kwargs)


def patch_prompt(monkeypatch, executor, response='Prompt response'):
    fake = AsyncMock(return_value=SimpleNamespace(content=response))
    method = executor._execute_with_llm
    # PromptExecutor owns the model dependency after extraction.
    monkeypatch.setitem(method.__globals__, 'chat', fake)
    return fake


@pytest.mark.asyncio
async def test_connect_users_passes_skill_instructions_and_request_to_model(monkeypatch):
    executor = SkillExecutor()
    model = patch_prompt(monkeypatch, executor)
    result = await executor.execute(skill('connect-users'), 'Find a Python mentor', 'UMEMBER',
                                    param_overrides={'query': 'Python'})
    assert result.success and result.message == 'Prompt response'
    prompt = model.call_args.args[0][1]['content']
    assert 'Use only supplied facts.' in prompt
    assert 'Find a Python mentor' in prompt and 'UMEMBER' in prompt
    model.assert_awaited_once()


@pytest.mark.asyncio
async def test_watt_qa_fallback_preserves_history_without_publishing(monkeypatch, tmp_path):
    executor = SkillExecutor()
    model = patch_prompt(monkeypatch, executor)
    publish = AsyncMock(side_effect=AssertionError('Q&A cannot publish'))
    monkeypatch.setattr('roo.clients.mlai_backend.MLAIBackendClient.generic_hackathon_create_announcement', publish)
    result = await executor.execute(skill('watt-the-hack', tmp_path), 'When is it?', 'UMEMBER',
                                   param_overrides={'action': 'event_qa'}, thread_history=[{'text':'Earlier question'}])
    assert result.success and result.message == 'Prompt response'
    assert 'Earlier question' in model.call_args.args[0][1]['content']
    publish.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('status, expected', [(403, 'only MLAI superusers'), (None, 'has been posted')])
async def test_watt_publish_preserves_requester_and_backend_denial(monkeypatch, status, expected):
    executor = SkillExecutor()
    create = AsyncMock(return_value={} if status is None else {'status_code': status})
    monkeypatch.setattr('roo.clients.mlai_backend.MLAIBackendClient', lambda: SimpleNamespace(generic_hackathon_create_announcement=create))
    monkeypatch.setattr('roo.slack_client.get_bot_user_id', lambda: 'UBOT')
    result = await executor.execute(skill('watt-the-hack'), 'announce', 'UHUMAN',
        param_overrides={'action':'announce','title':'Lunch','body':'At noon','user_id':'UFORGED'})
    assert expected in result.message
    assert create.call_args.kwargs == dict(slug='watt-the-hack', title='Lunch', body='At noon',
                                          requester_slack_id='UHUMAN', author_slack_id='UBOT')


@pytest.mark.asyncio
async def test_watt_wrong_channel_stops_before_backend_or_model(monkeypatch):
    executor = SkillExecutor()
    model = patch_prompt(monkeypatch, executor)
    monkeypatch.setattr('roo.slack_client.get_channel_name', lambda channel: 'general')
    create = AsyncMock(side_effect=AssertionError('wrong channel'))
    monkeypatch.setattr('roo.clients.mlai_backend.MLAIBackendClient.generic_hackathon_create_announcement', create)
    result = await executor.execute(skill('watt-the-hack', exclusive_channels=['watt-the-hack']),
                                   'announce Lunch', 'UUSER', channel_id='CGENERAL')
    assert 'only available' in result.message
    create.assert_not_called()
    model.assert_not_called()


@pytest.mark.asyncio
async def test_github_outage_returns_unavailable_without_starting_scan(monkeypatch):
    executor = SkillExecutor()
    client = SimpleNamespace(get_integration=AsyncMock(side_effect=MLAIBackendUnavailableError('offline')),
                             trigger_repo_scan=AsyncMock())
    monkeypatch.setattr('roo.clients.mlai_backend.MLAIBackendClient', lambda **kwargs: client)
    monkeypatch.setitem(executor._execute_github_integration.__globals__, 'get_settings',
        lambda: SimpleNamespace(MLAI_BACKEND_URL='https://backend.test', ROO_API_KEY='test', MLAI_API_KEY='', INTERNAL_API_KEY=''))
    result = await executor.execute(skill('github-integration'), 'scan my repo', 'UUSER')
    assert "couldn't reach MLAI backend" in result.message
    assert "haven't started" in result.message
    client.trigger_repo_scan.assert_not_called()


@pytest.mark.asyncio
async def test_github_existing_scan_requires_confirmation(monkeypatch):
    executor = SkillExecutor()
    client = SimpleNamespace(get_integration=AsyncMock(return_value={
        'github_repo':'org/repo', 'scan_completed':True,
        'connected_domains':[{'domain':'example.test', 'github_repo':'org/repo', 'scanned':True}]}),
        trigger_repo_scan=AsyncMock())
    monkeypatch.setattr('roo.clients.mlai_backend.MLAIBackendClient', lambda **kwargs: client)
    monkeypatch.setitem(executor._execute_github_integration.__globals__, 'get_settings',
        lambda: SimpleNamespace(MLAI_BACKEND_URL='https://backend.test', ROO_API_KEY='test', MLAI_API_KEY='', INTERNAL_API_KEY=''))
    result = await executor.execute(skill('github-integration'), 'scan example.test', 'UUSER',
                                   param_overrides={'action':'scan','domain':'example.test'})
    assert 'Do you want me to run another scan' in result.message
    assert result.blocks
    client.trigger_repo_scan.assert_not_called()


@pytest.mark.asyncio
async def test_invoice_chat_returns_help_without_creating_invoice():
    from roo.invoice_commands import HELP
    result = await SkillExecutor().execute(skill('studio-invoices'), 'create an invoice', 'UUSER')
    assert result.success and result.message == HELP
