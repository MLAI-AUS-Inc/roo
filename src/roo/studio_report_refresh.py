"""Private source barrier shared by both recipients of a Friday report."""
from datetime import datetime, time, timedelta, timezone
import hashlib
import hmac
import json
from uuid import uuid4

from .payment_reminders import ReceiptStore
from .studio_report_clients import select_client
from .studio_report_schedule import TZ
from .timesheets import TimesheetError, fingerprint, flag, timestamp


class SourcePending(TimesheetError):
    def __init__(self, retry_at):
        super().__init__('studio_source_waiting_data')
        self.retry_at = retry_at


class SourceRefresh:
    def __init__(self, config, clients, http, url, key):
        if not url.startswith('https://') or len(key) < 32:
            raise TimesheetError('invalid_studio_source_configuration')
        self.config, self.clients, self.http, self.url, self.key = config, clients, http, url, key
        self.store = ReceiptStore(config.directory / 'source-preparations')

    @classmethod
    def from_env(cls, env, config, clients, http):
        if not flag(env, 'STUDIO_REPORTS_REFRESH_ENABLED'):
            return None
        if not flag(env, 'STUDIO_CLIENT_BALANCE_WORKER_ENABLED') or not env.get('STUDIO_CLIENT_BALANCE_FILE'):
            raise TimesheetError('studio_source_requires_canonical_hours')
        return cls(config, clients, http, env.get('STUDIO_REPORTS_SOURCE_URL', ''),
                   env.get('STUDIO_REPORTS_SOURCE_KEY', ''))

    def identity(self, owner, cutoff):
        return fingerprint({'owner': owner, 'cutoff': timestamp(cutoff).isoformat(),
            'team': self.config.team, 'organization': self.config.organization,
            'project_ids': sorted(self.clients[owner]['project_ids'])})

    def validate(self, result, owner, cutoff, now):
        if (result.get('version') != 1 or result.get('owner') != owner
                or result.get('team') != self.config.team
                or result.get('organization') != self.config.organization
                or result.get('project_ids') != sorted(self.clients[owner]['project_ids'])
                or timestamp(result['cutoff']) != timestamp(cutoff)
                or timestamp(result['checked_at']) > timestamp(now) + timedelta(minutes=5)):
            raise TimesheetError('studio_source_scope_mismatch')
        if result.get('status') == 'ready':
            checks = result['checks']
            if (result.get('missing') or timestamp(result['checked_at']) < timestamp(cutoff)
                    or set(checks) != {'hours', 'contractor_invoices', 'client_invoices', 'payments'}
                    or any(timestamp(check['checked_at']) < timestamp(cutoff) for check in checks.values())
                    or timestamp(checks['hours']['complete_through']) < timestamp(cutoff)
                    or timestamp(checks['hours']['reviewed_at']) < timestamp(cutoff)
                    or checks['contractor_invoices'].get('unreviewed_count') != 0):
                raise TimesheetError('studio_source_coverage_invalid')
            from .studio_client_balance_source import validate_balance_snapshot
            snapshot = validate_balance_snapshot(result['snapshot'], self.config, self.clients)
            if set(snapshot['clients']) != {owner}:
                raise TimesheetError('studio_source_scope_mismatch')
            return snapshot
        if result.get('status') != 'waiting_data' or not result.get('missing'):
            raise TimesheetError('studio_source_response_invalid')
        return None

    def prepare(self, owner, cutoff, now):
        key = self.identity(owner, cutoff)
        with self.store.locked(key) as acquired:
            if not acquired:
                raise SourcePending(timestamp(now).timestamp() + 60)
            cached = self.store.read(key)
            if cached and cached.get('result'):
                snapshot = self.validate(cached['result'], owner, cutoff, now)
                if snapshot is not None:
                    return key, snapshot, cached['result']['checks']
            # A warm-up can never suppress the closing pass at/after 17:00.
            closing = timestamp(now) >= timestamp(cutoff)
            if (cached and timestamp(now).timestamp() < cached.get('retry_at', 0)
                    and not (closing and not cached.get('closing'))):
                raise SourcePending(cached['retry_at'])
            retry_at = timestamp(now).timestamp() + 300
            receipt = {'status': 'waiting_data', 'closing': closing, 'retry_at': retry_at}
            try:
                nonce = fingerprint({'random': uuid4().hex})
                response = self.http.post(self.url, headers={'X-Studio-Source-Key': self.key},
                    json={'owner': owner, 'team': self.config.team, 'cutoff': timestamp(cutoff).isoformat(),
                          'nonce': nonce}, timeout=180)
                response.raise_for_status()
                envelope = response.json()
                result = envelope['result']
                body = json.dumps(result, sort_keys=True, separators=(',', ':'))
                signature = hmac.new(self.key.encode(), body.encode(), hashlib.sha256).hexdigest()
                if (not hmac.compare_digest(signature, envelope['signature']) or result.get('nonce') != nonce):
                    raise TimesheetError('studio_source_signature_invalid')
                snapshot = self.validate(result, owner, cutoff, datetime.now(timezone.utc))
                receipt.update(result=result, status=result['status'])
            except Exception as exc:
                receipt['error'] = str(exc) if isinstance(exc, TimesheetError) else 'studio_source_unavailable'
                self.store.write(key, receipt)
                raise SourcePending(retry_at) from exc
            self.store.write(key, receipt)
            if snapshot is None:
                raise SourcePending(retry_at)
            return key, snapshot, result['checks']

    def for_request(self, request, now):
        _, _, targets = select_client(self.clients, request['actor'], request['selector'])
        owners = list(targets) or [request['actor']]
        if len(owners) != 1:
            raise TimesheetError('weekly_client_required')
        return self.prepare(owners[0], request['selector']['end'], now)

    def frozen(self, key, owner, cutoff, now):
        if key != self.identity(owner, cutoff):
            raise TimesheetError('studio_source_scope_mismatch')
        cached = self.store.read(key)
        if not cached or cached.get('status') != 'ready':
            raise TimesheetError('studio_source_not_ready')
        snapshot = self.validate(cached['result'], owner, cutoff, now)
        if snapshot is None:
            raise TimesheetError('studio_source_not_ready')
        return snapshot

    def reject_projection(self, key):
        # No report has been frozen or delivered yet. A corrected manifest must
        # be fetched on retry instead of reusing a ready but unusable snapshot.
        with self.store.locked(key) as acquired:
            if not acquired:
                return
            receipt = self.store.read(key)
            if receipt and receipt.get('result'):
                result = receipt['result']
                result.update(status='waiting_data', missing=['work_dates_require_review'])
                result.pop('snapshot', None)
                receipt['status'] = 'waiting_data'
                self.store.write(key, receipt)

    def prewarm(self, schedules, now):
        local = timestamp(now).astimezone(TZ)
        if local.weekday() != 4:
            return
        for schedule in schedules:
            due = datetime.combine(local.date(), time.fromisoformat(schedule['time']), TZ)
            if (due - timedelta(minutes=30) <= local < due
                    and (not schedule['starts_on'] or local.date().isoformat() >= schedule['starts_on'])):
                try:
                    self.prepare(schedule['client'], due, now)
                except (TimesheetError, OSError, ValueError, KeyError):
                    pass
