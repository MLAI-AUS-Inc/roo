"""Client selection cannot enlarge project access or redirect private delivery."""
from datetime import timedelta
import json

import pytest

from roo.studio_report_clients import select_client, select_project
from roo.studio_report_worker import StudioReportService, configuration
from roo.studio_reports import render_chart, summary
from roo.timesheets import TimesheetError
from tests.roo.test_studio_reports import setup, ticket, queue, NOW, P1, P2, P3


def staff(setup):
    setup.clients['UMARK'].update(name='Mark Ghiasy', aliases=['Mark Ghaisy', 'mark@example.com'])
    setup.clients['USTAFF'] = {'name': 'Staff', 'project_ids': [P1, P2, P3],
                               'monthly_hours': None, 'report_client_ids': ['UMARK']}
    setup.env['STUDIO_REPORTS_CLIENTS_JSON'] = json.dumps(setup.clients)
    config, clients = configuration(setup.env)
    return StudioReportService(config, setup.api, clients)


def request(actor='USTAFF', client='Mark Ghiasy', **selector):
    return {'team': 'T123', 'actor': actor, 'requested_at': NOW.isoformat(),
            'selector': {'month': 'recent', 'months': 3, 'client': client, **selector}}


@pytest.mark.parametrize('name', ['Mark Ghiasy', 'mark ghaisy', ' MARK   GHIASY ', 'mark@example.com', '<@UMARK>'])
def test_staff_named_report_uses_owner_budget_projects_and_requester_dm(setup, name):
    service = staff(setup)
    setup.api.collect.return_value = [ticket(), ticket(2, project=P3, title='Outside selected client')]
    req = request(client=name)
    result = service.report_for_request(req)
    assert result['client'] == 'Mark Ghiasy'
    assert set(result['projects']) == {P1, P2}
    assert result['monthly'][-1]['units'] == 4
    assert [m['allowance_units'] for m in result['monthly']] == [160] * 3
    assert set(setup.api.collect.call_args.args[0].projects) == {P1, P2}
    assert 'Outside selected client' not in json.dumps(result)
    assert 'studio-hours-projects.png' in [p.get('filename') for p in result['parts']]
    service.deliver_request(result, req, 'named-report', NOW)
    assert setup.api.open_dm.call_args.args == ('USTAFF',)
    assert all(call.args[0] == 'DUSTAFF' for call in setup.api.post_message.call_args_list)
    assert all(call.args[0] == 'DUSTAFF' for call in setup.api.upload_csv.call_args_list)


@pytest.mark.parametrize('actor,client', [('UMARK', 'Other'), ('UMARK', 'Staff'), ('USTAFF', 'Other'),
                                        ('USTAFF', 'Missing'), ('UUNKNOWN', 'Mark Ghiasy')])
def test_ungranted_and_unknown_client_never_read_source(setup, actor, client):
    service = staff(setup)
    with pytest.raises(TimesheetError):
        service.report_for_request(request(actor, client))
    setup.api.collect.assert_not_called()
    setup.api.verify.assert_not_called()
    setup.api.post_message.assert_not_called()


def test_same_name_collision_is_rejected_without_falling_back_to_own_scope(setup):
    service = staff(setup)
    service.clients['USTAFF']['aliases'] = ['Mark Ghiasy']
    with pytest.raises(TimesheetError, match='report_client_unavailable'):
        service.report_for_request(request())
    setup.api.collect.assert_not_called()


def test_client_can_select_self_by_name_and_all_stays_within_own_projects(setup):
    service = staff(setup)
    setup.api.collect.return_value = [ticket(), ticket(2, project=P3)]
    own = service.report_for_request(request('UMARK'))
    assert set(own['projects']) == {P1, P2}
    all_clients = service.report_for_request(request('UMARK', 'all'))
    assert all_clients['client_groups'] == [{'name': 'Mark Ghiasy', 'project_ids': [P1, P2]}]
    assert sum(m['units'] for m in all_clients['monthly']) == 4


