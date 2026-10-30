"""Explicit task-only usage stays independent of reviewed invoice-hour sources."""
import base64
import json
from types import SimpleNamespace

import pytest

from roo.studio_report_worker import configuration
from roo.backend_identity import BackendActorContext
from roo.studio_report_commands import enqueue
from roo.studio_reports import summary, total_units
from tests.roo.test_studio_devashi_reports import devashi
from tests.roo.test_studio_client_balance_source import NOW, P1, P2, setup as finance_setup
from tests.roo.test_studio_reports import invoice_manifest, ticket
from roo.timesheets import TimesheetError


@pytest.fixture
def task_client(devashi):
    setup = devashi
    setup.clients['UDEVASHI'].update(hours_source='linear', history_start_month='2026-08')
    setup.api.collect.return_value = [
        ticket(1, project=P2, completed='2026-08-04T00:00:00Z'),
        ticket(2, project=P2, size='Medium (M)', completed='2026-09-05T00:00:00Z'),
        ticket(3, project=P2, size='Large (L)', completed='2026-10-05T00:00:00Z'),
        ticket(4, project=P1, completed='2026-08-04T00:00:00Z', title='Mark private ticket sentinel'),
        ticket(5, project=P2, completed=None, state={'id': 'started', 'type': 'started'}),
    ]
    return setup


def request(actor='UDEVASHI', **selector):
    return {'actor': actor, 'team': 'T123', 'report_kind': 'hours',
            'selector': {'month': 'all', 'action': 'detailed', **selector},
            'requested_at': NOW.isoformat()}


def forbid_invoice_reads(monkeypatch):
    import roo.studio_client_balance_source as finance
    import roo.studio_report_worker as worker

    def unexpected(*args, **kwargs):
        pytest.fail('A task-only report touched an invoice-hour source')

    monkeypatch.setattr(finance, 'load_balance_snapshot', unexpected)
    monkeypatch.setattr(worker, 'load_backfill', unexpected)


def worker_env(setup):
    return {
        'STUDIO_REPORTS_SLACK_TEAM_ID': setup.config.team,
        'TIMESHEET_LINEAR_ORGANIZATION_ID': setup.config.organization,
        'TIMESHEET_PROJECTS_JSON': json.dumps(setup.config.projects),
        'TIMESHEET_BUILDERS_JSON': json.dumps([
            {'builder_id': builder, **value} for builder, value in setup.config.builders.items()
        ]),
        'STUDIO_REPORTS_CLIENTS_JSON': json.dumps(setup.clients),
        'STUDIO_REPORTS_DATA_DIR': str(setup.config.directory),
        'STUDIO_REPORTS_QUEUE_DIR': str(setup.config.queue),
    }


@pytest.mark.parametrize('alias', ['Study Nash', 'Studynash', '[Studio] Studynash', 'Project Study Nash'])
@pytest.mark.parametrize('actor', ['UDEVASHI', 'USAM'])
def test_project_alias_from_real_route_delivers_only_the_configured_owner_tasks(task_client, monkeypatch, alias, actor):
    forbid_invoice_reads(monkeypatch)
    task_client.clients['UDEVASHI']['aliases'].extend(
        ['Study Nash', 'Studynash', '[Studio] Studynash', 'Project Study Nash'])
    configuration(worker_env(task_client))
    settings = SimpleNamespace(ROO_SURFACE='public', STUDIO_REPORTS_ENABLED=True,
        STUDIO_REPORTS_SLACK_TEAM_ID=task_client.config.team,
        STUDIO_REPORTS_QUEUE_DIR=str(task_client.config.queue))
    context = BackendActorContext('T123', actor, 'C123', '1.0', 'project-alias-request')
    result = enqueue(settings, context,
        {'month': 'current', 'action': 'summary', 'client': alias},
        user_id=actor, channel_id='C123', thread_ts='1.0', now=NOW)
    assert 'DM' in result
    outcome, = task_client.service.commands(NOW)
    assert outcome['status'] == 'sent'
    saved = task_client.service.queue.read(outcome['request'])
    report = task_client.service.requests.read(outcome['request'])
    assert saved['status'] == 'done' and saved['actor'] == actor
    assert saved['selector']['client'] == alias
    assert set(report['projects']) == {P2} and total_units(report) == 12
    assert 'studio-hours-burndown.png' in {p.get('filename') for p in report['parts']}
    assert all(call.args == (actor,) for call in task_client.api.open_dm.call_args_list)
    assert all(call.args[0] == 'D' + actor for call in task_client.api.post_message.call_args_list)
    assert all(call.args[0] == 'D' + actor for call in task_client.api.upload_csv.call_args_list)


