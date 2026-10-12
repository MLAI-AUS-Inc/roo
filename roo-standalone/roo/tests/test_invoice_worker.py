"""Real allocation/artifact/receipt stores with an offline provider boundary."""
import base64
from dataclasses import replace
from datetime import timedelta
import io
import httpx

import pytest
from pypdf import PdfReader

from roo.payment_reminders import DeliveryRejected, ReceiptStore
from roo.tests.test_timesheets import config, USER, OTHER, item, ticket
from roo.timesheet_worker import TimesheetService
from roo.timesheets import TimesheetError, fingerprint, timestamp

THURSDAY = timestamp('2026-10-22T17:00:00+11:00')
FRIDAY = timestamp('2026-10-23T12:00:00+11:00')


class OfflineAPI:
    def __init__(self):
        self.dataset = [item(ticket(completedAt='2026-10-20T01:00:00Z', updatedAt='2026-10-20T01:00:00Z')),
                        item(ticket(2, assignee={'id': OTHER}, completedAt='2026-10-20T01:00:00Z', updatedAt='2026-10-20T01:00:00Z'))]
        self.messages, self.files, self.reads, self.inactive = [], [], 0, set()
        self.fail_upload = None
        self.acknowledgements, self.fail_ack = [], None

    def verify(self, config):
        assert config.team == 'T123'

    def verify_recipient(self, user, team):
        assert team == 'T123'
        if user in self.inactive:
            raise TimesheetError('recipient_unavailable')

    def collect(self, config, end, deferred):
        self.reads += 1
        return self.dataset

    def open_dm(self, user):
        return 'D' + user

    def post_message(self, channel, content):
        self.messages.append((channel, content))
        return '123.456'

    def upload_csv(self, channel, filename, content, **kwargs):
        if self.fail_upload:
            raise self.fail_upload
        self.files.append((channel, filename, content))
        return 'F123'

    def post_invoice_ack(self, channel, text, *, thread_ts):
        self.acknowledgements.append((channel, text, thread_ts))
        if self.fail_ack:
            raise self.fail_ack
        return '123.789'


def service(config, *, api=None, scheduled=True):
    from roo.invoice_worker import InvoiceService
    from roo.studio_invoices import InvoiceConfig
    api = api or OfflineAPI()
    return InvoiceService(TimesheetService(config, api),
                          InvoiceConfig(True, scheduled, config.directory.parent / 'invoice-queue'))


def queue(service, *, actor='UBUILDER', selector='current', now=THURSDAY, source='123.456', **updates):
    request = dict(team='T123', actor=actor, channel='C123', source=source,
                   selector=selector, requested_at=now.isoformat(), status='pending')
    request.update(updates)
    key = fingerprint({k: request[k] for k in ('team', 'actor', 'channel', 'source')})
    with service.queue.locked(key) as ok:
        assert ok
        service.queue.write(key, request)
    return key


def test_draft_does_not_allocate_and_final_is_immutable_and_private(config):
    s = service(config)
    draft = s.artifact('UBUILDER', 'current', THURSDAY)
    assert not s.timesheets.ledger.load()['allocated']
    assert draft['invoice']['total_cents'] == 6500
    assert draft['invoice']['draft']
    final = s.artifact('UBUILDER', '2026-10-23', FRIDAY)
    assert not final['invoice']['draft']
    assert s.timesheets.ledger.load()['allocated'] == {'issue-1': '2026-10-23', 'issue-2': '2026-10-23'}
    s.api.dataset = []
    reads = s.api.reads
    again = service(config, api=s.api).artifact('UBUILDER', 'latest', FRIDAY + timedelta(days=1))
    assert again == final
    assert s.api.reads == reads
    text = '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(base64.b64decode(final['pdf']))).pages)
    assert 'STU-1' in text and 'STU-2' not in text


