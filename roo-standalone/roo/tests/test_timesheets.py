from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from roo.payment_reminders import DeliveryRejected, ReceiptStore
from roo.timesheets import (TimesheetConfig, TimesheetError, LABELS, PROJECTS, Ledger, artifacts,
                            build_report, empty_ledger, reconstruct, summary, timestamp)
from roo.timesheet_commands import enqueue, enqueue_event, parse_command
from roo.timesheet_linear import TimesheetAPI
from roo.timesheet_worker import ReportAPI, TimesheetService, main

USER = '00000000-0000-0000-0000-000000000001'
OTHER = '00000000-0000-0000-0000-000000000002'
ORG = '00000000-0000-0000-0000-000000000003'
PROJECT = next(iter(PROJECTS))
END = timestamp('2026-09-11T12:00:00+10:00')


@pytest.fixture
def config(tmp_path):
    return TimesheetConfig.from_env({
        'TIMESHEETS_ENABLED': 'true', 'TIMESHEET_SLACK_TEAM_ID': 'T123',
        'TIMESHEET_RECIPIENT_SLACK_ID': 'USAM', 'TIMESHEET_LINEAR_ORGANIZATION_ID': ORG,
        'TIMESHEET_BUILDERS_JSON': json.dumps([
            {'linear_user_id': USER, 'slack_user_id': 'UBUILDER', 'name': 'Builder One'},
            {'linear_user_id': OTHER, 'slack_user_id': 'UOTHER', 'name': 'Builder Two'},
        ]), 'TIMESHEET_DATA_DIR': str(tmp_path / 'data'), 'TIMESHEET_QUEUE_DIR': str(tmp_path / 'queue'),
    })


def ticket(number=1, size='Small (S)', **updates):
    return {'id': f'issue-{number}', 'identifier': f'STU-{number}', 'title': f'Build {number}',
            'url': f'https://linear.app/mlai/issue/STU-{number}/build',
            'createdAt': '2026-08-29T01:00:00Z', 'updatedAt': '2026-09-10T01:00:00Z',
            'completedAt': '2026-09-10T01:00:00Z', 'archivedAt': None, 'trashed': False,
            'assignee': {'id': USER}, 'project': {'id': PROJECT},
            'state': {'id': 'done', 'type': 'completed'},
            'labels': [{'id': 'size', 'name': size}], **updates}


def item(issue=None, history=None):
    return {'issue': issue or ticket(), 'history': history or []}


def api_for(dataset):
    api = Mock()
    api.collect.return_value = dataset
    api.open_dm.return_value = 'DSAM'
    api.post_message.return_value = '1.2'
    api.upload_csv.return_value = 'F123'
    return api


@pytest.mark.parametrize('size,expected', [('Extra Small (XS)', .25), ('Small (S)', 1),
    ('Medium (M)', 2), ('Large (L)', 3), ('Extra Large (XL)', 5)])
def test_fixed_upper_bounds(config, size, expected):
    report = build_report(config, END.astimezone(config.tz), [item(ticket(size=size))], empty_ledger())
    assert report['rows'][0]['units'] / 4 == expected
    assert report['totals'][0]['ticket_units'] / 4 == expected


@pytest.mark.parametrize('completion,count', [('2026-08-28T12:00:00+10:00', 0),
    ('2026-08-28T12:00:00.000001+10:00', 1), ('2026-09-11T12:00:00+10:00', 1),
    ('2026-09-11T12:00:00.000001+10:00', 0)])
def test_completion_boundaries(config, completion, count):
    data = item(ticket(completedAt=completion, createdAt='2026-08-01T00:00:00Z'))
    if count == 0 and timestamp(completion) > END:
        data['history'] = [{'id': 'h1', 'createdAt': completion, 'fromStateId': 'todo', 'toStateId': 'done',
                            'fromState': {'id': 'todo', 'type': 'unstarted'}, 'toState': {'id': 'done', 'type': 'completed'}}]
    report = build_report(config, END.astimezone(config.tz), [data], empty_ledger())
    assert len(report['rows']) == count


def test_calendar_dst_and_invalid_cutoffs(config):
    assert config.cutoff('2026-10-09').utcoffset() == timedelta(hours=11)
    assert config.latest(timestamp('2026-09-25T11:59:59+10:00')).date().isoformat() == '2026-09-11'
    assert config.latest(timestamp('2026-09-25T12:00:00+10:00')).date().isoformat() == '2026-09-25'
    with pytest.raises(TimesheetError):
        config.cutoff('2026-09-18')


