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
from roo.studio_report_backfill import load_backfill, validate_backfill

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


def invoice_manifest(setup, **updates):
    value = {'version': 1, 'team': setup.config.team, 'organization': setup.config.organization,
        'sources': {'alice-001': {'invoice': 'Invoice 001', 'message_id': 'private-mail-id',
            'sha256': 'a' * 64, 'builder_id': B1, 'hours': '4'}},
        'entries': [{'id': 'alice-001:1', 'line_id': '1', 'source_id': 'alice-001',
            'project_id': P1, 'builder_id': B1, 'start': '2026-08-20', 'end': '2026-08-21',
            'hours': '2.5', 'description': 'Historical work', 'review_note': 'Matched invoice line to ticket',
            'replaces': ['issue-1']}], 'pending': [], **updates}
    return value


def test_invoice_replaces_estimate_across_months_and_preserves_work_period(setup):
    backfill = validate_backfill(invoice_manifest(setup), setup.config)
    data = [ticket(1, size='Extra Large (XL)'), ticket(2)]
    result = build_client_report(setup.config, setup.clients['UMARK'],
        {'month': 'recent', 'months': 3, 'action': 'detailed'}, data, NOW, backfill)
    assert [m['units'] for m in result['monthly']] == [0, 10, 4]
    assert '2.5h from reviewed invoices and 1h from completed-ticket estimates' in summary(result)
    assert '2026-08-20 – 2026-08-21 · invoice' in '\n'.join(detail_messages(result))
    csv = artifacts(result)['studio-hours-work.csv']
    assert 'reviewed_invoice,2026-08-20,2026-08-21' in csv
    assert 'private-mail-id' not in json.dumps(result) and 'a' * 64 not in json.dumps(result)
    # September-only excludes the August invoice and its September ticket copy.
    sept = build_client_report(setup.config, setup.clients['UMARK'], {}, data, NOW, backfill)
    assert sept['monthly'][0]['units'] == 4


def test_invoice_can_resolve_missing_effort_without_bypassing_ownership(setup):
    value = invoice_manifest(setup)
    backfill = validate_backfill(value, setup.config)
    result = build_client_report(setup.config, setup.clients['UMARK'], {'months': 3},
        [historical_ticket_with_deleted_label()], NOW, backfill)
    assert result['complete'] and sum(m['units'] for m in result['monthly']) == 10
    with pytest.raises(TimesheetError, match='invoice_ticket_scope_mismatch'):
        build_client_report(setup.config, setup.clients['UMARK'], {'months': 3},
            [ticket(builder=B2)], NOW, backfill)


def test_invoice_rows_pending_and_sources_are_scoped_to_owned_projects(setup):
    value = invoice_manifest(setup)
    value['entries'][0].update(project_id=P3, description='Other client private work', replaces=[])
    value['pending'] = [{'project_id': P3, 'months': ['2026-09'],
                         'reference': 'Secret invoice', 'reason': 'Secret work dates missing'}]
    backfill = validate_backfill(value, setup.config)
    result = build_client_report(setup.config, setup.clients['UMARK'], {'months': 3}, [], NOW, backfill)
    assert result['complete'] and not result['rows'] and 'Secret' not in json.dumps(result)
    assert 'Other client private' not in json.dumps(result)


@pytest.mark.parametrize('mutation', ['duplicate', 'duplicate_source', 'wrong_org', 'bad_project',
    'bad_builder', 'cross_month', 'negative', 'nan', 'fraction', 'over_invoice', 'no_review', 'bad_hash'])
def test_invalid_invoice_manifest_is_rejected_in_full(setup, mutation):
    from copy import deepcopy
    value = invoice_manifest(setup)
    entry = value['entries'][0]
    if mutation == 'duplicate': value['entries'].append(deepcopy(entry))
    elif mutation == 'duplicate_source': value['sources']['copy'] = deepcopy(value['sources']['alice-001'])
    elif mutation == 'wrong_org': value['organization'] = 'another-org'
    elif mutation == 'bad_project': entry['project_id'] = 'unknown'
    elif mutation == 'bad_builder': entry['builder_id'] = B2
    elif mutation == 'cross_month': entry['end'] = '2026-09-01'
    elif mutation == 'negative': entry['hours'] = '-1'
    elif mutation == 'nan': entry['hours'] = 'NaN'
    elif mutation == 'fraction': entry['hours'] = '0.333'
    elif mutation == 'over_invoice': entry['hours'] = '5'
    elif mutation == 'no_review': entry['review_note'] = ''
    elif mutation == 'bad_hash': value['sources']['alice-001']['sha256'] = 'missing'
    with pytest.raises(TimesheetError, match='invalid_invoice_backfill'):
        validate_backfill(value, setup.config)


