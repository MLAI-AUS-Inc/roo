"""Offline package validation, loading, and dispatch boundaries; no databases."""
from tests.roo.skill_test_support import patch_skill_dependency
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from roo.skills.catalogue import inspect_catalogue
from roo.skills.loader import load_skill_report, Skill
from roo.skills.lint import main as lint
from roo.skills.executor import SkillExecutor
from roo.skills.registry import HANDLERS


def package(root, folder='example', name='example', client=None, **metadata):
    path = root / folder
    path.mkdir()
    fields = {'name': name, 'description': 'Example', **metadata}
    (path / 'SKILL.md').write_text('---\n' + yaml.safe_dump(fields) + '---\nInstructions')
    if client is not None:
        (path / 'client.py').write_text(client)
    return path


def test_lint_does_not_import_clients_and_checks_required(tmp_path, capsys):
    marker = tmp_path / 'imported'
    package(tmp_path, name='tone-of-voice', client=f'open({str(marker)!r}, "w").write("bad")')
    assert lint(['--skills-dir', str(tmp_path), '--required', 'tone-of-voice']) == 0
    assert not marker.exists()
    assert lint(['--skills-dir', str(tmp_path), '--required', 'missing']) == 1
    assert 'missing or invalid' in capsys.readouterr().out


@pytest.mark.parametrize('metadata, field', [
    ({'requires_auth': 'true'}, 'requires_auth'),
    ({'exclusive_channels': 'general'}, 'exclusive_channels'),
    ({'actions': [{'name': 'read'}, {'name': 'read'}]}, 'duplicate action'),
    ({'actions': [{'name': 'read', 'params': {'limit': 'integer'}}]}, 'params.limit'),
    ({'routing': {'examples': [{'text': 'read', 'action': 'write'}]}}, 'undeclared action'),
    ({'routing': {'event_only': 'false'}}, 'event_only'),
])
def test_metadata_errors_are_actionable(tmp_path, metadata, field):
    package(tmp_path, **metadata)
    errors = inspect_catalogue(tmp_path)[0].diagnostics
    assert any(field in e.message for e in errors)
    assert all(e.path.name == 'SKILL.md' for e in errors)


def test_duplicate_packages_are_both_broken_before_import(tmp_path):
    package(tmp_path, folder='a', client='raise RuntimeError("must not import")')
    package(tmp_path, folder='b')
    report = load_skill_report(tmp_path)
    assert not report.skills
    assert [e.status for e in report.entries] == ['broken', 'broken']
    assert all('Duplicate' in e.errors[0] for e in report.entries)


def test_load_states_and_failure_cleanup(tmp_path):
    package(tmp_path, folder='good', name='good')
    package(tmp_path, folder='bad', name='bad', client='raise ImportError("broken dependency")')
    package(tmp_path, folder='private', name='private', client='raise AssertionError("disabled import")')
    report = load_skill_report(tmp_path, {'good', 'bad', 'absent'})
    assert {e.name: e.status for e in report.entries} == {
        'good': 'loaded', 'bad': 'broken', 'private': 'disabled', 'absent': 'missing'}
    assert 'skill_bad_client' not in sys.modules
    assert [s.name for s in report.skills] == ['good']
    with pytest.raises(ValueError, match='missing or invalid skills'):
        report.require({'good', 'bad', 'absent'})
    report.require({'good'})


def test_missing_directory_and_metadata_only_loading(tmp_path):
    report = load_skill_report(tmp_path / 'missing', {'needed'})
    with pytest.raises(ValueError):
        report.require({'needed'})
    package(tmp_path, client='raise AssertionError("metadata only")')
    report = load_skill_report(tmp_path, {'example'}, import_clients=False)
    assert len(report.skills) == 1
    assert report.skills[0]._module is None


def test_unregistered_skill_never_falls_back_to_chat(monkeypatch):
    executor = SkillExecutor()
    chat = AsyncMock(side_effect=AssertionError('must not run chat'))
    monkeypatch.setattr(executor.owner_of('_execute_with_llm'), '_execute_with_llm', chat)
    result = asyncio.run(executor.execute(
        Skill('unknown', 'unknown', '', Path('.')), 'write something', 'U_TEST'))
    assert not result.success
    assert result.error == 'unregistered_skill_handler'
    chat.assert_not_called()


def test_registry_preserves_linear_identity_and_full_history(monkeypatch):
    executor = SkillExecutor()
    handler = AsyncMock(return_value={'message': 'denied', 'data': {'authorised': False}})
    monkeypatch.setattr(executor.owner_of('_execute_linear_channel_issues'), '_execute_linear_channel_issues', handler)
    result = asyncio.run(executor.execute(
        Skill('linear-channel-issues', '', '', Path('.')), 'read', 'U_TEST',
        channel_id='C_TEST', thread_ts='1.0', thread_history=[{'text': 'short'}],
        linear_thread_history=[], slack_team_id='T_TEST', event_id='event-1',
        current_message_ts='2.0', param_overrides={'action': 'list_issues'}))
    assert handler.call_args.kwargs == dict(text='read', params={'action': 'list_issues'},
        user_id='U_TEST', channel_id='C_TEST', thread_history=[],
        slack_team_id='T_TEST', request_id='event-1')
    assert result.data == {'authorised': False}


