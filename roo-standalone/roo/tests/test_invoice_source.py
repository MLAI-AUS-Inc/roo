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
        elif 'InvoicePeriodIssues' in query or 'InvoiceChangedIssues' in query or 'TimesheetIssues' in query:
            assert 'includeArchived:true' in query or 'includeArchived: true' in query
            project = self.full if 'TimesheetIssues' in query else self.light
            values = [project(x['issue']) for x in self.dataset.values() if matches(x['issue'], variables['filter'])]
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


class SlackBoundary:
    """Minimal external Slack protocol, with real recipient and upload checks."""
    def __init__(self, provider):
        self.provider = provider
        self.messages, self.files, self.acks = [], [], []
        self.inactive = set()

    def handle(self, request):
        if request.url.host == 'api.linear.app': return self.provider.handle(request)
        assert request.url.host in {'slack.com','files.slack.com'}
        if request.url.host == 'files.slack.com':
            self.files.append(request.content)
            return httpx.Response(200)
        method = request.url.path.rsplit('/',1)[-1]
        data = json.loads(request.content) if request.method=='POST' else dict(request.url.params)
        body = {'ok':True}
        if method=='auth.test': body['team_id'] = self.provider.config.team
        elif method=='users.info':
            body['user'] = {'id':data['user'],'team_id':self.provider.config.team,
                            'deleted':data['user'] in self.inactive,'is_bot':False,'is_app_user':False}
        elif method=='conversations.open': body['channel'] = {'id':'D'+data['users']}
        elif method=='chat.postMessage':
            (self.acks if data.get('thread_ts') else self.messages).append(data)
            body.update(channel=data['channel'], ts='123.456')
        elif method=='files.getUploadURLExternal':
            body.update(upload_url='https://files.slack.com/upload/synthetic',file_id='F123')
        elif method=='files.completeUploadExternal': body['files'] = [{'id':'F123'}]
        else: pytest.fail('Unexpected Slack method ' + method)
        return httpx.Response(200,json=body)

    def api(self):
        from roo.invoice_source import InvoiceSourceAPI
        return InvoiceSourceAPI(httpx.Client(transport=httpx.MockTransport(self.handle)),
                                linear_key='synthetic',slack_token='synthetic')


def worker_env(config):
    return {'TIMESHEETS_ENABLED':'true', 'TIMESHEET_SLACK_TEAM_ID':config.team,
            'TIMESHEET_LINEAR_ORGANIZATION_ID':config.organization, 'TIMESHEET_RECIPIENT_SLACK_ID':config.recipient,
            'TIMESHEET_PROJECTS_JSON':json.dumps(config.projects),
            'TIMESHEET_BUILDERS_JSON':json.dumps([{'linear_user_id':k,**v} for k,v in config.builders.items()]),
            'TIMESHEET_DATA_DIR':str(config.directory), 'TIMESHEET_QUEUE_DIR':str(config.queue),
            'TIMESHEET_LINEAR_READ_API_KEY':'synthetic', 'TIMESHEET_SLACK_BOT_TOKEN':'synthetic',
            'STUDIO_INVOICES_ENABLED':'true', 'STUDIO_INVOICES_SCHEDULE_ENABLED':'false',
            'STUDIO_INVOICE_QUEUE_DIR':str(config.directory.parent/'invoice-queue')}


def test_cli_uses_scoped_reader_and_explicit_preparation(config, monkeypatch, capsys):
    from datetime import datetime
    from roo.timesheet_worker import main, TimesheetService
    from roo.invoice_worker import InvoiceService
    from roo.studio_invoices import InvoiceConfig
    from roo.tests.test_invoice_worker import queue
    provider = Provider(config, [current(1),current(2,assignee={'id':OTHER})])
    boundary = SlackBoundary(provider)
    client_class = httpx.Client
    monkeypatch.setattr('roo.timesheet_worker.httpx.Client', lambda **kw: client_class(transport=httpx.MockTransport(boundary.handle),**kw))
    class Clock(datetime):
        @classmethod
        def now(cls,*args,**kwargs): return NOW
    monkeypatch.setattr('roo.timesheet_worker.datetime',Clock)
    for k,v in worker_env(config).items(): monkeypatch.setenv(k,v)
    assert main(['--prepare-source']) == 0
    assert 'prepared' in capsys.readouterr().out
    assert not boundary.files and not boundary.messages
    assert not (config.directory/'ledger.json').exists()
    assert not list((config.directory/'invoices').glob('**/*.json'))
    s = InvoiceService(TimesheetService(config, None), InvoiceConfig(True,False,config.directory.parent/'invoice-queue'))
    queue(s, now=NOW, thread_ts='123.456')
    provider.calls.clear()
    assert main(['--once']) == 0
    assert provider.count('InvoicePeriodIssues') > 0
    assert provider.count('TimesheetIssues') == 0
    assert len(boundary.files)==1 and len(boundary.messages)==1 and len(boundary.acks)==1
    assert boundary.messages[0]['channel']=='DUBUILDER'
    from pypdf import PdfReader
    import io
    text = '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(boundary.files[0])).pages)
    assert 'STU-1' in text and 'STU-2' not in text
    assert not (config.directory/'ledger.json').exists()


