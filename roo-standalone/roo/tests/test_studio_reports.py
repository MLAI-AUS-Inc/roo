"""Synthetic source and Slack only: client isolation, accounting and delivery."""
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock
import base64

import httpx

import pytest

from roo.backend_identity import BackendActorContext
from roo.studio_report_commands import enqueue
from roo.studio_report_worker import StudioReportAPI, StudioReportService, configuration
from roo.studio_reports import (artifacts, build_client_report, detail_messages, render_chart,
                                resolve_period, summary)
from roo.timesheets import TimesheetError, timestamp

P1 = '00000000-0000-0000-0000-000000000001'
P2 = '00000000-0000-0000-0000-000000000002'
P3 = '00000000-0000-0000-0000-000000000003'
B1 = '00000000-0000-0000-0000-000000000011'
B2 = '00000000-0000-0000-0000-000000000012'
NOW = timestamp('2026-09-22T04:00:00Z')


@pytest.fixture
def setup(tmp_path):
    env = {
        'STUDIO_REPORTS_SLACK_TEAM_ID': 'T123',
        'TIMESHEET_LINEAR_ORGANIZATION_ID': '00000000-0000-0000-0000-000000000020',
        'TIMESHEET_PROJECTS_JSON': json.dumps({P1: 'Master App', P2: 'Cybertest', P3: 'Other client secret'}),
        'TIMESHEET_BUILDERS_JSON': json.dumps([
            {'builder_id': B1, 'slack_user_id': 'UALICE', 'name': 'Alice'},
            {'builder_id': B2, 'slack_user_id': 'UBOB', 'name': 'Bob'}]),
        'STUDIO_REPORTS_CLIENTS_JSON': json.dumps({
            'UMARK': {'name': 'Mark', 'project_ids': [P1, P2]},
            'UOTHER': {'name': 'Other', 'project_ids': [P3]},
        }),
        'STUDIO_REPORTS_DATA_DIR': str(tmp_path / 'data'),
        'STUDIO_REPORTS_QUEUE_DIR': str(tmp_path / 'queue'),
    }
    config, clients = configuration(env)
    settings = SimpleNamespace(ROO_SURFACE='public', STUDIO_REPORTS_ENABLED=True,
                               STUDIO_REPORTS_SLACK_TEAM_ID=config.team, STUDIO_REPORTS_QUEUE_DIR=str(config.queue))
    api = Mock()
    api.collect.return_value = []
    api.open_dm.side_effect = lambda user: 'D' + user
    api.post_message.return_value = '1.2'
    api.upload_csv.return_value = 'F123'
    service = StudioReportService(config, api, clients)
    return SimpleNamespace(config=config, clients=clients, settings=settings, service=service, api=api, env=env)


def ticket(number=1, *, project=P1, builder=B1, size='Small (S)', completed='2026-09-10T01:00:00Z', **updates):
    issue = {'id': f'issue-{number}', 'identifier': f'STU-{number}', 'title': f'Work {number}',
             'url': f'https://linear.app/mlai/issue/STU-{number}',
             'createdAt': '2026-01-01T00:00:00Z', 'updatedAt': completed or NOW.isoformat(),
             'completedAt': completed, 'state': {'id': 'done', 'type': 'completed'},
             'project': {'id': project}, 'assignee': {'id': builder},
             'labels': [{'id': 'size', 'name': size}], **updates}
    return {'issue': issue, 'history': []}


def report(setup, data, **selector):
    return build_client_report(setup.config, setup.clients['UMARK'], selector, data, NOW)


def queue(setup, *, actor='UMARK', event='Ev1', params=None, now=NOW):
    context = BackendActorContext('T123', actor, 'C123', '1.0', event)
    return enqueue(setup.settings, context, params or {}, user_id=actor,
                   channel_id='C123', thread_ts='1.0', now=now)


def test_multi_project_summary_reconciles_and_never_multiplies_allowance(setup):
    result = report(setup, [ticket(1, size='Extra Large (XL)'), ticket(2, project=P2, builder=B2, size='Large (L)'),
                            ticket(3, project=P2, size='Extra Small (XS)'), ticket(4, project=P3, title='Secret work')])
    assert result['monthly'] == [{'month': '2026-09', 'units': 33, 'allowance_units': 160, 'unresolved': 0}]
    text = summary(result)
    assert '8.25 of 40 hours used' in text and '31.75 hours remaining' in text
    assert 'Master App: 5h' in text and 'Cybertest: 3.25h' in text
    assert 'Bob 3h' in text and 'Alice 0.25h' in text
    assert 'Secret' not in json.dumps(result)
    assert 'in progress' in text and 'clocked time' in text