def test_corrections_carry_forward_without_rewriting_original(config):
    s = service(config)
    original = s.artifact('UBUILDER', '2026-10-23', FRIDAY)
    s.timesheets.correction(dict(id='fix1', period='2026-10-23', issue_id='issue-1', builder_id=USER,
                                 units=1, reason='Reviewed correction'), FRIDAY + timedelta(hours=1))
    assert s.artifact('UBUILDER', '2026-10-23', FRIDAY + timedelta(days=1)) == original
    next_final = s.artifact('UBUILDER', '2026-11-06', FRIDAY + timedelta(days=14))
    assert next_final['invoice']['total_cents'] == 1625
    assert next_final['invoice']['rows'][0]['kind'] == 'correction'


def test_member_mapping_and_active_account_are_required_and_sam_access_unchanged(config):
    s = service(config)
    for actor in ('USAM', 'UNKNOWN', 'UBUILDER invoice UOTHER'):
        with pytest.raises(TimesheetError, match='not_authorized'):
            s.artifact(actor, 'current', THURSDAY)
    s.api.inactive.add('UBUILDER')
    with pytest.raises(TimesheetError, match='recipient_unavailable'):
        s.artifact('UBUILDER', 'current', THURSDAY)
    with pytest.raises(TimesheetError, match='not_authorized'):
        s.timesheets.deliver_parts([], 'UBUILDER', 'bad', THURSDAY)


def test_rebinding_builder_to_different_slack_account_never_exposes_saved_final(config):
    s = service(config)
    s.artifact('UBUILDER', 'latest', FRIDAY)
    changed = replace(config, builders={**config.builders, USER: dict(name='new', slack_user_id='UNEW')})
    with pytest.raises(TimesheetError, match='invoice_owner_changed'):
        service(changed, api=s.api).artifact('UNEW', 'latest', FRIDAY)


def test_thursday_catchup_final_supersedes_draft_and_schedule_survives_restart(config):
    s = service(config)
    assert s.scheduled(THURSDAY - timedelta(seconds=1)) == []
    assert all(r['status'] == 'sent' for r in s.scheduled(THURSDAY))
    assert len(s.api.files) == 2
    assert not s.timesheets.ledger.load()['allocated']
    service(config, api=s.api).scheduled(THURSDAY + timedelta(hours=2))
    assert len(s.api.files) == 2
    service(config, api=s.api).scheduled(FRIDAY)
    assert len(s.api.files) == 4
    assert all('supersedes' in message for _, message in s.api.messages[-2:])
    service(config, api=s.api).scheduled(FRIDAY + timedelta(days=12))
    assert len(s.api.files) == 4


def test_missed_draft_is_never_sent_after_noon_and_no_earlier_invoices(config):
    s = service(config)
    s.scheduled(FRIDAY + timedelta(minutes=1))
    assert len(s.api.files) == 2
    assert all('final' in filename for _, filename, _ in s.api.files)
    assert not any('2026-10-09' in filename for _, filename, _ in s.api.files)


def test_definite_upload_failure_retries_only_pending_part_and_exact_artifact(config):
    s = service(config)
    s.api.fail_upload = DeliveryRejected('ratelimited', 60)
    assert all(r['status'] == 'pending' for r in s.scheduled(FRIDAY))
    assert len(s.api.messages) == 2
    s.api.fail_upload = None
    s.api.dataset = []
    service(config, api=s.api).scheduled(FRIDAY + timedelta(seconds=61))
    assert len(s.api.messages) == 2 and len(s.api.files) == 2