def test_all_clients_reconcile_once_and_show_unmapped_projects(setup, monkeypatch):
    from matplotlib.axes import Axes

    service = staff(setup)
    setup.api.collect.return_value = [ticket(1, size='Large (L)', completed='2026-07-10T00:00:00Z'),
                                     ticket(2, project=P2), ticket(3, project=P3, size='Medium (M)')]
    result = service.report_for_request(request(client='all'))
    text = summary(result)
    assert '6 hours used across 3 months' in text
    assert 'Mark Ghiasy · 4h' in text
    assert 'Projects without a client mapping · 2h' in text
    assert text.count('Master App:') == text.count('Cybertest:') == 1
    assert all(m['allowance_units'] is None for m in result['monthly'])
    assert 'Other ·' not in text  # Not a configured reporting grant.
    values = []
    original = Axes.barh
    def capture(axes, y, width, *args, **kwargs):
        values.extend(width)
        return original(axes, y, width, *args, **kwargs)
    monkeypatch.setattr(Axes, 'barh', capture)
    assert render_chart(result, breakdown='clients').startswith(b'\x89PNG')
    assert values == [4, 2]
    values.clear()
    assert render_chart(result, breakdown='projects').startswith(b'\x89PNG')
    assert values == [3, 2, 1]


@pytest.mark.parametrize('change', ['grant', 'owner_projects', 'requester_projects', 'budget', 'alias'])
def test_saved_client_report_revalidates_both_grants_and_owner_configuration(setup, change):
    service = staff(setup)
    req = request()
    result = service.report_for_request(req)
    if change == 'grant':
        service.clients['USTAFF']['report_client_ids'] = []
    elif change == 'owner_projects':
        service.clients['UMARK']['project_ids'] = [P2]
    elif change == 'requester_projects':
        service.clients['USTAFF']['project_ids'] = [P1, P3]
    elif change == 'budget':
        service.clients['UMARK']['monthly_hours'] = 80
    else:
        service.clients['UMARK']['aliases'].append('Another alias')
    with pytest.raises(TimesheetError):
        service.deliver_request(result, req, 'stale', NOW)
    setup.api.open_dm.assert_not_called()


@pytest.mark.parametrize('change', ['unknown', 'self_grant', 'outside_scope', 'overlap', 'bad_alias', 'reserved', 'bad_grants'])
def test_bad_reporting_configuration_fails_closed(setup, change):
    service = staff(setup)
    clients = service.clients
    if change == 'unknown':
        clients['USTAFF']['report_client_ids'] = ['UUNKNOWN']
    elif change == 'self_grant':
        clients['USTAFF']['report_client_ids'] = ['USTAFF']
    elif change == 'outside_scope':
        clients['USTAFF']['project_ids'] = [P1]
    elif change == 'overlap':
        clients['UOTHER']['project_ids'] = [P1, P3]
        clients['USTAFF']['report_client_ids'].append('UOTHER')
    elif change == 'bad_alias':
        clients['UMARK']['aliases'] = [None]
    elif change == 'reserved':
        clients['UMARK']['aliases'] = ['all']
    else:
        clients['USTAFF']['report_client_ids'] = 'UMARK'
    with pytest.raises(TimesheetError):
        configuration({**setup.env, 'STUDIO_REPORTS_CLIENTS_JSON': json.dumps(clients)})


def test_followups_retain_client_across_dm_and_explicit_month_but_can_reset(setup):
    queue(setup, params={'client': 'Mark Ghiasy', 'month': 'recent', 'months': 3})
    queue(setup, actor='UOTHER', event='other', params={'client': 'Other'}, now=NOW + timedelta(seconds=1))
    queue(setup, event='detail', params={'action': 'detailed', 'month': '2026-08'}, now=NOW + timedelta(seconds=2))
    queue(setup, event='self', params={'action': 'detailed', 'client': 'self'}, now=NOW + timedelta(seconds=3))
    requests = [setup.service.queue.read(p.stem) for p in setup.config.queue.glob('*.json')]
    detailed = [r['selector'] for r in requests if r['selector']['action'] == 'detailed']
    assert {'action': 'detailed', 'month': '2026-08', 'months': 1, 'client': 'Mark Ghiasy'} in detailed
    assert {'action': 'detailed', 'month': '2026-08', 'months': 1, 'client': 'self'} in detailed