def test_zero_projects_and_overage_are_clear(setup):
    empty = report(setup, [])
    assert '0 of 40 hours used' in summary(empty)
    assert 'Master App: 0h' in summary(empty) and 'Cybertest: 0h' in summary(empty)
    over = report(setup, [ticket(n, size='Extra Large (XL)') for n in range(9)])
    assert '5 hours over your allowance' in summary(over)
    assert 'hours remaining' not in summary(over)


@pytest.mark.parametrize('completed,month', [
    ('2026-08-31T13:59:59Z', '2026-08'), ('2026-08-31T14:00:00Z', '2026-09'),
    ('2026-09-30T13:59:59Z', '2026-09'), ('2026-09-30T14:00:00Z', '2026-10'),
    ('2026-10-31T12:59:59Z', '2026-10'), ('2026-10-31T13:00:00Z', '2026-11'),
])
def test_melbourne_half_open_month_boundaries_and_dst(setup, completed, month):
    result = build_client_report(setup.config, setup.clients['UMARK'], {'month': '2026-08', 'months': 4},
                                 [ticket(completed=completed)], timestamp('2026-11-15T00:00:00Z'))
    assert result['rows'][0]['month'] == month
    assert sum(item['units'] for item in result['monthly']) == 4


def test_monthly_history_has_separate_allowances_and_zero_months(setup):
    setup.clients['UMARK']['monthly_allowances'] = {'2026-08': 20}
    result = report(setup, [ticket(completed='2026-08-14T00:00:00Z')], month='2026-07', months=3)
    assert [m['units'] for m in result['monthly']] == [0, 4, 0]
    assert [m['allowance_units'] for m in result['monthly']] == [160, 80, 160]


def test_reopened_ticket_uses_first_completion_and_historical_project_size_builder(setup):
    data = ticket(project=P3, builder=B2, size='Extra Large (XL)', completed='2026-09-20T00:00:00Z')
    data['history'] = [
        {'id': 'first', 'createdAt': '2026-08-20T00:00:00Z', 'fromStateId': 'todo', 'toStateId': 'done',
         'fromState': {'id': 'todo', 'type': 'unstarted'}, 'toState': {'id': 'done', 'type': 'completed'}},
        {'id': 'reopen', 'createdAt': '2026-09-01T00:00:00Z', 'fromStateId': 'done', 'toStateId': 'todo',
         'fromState': {'id': 'done', 'type': 'completed'}, 'toState': {'id': 'todo', 'type': 'unstarted'},
         'fromProjectId': P1, 'toProjectId': P3, 'fromProject': {'id': P1}, 'toProject': {'id': P3},
         'fromAssigneeId': B1, 'toAssigneeId': B2, 'fromAssignee': {'id': B1}, 'toAssignee': {'id': B2},
         'addedLabelIds': ['size'], 'removedLabelIds': ['old'], 'removedLabels': [{'id': 'old', 'name': 'Small (S)'}]},
        {'id': 'second', 'createdAt': '2026-09-20T00:00:00Z', 'fromStateId': 'todo', 'toStateId': 'done',
         'fromState': {'id': 'todo', 'type': 'unstarted'}, 'toState': {'id': 'done', 'type': 'completed'}},
    ]
    result = report(setup, [data], month='2026-08', months=2)
    assert [m['units'] for m in result['monthly']] == [4, 0]
    assert result['rows'][0]['builder'] == 'Alice'
    assert result['rows'][0]['project'] == 'Master App'
    # Moving another client's completed work into an owned project grants no history access.
    data['issue']['project'] = {'id': P1}
    data['history'][1].update(fromProjectId=P3, toProjectId=P1, fromProject={'id': P3}, toProject={'id': P1})
    assert report(setup, [data], month='2026-08', months=2)['rows'] == []


def test_missing_size_or_builder_is_partial_not_false_remaining(setup):
    result = report(setup, [ticket(1), ticket(2, labels=[]), ticket(3, builder='unknown')])
    assert result['monthly'][0]['units'] == 4
    assert len(result['exceptions']) == 2
    assert 'Partial total' in summary(result)
    assert 'hours remaining' not in summary(result)
    assert 'studio-hours-unresolved.csv' in artifacts(result)


def test_source_errors_missing_history_and_duplicates_cannot_report_zero(setup):
    for data in ([{**ticket(), 'error': 'issue_unavailable'}],
                 [ticket(completed=None)], [ticket(), ticket()]):
        with pytest.raises(TimesheetError):
            report(setup, data)


