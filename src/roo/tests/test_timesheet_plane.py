"""Plane CE 1.4.0-shaped HTTP responses; no Plane or Slack credentials required."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import Mock
from uuid import UUID

import httpx
import pytest

from roo.timesheet_plane import PlaneAPI, PlaneOptions
from roo.timesheet_worker import TimesheetService
from roo.timesheets import TimesheetError, build_report, empty_ledger, summary, timestamp
from roo.tests.test_timesheets import config, api_for, item, ticket, USER, OTHER, PROJECT, END


def uid(n):
    return str(UUID(int=n + 100))


WORKSPACE, PLANE_PROJECT, ALICE, BOB, DONE, TODO, SIZE = map(uid, range(1, 8))


def raw(number=1, size='Small (S)', **changes):
    return {'id': uid(20 + number), 'workspace': WORKSPACE, 'project': PLANE_PROJECT,
            'name': f'Plane task {number}', 'sequence_id': number, 'created_at': '2026-08-29T00:00:00Z',
            'updated_at': '2026-09-12T01:00:00Z', 'completed_at': '2026-09-10T01:00:00Z',
            'state': DONE, 'assignees': [ALICE], 'labels': [{'id': SIZE, 'name': size}],
            'estimate_point': uid(99), 'archived_at': None, 'deleted_at': None, 'is_draft': False,
            'external_id': None, 'external_source': None, **changes}


def activity(number, field, when, **changes):
    return {'id': uid(100 + number), 'issue': uid(21), 'project': PLANE_PROJECT,
            'created_at': '2026-09-12T02:00:00Z', 'epoch': int(timestamp(when).timestamp()),
            'field': field, 'old_identifier': None, 'new_identifier': None,
            'old_value': '', 'new_value': '', **changes}


def page(rows, cursor=None):
    return {'results': rows, 'next_page_results': cursor is not None, 'next_cursor': cursor,
            'count': len(rows)}


def setup(config, items=None, histories=None, *, imported=None, override=None):
    options = PlaneOptions('https://plane.example', 'mlai', WORKSPACE, 'secret-plane-key',
                           {PLANE_PROJECT: PROJECT}, {ALICE: USER, BOB: OTHER}, imported or {})
    config = replace(config, source='plane', projects={PROJECT: 'Studio project'})
    items = items if items is not None else [raw()]
    histories = histories or {}
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'plane.example'
        assert request.headers['x-api-key'] == options.key
        assert 'authorization' not in request.headers
        if override:
            response = override(request)
            if response is not None:
                return response
        path = request.url.path
        root = '/api/v1/workspaces/mlai/projects/'
        if path == root:
            return httpx.Response(200, json=page([{'id': PLANE_PROJECT, 'workspace': WORKSPACE, 'identifier': 'STU'}]))
        if path.endswith('/states/'):
            return httpx.Response(200, json=page([{'id': DONE, 'project': PLANE_PROJECT, 'group': 'completed'},
                                                 {'id': TODO, 'project': PLANE_PROJECT, 'group': 'unstarted'}]))
        if path.endswith('/work-items/'):
            return httpx.Response(200, json=page(items))
        if path.endswith('/activities/'):
            key = path.split('/')[-3]
            return httpx.Response(200, json=page(histories.get(key, [])))
        key = path.split('/')[-2]
        return httpx.Response(200, json=next(deepcopy(i) for i in items if i['id'] == key))
    slack = Mock()
    slack.slack.return_value = {'team_id': config.team}
    api = PlaneAPI(httpx.Client(transport=httpx.MockTransport(handler)), options, slack)
    return config, api, requests, slack


@pytest.mark.parametrize('label,hours', [('Extra Small (XS)', .25), ('Small (S)', 1),
                                      ('Medium (M)', 2), ('Large (L)', 3), ('Extra Large (XL)', 5)])
def test_real_plane_adapter_same_size_rubric_without_counting_estimate_point(config, label, hours):
    cfg, api, requests, slack = setup(config, [raw(size=label)])
    report = TimesheetService(cfg, api).report('2026-09-11', END, preview=True)
    assert report['rows'][0]['units'] / 4 == hours
    assert report['rows'][0]['source'] == 'plane'
    assert report['rows'][0]['url'].startswith('https://plane.example/mlai/projects/')
    assert not report['complete'] and not report['coverage_verified']
    assert 'Do not use for payment' in summary(report)
    assert not (cfg.directory / 'ledger.json').exists()
    slack.post_message.assert_not_called()


def test_history_restores_size_and_assignee_at_cutoff_and_uses_action_epoch(config):
    history = [activity(1, 'labels', '2026-09-12T01:00:00Z', old_identifier=uid(88), new_identifier=SIZE,
                        old_value='Medium (M)', new_value='Extra Large (XL)'),
               activity(2, 'assignees', '2026-09-12T01:00:00Z', old_identifier=ALICE),
               activity(3, 'assignees', '2026-09-12T01:00:00Z', new_identifier=BOB),
               activity(4, 'state', '2026-09-10T01:00:00Z', old_identifier=TODO, new_identifier=DONE)]
    cfg, api, _, _ = setup(config, [raw(size='Extra Large (XL)', assignees=[BOB])], {uid(21): history})
    report = TimesheetService(cfg, api).report('2026-09-11', END, preview=True)
    row = report['rows'][0]
    assert (row['builder_id'], row['units']) == (USER, 8)
    assert row['completed_at'] == '2026-09-10T01:00:00+00:00'


@pytest.mark.parametrize('assignees', [[], [ALICE, BOB]])
def test_zero_or_multiple_assignees_are_exceptions(config, assignees):
    cfg, api, _, _ = setup(config, [raw(assignees=assignees)])
    report = TimesheetService(cfg, api).report('latest', END, preview=True)
    assert report['rows'] == []
    assert report['exceptions'][0]['reason'] == 'exactly_one_assignee_required'


def test_missing_labels_do_not_turn_estimate_definition_uuid_into_hours(config):
    cfg, api, _, _ = setup(config, [raw(labels=[])])
    report = TimesheetService(cfg, api).report('latest', END, preview=True)
    assert not report['rows']
    assert report['exceptions'][0]['reason'] == 'exactly_one_effort_label_required'


def test_activities_pagination_and_deferred_absence(config):
    history = activity(1, 'state', '2026-09-10T01:00:00Z', old_identifier=TODO, new_identifier=DONE)
    def override(request):
        if request.url.path.endswith('/activities/'):
            return httpx.Response(200, json=page([history], '100:1:0') if 'cursor' not in request.url.params else page([]))
    cfg, api, requests, _ = setup(config, override=override)
    api.verify(cfg)
    dataset = api.collect(cfg, END, {'missing-ticket': 'earlier_error'})
    assert len([r for r in requests if r.url.path.endswith('/activities/')]) == 2
    assert dataset[-1]['error'] == 'plane_deferred_issue_not_visible'


@pytest.mark.parametrize('body', [{'results': []}, {'results': [], 'next_page_results': True, 'next_cursor': ''}, []])
def test_truncated_provider_response_cannot_create_zero_report(config, body):
    cfg, api, _, _ = setup(config, override=lambda request: httpx.Response(200, json=body))
    with pytest.raises(TimesheetError):
        TimesheetService(cfg, api).report('latest', END, preview=True)
    assert not (cfg.directory / 'ledger.json').exists()


def test_redirect_cannot_exfiltrate_plane_key(config):
    cfg, api, requests, _ = setup(config, override=lambda request: httpx.Response(302, headers={'Location': 'https://attacker.invalid/'}))
    with pytest.raises(TimesheetError, match='redirect_rejected'):
        api.verify(cfg)
    assert len(requests) == 1


def test_imported_ticket_uses_original_ledger_identity_and_is_not_counted_again(config):
    linear = TimesheetService(config, api_for([item(ticket(id=uid(500)))]))
    linear.report('latest', END)
    cfg, api, _, _ = setup(config, [raw(external_source='linear', external_id=uid(500),
                                      completed_at='2026-09-24T00:00:00Z')], imported={uid(21): uid(500)})
    report = TimesheetService(cfg, api).report('2026-09-25', END + timedelta(days=14), preview=True)
    assert report['rows'] == []
    assert linear.ledger.load()['allocated'] == {uid(500): '2026-09-11'}
    assert '2026-09-25' not in linear.ledger.load()['periods']


def test_unmapped_import_is_flagged_instead_of_treated_as_new_work(config):
    cfg, api, _, _ = setup(config, [raw(external_source='linear', external_id=uid(500))])
    report = TimesheetService(cfg, api).report('latest', END, preview=True)
    assert not report['rows']
    assert report['exceptions'][0]['reason'] == 'plane_import_identity_mapping_required'


def test_plane_source_cannot_finalize_or_deliver_until_coverage_is_verified(config):
    cfg, api, requests, _ = setup(config)
    with pytest.raises(TimesheetError, match='plane_preview_only'):
        TimesheetService(cfg, api).report('latest', END)
    assert not requests
    with pytest.raises(TimesheetError, match='plane_preview_only'):
        TimesheetService(config, api_for([])).deliver({'source': 'plane'}, 'USAM', 'preview', END)


def test_late_plane_completion_is_in_next_round(config):
    late = '2026-09-11T02:01:00Z'
    hist = activity(1, 'state', late, old_identifier=TODO, new_identifier=DONE)
    cfg, api, _, _ = setup(config, [raw(size='Large (L)', completed_at=late)], {uid(21): [hist]})
    service = TimesheetService(cfg, api)
    assert service.report('2026-09-11', END, preview=True)['rows'] == []
    report = service.report('2026-09-25', END + timedelta(days=14), preview=True)
    assert sum(r['units'] for r in report['rows']) == 12


def test_conflicting_import_metadata_never_hides_another_linear_ticket(config):
    TimesheetService(config, api_for([item(ticket(id=uid(500)))])).report('latest', END)
    cfg, api, _, _ = setup(config, [raw(external_source='linear', external_id=uid(501))],
                           imported={uid(21): uid(500)})
    report = TimesheetService(cfg, api).report('2026-09-25', END + timedelta(days=14), preview=True)
    assert report['exceptions'][0]['reason'] == 'plane_import_identity_mapping_mismatch'


def test_second_precision_change_at_exact_cutoff_is_not_guessed(config):
    history = activity(1, 'labels', END.isoformat(), old_identifier=uid(88), new_identifier=SIZE,
                       old_value='Small (S)', new_value='Medium (M)')
    cfg, api, _, _ = setup(config, [raw(size='Medium (M)')], {uid(21): [history]})
    report = TimesheetService(cfg, api).report('latest', END, preview=True)
    assert report['exceptions'][0]['reason'] == 'activity_cutoff_time_ambiguous'


@pytest.mark.parametrize('change', [{'TIMESHEET_PLANE_BASE_URL': 'http://plane.example'},
    {'TIMESHEET_PLANE_BASE_URL': 'https://key@plane.example'}, {'TIMESHEET_PLANE_BASE_URL': 'https://plane.example/mlai'},
    {'TIMESHEET_PLANE_WORKSPACE': '../other'}, {'TIMESHEET_PLANE_API_KEY': ''}])
def test_plane_configuration_rejects_unsafe_origins_and_missing_key(change):
    env = {'TIMESHEET_PLANE_WORKSPACE_ID': WORKSPACE, 'TIMESHEET_PLANE_API_KEY': 'synthetic',
           'TIMESHEET_PLANE_PROJECT_MAP_JSON': json.dumps({PLANE_PROJECT: PROJECT}), **change}
    with pytest.raises(TimesheetError):
        PlaneOptions.from_env(env)
