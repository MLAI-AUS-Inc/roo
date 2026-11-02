"""Contract renewal, carry expiry, opening history and private access regressions."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from roo.studio_contract_cycles import anniversary, commencement, validate_contract_policy
from roo.studio_report_worker import configuration
from roo.studio_reports import TZ, artifacts, build_client_report, summary, total_units
from roo.studio_task_burndown import render_task_burndown
from roo.timesheets import TimesheetError, timestamp
from tests.roo.test_studio_reports import P1, P3, setup, ticket
from tests.roo.test_studio_task_burndown import plotted

NOW = timestamp('2026-11-02T03:15:00Z')


def terms(project=P3, **updates):
    return {'version': 1, 'project_ids': [project], 'commencement_date': '2026-10-16',
            'renewal': 'monthly', 'included_hours': 40, 'rollover_percent': 50,
            'rollover_cap_hours': 20, 'rollover_cycles': 1, 'rollover_first': True,
            'source': {'drive_file_id': 'synthetic_signed_file', 'sha256': 'a' * 64,
                       'signed_at': '2026-10-16T04:42:39Z', 'reviewed_at': '2026-11-02T03:00:00Z',
                       'reviewed_by': 'USAM'}, **updates}


@pytest.fixture
def contract(setup):
    setup.clients['UOTHER'].update(hours_source='linear', history_start_month='2026-08',
                                   contract_terms=terms())
    return setup


def work(number=1, **updates):
    return ticket(number, project=P3, completed='2026-10-21T01:00:00Z', **updates)


def report(contract, data, *, now=NOW, **selector):
    return build_client_report(contract.config, contract.clients['UOTHER'], selector, data, now)


def request(actor='UOTHER', **selector):
    return {'actor': actor, 'team': 'T123', 'selector': selector, 'requested_at': NOW.isoformat()}


def eight_hours():
    return [work(1, size='Extra Large (XL)'), work(2, size='Large (L)')]


def test_november_usage_keeps_october_cycle_balance_and_chart(contract, plotted):
    value = report(contract, eight_hours(), month='2026-11', action='detailed')
    assert total_units(value) == 0 and value['rows'] == []
    assert value['monthly'][0]['allowance_units'] is None
    cycle, = value['contract_allowance']['cycles']
    assert cycle['start'].startswith('2026-10-16T00:00:00+11:00')
    assert cycle['end'].startswith('2026-11-16T00:00:00+11:00')
    assert (cycle['used_hours'], cycle['remaining_hours'], cycle['carry_in_hours']) == ('8', '32', '0')
    text = summary(value)
    assert 'November 2026 — 0 hours used' in text
    assert '16 Oct 2026 – 15 Nov 2026' in text
    assert '*32h estimated remaining*' in text and 'Next renewal: 16 Nov 2026' in text
    assert 'rollover is not included' not in text and 'no rollover' not in text
    assert '32' in artifacts(value)['studio-hours-allowance-cycles.csv']
    assert render_task_burndown(value).startswith(b'\x89PNG')
    dates, values, _ = plotted['steps'][0]
    assert values == [40, 32, 32]
    assert dates[0].day == 16 and max(dates) == NOW.astimezone(TZ)
    assert 'next renewal 16 Nov 2026' in '\n'.join(plotted['text'])
    assert 'synthetic_signed_file' not in json.dumps(value)


def test_only_contract_anniversary_adds_new_hours_and_rollover(contract, plotted):
    value = report(contract, eight_hours(), month='2026-10', months=2,
                   now=timestamp('2026-11-16T00:00:00Z'))
    first, second = value['contract_allowance']['cycles']
    assert first['remaining_hours'] == '32' and first['rollover_out_hours'] == '16'
    assert second['included_hours'] == '40' and second['carry_in_hours'] == '16'
    assert second['remaining_hours'] == '56'
    render_task_burndown(value)
    assert [step[1] for step in plotted['steps']] == [[40, 32, 32], [56, 56]]
    assert plotted['steps'][1][0][0].date().isoformat() == '2026-11-16'


def test_rollover_used_first_expires_and_never_accumulates(contract):
    # 32h unused -> 16h carry. Spend only 5h of carry; 11h expires. The untouched
    # 40h base produces 20h carry, not half of all 51 remaining hours.
    data = eight_hours() + [ticket(3, project=P3, size='Extra Large (XL)', completed='2026-11-17T00:00:00Z')]
    value = report(contract, data, month='2026-10', months=3, now=timestamp('2026-12-17T00:00:00Z'))
    first, second, third = value['contract_allowance']['cycles']
    assert second['used_hours'] == '5' and second['remaining_hours'] == '51'
    assert second['base_unused_hours'] == '40'
    assert second['expired_rollover_hours'] == '11'
    assert second['rollover_out_hours'] == '20' and third['remaining_hours'] == '60'


def test_rollover_cap_and_no_rounding_of_half_quarter_hour(contract):
    value = report(contract, [work(size='Extra Small (XS)')], month='2026-11',
                   now=timestamp('2026-11-17T00:00:00Z'))
    first, second = value['contract_allowance']['cycles']
    assert first['rollover_out_hours'] == '19.875'
    assert second['carry_in_hours'] == '19.875' and second['remaining_hours'] == '59.875'
    assert '59.875h' in summary(value)
    contract.clients['UOTHER']['contract_terms']['rollover_cap_hours'] = 10
    value = report(contract, [], month='2026-11', now=timestamp('2026-11-17T00:00:00Z'))
    assert value['contract_allowance']['cycles'][-1]['carry_in_hours'] == '10'


def test_usage_consumes_carry_before_base_when_it_exceeds_carry(contract):
    later = [ticket(10 + index, project=P3, size=size, completed='2026-11-17T00:00:00Z')
             for index, size in enumerate(['Extra Large (XL)'] * 6)]
    value = report(contract, eight_hours() + later, month='2026-10', months=3,
                   now=timestamp('2026-12-17T00:00:00Z'))
    _, second, third = value['contract_allowance']['cycles']
    assert second['used_hours'] == '30'
    assert second['expired_rollover_hours'] == '0'
    assert second['base_unused_hours'] == '26' and second['rollover_out_hours'] == '13'
    assert third['remaining_hours'] == '53'


def test_multiple_projects_share_one_reviewed_allocation(contract):
    client = contract.clients['UOTHER']
    client['project_ids'] = [P1, P3]
    client['contract_terms']['project_ids'] = [P1, P3]
    value = report(contract, [work(), ticket(2, completed='2026-10-21T01:00:00Z')])
    cycle, = value['contract_allowance']['cycles']
    assert cycle['included_hours'] == '40' and cycle['remaining_hours'] == '38'


def test_zero_rollover_contract_is_supported_without_default_credits(contract):
    contract.clients['UOTHER']['contract_terms'].update(rollover_percent=0, rollover_cap_hours=0)
    value = report(contract, [], month='2026-11', now=timestamp('2026-11-17T00:00:00Z'))
    assert value['contract_allowance']['cycles'][-1]['remaining_hours'] == '40'


def test_precommencement_window_has_no_balance_or_chart(contract):
    value = report(contract, [ticket(1, project=P3)], month='2026-09')
    assert not value['contract_allowance']['cycles']
    assert 'Allowance starts 2026-10-16' in summary(value)
    assert render_task_burndown(value) is None


def test_exact_renewal_boundary_melbourne_and_no_precontract_consumption(contract):
    data = [ticket(1, project=P3, size='Extra Large (XL)', completed='2026-10-15T12:59:59Z'),
            ticket(2, project=P3, completed='2026-10-15T13:00:00Z'),
            ticket(3, project=P3, completed='2026-11-15T12:59:59Z'),
            ticket(4, project=P3, completed='2026-11-15T13:00:00Z')]
    value = report(contract, data, month='2026-10', months=2, now=timestamp('2026-11-15T13:00:00Z'))
    assert total_units(value) == 32  # All calendar history includes the pre-contract 5h.
    first, second = value['contract_allowance']['cycles']
    assert first['used_hours'] == '2' and first['rollover_out_hours'] == '19'
    assert second['used_hours'] == '1' and second['remaining_hours'] == '58'
    bounded = report(contract, data, period='weekly', start='2026-11-09T00:00:00+11:00',
                     end='2026-11-16T00:00:00+11:00', now=timestamp('2026-11-16T04:00:00Z'))
    assert total_units(bounded) == 4
    assert len(bounded['contract_allowance']['cycles']) == 1


def test_historical_cutoff_never_applies_later_work_or_future_renewal(contract, plotted):
    data = eight_hours() + [ticket(3, project=P3, completed='2026-11-01T03:00:00Z')]
    value = report(contract, data, month='2026-10')
    cycle, = value['contract_allowance']['cycles']
    assert cycle['remaining_hours'] == '32'
    assert cycle['as_of'] == '2026-11-01T00:00:00+11:00'
    render_task_burndown(value)
    assert max(plotted['steps'][0][0]) == timestamp(cycle['as_of'])


def test_unresolved_prior_work_withholds_balance_and_future_rollover(contract, plotted):
    value = report(contract, [work(labels=[])], month='2026-11', now=timestamp('2026-11-17T00:00:00Z'))
    assert value['complete']  # November's calendar usage itself is empty and complete.
    first, second = value['contract_allowance']['cycles']
    assert not first['complete'] and first['remaining_hours'] is None
    assert first['rollover_out_hours'] is None
    assert second['carry_in_hours'] is None and second['remaining_hours'] is None
    assert second['points'] == []
    assert 'remaining hours and rollover are unconfirmed' in summary(value)
    assert 'STU-1' in artifacts(value)['studio-hours-cycle-unresolved.csv']
    render_task_burndown(value)
    assert len(plotted['steps']) == 1 and plotted['steps'][0][2]['linestyle'] == '--'


def test_precontract_unresolved_work_does_not_poison_contract_balance(contract):
    value = report(contract, [ticket(1, project=P3, completed='2026-10-14T00:00:00Z', labels=[])], month='2026-10')
    assert not value['complete'] and value['contract_allowance']['complete']
    assert value['contract_allowance']['cycles'][0]['remaining_hours'] == '40'


def test_overage_does_not_roll_negative_credit_forward(contract):
    contract.clients['UOTHER']['contract_terms']['included_hours'] = 2
    value = report(contract, [work(size='Extra Large (XL)')], month='2026-11', now=timestamp('2026-11-17T00:00:00Z'))
    first, second = value['contract_allowance']['cycles']
    assert first['remaining_hours'] == '-3' and first['rollover_out_hours'] == '0'
    assert second['remaining_hours'] == '2'


@pytest.mark.parametrize('start,expected', [
    ('2027-01-31', ['2027-01-31', '2027-02-28', '2027-03-31']),
    ('2028-01-31', ['2028-01-31', '2028-02-29', '2028-03-31']),
    ('2026-09-16', ['2026-09-16', '2026-10-16', '2026-11-16']),
])
def test_anniversary_uses_months_not_thirty_days_and_preserves_anchor(start, expected):
    anchor = commencement(terms(commencement_date=start))
    dates = [anniversary(anchor, index) for index in range(3)]
    assert [at.date().isoformat() for at in dates] == expected
    if start == '2026-09-16':
        assert dates[0].utcoffset() == timedelta(hours=10)
        assert dates[1].utcoffset() == timedelta(hours=11)


def test_worker_collects_from_contract_start_but_keeps_calendar_usage_and_access_scoped(contract):
    contract.api.collect.return_value = eight_hours() + [ticket(99, project=P1, title='Mark sentinel')]
    value = contract.service.report_for_request(request(month='current'))
    source = contract.api.collect.call_args.args[0]
    assert source.beginning.isoformat() == '2026-10-16T00:00:00+11:00'
    assert set(source.projects) == {P3}
    assert 'Mark sentinel' not in json.dumps(value)
    assert total_units(value) == 0
    assert value['contract_allowance']['cycles'][0]['remaining_hours'] == '32'
    assert 'studio-hours-burndown.png' in {part.get('filename') for part in value['parts']}
    contract.api.open_dm.assert_not_called()


def test_staff_named_client_uses_owner_contract_and_changed_terms_invalidate_delivery(contract):
    contract.clients['USAM'] = {'name': 'Sam', 'project_ids': [P1, P3], 'monthly_hours': None,
                              'report_client_ids': ['UOTHER']}
    contract.api.collect.return_value = eight_hours()
    req = request('USAM', month='current', client='Other')
    value = contract.service.report_for_request(req)
    assert value['contract_allowance']['cycles'][0]['remaining_hours'] == '32'
    contract.clients['UOTHER']['contract_terms']['commencement_date'] = '2026-10-17'
    with pytest.raises(TimesheetError, match='client_access_changed'):
        contract.service.deliver_request(value, req, 'stale-terms', NOW)
    contract.api.open_dm.assert_not_called()
    aggregate = contract.service.report_for_request(request('USAM', month='current', client='all'))
    assert 'contract_allowance' not in aggregate
    assert all(m['allowance_units'] is None for m in aggregate['monthly'])


def test_client_all_selector_does_not_apply_contract_as_aggregate_allowance(contract):
    value = contract.service.report_for_request(request(month='current', client='all'))
    assert 'contract_allowance' not in value


def test_mark_exemption_keeps_calendar_behavior_and_source_unchanged(contract, plotted):
    env = {**contract.env, 'STUDIO_REPORTS_REQUIRE_CONTRACT_TERMS': 'true',
           'STUDIO_REPORTS_CONTRACT_EXEMPT_CLIENT_IDS_JSON': '["UMARK"]',
           'STUDIO_REPORTS_CLIENTS_JSON': json.dumps(contract.clients)}
    _, clients = configuration(env)
    assert clients['UMARK'] == contract.clients['UMARK']
    value = build_client_report(contract.config, clients['UMARK'], {'month': '2026-10', 'months': 2},
                                [ticket(completed='2026-10-20T00:00:00Z')], NOW)
    assert 'contract_allowance' not in value
    assert [m['allowance_units'] for m in value['monthly']] == [160, 160]
    render_task_burndown(value)
    assert [step[1] for step in plotted['steps']] == [[40, 39, 39], [40, 40]]


def test_new_owners_require_terms_and_new_projects_require_contract_review(contract):
    env = {'STUDIO_REPORTS_REQUIRE_CONTRACT_TERMS': 'true',
           'STUDIO_REPORTS_CONTRACT_EXEMPT_CLIENT_IDS_JSON': '["UMARK"]'}
    validate_contract_policy(contract.clients, env)
    copied = deepcopy(contract.clients)
    copied['UNEW'] = {'name': 'New owner', 'project_ids': ['new-project']}
    with pytest.raises(TimesheetError, match='contract_terms_required'):
        validate_contract_policy(copied, env)
    contract.clients['UOTHER']['project_ids'].append(P1)
    with pytest.raises(TimesheetError, match='invalid_contract_terms'):
        validate_contract_policy(contract.clients, env)


@pytest.mark.parametrize('change', [
    {'commencement_date': '2026-02-30'}, {'commencement_date': '2026-10-16T00:00:00Z'},
    {'renewal': '30_days'}, {'included_hours': 'NaN'}, {'included_hours': -1},
    {'rollover_percent': 101}, {'rollover_percent': True}, {'rollover_cycles': 2},
    {'rollover_first': False}, {'rollover_cap_hours': -1}, {'project_ids': [P1]},
    {'unreviewed_extra': 1}, {'source': {}}, {'source': {**terms()['source'], 'sha256': 'invalid'}},
    {'source': {**terms()['source'], 'signed_at': '2027-01-01T00:00:00Z'}},
])
def test_invalid_or_unreviewed_terms_fail_closed(contract, change):
    contract.clients['UOTHER']['contract_terms'].update(change)
    with pytest.raises(TimesheetError, match='invalid_contract_terms'):
        validate_contract_policy(contract.clients, {})


def test_exemption_cannot_accidentally_enable_contracts_for_mark(contract):
    contract.clients['UMARK'].update(hours_source='linear', contract_terms=terms(project=P1))
    contract.clients['UMARK']['contract_terms']['project_ids'] = contract.clients['UMARK']['project_ids']
    with pytest.raises(TimesheetError, match='contract_terms_on_exempt_client'):
        validate_contract_policy(contract.clients, {'STUDIO_REPORTS_CONTRACT_EXEMPT_CLIENT_IDS_JSON': '["UMARK"]'})


def test_broken_prior_ownership_evidence_blocks_report_instead_of_inventing_credit(contract):
    bad = work()
    bad['error'] = 'missing_history'
    with pytest.raises(TimesheetError, match='source_evidence_incomplete'):
        report(contract, [bad], month='2026-11')


def test_request_cannot_override_private_contract(contract):
    value = contract.service.report_for_request(request(month='current',
        contract_terms=terms(commencement_date='2026-11-01', included_hours=1000)))
    cycle, = value['contract_allowance']['cycles']
    assert cycle['included_hours'] == '40' and cycle['start'].startswith('2026-10-16')