@pytest.mark.parametrize('failure', [DeliveryRejected('ratelimited', 60), TimeoutError('uncertain')])
def test_latest_retry_after_next_cutoff_delivers_original_selected_final(config, failure):
    s = service(config)
    key = queue(s, selector='latest', now=FRIDAY)
    s.api.fail_upload = failure
    s.commands(FRIDAY)
    s.api.fail_upload = None
    s.api.dataset.append(item(ticket(3, 'Medium (M)', completedAt='2026-11-05T01:00:00Z',
                                    updatedAt='2026-11-05T01:00:00Z')))
    if isinstance(failure, TimeoutError):
        for path in s.deliveries.directory.glob('invoice-command-*.json'):
            with s.deliveries.locked(path.stem) as acquired:
                assert acquired
                receipt = s.deliveries.read(path.stem)
                receipt['parts'][1]['status'] = 'pending'  # operator confirmed absent
                s.deliveries.write(path.stem, receipt)
    restarted = service(config, api=s.api)
    result = restarted.commands(FRIDAY + timedelta(days=14))
    assert result[0]['status'] == 'sent'
    assert restarted.queue.read(key)['status'] == 'done'
    assert len(s.api.files) == 1 and '2026-10-23' in s.api.files[0][1]
    assert sum('Your final Invoice' in text for _, text in s.api.messages) == 1


@pytest.mark.parametrize('delay_at,expected_draft_messages', [('verify', 0), ('open', 0), ('post', 1)])
def test_deadline_checked_after_each_slow_delivery_operation(config, monkeypatch, delay_at, expected_draft_messages):
    from roo import invoice_worker
    s = service(config)
    queue(s, now=FRIDAY - timedelta(seconds=1))
    clock = [0.0]
    monkeypatch.setattr(invoice_worker.time, 'monotonic', lambda: clock[0])
    target = {'verify': 'verify_recipient', 'open': 'open_dm', 'post': 'post_message'}[delay_at]
    original = getattr(s.api, target)
    calls = [0]
    def slow_once(*args, **kwargs):
        result = original(*args, **kwargs)
        calls[0] += 1
        if calls[0] == (2 if delay_at == 'verify' else 1):
            clock[0] = 2.0
        return result
    setattr(s.api, target, slow_once)
    # No outgoing draft PDF may start after noon. A command continues with final.
    s.commands(FRIDAY - timedelta(seconds=1))
    assert not any('draft' in filename for _, filename, _ in s.api.files)
    assert sum('Your draft Invoice' in text for _, text in s.api.messages) == expected_draft_messages
    assert len(s.api.files) == 1 and 'final' in s.api.files[0][1]
    assert all(b'$' not in data[:10] for _, _, data in s.api.files)
    assert all(data.startswith(b'%PDF') for _, _, data in s.api.files)


def test_ambiguous_upload_does_not_resend_without_operator_recovery(config):
    s = service(config)
    s.api.fail_upload = TimeoutError('unknown upload outcome')
    s.scheduled(FRIDAY)
    s.api.fail_upload = None
    result = service(config, api=s.api).scheduled(FRIDAY + timedelta(minutes=3))
    assert all(r['status'] == 'needs_review' for r in result)
    assert len(s.api.messages) == 2 and s.api.files == []


def test_queue_is_own_only_replay_safe_and_late_draft_returns_final(config):
    s = service(config)
    key = queue(s)
    result = s.commands(FRIDAY)
    assert result[0]['status'] == 'sent'
    assert s.queue.read(key)['status'] == 'done'
    assert len(s.api.files) == 1 and s.api.files[0][0] == 'DUBUILDER'
    assert 'final' in s.api.files[0][1]
    assert s.commands(FRIDAY) == []
    queue(s, actor='USAM', source='other')
    s.commands(FRIDAY)
    assert len(s.api.files) == 1
    queue(s, source='tampered')
    path = next(p for p in s.queue.directory.glob('*.json') if s.queue.read(p.stem)['status'] == 'pending')
    request = s.queue.read(path.stem)
    request['actor'] = 'UOTHER'
    s.queue.write(path.stem, request)
    s.commands(FRIDAY)
    assert len(s.api.files) == 1