@pytest.mark.parametrize('alias', ['Study Nash', 'Studynash', '[Studio] Studynash', 'Project Study Nash'])
def test_project_alias_cannot_grant_another_client_access(task_client, monkeypatch, alias):
    forbid_invoice_reads(monkeypatch)
    task_client.clients['UDEVASHI']['aliases'].append(alias)
    with pytest.raises(TimesheetError, match='report_client_unavailable'):
        task_client.service.report_for_request(request('UMARK', month='current', client=alias))
    task_client.api.collect.assert_not_called()
    task_client.api.open_dm.assert_not_called()


@pytest.mark.parametrize('actor,client', [
    ('UDEVASHI', None), ('USAM', 'Devarshi Desai'), ('USAM', 'Devashi Desai'),
])
def test_linear_all_time_uses_private_history_and_ignores_finance_and_backfill(task_client, monkeypatch, actor, client):
    forbid_invoice_reads(monkeypatch)
    # Even configured-but-unavailable invoice files cannot block task-only usage.
    task_client.service.backfill_path = task_client.path.parent / 'missing-invoices.json'
    task_client.service.balance_path = task_client.path.parent / 'missing-finance.json'
    req = request(actor, **({'client': client} if client else {}))
    report = task_client.service.report_for_request(req)

    assert report['selector']['month'] == '2026-08'
    assert report['selector']['months'] == 3
    assert report['selector']['project_to_date'] is True
    assert [month['units'] for month in report['monthly']] == [4, 8, 12]
    assert [month['allowance_units'] for month in report['monthly']] == [160] * 3
    assert total_units(report) == 24
    assert report['basis'] == 'completed_ticket_size_hours'
    assert report['hours_source'] == 'linear'
    assert set(report['projects']) == {P2}
    assert {row['project_id'] for row in report['rows']} == {P2}
    assert not report.get('finance_owners')
    assert 'Mark private ticket sentinel' not in json.dumps(report)
    source = task_client.api.collect.call_args.args[0]
    assert set(source.projects) == {P2}
    assert source.beginning.strftime('%Y-%m') == '2026-08'
    assert 'studio-hours-burndown.png' in {part.get('filename') for part in report['parts']}
    chart = next(part for part in report['parts'] if part.get('filename') == 'studio-hours-burndown.png')
    assert base64.b64decode(chart['content']).startswith(b'\x89PNG')

    task_client.service.deliver_request(report, req, actor + '-linear', NOW)
    assert all(call.args == (actor,) for call in task_client.api.open_dm.call_args_list)
    assert all(call.args[0] == 'D' + actor for call in task_client.api.post_message.call_args_list)
    assert all(call.args[0] == 'D' + actor for call in task_client.api.upload_csv.call_args_list)


def test_each_linear_request_reads_fresh_completed_tasks(task_client, monkeypatch):
    forbid_invoice_reads(monkeypatch)
    original = task_client.api.collect.return_value
    task_client.api.collect.side_effect = [original, [*original,
        ticket(6, project=P2, size='Extra Large (XL)', completed='2026-10-06T00:00:00Z')]]
    first = task_client.service.report_for_request(request())
    second = task_client.service.report_for_request(request())
    assert total_units(first) == 24
    assert total_units(second) == 44
    assert task_client.api.collect.call_count == 2
    assert all(set(call.args[0].projects) == {P2} for call in task_client.api.collect.call_args_list)


