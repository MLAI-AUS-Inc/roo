"""Studio timesheets: deterministic reports, durable requests and private delivery."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import signal
import threading
from urllib.parse import urlsplit

import httpx

from .payment_reminders import DeliveryRejected, ReceiptStore
from .timesheet_linear import TimesheetAPI
from .timesheets import (Ledger, TimesheetConfig, TimesheetError, artifacts, build_report,
                         fingerprint, summary, timestamp)


class ReportAPI(TimesheetAPI):
    def upload_csv(self, channel, filename, content):
        if not re.fullmatch(r'D[A-Z0-9]+', channel):
            raise TimesheetError('private_destination_required')
        return self._upload_csv(channel, filename, content)

    def _upload_csv(self, channel, filename, content, *, thread_ts=None):
        data = content.encode('utf-8')
        response = self.client.get('https://slack.com/api/files.getUploadURLExternal',
            headers={'Authorization': 'Bearer ' + self.slack_token},
            params={'filename': filename, 'length': len(data)})
        if response.status_code == 429:
            raise DeliveryRejected('ratelimited', int(response.headers.get('Retry-After', '60')))
        response.raise_for_status()
        body = response.json()
        if body.get('ok') is not True:
            raise DeliveryRejected('upload_url_rejected')
        url = body.get('upload_url', '')
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'files.slack.com'
                or not parsed.path.startswith('/upload/') or parsed.fragment
                or not re.fullmatch(r'F[A-Z0-9]+', body.get('file_id', ''))):
            raise TimesheetError('invalid_upload_destination')
        # The URL itself grants upload access. Do not forward Slack's bearer token.
        uploaded = self.client.post(url, content=data, headers={'Content-Type': 'application/octet-stream'})
        uploaded.raise_for_status()
        result = self.slack('files.completeUploadExternal', {
            'files': [{'id': body['file_id'], 'title': filename}], 'channel_id': channel,
            **({'thread_ts': thread_ts} if thread_ts else {}),
        })
        files = result.get('files')
        if not isinstance(files, list) or len(files) != 1 or files[0].get('id') != body['file_id']:
            raise TimesheetError('unconfirmed_file_delivery')
        return body['file_id']


def bind_ledger(config, ledger):
    identity = {'team': config.team, 'organization': config.organization, 'first': config.first.isoformat()}
    if ledger.get('identity', identity) != identity:
        raise TimesheetError('ledger_identity_mismatch')
    ledger['identity'] = identity


def allocate(ledger, report):
    period = report['period']
    ledger['periods'][period] = report
    for row in report['rows']:
        if row['kind'] == 'ticket':
            ledger['allocated'][row['issue_id']] = period
    ledger['deferred'].update(report['deferred'])
    for issue_id in ledger['allocated']:
        ledger['deferred'].pop(issue_id, None)
    included = {r['reference'] for r in report['rows'] if r['kind'] == 'correction'}
    for correction in ledger['corrections']:
        if correction['row']['reference'] in included and not correction.get('period'):
            correction['period'] = period


class TimesheetService:
    def __init__(self, config, api):
        self.config, self.api = config, api
        self.ledger = Ledger(config.directory)
        self.deliveries = ReceiptStore(config.directory / 'deliveries')
        self.requests = ReceiptStore(config.directory / 'requests')
        self.queue = ReceiptStore(config.queue)

    def report(self, selector, now, *, preview=False):
        config = self.config
        if config.source == 'plane' and not preview:
            raise TimesheetError('plane_preview_only_pending_coverage_verification')
        now = timestamp(now)
        draft = selector == 'current'
        end = now.astimezone(config.tz) if draft else (
            config.latest(now) if selector == 'latest' else config.cutoff(selector))
        if end < config.beginning or (not draft and end.date() < config.first):
            raise TimesheetError('no_completed_fortnight')
        if end > now:
            raise TimesheetError('period_not_closed')
        with self.ledger.locked('ledger') as acquired:
            if not acquired:
                raise TimesheetError('report_busy')
            data = self.ledger.load()
            bind_ledger(config, data)
            period = end.date().isoformat()
            if not draft and period in data['periods']:
                if config.source == 'plane':
                    raise TimesheetError('plane_preview_requires_unfinalized_period')
                return deepcopy(data['periods'][period])
            self.api.verify(config)
            dataset = self.api.collect(config, end, data['deferred'])
            cutoff = config.cutoff(config.first)
            last = config.latest(now) if draft else end
            while cutoff <= last:
                key = cutoff.date().isoformat()
                if key not in data['periods']:
                    report = build_report(config, cutoff, dataset, data, generated_at=now)
                    report['evidence'] = dataset
                    report['evidence_sha256'] = fingerprint(dataset)
                    allocate(data, report)
                    if not draft and not preview:
                        self.ledger.save(data)
                cutoff += timedelta(days=14)
            if draft:
                return build_report(config, end, dataset, data, draft=True, generated_at=now)
            return deepcopy(data['periods'][period])

    def deliver(self, report, recipient, delivery_key, now):
        if self.config.source == 'plane' or report.get('source') == 'plane':
            raise TimesheetError('plane_preview_only_pending_coverage_verification')
        parts = [{'kind': 'message', 'content': summary(report), 'status': 'pending'}]
        parts += [{'kind': 'file', 'filename': name, 'content': content, 'status': 'pending'}
                  for name, content in artifacts(report).items()]
        return self.deliver_parts(parts, recipient, delivery_key, now)

    def deliver_parts(self, parts, recipient, delivery_key, now):
        config = self.config
        if recipient != config.recipient:
            raise TimesheetError('not_authorized')
        with self.ledger.locked('ledger') as acquired:
            if not acquired:
                return 'busy'
            bind_ledger(config, self.ledger.load())
        self.api.verify_recipient(recipient, config.team)
        def private_channel(user):
            channel = self.api.open_dm(user)
            if not re.fullmatch(r'D[A-Z0-9]+', channel):
                raise TimesheetError('private_destination_required')
            return channel
        return self._deliver_transport(parts, recipient, delivery_key, now, private_channel,
                                       self.api.post_message, self.api.upload_csv)

    def _deliver_transport(self, parts, recipient, delivery_key, now, destination, post, upload):
        """Durable transport checkpoints, after the caller validates its audience."""
        with self.deliveries.locked(delivery_key) as acquired:
            if not acquired:
                return 'busy'
            delivery = self.deliveries.read(delivery_key)
            digest = fingerprint(parts)
            if delivery and (delivery['recipient'] != recipient or delivery['report_hash'] != digest):
                raise TimesheetError('delivery_identity_changed')
            if delivery is None:
                delivery = {'recipient': recipient, 'report_hash': digest, 'parts': parts}
                self.deliveries.write(delivery_key, delivery)
            if any(p['status'] in {'sending', 'failed'} for p in delivery['parts']):
                return 'needs_review'
            if all(p['status'] == 'sent' for p in delivery['parts']):
                return 'sent'
            channel = destination(recipient)
            for part in delivery['parts']:
                if part['status'] == 'sent':
                    continue
                if timestamp(now).timestamp() < part.get('retry_at', 0):
                    return 'pending'
                part['status'] = 'sending'
                part['attempted_at'] = timestamp(now).isoformat()
                self.deliveries.write(delivery_key, delivery)
                try:
                    if part['kind'] == 'message':
                        ref = post(channel, part['content'])
                    else:
                        ref = upload(channel, part['filename'], part['content'])
                except DeliveryRejected as exc:
                    part['status'] = 'pending' if exc.retry_after is not None else 'failed'
                    if exc.retry_after is not None:
                        part['retry_at'] = timestamp(now).timestamp() + max(60, exc.retry_after)
                    self.deliveries.write(delivery_key, delivery)
                    return 'pending' if exc.retry_after is not None else 'needs_review'
                # Any other exception intentionally preserves sending for manual review.
                part['status'], part['reference'] = 'sent', ref
                self.deliveries.write(delivery_key, delivery)
            return 'sent'

    def deliver_request(self, report, request, key, now):
        # Permission to trigger a report does not change its production recipient.
        return self.deliver(report, self.config.recipient, key, now)

    def request_authorized(self, request):
        return request.get('team') == self.config.team and request.get('actor') == self.config.recipient

    def commands(self, now):
        results = []
        if not self.config.queue.exists():
            return results
        for path in sorted(self.config.queue.glob('*.json')):
            key = path.stem
            # A damaged request must not stop all other requests or the schedule.
            try:
                if not re.fullmatch(r'[a-f0-9]{64}', key) or not isinstance(self.queue.read(key), dict):
                    raise TimesheetError('invalid_request')
            except Exception:
                results.append({'status': 'error', 'reason': 'invalid_request'})
                continue
            with self.queue.locked(key) as acquired:
                if not acquired:
                    continue
                request = self.queue.read(key)
                if request.get('status') in {'done', 'denied', 'rejected'}:
                    continue
                if not self.request_authorized(request):
                    request['status'] = 'denied'
                    self.queue.write(key, request)
                    continue
                if timestamp(now).timestamp() < request.get('retry_at', 0):
                    continue
                try:
                    # Persist the preview too: retries must send exactly the same artifact.
                    # Keep source evidence out of the queue mounted in Public Roo.
                    report = self.requests.read(key)
                    if report is None:
                        report = self.report(request['selector'], timestamp(request['requested_at']))
                        with self.requests.locked(key) as acquired_snapshot:
                            if not acquired_snapshot:
                                raise TimesheetError('report_busy')
                            self.requests.write(key, report)
                    status = self.deliver_request(report, request, 'command-' + key, now)
                    request['status'] = 'done' if status == 'sent' else status
                    request.pop('error', None)
                    results.append({'request': key, 'status': status})
                except Exception as exc:
                    code = str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__
                    request['status'], request['error'] = 'error', code
                    request['retry_at'] = timestamp(now).timestamp() + 60
                    results.append({'request': key, 'status': 'error', 'reason': code})
                if request['status'] in {'error', 'needs_review'}:
                    terminal = request.get('error') in {'no_completed_fortnight', 'not_a_payment_cutoff', 'period_not_closed', 'invalid_timestamp'}
                    notice = ('That fortnight is not available. Use `timesheet current` for an open-period draft.' if terminal else
                              'Roo could not finish your timesheet request yet. No unavailable data has been counted as zero. '
                              'Roo will retry safe failures; an uncertain Slack delivery needs operator review.')
                    try:
                        outcome = self.deliver_parts([{'kind': 'message', 'content': notice, 'status': 'pending'}],
                            request['actor'], 'notice-' + key, now)
                        if terminal and outcome == 'sent':
                            request['status'] = 'rejected'
                    except Exception:
                        # The notice has its own durable receipt and must never
                        # overwrite a report's delivery or allocation state.
                        pass
                self.queue.write(key, request)
        return results

    def scheduled(self, now):
        if not self.config.scheduled:
            return []
        latest = self.config.latest(now)
        if latest.date() < self.config.first:
            return []
        self.report('latest', now)
        with self.ledger.locked('ledger') as acquired:
            if not acquired:
                return [{'status': 'busy'}]
            reports = deepcopy(self.ledger.load()['periods'])
        results = []
        for key in sorted(reports):
            if self.config.cutoff(key) <= timestamp(now):
                status = self.deliver(reports[key], self.config.recipient, 'scheduled-' + key, now)
                results.append({'period': key, 'status': status})
        return results

    def correction(self, adjustment, now):
        required = {'id', 'period', 'issue_id', 'builder_id', 'units', 'reason'}
        if set(adjustment) != required or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', adjustment['id']):
            raise TimesheetError('invalid_correction')
        if (type(adjustment['units']) is not int or not adjustment['units'] or not adjustment['reason'].strip()
                or adjustment['builder_id'] not in self.config.builders):
            raise TimesheetError('invalid_correction')
        with self.ledger.locked('ledger') as acquired:
            if not acquired:
                raise TimesheetError('report_busy')
            data = self.ledger.load()
            bind_ledger(self.config, data)
            existing = next((x for x in data['corrections'] if x['id'] == adjustment['id']), None)
            if existing:
                if existing['input'] != adjustment:
                    raise TimesheetError('correction_id_reused')
                return
            source = data['periods'].get(adjustment['period'])
            original = next((r for r in (source or {}).get('rows', []) if r['issue_id'] == adjustment['issue_id'] and r['kind'] == 'ticket'), None)
            if original is None:
                raise TimesheetError('original_row_required')
            row = dict(original)
            builder = self.config.builders[adjustment['builder_id']]
            row.update(kind='correction', units=adjustment['units'], reason=adjustment['reason'],
                       reference=adjustment['id'] + ':' + adjustment['period'] + ':' + adjustment['issue_id'],
                       builder_id=adjustment['builder_id'], builder=builder['name'], slack_user_id=builder['slack_user_id'])
            data['corrections'].append({'id': adjustment['id'], 'input': adjustment, 'row': row,
                                       'recorded_at': timestamp(now).isoformat(), 'period': None})
            self.ledger.save(data)


def export_report(report, directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in artifacts(report).items():
        path = directory / name
        with path.open('w', encoding='utf-8', newline='') as handle:
            os.chmod(path, 0o600)
            handle.write(content)
    return list(artifacts(report))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', help='Explicit dedicated development/worker environment; never implicitly load public .env')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--once', action='store_true')
    group.add_argument('--preflight', action='store_true')
    group.add_argument('--status', action='store_true', help='Inspect local period and delivery status without network calls')
    group.add_argument('--preview', metavar='latest|current|YYYY-MM-DD')
    group.add_argument('--export', metavar='YYYY-MM-DD', help='Regenerate an existing immutable snapshot only')
    group.add_argument('--correct', metavar='JSON_FILE', help='Record a quarter-hour adjustment for the next open period')
    group.add_argument('--recover', metavar='DELIVERY_KEY')
    parser.add_argument('--part', type=int)
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument('--confirmed-absent', action='store_true')
    recovery.add_argument('--delivered-reference')
    parser.add_argument('--output-dir', default='./timesheet-preview')
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.env_file:
        from dotenv import dotenv_values
        env.update({k: v for k, v in dotenv_values(args.env_file).items() if v is not None})
    config = TimesheetConfig.from_env(env)
    local_only = args.status or args.export or args.correct or args.recover
    if not config.enabled and not local_only:
        print(json.dumps({'status': 'disabled'}))
        return 0
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        try:
            api = None
            if not local_only:
                if config.source == 'plane':
                    from .timesheet_plane import PlaneAPI, PlaneOptions
                    slack_api = ReportAPI(client, linear_key='unused-plane-source', slack_token=env.get('TIMESHEET_SLACK_BOT_TOKEN'))
                    api = PlaneAPI(client, PlaneOptions.from_env(env), slack_api)
                else:
                    api = ReportAPI(client, linear_key=env.get('TIMESHEET_LINEAR_READ_API_KEY'), slack_token=env.get('TIMESHEET_SLACK_BOT_TOKEN'))
            service = TimesheetService(config, api)
            if args.status:
                with service.ledger.locked('ledger') as acquired:
                    if not acquired:
                        raise TimesheetError('report_busy')
                    data = service.ledger.load()
                    bind_ledger(config, data)
                deliveries = [{ 'key': p.stem, 'parts': [part['status'] for part in service.deliveries.read(p.stem)['parts']] }
                              for p in sorted(service.deliveries.directory.glob('*.json'))]
                print(json.dumps({'periods': list(data['periods']), 'allocated_tickets': len(data['allocated']),
                                  'deferred_tickets': len(data['deferred']), 'deliveries': deliveries}))
                return 0
            if args.preflight:
                api.verify(config)
                api.verify_recipient(config.recipient, config.team)
                for linear_id, builder in config.builders.items():
                    api.verify_recipient(builder['slack_user_id'], config.team)
                    if config.source == 'linear':
                        data = api.linear('query TimesheetBuilder($id:String!) { user(id:$id) { id } }', {'id': linear_id})
                        if (data.get('user') or {}).get('id') != linear_id:
                            raise TimesheetError('builder_unavailable')
                print(json.dumps({'status': 'verified', 'projects': len(config.projects), 'builders': len(config.builders)}))
                return 0
            if args.preview:
                report = service.report(args.preview, datetime.now(timezone.utc), preview=True)
                print(json.dumps({'status': 'preview', 'files': export_report(report, Path(args.output_dir)), 'summary': summary(report)}))
                return 0
            if args.export:
                with service.ledger.locked('ledger') as acquired:
                    if not acquired:
                        raise TimesheetError('report_busy')
                    data = service.ledger.load()
                    bind_ledger(config, data)
                    report = data['periods'].get(args.export)
                if report is None:
                    raise TimesheetError('snapshot_not_found')
                print(json.dumps({'status': 'exported', 'files': export_report(report, Path(args.output_dir))}))
                return 0
            if args.correct:
                service.correction(json.loads(Path(args.correct).read_text()), datetime.now(timezone.utc))
                print(json.dumps({'status': 'correction_recorded'}))
                return 0
            if args.recover:
                if args.part is None or (not args.confirmed_absent and not args.delivered_reference):
                    raise TimesheetError('delivery_confirmation_required')
                with service.deliveries.locked(args.recover) as acquired:
                    if not acquired:
                        raise TimesheetError('delivery_busy')
                    receipt = service.deliveries.read(args.recover)
                    if not receipt or args.part < 0 or args.part >= len(receipt['parts']):
                        raise TimesheetError('unknown_delivery_part')
                    part = receipt['parts'][args.part]
                    if part['status'] not in {'sending', 'failed'}:
                        raise TimesheetError('part_not_in_review')
                    part['status'] = 'pending' if args.confirmed_absent else 'sent'
                    if args.delivered_reference:
                        part['reference'] = args.delivered_reference
                    part['reviewed_at'] = datetime.now(timezone.utc).isoformat()
                    service.deliveries.write(args.recover, receipt)
                print(json.dumps({'status': 'delivery_recovered'}))
                return 0
        except Exception as exc:
            print(json.dumps({'status': 'error', 'reason': str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__}))
            return 1
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        while not stop.is_set():
            results = []
            try:
                results.extend(service.commands(datetime.now(timezone.utc)))
            except Exception as exc:
                results.append({'status': 'error', 'reason': str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__})
            try:
                results.extend(service.scheduled(datetime.now(timezone.utc)))
            except Exception as exc:
                results.append({'status': 'error', 'reason': str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__})
            if results or args.once:
                print(json.dumps({'results': results}), flush=True)
            if args.once:
                return int(any(x['status'] not in {'sent', 'done'} for x in results))
            stop.wait(10)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