def test_open_and_future_work_do_not_count(setup):
    result = report(setup, [ticket(1, completed=None, state={'type': 'started'}),
                            ticket(2, completed='2026-09-23T00:00:00Z')])
    assert result['rows'] == []


def test_detailed_work_and_csv_are_complete_and_safe(setup):
    result = report(setup, [ticket(1, title='=WEBSERVICE("secret")'), ticket(2, title='<!channel> & <@UOTHER>')], action='detailed')
    text = '\n'.join(detail_messages(result))
    assert '2026-09-10 · Alice · *1h*' in text
    assert '<!channel>' not in text and '&lt;@UOTHER&gt;' in text
    content = artifacts(result)['studio-hours-work.csv']
    assert "'=WEBSERVICE" in content
    assert len(content.splitlines()) == 3
    assert 'full ticket' in text


@pytest.mark.parametrize('params', [{'month': '2026-13'}, {'month': '2026-10'}, {'months': 13},
                                     {'months': True}, {'months': 0}, {'month': '../private'}, {'action': 'pay'}])
def test_invalid_periods_are_rejected(params):
    with pytest.raises(TimesheetError):
        resolve_period(params, NOW)


def test_relative_month_uses_local_calendar():
    params, _, _ = resolve_period({'month': 'last'}, timestamp('2026-08-31T15:00:00Z'))
    assert params['month'] == '2026-08'


def test_queue_identity_cannot_be_overridden_and_retries_deduplicate(setup):
    assert 'DM' in queue(setup, params={'actor': 'UOTHER', 'project_ids': [P3], 'monthly_hours': 999})
    assert 'already' in queue(setup)
    assert len(list(setup.config.queue.glob('*.json'))) == 1
    request = setup.service.queue.read(next(setup.config.queue.glob('*.json')).stem)
    assert request['actor'] == 'UMARK'
    assert set(request['selector']) == {'month', 'months', 'action'}
    context = BackendActorContext('T123', 'UOTHER', 'C123', '1.0', 'evil')
    assert 'could not verify' in enqueue(setup.settings, context, {}, user_id='UMARK', channel_id='C123', thread_ts='1.0')
    setup.settings.ROO_SURFACE = 'admin'
    assert 'not enabled' in queue(setup, event='admin')


def test_detail_inherits_only_same_clients_period_and_explicit_month_wins(setup):
    queue(setup, params={'month': '2026-07', 'months': 3})
    queue(setup, actor='UOTHER', event='other', params={'month': '2026-06'}, now=NOW + timedelta(seconds=1))
    queue(setup, event='detail', params={'action': 'detailed'}, now=NOW + timedelta(seconds=2))
    requests = [setup.service.queue.read(path.stem) for path in setup.config.queue.glob('*.json')]
    detail = next(item for item in requests if item['selector']['action'] == 'detailed')
    assert detail['selector'] == {'action': 'detailed', 'month': '2026-07', 'months': 3}
    queue(setup, event='explicit', params={'action': 'detailed', 'month': 'last'}, now=NOW + timedelta(seconds=3))
    requests = [setup.service.queue.read(path.stem) for path in setup.config.queue.glob('*.json')]
    assert any(item['selector'] == {'action': 'detailed', 'month': '2026-08', 'months': 1} for item in requests)


def test_worker_reads_fresh_scoped_source_and_delivers_privately_once(setup):
    setup.api.collect.return_value = [ticket(), ticket(2, project=P3, title='Secret')]
    queue(setup)
    assert setup.service.commands(NOW)[0]['status'] == 'sent'
    source, at, deferred = setup.api.collect.call_args.args
    assert set(source.projects) == {P1, P2}
    assert source.beginning.isoformat() == '2026-09-01T00:00:00+10:00'
    assert at == NOW
    assert setup.api.open_dm.call_args.args == ('UMARK',)
    assert all(call.args[0] == 'DUMARK' for call in setup.api.post_message.call_args_list)
    assert 'Secret' not in str(setup.api.post_message.call_args_list)
    assert setup.service.commands(NOW) == []
    setup.api.collect.assert_called_once()
    assert not (setup.config.directory / 'ledger.json').exists()
    queue(setup, event='fresh', now=NOW + timedelta(minutes=1))
    setup.service.commands(NOW + timedelta(minutes=1))
    assert setup.api.collect.call_count == 2