@pytest.mark.parametrize('change', ['policy', 'history', 'projects', 'staff_grant'])
def test_saved_linear_delivery_rechecks_current_owner_and_staff_policy(task_client, monkeypatch, change):
    forbid_invoice_reads(monkeypatch)
    req = request('USAM', client='Devashi Desai')
    report = task_client.service.report_for_request(req)
    if change == 'policy':
        task_client.clients['UDEVASHI']['hours_source'] = 'auto'
    elif change == 'history':
        task_client.clients['UDEVASHI']['history_start_month'] = '2026-07'
    elif change == 'projects':
        task_client.clients['UDEVASHI']['project_ids'] = [P1]
    else:
        task_client.clients['USAM']['report_client_ids'] = ['UMARK']
    with pytest.raises(TimesheetError, match='client_access_changed|report_client_unavailable'):
        task_client.service.deliver_request(report, req, 'stale-linear', NOW)
    task_client.api.open_dm.assert_not_called()


@pytest.mark.parametrize('actor,selector', [
    ('UUNKNOWN', {}), ('UMARK', {'client': 'Devashi Desai'}),
    ('UDEVASHI', {'client': 'Mark Ghiasy'}),
])
def test_linear_mode_does_not_grant_other_owners_or_unauthenticated_access(task_client, monkeypatch, actor, selector):
    forbid_invoice_reads(monkeypatch)
    with pytest.raises(TimesheetError):
        task_client.service.report_for_request(request(actor, **selector))
    task_client.api.collect.assert_not_called()
    task_client.api.verify_recipient.assert_not_called()
    task_client.api.open_dm.assert_not_called()


def test_mixed_staff_overview_keeps_mark_recorded_hours_and_devashi_linear_tasks(task_client):
    backfill = invoice_manifest(task_client)
    backfill['entries'][0].update(project_id=P2, start='2026-08-04', end='2026-08-04',
                                 description='Invoice overlay sentinel', replaces=['issue-1'])
    backfill['pending'] = [{'project_id': P2, 'months': ['2026-10'],
                            'reference': 'Invoice pending sentinel', 'reason': 'No work dates'}]
    path = task_client.path.parent / 'invoices.json'
    path.write_text(json.dumps(backfill))
    task_client.service.backfill_path = path
    report = task_client.service.report_for_request(request('USAM', client='all'))

    assert report['finance_owners'] == ['UMARK']
    assert report['linear_project_ids'] == [P2]
    assert total_units(report) == 129_010_000  # Mark 123.01h plus six task-size hours.
    assert sum(row['units'] for row in report['rows'] if row['project_id'] == P2) == 6_000_000
    assert {row['source'] for row in report['rows'] if row['project_id'] == P1} == {'reviewed_invoice'}
    assert set(task_client.api.collect.call_args.args[0].projects) == {P2}
    # Mark's reviewed invoice quantities retain their existing qualification;
    # removing Study Nash invoice exceptions must not turn those into verified
    # clock time or confirmed contractual entitlement.
    assert not report['complete']
    assert all(month['allowance_units'] is None for month in report['monthly'])
    assert not report['exceptions']
    assert 'Invoice overlay sentinel' not in json.dumps(report)
    assert 'Invoice pending sentinel' not in json.dumps(report)
    assert 'studio-hours-burndown.png' not in {part.get('filename') for part in report['parts']}


def test_default_mark_reporting_still_uses_its_existing_recorded_source(task_client):
    report = task_client.service.report_for_request(request('UMARK'))
    assert total_units(report) == 123_010_000
    assert report['finance_owners'] == ['UMARK']
    assert report['canonical_only']
    assert not report.get('linear_project_ids')
    task_client.api.collect.assert_not_called()


