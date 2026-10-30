"""Single-project onboarding parity; every identity and finance record is synthetic."""
import base64
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from roo.studio_report_worker import StudioReportService
from roo.studio_reports import total_units
from tests.roo.test_studio_client_balance_source import (
    NOW, P1, P2, setup as finance_setup,
)
from tests.roo.test_studio_reports import ticket
from roo.timesheets import TimesheetError


@pytest.fixture
def devashi(finance_setup):
    """Two independent artificial owners, with staff explicitly granted both."""
    base = finance_setup
    base.config = replace(base.config, projects={
        P1: '[Studio] Master App', P2: '[Studio] Study Nash',
    })
    del base.clients['UOTHER']
    base.clients['UMARK']['name'] = 'Mark Ghiasy'
    base.clients['UDEVASHI'] = {
        'name': 'Devarshi Desai', 'aliases': ['Devashi Desai'],
        'project_ids': [P2], 'monthly_hours': 40,
    }
    base.clients['USAM']['report_client_ids'] = ['UMARK', 'UDEVASHI']
    builder = next(iter(base.config.builders))
    base.dataset.update(hours_reporting=True)
    base.dataset['hours'][0].update(builder_id=builder, description='Mark private work sentinel')

    # These quantities, rate, dates and receipts are test fixtures, never client
    # contract settings or evidence for the real Study Nash project.
    source = deepcopy(base.dataset)
    source['project_ids'] = [P2]
    source['contractor_invoices'][0]['hour_units'] = 6_250_000
    source['hours'][0].update(project_id=P2, hour_units=6_250_000,
                              description='Study Nash synthetic work')
    source['invoices'][0].update(amount_cents=12500, paid_cents=12500)
    source['payments'][0].update(amount_cents=12500,
        allocations=[{'invoice_id': 'INV1', 'amount_cents': 12500}])
    base.value['clients']['UDEVASHI'] = source
    base.path.write_text(json.dumps(base.value))
    base.api.open_dm.side_effect = lambda actor: 'D' + actor
    base.service = StudioReportService(base.config, base.api, base.clients,
                                       balance_path=base.path, balance_enabled=True)
    base.devashi_source = source
    return base


def request(actor='UDEVASHI', kind='hours', **selector):
    return {'actor': actor, 'team': 'T123', 'report_kind': kind,
            'selector': {'month': 'all', 'action': 'detailed', **selector},
            'requested_at': NOW.isoformat()}


@pytest.mark.parametrize('actor,selector', [
    ('UDEVASHI', {}), ('USAM', {'client': 'Devarshi Desai'}),
    ('USAM', {'client': 'Devashi Desai'}),
])
def test_single_project_hours_and_burndown_share_exact_source_and_private_delivery(devashi, actor, selector):
    hours_request = request(actor, **selector)
    balance_request = request(actor, 'client_balance', **selector)
    hours = devashi.service.report_for_request(hours_request)
    balance = devashi.service.report_for_request(balance_request)

    assert set(hours['projects']) == {P2}
    assert hours['client'] == balance['client'] == 'Devarshi Desai'
    assert {row['project_id'] for row in hours['rows']} == {P2}
    assert hours['finance_owners'] == ['UDEVASHI']
    assert balance['finance_owner'] == 'UDEVASHI'
    assert total_units(hours) == total_units(balance['hours_report']) == balance['used_hour_units'] == 6_250_000
    assert balance['credited_hour_units'] == 1_000_000
    assert balance['balance_hour_units'] == 5_250_000
    assert len([point for point in balance['points'] if point['stage'] == 'after_payment']) == 1
    assert 'Mark private work sentinel' not in json.dumps([hours, balance])
    filenames = {part.get('filename') for part in balance['parts']}
    assert {'studio-client-balance.png', 'studio-hours.png', 'studio-hours-projects.png'} <= filenames
    for part in balance['parts']:
        if part.get('filename', '').endswith('.png'):
            assert base64.b64decode(part['content']).startswith(b'\x89PNG')
    devashi.api.collect.assert_not_called()
    devashi.api.verify.assert_not_called()
    devashi.api.post_message.assert_not_called()

    devashi.service.deliver_request(hours, hours_request, actor + '-hours', NOW)
    devashi.service.deliver_request(balance, balance_request, actor + '-balance', NOW)
    assert all(call.args == (actor,) for call in devashi.api.open_dm.call_args_list)
    assert all(call.args[0] == 'D' + actor for call in devashi.api.post_message.call_args_list)
    assert all(call.args[0] == 'D' + actor for call in devashi.api.upload_csv.call_args_list)