def test_post_cutoff_changes_reversed_for_size_owner_project_and_status(config):
    old_label = {'id': 'old', 'name': 'Medium (M)'}
    data = ticket(labels=[{'id': 'new', 'name': 'Extra Large (XL)'}], assignee={'id': OTHER},
                  project={'id': 'unpaid'}, state={'id': 'todo', 'type': 'unstarted'}, completedAt=None)
    history = [
        {'id': 'h1', 'createdAt': '2026-09-10T01:00:00Z', 'toStateId': 'done', 'fromStateId': 'todo',
         'toState': {'id': 'done', 'type': 'completed'}, 'fromState': {'id': 'todo', 'type': 'unstarted'}},
        {'id': 'h2', 'createdAt': '2026-09-11T03:00:00Z',
         'fromAssigneeId': USER, 'toAssigneeId': OTHER, 'fromAssignee': {'id': USER}, 'toAssignee': {'id': OTHER},
         'fromProjectId': PROJECT, 'toProjectId': 'unpaid', 'fromProject': {'id': PROJECT}, 'toProject': {'id': 'unpaid'},
         'fromStateId': 'done', 'toStateId': 'todo', 'fromState': {'id': 'done', 'type': 'completed'},
         'toState': {'id': 'todo', 'type': 'unstarted'}, 'addedLabelIds': ['new'],
         'removedLabelIds': ['old'], 'removedLabels': [old_label]},
    ]
    report = build_report(config, END.astimezone(config.tz), [item(data, history)], empty_ledger())
    assert len(report['rows']) == 1
    row = report['rows'][0]
    assert (row['builder_id'], row['project_id'], row['size'], row['units']) == (USER, PROJECT, 'M', 8)


def test_missing_history_and_invalid_labels_are_exceptions_not_zero(config):
    bad_history = [{'id': 'h', 'createdAt': '2026-09-11T03:00:00Z', 'removedLabelIds': ['lost']}]
    dataset = [item(ticket(1, labels=[])), item(ticket(2, assignee=None)), item(ticket(3), bad_history),
               item(ticket(4, labels=[{'id': 'a', 'name': 'Small (S)'}, {'id': 'b', 'name': 'Medium (M)'}]))]
    report = build_report(config, END.astimezone(config.tz), dataset, empty_ledger())
    assert report['rows'] == []
    assert len(report['exceptions']) == 4
    assert not report['complete']
    assert 'Incomplete totals' in summary(report)
    assert 'exceptions.csv' in ' '.join(artifacts(report))


def test_unpaid_open_and_old_work_excluded_but_archived_completed_work_included(config):
    dataset = [item(ticket(1, project={'id': 'unpaid'})),
               item(ticket(2, state={'type': 'started'})),
               item(ticket(3, completedAt='2026-08-20T01:00:00Z')),
               item(ticket(4, archivedAt='2026-09-11T01:00:00Z'))]
    report = build_report(config, END.astimezone(config.tz), dataset, empty_ledger())
    assert [r['identifier'] for r in report['rows']] == ['STU-4']


def test_final_snapshot_repeat_and_reopen_never_duplicate_credit(config):
    api = api_for([item()])
    service = TimesheetService(config, api)
    first = service.report('2026-09-11', END)
    api.collect.side_effect = AssertionError('Historical report must not reread Linear')
    again = TimesheetService(config, api).report('2026-09-11', END + timedelta(days=10))
    assert first == again
    api.collect.side_effect = None
    api.collect.return_value = [item(ticket(completedAt='2026-09-24T01:00:00Z'))]
    next_report = service.report('2026-09-25', END + timedelta(days=14))
    assert next_report['rows'] == []


def test_late_and_deferred_tickets_enter_next_round(config):
    late = item(ticket(2, completedAt='2026-09-11T03:00:00Z'), [{
        'id': 'h', 'createdAt': '2026-09-11T03:00:00Z', 'fromStateId': 'todo', 'toStateId': 'done',
        'fromState': {'id': 'todo', 'type': 'unstarted'}, 'toState': {'id': 'done', 'type': 'completed'}}])
    api = api_for([item(ticket(1, labels=[])), late])
    service = TimesheetService(config, api)
    assert service.report('2026-09-11', END)['rows'] == []
    api.collect.return_value = [item(ticket(1)), late]
    report = service.report('2026-09-25', END + timedelta(days=14))
    assert {r['identifier'] for r in report['rows']} == {'STU-1', 'STU-2'}


