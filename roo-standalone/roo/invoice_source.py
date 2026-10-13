"""Private indexed Studio coverage: prepare once, then refresh scoped deltas."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import time
import uuid

from .payment_reminders import ReceiptStore
from .studio_report_source import StudioSourceAPI, batch_query
from .timesheet_linear import FIELDS, HISTORY, ISSUE, LABEL_PAGE, next_page
from .timesheets import RUBRIC, SourceRetryError, TimesheetError, fingerprint, timestamp

VERSION = 1
# Hydration reads issue fields and labels only, so it batches more than full
# evidence. Every known issue is hydrated each pass; 25 stays far below
# Linear's per-query complexity limit (labels(first:100) dominates).
HYDRATE_BATCH_SIZE = 25
LIGHT_FIELDS = 'id createdAt updatedAt completedAt archivedAt trashed project { id } team { id }'
PROJECT_TEAMS = '''query InvoiceProjectTeams($id:String!, $after:String) {
 project(id:$id) { id teams(first:100, after:$after) { nodes { id } pageInfo { hasNextPage endCursor } } }
}'''
MOVE_HISTORY = '''query InvoiceMoveHistory($id:String!, $after:String) {
 issue(id:$id) { id updatedAt history(first:100, after:$after, includeArchived:true, orderBy:createdAt) {
 nodes { id createdAt fromProjectId toProjectId } pageInfo { hasNextPage endCursor }
 } }
}'''


def listing_query(operation):
    return ('query ' + operation + '($filter:IssueFilter!, $after:String) { '
            'issues(first:100, after:$after, includeArchived:true, orderBy:createdAt, filter:$filter) { '
            'nodes { ' + LIGHT_FIELDS + ' } pageInfo { hasNextPage endCursor } } }')


def candidate_query(ids):
    variables = {f'id{i}': key for i, key in enumerate(ids)}
    args = ', '.join(f'${key}:String!' for key in variables)
    selections = ' '.join(f'i{i}: issue(id:$id{i}) {{ {FIELDS} }}' for i in range(len(ids)))
    return f'query InvoiceCandidates({args}) {{ {selections} }}', variables


def normalized(issue):
    value = deepcopy(issue)
    if isinstance(value.get('labels'), dict):
        value['labels'] = value['labels']['nodes']
    return value


class InvoiceSourceAPI(StudioSourceAPI):
    def linear(self, query, variables=None):
        # The legacy full-history fallback can span several attempts. Page
        # receipts are usable only after a fresh issue read pins its version;
        # its final version check still gates the completed evidence.
        variables = variables or {}
        key = None
        versions = getattr(self, '_page_versions', {})
        # Label names can change without an issue version change. Never reuse
        # label-page responses; candidate hydration must observe all names.
        if query == HISTORY and variables.get('id') in versions:
            key = fingerprint({'query':query, 'variables':variables, 'version':versions[variables['id']]})
            try:
                cached = self._pages.read(key)
            except (OSError, ValueError):
                cached = None
            if (isinstance(cached, dict) and isinstance(cached.get('data'), dict)
                    and cached.get('digest') == fingerprint(cached['data'])):
                return deepcopy(cached['data'])
        data = super().linear(query, variables)
        if query == ISSUE and isinstance(data.get('issue'), dict):
            if not hasattr(self, '_page_versions'): self._page_versions = {}
            self._page_versions[variables['id']] = data['issue']['updatedAt']
        if key and (data.get('issue') or {}).get('updatedAt') == versions[variables['id']]:
            with self._pages.locked(key) as acquired:
                if acquired:
                    self._pages.write(key, {'data':data, 'digest':fingerprint(data)})
        return data

    def verify(self, config):
        self._bind_source(config)
        with self.source_attempt():
            super().verify(config)
            teams = set()
            for project_id in config.projects:
                after, seen = None, set()
                while True:
                    project = self.linear(PROJECT_TEAMS, {'id': project_id, 'after': after}).get('project')
                    if not isinstance(project, dict) or project.get('id') != project_id:
                        raise TimesheetError('invoice_scope_unverified')
                    connection = project.get('teams')
                    after = next_page(connection, seen)
                    if not connection['nodes']:
                        raise TimesheetError('invoice_scope_unverified')
                    for team in connection['nodes']:
                        try:
                            teams.add(str(uuid.UUID(team['id'])))
                        except (KeyError, TypeError, ValueError, AttributeError):
                            raise TimesheetError('invoice_scope_unverified') from None
                    if after is None:
                        break
            self._teams = sorted(teams)
            self._scope_config = fingerprint(self._scope_input(config))
            self._index = ReceiptStore(config.directory / 'linear-invoice-index' / self._scope_config)
            self._pages = ReceiptStore(self._index.directory / 'full-pages')
            self._page_versions = {}

    def _scope_input(self, config):
        return {'version': VERSION, 'source': self._source_namespace, 'projects': sorted(config.projects),
                'teams': self._teams, 'first': config.first.isoformat(), 'beginning': config.beginning.isoformat(),
                'timezone': str(config.tz), 'rubric': RUBRIC}

    def _ensure_scope(self, config):
        self._bind_source(config)
        if (not hasattr(self, '_teams') or self._scope_config != fingerprint(self._scope_input(config))):
            self.verify(config)

    def _read(self, key):
        try:
            value = self._index.read(key)
            if value is not None:
                digest = value.get('digest') if isinstance(value, dict) else None
                if not digest or digest != fingerprint({k:v for k,v in value.items() if k != 'digest'}):
                    raise ValueError('invalid index digest')
            return value
        except (OSError, ValueError, TypeError):
            raise TimesheetError('invoice_source_coverage') from None

    def _write(self, key, value):
        value = {k:v for k,v in value.items() if k != 'digest'}
        self._index.write(key, {**value, 'digest': fingerprint(value)})

    def prepare(self, config, observed_at):
        return self._refresh(config, timestamp(observed_at), {}, draft=False,
                             period_start=config.beginning, required_from=config.beginning,
                             allocated=set(), preparing=True)

    def collect_for_report(self, config, end, deferred, *, draft, period_start, required_from, allocated):
        return self._refresh(config, timestamp(end), deferred, draft=draft, period_start=timestamp(period_start),
                             required_from=timestamp(required_from), allocated=set(allocated), preparing=False)

    def _new_pass(self, progress):
        progress.update(stage='primary', cursor=None, seen=[], discovery={}, candidates={},
                        items={}, hydrate_done=[], evidence_done=[])

    def _refresh(self, config, end, deferred, *, draft, period_start, required_from, allocated, preparing):
        self._ensure_scope(config)
        started = time.monotonic()
        self._stats = {'candidates':0, 'cached':0, 'batches':0, 'primary':0, 'delta':0,
                       'hydration_calls':0, 'move_pages':0, 'pages':0, 'complete':False}
        try:
            with self._index.locked('generation') as acquired:
                if not acquired:
                    raise SourceRetryError('linear_source_busy', time.time() + 60)
                manifest = self._read('manifest')
                if manifest is None and not preparing:
                    raise TimesheetError('invoice_source_not_prepared')
                progress = self._read('progress')
                mode = 'prepare' if preparing else 'collect'
                base = fingerprint(manifest)
                # Finish checkpointed work rather than discard expensive pages,
                # but a report must then close with a fresh pass in this attempt.
                # Preparation produces only an index with its original watermark;
                # the mandatory subsequent report refresh closes financial input.
                resumed_report = bool(progress and progress.get('base') == base
                                      and progress.get('mode') == mode and not preparing)
                if not progress or progress.get('base') != base or progress.get('mode') != mode:
                    lower = config.beginning if not manifest else timestamp(manifest['watermark']) - timedelta(minutes=5)
                    # Preparation always covers the configured historical beginning.
                    if preparing: lower = config.beginning
                    progress = {'base':base, 'mode':mode, 'started':datetime.fromtimestamp(time.time(), timezone.utc).isoformat(),
                                'lower':lower.isoformat(), 'known':deepcopy((manifest or {}).get('known', {})),
                                'outsiders':deepcopy((manifest or {}).get('outsiders', {})), 'moves':{},
                                'pass':0, 'previous':None}
                    self._new_pass(progress)
                # Resumed discovery uses its original filters; a fresh closing pass
                # reconciles the new cutoff/deferred set before publication.
                progress['known'].update({key: {} for key in deferred if key not in progress['known']})
                if progress['stage'] in {'evidence','complete'} and set(progress['known']) - allocated - set(progress['candidates']):
                    progress['stage'] = 'hydrate'
                with self.source_attempt():
                    while True:
                        filters = self._filters(config, end, period_start, progress['lower'], preparing)
                        self._discover(progress, filters)
                        self._classify(progress, config, allocated)
                        ids = sorted(set(progress['known']) - allocated)
                        self._hydrate(progress, ids)
                        self._evidence(progress, ids)
                        signature = fingerprint({'candidates':{key:fingerprint(value) for key,value in progress['candidates'].items()},
                                                 'ids':ids, 'outsiders':progress['outsiders']})
                        if progress['previous'] == signature and not resumed_report:
                            # Required candidates were read again after the initial
                            # evidence pass; publication is limited to stable inputs.
                            manifest = {'version':VERSION, 'watermark':progress['started'],
                                        'known':progress['known'], 'outsiders':progress['outsiders'],
                                        'evidence':{key:fingerprint(value) for key,value in progress['items'].items()}}
                            self._write('manifest', manifest)
                            self._write('progress', {'base':None, 'mode':'complete'})
                            self._stats['complete'] = True
                            if preparing:
                                return {'status':'prepared', 'known_issues':len(manifest['known']), **self._stats}
                            return [deepcopy(progress['items'][key]) for key in ids]
                        if resumed_report:
                            resumed_report = False
                            progress.update(previous=signature, **{'pass':0})
                            self._new_pass(progress)
                            self._write('progress', progress)
                            continue
                        progress['pass'] += 1
                        if progress['pass'] >= 3:
                            progress.update(previous=None, **{'pass':0})
                            self._new_pass(progress)
                            self._write('progress', progress)
                            self.defer_source()
                        progress['previous'] = signature
                        self._new_pass(progress)
                        self._write('progress', progress)
        finally:
            self._scan, self._ready, self._pending = None, {}, []
            print(json.dumps({'event':'invoice_linear_read', **self._stats,
                              'seconds':round(time.monotonic()-started, 2)}), flush=True)

    def _filters(self, config, end, period_start, lower, preparing):
        primary = {'team': {'id': {'in':self._teams}}, 'project': {'id': {'in':list(config.projects)}},
                   'createdAt': {'lte':end.isoformat()}}
        if not preparing:
            primary['or'] = [{'completedAt': {'gt':period_start.isoformat(), 'lte':end.isoformat()}},
                             {'updatedAt': {'gte':lower}}]
        delta = {'updatedAt': {'gte':lower}}
        return {'primary':primary, 'delta':delta}

    def _discover(self, progress, filters):
        for stage, operation in (('primary','InvoicePeriodIssues'), ('delta','InvoiceChangedIssues')):
            if progress['stage'] != stage:
                continue
            # Pin page filters across attempt boundaries, including a new now.
            if progress['cursor'] is None:
                progress['filters'] = filters[stage]
            seen = set(progress['seen'])
            while True:
                connection = self.linear(listing_query(operation), {'filter':progress['filters'], 'after':progress['cursor']}).get('issues')
                after = next_page(connection, seen)
                for value in connection['nodes']:
                    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not value['id']:
                        raise TimesheetError('invoice_source_coverage')
                    timestamp(value['updatedAt'])
                    progress['discovery'][value['id']] = value
                self._stats[stage] += len(connection['nodes'])
                self._stats['pages'] += 1
                progress.update(cursor=after, seen=sorted(seen))
                if after is None:
                    progress.update(stage='delta' if stage=='primary' else 'classify', seen=[])
                self._write('progress', progress)
                if after is None: break

    def _classify(self, progress, config, allocated):
        if progress['stage'] != 'classify': return
        for key, value in sorted(progress['discovery'].items()):
            if key in allocated: continue
            if key in progress['known'] or (value.get('project') or {}).get('id') in config.projects:
                progress['known'][key] = value
                continue
            stamp = fingerprint(value)
            if progress['outsiders'].get(key) == stamp: continue
            relevant = self._move_history(progress, key, value, config)
            if relevant:
                progress['known'][key] = value
                progress['outsiders'].pop(key, None)
            else:
                progress['outsiders'][key] = stamp
            self._write('progress', progress)
        progress['stage'] = 'hydrate'
        self._write('progress', progress)

    def _move_history(self, progress, key, value, config):
        state = progress['moves'].get(key)
        stamp = fingerprint(value)
        if not state or state['stamp'] != stamp:
            state = {'stamp':stamp, 'cursor':None, 'seen':[], 'events':{}, 'complete':False}
            progress['moves'][key] = state
        if not state['complete']:
            seen = set(state['seen'])
            while True:
                raw = self.linear(MOVE_HISTORY, {'id':key, 'after':state['cursor']}).get('issue')
                if not raw or raw.get('id') != key:
                    raise TimesheetError('invoice_source_coverage')
                if raw.get('updatedAt') != value['updatedAt']:
                    self._invalidate_discovery(progress, key)
                    self.defer_source()
                connection = raw.get('history')
                after = next_page(connection, seen)
                for event in connection['nodes']:
                    if not isinstance(event, dict) or not event.get('id'):
                        raise TimesheetError('invoice_source_coverage')
                    if event['id'] in state['events'] and state['events'][event['id']] != event:
                        raise TimesheetError('inconsistent_history')
                    state['events'][event['id']] = event
                state.update(cursor=after, seen=sorted(seen), complete=after is None)
                self._stats['move_pages'] += 1
                self._write('progress', progress)
                if after is None: break
        query, variables = batch_query([key], versions=True)
        checked = self.linear(query.replace('StudioEvidenceVersions','InvoiceIssueVersions'), variables).get('i0')
        if not checked or checked.get('id') != key:
            raise TimesheetError('invoice_source_coverage')
        if checked.get('updatedAt') != value['updatedAt']:
            self._invalidate_discovery(progress, key)
            self.defer_source()
        return any(h.get('fromProjectId') in config.projects or h.get('toProjectId') in config.projects
                   for h in state['events'].values())

    def _invalidate_discovery(self, progress, key):
        # A new history version cannot be compared forever with an old listing.
        # Re-list on retry, retaining unrelated move pages and full evidence.
        progress['moves'].pop(key, None)
        progress.update(stage='primary', cursor=None, seen=[], previous=None)
        self._write('progress', progress)

    def _hydrate(self, progress, ids):
        if progress['stage'] != 'hydrate': return
        pending = [key for key in ids if key not in progress['hydrate_done']]
        for start in range(0,len(pending),HYDRATE_BATCH_SIZE):
            batch = pending[start:start+HYDRATE_BATCH_SIZE]
            query, variables = candidate_query(batch)
            raw = self.linear(query, variables)
            self._stats['hydration_calls'] += 1
            for i, key in enumerate(batch):
                issue = raw.get(f'i{i}')
                if not issue or issue.get('id') != key or issue.get('trashed'):
                    raise TimesheetError('invoice_source_coverage')
                connection = issue['labels']
                self._page_versions[key] = issue['updatedAt']
                seen, labels = set(), {}
                while True:
                    after = next_page(connection, seen)
                    labels.update({v['id']:v['name'] for v in connection['nodes']})
                    if after is None: break
                    checked = self.linear(LABEL_PAGE, {'id':key,'after':after}).get('issue')
                    if not checked or checked.get('updatedAt') != issue['updatedAt']:
                        self.defer_source()
                    connection = checked['labels']
                if seen:
                    issue['labels'] = {'nodes':[{'id':k,'name':v} for k,v in labels.items()],
                                       'pageInfo':{'hasNextPage':False,'endCursor':None}}
                progress['candidates'][key] = issue
                progress['hydrate_done'].append(key)
            self._write('progress', progress)
        progress['stage'] = 'evidence'
        self._write('progress', progress)

    def _evidence(self, progress, ids):
        if progress['stage'] != 'evidence': return
        self.prepare_evidence(progress['candidates'])
        for key in ids:
            if key in progress['evidence_done']: continue
            original = progress['candidates'][key]
            try:
                value = self.evidence(key)
                if fingerprint(value['issue']) != fingerprint(normalized(original)):
                    # Never treat an old hydrated candidate and new history as stable.
                    progress['candidates'][key] = {**value['issue'], 'labels':{
                        'nodes':value['issue']['labels'], 'pageInfo':{'hasNextPage':False,'endCursor':None}}}
                else:
                    with self._cache.locked(fingerprint(key)) as acquired:
                        if acquired:
                            self._cache.write(fingerprint(key), {'candidate':fingerprint(original),
                                'evidence':value, 'digest':fingerprint(value)})
            except SourceRetryError: raise
            except TimesheetError as exc:
                if str(exc) in {'issue_unavailable','invalid_linear_cursor','incomplete_linear_page'}:
                    raise TimesheetError('invoice_source_coverage') from None
                if str(exc) == 'issue_changed_during_read':
                    self.defer_source()
                value = {'issue':normalized(original), 'history':[], 'error':str(exc)}
            progress['items'][key] = value
            progress['evidence_done'].append(key)
            self._write('progress', progress)
        progress['stage'] = 'complete'
        self._write('progress', progress)
