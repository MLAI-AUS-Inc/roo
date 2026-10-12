"""Offline provider boundary; real scoped discovery, receipts and accounting."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import httpx
import pytest

from roo.tests.test_timesheets import config, ticket, item, USER, OTHER, PROJECT, ORG
from roo.timesheets import TimesheetError, SourceRetryError, timestamp, empty_ledger, build_report
from roo.timesheet_linear import TimesheetAPI

NOW = timestamp('2026-10-22T06:00:00Z')
START = timestamp('2026-10-09T01:00:00Z')
TEAM = '00000000-0000-0000-0000-000000000050'
TEAM2 = '00000000-0000-0000-0000-000000000051'


def page(nodes, cursor=None):
    return {'nodes': nodes, 'pageInfo': {'hasNextPage': cursor is not None, 'endCursor': cursor}}


def matches(issue, filters):
    for name, condition in filters.items():
        if name == 'or':
            if not any(matches(issue, part) for part in condition): return False
        elif name == 'and':
            if not all(matches(issue, part) for part in condition): return False
        elif name in {'team', 'project'}:
            if (issue.get(name) or {}).get('id') not in condition['id']['in']: return False
        else:
            value = issue.get(name)
            if value is None: return False
            value = timestamp(value)
            for op, boundary in condition.items():
                boundary = timestamp(boundary)
                if not {'gt': value > boundary, 'gte': value >= boundary,
                        'lt': value < boundary, 'lte': value <= boundary}[op]: return False
    return True


class Provider:
    def __init__(self, config, dataset=(), *, page_size=100):
        self.config = config
        self.dataset = {x['issue']['id']: deepcopy(x) for x in dataset}
        self.calls = []
        self.page_size = page_size
        self.fail_at = None
        self.after_call = None
        self.project_teams = {key: [TEAM] for key in config.projects}
        self.repeated_cursor = False
        for value in self.dataset.values():
            value['issue'].setdefault('team', {'id': TEAM})

    def connection(self, values, after):
        start = int(after or 0)
        stop = start + self.page_size
        cursor = str(stop) if stop < len(values) else None
        if self.repeated_cursor and after: cursor = after
        return page(deepcopy(values[start:stop]), cursor)

    def handle(self, request):
        payload = json.loads(request.content)
        query, variables = payload['query'], payload['variables']
        self.calls.append((query, deepcopy(variables)))
        if len(self.calls) == self.fail_at:
            return httpx.Response(400, json={'errors': [{'extensions': {'code': 'RATELIMITED'}}]},
                                  headers={'Retry-After': '120'})
        if 'TimesheetOrganization' in query:
            data = {'organization': {'id': self.config.organization}}
        elif 'InvoiceProjectTeams' in query:
            project = variables['id']
            teams = self.project_teams[project]
            data = {'project': {'id': project, 'teams': self.connection([{'id': t} for t in teams], variables.get('after'))}}
        elif 'TimesheetProject' in query:
            data = {'project': {'id': variables['id']}}
        elif 'InvoicePeriodIssues' in query or 'InvoiceChangedIssues' in query:
            assert 'includeArchived:true' in query
            values = [self.light(x['issue']) for x in self.dataset.values() if matches(x['issue'], variables['filter'])]
            data = {'issues': self.connection(sorted(values, key=lambda x: x['id']), variables.get('after'))}
        elif 'InvoiceMoveHistory' in query:
            value = self.dataset.get(variables['id'])
            data = {'issue': None if value is None else {'id': value['issue']['id'],
                'updatedAt': value['issue']['updatedAt'],
                'history': self.connection([{k: h[k] for k in ('id','createdAt','fromProjectId','toProjectId') if k in h}
                                           for h in value['history']], variables.get('after'))}}
        elif 'StudioEvidenceVersions' in query or 'InvoiceIssueVersions' in query:
            data = {f'i{i}': None if key not in self.dataset else {k: self.dataset[key]['issue'][k] for k in ('id','updatedAt')}
                    for i, key in enumerate(variables.values())}
        elif 'StudioEvidenceBatch' in query or 'InvoiceCandidates' in query:
            data = {}
            for i, key in enumerate(variables.values()):
                value = self.dataset.get(key)
                raw = self.full(value['issue']) if value else None
                if raw and 'StudioEvidenceBatch' in query:
                    raw['history'] = self.connection(value['history'], None)
                data[f'i{i}'] = raw
        elif 'TimesheetIssue' in query:
            value = self.dataset.get(variables['id'])
            data = {'issue': self.full(value['issue']) if value else None}
        elif 'TimesheetHistory' in query or 'TimesheetLabels' in query:
            value = self.dataset[variables['id']]
            name = 'history' if 'TimesheetHistory' in query else 'labels'
            data = {'issue': {'id': value['issue']['id'], 'updatedAt': value['issue']['updatedAt'],
                name: self.connection(value['history'] if name == 'history' else value['issue']['labels'], variables.get('after'))}}
        else:
            pytest.fail('Unexpected operation: ' + query[:100])
        if self.after_call:
            self.after_call(query, variables)
        return httpx.Response(200, json={'data': data})

    def light(self, issue):
        return {k: deepcopy(issue.get(k)) for k in ('id','updatedAt','createdAt','completedAt','archivedAt','trashed','project','team')}

    def full(self, issue):
        result = deepcopy(issue)
        result.pop('team', None)
        result['labels'] = self.connection(result['labels'], None)
        return result

    def api(self):
        from roo.invoice_source import InvoiceSourceAPI
        api = InvoiceSourceAPI(httpx.Client(transport=httpx.MockTransport(self.handle)),
                               linear_key='synthetic', slack_token='synthetic')
        api.slack = lambda *a, **k: {'team_id': self.config.team}
        return api

    def count(self, operation):
        return sum(operation in q for q, _ in self.calls)


def current(number=1, **updates):
    completed = '2026-10-20T01:00:00Z'
    return item(ticket(number, completedAt=completed, updatedAt=completed, team={'id':TEAM}, **updates))


def collect(api, config, *, draft=True, deferred=None, allocated=None, required_from=None, end=NOW):
    return api.collect_for_report(config, end, deferred or {}, draft=draft,
        period_start=START, required_from=required_from or config.beginning, allocated=allocated or set())


@pytest.fixture(autouse=True)
def offline_clock(monkeypatch):
    monkeypatch.setattr('roo.studio_report_source.time.time', lambda: NOW.timestamp())


def test_primary_query_is_team_project_and_period_scoped(config):
    provider = Provider(config, [current(), current(2, project={'id':'operations'})])
    provider.project_teams[next(reversed(config.projects))] = [TEAM, TEAM2]
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    provider.calls.clear()
    result = collect(api, config)
    assert [v['issue']['id'] for v in result] == ['issue-1']
    primary = next(v for q, v in provider.calls if 'InvoicePeriodIssues' in q)
    assert primary['filter']['team']['id']['in'] == [TEAM, TEAM2]
    assert set(primary['filter']['project']['id']['in']) == set(config.projects)
    assert timestamp(primary['filter']['createdAt']['lte']) == NOW
    assert primary['filter']['or'][0]['completedAt'] == {'gt': START.isoformat(), 'lte': NOW.isoformat()}
    assert timestamp(primary['filter']['or'][1]['updatedAt']['gte']) == NOW - timedelta(minutes=5)
    assert 'assignee' not in primary['filter'] and 'state' not in primary['filter']


def test_ordinary_request_never_bootstraps_and_scope_changes_require_preparation(config):
    provider = Provider(config, [current()])
    api = provider.api()
    api.verify(config)
    with pytest.raises(TimesheetError, match='invoice_source_not_prepared'):
        collect(api, config)
    assert not any('Issues' in q or 'Evidence' in q for q, _ in provider.calls)
    api.prepare(config, NOW)
    changed = replace(config, first=config.first + timedelta(days=14))
    api.verify(changed)
    with pytest.raises(TimesheetError, match='invoice_source_not_prepared'):
        collect(api, changed)
    assert not (config.directory / 'ledger.json').exists()
    assert not (config.directory / 'invoices').exists()


def test_ten_thousand_unchanged_outsiders_need_no_history(config):
    old = [item(ticket(i, project={'id':'outside'}, updatedAt='2026-01-01T00:00:00Z')) for i in range(10000,20000)]
    provider = Provider(config, old + [current(i) for i in range(17)])
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    provider.calls.clear()
    api = provider.api()
    api.verify(config)
    assert len(collect(api, config)) == 17
    assert provider.count('StudioEvidenceBatch') == 0
    assert provider.count('InvoiceMoveHistory') == 0
    assert provider.count('InvoiceCandidates') > 0
    provider.calls.clear()
    provider.dataset['issue-1']['issue']['labels'][0]['name'] = 'Large (L)'
    outsider = current(99, project={'id':'outside'})
    outsider['issue'].update(updatedAt=NOW.isoformat(), team={'id':TEAM2})
    provider.dataset['issue-99'] = outsider
    result = collect(api, config, deferred={'issue-1': {'reason': 'missing_effort'}})
    assert next(x for x in result if x['issue']['id']=='issue-1')['issue']['labels'][0]['name'] == 'Large (L)'
    batches = [v for q, v in provider.calls if 'StudioEvidenceBatch' in q]
    assert batches == [{'id0':'issue-1'}]
    assert provider.count('InvoiceMoveHistory') == 1
    provider.calls.clear()
    assert collect(api, config, allocated={f'issue-{i}' for i in range(17)}) == []
    assert provider.count('InvoiceCandidates') == 0


def test_unknown_cross_team_move_and_known_missing_issue(config):
    provider = Provider(config, [current()])
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    moved = current(2, project={'id':'outside'})
    moved['issue'].update(updatedAt=NOW.isoformat(), team={'id':TEAM2}, archivedAt=NOW.isoformat())
    moved['history'] = [{'id':'move', 'createdAt':NOW.isoformat(), 'fromProjectId':PROJECT,
                         'toProjectId':'outside', 'fromProject':{'id':PROJECT}, 'toProject':{'id':'outside'}}]
    provider.dataset['issue-2'] = moved
    assert {x['issue']['id'] for x in collect(api, config)} == {'issue-1','issue-2'}
    del provider.dataset['issue-1']
    with pytest.raises(TimesheetError, match='invoice_source_coverage'):
        collect(provider.api(), config)


@pytest.mark.parametrize('bad', ['no_teams', 'cursor', 'wrong_id'])
def test_scope_preflight_rejects_incomplete_team_relations(config, bad):
    provider = Provider(config, page_size=1)
    if bad == 'no_teams': provider.project_teams[PROJECT] = []
    elif bad == 'cursor':
        provider.project_teams[PROJECT] = [TEAM, TEAM2, ORG]
        provider.repeated_cursor = True
    else:
        original = provider.handle
        def handle(request):
            response = original(request)
            if 'InvoiceProjectTeams' in json.loads(request.content)['query']:
                data = response.json()
                data['data']['project']['id'] = 'wrong'
                return httpx.Response(200, json=data)
            return response
        provider.handle = handle
    with pytest.raises(TimesheetError):
        provider.api().verify(config)


def test_bootstrap_keeps_first_completion_and_does_not_allocate(config):
    reopened = current(1)
    reopened['history'] = [{'id':'old-done','createdAt':'2026-08-20T01:00:00Z',
        'fromState':{'id':'todo','type':'unstarted'},'toState':{'id':'done','type':'completed'}},
        {'id':'reopen','createdAt':'2026-10-10T01:00:00Z','fromStateId':'done','toStateId':'todo',
         'fromState':{'id':'done','type':'completed'},'toState':{'id':'todo','type':'unstarted'}},
        {'id':'new-done','createdAt':'2026-10-20T01:00:00Z','fromStateId':'todo','toStateId':'done',
         'fromState':{'id':'todo','type':'unstarted'},'toState':{'id':'done','type':'completed'}}]
    provider = Provider(config, [reopened, item(ticket(2)), current(3)])
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    result = collect(api, config)
    assert len(next(x for x in result if x['issue']['id']=='issue-1')['history']) == 3
    report = build_report(config, NOW.astimezone(config.tz), result, empty_ledger(), draft=True)
    assert [r['issue_id'] for r in report['rows']] == ['issue-3']
    assert not (config.directory / 'ledger.json').exists()


def test_failed_refresh_does_not_publish_or_advance_watermark(config, monkeypatch):
    clock = [NOW.timestamp()]
    monkeypatch.setattr('roo.studio_report_source.time.time', lambda: clock[0])
    provider = Provider(config, [current(i) for i in range(17)], page_size=5)
    api = provider.api()
    api.verify(config)
    provider.fail_at = len(provider.calls) + 5
    with pytest.raises(SourceRetryError): api.prepare(config, NOW)
    assert not list((config.directory / 'linear-invoice-index').glob('*/manifest.json'))
    provider.fail_at = None
    clock[0] += 121
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    path = next((config.directory / 'linear-invoice-index').glob('*/manifest.json'))
    before = path.read_bytes()
    provider.dataset['issue-0']['issue']['title'] = 'changed'
    provider.fail_at = len(provider.calls) + 3
    with pytest.raises(SourceRetryError): collect(api, config)
    assert path.read_bytes() == before
    provider.fail_at = None
    clock[0] += 121
    result = collect(provider.api(), config)
    assert next(x for x in result if x['issue']['id']=='issue-0')['issue']['title'] == 'changed'


def test_resumed_budget_keeps_discovery_pages_and_completes(config, monkeypatch):
    clock = [NOW.timestamp()]
    monkeypatch.setattr('roo.studio_report_source.time.time', lambda: clock[0])
    provider = Provider(config, [current(i) for i in range(12)], page_size=2)
    api = provider.api()
    api.verify(config)
    for _ in range(12):
        try:
            api.verify(config)
            with api.source_attempt(max_requests=8): api.prepare(config, NOW)
            break
        except SourceRetryError as error:
            assert str(error) == 'linear_source_budget'
            clock[0] += 61
            api = provider.api()
    else: pytest.fail('resumable preparation never completed')
    first_pages = [v for q, v in provider.calls if 'InvoicePeriodIssues' in q and not v['after']]
    assert len(first_pages) <= 3
    assert len(collect(api, config)) == 12


def test_closing_discovery_finds_concurrent_payable_issue_before_sealing(config):
    provider = Provider(config, [current()])
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    inserted = [False]
    def changed(query, variables):
        if 'StudioEvidenceVersions' in query and not inserted[0]:
            provider.dataset['issue-2'] = current(2)
            inserted[0] = True
    provider.dataset['issue-1']['issue']['title'] = 'force reread'
    provider.after_call = changed
    result = collect(api, config, draft=False)
    assert {x['issue']['id'] for x in result} == {'issue-1','issue-2'}


def test_three_unstable_passes_never_publish(config):
    provider = Provider(config, [current()])
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    path = next((config.directory / 'linear-invoice-index').glob('*/manifest.json'))
    before = path.read_bytes()
    def changed(query, variables):
        if 'InvoiceCandidates' in query:
            provider.dataset['issue-1']['issue']['title'] += '!'
    provider.after_call = changed
    with pytest.raises(SourceRetryError, match='linear_source_unstable'):
        collect(api, config, draft=False)
    assert path.read_bytes() == before


def test_legacy_adapter_collection_is_unchanged(config):
    api = TimesheetAPI(httpx.Client(), linear_key='synthetic', slack_token='synthetic')
    calls = []
    api.collect = lambda *args: calls.append(args) or ['legacy']
    assert api.collect_for_report(config, NOW, {}, draft=True, period_start=START,
                                  required_from=config.beginning, allocated=set()) == ['legacy']
    assert calls == [(config, NOW, {})]


def test_long_history_pages_survive_attempt_budget(config, monkeypatch):
    clock = [NOW.timestamp()]
    monkeypatch.setattr('roo.studio_report_source.time.time', lambda: clock[0])
    value = current()
    value['history'] = [{'id':f'h{i}', 'createdAt':'2026-08-20T01:00:00Z', 'fromTitle':'older'} for i in range(30)]
    provider = Provider(config, [value], page_size=2)
    api = provider.api()
    api.verify(config)
    for _ in range(10):
        try:
            api.verify(config)
            with api.source_attempt(max_requests=12): api.prepare(config, NOW)
            break
        except SourceRetryError:
            clock[0] += 61
            api = provider.api()
    else: pytest.fail('long full history restarted instead of resuming')
    first_history = [v for q,v in provider.calls if 'TimesheetHistory' in q and v['after'] is None]
    assert len(first_history) == 1
    assert len(collect(api, config)[0]['history']) == 30


def test_negative_outsider_later_moves_through_studio_and_long_move_pages(config):
    outsider = current(9, project={'id':'outside'})
    provider = Provider(config, [current(), outsider], page_size=2)
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    outsider = provider.dataset['issue-9']
    outsider['issue']['updatedAt'] = NOW.isoformat()
    outsider['history'] = [{'id':f'noise{i}', 'createdAt':'2026-09-20T01:00:00Z'} for i in range(5)] + [
        {'id':'enter','createdAt':'2026-10-15T01:00:00Z','fromProjectId':'outside','toProjectId':PROJECT,
         'fromProject':{'id':'outside'}, 'toProject':{'id':PROJECT}},
        {'id':'leave','createdAt':NOW.isoformat(),'fromProjectId':PROJECT,'toProjectId':'outside',
         'fromProject':{'id':PROJECT},'toProject':{'id':'outside'}}]
    assert {x['issue']['id'] for x in collect(api, config)} == {'issue-1','issue-9'}


def test_corrupt_index_does_not_silently_bootstrap_or_return_zero(config):
    provider = Provider(config, [current()])
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    path = next((config.directory / 'linear-invoice-index').glob('*/manifest.json'))
    value = json.loads(path.read_text())
    value['known'] = {}
    path.write_text(json.dumps(value))
    provider.calls.clear()
    with pytest.raises(TimesheetError, match='invoice_source_coverage'): collect(api, config)
    assert provider.calls == []


def test_label_rename_on_later_page_rehydrates_without_issue_version_change(config):
    value = current(labels=[{'id':'a','name':'Misc'}, {'id':'b','name':'Misc2'}, {'id':'size','name':'Small (S)'}])
    provider = Provider(config, [value], page_size=2)
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    provider.dataset['issue-1']['issue']['labels'][2]['name'] = 'Large (L)'
    result = collect(api, config, deferred={'issue-1': {'reason':'missing_effort'}})
    assert result[0]['issue']['labels'][2]['name'] == 'Large (L)'


@pytest.mark.parametrize('change', ['credential','organization','timezone','rubric','version','projects'])
def test_source_identity_changes_require_new_preparation(config, monkeypatch, change):
    from zoneinfo import ZoneInfo
    provider = Provider(config, [current()])
    api = provider.api()
    api.verify(config)
    api.prepare(config, NOW)
    fresh = provider.api()
    if change == 'credential': fresh.linear_key = 'rotated'
    elif change == 'organization':
        config = replace(config, organization=TEAM2)
        provider.config = config
    elif change == 'timezone': config = replace(config, tz=ZoneInfo('UTC'))
    elif change == 'rubric': monkeypatch.setattr('roo.invoice_source.RUBRIC','new')
    elif change == 'version': monkeypatch.setattr('roo.invoice_source.VERSION',2)
    elif change == 'projects': config = replace(config, projects={PROJECT:config.projects[PROJECT]})
    fresh.verify(config)
    with pytest.raises(TimesheetError, match='invoice_source_not_prepared'): collect(fresh, config)