def test_preview_simulates_missing_periods_without_allocating_or_delivering(config):
    api = api_for([item(), item(ticket(2, completedAt='2026-09-14T00:00:00Z'))])
    report = TimesheetService(config, api).report('current', END + timedelta(days=4))
    assert report['draft']
    assert report['period'] == '2026-09-25'
    assert [r['identifier'] for r in report['rows']] == ['STU-2']
    assert not (config.directory / 'ledger.json').exists()
    api.post_message.assert_not_called()
    api.upload_csv.assert_not_called()
    historical = TimesheetService(config, api).report('2026-09-11', END, preview=True)
    assert len(historical['rows']) == 1
    assert not (config.directory / 'ledger.json').exists()


def test_ledger_write_failure_prevents_delivery_and_replay_recovers(config, monkeypatch):
    service = TimesheetService(config, api_for([item()]))
    original = service.ledger.save
    monkeypatch.setattr(service.ledger, 'save', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        service.report('latest', END)
    service.api.post_message.assert_not_called()
    monkeypatch.setattr(service.ledger, 'save', original)
    assert len(service.report('latest', END)['rows']) == 1


def test_partial_delivery_retry_preserves_confirmed_summary_and_files(config):
    api = api_for([item()])
    service = TimesheetService(config, api)
    report = service.report('latest', END)
    api.upload_csv.side_effect = ['F1', DeliveryRejected('ratelimited', 120)]
    assert service.deliver(report, 'USAM', 'test', END) == 'pending'
    assert service.deliver(report, 'USAM', 'test', END + timedelta(seconds=60)) == 'pending'
    api.upload_csv.side_effect = ['F2']
    assert TimesheetService(config, api).deliver(report, 'USAM', 'test', END + timedelta(seconds=120)) == 'sent'
    api.post_message.assert_called_once()
    assert api.upload_csv.call_count == 3


def test_ambiguous_delivery_never_retries_automatically(config):
    api = api_for([item()])
    service = TimesheetService(config, api)
    report = service.report('latest', END)
    api.upload_csv.side_effect = httpx.ReadTimeout('private response must not leak')
    with pytest.raises(httpx.ReadTimeout):
        service.deliver(report, 'USAM', 'test', END)
    assert TimesheetService(config, api).deliver(report, 'USAM', 'test', END) == 'needs_review'
    api.upload_csv.assert_called_once()


def test_delivery_rechecks_recipient_and_private_channel(config):
    api = api_for([item()])
    service = TimesheetService(config, api)
    report = service.report('latest', END)
    with pytest.raises(TimesheetError, match='not_authorized'):
        service.deliver(report, 'USTRANGER', 'test', END)
    api.open_dm.return_value = 'CPUBLIC'
    with pytest.raises(TimesheetError, match='private_destination'):
        service.deliver(report, 'USAM', 'test', END)
    api.post_message.assert_not_called()
    api.upload_csv.assert_not_called()


def test_csv_formula_escaping_and_totals(config):
    issue = ticket(title=' =HYPERLINK("https://bad")\nprivate, text')
    report = build_report(config, END.astimezone(config.tz), [item(issue)], empty_ledger())
    import csv
    content = next(v for k, v in artifacts(report).items() if k.endswith('-tickets.csv'))
    rows = list(csv.DictReader(io.StringIO(content)))
    assert rows[0]['title'].startswith("' =")
    assert rows[0]['size_based_hours'] == '1.00'


def settings(config):
    return SimpleNamespace(ROO_SURFACE='public', TIMESHEET_COMMANDS_ENABLED=True,
        TIMESHEET_SLACK_TEAM_ID='T123', TIMESHEET_SLACK_BOT_USER_ID='UBOT',
        TIMESHEET_RECIPIENT_SLACK_ID='USAM', TIMESHEET_QUEUE_DIR=str(config.queue),
        TIMESHEET_FIRST_CUTOFF='2026-09-11')


@pytest.mark.parametrize('text,dm,expected', [('timesheet', True, 'latest'),
    ('timesheet current', True, 'current'), ('<@UBOT> timesheet 2026-09-11', False, '2026-09-11'),
    ('<@OTHER> timesheet', False, None), ('please explain timesheets', True, None)])
def test_exact_command_parser(text, dm, expected):
    assert parse_command(text, 'UBOT', dm=dm) == expected


def test_request_queue_duplicate_and_worker_replay(config):
    params = dict(team='T123', actor='USAM', channel='DSAM', source_id='123.456', text='timesheet', dm=True, now=END)
    assert enqueue(settings(config), **params)['new']
    assert not enqueue(settings(config), **params)['new']
    api = api_for([item()])
    service = TimesheetService(config, api)
    assert service.commands(END)[0]['status'] == 'sent'
    assert service.commands(END) == []
    api.post_message.assert_called_once()
    assert api.upload_csv.call_count == 2


def test_unauthorized_and_wrong_workspace_requests_never_reach_queue(config):
    for team, actor in [('TOTHER', 'USAM'), ('T123', 'USTRANGER')]:
        result = enqueue(settings(config), team=team, actor=actor, channel='DSAM', source_id='123.456',
                         text='timesheet', dm=True, now=END)
        assert result['handled']
    assert not config.queue.exists()


def test_worker_independently_rejects_revoked_requester(config):
    enqueue(settings(config), team='T123', actor='USAM', channel='DSAM', source_id='123.456', text='timesheet', dm=True, now=END)
    service = TimesheetService(replace(config, recipient='UOTHER'), api_for([item()]))
    service.commands(END)
    service.api.collect.assert_not_called()
    service.api.post_message.assert_not_called()
    assert json.loads(next(config.queue.glob('*.json')).read_text())['status'] == 'denied'


def test_bot_and_edited_messages_are_ignored(config):
    for extra in ({'bot_id': 'B123'}, {'subtype': 'message_changed'}):
        result = enqueue_event(settings(config), {'team_id': 'T123', 'event': {
            'type': 'message', 'channel_type': 'im', 'channel': 'DSAM', 'user': 'USAM',
            'text': 'timesheet', 'ts': '1.2', **extra}}, END)
        assert not result['handled']


def test_correction_is_idempotent_and_does_not_change_finalized_report(config):
    api = api_for([item()])
    service = TimesheetService(config, api)
    first = service.report('latest', END)
    adjustment = {'id': 'adjust1', 'period': '2026-09-11', 'issue_id': 'issue-1',
                  'builder_id': USER, 'units': -1, 'reason': 'Correct agreed effort'}
    service.correction(adjustment, END + timedelta(days=1))
    service.correction(adjustment, END + timedelta(days=1))
    assert service.report('2026-09-11', END + timedelta(days=2)) == first
    report = service.report('2026-09-25', END + timedelta(days=14))
    assert len(report['rows']) == 1
    assert report['rows'][0]['kind'] == 'correction'
    assert report['rows'][0]['units'] == -1
    import csv
    csv_rows = list(csv.DictReader(io.StringIO(next(v for k, v in artifacts(report).items() if k.endswith('-tickets.csv')))))
    assert csv_rows[0]['size_based_hours'] == '-0.25'
    assert service.report('2026-10-09', END + timedelta(days=28))['rows'] == []


def test_source_failure_does_not_finalize_empty_report(config):
    api = api_for([])
    api.collect.side_effect = ValueError('bad source')
    with pytest.raises(ValueError):
        TimesheetService(config, api).report('latest', END)
    assert not (config.directory / 'ledger.json').exists()


def test_csv_upload_uses_private_channel_and_keeps_token_off_upload_host():
    requests = []
    def handler(req):
        requests.append(req)
        if req.url.path.endswith('files.getUploadURLExternal'):
            return httpx.Response(200, json={'ok': True, 'file_id': 'F123', 'upload_url': 'https://files.slack.com/upload/v1/secret'})
        if req.url.host == 'files.slack.com':
            assert 'Authorization' not in req.headers
            assert req.content == b'name,hours\nBuilder,1.00\n'
            return httpx.Response(200, text='OK')
        assert json.loads(req.content)['channel_id'] == 'DSAM'
        return httpx.Response(200, json={'ok': True, 'files': [{'id': 'F123'}]})
    api = ReportAPI(httpx.Client(transport=httpx.MockTransport(handler)), linear_key='test', slack_token='private-token')
    assert api.upload_csv('DSAM', 'timesheet.csv', 'name,hours\nBuilder,1.00\n') == 'F123'
    assert len(requests) == 3


@pytest.mark.parametrize('url', ['https://evil.test/upload/v1/x', 'http://files.slack.com/upload/x',
                                'https://files.slack.com:444/upload/x', 'https://files.slack.com.evil/upload/x'])
def test_upload_capability_validation(url):
    def handler(req):
        assert req.url.host == 'slack.com'
        return httpx.Response(200, json={'ok': True, 'file_id': 'F123', 'upload_url': url})
    api = ReportAPI(httpx.Client(transport=httpx.MockTransport(handler)), linear_key='test', slack_token='test')
    with pytest.raises(TimesheetError):
        api.upload_csv('DSAM', 'timesheet.csv', 'test')


def test_disabled_worker_makes_no_calls(monkeypatch, capsys):
    monkeypatch.setenv('TIMESHEETS_ENABLED', 'false')
    assert main(['--once']) == 0
    assert json.loads(capsys.readouterr().out) == {'status': 'disabled'}


def test_backlog_never_applies_correction_before_it_was_recorded(config):
    service = TimesheetService(config, api_for([item()]))
    service.report('latest', END)
    service.correction({'id': 'adjust', 'period': '2026-09-11', 'issue_id': 'issue-1',
        'builder_id': USER, 'units': -1, 'reason': 'Agreed correction'}, END + timedelta(days=20))
    assert service.report('2026-09-25', END + timedelta(days=21))['rows'] == []
    assert service.ledger.load()['corrections'][0]['period'] is None
    report = service.report('2026-10-09', END + timedelta(days=28))
    assert [r['kind'] for r in report['rows']] == ['correction']
    assert service.ledger.load()['corrections'][0]['period'] == '2026-10-09'


def test_missing_reverse_transition_is_an_exception(config):
    history = [{'id': 'h', 'createdAt': '2026-09-12T00:00:00Z', 'fromAssigneeId': USER,
                'toAssigneeId': OTHER, 'fromAssignee': {'id': USER}, 'toAssignee': {'id': OTHER}}]
    report = build_report(config, config.cutoff(config.first), [item(ticket(), history)], empty_ledger())
    assert report['rows'] == []
    assert report['exceptions'][0]['reason'] == 'inconsistent_history_chain'


def test_worker_source_outage_notifies_privately_once_then_recovers(config):
    enqueue(settings(config), team='T123', actor='USAM', channel='C123', source_id='123.456', text='timesheet', dm=True, now=END)
    api = api_for([item()])
    api.collect.side_effect = httpx.ReadTimeout('sensitive provider response')
    service = TimesheetService(config, api)
    assert service.commands(END)[0]['status'] == 'error'
    service.commands(END + timedelta(seconds=60))
    api.post_message.assert_called_once()
    assert api.post_message.call_args.args[0] == 'DSAM'
    assert 'sensitive' not in api.post_message.call_args.args[1]
    assert not (config.directory / 'ledger.json').exists()
    api.collect.side_effect = None
    assert service.commands(END + timedelta(seconds=120))[0]['status'] == 'sent'
    queued = json.loads(next(config.queue.glob('*.json')).read_text())
    assert 'report' not in queued
    assert (config.directory / 'requests').exists()


def test_queue_snapshot_failure_after_finalization_does_not_repeat_allocation(config, monkeypatch):
    enqueue(settings(config), team='T123', actor='USAM', channel='DSAM', source_id='123.456', text='timesheet', dm=True, now=END)
    api = api_for([item()])
    service = TimesheetService(config, api)
    original = service.requests.write
    monkeypatch.setattr(service.requests, 'write', Mock(side_effect=OSError('disk full')))
    service.commands(END)
    assert service.ledger.load()['allocated'] == {'issue-1': '2026-09-11'}
    api.upload_csv.assert_not_called()
    monkeypatch.setattr(service.requests, 'write', original)
    assert service.commands(END + timedelta(seconds=60))[0]['status'] == 'sent'
    api.collect.assert_called_once()
    assert api.upload_csv.call_count == 2


def test_corrupt_queue_entry_does_not_block_valid_request(config):
    enqueue(settings(config), team='T123', actor='USAM', channel='DSAM', source_id='123.456', text='timesheet', dm=True, now=END)
    (config.queue / ('0' * 64 + '.json')).write_text('{broken')
    results = TimesheetService(config, api_for([item()])).commands(END)
    assert [r['status'] for r in results] == ['error', 'sent']


def test_wrong_ledger_workspace_cannot_reuse_saved_report(config):
    api = api_for([item()])
    TimesheetService(config, api).report('latest', END)
    with pytest.raises(TimesheetError, match='ledger_identity_mismatch'):
        TimesheetService(replace(config, team='TOTHER'), api).report('latest', END)


def test_failure_after_external_send_requires_review(config, monkeypatch):
    api = api_for([item()])
    service = TimesheetService(config, api)
    report = service.report('latest', END)
    original = service.deliveries.write
    def fail_after_send(key, receipt):
        if any(p['status'] == 'sent' for p in receipt['parts']):
            raise OSError('disk full')
        original(key, receipt)
    monkeypatch.setattr(service.deliveries, 'write', fail_after_send)
    with pytest.raises(OSError):
        service.deliver(report, 'USAM', 'crash', END)
    assert TimesheetService(config, api).deliver(report, 'USAM', 'crash', END) == 'needs_review'
    api.post_message.assert_called_once()
    api.upload_csv.assert_not_called()


def test_scheduled_catchup_and_restart_delivers_each_period_once_to_sam(config):
    config = replace(config, scheduled=True)
    api = api_for([item()])
    service = TimesheetService(config, api)
    assert service.scheduled(END - timedelta(seconds=1)) == []
    api.collect.assert_not_called()
    result = service.scheduled(END + timedelta(days=14))
    assert result == [{'period': '2026-09-11', 'status': 'sent'}, {'period': '2026-09-25', 'status': 'sent'}]
    assert api.post_message.call_count == 2
    assert api.upload_csv.call_count == 4
    assert {call.args[0] for call in api.open_dm.call_args_list} == {'USAM'}
    TimesheetService(config, api).scheduled(END + timedelta(days=15))
    assert api.post_message.call_count == 2
    assert api.upload_csv.call_count == 4


def test_schedule_is_opt_in(config):
    api = api_for([item()])
    assert TimesheetService(config, api).scheduled(END) == []
    api.collect.assert_not_called()


def test_local_status_needs_no_api_credentials(config, monkeypatch, capsys):
    for name, value in {'TIMESHEETS_ENABLED': 'true', 'TIMESHEET_SLACK_TEAM_ID': config.team,
            'TIMESHEET_RECIPIENT_SLACK_ID': config.recipient, 'TIMESHEET_ALLOWED_SLACK_IDS': config.recipient,
            'TIMESHEET_LINEAR_ORGANIZATION_ID': config.organization, 'TIMESHEET_DATA_DIR': str(config.directory),
            'TIMESHEET_QUEUE_DIR': str(config.queue), 'TIMESHEET_LINEAR_READ_API_KEY': '',
            'TIMESHEET_SLACK_BOT_TOKEN': ''}.items():
        monkeypatch.setenv(name, value)
    assert main(['--status']) == 0
    assert json.loads(capsys.readouterr().out)['allocated_tickets'] == 0


def test_legacy_requester_configuration_cannot_add_production_users():
    cfg = TimesheetConfig.from_env({'TIMESHEET_RECIPIENT_SLACK_ID': 'USAM',
                                   'TIMESHEET_ALLOWED_SLACK_IDS': 'USAM,UALAN'})
    assert cfg.allowed_users == ('USAM',)


def test_worker_rejects_forged_alan_request_before_reading_source(config):
    cfg = replace(config, allowed_users=('USAM', 'UALAN'))
    enqueue(settings(cfg), team='T123', actor='USAM', channel='C123', source_id='123.456',
            text='<@UBOT> timesheet', dm=False, now=END)
    path = next(cfg.queue.glob('*.json'))
    request = json.loads(path.read_text())
    request['actor'] = 'UALAN'
    path.write_text(json.dumps(request))
    api = api_for([item()])
    service = TimesheetService(cfg, api)
    assert service.commands(END) == []
    assert json.loads(path.read_text())['status'] == 'denied'
    api.collect.assert_not_called()
    api.open_dm.assert_not_called()
    api.post_message.assert_not_called()


def test_delivery_cannot_redirect_to_extra_legacy_requester(config):
    api = api_for([item()])
    service = TimesheetService(replace(config, allowed_users=('USAM', 'UALAN')), api)
    report = service.report('latest', END)
    with pytest.raises(TimesheetError, match='not_authorized'):
        service.deliver(report, 'UALAN', 'redirect', END)
    api.open_dm.assert_not_called()
    api.upload_csv.assert_not_called()
