"""Read-only Plane CE 1.4.0 adapter for timesheet compatibility previews.

The v1 work-item manager excludes archived issues and archived projects. Until
full archive and migration coverage is verified, the worker refuses finalization
or delivery from this source. No session-cookie or private/admin API fallback.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
from urllib.parse import urlsplit
from uuid import UUID

from .timesheets import TimesheetError, timestamp

ITEM_FIELDS = 'id,workspace,project,name,sequence_id,created_at,updated_at,completed_at,state,labels,assignees,estimate_point,archived_at,is_draft,deleted_at,external_source,external_id'


def uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TimesheetError('invalid_plane_id') from exc


def mapping(value):
    data = json.loads(value)
    if not isinstance(data, dict):
        raise TimesheetError('invalid_plane_mapping')
    result = {uuid(k): uuid(v) for k, v in data.items()}
    if len(set(result.values())) != len(result):
        raise TimesheetError('ambiguous_plane_mapping')
    return result


@dataclass(frozen=True)
class PlaneOptions:
    base: str
    workspace: str
    workspace_id: str
    key: str
    projects: dict
    users: dict
    imported_issues: dict

    @classmethod
    def from_env(cls, env):
        base = env.get('TIMESHEET_PLANE_BASE_URL', 'https://plane.mlai.au').rstrip('/')
        parsed = urlsplit(base)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path):
            raise TimesheetError('plane_https_origin_required')
        workspace = env.get('TIMESHEET_PLANE_WORKSPACE', 'mlai')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', workspace):
            raise TimesheetError('invalid_plane_workspace')
        key = env.get('TIMESHEET_PLANE_API_KEY')
        if not key:
            raise TimesheetError('plane_api_key_required')
        projects = mapping(env.get('TIMESHEET_PLANE_PROJECT_MAP_JSON', '{}'))
        if not projects:
            raise TimesheetError('plane_project_mapping_required')
        return cls(base, workspace, uuid(env.get('TIMESHEET_PLANE_WORKSPACE_ID')), key, projects,
                   mapping(env.get('TIMESHEET_PLANE_USER_MAP_JSON', '{}')),
                   mapping(env.get('TIMESHEET_PLANE_IMPORTED_ISSUE_MAP_JSON', '{}')))


def reference(value):
    return uuid(value['id'] if isinstance(value, dict) else value)


def activity_time(activity):
    # Plane writes activity asynchronously. epoch records the originating action
    # time; created_at can be later and must not move work across the cutoff.
    value = activity.get('epoch')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TimesheetError('plane_activity_time_missing')
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, ValueError, OSError) as exc:
        raise TimesheetError('plane_activity_time_invalid') from exc


class PlaneAPI:
    def __init__(self, client, options, slack_api):
        self.client, self.options, self.slack_api = client, options, slack_api
        self.project_data = {}
        self.states = {}

    def __getattr__(self, name):
        if name in {'verify_recipient', 'open_dm', 'post_message', 'upload_csv'}:
            return getattr(self.slack_api, name)
        raise AttributeError(name)

    def get(self, relative, params=None):
        # Construct every URL locally; never follow a provider pagination URL.
        if not re.fullmatch(r'[A-Za-z0-9_/-]+', relative) or '..' in relative:
            raise TimesheetError('invalid_plane_path')
        url = self.options.base + '/api/v1/workspaces/' + self.options.workspace + '/' + relative
        response = self.client.get(url, headers={'X-API-Key': self.options.key}, params=params,
                                   follow_redirects=False)
        if 300 <= response.status_code < 400:
            raise TimesheetError('plane_redirect_rejected')
        if response.status_code == 429:
            raise TimesheetError('plane_rate_limited')
        if response.status_code in {401, 403}:
            raise TimesheetError('plane_read_access_required')
        response.raise_for_status()
        return response.json()

    def pages(self, relative, params=None):
        cursor, seen, rows = None, set(), {}
        while True:
            data = self.get(relative, {**(params or {}), 'per_page': 100, 'order_by': 'created_at',
                                       **({'cursor': cursor} if cursor else {})})
            if not isinstance(data, dict) or not isinstance(data.get('results'), list):
                raise TimesheetError('incomplete_plane_page')
            for item in data['results']:
                item_id = uuid(item.get('id'))
                if item_id in rows:
                    raise TimesheetError('unstable_plane_pagination')
                rows[item_id] = item
            more = data.get('next_page_results')
            if more is False:
                return list(rows.values())
            cursor = data.get('next_cursor')
            if more is not True or not isinstance(cursor, str) or not cursor or cursor in seen:
                raise TimesheetError('invalid_plane_cursor')
            seen.add(cursor)

    def verify(self, config):
        if config.source != 'plane':
            raise TimesheetError('plane_source_configuration_required')
        if self.slack_api.slack('auth.test').get('team_id') != config.team:
            raise TimesheetError('workspace_mismatch')
        if set(self.options.projects.values()) != set(config.projects):
            raise TimesheetError('plane_selected_projects_mismatch')
        projects = self.pages('projects/')
        self.project_data = {uuid(p['id']): p for p in projects}
        for key in self.options.projects:
            project = self.project_data.get(key)
            if project is None or reference(project['workspace']) != self.options.workspace_id:
                raise TimesheetError('plane_project_access_unavailable')
            if project.get('archived_at') or project.get('deleted_at'):
                raise TimesheetError('plane_archived_project_not_readable')
        self.states = {}
        for key in self.options.projects:
            for state in self.pages(f'projects/{key}/states/'):
                if reference(state['project']) != key:
                    raise TimesheetError('plane_state_project_mismatch')
                self.states[uuid(state['id'])] = state

    def state(self, value):
        key = reference(value)
        state = self.states.get(key)
        if not state or state.get('group') not in {'backlog', 'unstarted', 'started', 'completed', 'cancelled', 'canceled', 'triage'}:
            raise TimesheetError('plane_state_history_missing')
        return {'id': key, 'type': state['group']}

    def user(self, value):
        key = reference(value)
        return self.options.users.get(key, key)

    def issue(self, project_id, issue_id):
        data = self.get(f'projects/{project_id}/work-items/{issue_id}/', {'expand': 'labels', 'fields': ITEM_FIELDS})
        if (uuid(data['id']) != issue_id or reference(data['project']) != project_id
                or reference(data['workspace']) != self.options.workspace_id):
            raise TimesheetError('plane_issue_identity_mismatch')
        return data

    def normalize(self, raw, activities):
        project_id, item_id = reference(raw['project']), uuid(raw['id'])
        canonical_id = self.options.imported_issues.get(item_id)
        if (raw.get('external_source') or raw.get('external_id')) and not canonical_id:
            raise TimesheetError('plane_import_identity_mapping_required')
        if canonical_id and str(raw.get('external_source', '')).lower() == 'linear':
            try:
                external_id = str(UUID(str(raw.get('external_id'))))
            except ValueError:
                external_id = None
            if external_id is not None and external_id != canonical_id:
                raise TimesheetError('plane_import_identity_mapping_mismatch')
        labels = raw.get('labels')
        assignees = raw.get('assignees')
        if not isinstance(labels, list) or not isinstance(assignees, list):
            raise TimesheetError('plane_relationships_missing')
        normalized_labels = []
        for label in labels:
            if not isinstance(label, dict) or not isinstance(label.get('name'), str):
                raise TimesheetError('plane_expanded_labels_required')
            normalized_labels.append({'id': uuid(label['id']), 'name': label['name']})
        # We deliberately use the agreed effort labels. Plane estimate_point is
        # an estimate-definition ID/index, not hours, and is never summed.
        issue = {'id': canonical_id or 'plane:' + self.options.workspace_id + ':' + item_id,
                 'source': 'plane', 'source_issue_id': item_id,
                 'identifier': str(self.project_data[project_id]['identifier']) + '-' + str(raw['sequence_id']),
                 'url': f'{self.options.base}/{self.options.workspace}/projects/{project_id}/issues/{item_id}',
                 'title': raw['name'], 'createdAt': raw['created_at'], 'updatedAt': raw['updated_at'],
                 'completedAt': raw['completed_at'], 'archivedAt': raw.get('archived_at'),
                 'trashed': bool(raw.get('deleted_at')), 'state': self.state(raw['state']),
                 'project': {'id': self.options.projects[project_id]}, 'assignee': None,
                 'assignee_ids': [self.user(x) for x in assignees], 'labels': normalized_labels}
        if raw.get('is_draft'):
            issue['state'] = {'id': 'draft', 'type': 'unstarted'}
        history = []
        for activity in activities:
            if reference(activity['issue']) != item_id or reference(activity['project']) != project_id:
                raise TimesheetError('plane_activity_identity_mismatch')
            field = activity.get('field')
            if field in {'project', 'project_id', 'moved'}:
                raise TimesheetError('plane_project_move_requires_reconciliation')
            if field not in {'state', 'labels', 'assignees', 'name'}:
                continue
            record = {'id': uuid(activity['id']), 'createdAt': activity_time(activity), 'time_precision_seconds': 1}
            old, new = activity.get('old_identifier'), activity.get('new_identifier')
            if field == 'state':
                record.update(fromStateId=uuid(old) if old else None, toStateId=uuid(new) if new else None,
                              fromState=self.state(old) if old else None, toState=self.state(new) if new else None)
            elif field == 'labels':
                record['addedLabelIds'] = [uuid(new)] if new else []
                record['removedLabelIds'] = [uuid(old)] if old else []
                if old:
                    if not isinstance(activity.get('old_value'), str) or not activity['old_value']:
                        raise TimesheetError('plane_label_history_missing')
                    record['removedLabels'] = [{'id': uuid(old), 'name': activity['old_value']}]
            elif field == 'assignees':
                record['addedAssigneeIds'] = [self.user(new)] if new else []
                record['removedAssigneeIds'] = [self.user(old)] if old else []
            elif field == 'name':
                if not isinstance(activity.get('old_value'), str):
                    raise TimesheetError('plane_title_history_missing')
                record['fromTitle'] = activity['old_value']
            history.append(record)
        return {'issue': issue, 'history': history, 'plane_evidence': {'issue': raw, 'activities': activities}}

    def evidence(self, project_id, issue_id):
        for _ in range(3):
            raw = self.issue(project_id, issue_id)
            activities = self.pages(f'projects/{project_id}/work-items/{issue_id}/activities/')
            after = self.issue(project_id, issue_id)
            if raw != after:
                continue
            return self.normalize(raw, activities)
        raise TimesheetError('plane_issue_changed_during_read')

    def collect(self, config, end, deferred):
        result, seen = [], set()
        for project_id in self.options.projects:
            for raw in self.pages(f'projects/{project_id}/work-items/', {'expand': 'labels', 'fields': ITEM_FIELDS}):
                item_id = uuid(raw['id'])
                if item_id in seen:
                    raise TimesheetError('duplicate_plane_issue')
                seen.add(item_id)
                if timestamp(raw['created_at']) > timestamp(end):
                    continue
                try:
                    result.append(self.evidence(project_id, item_id))
                except TimesheetError as exc:
                    canonical = self.options.imported_issues.get(item_id)
                    if str(exc) == 'plane_import_identity_mapping_mismatch':
                        canonical = None  # A bad alias must not hide behind an allocated legacy ID.
                    result.append({'issue': {'id': canonical or 'plane:' + self.options.workspace_id + ':' + item_id,
                        'identifier': str(self.project_data[project_id]['identifier']) + '-' + str(raw['sequence_id']),
                        'url': f'{self.options.base}/{self.options.workspace}/projects/{project_id}/issues/{item_id}'},
                        'history': [], 'error': str(exc)})
        # Missing deferred work may have been archived or moved. Never silently
        # erase it from the exceptions list when v1 can no longer return it.
        resolved = {item['issue']['id'] for item in result}
        for key in set(deferred) - resolved:
            result.append({'issue': {'id': key, 'identifier': key, 'url': ''}, 'history': [],
                           'error': 'plane_deferred_issue_not_visible'})
        return result
