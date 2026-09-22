"""Synthetic provider responses only; no production credentials or services."""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import Mock

import httpx
import pytest

from roo.studio_report_source import BATCH_SIZE, StudioSourceAPI
from roo.studio_reports import build_client_report
from roo.studio_report_worker import StudioReportAPI
from roo.timesheet_linear import ISSUES
from roo.timesheet_worker import ReportAPI
from roo.timesheets import SourceRetryError
from roo.tests.test_studio_reports import setup, ticket, queue, NOW, P1, P3


def page(nodes, more=False):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': more, 'endCursor': 'next' if more else None}}


def candidates(count):
    result = []
    for number in range(count):
        item = ticket(number)['issue']
        item['labels'] = page(item['labels'])
        result.append(item)
    return result


def source(items, *, key='synthetic', fail_batch=None, alter=None):
    api = StudioReportAPI(httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('unexpected network'))),
                          linear_key=key, slack_token='synthetic')
    batches = []

    def handle(query, variables=None):
        if query == ISSUES:
            assert variables['filter']['or'][1]['updatedAt']
            return {'issues': page(deepcopy(items))}
        ids = list(variables.values())
        assert len(ids) <= BATCH_SIZE
        if 'StudioEvidenceBatch' in query:
            batches.append(ids)
            if len(batches) == fail_batch:
                raise SourceRetryError('linear_rate_limited', NOW.timestamp() + 600)
        result = {}
        for index, issue_id in enumerate(ids):
            item = deepcopy(next(item for item in items if item['id'] == issue_id))
            if 'StudioEvidenceVersions' in query:
                item = {k: item[k] for k in ('id', 'updatedAt')}
            else:
                item['history'] = page([])
            if alter:
                alter(query, item)
            result[f'i{index}'] = item
        return result

    api.linear = Mock(side_effect=handle)
    return api, batches


def test_cold_reads_are_batched_and_warm_reads_still_refresh_candidates(setup):
    items = candidates(19)
    api, batches = source(items)
    result = api.collect(setup.config, NOW, {})
    assert len(result) == 19 and len(batches) == 3
    assert api.linear.call_count == 7  # one live listing plus two reads per batch
    # A process restart retains completed reads on the private worker volume.
    warm, batches = source(items)
    assert warm.collect(setup.config, NOW, {}) == result
    assert warm.linear.call_count == 1 and not batches
    assert not list(setup.config.queue.glob('**/*evidence*'))


def test_changes_invalidate_cache_and_cache_never_widens_project_scope(setup):
    items = candidates(2)
    items[1]['project'] = {'id': P3}
    api, _ = source(items)
    assert len(api.collect(setup.config, NOW, {})) == 2
    # Even a field change with an unchanged timestamp forces another read.
    items[0]['title'] = 'Changed title'
    restricted = replace(setup.config, projects={P1: setup.config.projects[P1]})
    api, batches = source(items)
    result = api.collect(restricted, NOW, {})
    assert len(result) == 1 and result[0]['issue']['title'] == 'Changed title'
    assert batches == [['issue-0']]
    # Deleted/inaccessible candidates cannot be resurrected from the cache.
    api, _ = source([])
    assert api.collect(restricted, NOW, {}) == []


def test_successful_batches_survive_rate_limit_failure_and_resume(setup):
    items = candidates(17)
    api, _ = source(items, fail_batch=2)
    with pytest.raises(SourceRetryError, match='linear_rate_limited') as error:
        api.collect(setup.config, NOW, {})
    assert error.value.retry_at == NOW.timestamp() + 600
    resumed, batches = source(items)
    assert len(resumed.collect(setup.config, NOW, {})) == 17
    assert batches == [[f'issue-{i}' for i in range(8, 16)], ['issue-16']]


def test_shared_cache_keeps_historical_ownership_checks_for_each_client(setup):
    items = candidates(1)
    items[0]['updatedAt'] = '2026-09-12T00:00:00Z'
    def alter(query, item):
        if 'StudioEvidenceBatch' in query:
            item['history'] = page([{'id':'move', 'createdAt':'2026-09-12T00:00:00Z',
                'fromProjectId':P3, 'toProjectId':P1, 'fromProject':{'id':P3}, 'toProject':{'id':P1}}])
    api, _ = source(items, alter=alter)
    data = api.collect(setup.config, NOW, {})
    staff = {'name':'Staff', 'project_ids':list(setup.config.projects), 'monthly_hours':None}
    assert build_client_report(setup.config, staff, {}, data, NOW)['rows'][0]['project_id'] == P3
    warm, batches = source(items)
    data = warm.collect(replace(setup.config, projects={P1:'Master App'}), NOW, {})
    assert not batches
    assert build_client_report(setup.config, setup.clients['UMARK'], {}, data, NOW)['rows'] == []


