"""Resumable, version-checked Linear reads in the private Studio worker."""
from copy import deepcopy
from email.utils import parsedate_to_datetime
import json
import math
import time

import httpx

from .payment_reminders import ReceiptStore
from .timesheet_linear import FIELDS, HISTORY_FIELDS, next_page
from .timesheet_worker import ReportAPI
from .timesheets import SourceRetryError, TimesheetError, fingerprint

BATCH_SIZE = 8


def retry_time(headers, now):
    """Linear uses HTTP 400 + RATELIMITED and millisecond reset headers."""
    retry = now + 60
    value = headers.get('retry-after')
    if value:
        try:
            delay = float(value)
            candidate = now + delay
        except ValueError:
            try:
                candidate = parsedate_to_datetime(value).timestamp()
            except (ValueError, TypeError, OverflowError):
                candidate = retry
        if math.isfinite(candidate):
            retry = max(retry, candidate)
    for dimension in ('requests', 'complexity', 'endpoint-requests'):
        prefix = 'x-ratelimit-' + dimension
        try:
            remaining = float(headers[prefix + '-remaining'])
            reset = float(headers[prefix + '-reset']) / 1000
            if remaining <= 0 and math.isfinite(reset):
                retry = max(retry, reset)
        except (KeyError, ValueError, TypeError):
            pass
    return retry


def batch_query(ids, *, versions=False):
    fields = 'id updatedAt' if versions else FIELDS + '''
 history(first:50, includeArchived:true, orderBy:createdAt) {
   nodes { ''' + HISTORY_FIELDS + ''' }
   pageInfo { hasNextPage endCursor }
 }'''
    variables = {f'id{i}': key for i, key in enumerate(ids)}
    arguments = ', '.join(f'${key}: String!' for key in variables)
    selections = ' '.join(f'i{i}: issue(id:$id{i}) {{ {fields} }}' for i in range(len(ids)))
    operation = 'StudioEvidenceVersions' if versions else 'StudioEvidenceBatch'
    return f'query {operation}({arguments}) {{ {selections} }}', variables


class StudioSourceAPI(ReportAPI):
    def linear(self, query, variables=None):
        try:
            response = self.client.post('https://api.linear.app/graphql',
                headers={'Authorization': self.linear_key}, json={'query': query, 'variables': variables or {}})
        except httpx.RequestError as exc:
            raise SourceRetryError('linear_transport_error', time.time() + 60) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        errors = (body.get('errors') or []) if isinstance(body, dict) else []
        limited = any(isinstance(error, dict) and (error.get('extensions') or {}).get('code') == 'RATELIMITED'
                      for error in errors)
        if response.status_code == 429 or limited:
            raise SourceRetryError('linear_rate_limited', retry_time(response.headers, time.time()))
        if response.is_error:
            raise SourceRetryError(f'linear_http_{response.status_code}', time.time() + 60)
        if not isinstance(body, dict) or errors or not isinstance(body.get('data'), dict):
            raise SourceRetryError('linear_invalid_response', time.time() + 60)
        return body['data']

    def collect(self, config, end, deferred):
        # Credentials and organization partition evidence. The cache is never
        # mounted in Public Roo; fresh candidate reads still gate every report.
        namespace = fingerprint({'version': 1, 'team': config.team,
                                 'organization': config.organization, 'credential': self.linear_key})
        self._cache = ReceiptStore(config.directory / 'linear-evidence' / namespace)
        started = time.monotonic()
        self._stats = {'candidates': 0, 'cached': 0, 'batches': 0, 'complete': False}
        try:
            result = super().collect(config, end, deferred)
            self._stats['complete'] = True
            return result
        finally:
            print(json.dumps({'event': 'studio_linear_read', **self._stats,
                              'seconds': round(time.monotonic() - started, 2)}), flush=True)
            self._scan = None
            self._ready = {}
            self._pending = []

    def prepare_evidence(self, issues):
        self._scan, self._ready, self._pending = issues, {}, []
        self._stats['candidates'] = len(issues)
        for issue_id, original in issues.items():
            try:
                cached = self._cache.read(fingerprint(issue_id))
            except (OSError, ValueError):
                cached = None  # Corrupt cache entries require fresh evidence.
            if (isinstance(cached, dict) and cached.get('candidate') == fingerprint(original)
                    and isinstance(cached.get('evidence'), dict)
                    and cached.get('digest') == fingerprint(cached['evidence'])):
                self._ready[issue_id] = cached['evidence']
                self._stats['cached'] += 1
            else:
                self._pending.append(issue_id)

    def evidence(self, issue_id):
        if getattr(self, '_scan', None) is None:
            return super().evidence(issue_id)
        while issue_id not in self._ready:
            batch = self._pending[:BATCH_SIZE]
            if not batch:
                raise TimesheetError('issue_unavailable')
            del self._pending[:BATCH_SIZE]
            self._read_batch(batch)
        value = self._ready[issue_id]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    def _read_batch(self, ids):
        self._stats['batches'] += 1
        query, variables = batch_query(ids)
        data = self.linear(query, variables)
        query, variables = batch_query(ids, versions=True)
        versions = self.linear(query, variables)
        for index, issue_id in enumerate(ids):
            raw, checked = data.get(f'i{index}'), versions.get(f'i{index}')
            try:
                if not isinstance(raw, dict) or raw.get('id') != issue_id:
                    raise TimesheetError('issue_unavailable')
                issue = {k: v for k, v in raw.items() if k != 'history'}
                # Fall back to the full paginated reader if either connection
                # is longer or the issue changed during the batched read.
                if (not isinstance(checked, dict) or checked.get('id') != issue_id
                        or checked.get('updatedAt') != issue.get('updatedAt')
                        or next_page(raw['history'], set()) is not None
                        or next_page(issue['labels'], set()) is not None):
                    item = super().evidence(issue_id)
                else:
                    history = {}
                    for event in raw['history']['nodes']:
                        if event['id'] in history and history[event['id']] != event:
                            raise TimesheetError('inconsistent_history')
                        history[event['id']] = event
                    item = {'issue': {**issue, 'labels': issue['labels']['nodes']}, 'history': list(history.values())}
                self._ready[issue_id] = item
                # Never bind newer evidence to an older candidate version.
                original = self._scan[issue_id]
                if (fingerprint(issue) == fingerprint(original)
                        and item['issue']['updatedAt'] == original['updatedAt']):
                    key = fingerprint(issue_id)
                    with self._cache.locked(key) as acquired:
                        if acquired:
                            self._cache.write(key, {'candidate': fingerprint(original),
                                                   'evidence': item, 'digest': fingerprint(item)})
            except SourceRetryError:
                raise
            except TimesheetError as exc:
                self._ready[issue_id] = exc