def test_fresh_interpreter_disabled_worker_has_no_import_cycle(tmp_path):
    import os
    import subprocess
    import sys
    env = {**os.environ,'TIMESHEETS_ENABLED':'false','STUDIO_INVOICES_ENABLED':'false'}
    result = subprocess.run([sys.executable,'-m','roo.timesheet_worker','--once'],env=env,capture_output=True,text=True)
    assert result.returncode==0 and json.loads(result.stdout)=={'status':'disabled'}


def test_cooldown_blocks_all_builders_without_zero_artifacts(config, monkeypatch):
    from roo.timesheet_worker import TimesheetService
    from roo.invoice_worker import InvoiceService
    from roo.studio_invoices import InvoiceConfig
    from roo.tests.test_invoice_worker import queue
    clock = [NOW.timestamp()]
    monkeypatch.setattr('roo.studio_report_source.time.time',lambda:clock[0])
    provider = Provider(config, [current(),current(2,assignee={'id':OTHER})])
    boundary = SlackBoundary(provider)
    api = boundary.api()
    api.verify(config)
    api.prepare(config,NOW)
    s = InvoiceService(TimesheetService(config,api),InvoiceConfig(True,True,config.directory.parent/'invoice-queue'))
    provider.fail_at = len(provider.calls)+1
    queue(s,actor='UBUILDER',now=NOW,source='a')
    queue(s,actor='UOTHER',now=NOW,source='b')
    results = s.commands(NOW)
    assert len(results)==2 and all(r['reason']=='linear_rate_limited' for r in results)
    count = len(provider.calls)
    resumed = InvoiceService(TimesheetService(config,boundary.api()),s.options)
    resumed.scheduled(NOW)
    assert len(provider.calls)==count
    assert not boundary.files and not boundary.acks
    assert not s.timesheets.ledger.load()['allocated']
    assert not list(s.drafts.directory.glob('*.json'))
    provider.fail_at=None
    clock[0] += 121
    results = resumed.commands(NOW+timedelta(seconds=121))
    assert all(r['status']=='sent' for r in results)
    assert len(boundary.files)==2


def test_empty_ledger_draft_replays_earlier_periods_without_writes(config):
    from roo.timesheet_worker import TimesheetService
    from roo.tests.test_invoice_worker import service, THURSDAY, FRIDAY
    reopened = current(1)
    reopened['history'] = [
        {'id':'old','createdAt':'2026-09-10T01:00:00Z','fromStateId':'todo','toStateId':'done',
         'fromState':{'id':'todo','type':'unstarted'},'toState':{'id':'done','type':'completed'}},
        {'id':'reopen','createdAt':'2026-10-10T01:00:00Z','fromStateId':'done','toStateId':'todo',
         'fromState':{'id':'done','type':'completed'},'toState':{'id':'todo','type':'unstarted'}},
        {'id':'again','createdAt':'2026-10-20T01:00:00Z','fromStateId':'todo','toStateId':'done',
         'fromState':{'id':'todo','type':'unstarted'},'toState':{'id':'done','type':'completed'}}]
    provider=Provider(config,[reopened,item(ticket(2)),current(3),current(4,labels=[])])
    boundary=SlackBoundary(provider)
    api=boundary.api()
    api.verify(config)
    api.prepare(config,NOW)
    s=service(config,api=api)
    draft=s.artifact('UBUILDER','current',THURSDAY)
    assert [r['issue_id'] for r in draft['invoice']['rows']]==['issue-3']
    assert draft['invoice']['total_cents']==6500 and not draft['complete']
    assert not s.timesheets.ledger.load()['allocated']
    final=s.artifact('UBUILDER','2026-10-23',FRIDAY)
    ledger=s.timesheets.ledger.load()
    assert ledger['allocated']=={'issue-1':'2026-09-11','issue-2':'2026-09-11','issue-3':'2026-10-23'}
    assert 'issue-4' in ledger['deferred']
    provider.calls.clear()
    assert service(config,api=boundary.api()).artifact('UBUILDER','2026-10-23',FRIDAY)==final
    assert provider.calls==[]