def test_unresolved_invoice_dates_prevent_false_remaining_hours(setup):
    value = invoice_manifest(setup)
    value['pending'] = [{'project_id': P1, 'months': ['2026-08', '2026-09'],
                        'reference': 'Invoice 002', 'reason': 'Monthly split needs review'}]
    result = build_client_report(setup.config, setup.clients['UMARK'], {'months': 3}, [], NOW,
                                  validate_backfill(value, setup.config))
    assert not result['complete'] and [m['unresolved'] for m in result['monthly']] == [0, 1, 1]
    assert "Remaining hours aren't confirmed" in summary(result)
    assert 'Invoice 002' in artifacts(result)['studio-hours-unresolved.csv']


def test_missing_or_changed_backfill_blocks_delivery_and_unknown_actor_reads_nothing(setup, tmp_path):
    path = tmp_path / 'invoices.json'
    with pytest.raises(TimesheetError, match='invoice_backfill_unavailable'):
        load_backfill(path, setup.config)
    path.write_text(json.dumps(invoice_manifest(setup)))
    service = StudioReportService(setup.config, setup.api, setup.clients, path)
    request = {'team': 'T123', 'actor': 'UMARK', 'selector': {'months': 3}, 'requested_at': NOW.isoformat()}
    result = service.report_for_request(request)
    value = invoice_manifest(setup); value['entries'][0]['hours'] = '3'
    path.write_text(json.dumps(value))
    with pytest.raises(TimesheetError, match='invoice_backfill_changed'):
        service.deliver_request(result, request, 'test', NOW)
    terminal, notice = service.failure_notice({'error': 'invoice_backfill_changed'})
    assert terminal and 'request a new report' in notice
    setup.api.post_message.assert_not_called()
    path.unlink()
    with pytest.raises(TimesheetError, match='client_access_not_configured'):
        service.report_for_request({**request, 'actor': 'UUNKNOWN'})


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


@pytest.mark.parametrize('months', [1, 3])
def test_staff_overview_has_no_combined_budget_in_text_or_chart(setup, months, monkeypatch):
    from matplotlib.figure import Figure

    setup.clients['UMARK']['monthly_hours'] = None
    result = report(setup, [ticket(), ticket(2, labels=[])],
                    month='2026-07' if months == 3 else '2026-09', months=months)
    text = summary(result)
    assert 'September 2026 — 1 hours used' in text
    assert 'Partial total: 1 completed work items need review.' in text
    assert 'remaining' not in text.lower() and 'of 40' not in text
    assert 'no combined monthly allowance' in text
    labels = []
    original = Figure.text

    def capture(figure, x, y, text, **kwargs):
        labels.append(text)
        return original(figure, x, y, text, **kwargs)

    monkeypatch.setattr(Figure, 'text', capture)
    assert render_chart(result).startswith(b'\x89PNG')
    assert 'allowance' not in ' '.join(labels) and 'of 40' not in ' '.join(labels)


def test_overview_monthly_overrides_handle_budgeted_and_unbudgeted_months(setup):
    setup.clients['UMARK'].update(monthly_hours=None, monthly_allowances={'2026-08': 20})
    result = report(setup, [], month='2026-07', months=3)
    assert [m['allowance_units'] for m in result['monthly']] == [None, 80, None]
    text = summary(result)
    assert 'July 2026 — 0 hours used' in text
    assert 'August 2026 — 0 of 20 hours used' in text
    assert 'September 2026 — 0 hours used' in text
    assert render_chart(result).startswith(b'\x89PNG')
    # Explicit dated nulls can also remove an otherwise configured allowance.
    setup.clients['UMARK'].update(monthly_hours=40, monthly_allowances={'2026-09': None})
    assert report(setup, [])['monthly'][0]['allowance_units'] is None