def test_staff_self_overview_invalidates_when_owner_history_policy_changes(task_client):
    req = request('USAM', month='current')
    report = task_client.service.report_for_request(req)
    task_client.clients['UDEVASHI']['history_start_month'] = '2026-07'
    with pytest.raises(TimesheetError, match='client_access_changed'):
        task_client.service.deliver_request(report, req, 'stale-staff-self', NOW)
    task_client.api.open_dm.assert_not_called()


def test_linear_all_time_without_private_history_fails_without_invoice_fallback(task_client, monkeypatch):
    forbid_invoice_reads(monkeypatch)
    del task_client.clients['UDEVASHI']['history_start_month']
    with pytest.raises(TimesheetError, match='all_time_coverage_not_configured'):
        task_client.service.report_for_request(request())
    task_client.api.collect.assert_not_called()
    task_client.api.verify_recipient.assert_not_called()
    task_client.api.open_dm.assert_not_called()


def test_partial_task_sizes_and_builders_withhold_confirmed_balance(task_client, monkeypatch):
    forbid_invoice_reads(monkeypatch)
    completed = '2026-10-05T00:00:00Z'
    task_client.api.collect.return_value = [
        ticket(1, project=P2, completed=completed),
        ticket(2, project=P2, completed=completed, labels=[]),
        ticket(3, project=P2, completed=completed, labels=[
            {'id': 'small', 'name': 'Small (S)'}, {'id': 'large', 'name': 'Large (L)'}]),
        ticket(4, project=P2, completed=completed, builder='unmapped-builder'),
    ]
    report = task_client.service.report_for_request(request(month='current'))
    assert total_units(report) == 4
    assert not report['complete']
    assert report['monthly'][0]['unresolved'] == 3
    assert {item['reason'] for item in report['exceptions']} == {
        'exactly_one_effort_label_required', 'builder_mapping_required',
    }
    assert 'hours remaining' not in summary(report)
    messages = '\n'.join(part['content'] for part in report['parts'] if part['kind'] == 'message')
    assert 'partial' in messages.lower()
    assert 'unconfirmed' in messages.lower() or 'not confirmed' in messages.lower()
    assert 'studio-hours-unresolved.csv' in {part.get('filename') for part in report['parts']}


@pytest.mark.parametrize('invalid', ['inaccessible', 'duplicate', 'history'])
def test_incomplete_task_evidence_never_returns_an_apparent_zero(task_client, monkeypatch, invalid):
    forbid_invoice_reads(monkeypatch)
    row = ticket(1, project=P2, completed='2026-10-05T00:00:00Z')
    if invalid == 'inaccessible':
        task_client.api.collect.return_value = [{**row, 'error': 'issue_unavailable'}]
    elif invalid == 'duplicate':
        task_client.api.collect.return_value = [row, row]
    else:
        row['history'] = [{'id': 'broken-move', 'createdAt': '2026-10-06T00:00:00Z',
                           'fromProjectId': P1, 'toProjectId': P1,
                           'fromProject': {'id': P1}, 'toProject': {'id': P1}}]
        task_client.api.collect.return_value = [row]
    with pytest.raises(TimesheetError, match='source_evidence_incomplete|duplicate_issue_evidence'):
        task_client.service.report_for_request(request())
    task_client.api.open_dm.assert_not_called()


def test_private_configuration_accepts_explicit_task_only_policy(task_client):
    _, clients = configuration(worker_env(task_client))
    assert clients['UDEVASHI']['hours_source'] == 'linear'
    assert clients['UDEVASHI']['history_start_month'] == '2026-08'
    assert clients['UMARK'].get('hours_source', 'auto') == 'auto'


@pytest.mark.parametrize('update', [
    {'hours_source': 'invoice-magic'}, {'hours_source': None},
    {'history_start_month': '2026-13'}, {'history_start_month': ['2026-08']},
])
def test_invalid_private_task_policy_fails_configuration(task_client, update):
    task_client.clients['UDEVASHI'].update(update)
    with pytest.raises(TimesheetError):
        configuration(worker_env(task_client))