def test_nineteen_member_scoped_results_match_exhaustive_mixed_history_reader(config):
    from roo.timesheet_worker import TimesheetService
    from roo.tests.test_invoice_worker import service
    from roo.studio_invoices import build_invoice
    builders={f'00000000-0000-0000-0000-{i:012d}':{'name':f'Builder {i}','slack_user_id':f'UBUILDER{i}'} for i in range(1,20)}
    config=replace(config,builders=builders)
    dataset=[current(i,assignee={'id':key},size=['Extra Small (XS)','Small (S)','Medium (M)','Large (L)','Extra Large (XL)'][(i-1)%5])
             for i,key in enumerate(builders,1)]
    dataset += [item(ticket(50)),current(51,labels=[]),current(52,project={'id':'operations'})]
    # Owner/size/project at cutoff differ from their current values.
    moved=current(53,project={'id':'outside'},assignee={'id':OTHER},size='Extra Large (XL)')
    moved['issue'].update(updatedAt='2026-10-22T07:00:00Z',team={'id':TEAM2})
    moved['history']=[{'id':'move','createdAt':'2026-10-22T07:00:00Z',
        'fromProjectId':PROJECT,'toProjectId':'outside','fromProject':{'id':PROJECT},'toProject':{'id':'outside'},
        'fromAssigneeId':USER,'toAssigneeId':OTHER,'fromAssignee':{'id':USER},'toAssignee':{'id':OTHER},
        'addedLabelIds':['size'],'removedLabelIds':['old'],'removedLabels':[{'id':'old','name':'Large (L)'}]}]
    dataset.append(moved)
    provider=Provider(config,dataset)
    boundary=SlackBoundary(provider)
    api=boundary.api()
    api.verify(config)
    api.prepare(config,NOW)
    legacy=TimesheetAPI(httpx.Client(transport=httpx.MockTransport(boundary.handle)),linear_key='synthetic',slack_token='synthetic')
    old=TimesheetService(replace(config,directory=config.directory.parent/'legacy'),legacy)
    expected=old.report('current',NOW)
    provider.calls.clear()
    s=service(config,api=api)
    results=s.scheduled(NOW)
    assert len(results)==19 and all(r['status']=='sent' for r in results)
    assert provider.count('InvoicePeriodIssues')==2
    assert provider.count('StudioEvidenceBatch')==0
    assert len(boundary.files)==19
    docs=[s.drafts.read(p.stem) for p in s.drafts.directory.glob('*.json')]
    for key in builders:
        actual=next(d for d in docs if d['builder_id']==key)['invoice']
        previous=build_invoice(config,expected,key,'2026-10-23')
        assert actual['rows']==previous['rows']
        assert actual['total_cents']==previous['total_cents']
    assert not s.timesheets.ledger.load()['allocated']


def test_saved_artifact_delivery_works_during_source_cooldown(config):
    from roo.tests.test_invoice_worker import service, queue
    provider=Provider(config,[current()])
    boundary=SlackBoundary(provider)
    api=boundary.api()
    api.verify(config)
    api.prepare(config,NOW)
    s=service(config,api=api)
    key=queue(s,now=NOW,thread_ts='123.456')
    artifact=s.command_artifact(s.queue.read(key),key,NOW)
    provider.fail_at=len(provider.calls)+1
    with pytest.raises(SourceRetryError): api.verify(config)
    count=len(provider.calls)
    resumed=service(config,api=boundary.api())
    assert resumed.commands(NOW)[0]['status']=='sent'
    assert len(provider.calls)==count and len(boundary.files)==1 and len(boundary.acks)==1
    assert resumed.command_artifact(resumed.queue.read(key),key,NOW)==artifact