def test_staff_access_does_not_widen_client_scope_or_invalidate_client_reports(setup):
    before = setup.service.scope_fingerprint('UMARK')
    setup.env['STUDIO_REPORTS_CLIENTS_JSON'] = json.dumps({**setup.clients,
        'USTAFF': {'name': 'Staff', 'project_ids': [P1, P2, P3], 'monthly_hours': None}})
    config, clients = configuration(setup.env)
    service = StudioReportService(config, setup.api, clients)
    setup.api.collect.return_value = [ticket(), ticket(2, project=P3, title='Other client work')]
    request = {'team': 'T123', 'selector': {}, 'requested_at': NOW.isoformat()}
    overview = service.report_for_request({**request, 'actor': 'USTAFF'})
    assert set(setup.api.collect.call_args.args[0].projects) == {P1, P2, P3}
    assert overview['monthly'][0]['units'] == 8
    assert overview['monthly'][0]['allowance_units'] is None
    client = service.report_for_request({**request, 'actor': 'UMARK'})
    assert set(setup.api.collect.call_args.args[0].projects) == {P1, P2}
    assert client['monthly'][0]['units'] == 4
    assert client['monthly'][0]['allowance_units'] == 160
    assert 'Other client work' not in json.dumps(client)
    assert service.scope_fingerprint('UMARK') == before
    # Staff access is still an explicit grant and revocation invalidates snapshots.
    clients['USTAFF']['project_ids'].remove(P3)
    with pytest.raises(TimesheetError, match='client_access_changed'):
        service.deliver_request(overview, {**request, 'actor': 'USTAFF'}, 'old', NOW)
    setup.api.post_message.assert_not_called()


@pytest.mark.parametrize('value', [0, -1, 'invalid', 'NaN', 0.1, False])
def test_invalid_budget_does_not_silently_become_staff_overview(setup, value):
    setup.clients['UMARK']['monthly_hours'] = value
    setup.env['STUDIO_REPORTS_CLIENTS_JSON'] = json.dumps(setup.clients)
    with pytest.raises(TimesheetError, match='invalid_monthly_allowance'):
        configuration(setup.env)


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


def historical_ticket_with_deleted_label():
    item = ticket(completed='2026-07-10T00:00:00Z')
    item['history'] = [{'id': 'deleted-label', 'createdAt': '2026-08-10T00:00:00Z',
                        'removedLabelIds': ['old-effort'], 'removedLabels': []}]
    return item


def test_deleted_historical_labels_are_scoped_partial_items_without_guessed_hours(setup):
    result = report(setup, [historical_ticket_with_deleted_label(), ticket(2)], month='recent', months=3)
    assert [month['units'] for month in result['monthly']] == [0, 0, 4]
    assert [month['unresolved'] for month in result['monthly']] == [1, 0, 0]
    assert result['exceptions'] == [{'month':'2026-07', 'project':'Master App', 'identifier':'STU-1',
                                     'reason':'historical_effort_labels_unavailable'}]
    assert not result['complete'] and 'partial total' in summary(result)
    assert 'historical_effort_labels_unavailable' in artifacts(result)['studio-hours-unresolved.csv']
    assert [row['identifier'] for row in result['rows']] == ['STU-2']


def test_deleted_labels_do_not_expose_another_projects_historical_work(setup):
    item = historical_ticket_with_deleted_label()
    # The ticket moved into Mark's project after it was completed elsewhere.
    item['history'].append({'id':'move', 'createdAt':'2026-08-01T00:00:00Z',
                           'fromProjectId':P3, 'toProjectId':P1,
                           'fromProject':{'id':P3}, 'toProject':{'id':P1}})
    result = report(setup, [item], month='recent', months=3)
    assert result['rows'] == result['exceptions'] == []
    assert 'STU-1' not in json.dumps(result)


@pytest.mark.parametrize('from_project,to_project', [(None,P1), ({'id':P3},P2)])
def test_deleted_labels_still_block_when_project_history_cannot_be_verified(setup, from_project, to_project):
    item = historical_ticket_with_deleted_label()
    item['history'].append({'id':'move', 'createdAt':'2026-08-01T00:00:00Z',
                           'fromProjectId':P3, 'toProjectId':to_project,
                           'fromProject':from_project, 'toProject':{'id':to_project}})
    with pytest.raises(TimesheetError, match='source_evidence_incomplete'):
        report(setup, [item], month='recent', months=3)


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


