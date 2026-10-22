"""Friday charts share verified ownership, one source read and a frozen cutoff."""
import base64
from datetime import timedelta
import json
from unittest.mock import Mock

import pytest

from roo.payment_reminders import DeliveryNotSent
from roo.studio_report_schedule import enqueue_weekly, weekly_configuration
from roo.studio_reports import render_chart, resolve_period, total_units
from tests.roo.test_studio_reports import setup, ticket, P1, P2, P3
from tests.roo.test_studio_client_balance_source import setup as finance_setup
from roo.timesheets import TimesheetError, timestamp

FRIDAY = timestamp('2026-10-16T17:00:00+11:00')


def scheduled(setup, actor='UMARK', now=FRIDAY, time='17:00'):
    setup.clients['USAM'] = {'name': 'Sam', 'project_ids': [P1, P2, P3],
                             'report_client_ids': ['UMARK'], 'monthly_hours': None}
    schedules = weekly_configuration({'STUDIO_REPORTS_WEEKLY_ENABLED': 'true',
        'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': json.dumps([
            {'id': 'friday-' + actor, 'actor': actor, 'client': 'UMARK', 'time': time,
             'starts_on': now.date().isoformat()}])},
        setup.clients)
    setup.service.schedules = schedules
    event = enqueue_weekly(setup.service, schedules, now)[0]
    return event['request'], setup.service.weekly_queue.read(event['request'])


def activity():
    return [ticket(1, size='Small (S)', completed='2026-10-02T01:00:00Z'),
            ticket(2, project=P2, size='Extra Large (XL)', completed='2026-10-07T01:00:00Z'),
            ticket(3, project=P2, size='Large (L)', completed='2026-10-09T01:00:00Z'),
            ticket(4, size='Medium (M)', completed='2026-10-13T01:00:00Z'),
            ticket(5, size='Large (L)', completed='2026-10-15T01:00:00Z'),
            ticket(6, completed=FRIDAY.isoformat()),
            ticket(7, project=P3, completed='2026-10-14T01:00:00Z', title='Other client private work'),
            ticket(8, completed='2026-09-30T01:00:00Z'),
            ticket(9, completed='2026-10-17T01:00:00Z')]


@pytest.mark.parametrize('actor', ['UMARK', 'USAM'])
def test_both_period_charts_are_real_pngs_and_deliver_only_to_authorised_actor(setup, actor):
    setup.api.collect.return_value = activity()
    key, request = scheduled(setup, actor)
    assert setup.service.commands(FRIDAY) == [{'request': key, 'status': 'sent'}]
    report = setup.service.requests.read(key)
    month = report['month_to_date']
    assert total_units(report) == 20 and total_units(month) == 56
    assert month['selector']['start'] == '2026-10-01T00:00:00+10:00'
    assert month['selector']['end'] == report['selector']['end']
    assert month['selector']['client'] == report['selector']['client'] == 'UMARK'
    assert set(month['projects']) == set(report['projects']) == {P1, P2}
    assert setup.api.collect.call_count == 1
    assert setup.api.collect.call_args.args[0].beginning == timestamp('2026-10-01T00:00:00+10:00')
    assert 'Other client private work' not in json.dumps(report)
    pngs = [part for part in report['parts'] if part.get('filename', '').endswith('.png')]
    assert [part['filename'] for part in pngs] == ['studio-hours-week.png', 'studio-hours-month-to-date.png']
    assert all(base64.b64decode(part['content']).startswith(b'\x89PNG\r\n\x1a\n') for part in pngs)
    assert {call.args[0] for call in setup.api.upload_csv.call_args_list} == {'D' + actor}
    assert {call.args[1] for call in setup.api.upload_csv.call_args_list} == {part['filename'] for part in pngs}
    assert setup.service.commands(FRIDAY + timedelta(minutes=1)) == []
    assert setup.api.upload_csv.call_count == 2


def test_project_colours_match_even_when_week_and_month_project_order_differs(setup, monkeypatch):
    from matplotlib.axes import Axes
    captures = []
    original = Axes.barh

    def capture(axes, y, width, *args, **kwargs):
        captures.append((axes, list(width), list(kwargs['color'])))
        return original(axes, y, width, *args, **kwargs)

    monkeypatch.setattr(Axes, 'barh', capture)
    setup.api.collect.return_value = activity()
    _, request = scheduled(setup)
    setup.service.report_for_request(request)
    charts = []
    for axes, values, colours in captures:
        charts.append({label.get_text(): (value, colour)
                       for label, value, colour in zip(axes.get_yticklabels(), values, colours)})
    assert len(charts) == 2
    assert charts[0]['Master App'][0] == 5 and charts[0]['Cybertest'][0] == 0
    assert charts[1]['Master App'][0] == 6 and charts[1]['Cybertest'][0] == 8
    assert {name: pair[1] for name, pair in charts[0].items()} == {name: pair[1] for name, pair in charts[1].items()}


@pytest.mark.parametrize('cutoff,weekly_start,month_start', [
    ('2026-10-02T17:00:00+10:00', '2026-09-28T00:00:00+10:00', '2026-10-01T00:00:00+10:00'),
    ('2027-01-01T17:00:00+11:00', '2026-12-28T00:00:00+11:00', '2027-01-01T00:00:00+11:00'),
])
def test_first_friday_of_month_keeps_prior_month_work_only_in_week_chart(setup, cutoff, weekly_start, month_start):
    due = timestamp(cutoff)
    setup.api.collect.return_value = [ticket(1, completed=(timestamp(weekly_start) + timedelta(days=1)).isoformat()),
                                     ticket(2, size='Medium (M)', completed=(timestamp(month_start) + timedelta(hours=1)).isoformat())]
    _, request = scheduled(setup, now=due)
    report = setup.service.report_for_request(request)
    assert total_units(report) == 12 and total_units(report['month_to_date']) == 8
    assert report['month_to_date']['selector']['start'] == month_start
    assert setup.api.collect.call_args.args[0].beginning == timestamp(weekly_start)
    assert {row['identifier'] for row in report['month_to_date']['rows']} == {'STU-2'}


