"""Exercise the real paginated Linear adapter, with synthetic provider responses."""
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest

from roo.timesheet_linear import TimesheetAPI, ISSUES, ISSUE, HISTORY, LABEL_PAGE
from roo.timesheets import TimesheetError
from roo.tests.test_timesheets import config, ticket, PROJECT, END


def page(nodes, cursor=None):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': cursor is not None, 'endCursor': cursor}}


def issue(number=1):
    result = ticket(number)
    result['labels'] = page(result['labels'])
    return result


def adapter(handler):
    api = TimesheetAPI(httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('unexpected network'))),
                        linear_key='synthetic', slack_token='synthetic')
    api.linear = Mock(side_effect=handler)
    return api


def test_all_issue_history_and_label_pages_and_move_out_of_paid_project(config):
    first, second = issue(), issue(2)
    first['labels'] = page([{'id': 'size', 'name': 'Small (S)'}], 'labels-2')
    second['project'] = {'id': 'unpaid'}
    seen = []
    history = {'id': 'h2', 'createdAt': '2026-09-12T00:00:00Z',
               'fromProjectId': PROJECT, 'toProjectId': 'unpaid',
               'fromProject': {'id': PROJECT}, 'toProject': {'id': 'unpaid'}}
    def handle(query, variables):
        seen.append((query, variables))
        current = first if variables.get('id') == 'issue-1' else second
        if query == ISSUES:
            assert variables['filter']['or'][0]['project']['id']['in'] == list(config.projects)
            return {'issues': page([first], 'issues-2') if variables['after'] is None else page([second])}
        if query == ISSUE:
            return {'issue': deepcopy(current)}
        if query == HISTORY:
            entries = [] if current is first else [history]
            connection = page(entries, 'history-2') if variables['after'] is None else page([])
            return {'issue': {'id': current['id'], 'updatedAt': current['updatedAt'], 'history': connection}}
        assert query == LABEL_PAGE
        return {'issue': {'id': current['id'], 'updatedAt': current['updatedAt'],
                         'labels': page([{'id': 'topic', 'name': 'Feature'}])}}
    result = adapter(handle).collect(config, END, {})
    assert len(result) == 2
    assert len(result[0]['issue']['labels']) == 2
    assert result[1]['history'] == [history]
    assert sum(q == HISTORY for q, v in seen) == 4
    assert sum(q == ISSUES for q, v in seen) == 2


def test_issue_changed_during_history_read_is_retried_as_one_version():
    current = issue()
    history_reads = 0
    def handle(query, variables):
        nonlocal history_reads
        if query == ISSUE:
            return {'issue': deepcopy(current)}
        assert query == HISTORY
        history_reads += 1
        if history_reads == 1:
            current['updatedAt'] = '2026-09-10T02:00:00Z'
        return {'issue': {'id': current['id'], 'updatedAt': current['updatedAt'], 'history': page([])}}
    result = adapter(handle).evidence('issue-1')
    assert history_reads == 2
    assert result['issue']['updatedAt'] == '2026-09-10T02:00:00Z'


@pytest.mark.parametrize('connection', [{}, {'nodes': []}, page([], 'repeat')])
def test_incomplete_or_looping_candidate_pages_abort_whole_report(config, connection):
    api = adapter(lambda *args: {'issues': connection})
    with pytest.raises(TimesheetError):
        api.collect(config, END, {})
    assert api.linear.call_count <= 2


def test_missing_history_reference_for_known_project_is_explicit_exception(config):
    api = adapter(lambda *args: {'issues': page([issue()])})
    api.evidence = Mock(side_effect=TimesheetError('issue_unavailable'))
    assert api.collect(config, END, {})[0]['error'] == 'issue_unavailable'


def test_failed_history_outside_allowlist_cannot_hide_prior_paid_project(config):
    candidate = issue()
    candidate['project'] = {'id': 'unpaid'}
    api = adapter(lambda *args: {'issues': page([candidate])})
    api.evidence = Mock(side_effect=TimesheetError('issue_unavailable'))
    with pytest.raises(TimesheetError):
        api.collect(config, END, {})


def test_delivery_identity_checks_token_workspace_before_recipient():
    api = adapter(None)
    api.slack = Mock(return_value={'team_id': 'TOTHER'})
    with pytest.raises(TimesheetError, match='workspace_mismatch'):
        api.verify_recipient('USAM', 'T123')
    api.slack.assert_called_once_with('auth.test')