@pytest.mark.parametrize('change', ['credential', 'organization', 'corrupt'])
def test_cache_is_partitioned_and_corruption_requires_source_read(setup, change):
    items = candidates(1)
    api, _ = source(items)
    api.collect(setup.config, NOW, {})
    config = setup.config
    if change == 'organization':
        config = replace(config, organization='00000000-0000-0000-0000-000000000099')
    if change == 'corrupt':
        path = next((config.directory / 'linear-evidence').glob('*/*.json'))
        value = json.loads(path.read_text())
        value['evidence']['issue']['title'] = 'Corrupted cached value'
        path.write_text(json.dumps(value))
    api, batches = source(items, key='rotated' if change == 'credential' else 'synthetic')
    result = api.collect(config, NOW, {})
    assert len(batches) == 1 and result[0]['issue']['title'] == 'Work 0'


@pytest.mark.parametrize('reason', ['history_pages', 'label_pages', 'changed_version'])
def test_complex_or_changed_issues_use_existing_full_evidence_reader(setup, monkeypatch, reason):
    items = candidates(1)
    def alter(query, item):
        if 'StudioEvidenceBatch' in query:
            if reason == 'history_pages':
                item['history'] = page([], more=True)
            elif reason == 'label_pages':
                item['labels']['pageInfo'] = page([], more=True)['pageInfo']
        elif reason == 'changed_version':
            item['updatedAt'] = '2026-09-22T02:00:00Z'
    complete = ticket(0)
    complete['history'] = [{'id': 'later-page', 'fromProjectId': P3}]
    fallback = Mock(return_value=complete)
    monkeypatch.setattr(ReportAPI, 'evidence', fallback)
    api, _ = source(items, alter=alter)
    assert api.collect(setup.config, NOW, {}) == [complete]
    fallback.assert_called_once_with('issue-0')


@pytest.mark.parametrize('status', [200, 400, 429])
def test_rate_limit_errors_preserve_provider_retry_time_without_logging_body(status, monkeypatch):
    monkeypatch.setattr('roo.studio_report_source.time.time', lambda: NOW.timestamp())
    reset = NOW.timestamp() + 300
    def handle(request):
        return httpx.Response(status, json={'errors':[{'message':'private provider detail',
            'extensions':{'code':'RATELIMITED'}}]}, headers={
            'x-ratelimit-requests-remaining':'0','x-ratelimit-requests-reset':str(reset * 1000),
            'x-ratelimit-complexity-remaining':'1000','x-ratelimit-complexity-reset':str((reset + 900) * 1000),
            'Retry-After':'120'})
    api = StudioSourceAPI(httpx.Client(transport=httpx.MockTransport(handle)), linear_key='synthetic', slack_token='synthetic')
    with pytest.raises(SourceRetryError, match='^linear_rate_limited$') as error:
        api.linear('query { organization { id } }')
    assert error.value.retry_at == reset


@pytest.mark.parametrize('status,body,code', [(503, {}, 'linear_http_503'),
    (400, {'errors':[{'message':'private'}]}, 'linear_http_400'), (200, {}, 'linear_invalid_response')])
def test_source_errors_are_specific_and_sanitized(status, body, code):
    api = StudioSourceAPI(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))),
                          linear_key='synthetic', slack_token='synthetic')
    with pytest.raises(SourceRetryError, match='^' + code + '$'):
        api.linear('query { organization { id } }')


@pytest.mark.parametrize('provider_delay,expected_delay', [(30, 560), (900, 900)])
def test_slow_failure_backs_off_from_failure_and_honors_source_cooldown(setup, monkeypatch, provider_delay, expected_delay):
    queue(setup)
    setup.api.collect.side_effect = SourceRetryError('linear_rate_limited', NOW.timestamp() + provider_delay)
    monkeypatch.setattr('roo.timesheet_worker.time.monotonic', Mock(side_effect=[0, 500]))
    assert setup.service.commands(NOW)[0]['reason'] == 'linear_rate_limited'
    path = next(setup.config.queue.glob('*.json'))
    request = json.loads(path.read_text())
    assert request['retry_at'] == NOW.timestamp() + expected_delay
    assert not (setup.config.directory / 'requests' / path.name).exists()