@pytest.mark.parametrize('now,mode,start,end', [
    ('2026-09-22T04:00:00Z', 'recent', '2026-07', '2026-10'),
    ('2026-09-22T04:00:00Z', 'last_complete', '2026-06', '2026-09'),
    ('2026-08-31T15:00:00Z', 'recent', '2026-07', '2026-10'),
    ('2026-01-01T00:00:00Z', 'recent', '2025-11', '2026-02'),
    ('2026-01-01T00:00:00Z', 'last_complete', '2025-10', '2026-01'),
])
def test_three_month_periods_use_melbourne_calendar_and_cross_years(now, mode, start, end):
    selector, beginning, ending = resolve_period({'month': mode, 'months': 3}, timestamp(now))
    assert selector == {'month': start, 'months': 3, 'action': 'summary'}
    assert beginning.strftime('%Y-%m') == start and ending.strftime('%Y-%m') == end
    assert beginning.hour == ending.hour == 0


def test_month_count_without_start_defaults_to_recent_period_and_detail_reuses_it(setup):
    queue(setup, params={'months': 3})
    queue(setup, event='detail', params={'action': 'detailed'}, now=NOW + timedelta(seconds=1))
    requests = [setup.service.queue.read(path.stem) for path in setup.config.queue.glob('*.json')]
    assert {item['selector']['month'] for item in requests} == {'2026-07'}
    assert {item['selector']['months'] for item in requests} == {3}
    assert {item['selector']['action'] for item in requests} == {'summary', 'detailed'}


def test_three_month_summary_is_compact_with_reconciled_project_and_builder_totals(setup):
    setup.clients['UMARK']['monthly_hours'] = None
    result = report(setup, [ticket(1, size='Large (L)', completed='2026-07-10T00:00:00Z'),
                            ticket(2, size='Medium (M)', completed='2026-08-10T00:00:00Z'),
                            ticket(3, project=P2, builder=B2), ticket(4, project=P3, title='Private')],
                    month='recent', months=3)
    text = summary(result)
    assert 'July 2026 – September 2026' in text
    assert '6 hours used across 3 months' in text
    assert 'July 2026 — 3 hours used' in text and 'August 2026 — 2 hours used' in text
    assert 'September 2026 — 1 hours used* · month to date' in text
    assert text.count('Master App:') == 1 and 'Master App: 5h' in text
    assert text.count('Cybertest:') == 1 and 'Cybertest: 1h' in text
    assert 'Alice 5h' in text and 'Bob 1h' in text
    assert 'Private' not in text and 'Work 1' not in text
    assert 'studio-hours-work.csv' not in artifacts(result)


def test_three_month_client_allowances_remain_separate_and_partial_total_is_clear(setup):
    result = report(setup, [ticket(1), ticket(2, labels=[])], month='recent', months=3)
    text = summary(result)
    assert '1 hours used across 3 months* · partial total' in text
    assert text.count('of 40 hours used') == 3
    assert '120' not in text and 'no rollover' in text
    assert "Remaining hours aren't confirmed" in text
    assert 'studio-hours-unresolved.csv' in artifacts(result)


def test_monthly_chart_marks_only_current_month_to_date(setup, monkeypatch):
    from matplotlib.axes import Axes

    labels = []
    original = Axes.bar
    def capture(axes, x, *args, **kwargs):
        labels.extend(x)
        return original(axes, x, *args, **kwargs)
    monkeypatch.setattr(Axes, 'bar', capture)
    assert render_chart(report(setup, [], month='recent', months=3)).startswith(b'\x89PNG')
    assert labels == ['Jul 2026', 'Aug 2026', 'Sep 2026\n(to date)']
    labels.clear()
    assert render_chart(report(setup, [], month='last_complete', months=3)).startswith(b'\x89PNG')
    assert labels == ['Jun 2026', 'Jul 2026', 'Aug 2026']


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


def invoice_first_manifest(setup):
    value = invoice_manifest(setup, version=2,
        coverage={'start': '2026-05-01', 'through': '2026-09-22'},
        invoice_first_projects=[P1], qualifications=[
            {'project_id': P1, 'reference': 'Invoice 001', 'reason': 'Includes approved billing hour-units.'}])
    value['sources']['alice-001']['hours'] = '12.83'
    value['entries'][0].update(hours='12.83', start=None, end=None,
        date_status='unallocated', date_note='Source has no confirmed monthly split.')
    return value


