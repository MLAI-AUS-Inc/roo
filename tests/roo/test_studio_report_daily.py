"""Synthetic daily refresh, partial data and finance classification regressions."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import Mock

import pytest

from roo.studio_report_daily import DailyRefresh
from roo.studio_report_refresh import SourcePending
from roo.studio_client_balance import build_balance_report, summary
from roo.studio_client_balance_source import validate_balance_snapshot
from roo.studio_recorded_hours import build_recorded_report
from roo.timesheets import TimesheetError, timestamp
from tests.roo.test_studio_client_balance_source import setup
from tests.roo.test_studio_report_refresh import configure, clock, FRIDAY


def latest(setup):
    refresh, http, template = configure(setup)
    refresh.latest_enabled = True
    template.update(mode='latest', status='partial', missing=['hours_coverage_missing', 'new_invoices_require_review'])
    checks = template['checks']
    review = FRIDAY - timedelta(days=1)
    checks['hours'].update(reviewed_at=review.isoformat(), complete_through=review.isoformat())
    checks['contractor_invoices']['unreviewed_count'] = 2
    data = template['snapshot']['clients']['UMARK']
    data['source_freshness'] = {'finance_checked_at': FRIDAY.isoformat(),
        'hours_complete_through': review.isoformat(), 'hours_reviewed_at': review.isoformat(),
        'latest_work_date': '2026-10-12', 'unreviewed_invoice_count': 2, 'missing': template['missing']}
    return refresh, http, template


def test_latest_preserves_review_gaps_and_cannot_satisfy_strict_report(setup):
    refresh, http, template = latest(setup)
    key, value, checks = refresh.prepare('UMARK', FRIDAY, FRIDAY, mode='latest')
    assert checks['contractor_invoices']['unreviewed_count'] == 2
    assert refresh.frozen(key, 'UMARK', FRIDAY, FRIDAY, mode='latest') == value
    assert refresh.identity('UMARK', FRIDAY) != key
    assert refresh.mode({'selector': {'period': 'weekly'}}) == 'strict'
    with pytest.raises(SourcePending):
        refresh.prepare('UMARK', FRIDAY, FRIDAY)


@pytest.mark.parametrize('mutate', [
    lambda r: r['missing'].append('finance_sync_failed'),
    lambda r: r['snapshot']['clients']['UMARK']['source_freshness'].update(hours_reviewed_at=FRIDAY.isoformat()),
    lambda r: r['snapshot']['clients']['UMARK']['source_freshness'].update(latest_work_date='2026-10-16'),
    lambda r: r['checks']['payments'].update(checked_at=(FRIDAY - timedelta(days=1)).isoformat()),
])
def test_latest_rejects_forged_freshness_or_finance_gaps(setup, mutate):
    refresh, _, template = latest(setup)
    mutate(template)
    with pytest.raises(SourcePending):
        refresh.prepare('UMARK', FRIDAY, FRIDAY, mode='latest')


def test_cash_only_receipt_cannot_buy_hours_or_disappear_from_invoice_status(setup):
    value = deepcopy(setup.value)
    data = value['clients']['UMARK']
    data['invoices'][0]['credit_eligible'] = False
    data['payments'][0]['credit_amount_cents'] = 0
    validate_balance_snapshot(value, setup.config, setup.clients)
    report = build_balance_report(setup.clients['UMARK'], {'month': 'all'}, data, data['as_of'])
    assert report['lifetime_credited_hour_units'] == 0
    assert report['lifetime_payment_cents'] == 10000
    assert report['invoices'][0]['paid_cents'] == 10000
    data['payments'][0]['credit_amount_cents'] = 10000
    with pytest.raises(TimesheetError):
        validate_balance_snapshot(value, setup.config, setup.clients)


def test_november_work_is_unavailable_after_october_records_despite_new_cash_sync(setup):
    _, _, template = latest(setup)
    data = template['snapshot']['clients']['UMARK']
    data['as_of'] = '2026-11-02T00:00:00Z'
    data['coverage']['through'] = '2026-11-02'
    data['source_freshness']['finance_checked_at'] = data['as_of']
    report = build_recorded_report(setup.config, setup.clients['UMARK'], {'month': '2026-11'}, data, data['as_of'])
    assert not report['period_has_coverage']
    assert report['monthly'][0]['coverage_available'] is False
    assert report['source_as_of'] == data['source_freshness']['hours_reviewed_at']
    balance = build_balance_report(setup.clients['UMARK'], {'month': '2026-11', 'action': 'summary'}, data, data['as_of'])
    assert not balance['work_period_has_coverage']
    assert 'unavailable for this period; not counted as zero' in summary(balance)


def test_latest_on_demand_report_discloses_work_and_finance_dates(setup):
    latest(setup)
    report = setup.service.report_for_request({'actor': 'UMARK', 'team': setup.config.team,
        'report_kind': 'client_balance', 'selector': {'month': 'all', 'action': 'summary'},
        'requested_at': FRIDAY.isoformat()})
    assert 'Xero checked:' in report['parts'][0]['content']
    assert '2 invoice emails await review' in report['parts'][0]['content']
    assert report['source_preparations']
    assert report['hours_report']['coverage_through'] == '2026-10-12'
    setup.api.post_message.assert_not_called()


def test_daily_publishes_validated_charts_once_across_restarts_without_delivery(setup):
    _, http, _ = latest(setup)
    daily = DailyRefresh(setup.service, ['UMARK'])
    assert daily.tick(FRIDAY)[0]['status'] == 'done'
    assert DailyRefresh(setup.service, ['UMARK']).tick(FRIDAY + timedelta(minutes=1)) == []
    assert http.post.call_count == 1
    published = json.loads(setup.path.read_text())
    assert published['clients']['UMARK']['source_freshness']['unreviewed_invoice_count'] == 2
    images = list((setup.config.directory / 'daily-reports').rglob('*.png'))
    assert len(images) == 3 and all(p.read_bytes().startswith(b'\x89PNG') for p in images)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in images)
    setup.api.post_message.assert_not_called()
    setup.api.upload_csv.assert_not_called()
    setup.api.collect.assert_not_called()


def test_failed_render_preserves_last_good_snapshot_and_retries(setup, monkeypatch):
    latest(setup)
    before = setup.path.read_bytes()
    monkeypatch.setattr('roo.studio_report_daily.render_bundle', Mock(side_effect=RuntimeError('secret')))
    daily = DailyRefresh(setup.service, ['UMARK'])
    result = daily.tick(FRIDAY)
    assert result[0]['status'] == 'error' and 'secret' not in json.dumps(result)
    assert setup.path.read_bytes() == before
    assert daily.tick(FRIDAY + timedelta(seconds=30)) == []


def test_daily_does_not_overwrite_a_newer_manual_review(setup, monkeypatch):
    latest(setup)
    # A separate reviewer published after the source's work-review cutoff,
    # even though the incoming Xero read itself happened later.
    value = deepcopy(setup.value)
    value['clients']['UMARK']['as_of'] = (FRIDAY - timedelta(hours=1)).isoformat()
    setup.path.write_text(json.dumps(value))
    before = setup.path.read_bytes()
    monkeypatch.setattr('roo.studio_report_daily.render_bundle', Mock(return_value={'report.txt': 'synthetic'}))
    assert DailyRefresh(setup.service, ['UMARK']).tick(FRIDAY)[0]['status'] == 'error'
    assert setup.path.read_bytes() == before


@pytest.mark.parametrize('day,offset', [('2026-10-04', '+11:00'), ('2026-11-01', '+11:00'),
                                       ('2027-01-01', '+11:00'), ('2027-04-04', '+10:00')])
def test_daily_schedule_handles_month_year_and_dst_rollover(setup, monkeypatch, day, offset):
    latest(setup)
    daily = DailyRefresh(setup.service, ['UMARK'])
    setup.service.refresh.prepare = Mock(side_effect=SourcePending(timestamp(day + 'T07:00:00' + offset).timestamp()))
    assert daily.tick(timestamp(day + 'T05:59:00' + offset)) == []
    assert daily.tick(timestamp(day + 'T06:00:00' + offset))[0]['status'] == 'waiting_data'
    assert daily.tick(timestamp(day + 'T06:59:00' + offset)) == []
    assert setup.service.refresh.prepare.call_count == 1


def test_daily_requires_explicit_flags_and_owner_scope(setup):
    latest(setup)
    assert DailyRefresh.from_env(setup.service, {}) is None
    with pytest.raises(TimesheetError):
        DailyRefresh.from_env(setup.service, {'STUDIO_REPORTS_DAILY_REFRESH_ENABLED': 'true'})
    with pytest.raises(TimesheetError):
        DailyRefresh(setup.service, ['UNOTGRANTED'])


def test_retry_after_midnight_resumes_previous_day_until_next_scheduled_run(setup):
    latest(setup)
    before = timestamp('2026-10-16T23:50:00+11:00')
    after = timestamp('2026-10-17T00:10:00+11:00')
    setup.service.refresh.prepare = Mock(side_effect=SourcePending(after.timestamp()))
    daily = DailyRefresh(setup.service, ['UMARK'])
    assert daily.tick(before)[0]['status'] == 'waiting_data'
    assert daily.tick(after - timedelta(seconds=1)) == []
    assert daily.tick(after)[0]['status'] == 'waiting_data'
    assert setup.service.refresh.prepare.call_count == 2


def test_manual_catchup_before_six_does_not_consume_next_daily_run(setup):
    latest(setup)
    now = timestamp('2026-10-17T00:10:00+11:00')
    setup.service.refresh.prepare = Mock(side_effect=SourcePending(now.timestamp() + 300))
    daily = DailyRefresh(setup.service, ['UMARK'])
    assert daily.tick(now) == []
    assert daily.tick(now, catch_up=True)[0]['status'] == 'waiting_data'
    assert daily.tick(timestamp('2026-10-17T06:00:00+11:00'))[0]['status'] == 'waiting_data'
