"""Private on-demand and weekly client reports, isolated from the payroll ledger."""
from __future__ import annotations

import argparse
import base64
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import threading
from types import SimpleNamespace
from uuid import UUID

import httpx

from .studio_reports import (allowance_units, artifacts, build_client_report, detail_messages,
                             month_offset, render_chart, resolve_period, split_messages, summary)
from .timesheet_worker import TimesheetService
from .studio_report_source import StudioSourceAPI
from .studio_report_backfill import history_beginning, load_backfill
from .studio_report_clients import select_client, validate_client_reporting
from .studio_report_policy import filter_backfill, linear_projects, validate_hours_policy
from .studio_report_schedule import enqueue_weekly, scheduled_request_authorized, weekly_configuration
from .payment_reminders import ReceiptStore
from .timesheets import TimesheetConfig, TimesheetError, fingerprint, flag, timestamp


def configuration(env):
    """Reuse the existing read-only source mappings; never enable payroll writes."""
    config = TimesheetConfig.from_env({**env, 'TIMESHEETS_ENABLED': 'false',
                                      'TIMESHEETS_SCHEDULE_ENABLED': 'false'})
    config = replace(config, team=env.get('STUDIO_REPORTS_SLACK_TEAM_ID', ''),
                     directory=Path(env.get('STUDIO_REPORTS_DATA_DIR', '/app/studio-reports/data')),
                     queue=Path(env.get('STUDIO_REPORTS_QUEUE_DIR', '/app/studio-reports/queue')))
    if config.source != 'linear':
        raise TimesheetError('client_reports_require_verified_linear_coverage')
    if not re.fullmatch(r'T[A-Z0-9]+', config.team):
        raise TimesheetError('invalid_workspace')
    try:
        UUID(config.organization)
        raw = json.loads(env.get('STUDIO_REPORTS_CLIENTS_JSON', '{}'))
    except (ValueError, TypeError) as exc:
        raise TimesheetError('invalid_client_configuration') from exc
    if not isinstance(raw, dict) or not raw:
        raise TimesheetError('client_mappings_required')
    for actor, client in raw.items():
        if (not re.fullmatch(r'[UW][A-Z0-9]+', actor) or not isinstance(client, dict)
                or not isinstance(client.get('name'), str) or not client['name'].strip()
                or not isinstance(client.get('project_ids'), list) or not client['project_ids']
                or any(not isinstance(key, str) or key not in config.projects for key in client['project_ids'])
                or len(set(client['project_ids'])) != len(client['project_ids'])):
            raise TimesheetError('invalid_client_mapping')
        allowance_units(client.get('monthly_hours', 40))
        overrides = client.get('monthly_allowances', {})
        if not isinstance(overrides, dict):
            raise TimesheetError('invalid_monthly_allowances')
        for month, value in overrides.items():
            month_offset(month, 0)
            allowance_units(value)
    validate_client_reporting(raw)
    validate_hours_policy(raw)
    return config, raw


class StudioReportAPI(StudioSourceAPI):
    def upload_csv(self, channel, filename, content):
        if filename.endswith('.png'):
            content = base64.b64decode(content, validate=True)
        return super().upload_csv(channel, filename, content)