def test_unavailable_selection_gets_terminal_private_notice_without_zero_report(setup):
    service = staff(setup)
    queue(setup, actor='USTAFF', params={'client': 'Unknown'})
    result = service.commands(NOW)
    assert result[0]['reason'] == 'report_client_unavailable'
    saved = service.queue.read(next(setup.config.queue.glob('*.json')).stem)
    assert saved['status'] == 'rejected'
    assert service.commands(NOW + timedelta(minutes=2)) == []
    setup.api.collect.assert_not_called()
    setup.api.upload_csv.assert_not_called()
    assert 'couldn’t match that client' in setup.api.post_message.call_args.args[1]
    assert setup.api.open_dm.call_args.args == ('USTAFF',)


@pytest.mark.parametrize('selector', ['[Studio] Master App', 'Master App', 'MASTERAPP', P1])
def test_project_within_mark_grant_omits_shared_allowance_and_other_groups(setup, selector):
    service = staff(setup)
    projects = {**service.config.projects, P1: '[Studio] Master App'}
    owner, _, _ = select_client(service.clients, 'USTAFF', {'client': 'Mark Ghiasy'})
    owner = {**owner, 'monthly_allowances': {'2026-09': 55}, 'contract_terms': {'project_ids': [P1, P2]}}
    selected, groups = select_project(owner, [], {'project': selector}, projects)
    assert selected['project_ids'] == [P1] and groups == []
    assert selected['monthly_hours'] is None and selected['monthly_allowances'] == {}
    assert 'contract_terms' not in selected
    assert owner['project_ids'] == [P1, P2] and 'contract_terms' in owner

    aggregate, groups, _ = select_client(service.clients, 'USTAFF', {'client': 'all'})
    _, scoped_groups = select_project(aggregate, groups, {'project': selector}, projects)
    assert scoped_groups == [{'name': 'Mark Ghiasy', 'project_ids': [P1]}]


@pytest.mark.parametrize('query', ['Study Nash', 'Project Study Nash'])
def test_study_nash_retains_single_project_terms_with_ordinary_spacing(query):
    terms = {'project_ids': [P3]}
    lead = {'name': 'Devarshi Desai', 'project_ids': [P3], 'contract_terms': terms}
    selected, _ = select_project(lead, [], {'project': query}, {P3: '[Studio] Studynash'})
    assert selected['contract_terms'] == terms


@pytest.mark.parametrize('query', ['Master App', 'Project Master App', 'Unknown', 'Project Unknown',
                                   'Mas', P3, None, [], '', '[Studio]', 'a\nb', 'x' * 201])
def test_project_never_matches_ungranted_ids_names_or_ambiguous_labels(setup, query):
    service = staff(setup)
    owner, groups, _ = select_client(service.clients, 'USTAFF', {'client': 'Mark Ghiasy'})
    projects = {P1: '[Studio] Master App', P2: 'Masterapp', P3: 'Unknown'}
    with pytest.raises(TimesheetError, match='^report_project_unavailable$'):
        select_project(owner, groups, {'project': query}, projects)


def test_duplicate_label_outside_grant_does_not_invalidate_allowed_project(setup):
    owner = setup.clients['UOTHER']
    projects = {P1: 'Build', P2: 'Other', P3: 'Build'}
    selected, _ = select_project(owner, [], {'project': 'Build'}, projects)
    assert selected['project_ids'] == [P3]


def test_literal_project_prefix_wins_over_generic_prefix(setup):
    owner = setup.clients['UMARK']
    projects = {P1: '[Studio] Project Acquire', P2: '[Studio] Acquire'}
    selected, _ = select_project(owner, [], {'project': 'Project Acquire'}, projects)
    assert selected['project_ids'] == [P1]
