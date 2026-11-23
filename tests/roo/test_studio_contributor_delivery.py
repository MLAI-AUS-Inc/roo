"""On-demand contributor reports use the existing private source and delivery boundaries."""
import base64
from datetime import timedelta
import json

import pytest

from roo.payment_reminders import DeliveryNotSent
from roo.studio_reports import total_units
from roo.timesheets import TimesheetError
from tests.roo.test_studio_client_balance_source import setup as finance_setup
from tests.roo.test_studio_devashi_reports import devashi
from tests.roo.test_studio_report_linear_policy import request, task_client
from tests.roo.test_studio_reports import B2, NOW, P1, P2, P3, queue, setup, ticket


def chart_names(report):
    return [part['filename'] for part in report['parts'] if part.get('filename', '').endswith('.png')]


def test_both_charts_retry_to_requester_from_frozen_project_snapshot(setup):
    setup.api.collect.return_value = [ticket(), ticket(2, builder=B2, size='Medium (M)'),
                                      ticket(3, project=P2, title='Sibling project work')]
    queue(setup, params={'project': 'MasterApp', 'chart': 'both', 'months': 2})
    setup.api.upload_csv.side_effect = ['FSTACKED', DeliveryNotSent(), 'FCUMULATIVE']
    first, = setup.service.commands(NOW)
    assert first['status'] == 'pending'
    assert setup.service.commands(NOW + timedelta(minutes=2))[0]['status'] == 'sent'
    assert setup.api.collect.call_count == 1
    assert set(setup.api.collect.call_args.args[0].projects) == {P1}
    assert {call.args[0] for call in setup.api.upload_csv.call_args_list} == {'DUMARK'}
    report = setup.service.requests.read(first['request'])
    stacked, cumulative = chart_names(report)
    assert [call.args[1] for call in setup.api.upload_csv.call_args_list] == [stacked, cumulative, cumulative]
    assert all(base64.b64decode(p['content']).startswith(b'\x89PNG\r\n\x1a\n')
               for p in report['parts'] if p['kind'] == 'file')
    assert total_units(report) == 12 and len(report['projects']) == 1
    assert [m['allowance_units'] for m in report['monthly']] == [None, None]
    assert 'Sibling project work' not in json.dumps(report)


def test_inaccessible_project_rejected_before_any_read(setup):
    queue(setup, params={'project': P3, 'chart': 'both'})
    result, = setup.service.commands(NOW)
    assert result['reason'] == 'report_project_unavailable'
    assert setup.service.queue.read(result['request'])['status'] == 'rejected'
    setup.api.verify.assert_not_called()
    setup.api.collect.assert_not_called()
    setup.api.upload_csv.assert_not_called()
    assert setup.api.open_dm.call_args.args == ('UMARK',)
    assert setup.config.projects[P3] not in setup.api.post_message.call_args.args[1]


def test_hours_chart_notice_only_describes_work_sources(setup):
    checks = {'UMARK': {'hours': {'checked_at': NOW.isoformat()},
                        'contractor_invoices': {'unreviewed_count': 9}}}
    req = {'selector': {'project': 'Master App', 'chart': 'both'}}
    notice = setup.service.source_notice(checks, req)['content']
    assert 'Work sources checked' in notice and 'unsubmitted work is unknown' in notice
    assert 'invoice' not in notice and 'receipt' not in notice


def test_one_failed_chart_keeps_other_chart_and_totals(setup, monkeypatch):
    from roo import studio_contributor_charts

    original = studio_contributor_charts.render_contributor_chart
    def render(report, view):
        if view == 'stacked':
            raise ValueError('private renderer detail')
        return original(report, view)

    monkeypatch.setattr(studio_contributor_charts, 'render_contributor_chart', render)
    setup.api.collect.return_value = [ticket()]
    req = {'actor': 'UMARK', 'team': 'T123', 'requested_at': NOW.isoformat(), 'selector': {'chart': 'both'}}
    report = setup.service.report_for_request(req)
    assert total_units(report) == 4
    assert chart_names(report) == ['studio-hours-contributors-cumulative.png']
    messages = '\n'.join(part['content'] for part in report['parts'] if part['kind'] == 'message')
    assert 'stacked contributor chart could not be rendered' in messages
    assert 'private renderer detail' not in messages


def test_granted_staff_study_nash_chart_uses_linear_and_rechecks_before_delivery(task_client):
    req = request('USAM', client='Devashi Desai', project='Studynash', chart='cumulative')
    report = task_client.service.report_for_request(req)
    assert chart_names(report) == ['studio-hours-contributors-cumulative.png']
    assert report['basis'] == 'completed_ticket_size_hours'
    assert total_units(report) / report['unit_scale'] == 6
    assert set(task_client.api.collect.call_args.args[0].projects) == {P2}
    assert 'Mark private' not in json.dumps(report)
    task_client.clients['USAM']['report_client_ids'].remove('UDEVASHI')
    with pytest.raises(TimesheetError, match='report_client_unavailable'):
        task_client.service.deliver_request(report, req, 'stale-study-nash', NOW)
    task_client.api.open_dm.assert_not_called()


def test_narrowed_reviewed_owner_preserves_microhours_and_excludes_sibling(finance_setup):
    data = finance_setup
    data.clients['UMARK']['project_ids'].append(P2)
    data.dataset.update(hours_reporting=True, project_ids=[P1, P2])
    work = data.dataset['hours'][0]
    work['builder_id'] = next(iter(data.config.builders))
    data.dataset['hours'].append({**work, 'id': 'SIBLING', 'project_id': P2,
                                  'invoice_id': None, 'hour_units': 100000, 'description': 'Hidden sibling work'})
    data.path.write_text(json.dumps(data.value))
    req = request('USAM', client='Mark', project='Project A', chart='stacked')
    report = data.service.report_for_request(req)
    assert chart_names(report) == ['studio-hours-contributors-stacked.png']
    assert report['finance_owners'] == ['UMARK']
    assert total_units(report) == 123010000 and report['unit_scale'] == 1000000
    assert report['projects'] == {P1: 'Project A'}
    assert 'Hidden sibling work' not in json.dumps(report)
    data.api.collect.assert_not_called()
    # Changing the project selector on a saved receipt cannot reuse this report.
    req['selector']['project'] = 'Project B'
    with pytest.raises(TimesheetError, match='client_access_changed'):
        data.service.deliver_request(report, req, 'changed-project', NOW)
    data.api.open_dm.assert_not_called()