def test_no_work_delivers_explanation_no_pdf_and_source_failures_never_become_zero(config):
    s = service(config)
    s.api.dataset = []
    queue(s)
    assert s.commands(THURSDAY)[0]['status'] == 'sent'
    assert s.api.files == []
    assert 'no payable' in s.api.messages[0][1].lower()
    queue(s, source='error')
    s.api.collect = lambda *args: (_ for _ in ()).throw(TimesheetError('source_unavailable'))
    assert s.commands(THURSDAY)[0]['status'] == 'error'
    assert 'no payable' not in s.api.messages[-1][1].lower()


def test_inactive_builder_does_not_block_other_scheduled_member(config):
    s = service(config)
    s.api.inactive.add('UBUILDER')
    result = s.scheduled(FRIDAY)
    assert {r['status'] for r in result} == {'sent', 'error'}
    assert [f[0] for f in s.api.files] == ['DUOTHER']


def test_concurrent_final_creation_reports_busy_without_duplicate(config):
    s = service(config)
    key = s.final_key(USER, '2026-10-23')
    with s.finals.locked(key) as locked:
        assert locked
        with pytest.raises(TimesheetError, match='invoice_busy'):
            service(config, api=s.api).artifact('UBUILDER', 'latest', FRIDAY)
    assert len(list(s.finals.directory.glob('*.json'))) == 0


def test_latest_retrieves_most_recent_payable_final_when_new_period_has_no_work(config):
    s = service(config)
    original = s.artifact('UBUILDER', 'latest', FRIDAY)
    s.api.dataset = []
    s.artifact('UBUILDER', '2026-11-06', FRIDAY + timedelta(days=14))
    assert s.artifact('UBUILDER', 'latest', FRIDAY + timedelta(days=14)) == original


def test_zero_payable_with_unresolved_evidence_explains_excluded_work(config):
    s = service(config)
    s.api.dataset[0]['issue']['labels'] = []
    queue(s)
    s.commands(THURSDAY)
    assert s.api.files == []
    assert 'review' in s.api.messages[0][1].lower()


def test_collection_crossing_friday_noon_does_not_deliver_obsolete_draft(config, monkeypatch):
    from roo import invoice_worker
    s = service(config)
    moments = iter([0, 60, 61])
    monkeypatch.setattr(invoice_worker.time, 'monotonic', lambda: next(moments))
    s.scheduled(FRIDAY - timedelta(seconds=30))
    assert s.api.messages == [] and s.api.files == []


def test_post_restart_operator_recovery_uses_existing_timesheet_receipts(config):
    s = service(config)
    s.api.fail_upload = TimeoutError()
    s.scheduled(FRIDAY)
    s.api.fail_upload = None
    for path in s.deliveries.directory.glob('invoice-*.json'):
        with s.deliveries.locked(path.stem) as acquired:
            assert acquired
            receipt = s.deliveries.read(path.stem)
            receipt['parts'][1]['status'] = 'pending'  # operator confirmed absent
            s.deliveries.write(path.stem, receipt)
    service(config, api=s.api).scheduled(FRIDAY + timedelta(minutes=3))
    assert len(s.api.messages) == 2 and len(s.api.files) == 2


