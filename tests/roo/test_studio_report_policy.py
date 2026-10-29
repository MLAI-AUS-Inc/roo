"""Synthetic source policy, existing grants and non-mutating backfill partitions."""
from copy import deepcopy

import pytest

from roo.studio_report_policy import filter_backfill, linear_projects, validate_hours_policy
from roo.timesheets import TimesheetError


@pytest.fixture
def clients():
    return {
        'UMARK': {'name': 'Mark', 'project_ids': ['mark-project']},
        'UDEV': {'name': 'Devarshi', 'aliases': ['Devashi'],
                 'project_ids': ['study-nash'], 'hours_source': 'linear',
                 'history_start_month': '2026-05'},
        'UOTHER': {'name': 'Other', 'project_ids': ['other-project'], 'hours_source': 'linear'},
        'USTAFF': {'name': 'Staff', 'project_ids': ['mark-project', 'study-nash', 'other-project', 'unmapped'],
                   'monthly_hours': None, 'report_client_ids': ['UMARK', 'UDEV']},
    }


def test_auto_remains_default_and_staff_project_access_is_not_ownership(clients):
    before = deepcopy(clients)
    assert validate_hours_policy(clients) is None
    assert clients == before
    assert 'hours_source' not in clients['UMARK']


@pytest.mark.parametrize('value', [None, True, 1, [], {}, '', 'invoice', 'LINEAR'])
def test_invalid_source_fails_closed(clients, value):
    clients['UDEV']['hours_source'] = value
    with pytest.raises(TimesheetError, match='invalid_client_hours_policy'):
        validate_hours_policy(clients)


@pytest.mark.parametrize('value', [None, True, 202605, [], 'current', 'all', '2026-5',
                                  '2026-05-01', '2026-00', '2026-13', '0000-01', ' 2026-05'])
def test_history_requires_an_explicit_calendar_month(clients, value):
    clients['UDEV']['history_start_month'] = value
    with pytest.raises(TimesheetError, match='invalid_client_hours_policy'):
        validate_hours_policy(clients)


@pytest.mark.parametrize('source', ['auto', 'linear'])
def test_conflicting_owner_sources_are_rejected_in_either_order(clients, source):
    clients['UMARK']['hours_source'] = source
    clients['UMARK']['project_ids'] = ['study-nash']
    clients['UDEV']['hours_source'] = 'linear' if source == 'auto' else 'auto'
    with pytest.raises(TimesheetError, match='invalid_client_hours_policy'):
        validate_hours_policy(clients)


@pytest.mark.parametrize('actor,selector,expected', [
    ('UDEV', {}, {'study-nash'}),
    ('UDEV', {'client': 'self'}, {'study-nash'}),
    ('UDEV', {'client': 'all'}, {'study-nash'}),
    ('UDEV', {'client': 'Devashi'}, {'study-nash'}),
    ('UMARK', {}, set()),
    ('USTAFF', {}, {'study-nash'}),
    ('USTAFF', {'client': 'self'}, {'study-nash'}),
    ('USTAFF', {'client': 'all'}, {'study-nash'}),
    ('USTAFF', {'client': 'Devarshi'}, {'study-nash'}),
    ('USTAFF', {'client': 'Mark'}, set()),
])
def test_linear_projects_follow_selected_grants_not_overlapping_staff_access(clients, actor, selector, expected):
    assert linear_projects(clients, actor, selector) == expected


@pytest.mark.parametrize('actor,selector', [
    ('UMARK', {'client': 'Devarshi'}),
    ('UDEV', {'client': 'Mark'}),
    ('USTAFF', {'client': 'Other'}),
    ('UUNKNOWN', {'client': 'Devarshi'}),
])
def test_source_selection_never_creates_a_grant(clients, actor, selector):
    with pytest.raises(TimesheetError):
        linear_projects(clients, actor, selector)


def test_staff_self_does_not_use_an_owner_whose_full_scope_is_unavailable(clients):
    clients['UDEV']['project_ids'].append('not-in-staff-scope')
    assert linear_projects(clients, 'USTAFF', {}) == set()
    with pytest.raises(TimesheetError):
        linear_projects(clients, 'USTAFF', {'client': 'Devarshi'})


def test_backfill_partition_removes_only_excluded_project_records_and_policy():
    manifest = {'version': 2, 'coverage': {'start': '2026-05-01', 'through': '2026-09-30'},
                'sources': {'shared-invoice': {'hours': '10'}, 'dev-only-invoice': {'hours': '5'}},
                'invoice_first_projects': ['mark-project', 'study-nash'],
                'entries': [{'project_id': 'mark-project', 'id': 'mark', 'source_id': 'shared-invoice'},
                            {'project_id': 'study-nash', 'id': 'dev', 'source_id': 'shared-invoice'},
                            {'project_id': 'study-nash', 'id': 'dev-only', 'source_id': 'dev-only-invoice'}],
                'pending': [{'project_id': 'study-nash'}, {'project_id': 'mark-project'}],
                'qualifications': [{'project_id': 'mark-project'}, {'project_id': 'study-nash'}]}
    before = deepcopy(manifest)
    filtered = filter_backfill(manifest, {'study-nash'})
    assert filtered['entries'] == [{'project_id': 'mark-project', 'id': 'mark', 'source_id': 'shared-invoice'}]
    assert filtered['pending'] == filtered['qualifications'] == [{'project_id': 'mark-project'}]
    assert filtered['invoice_first_projects'] == ['mark-project']
    assert filtered['sources'] == {'shared-invoice': {'hours': '10'}}
    assert filtered['coverage'] == manifest['coverage']
    filtered['sources']['shared-invoice']['hours'] = '99'
    filtered['coverage']['through'] = '2099-01-01'
    assert manifest == before


def test_excluded_source_changes_do_not_change_the_filtered_manifest():
    manifest = {'entries': [{'project_id': 'mark-project', 'source_id': 'mark'},
                            {'project_id': 'study-nash', 'source_id': 'dev'}],
                'sources': {'mark': {'hours': '10'}, 'dev': {'hours': '5'}}, 'pending': []}
    first = filter_backfill(manifest, {'study-nash'})
    manifest['sources']['dev']['hours'] = '99'
    assert filter_backfill(manifest, {'study-nash'}) == first


def test_other_retained_references_keep_their_source():
    manifest = {'entries': [], 'pending': [{'project_id': 'mark-project', 'source_id': 'review'}],
                'sources': {'review': {'hours': '5'}, 'orphan': {'hours': '10'}}}
    assert filter_backfill(manifest, {'study-nash'})['sources'] == {'review': {'hours': '5'}}


def test_no_exclusions_preserve_source_configuration_unchanged():
    manifest = {'entries': [], 'pending': [], 'sources': {'unallocated': {'hours': '5'}}}
    result = filter_backfill(manifest, set())
    assert result == manifest
    assert result is not manifest


def test_backfill_none_and_v1_keep_their_existing_shape():
    assert filter_backfill(None, {'study-nash'}) is None
    manifest = {'version': 1, 'entries': [{'project_id': 'study-nash'}], 'pending': [], 'sources': {}}
    filtered = filter_backfill(manifest, {'study-nash'})
    assert filtered == {'version': 1, 'entries': [], 'pending': [], 'sources': {}}
    assert len(manifest['entries']) == 1