def test_invoice_first_exact_hundredths_unallocated_and_no_duplicate_estimates(setup):
    value = validate_backfill(invoice_first_manifest(setup), setup.config)
    result = build_client_report(setup.config, setup.clients['UMARK'],
        {'month': '2026-05', 'months': 5, 'action': 'detailed'},
        [ticket(1), ticket(2, size='Extra Large (XL)'), ticket(3, project=P2)], NOW, value)
    assert result['unit_scale'] == 100
    assert result['unallocated_units'] == 1283
    assert sum(m['units'] for m in result['monthly']) == 100  # Other project still uses its estimate.
    assert sum(r['units'] for r in result['rows']) == 1383
    assert all(m['allowance_units'] == 4000 for m in result['monthly'])
    text = summary(result)
    assert '13.83 hours used' in text and '12.83h included' in text
    assert 'Master App: 12.83h' in text and 'Cybertest: 1h' in text
    assert 'hours remaining' not in text
    assert 'Includes approved billing hour-units.' in text
    csv = artifacts(result)['studio-hours-work.csv']
    assert '12.83,reviewed_invoice,,,Source has no confirmed monthly split.' in csv
    assert 'Work month unconfirmed' in '\n'.join(detail_messages(result))
    assert 'private-mail-id' not in json.dumps(result)
    assert 'ticket_not_added_to_invoice_total' in artifacts(result)['studio-hours-unresolved.csv']
    narrow = build_client_report(setup.config, setup.clients['UMARK'], {'months': 3}, [], NOW, value)
    assert not narrow['rows'] and not narrow['complete']
    assert any('Excluded from this subtotal' in e['reason'] for e in narrow['exceptions'])


def test_cross_month_recorded_work_is_counted_only_for_containing_period(setup):
    value = invoice_first_manifest(setup)
    source = value['sources']['alice-001']
    source.update(kind='recorded_time', reference='LOG-123', hours='8.66')
    source.pop('invoice')
    value['entries'][0].update(date_status='work_period', start='2026-08-28', end='2026-09-03', hours='8.66')
    value = validate_backfill(value, setup.config)
    full = build_client_report(setup.config, setup.clients['UMARK'], {'months': 2}, [], NOW, value)
    assert full['unallocated_units'] == 866 and len(full['rows']) == 1
    assert '8.66h from additional recorded work' in summary(full)
    assert not sum(m['units'] for m in full['monthly'])
    for month in ['2026-08', '2026-09']:
        narrow = build_client_report(setup.config, setup.clients['UMARK'], {'month': month}, [], NOW, value)
        assert not narrow['rows'] and narrow['monthly'][0]['unresolved'] == 1


def test_project_to_date_worker_resolves_private_coverage_and_keeps_recipient(setup, tmp_path):
    path = tmp_path / 'invoice.json'
    path.write_text(json.dumps(invoice_first_manifest(setup)))
    service = StudioReportService(setup.config, setup.api, setup.clients, path)
    result = service.report_for_request({'team': 'T123', 'actor': 'UMARK',
        'selector': {'month': 'all'}, 'requested_at': NOW.isoformat()})
    assert result['selector'] == {'month': '2026-05', 'months': 5, 'action': 'summary', 'project_to_date': True}
    assert [m['month'] for m in result['monthly']] == ['2026-05', '2026-06', '2026-07', '2026-08', '2026-09']
    assert setup.api.collect.call_args.args[0].beginning.strftime('%Y-%m') == '2026-05'
    setup.api.verify_recipient.assert_called_once_with('UMARK', 'T123')
    setup.api.post_message.assert_not_called()
    queue(setup, params={'month': 'all'})
    queue(setup, event='Ev2', params={'action': 'detailed'}, now=NOW + timedelta(seconds=1))
    requests = [json.loads(p.read_text()) for p in setup.config.queue.glob('*.json')]
    assert all(r['selector']['month'] == 'all' for r in requests)
    with pytest.raises(TimesheetError, match='all_time_coverage_not_configured'):
        setup.service.report_for_request({'team':'T123','actor':'UMARK','selector':{'month':'all'},'requested_at':NOW.isoformat()})