def test_cli_preflight_and_preparation_share_request_budget(config, monkeypatch):
    from roo.timesheet_worker import main
    from datetime import datetime
    provider=Provider(config,[current(i) for i in range(240)],page_size=2)
    boundary=SlackBoundary(provider)
    client_class=httpx.Client
    monkeypatch.setattr('roo.timesheet_worker.httpx.Client',lambda **kw:client_class(transport=httpx.MockTransport(boundary.handle),**kw))
    class Clock(datetime):
        @classmethod
        def now(cls,*args,**kwargs): return NOW
    monkeypatch.setattr('roo.timesheet_worker.datetime',Clock)
    for k,v in worker_env(config).items(): monkeypatch.setenv(k,v)
    assert main(['--prepare-source'])==1
    assert len(provider.calls)<=200
    assert not boundary.messages and not boundary.files
    assert not list((config.directory/'linear-invoice-index').glob('*/manifest.json'))


def test_cutoff_dst_and_recovery_preserve_artifacts(config):
    from roo.tests.test_invoice_worker import service, queue
    from roo.timesheet_worker import TimesheetService
    provider=Provider(config,[item(ticket(10,completedAt='2026-09-24T01:00:00Z',updatedAt='2026-09-24T01:00:00Z')),
                              item(ticket(11,completedAt='2026-10-07T01:00:00Z',updatedAt='2026-10-07T01:00:00Z')),current()])
    boundary=SlackBoundary(provider)
    api=boundary.api()
    api.verify(config)
    api.prepare(config,NOW)
    engine=TimesheetService(config,api)
    september=engine.report('2026-09-25',NOW)
    october=engine.report('2026-10-09',NOW)
    assert september['end']=='2026-09-25T12:00:00+10:00'
    assert october['end']=='2026-10-09T12:00:00+11:00'
    assert [r['issue_id'] for r in september['rows']]==['issue-10']
    assert [r['issue_id'] for r in october['rows']]==['issue-11']
    s=service(config,api=api)
    key=queue(s,now=NOW,thread_ts='123.456')
    artifact=s.command_artifact(s.queue.read(key),key,NOW)
    assert artifact['invoice']['total_cents']==6500
    assert s.deliver(artifact,'UBUILDER','command-'+key+'-draft',NOW)=='sent'
    # Crash before the queue's delivered checkpoint, then source exhaustion.
    provider.fail_at=len(provider.calls)+1
    with pytest.raises(SourceRetryError): api.verify(config)
    count=len(provider.calls)
    resumed=service(config,api=boundary.api())
    assert resumed.commands(NOW)[0]['status']=='sent'
    assert resumed.commands(NOW)==[]
    assert len(provider.calls)==count and len(boundary.files)==1 and len(boundary.messages)==1 and len(boundary.acks)==1
    assert resumed.command_artifact(resumed.queue.read(key),key,NOW)==artifact


def test_invalid_required_history_pagination_blocks_source_coverage(config):
    value=current()
    value['history']=[{'id':f'h{i}','createdAt':'2026-09-20T01:00:00Z'} for i in range(6)]
    provider=Provider(config,[value],page_size=2)
    api=provider.api()
    api.verify(config)
    provider.repeated_cursor=True
    with pytest.raises(TimesheetError,match='invoice_source_coverage'): api.prepare(config,NOW)
    assert not list((config.directory/'linear-invoice-index').glob('*/manifest.json'))


def test_unstable_required_history_retries_instead_of_sealing_partial_report(config):
    value=current()
    value['history']=[{'id':f'h{i}','createdAt':'2026-09-20T01:00:00Z'} for i in range(4)]
    provider=Provider(config,[value],page_size=2)
    original=provider.handle
    def handle(request):
        response=original(request)
        if 'TimesheetHistory' in json.loads(request.content)['query']:
            data=response.json()
            data['data']['issue']['updatedAt']='2026-10-22T06:01:00Z'
            return httpx.Response(200,json=data)
        return response
    provider.handle=handle
    api=provider.api()
    api.verify(config)
    with pytest.raises(SourceRetryError,match='linear_source_unstable'): api.prepare(config,NOW)
    assert not list((config.directory/'linear-invoice-index').glob('*/manifest.json'))