@pytest.mark.parametrize('expire_after', ['url', 'upload'])
def test_pdf_sharing_deadline_checked_inside_slack_upload_protocol(expire_after):
    from roo.timesheet_worker import ReportAPI
    expired = [False]
    seen = []
    def respond(request):
        seen.append(str(request.url))
        if request.url.path.endswith('files.getUploadURLExternal'):
            expired[0] = expire_after == 'url'
            return httpx.Response(200, json={'ok': True, 'upload_url': 'https://files.slack.com/upload/test', 'file_id': 'F123'})
        if request.url.host == 'files.slack.com':
            expired[0] = expire_after == 'upload'
            return httpx.Response(200)
        return httpx.Response(200, json={'ok': True, 'files': [{'id': 'F123'}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = ReportAPI(client, linear_key='synthetic', slack_token='synthetic')
        with pytest.raises(TimesheetError, match='draft_expired'):
            api.upload_csv('D123', 'draft.pdf', b'%PDF-test', before_send=lambda: not expired[0])
    assert not any('completeUploadExternal' in url for url in seen)
    assert len(seen) == (1 if expire_after == 'url' else 2)


def test_channel_confirmation_waits_for_pdf_and_retries_without_resending(config):
    s = service(config)
    key = queue(s, thread_ts='123.450')
    s.api.fail_upload = DeliveryRejected('ratelimited', 60)
    s.commands(THURSDAY)
    assert s.api.acknowledgements == []
    s.api.fail_upload = None
    s.api.fail_ack = DeliveryRejected('ratelimited', 60)
    s.commands(THURSDAY + timedelta(seconds=61))
    assert len(s.api.files) == 1
    assert s.api.acknowledgements == [('C123', '<@UBUILDER> I’ve sent your invoice privately. Check your DMs.', '123.450')]
    assert s.queue.read(key)['status'] != 'done'
    s.api.fail_ack = None
    # Crossing Friday must only retry the public confirmation, preserving the delivered draft.
    restarted = service(config, api=s.api)
    restarted.commands(FRIDAY)
    assert len(s.api.files) == 1 and 'draft' in s.api.files[0][1]
    assert s.queue.read(key)['status'] == 'done'
    assert len(s.api.acknowledgements) == 2
    assert restarted.commands(FRIDAY) == []


def test_no_work_confirms_private_explanation_without_claiming_invoice(config):
    s = service(config)
    s.api.dataset = []
    queue(s, thread_ts='123.456')
    s.commands(THURSDAY)
    assert s.api.files == []
    assert s.api.acknowledgements == [('C123', '<@UBUILDER> I’ve sent you a private update about your invoice request. Check your DMs.', '123.456')]


def test_uncertain_channel_confirmation_needs_review_without_repeat(config):
    s = service(config)
    key = queue(s, thread_ts='123.456')
    s.api.fail_ack = TimeoutError('unknown outcome')
    s.commands(THURSDAY)
    s.api.fail_ack = None
    restarted = service(config, api=s.api)
    assert restarted.commands(FRIDAY)[0]['status'] == 'needs_review'
    assert len(s.api.files) == 1 and len(s.api.acknowledgements) == 1
    assert s.queue.read(key)['status'] == 'needs_review'


def test_crash_after_private_receipt_recovers_confirmation_without_new_final(config, monkeypatch):
    s = service(config)
    key = queue(s, thread_ts='123.456')
    original = s.queue.write
    def crash_before_checkpoint(key, request):
        if request.get('delivered'):
            raise OSError('process stopped before queue checkpoint')
        return original(key, request)
    monkeypatch.setattr(s.queue, 'write', crash_before_checkpoint)
    s.commands(THURSDAY)
    assert len(s.api.files) == 1 and s.api.acknowledgements == []
    restarted = service(config, api=s.api)
    restarted.commands(FRIDAY)
    assert len(s.api.files) == 1 and 'draft' in s.api.files[0][1]
    assert len(s.api.acknowledgements) == 1
    assert restarted.queue.read(key)['status'] == 'done'


@pytest.mark.parametrize('updates', [dict(channel='D123'), dict(thread_ts=None), dict(actor='USAM')])
def test_dm_slash_and_unmapped_requests_never_publish_confirmation(config, updates):
    s = service(config)
    queue(s, **updates)
    s.commands(THURSDAY)
    assert s.api.acknowledgements == []


def test_invoice_channel_confirmation_provider_uses_thread_and_no_unfurls():
    from roo.timesheet_worker import ReportAPI
    import json
    seen = []
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'ok': True, 'channel': 'G123', 'ts': '123.789'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        api = ReportAPI(client, linear_key='synthetic', slack_token='synthetic')
        assert api.post_invoice_ack('G123', 'Sent privately.', thread_ts='123.456') == '123.789'
    assert seen == [{'channel': 'G123', 'text': 'Sent privately.', 'thread_ts': '123.456',
                     'unfurl_links': False, 'unfurl_media': False}]