def test_extracted_tone_handler_preserves_prompt_and_model(monkeypatch):
    client = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content='Rewritten')))
    patch_skill_dependency(monkeypatch, 'get_llm_client', lambda provider: client)
    result = asyncio.run(SkillExecutor().execute(
        Skill('tone-of-voice', 'rewrite', 'Original style rules', Path('.')),
        'rewrite this', 'U_TEST', param_overrides={'text': 'Actual source'}))
    assert result.message == 'Rewritten'
    messages = client.chat.call_args.args[0]
    assert messages[0] == {'role': 'system', 'content': 'Original style rules'}
    assert messages[1]['content'].endswith('Actual source')
    assert client.chat.call_args.kwargs == dict(model='gpt-5.2', max_tokens=8192, reasoning_effort='high')


def test_every_current_package_has_explicit_handler_or_event_binding():
    from roo.skills.registry import EVENT_ONLY_SKILLS
    root = Path(__file__).resolve().parents[2] / 'src/skills'
    for item in inspect_catalogue(root):
        assert not item.diagnostics
        assert item.name in HANDLERS or item.name in EVENT_ONLY_SKILLS


def test_agent_requires_default_packages_and_rejects_unbound_packages(tmp_path, monkeypatch):
    from roo.agent import RooAgent
    from roo.config import Settings
    def configured(**overrides):
        return Settings(_env_file=None, SKILLS_DIR=str(tmp_path), OPENAI_API_KEY='test',
                        SLACK_BOT_TOKEN='test', SLACK_SIGNING_SECRET='test', **overrides)
    monkeypatch.setattr('roo.agent.get_settings', lambda: configured())
    with pytest.raises(ValueError, match='missing or invalid skills'):
        RooAgent()
    package(tmp_path)
    monkeypatch.setattr('roo.agent.get_settings', lambda: configured(ROO_ENABLED_SKILLS='example'))
    with pytest.raises(ValueError, match='no execution binding'):
        RooAgent()


def test_default_catalogue_loads_with_real_clients_offline(monkeypatch):
    from roo.agent import RooAgent
    from roo.config import Settings
    settings = Settings(_env_file=None, SKILLS_DIR=str(Path(__file__).resolve().parents[2] / 'src/skills'),
                        OPENAI_API_KEY='test', SLACK_BOT_TOKEN='test', SLACK_SIGNING_SECRET='test')
    monkeypatch.setattr('roo.agent.get_settings', lambda: settings)
    agent = RooAgent()
    assert {skill.name for skill in agent.skills} == settings.enabled_skill_names
    assert not [e for e in agent.skill_load_report.entries if e.status in {'missing', 'broken'}]


def test_disabled_package_never_imports_even_with_invalid_metadata(tmp_path):
    package(tmp_path, name='private', requires_auth='wrong',
            client='raise AssertionError("private code imported")')
    report = load_skill_report(tmp_path, set())
    assert report.entries[0].status == 'disabled'
    assert not report.skills
    assert inspect_catalogue(tmp_path)[0].diagnostics


def test_legacy_metadata_preserves_actions_and_routing(tmp_path):
    path = tmp_path / 'legacy.md'
    path.write_text('---\nname: legacy\ndescription: Example\nactions:\n  - name: read\nrouting:\n  use_when: Read examples\n---\nInstructions')
    skill = load_skill_report(tmp_path).skills[0]
    assert skill.action_names() == ['read']
    assert skill.routing['use_when'] == 'Read examples'


def test_unknown_field_warns_without_disabling_valid_skill(tmp_path, capsys):
    package(tmp_path, name='tone-of-voice', require_auth=True)
    assert lint(['--skills-dir', str(tmp_path)]) == 0
    assert "WARNING" in capsys.readouterr().out
    report = load_skill_report(tmp_path)
    assert report.entries[0].status == 'loaded'
    assert 'require_auth' in report.entries[0].warnings[0]


@pytest.mark.parametrize('name', sorted(set(HANDLERS) - {'studio-invoices', 'studio-hours', 'studio-client-balance'}))
def test_registry_dispatch_preserves_actor_and_rich_result(name, monkeypatch):
    executor = SkillExecutor()
    expected = '_execute_' + name.replace('-', '_')
    handler = AsyncMock(return_value={
        'message': 'review required', 'data': {'action': 'preview'},
        'blocks': [{'type': 'divider'}], 'suppress_post': True,
    })
    monkeypatch.setattr(executor.owner_of(expected), expected, handler)
    result = asyncio.run(executor.execute(
        Skill(name, '', '', Path('.')), 'request', 'U_VERIFIED',
        channel_id='C_SOURCE', thread_ts='1.0', param_overrides={'action': 'preview'}))
    handler.assert_awaited_once()
    assert 'U_VERIFIED' in handler.call_args.args or handler.call_args.kwargs.get('user_id') == 'U_VERIFIED'
    assert result.message == 'review required'
    assert result.data == {'action': 'preview'}
    assert result.blocks == [{'type': 'divider'}]
    assert result.suppress_post is True


def test_required_domain_client_is_checked_at_lint_and_load_time(tmp_path):
    path = package(tmp_path, name='linear-meeting-actions')
    assert lint(['--skills-dir', str(tmp_path)]) == 1
    report = load_skill_report(tmp_path)
    assert report.entries[0].status == 'broken'
    (path / 'client.py').write_text('NotTheClient = object')
    # Metadata lint checks file presence, never imports it to inspect classes.
    assert lint(['--skills-dir', str(tmp_path)]) == 0
    report = load_skill_report(tmp_path)
    assert report.entries[0].status == 'broken'
    assert 'LinearMeetingActionsClient' in report.entries[0].errors[0]