def test_project_to_date_can_cover_more_than_one_year(setup):
    value = invoice_first_manifest(setup)
    value['coverage']['start'] = '2025-05-01'
    result = build_client_report(setup.config, setup.clients['UMARK'], {'month': 'all'}, [], NOW,
                                 validate_backfill(value, setup.config))
    assert len(result['monthly']) == 17 and result['unallocated_units'] == 1283
    # A resolved private selector remains valid when building the report again.
    assert resolve_period(result['selector'], NOW)[0]['months'] == 17


@pytest.mark.parametrize('mutation', ['precision', 'unknown_policy', 'duplicate_policy', 'missing_note',
    'undated_has_date', 'bad_coverage', 'secret_qualification', 'source_duplicate', 'source_overcount'])
def test_version_two_manifest_rejects_invalid_accounting_and_scope(setup, mutation):
    from copy import deepcopy
    value = invoice_first_manifest(setup)
    entry = value['entries'][0]
    if mutation == 'precision': entry['hours'] = '1.001'
    elif mutation == 'unknown_policy': value['invoice_first_projects'] = ['unknown']
    elif mutation == 'duplicate_policy': value['invoice_first_projects'] = [P1, P1]
    elif mutation == 'missing_note': entry.pop('date_note')
    elif mutation == 'undated_has_date': entry['start'] = '2026-05-01'
    elif mutation == 'bad_coverage': value['coverage']['start'] = '2027-01-01'
    elif mutation == 'secret_qualification': value['qualifications'][0]['project_id'] = P3
    elif mutation == 'source_duplicate': value['sources']['copy'] = deepcopy(value['sources']['alice-001'])
    elif mutation == 'source_overcount': entry['hours'] = '12.84'
    with pytest.raises(TimesheetError, match='invalid_invoice_backfill'):
        validate_backfill(value, setup.config)


def test_invoice_first_qualifications_and_undated_rows_never_cross_project_access(setup):
    value = invoice_first_manifest(setup)
    value['invoice_first_projects'] = [P3]
    value['entries'][0].update(project_id=P3, replaces=[])
    value['qualifications'][0].update(project_id=P3, reason='Another client secret')
    result = build_client_report(setup.config, setup.clients['UMARK'], {'months': 5}, [], NOW,
                                 validate_backfill(value, setup.config))
    assert not result['rows'] and not result['qualifications'] and result['complete']
    assert 'Another client secret' not in json.dumps(result)


def test_charts_preserve_exact_period_and_monthly_reconciliation(setup, monkeypatch):
    from matplotlib.axes import Axes
    calls = {}
    real_bar, real_barh = Axes.bar, Axes.barh
    def bar(self, x, height, *args, **kwargs):
        calls['monthly'] = (x, height)
        return real_bar(self, x, height, *args, **kwargs)
    def barh(self, y, width, *args, **kwargs):
        calls['projects'] = width
        return real_barh(self, y, width, *args, **kwargs)
    monkeypatch.setattr(Axes, 'bar', bar)
    monkeypatch.setattr(Axes, 'barh', barh)
    value = validate_backfill(invoice_first_manifest(setup), setup.config)
    result = build_client_report(setup.config, setup.clients['UMARK'], {'month':'all'}, [], NOW, value)
    assert render_chart(result).startswith(b'\x89PNG')
    labels, values = calls['monthly']
    assert labels[-1] == 'Month\nunallocated' and sum(values) == 12.83
    assert render_chart(result, breakdown='projects').startswith(b'\x89PNG')
    assert calls['projects'] == [12.83, 0]


def test_reviewed_cutoff_preserves_later_live_work_with_separate_basis(setup):
    value = validate_backfill(invoice_first_manifest(setup), setup.config)
    later = timestamp('2026-10-10T04:00:00Z')
    data = [ticket(2, completed='2026-10-01T04:00:00Z')]
    result = build_client_report(setup.config, setup.clients['UMARK'], {'month':'all'}, data, later, value)
    assert sum(r['units'] for r in result['rows']) == 1383
    assert result['monthly'][-1]['units'] == 100
    assert result['basis'] == 'reviewed_invoices_and_completed_ticket_sizes'
    assert '1h from completed-ticket estimates' in summary(result)