def test_unknown_client_gets_private_setup_notice_without_reading_source(setup):
    queue(setup, actor='UUNKNOWN')
    setup.service.commands(NOW)
    setup.api.collect.assert_not_called()
    assert setup.api.open_dm.call_args.args == ('UUNKNOWN',)
    assert 'project access' in setup.api.post_message.call_args.args[1]
    request = setup.service.queue.read(next(setup.config.queue.glob('*.json')).stem)
    assert request['status'] == 'rejected'


def test_source_outage_never_delivers_a_zero_report(setup):
    setup.api.collect.side_effect = RuntimeError('private provider detail')
    queue(setup)
    result = setup.service.commands(NOW)
    assert result[0]['status'] == 'error'
    assert 'private provider detail' not in str(result)
    assert 'not been counted as zero' in setup.api.post_message.call_args.args[1]
    setup.api.upload_csv.assert_not_called()


def test_revoked_access_blocks_saved_report(setup):
    request = {'actor': 'UMARK', 'team': 'T123', 'selector': {}, 'requested_at': NOW.isoformat()}
    snapshot = setup.service.report_for_request(request)
    setup.clients['UMARK']['project_ids'] = [P2]
    with pytest.raises(TimesheetError, match='client_access_changed'):
        setup.service.deliver_request(snapshot, request, 'old', NOW)
    setup.api.post_message.assert_not_called()
    del setup.clients['UMARK']
    with pytest.raises(TimesheetError, match='client_access_not_configured'):
        setup.service.deliver_request(snapshot, request, 'old', NOW)


def test_uncertain_delivery_not_retried_and_source_snapshot_preserved(setup):
    setup.api.collect.return_value = [ticket()]
    setup.api.upload_csv.side_effect = TimeoutError()
    queue(setup)
    setup.service.commands(NOW)
    setup.service.commands(NOW + timedelta(minutes=2))
    assert setup.api.upload_csv.call_count == 1
    assert setup.api.collect.call_count == 1
    assert len(setup.api.post_message.call_args_list) == 2  # summary + one durable failure notice


def test_chart_render_failure_keeps_report_usable(setup, monkeypatch):
    monkeypatch.setattr('roo.studio_report_worker.render_chart', Mock(side_effect=RuntimeError()))
    queue(setup)
    assert setup.service.commands(NOW)[0]['status'] == 'sent'
    assert 'chart is unavailable' in setup.api.post_message.call_args_list[-1].args[1]


@pytest.mark.parametrize('selector', [{}, {'month': '2026-07', 'months': 3}])
def test_chart_is_real_png(setup, selector):
    result = report(setup, [ticket()], **selector)
    assert render_chart(result).startswith(b'\x89PNG\r\n\x1a\n')


def test_config_rejects_unknown_projects_bad_allowances_and_unverified_source(setup):
    for client in ({'name': 'Mark', 'project_ids': ['unknown']},
                   {'name': 'Mark', 'project_ids': [P1, P1]},
                   {'name': 'Mark', 'project_ids': [P1], 'monthly_hours': 'NaN'},
                   {'name': 'Mark', 'project_ids': [P1], 'monthly_hours': 0}):
        with pytest.raises(TimesheetError):
            configuration({**setup.env, 'STUDIO_REPORTS_CLIENTS_JSON': json.dumps({'UMARK': client})})
    with pytest.raises(TimesheetError):
        configuration({**setup.env, 'TIMESHEET_SOURCE': 'plane'})


def test_png_upload_uses_binary_and_private_destination_without_forwarding_token():
    png = b'\x89PNG\r\n\x1a\nsynthetic'
    calls = []
    def transport(request):
        calls.append(request)
        if request.url.path == '/api/files.getUploadURLExternal':
            assert request.url.params['length'] == str(len(png))
            return httpx.Response(200, json={'ok': True, 'upload_url': 'https://files.slack.com/upload/test', 'file_id': 'F123'})
        if request.url.path == '/upload/test':
            assert request.content == png
            assert 'Authorization' not in request.headers
            return httpx.Response(200)
        assert request.url.path == '/api/files.completeUploadExternal'
        assert json.loads(request.content)['channel_id'] == 'DMARK'
        return httpx.Response(200, json={'ok': True, 'files': [{'id': 'F123'}]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        api = StudioReportAPI(client, linear_key='synthetic', slack_token='synthetic')
        encoded = base64.b64encode(png).decode('ascii')
        assert api.upload_csv('DMARK', 'studio-hours.png', encoded) == 'F123'
        with pytest.raises(TimesheetError, match='private_destination_required'):
            api.upload_csv('CPUBLIC', 'studio-hours.png', encoded)
    assert len(calls) == 3