@pytest.mark.parametrize('stage', ['after_discovery','complete'])
def test_restart_requires_fresh_closing_discovery_before_publication(config, monkeypatch, stage):
    clock=[NOW.timestamp()]
    monkeypatch.setattr('roo.studio_report_source.time.time',lambda:clock[0])
    provider=Provider(config,[current()])
    api=provider.api()
    api.verify(config)
    api.prepare(config,NOW)
    if stage=='after_discovery':
        with pytest.raises(SourceRetryError,match='linear_source_budget'):
            with api.source_attempt(max_requests=5): collect(api,config,draft=False)
        clock[0]+=61
    else:
        original=api._index.write
        def crash(key,value):
            original(key,value)
            if key=='progress' and value.get('stage')=='complete' and value.get('pass')==1:
                raise OSError('simulated crash before publication')
        monkeypatch.setattr(api._index,'write',crash)
        with pytest.raises(OSError): collect(api,config,draft=False)
    provider.dataset['issue-2']=current(2)
    provider.calls.clear()
    result=collect(provider.api(),config,draft=False)
    assert {x['issue']['id'] for x in result}=={'issue-1','issue-2'}
    assert provider.count('InvoicePeriodIssues')>0


def test_resumed_refresh_reconciles_changed_cutoff_and_new_deferred_ids(config, monkeypatch):
    clock=[NOW.timestamp()]
    monkeypatch.setattr('roo.studio_report_source.time.time',lambda:clock[0])
    provider=Provider(config,[current()])
    api=provider.api()
    api.verify(config)
    api.prepare(config,NOW)
    with pytest.raises(SourceRetryError):
        with api.source_attempt(max_requests=5): collect(api,config,draft=False)
    clock[0]+=61
    new=current(2)
    new['issue'].update(completedAt=(NOW+timedelta(hours=1)).isoformat(),updatedAt=(NOW+timedelta(hours=1)).isoformat())
    provider.dataset['issue-2']=new
    deferred=current(3,project={'id':'outside'})
    deferred['issue'].update(team={'id':TEAM2},updatedAt='2026-08-01T00:00:00Z')
    provider.dataset['issue-3']=deferred
    result=collect(provider.api(),config,draft=False,end=NOW+timedelta(hours=2),deferred={'issue-3':{'reason':'missing_effort'}})
    assert {x['issue']['id'] for x in result}=={'issue-1','issue-2','issue-3'}


@pytest.mark.parametrize('mismatch', ['history_page','version_check'])
def test_outsider_edit_invalidates_stale_discovery_and_recovers(config, monkeypatch, mismatch):
    clock=[NOW.timestamp()]
    monkeypatch.setattr('roo.studio_report_source.time.time',lambda:clock[0])
    provider=Provider(config,[current(),current(2,project={'id':'outside'})])
    api=provider.api()
    api.verify(config)
    changed=[False]
    def edit(query,variables):
        operation='InvoiceChangedIssues' if mismatch=='history_page' else 'InvoiceMoveHistory'
        if operation in query and not changed[0]:
            provider.dataset['issue-2']['issue']['updatedAt']=(NOW+timedelta(seconds=1)).isoformat()
            changed[0]=True
    provider.after_call=edit
    with pytest.raises(SourceRetryError,match='linear_source_unstable'): api.prepare(config,NOW)
    clock[0]+=61
    provider.after_call=None
    resumed=provider.api()
    resumed.verify(config)
    result=resumed.prepare(config,NOW)
    assert result['status']=='prepared'
    assert [x['issue']['id'] for x in collect(resumed,config)]==['issue-1']


def test_slow_provider_report_over_long_history_completes_instead_of_repeating(config, monkeypatch):
    # Production shape: 378 historical issues at ~0.7s per Linear call. Each
    # attempt previously spent its whole time budget and restarted forever.
    clock, elapsed = [NOW.timestamp()], [0.0]
    monkeypatch.setattr('roo.studio_report_source.time.time', lambda: clock[0])
    monkeypatch.setattr('roo.studio_report_source.time.monotonic', lambda: elapsed[0])
    provider = Provider(config, [item(ticket(i)) for i in range(378)])

    def attempt(work):
        for _ in range(10):
            api = provider.api()
            try:
                with api.source_attempt():
                    api.verify(config)
                    return work(api)
            except SourceRetryError as error:
                assert str(error) == 'linear_source_budget'
                clock[0] += 61
        pytest.fail('source attempts repeated without completing')

    attempt(lambda api: api.prepare(config, NOW))
    provider.after_call = lambda query, variables: elapsed.__setitem__(0, elapsed[0] + 0.7)
    before = len(provider.calls)
    assert len(attempt(lambda api: collect(api, config, draft=False))) == 378
    assert len(provider.calls) - before <= 60