class StudioReportService(TimesheetService):
    def __init__(self, config, api, clients, backfill_path=None, *, balance_path=None,
                 balance_enabled=False, balance_max_age_days=7, schedules=None,
                 allow_historical_snapshot=False, refresh=None):
        super().__init__(config, api)
        self.clients = clients
        self.backfill_path = backfill_path
        self.balance_path = balance_path
        self.balance_enabled = balance_enabled
        self.balance_max_age_days = balance_max_age_days
        self.allow_historical_snapshot = allow_historical_snapshot
        self.schedules = schedules or []
        self.refresh = refresh
        self.weekly_queue = ReceiptStore(config.directory / 'weekly-queue')

    def request_authorized(self, request):
        # Unknown clients receive a private, generic setup message. No source read.
        verified = (request.get('team') == self.config.team
                    and re.fullmatch(r'[UW][A-Z0-9]+', str(request.get('actor', ''))) is not None)
        if not verified:
            return False
        if not isinstance(request.get('selector'), dict):
            return False
        if request.get('scheduled') is not None or request['selector'].get('period') == 'weekly':
            return scheduled_request_authorized(request, self.schedules, service=self)
        return True

    def request_key_authorized(self, request, key):
        if 'scheduled' in request:
            return (isinstance(request['scheduled'], dict)
                    and request['scheduled'].get('request_key') == key)
        return True

    def commands(self, now):
        results = super().commands(now)
        results.extend(super().commands(now, queue=self.weekly_queue))
        return results

    def request_error_status(self, request, code):
        return 'waiting_data' if code == 'studio_source_waiting_data' else 'error'

    def scope_fingerprint(self, actor, selector=None):
        if actor not in self.clients:
            raise TimesheetError('client_access_not_configured')
        client = self.clients[actor]
        _, groups, targets = select_client(self.clients, actor, selector or {})
        scope = {'actor': actor, 'team': self.config.team, 'organization': self.config.organization,
                 'client': client, 'projects': {key: self.config.projects[key] for key in client['project_ids']}}
        if targets:
            scope.update(targets=targets, groups=groups)
        # Staff self-overviews also inherit source policies from their explicit
        # owner grants. A changed owner policy must invalidate a saved report.
        task_projects = linear_projects(self.clients, actor, selector or {})
        if task_projects:
            scope['linear_policies'] = {key: self.clients[key] for key in
                [actor, *client.get('report_client_ids', [])]
                if self.clients[key].get('hours_source') == 'linear'
                and set(self.clients[key]['project_ids']) & task_projects}
        return fingerprint(scope)

    def report_for_request(self, request):
        if not self.request_authorized(request):
            raise TimesheetError('not_authorized')
        kind = request.get('report_kind', 'hours')
        if kind == 'client_balance':
            return self.balance_for_request(request)
        if kind != 'hours':
            raise TimesheetError('invalid_report_kind')
        actor = request['actor']
        now = timestamp(request['requested_at'])
        selector, start, _ = resolve_period(request['selector'], now)
        # A delayed Friday report can use a later reviewed snapshot. Freshness
        # is checked at the trusted read time; report bounds remain frozen.
        freshness_at = datetime.now(timezone.utc) if selector.get('period') == 'weekly' else now
        scope = self.scope_fingerprint(actor, selector)
        client, groups, targets = select_client(self.clients, actor, selector)
        task_projects = linear_projects(self.clients, actor, selector)
        task_only = set(client['project_ids']) <= task_projects
        owners = targets or {key: self.clients[key] for key in
                              [actor, *self.clients[actor].get('report_client_ids', [])]}
        reviewed, finance_owners, snapshot = [], [], None
        preparation_key, source_checks = None, None
        source_stale = False
        if selector.get('period') == 'weekly' and self.refresh and not task_only:
            # Source preparation is private and shared by owner + frozen cutoff,
            # not repeated independently for Mark and Sam.
            preparation_key, snapshot, source_checks = self.refresh.for_request(request, freshness_at)
        if self.balance_enabled and self.balance_path and not task_only:
            from .studio_client_balance_source import load_balance_snapshot, validate_snapshot_age
            from .studio_recorded_hours import build_recorded_report
            snapshot = snapshot or load_balance_snapshot(self.balance_path, self.config, self.clients)
            for owner, owner_client in owners.items():
                data = snapshot['clients'].get(owner)
                if (data is not None and data.get('hours_reporting')
                        and set(owner_client['project_ids']) <= set(client['project_ids'])
                        and not set(owner_client['project_ids']) & task_projects):
                    expired = validate_snapshot_age(data, freshness_at,
                        max_age_days=self.balance_max_age_days,
                        allow_historical=self.allow_historical_snapshot)
                    source_stale = source_stale or expired
                    finance_owners.append(owner)
        covered = {key for owner in finance_owners for key in self.clients[owner]['project_ids']}
        legacy_projects = set(client['project_ids']) - covered
        backfill_projects = legacy_projects - task_projects
        backfill = (filter_backfill(load_backfill(self.backfill_path, self.config), task_projects)
                    if backfill_projects else None)
        if start is None:
            beginnings = [snapshot['clients'][owner]['coverage']['start'][:7] for owner in finance_owners]
            for owner_client in owners.values():
                if (owner_client.get('hours_source') == 'linear'
                        and set(owner_client['project_ids']) & task_projects):
                    beginning = owner_client.get('history_start_month')
                    if not beginning:
                        raise TimesheetError('all_time_coverage_not_configured')
                    beginnings.append(beginning)
            if backfill_projects or not beginnings:
                beginnings.append(history_beginning(backfill, list(backfill_projects or client['project_ids'])))
            selector, start, _ = resolve_period(selector, now,
                beginning=min(beginnings))
        month_selector, month_start = None, None
        if selector.get('period') == 'weekly':
            month_selector, month_start, _ = resolve_period({
                'period': 'month_to_date', 'end': selector['end'], 'action': 'summary',
                **({'client': selector['client']} if selector.get('client') else {})}, now)
        # The collection window must include project moves since the requested
        # month, even for months older than the payroll worker's initial cutoff.
        source = SimpleNamespace(team=self.config.team, organization=self.config.organization,
                                 projects={key: self.config.projects[key] for key in legacy_projects},
                                 beginning=min(start, month_start) if month_start else start,
                                 directory=self.config.directory)
        self.api.verify_recipient(actor, self.config.team)
        dataset = []
        if legacy_projects:
            self.api.verify(source)
            dataset = self.api.collect(source, now, {})
        report = build_client_report(self.config, client, selector, dataset, now, backfill)
        month_report = (build_client_report(self.config, client, month_selector, dataset, now, backfill)
                        if month_selector else None)
        if finance_owners:
            from .studio_recorded_hours import overlay_recorded_reports
            reviewed = [build_recorded_report(self.config, self.clients[owner], selector,
                        snapshot['clients'][owner], now) for owner in finance_owners]
            report = overlay_recorded_reports(report, reviewed)
            report.update(finance_digest=fingerprint(snapshot), finance_owners=finance_owners,
                          canonical_only=not legacy_projects, source_stale=source_stale)
            if month_report is not None:
                month_reviewed = [build_recorded_report(self.config, self.clients[owner], month_selector,
                                  snapshot['clients'][owner], now) for owner in finance_owners]
                month_report = overlay_recorded_reports(month_report, month_reviewed)
                month_report['source_stale'] = source_stale
        if groups:
            report['client_groups'] = groups
        report.update(actor=actor, scope=scope, backfill_digest=fingerprint(backfill),
                      backfill_source_used=bool(backfill_projects),
                      linear_project_ids=sorted(task_projects))
        if task_only:
            report['hours_source'] = 'linear'
        if month_report is not None:
            report['month_to_date'] = month_report
        if preparation_key:
            # Incomplete source quantities/timing cannot become a zero weekly
            # bar or a completed delivery, even after a successful finance sync.
            if (not finance_owners or report.get('partial') or report.get('exceptions')
                    or not report.get('period_has_coverage')):
                from .studio_report_refresh import SourcePending
                self.refresh.reject_projection(preparation_key)
                raise SourcePending(datetime.now(timezone.utc).timestamp() + 300)
            report.update(preparation_key=preparation_key, source_checks=source_checks)
        # Freeze rendered parts too. A delivery retry uses the same content hash
        # even when font versions or optional chart availability change.
        parts = [{'kind': 'message', 'content': chunk, 'status': 'pending'} for chunk in split_messages(summary(report))]
        if task_only:
            parts.append({'kind': 'message', 'content':
                'Task-size estimates are compared with the base monthly allowance. '
                'Contractual rollover is not included; this is not a clock-time ledger or confirmed contractual entitlement.'
                + (' Unresolved completed tasks mean the balance is unconfirmed.' if not report['complete'] else ''),
                'status': 'pending'})
        if source_checks:
            checked = min(check['checked_at'] for check in source_checks.values())
            parts.insert(0, {'kind': 'message', 'content':
                f"Sources checked: {timestamp(checked).astimezone(self.config.tz).isoformat()}. "
                f"Reporting cutoff: {selector['end']}. Hours, invoices and payments use one verified snapshot.",
                'status': 'pending'})
        try:
            if month_report is not None:
                for period_report, filename in [(report, 'studio-hours-week.png'),
                                                (month_report, 'studio-hours-month-to-date.png')]:
                    if period_report is month_report:
                        parts.extend({'kind': 'message', 'content': chunk, 'status': 'pending'}
                                     for chunk in split_messages(summary(month_report)))
                    parts.append({'kind': 'file', 'filename': filename,
                                  'content': base64.b64encode(render_chart(period_report)).decode('ascii'),
                                  'status': 'pending'})
                if preparation_key:
                    from .studio_client_balance import build_balance_report, render_chart as balance_chart, summary as balance_summary
                    owner = finance_owners[0]
                    payment_report = build_balance_report(self.clients[owner], month_selector,
                        snapshot['clients'][owner], request['requested_at'])
                    report['payment_report'] = payment_report
                    parts.extend({'kind': 'message', 'content': chunk, 'status': 'pending'}
                                 for chunk in split_messages(balance_summary(payment_report)))
                    parts.append({'kind': 'file', 'filename': 'studio-client-payment-burndown.png',
                                  'content': base64.b64encode(balance_chart(payment_report)).decode('ascii'),
                                  'status': 'pending'})
            else:
                chart = render_chart(report)
                parts.append({'kind': 'file', 'filename': 'studio-hours.png',
                              'content': base64.b64encode(chart).decode('ascii'), 'status': 'pending'})
                if selector.get('client') and len(report['monthly']) > 1 and selector.get('period') != 'weekly':
                    parts.append({'kind': 'file', 'filename': 'studio-hours-projects.png',
                                  'content': base64.b64encode(render_chart(report, breakdown='projects')).decode('ascii'), 'status': 'pending'})
                if groups:
                    parts.append({'kind': 'file', 'filename': 'studio-hours-clients.png',
                                  'content': base64.b64encode(render_chart(report, breakdown='clients')).decode('ascii'), 'status': 'pending'})
                if task_only:
                    from .studio_task_burndown import render_task_burndown
                    burndown = render_task_burndown(report)
                    if burndown is not None:
                        parts.append({'kind': 'file', 'filename': 'studio-hours-burndown.png',
                                      'content': base64.b64encode(burndown).decode('ascii'), 'status': 'pending'})
        except Exception as exc:
            if month_report is not None:
                raise TimesheetError('scheduled_report_chart_unavailable') from exc
            parts.append({'kind': 'message', 'content': 'The chart is unavailable this time; the full totals are above.', 'status': 'pending'})
        if selector['action'] == 'detailed':
            parts.extend({'kind': 'message', 'content': chunk, 'status': 'pending'} for chunk in detail_messages(report))
        parts.extend({'kind': 'file', 'filename': name, 'content': content, 'status': 'pending'}
                     for name, content in artifacts(report).items())
        if month_report is not None:
            parts.extend({'kind': 'file', 'filename': name.replace('studio-hours-', 'studio-hours-month-to-date-', 1),
                          'content': content, 'status': 'pending'} for name, content in artifacts(month_report).items())
        report['parts'] = parts
        return report

    def balance_for_request(self, request):
        from .studio_client_balance import artifacts as balance_artifacts
        from .studio_client_balance import build_balance_report, render_chart as balance_chart, summary as balance_summary
        from .studio_client_balance_source import (load_balance_snapshot, select_balance_dataset,
                                                  validate_snapshot_age)

        if not self.balance_enabled:
            raise TimesheetError('client_balance_not_configured')
        actor, selector = request['actor'], request['selector']
        # Resolve access before touching finance records. The Public runtime
        # only queues identity and selection; this file is in the private volume.
        scope = self.scope_fingerprint(actor, selector)
        snapshot = load_balance_snapshot(self.balance_path, self.config, self.clients)
        client, dataset, owner = select_balance_dataset(snapshot, self.config, self.clients,
            actor, selector, request['requested_at'], max_age_days=self.balance_max_age_days,
            allow_historical=self.allow_historical_snapshot)
        self.api.verify_recipient(actor, self.config.team)
        report = build_balance_report(client, selector, dataset, request['requested_at'])
        report.update(actor=actor, scope=scope, finance_owner=owner,
                      finance_digest=fingerprint(snapshot),
                      source_stale=validate_snapshot_age(dataset, request['requested_at'],
                          max_age_days=self.balance_max_age_days,
                          allow_historical=self.allow_historical_snapshot))
        parts = [{'kind': 'message', 'content': chunk, 'status': 'pending'}
                 for chunk in split_messages(balance_summary(report))]
        # A failed chart must not be silently represented as a completed chart report.
        parts.append({'kind': 'file', 'filename': 'studio-client-balance.png',
                      'content': base64.b64encode(balance_chart(report)).decode('ascii'), 'status': 'pending'})
        # The payment curve and project/month charts use one frozen reviewed
        # dataset and cutoff. No extra Linear, bank or accounting reads.
        if dataset.get('hours_reporting'):
            from .studio_recorded_hours import build_recorded_report
            hours = build_recorded_report(self.config, client, selector, dataset, request['requested_at'])
            hours['source_stale'] = report['source_stale']
            report['hours_report'] = hours
            parts.extend({'kind': 'message', 'content': chunk, 'status': 'pending'}
                         for chunk in split_messages(summary(hours)))
            for breakdown, filename in [(None, 'studio-hours.png'), ('projects', 'studio-hours-projects.png')]:
                parts.append({'kind': 'file', 'filename': filename,
                              'content': base64.b64encode(render_chart(hours, breakdown=breakdown)).decode('ascii'),
                              'status': 'pending'})
        parts.extend({'kind': 'file', 'filename': name, 'content': content, 'status': 'pending'}
                     for name, content in balance_artifacts(report).items())
        report['parts'] = parts
        return report

    def deliver_request(self, report, request, key, now):
        if not self.request_authorized(request) or report.get('actor') != request['actor']:
            raise TimesheetError('not_authorized')
        if report.get('scope') != self.scope_fingerprint(request['actor'], request['selector']):
            raise TimesheetError('client_access_changed')
        if request.get('report_kind', 'hours') == 'client_balance':
            from .studio_client_balance_source import load_balance_snapshot, select_balance_dataset
            if not self.balance_enabled or report.get('kind') != 'client_balance':
                raise TimesheetError('client_balance_not_configured')
            snapshot = load_balance_snapshot(self.balance_path, self.config, self.clients)
            _, _, owner = select_balance_dataset(snapshot, self.config, self.clients, request['actor'],
                request['selector'], now, max_age_days=self.balance_max_age_days,
                allow_historical=self.allow_historical_snapshot and report.get('source_stale') is True)
            if owner != report.get('finance_owner') or fingerprint(snapshot) != report.get('finance_digest'):
                raise TimesheetError('client_balance_source_changed')
            return self.deliver_parts(report['parts'], request['actor'], key, now)
        if request.get('report_kind', 'hours') != 'hours':
            raise TimesheetError('invalid_report_kind')
        if report.get('finance_owners'):
            from .studio_client_balance_source import load_balance_snapshot, validate_snapshot_age
            if not self.balance_enabled:
                raise TimesheetError('client_balance_not_configured')
            if report.get('preparation_key'):
                if not self.refresh or len(report['finance_owners']) != 1:
                    raise TimesheetError('studio_source_not_ready')
                snapshot = self.refresh.frozen(report['preparation_key'], report['finance_owners'][0],
                                               request['selector']['end'], now)
            else:
                snapshot = load_balance_snapshot(self.balance_path, self.config, self.clients)
            if fingerprint(snapshot) != report.get('finance_digest'):
                raise TimesheetError('client_balance_source_changed')
            for owner in report['finance_owners']:
                validate_snapshot_age(snapshot['clients'][owner], now, max_age_days=self.balance_max_age_days,
                    allow_historical=self.allow_historical_snapshot and report.get('source_stale') is True)
        if (not report.get('canonical_only') and report.get('backfill_source_used', True) and
                report.get('backfill_digest', fingerprint(None)) != fingerprint(filter_backfill(
                    load_backfill(self.backfill_path, self.config), set(report.get('linear_project_ids', []))))):
            raise TimesheetError('invoice_backfill_changed')
        return self.deliver_parts(report['parts'], request['actor'], key, now)

    def deliver_parts(self, parts, recipient, delivery_key, now):
        # Report delivery requires a current ownership check above. The only
        # other caller sends fixed failure/setup text, never another client's data.
        self.api.verify_recipient(recipient, self.config.team)

        def destination(user):
            channel = self.api.open_dm(user)
            if not re.fullmatch(r'D[A-Z0-9]+', channel):
                raise TimesheetError('private_destination_required')
            return channel

        return self._deliver_transport(parts, recipient, delivery_key, now, destination,
                                       self.api.post_message, self.api.upload_csv)

    def failure_notice(self, request):
        error = request.get('error')
        if error == 'studio_source_waiting_data':
            # Freeze the first gap notice too, so changing source status cannot
            # change an already-sent notice's durable delivery hash on retry.
            notices = ReceiptStore(self.config.directory / 'source-notices')
            key = fingerprint({field: request[field] for field in ('actor', 'team', 'selector', 'requested_at')})
            with notices.locked(key) as acquired:
                if not acquired:
                    raise TimesheetError('report_busy')
                saved = notices.read(key)
                if saved is None:
                    _, _, targets = select_client(self.clients, request['actor'], request['selector'])
                    owner = next(iter(targets)) if targets else request['actor']
                    prepared = self.refresh.store.read(self.refresh.identity(owner, request['selector']['end'])) if self.refresh else None
                    gaps = (prepared or {}).get('result', {}).get('missing', ['source_connection_unavailable'])
                    labels = {'hours_coverage_missing': 'hours coverage',
                        'new_invoices_require_review': 'new contractor invoices awaiting review',
                        'contractor_invoice_sync_failed': 'contractor invoice sync',
                        'work_allocation_requires_review': 'work allocations',
                        'work_dates_require_review': 'precise work dates',
                        'source_connection_unavailable': 'source connection'}
                    missing = ', '.join(sorted({labels.get(code, 'invoice/payment verification') for code in gaps}))
                    saved = {'text': f'Your Friday charts are waiting for: {missing}. '
                        'Unavailable data has not been counted as zero. I’ll retry every five minutes and send the charts '
                        'when the checks pass, using the original Friday reporting cutoff.'}
                    notices.write(key, saved)
                return False, saved['text']
        if request.get('report_kind') == 'client_balance':
            if error in {'client_balance_not_configured', 'client_balance_access_unavailable',
                         'client_access_not_configured', 'report_client_unavailable', 'invalid_report_client'}:
                return True, 'Your client hours and purchased-hours report access is not available. Please ask the Studio team to check the client finance setup.'
            if error == 'client_balance_single_client_required':
                return True, 'Please request the hours and purchased-hours chart for one client, such as “Mark Ghiasy”.'
            if error in {'client_balance_source_changed', 'client_access_changed'}:
                return True, 'Your client finance records or access changed while the report was being prepared. Please request a new chart.'
            if error == 'client_balance_source_stale':
                return True, ('The reviewed client finance records need refreshing before I can produce a current chart. '
                              'The Studio team can refresh the source or enable clearly labelled historical charts '
                              'through the existing review cutoff.')
            return False, 'I couldn’t finish your client hours and purchased-hours report. Missing financial data has not been counted as zero; the Studio team can check the report source.'
        if error == 'all_time_coverage_not_configured':
            return True, 'The beginning of your project history has not been configured. Please request a specific month or date range.'
        if error in {'client_balance_source_changed', 'client_balance_source_stale'}:
            return True, 'The reviewed work-hours source changed or needs refreshing. Please request a new report after the reconciliation snapshot is refreshed.'
        if error in {'report_client_unavailable', 'invalid_report_client'}:
            return True, ('I couldn’t match that client to your report access. Please use their full configured '
                          'name, or ask the Studio team to check your client reporting access.')
        if error == 'invoice_backfill_changed':
            return True, 'Your Studio hours records were updated while this report was being prepared. Please request a new report for the latest totals.'
        if error in {'client_access_not_configured', 'client_access_changed'}:
            return True, ('Your Studio project access needs to be set up or refreshed. '
                          'Please ask the MLAI Studio team to link your Slack account to your projects, then request a new report.')
        if error in {'invalid_month', 'invalid_month_count', 'future_month', 'invalid_report_action'}:
            return True, 'Please request a calendar month or a range of up to 12 months, ending no later than this month.'
        return False, ('I couldn’t finish your Studio hours report yet. Unavailable data has not been counted as zero. '
                       'Safe failures will be retried; an uncertain delivery needs the Studio team to review it.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--once', action='store_true')
    group.add_argument('--preview', metavar='YYYY-MM|current|last')
    group.add_argument('--recover', metavar='DELIVERY_KEY')
    parser.add_argument('--actor', help='Verified client Slack ID for local preview only')
    parser.add_argument('--months', type=int, default=1)
    parser.add_argument('--client', help='Configured client name/alias, or all; never changes the requester')
    parser.add_argument('--detailed', action='store_true')
    parser.add_argument('--balance', action='store_true', help='Preview the private client hours and purchased-hours report')
    parser.add_argument('--output-dir', default='./studio-report-preview')
    parser.add_argument('--part', type=int)
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument('--confirmed-absent', action='store_true')
    recovery.add_argument('--delivered-reference')
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.env_file:
        from dotenv import dotenv_values
        env.update({k: v for k, v in dotenv_values(args.env_file).items() if v is not None})
    try:
        if not flag(env, 'STUDIO_REPORTS_WORKER_ENABLED'):
            print(json.dumps({'status': 'disabled'}))
            return 0
        config, clients = configuration(env)
        schedules = weekly_configuration(env, clients)
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            from .studio_report_refresh import SourceRefresh
            api = StudioReportAPI(client, linear_key=env.get('TIMESHEET_LINEAR_READ_API_KEY'),
                                  slack_token=env.get('TIMESHEET_SLACK_BOT_TOKEN'))
            service = StudioReportService(config, api, clients, env.get('STUDIO_REPORTS_BACKFILL_FILE'),
                balance_path=env.get('STUDIO_CLIENT_BALANCE_FILE'),
                balance_enabled=flag(env, 'STUDIO_CLIENT_BALANCE_WORKER_ENABLED'),
                balance_max_age_days=int(env.get('STUDIO_CLIENT_BALANCE_MAX_AGE_DAYS', '7')),
                schedules=schedules,
                allow_historical_snapshot=flag(env, 'STUDIO_REPORTS_HISTORICAL_FALLBACK_ENABLED'),
                refresh=SourceRefresh.from_env(env, config, clients, client))
            if args.recover:
                if args.part is None or not (args.confirmed_absent or args.delivered_reference):
                    raise TimesheetError('delivery_confirmation_required')
                with service.deliveries.locked(args.recover) as acquired:
                    if not acquired:
                        raise TimesheetError('delivery_busy')
                    receipt = service.deliveries.read(args.recover)
                    if not receipt or not 0 <= args.part < len(receipt['parts']):
                        raise TimesheetError('unknown_delivery_part')
                    part = receipt['parts'][args.part]
                    if part['status'] not in {'sending', 'failed'}:
                        raise TimesheetError('part_not_in_review')
                    part['status'] = 'pending' if args.confirmed_absent else 'sent'
                    if args.delivered_reference:
                        part['reference'] = args.delivered_reference
                    service.deliveries.write(args.recover, receipt)
                print(json.dumps({'status': 'delivery_recovered'}))
                return 0
            if args.preview:
                report = service.report_for_request({'actor': args.actor, 'team': config.team,
                    **({'report_kind': 'client_balance'} if args.balance else {}),
                    'selector': {'month': args.preview, 'months': args.months, 'action': 'detailed' if args.detailed else 'summary',
                                 **({'client': args.client} if args.client else {})},
                    'requested_at': datetime.now(timezone.utc).isoformat()})
                directory = Path(args.output_dir)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                for index, part in enumerate(report['parts']):
                    name = part.get('filename', f'report-{index}.txt')
                    data = base64.b64decode(part['content']) if name.endswith('.png') else part['content'].encode('utf-8')
                    path = directory / name
                    with path.open('wb') as handle:
                        os.chmod(path, 0o600)
                        handle.write(data)
                print(json.dumps({'status': 'preview', 'directory': str(directory)}))
                return 0
            stop = threading.Event()
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop.set())
            while not stop.is_set():
                now = datetime.now(timezone.utc)
                if service.refresh:
                    service.refresh.prewarm(schedules, now)
                results = enqueue_weekly(service, schedules, now)
                results.extend(service.commands(now))
                if results or args.once:
                    print(json.dumps({'results': results}), flush=True)
                if args.once:
                    return int(any(item['status'] not in {'sent', 'done', 'queued'} for item in results))
                stop.wait(10)
    except Exception as exc:
        print(json.dumps({'status': 'error', 'reason': str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
