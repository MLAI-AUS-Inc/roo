"""Private member invoice capability inside the existing timesheet worker."""
from __future__ import annotations

import base64
from datetime import timedelta
import re
import time

from .payment_reminders import ReceiptStore
from .studio_invoices import (FIRST_INVOICE, build_invoice, current_period, draft_at,
                              money, render_pdf, validate_period)
from .timesheets import TimesheetError, fingerprint, timestamp


class InvoiceService:
    def __init__(self, timesheets, options):
        self.timesheets, self.options = timesheets, options
        self.config, self.api = timesheets.config, timesheets.api
        root = self.config.directory / 'invoices'
        self.finals = ReceiptStore(root / 'finals')
        self.drafts = ReceiptStore(root / 'drafts')
        self.selections = ReceiptStore(root / 'requests')
        self.notices = ReceiptStore(root / 'notices')
        self.queue = ReceiptStore(options.queue)
        self.deliveries = timesheets.deliveries  # existing status/recovery CLI

    def owner(self, actor):
        if not self.options.enabled or not self.config.enabled or self.config.source != 'linear':
            raise TimesheetError('invoices_disabled')
        matches = [key for key, b in self.config.builders.items() if b['slack_user_id'] == actor]
        if len(matches) != 1:
            raise TimesheetError('not_authorized')
        self.api.verify_recipient(actor, self.config.team)
        return matches[0]

    def final_key(self, builder_id, period):
        return fingerprint({'team': self.config.team, 'organization': self.config.organization,
                            'builder_id': builder_id, 'period': period})

    def artifact(self, actor, selector, now, *, draft_key=None):
        builder_id = self.owner(actor)
        now = timestamp(now)
        if selector == 'latest':
            end = self.config.latest(now)
            validate_period(self.config, end.date())
            most_recent = None
            while end.date() >= FIRST_INVOICE:
                result = self.artifact(actor, end.date().isoformat(), now)
                most_recent = most_recent or result
                if result['invoice']:
                    return result
                end -= timedelta(days=14)
            return most_recent
        end = (current_period(self.config, now) if selector == 'current' else
               self.config.latest(now) if selector == 'latest' else validate_period(self.config, selector))
        period = end.date().isoformat()
        validate_period(self.config, period)
        draft = end > now
        if draft and end != current_period(self.config, now):
            raise TimesheetError('invoice_period_in_future')
        key = self.final_key(builder_id, period)
        store = self.finals
        if draft:
            store = self.drafts
            key += '-' + (fingerprint(draft_key) if draft_key else fingerprint(now.isoformat()))
        with store.locked(key) as acquired:
            if not acquired:
                raise TimesheetError('invoice_busy')
            existing = store.read(key)
            if existing:
                self.check_owner(existing, actor, builder_id)
                return existing
            report = self.timesheets.report('current' if draft else period, now)
            invoice = build_invoice(self.config, report, builder_id, period)
            artifact = {'team': self.config.team, 'organization': self.config.organization,
                        'builder_id': builder_id, 'slack_user_id': actor, 'period': period,
                        'draft': draft, 'complete': report['complete'], 'invoice': invoice,
                        'pdf': base64.b64encode(render_pdf(invoice)).decode('ascii') if invoice else None}
            store.write(key, artifact)
            return artifact

    def check_owner(self, artifact, actor, builder_id):
        if (artifact['team'] != self.config.team or artifact['organization'] != self.config.organization
                or artifact['builder_id'] != builder_id or artifact['slack_user_id'] != actor):
            raise TimesheetError('invoice_owner_changed')

    def deliver(self, artifact, actor, key, now):
        started = time.monotonic()
        clock = lambda: timestamp(now) + timedelta(seconds=time.monotonic() - started)
        self.check_owner(artifact, actor, self.owner(actor))
        period = artifact['period']
        before_send = (lambda: clock() < validate_period(self.config, period)) if artifact['draft'] else None
        doc = artifact['invoice']
        label = 'draft' if artifact['draft'] else 'final'
        if doc:
            notice = f"Your {label} Invoice to MLAI for the fortnight ending {period}: {money(doc['total_cents'])} AUD. "
            notice += ('This is an estimate. The Friday final supersedes it. ' if artifact['draft'] else
                       'This final supersedes any draft for this fortnight. ')
            notice += 'Fill in your business, ABN and bank details locally, then save a copy for issuing.'
            if not doc['complete']:
                notice += ' Some work needs source review and is excluded; corrections carry forward.'
        else:
            notice = f'There is no payable work for your fortnight ending {period}; no invoice PDF was created.'
            if not artifact['complete']:
                notice += ' Some source work needs review and is excluded; this is not proof of zero work.'
            if not artifact['draft']:
                notice += ' This final outcome supersedes any draft. Any reviewed adjustment carries forward.'
        parts = [{'kind': 'message', 'content': notice, 'status': 'pending'}]
        if doc:
            parts.append({'kind': 'file', 'filename': f"roo-invoice-{period}-{label}.pdf",
                          'content': artifact['pdf'], 'status': 'pending'})
        def destination(user):
            channel = self.api.open_dm(user)
            if not re.fullmatch(r'D[A-Z0-9]+', channel):
                raise TimesheetError('private_destination_required')
            return channel
        return self.timesheets._deliver_transport(parts, actor, 'invoice-' + key, now,
            destination, self.api.post_message,
            lambda channel, name, content: self.api.upload_csv(channel, name, base64.b64decode(content, validate=True),
                                                               before_send=before_send), before_send=before_send)

    def command_artifact(self, request, key, now):
        """Pin the chosen period privately before any transport can start."""
        actor = request['actor']
        with self.selections.locked(key) as acquired:
            if not acquired:
                raise TimesheetError('invoice_busy')
            selection = self.selections.read(key)
            if selection:
                self.check_owner(selection, actor, self.owner(actor))
                selector = selection['period']
            else:
                selector = request['selector']
                if selector == 'current':
                    selector = current_period(self.config, timestamp(request['requested_at'])).date().isoformat()
                elif selector != 'latest':
                    validate_period(self.config, selector)
            artifact = self.artifact(actor, selector, now, draft_key='command-' + key)
            if selection is None:
                self.selections.write(key, {k: artifact[k] for k in
                    ('team', 'organization', 'builder_id', 'slack_user_id', 'period')})
            return artifact

    def authorized_request(self, request, key):
        identity = {k: request.get(k) for k in ('team', 'actor', 'channel', 'source')}
        if (request.get('team') != self.config.team or not isinstance(identity['source'], str)
                or not identity['source'] or fingerprint(identity) != key
                or not re.fullmatch(r'[CDG][A-Z0-9]+', str(identity['channel']))):
            return False
        return any(b['slack_user_id'] == request.get('actor') for b in self.config.builders.values())

    def acknowledge(self, request, key, now):
        channel, thread = request['channel'], request.get('thread_ts')
        if channel.startswith('D') or thread is None:
            return 'sent'  # DMs and ephemeral slash commands need no channel reply.
        if not re.fullmatch(r'\d+\.\d+', str(thread)):
            raise TimesheetError('invalid_confirmation_destination')
        actor = request['actor']
        self.owner(actor)
        text = (f'<@{actor}> I’ve sent your invoice privately. Check your DMs.' if request['has_invoice'] else
                f'<@{actor}> I’ve sent you a private update about your invoice request. Check your DMs.')
        return self.timesheets._deliver_transport(
            [{'kind': 'message', 'content': {'text': text, 'channel': channel, 'thread_ts': thread}, 'status': 'pending'}], actor,
            'invoice-ack-' + key, now, lambda _: channel,
            lambda destination, content: self.api.post_invoice_ack(destination, content['text'], thread_ts=content['thread_ts']),
            self.api.upload_csv)

    def commands(self, now):
        with self.timesheets.report_batch():
            return self._commands(now)

    def waiting_notice(self, request, key, notice, deadline):
        """Pin original notice content so changing retry reasons cannot resend it."""
        with self.notices.locked(key) as acquired:
            if not acquired:
                raise TimesheetError('invoice_busy')
            existing = self.notices.read(key)
            if existing is not None:
                return existing['text']
            legacy = self.deliveries.read('invoice-notice-' + key)
            if legacy:
                notice = legacy['parts'][0]['content']
            else:
                code = request['error']
                if code == 'invoice_source_not_prepared':
                    notice = 'Your invoice request is saved. Studio’s invoice source is being prepared; it will resume once ready.'
                elif code == 'linear_rate_limited' and deadline:
                    local = timestamp(deadline).astimezone(self.config.tz).strftime('%d %B %Y %H:%M %Z')
                    notice = ('Your invoice request is saved. Linear is busy. The earliest retry is '
                              + local + '; it will resume when the source is available.')
                elif code in {'linear_source_budget','linear_source_unstable'}:
                    notice = 'Your invoice request is saved. Roo is still checking the source and will resume once ready.'
            self.notices.write(key, {'reason':request['error'], 'text':notice, 'deadline':deadline})
            return notice

    def _commands(self, now):
        if not self.options.enabled or not self.queue.directory.exists():
            return []
        results = []
        started = time.monotonic()
        for path in sorted(self.queue.directory.glob('*.json')):
            key = path.stem
            try:
                if not re.fullmatch(r'[a-f0-9]{64}', key):
                    raise TimesheetError('invalid_request')
                with self.queue.locked(key) as acquired:
                    if not acquired:
                        continue
                    request = self.queue.read(key)
                    if not isinstance(request, dict):
                        raise TimesheetError('invalid_request')
                    if request.get('status') in {'done', 'denied', 'rejected'}:
                        continue
                    if not self.authorized_request(request, key):
                        request['status'] = 'denied'
                        self.queue.write(key, request)
                        continue
                    if timestamp(now).timestamp() < request.get('retry_at', 0):
                        continue
                    actor = request['actor']
                    try:
                        if not request.get('delivered'):
                            # The process can stop between the last Slack receipt and the queue checkpoint.
                            for label in ('final', 'draft'):
                                receipt = self.deliveries.read('invoice-command-' + key + '-' + label)
                                if (receipt and receipt.get('recipient') == actor and receipt.get('parts')
                                        and all(p['status'] == 'sent' for p in receipt['parts'])):
                                    request.update(delivered=True, has_invoice=any(p['kind'] == 'file' for p in receipt['parts']))
                                    self.queue.write(key, request)
                                    break
                        if not request.get('delivered'):
                            artifact = self.command_artifact(request, key, now)
                            send_at = timestamp(now) + timedelta(seconds=time.monotonic() - started)
                            if artifact['draft'] and send_at >= validate_period(self.config, artifact['period']):
                                artifact = self.artifact(actor, artifact['period'], send_at)
                            status = self.deliver(artifact, actor, 'command-' + key + ('-draft' if artifact['draft'] else '-final'), send_at)
                            if status == 'expired' and artifact['draft']:
                                send_at = timestamp(now) + timedelta(seconds=time.monotonic() - started)
                                artifact = self.artifact(actor, artifact['period'], send_at)
                                status = self.deliver(artifact, actor, 'command-' + key + '-final', send_at)
                            if status == 'sent':
                                # Persist the private delivery before attempting its public confirmation.
                                request.update(delivered=True, has_invoice=bool(artifact['invoice']))
                                self.queue.write(key, request)
                        if request.get('delivered'):
                            status = self.acknowledge(request, key, timestamp(now) + timedelta(seconds=time.monotonic() - started))
                        request['status'] = 'done' if status == 'sent' else status
                        request.pop('error', None)
                        results.append({'request': key, 'status': status})
                    except Exception as exc:
                        code = str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__
                        request.update(status='error', error=code,
                            retry_at=max(timestamp(now).timestamp() + time.monotonic() - started + 60,
                                         getattr(exc, 'retry_at', 0)))
                        results.append({'request': key, 'status': 'error', 'reason': code})
                        terminal = code in {'invoice_before_rollout', 'not_a_payment_cutoff',
                                            'invoice_period_in_future', 'invalid_timestamp', 'invoice_owner_changed'}
                        notice = ('That invoice fortnight is unavailable. Use `invoice` for the current draft or `invoice latest` for a final.'
                                  if terminal else 'Roo could not finish your invoice request yet. Source failures are never counted as zero. '
                                  'Safe failures will retry; an uncertain delivery requires operator review.')
                        try:
                            if request.get('delivered'):
                                raise TimesheetError('confirmation_only_retry')
                            self.owner(actor)
                            deadline = getattr(exc, 'retry_at', None)
                            # timestamp accepts dates/strings, not Unix seconds.
                            if deadline is not None:
                                from datetime import datetime, timezone
                                deadline = datetime.fromtimestamp(deadline, timezone.utc).isoformat()
                            notice = self.waiting_notice(request, key, notice, deadline)
                            outcome = self.timesheets._deliver_transport(
                                [{'kind': 'message', 'content': notice, 'status': 'pending'}], actor,
                                'invoice-notice-' + key, now, self.api.open_dm,
                                self.api.post_message, self.api.upload_csv)
                            if terminal and outcome == 'sent':
                                request['status'] = 'rejected'
                        except Exception:
                            pass
                    self.queue.write(key, request)
            except Exception as exc:
                results.append({'status': 'error', 'reason': str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__})
        return results

    def scheduled(self, now):
        with self.timesheets.report_batch():
            return self._scheduled(now)

    def _scheduled(self, now):
        if not self.options.enabled or not self.options.scheduled:
            return []
        now = timestamp(now)
        results = []
        started = time.monotonic()
        latest = self.config.latest(now)
        dates = []
        end = validate_period(self.config, FIRST_INVOICE)
        while end <= latest:
            dates.append((end.date().isoformat(), False))
            end += timedelta(days=14)
        current = current_period(self.config, now).date().isoformat()
        if current >= FIRST_INVOICE.isoformat() and draft_at(self.config, current) <= now:
            dates.append((current, True))
        for period, draft in dates:
            for builder_id, builder in self.config.builders.items():
                try:
                    actor = builder['slack_user_id']
                    delivery = 'scheduled-' + self.final_key(builder_id, period) + ('-draft' if draft else '-final')
                    receipt = self.deliveries.read('invoice-' + delivery)
                    if receipt and all(p['status'] == 'sent' for p in receipt['parts']):
                        continue
                    artifact = self.artifact(actor, period, now, draft_key='scheduled')
                    send_at = now + timedelta(seconds=time.monotonic() - started)
                    if draft and send_at >= validate_period(self.config, period):
                        continue  # Catch-up window has closed during collection.
                    status = self.deliver(artifact, actor, delivery, send_at)
                    results.append({'period': period, 'builder': actor, 'status': status})
                except Exception as exc:
                    results.append({'period': period, 'builder': builder['slack_user_id'], 'status': 'error',
                                    'reason': str(exc) if isinstance(exc, TimesheetError) else type(exc).__name__})
        return results
