"""Daily private refresh. Reads sources and publishes files; never sends messages."""
from datetime import time, timedelta
import json
import os
from pathlib import Path
import tempfile

from .payment_reminders import ReceiptStore
from .studio_client_balance_source import load_balance_snapshot, validate_balance_snapshot
from .studio_report_refresh import SourcePending
from .studio_report_schedule import TZ
from .timesheets import TimesheetError, fingerprint, flag, timestamp


def private_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.refresh-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content if isinstance(content, bytes) else content.encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_bundle(service, owner, snapshot, now):
    from .studio_client_balance import build_balance_report, artifacts, summary, render_chart
    from .studio_recorded_hours import build_recorded_report
    from .studio_reports import artifacts as hours_artifacts, summary as hours_summary, render_chart as hours_chart
    dataset, client = snapshot['clients'][owner], service.clients[owner]
    selector = {'month': 'all', 'action': 'detailed'}
    balance = build_balance_report(client, selector, dataset, now)
    hours = build_recorded_report(service.config, client, selector, dataset, now)
    return {'snapshot.json': json.dumps(snapshot),
            'reports.json': json.dumps({'balance': balance, 'hours': hours}),
            'report.txt': summary(balance) + '\n\n' + hours_summary(hours),
            'studio-client-balance.png': render_chart(balance),
            'studio-hours.png': hours_chart(hours),
            'studio-hours-projects.png': hours_chart(hours, breakdown='projects'),
            **artifacts(balance), **hours_artifacts(hours)}


class DailyRefresh:
    def __init__(self, service, owners, at='06:00'):
        if not service.refresh or not service.refresh.latest_enabled or not service.balance_path:
            raise TimesheetError('daily_refresh_requires_latest_source')
        if not owners or len(set(owners)) != len(owners) or any(owner not in service.clients for owner in owners):
            raise TimesheetError('invalid_daily_refresh_owners')
        self.service, self.owners = service, owners
        try:
            self.at = time.fromisoformat(at)
            if self.at.tzinfo or self.at.second or self.at.microsecond:
                raise ValueError
        except ValueError as exc:
            raise TimesheetError('invalid_daily_refresh_time') from exc
        self.store = ReceiptStore(service.config.directory / 'daily-refresh')

    @classmethod
    def from_env(cls, service, env):
        if not flag(env, 'STUDIO_REPORTS_DAILY_REFRESH_ENABLED'):
            return None
        try:
            owners = json.loads(env.get('STUDIO_REPORTS_DAILY_REFRESH_OWNERS_JSON', '[]'))
            if not isinstance(owners, list) or any(not isinstance(owner, str) for owner in owners):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise TimesheetError('invalid_daily_refresh_owners') from exc
        return cls(service, owners, env.get('STUDIO_REPORTS_DAILY_REFRESH_TIME', '06:00'))

    def tick(self, now, *, catch_up=False):
        local = timestamp(now).astimezone(TZ)
        day = local.date() - timedelta(days=int(local.time() < self.at))
        results = []
        for owner in self.owners:
            key = fingerprint({'owner': owner, 'day': day.isoformat()})
            with self.store.locked(key) as acquired:
                if not acquired:
                    continue
                previous = self.store.read(key) or {}
                # Before today's run, only resume an existing pending run.
                # First activation waits until its configured local time.
                if local.time() < self.at and not previous and not catch_up:
                    continue
                if previous.get('status') == 'done' or previous.get('retry_at', 0) > timestamp(now).timestamp():
                    continue
                receipt = {'day': day.isoformat(), 'attempted_at': timestamp(now).isoformat()}
                try:
                    _, snapshot, checks = self.service.refresh.prepare(owner, now, now, mode='latest')
                    bundle = render_bundle(self.service, owner, snapshot, now)
                    directory = (self.service.config.directory / 'daily-reports' / owner /
                                 day.isoformat() / fingerprint(snapshot))
                    for name, content in bundle.items():
                        private_write(directory / name, content)
                    # A shared publication lock prevents concurrent owners losing updates.
                    with self.store.locked('publication') as publish:
                        if not publish:
                            raise TimesheetError('daily_publication_busy')
                        current = load_balance_snapshot(self.service.balance_path, self.service.config, self.service.clients)
                        if owner not in current['clients']:
                            raise TimesheetError('daily_finance_scope_missing')
                        current['clients'][owner] = snapshot['clients'][owner]
                        current['evidence'].update(snapshot['evidence'])
                        validate_balance_snapshot(current, self.service.config, self.service.clients)
                        private_write(self.service.balance_path, json.dumps(current))
                        self.store.write(fingerprint({'latest': owner}), {'directory': str(directory), 'checks': checks})
                    receipt.update(status='done', directory=str(directory), checks=checks,
                                   missing=snapshot['clients'][owner]['source_freshness']['missing'])
                except SourcePending as exc:
                    receipt.update(status='waiting_data', retry_at=exc.retry_at)
                except Exception:
                    receipt.update(status='error', retry_at=timestamp(now).timestamp() + 300,
                                   reason='daily_refresh_failed')
                self.store.write(key, receipt)
                results.append({'status': receipt['status'], 'job': 'studio_daily_refresh',
                                **({'retry_at': receipt['retry_at']} if 'retry_at' in receipt else {})})
        return results