@pytest.mark.parametrize('kind', ['hours', 'client_balance'])
@pytest.mark.parametrize('actor,client', [
    ('UDEVASHI', 'Mark Ghiasy'), ('UMARK', 'Devarshi Desai'),
    ('UMARK', 'Devashi Desai'),
])
def test_owner_names_never_grant_other_clients_reports(devashi, monkeypatch, kind, actor, client):
    import roo.studio_client_balance_source as source
    read = lambda *args, **kwargs: pytest.fail('Cross-client request read private finance records')
    monkeypatch.setattr(source, 'load_balance_snapshot', read)
    with pytest.raises(TimesheetError, match='report_client_unavailable'):
        devashi.service.report_for_request(request(actor, kind, client=client))
    devashi.api.collect.assert_not_called()
    devashi.api.verify_recipient.assert_not_called()
    devashi.api.open_dm.assert_not_called()


def test_new_request_rereads_devashi_finance_and_old_snapshot_cannot_deliver(devashi):
    req = request(kind='client_balance')
    first = devashi.service.report_for_request(req)
    devashi.devashi_source['hours'][0]['hour_units'] = 8_500_000
    devashi.devashi_source['contractor_invoices'][0]['hour_units'] = 8_500_000
    devashi.path.write_text(json.dumps(devashi.value))
    second = devashi.service.report_for_request(req)
    assert first['used_hour_units'] == 6_250_000
    assert second['used_hour_units'] == total_units(second['hours_report']) == 8_500_000
    assert second['balance_hour_units'] == 7_500_000
    with pytest.raises(TimesheetError, match='client_balance_source_changed'):
        devashi.service.deliver_request(first, req, 'outdated-devashi', NOW)
    devashi.api.collect.assert_not_called()
    devashi.api.open_dm.assert_not_called()


def test_full_balance_service_renders_gst_inclusive_package_at_exact_receipt_rate(devashi):
    source = devashi.devashi_source
    source['allocation_rate_cents_per_hour'] = 13750
    source['invoices'][0].update(amount_cents=550000, paid_cents=550000)
    source['payments'][0].update(amount_cents=550000,
        allocations=[{'invoice_id': 'INV1', 'amount_cents': 550000}])
    devashi.path.write_text(json.dumps(devashi.value))

    report = devashi.service.report_for_request(request(kind='client_balance'))

    assert report['used_hour_units'] == total_units(report['hours_report']) == 6_250_000
    assert report['payment_cents'] == 550000
    assert report['credited_hour_units'] == 40_000_000
    assert report['balance_hour_units'] == -33_750_000
    text = '\n'.join(part['content'] for part in report['parts'] if part['kind'] == 'message')
    assert 'Hours purchased: *40h*' in text
    assert 'A$5,500.00 received' in text
    assert 'A$137.50/h' in text
    charts = [part for part in report['parts'] if part.get('filename', '').endswith('.png')]
    assert {part['filename'] for part in charts} == {
        'studio-client-balance.png', 'studio-hours.png', 'studio-hours-projects.png',
    }
    assert all(base64.b64decode(part['content']).startswith(b'\x89PNG') for part in charts)
    devashi.api.collect.assert_not_called()
    devashi.api.post_message.assert_not_called()
    devashi.api.upload_csv.assert_not_called()


def test_ticket_based_monthly_requests_read_fresh_and_keep_study_nash_scope(devashi):
    devashi.service.balance_enabled = False
    private = ticket(1, project=P1, completed='2026-10-05T00:00:00Z', title='Mark private work sentinel')
    current = ticket(2, project=P2, completed='2026-10-05T00:00:00Z')
    newer = ticket(3, project=P2, size='Extra Large (XL)', completed='2026-10-06T00:00:00Z')
    devashi.api.collect.side_effect = [[private, current], [private, newer]]
    req = request(month='current')
    first = devashi.service.report_for_request(req)
    second = devashi.service.report_for_request(req)
    assert first['monthly'][0]['units'] == 4
    assert second['monthly'][0]['units'] == 20
    assert first['monthly'][0]['allowance_units'] == second['monthly'][0]['allowance_units'] == 160
    assert devashi.api.collect.call_count == 2
    assert all(set(call.args[0].projects) == {P2} for call in devashi.api.collect.call_args_list)
    assert 'Mark private work sentinel' not in json.dumps([first, second])
    devashi.api.open_dm.assert_not_called()


@pytest.mark.parametrize('missing,error', [
    ('owner', 'client_balance_access_unavailable'), ('file', 'client_balance_source_unavailable'),
])
def test_missing_devashi_finance_never_becomes_zero_or_another_clients_balance(devashi, missing, error):
    if missing == 'owner':
        del devashi.value['clients']['UDEVASHI']
        devashi.path.write_text(json.dumps(devashi.value))
    else:
        devashi.path.unlink()
    with pytest.raises(TimesheetError, match=error):
        devashi.service.report_for_request(request(kind='client_balance'))
    devashi.api.collect.assert_not_called()
    devashi.api.post_message.assert_not_called()
    devashi.api.upload_csv.assert_not_called()
    devashi.api.open_dm.assert_not_called()