def test_monthly_chart_failure_cannot_mark_friday_report_complete(setup, monkeypatch):
    setup.api.collect.return_value = activity()
    key, _ = scheduled(setup)
    monkeypatch.setattr('roo.studio_report_worker.render_chart', Mock(side_effect=[b'png', RuntimeError('PRIVATE_RENDER_FAILURE')]))
    result = setup.service.commands(FRIDAY)
    assert result[0]['reason'] == 'scheduled_report_chart_unavailable'
    assert setup.service.requests.read(key) is None
    assert setup.service.weekly_queue.read(key)['status'] == 'error'
    assert 'PRIVATE_RENDER_FAILURE' not in json.dumps(setup.service.weekly_queue.read(key))
    setup.api.upload_csv.assert_not_called()
    monkeypatch.setattr('roo.studio_report_worker.render_chart', render_chart)
    assert setup.service.commands(FRIDAY + timedelta(minutes=2))[0]['status'] == 'sent'
    assert setup.api.upload_csv.call_count == 2


def test_retry_sends_only_unsent_month_chart_using_same_frozen_source(setup):
    setup.api.collect.return_value = activity()
    key, _ = scheduled(setup)
    setup.api.upload_csv.side_effect = ['FWEEK', DeliveryNotSent(), 'FMONTH']
    assert setup.service.commands(FRIDAY)[0]['status'] == 'pending'
    assert setup.service.commands(FRIDAY + timedelta(minutes=2))[0]['status'] == 'sent'
    assert setup.api.collect.call_count == 1
    assert [call.args[1] for call in setup.api.upload_csv.call_args_list] == [
        'studio-hours-week.png', 'studio-hours-month-to-date.png', 'studio-hours-month-to-date.png']
    report = setup.service.requests.read(key)
    assert report['month_to_date']['selector']['end'] == report['selector']['end']


@pytest.mark.parametrize('historical', [False, True])
def test_week_without_source_coverage_still_has_labelled_chart_and_valid_month_data(finance_setup, monkeypatch, historical):
    import roo.studio_report_worker as worker
    class Clock:
        @classmethod
        def now(cls, tz):
            return (FRIDAY + timedelta(days=10 if historical else 0)).astimezone(tz)
    monkeypatch.setattr(worker, 'datetime', Clock)
    finance_setup.dataset.update(hours_reporting=True)
    finance_setup.dataset['hours'][0].update(work_start='2026-10-08', work_end='2026-10-09', hour_units=5_000_000,
                                           builder_id=next(iter(finance_setup.config.builders)))
    finance_setup.dataset['contractor_invoices'][0]['hour_units'] = 5_000_000
    finance_setup.path.write_text(json.dumps(finance_setup.value))
    schedules = weekly_configuration({'STUDIO_REPORTS_WEEKLY_ENABLED': 'true',
        'STUDIO_REPORTS_WEEKLY_SCHEDULES_JSON': json.dumps([
            {'id': 'mark-friday', 'actor': 'UMARK', 'client': 'UMARK', 'starts_on': '2026-10-16'}])}, finance_setup.clients)
    finance_setup.service.schedules = schedules
    finance_setup.service.allow_historical_snapshot = historical
    key = enqueue_weekly(finance_setup.service, schedules, FRIDAY)[0]['request']
    request = finance_setup.service.weekly_queue.read(key)
    report = finance_setup.service.report_for_request(request)
    assert not report['period_has_coverage'] and report['month_to_date']['period_has_coverage']
    assert total_units(report['month_to_date']) == 5_000_000
    files = [part for part in report['parts'] if part.get('filename', '').endswith('.png')]
    assert len(files) == 2
    assert all(base64.b64decode(part['content']).startswith(b'\x89PNG') for part in files)
    text = '\n'.join(part['content'] for part in report['parts'] if part['kind'] == 'message')
    assert 'Weekly hours unavailable' in text and '5 invoice/recorded hour-units counted' in text
    assert report['source_stale'] is historical and report['month_to_date']['source_stale'] is historical
    assert ('Historical snapshot' in text) is historical
    assert 'payments' not in json.dumps(report) and 'BANK1' not in json.dumps(report)
    finance_setup.api.collect.assert_not_called()


def test_midnight_first_friday_still_sends_week_chart_and_empty_new_month_chart(setup):
    due = timestamp('2026-05-01T00:00:00+10:00')
    setup.api.collect.return_value = [ticket(completed='2026-04-28T01:00:00Z')]
    _, request = scheduled(setup, now=due, time='00:00')
    report = setup.service.report_for_request(request)
    month = report['month_to_date']
    assert total_units(report) == 4 and total_units(month) == 0
    assert month['selector']['start'] == month['selector']['end']
    assert len([part for part in report['parts'] if part.get('filename', '').endswith('.png')]) == 2
    assert 'no time has elapsed' in '\n'.join(part['content'] for part in report['parts'] if part['kind'] == 'message')


@pytest.mark.parametrize('end', [None, '2026-10-17T17:00:00+11:00'])
def test_month_to_date_invalid_or_future_cutoff_fails_closed(end):
    with pytest.raises(TimesheetError, match='invalid_report_month_to_date'):
        resolve_period({'period': 'month_to_date', 'end': end}, FRIDAY)
