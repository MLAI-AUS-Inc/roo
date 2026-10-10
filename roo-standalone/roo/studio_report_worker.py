"""On-demand client reports, isolated from Public Roo and the payroll ledger."""
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
    return config, raw


class StudioReportAPI(StudioSourceAPI):
    def upload_csv(self, channel, filename, content):
        if filename.endswith('.png'):
            content = base64.b64decode(content, validate=True)
        return super().upload_csv(channel, filename, content)


class StudioReportService(TimesheetService):
    def __init__(self, config, api, clients, backfill_path=None, *, balance_path=None,
                 balance_enabled=False, balance_max_age_days=7):
        super().__init__(config, api)
        self.clients = clients
        self.backfill_path = backfill_path
        self.balance_path = balance_path
        self.balance_enabled = balance_enabled
        self.balance_max_age_days = balance_max_age_days

    def request_authorized(self, request):
        # Unknown clients receive a private, generic setup message. No source read.
        return (request.get('team') == self.config.team
                and re.fullmatch(r'[UW][A-Z0-9]+', str(request.get('actor', ''))) is not None)

    def scope_fingerprint(self, actor, selector=None):
        if actor not in self.clients:
            raise TimesheetError('client_access_not_configured')
        client = self.clients[actor]
        _, groups, targets = select_client(self.clients, actor, selector or {})
        scope = {'actor': actor, 'team': self.config.team, 'organization': self.config.organization,
                 'client': client, 'projects': {key: self.config.projects[key] for key in client['project_ids']}}
        if targets:
            scope.update(targets=targets, groups=groups)
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
        scope = self.scope_fingerprint(actor, selector)
        client, groups, _ = select_client(self.clients, actor, selector)
        backfill = load_backfill(self.backfill_path, self.config)
        if start is None:
            selector, start, _ = resolve_period(selector, now,
                beginning=history_beginning(backfill, client['project_ids']))
        # The collection window must include project moves since the requested
        # month, even for months older than the payroll worker's initial cutoff.
        source = SimpleNamespace(team=self.config.team, organization=self.config.organization,
                                 projects={key: self.config.projects[key] for key in client['project_ids']},
                                 beginning=start, directory=self.config.directory)
        self.api.verify_recipient(actor, self.config.team)
        self.api.verify(source)
        dataset = self.api.collect(source, now, {})
        report = build_client_report(self.config, client, selector, dataset, now, backfill)
        if groups:
            report['client_groups'] = groups
        report.update(actor=actor, scope=scope, backfill_digest=fingerprint(backfill))
        # Freeze rendered parts too. A delivery retry uses the same content hash
        # even when font versions or optional chart availability change.
        parts = [{'kind': 'message', 'content': chunk, 'status': 'pending'} for chunk in split_messages(summary(report))]
        try:
            chart = render_chart(report)
            parts.append({'kind': 'file', 'filename': 'studio-hours.png',
                          'content': base64.b64encode(chart).decode('ascii'), 'status': 'pending'})
            if selector.get('client') and len(report['monthly']) > 1:
                parts.append({'kind': 'file', 'filename': 'studio-hours-projects.png',
                              'content': base64.b64encode(render_chart(report, breakdown='projects')).decode('ascii'), 'status': 'pending'})
            if groups:
                parts.append({'kind': 'file', 'filename': 'studio-hours-clients.png',
                              'content': base64.b64encode(render_chart(report, breakdown='clients')).decode('ascii'), 'status': 'pending'})
        except Exception:
            parts.append({'kind': 'message', 'content': 'The chart is unavailable this time; the full totals are above.', 'status': 'pending'})
        if selector['action'] == 'detailed':
            parts.extend({'kind': 'message', 'content': chunk, 'status': 'pending'} for chunk in detail_messages(report))
        parts.extend({'kind': 'file', 'filename': name, 'content': content, 'status': 'pending'}
                     for name, content in artifacts(report).items())
        report['parts'] = parts
        return report

    def balance_for_request(self, request):
        from .studio_client_balance import artifacts as balance_artifacts
        from .studio_client_balance import build_balance_report, render_chart as balance_chart, summary as balance_summary
        from .studio_client_balance_source import load_balance_snapshot, select_balance_dataset

        if not self.balance_enabled:
            raise TimesheetError('client_balance_not_configured')
        actor, selector = request['actor'], request['selector']
        # Resolve access before touching finance records. The Public runtime
        # only queues identity and selection; this file is in the private volume.
        scope = self.scope_fingerprint(actor, selector)
        snapshot = load_balance_snapshot(self.balance_path, self.config, self.clients)
        client, dataset, owner = select_balance_dataset(snapshot, self.config, self.clients,
            actor, selector, request['requested_at'], max_age_days=self.balance_max_age_days)
        self.api.verify_recipient(actor, self.config.team)
        report = build_balance_report(client, selector, dataset, request['requested_at'])
        report.update(actor=actor, scope=scope, finance_owner=owner,
                      finance_digest=fingerprint(snapshot))
        parts = [{'kind': 'message', 'content': chunk, 'status': 'pending'}
                 for chunk in split_messages(balance_summary(report))]
        # A failed chart must not be silently represented as a completed chart report.
        parts.append({'kind': 'file', 'filename': 'studio-client-balance.png',
                      'content': base64.b64encode(balance_chart(report)).decode('ascii'), 'status': 'pending'})
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
                request['selector'], now, max_age_days=self.balance_max_age_days)
            if owner != report.get('finance_owner') or fingerprint(snapshot) != report.get('finance_digest'):
                raise TimesheetError('client_balance_source_changed')
            return self.deliver_parts(report['parts'], request['actor'], key, now)
        if request.get('report_kind', 'hours') != 'hours':
            raise TimesheetError('invalid_report_kind')
        if report.get('backfill_digest', fingerprint(None)) != fingerprint(load_backfill(self.backfill_path, self.config)):
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
        if request.get('report_kind') == 'client_balance':
            if error in {'client_balance_not_configured', 'client_balance_access_unavailable',
                         'client_access_not_configured', 'report_client_unavailable', 'invalid_report_client'}:
                return True, 'Your client hours and purchased-hours report access is not available. Please ask the Studio team to check the client finance setup.'
            if error == 'client_balance_single_client_required':
                return True, 'Please request the hours and purchased-hours chart for one client, such as “Mark Ghiasy”.'
            if error in {'client_balance_source_changed', 'client_access_changed'}:
                return True, 'Your client finance records or access changed while the report was being prepared. Please request a new chart.'
            if error == 'client_balance_source_stale':
                return True, 'The reviewed client finance records need refreshing before I can produce a current chart. Please ask the Studio team to refresh the reconciliation snapshot.'
            return False, 'I couldn’t finish your client hours and purchased-hours report. Missing financial data has not been counted as zero; the Studio team can check the report source.'
        if error == 'all_time_coverage_not_configured':
            return True, 'The beginning of your project history has not been configured. Please request a specific month or date range.'
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
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            api = StudioReportAPI(client, linear_key=env.get('TIMESHEET_LINEAR_READ_API_KEY'),
                                  slack_token=env.get('TIMESHEET_SLACK_BOT_TOKEN'))
            service = StudioReportService(config, api, clients, env.get('STUDIO_REPORTS_BACKFILL_FILE'),
                balance_path=env.get('STUDIO_CLIENT_BALANCE_FILE'),
                balance_enabled=flag(env, 'STUDIO_CLIENT_BALANCE_WORKER_ENABLED'),
                balance_max_age_days=int(env.get('STUDIO_CLIENT_BALANCE_MAX_AGE_DAYS', '7')))
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
                results = service.commands(datetime.now(timezone.utc))
                if results or args.once:
                    print(json.dumps({'results': results}), flush=True)
                if args.once:
                    return int(any(item['status'] not in {'sent', 'done'} for item in results))
                stop.wait(10)
    except Exception as exc:
        print(json.dumps({'status': 'error', 'reason': str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
