"""Weekly reports keep exact client ownership, date boundaries and delivery gates."""
from datetime import timedelta
import json

import pytest

from roo.studio_reports import artifacts, build_client_report, render_chart, resolve_period, summary
from roo.timesheets import TimesheetError, timestamp
from tests.roo.test_studio_reports import setup, ticket, queue, NOW, P1, P2, P3

FRIDAY = timestamp('2026-10-02T07:00:00Z')  # 17:00 AEST
WEEK = {'period': 'weekly', 'start': '2026-09-28T00:00:00+10:00',
        'end': '2026-10-02T17:00:00+10:00', 'action': 'detailed'}


def test_week_crosses_month_and_never_compares_with_monthly_allowance(setup):
    data = [ticket(1, completed='2026-09-28T00:00:00Z'),
            ticket(2, project=P2, size='Large (L)', completed='2026-10-01T00:00:00Z'),
            ticket(3, project=P3, title='Other client secret', completed='2026-10-01T00:00:00Z')]
    report = build_client_report(setup.config, setup.clients['UMARK'], WEEK, data, FRIDAY)
    assert [month['units'] for month in report['monthly']] == [4, 12]
    assert all(month['allowance_units'] is None for month in report['monthly'])
    text = summary(report)
    assert '4 hours counted this week' in text
    assert 'Master App: 1h' in text and 'Cybertest: 3h' in text
    assert '28 Sep 2026' in text and '02 Oct 2026' in text
    assert 'allowance' not in text and 'Other client secret' not in json.dumps(report)
    assert 'STU-1' in artifacts(report)['studio-hours-work.csv']
    assert render_chart(report).startswith(b'\x89PNG')


def test_first_completion_is_counted_once_with_half_open_week_boundaries(setup):
    start = timestamp(WEEK['start'])
    data = [ticket(1, completed=(start - timedelta(seconds=1)).isoformat()),
            ticket(2, completed=start.isoformat()),
            ticket(3, completed=(FRIDAY - timedelta(seconds=1)).isoformat()),
            ticket(4, completed=FRIDAY.isoformat()),
            ticket(5, completed=(FRIDAY - timedelta(days=1)).isoformat())]
    # This issue was first completed before this week and recompleted inside it.
    data[-1]['history'] = [{'id': 'first', 'createdAt': (start - timedelta(days=1)).isoformat(),
                           'toState': {'id': 'done', 'type': 'completed'},
                           'fromState': {'id': 'todo', 'type': 'unstarted'}}]
    report = build_client_report(setup.config, setup.clients['UMARK'], WEEK, data, FRIDAY)
    assert [row['identifier'] for row in report['rows']] == ['STU-2', 'STU-3']


def test_week_includes_zero_projects_and_missing_effort_is_partial(setup):
    report = build_client_report(setup.config, setup.clients['UMARK'], WEEK,
                                [ticket(size='Unknown', completed='2026-10-01T00:00:00Z')], FRIDAY)
    text = summary(report)
    assert 'partial total' in text and 'Master App: Unavailable' in text and 'Cybertest: 0h' in text
    assert 'studio-hours-unresolved.csv' in artifacts(report)


def test_week_history_failure_blocks_apparent_zero_report(setup):
    evidence = ticket(completed='2026-10-01T00:00:00Z')
    evidence['error'] = 'history_unavailable'
    with pytest.raises(TimesheetError, match='source_evidence_incomplete'):
        build_client_report(setup.config, setup.clients['UMARK'], WEEK, [evidence], FRIDAY)


@pytest.mark.parametrize('mutation', [
    {'start': '2026-09-28'}, {'end': '2026-09-28T00:00:00+10:00'},
    {'start': '2026-09-20T00:00:00+10:00'}, {'end': '2026-10-03T17:00:00+10:00'},
    {'start': None},
])
def test_invalid_or_future_week_rejected(mutation):
    with pytest.raises(TimesheetError, match='invalid_report_week'):
        resolve_period({**WEEK, **mutation}, FRIDAY)


def test_rolling_week_resolves_dst_in_melbourne():
    selector, start, end = resolve_period({'period': 'weekly',
        'start': '2026-10-02T17:00:00+10:00', 'end': '2026-10-09T17:00:00+11:00'},
        timestamp('2026-10-09T06:00:00Z'))
    assert (timestamp(end) - timestamp(start)).total_seconds() == 167 * 3600
    assert selector['start'].endswith('+10:00') and selector['end'].endswith('+11:00')


def test_unsigned_week_request_cannot_use_shared_queue_to_send_report(setup):
    request = {'actor': 'UMARK', 'team': 'T123', 'requested_at': FRIDAY.isoformat(), 'selector': WEEK}
    with pytest.raises(TimesheetError, match='not_authorized'):
        setup.service.report_for_request(request)
    setup.api.collect.assert_not_called()
    setup.api.open_dm.assert_not_called()


def test_scheduled_week_does_not_break_monthly_detail_followup(setup):
    queue(setup, event='month', params={'month': '2026-08'}, now=NOW)
    key = 'a' * 64
    setup.service.queue.write(key, {'actor': 'UMARK', 'team': 'T123', 'selector': WEEK,
                                   'status': 'done', 'requested_at': FRIDAY.isoformat()})
    queue(setup, event='detail', params={'action': 'detailed'}, now=FRIDAY + timedelta(seconds=1))
    requests = [setup.service.queue.read(path.stem) for path in setup.config.queue.glob('*.json')]
    detail = [r for r in requests if r['selector'].get('action') == 'detailed'
              and r['selector'].get('period') != 'weekly']
    assert detail[0]['selector']['month'] == '2026-08'
