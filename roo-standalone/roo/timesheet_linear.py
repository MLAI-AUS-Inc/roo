"""Read-only Linear evidence collection for cutoff-bound timesheets."""
from .payment_reminder_worker import ReminderAPI
from .timesheets import TimesheetError, timestamp

FIELDS = '''id identifier title url createdAt updatedAt completedAt archivedAt trashed
 assignee { id } project { id } state { id type }
 labels(first: 100, includeArchived: true) { nodes { id name } pageInfo { hasNextPage endCursor } }'''
ISSUES = 'query TimesheetIssues($filter: IssueFilter!, $after: String) { issues(first: 100, after: $after, includeArchived: true, orderBy: createdAt, filter: $filter) { nodes { ' + FIELDS + ' } pageInfo { hasNextPage endCursor } } }'
ISSUE = 'query TimesheetIssue($id: String!) { issue(id: $id) { ' + FIELDS + ' } }'
HISTORY = '''query TimesheetHistory($id: String!, $after: String) {
 issue(id: $id) { id updatedAt history(first: 100, after: $after, includeArchived: true, orderBy: createdAt) {
 nodes { id createdAt fromTitle
 fromAssigneeId toAssigneeId fromAssignee { id } toAssignee { id }
 fromProjectId toProjectId fromProject { id } toProject { id }
 fromStateId toStateId fromState { id type } toState { id type }
 addedLabelIds removedLabelIds removedLabels { id name } }
 pageInfo { hasNextPage endCursor }
 } }
}'''
LABEL_PAGE = '''query TimesheetLabels($id: String!, $after: String) { issue(id: $id) {
 id updatedAt labels(first:100, after:$after, includeArchived:true) { nodes { id name } pageInfo { hasNextPage endCursor } }
} }'''


def next_page(connection, seen):
    if not isinstance(connection, dict) or not isinstance(connection.get('nodes'), list):
        raise TimesheetError('incomplete_linear_page')
    page = connection.get('pageInfo') or {}
    if page.get('hasNextPage') is False:
        return None
    cursor = page.get('endCursor')
    if page.get('hasNextPage') is not True or not isinstance(cursor, str) or not cursor or cursor in seen:
        raise TimesheetError('invalid_linear_cursor')
    seen.add(cursor)
    return cursor


class TimesheetAPI(ReminderAPI):
    def verify(self, config):
        if self.slack('auth.test').get('team_id') != config.team:
            raise TimesheetError('workspace_mismatch')
        organization = self.linear('query TimesheetOrganization { organization { id } }').get('organization')
        if not organization or organization.get('id') != config.organization:
            raise TimesheetError('organization_mismatch')
        for project_id in config.projects:
            project = self.linear('query TimesheetProject($id: String!) { project(id:$id) { id } }', {'id': project_id}).get('project')
            if not project or project.get('id') != project_id:
                raise TimesheetError('project_access_unavailable')

    def verify_recipient(self, user_id, team):
        if self.slack('auth.test').get('team_id') != team:
            raise TimesheetError('workspace_mismatch')
        user = self.slack('users.info', {'user': user_id}).get('user') or {}
        if (user.get('id') != user_id or user.get('team_id') != team or user.get('deleted')
                or user.get('is_bot') or user.get('is_app_user')):
            raise TimesheetError('recipient_unavailable')

    def read_issue(self, issue_id):
        issue = self.linear(ISSUE, {'id': issue_id}).get('issue')
        if not issue or issue.get('id') != issue_id:
            raise TimesheetError('issue_unavailable')
        return issue

    def evidence(self, issue_id):
        # Re-read the version after every paginated evidence read. Never combine
        # a current issue with history from another version.
        for _ in range(3):
            issue = self.read_issue(issue_id)
            version = issue['updatedAt']
            history, after, seen = {}, None, set()
            stable = True
            while True:
                data = self.linear(HISTORY, {'id': issue_id, 'after': after})['issue']
                if data['id'] != issue_id or data['updatedAt'] != version:
                    stable = False
                    break
                connection = data['history']
                after = next_page(connection, seen)
                for h in connection['nodes']:
                    if h['id'] in history and history[h['id']] != h:
                        raise TimesheetError('inconsistent_history')
                    history[h['id']] = h
                if after is None:
                    break
            if not stable:
                continue
            connection = issue['labels']
            labels, seen = {}, set()
            while True:
                after = next_page(connection, seen)
                labels.update({x['id']: x['name'] for x in connection['nodes']})
                if after is None:
                    break
                data = self.linear(LABEL_PAGE, {'id': issue_id, 'after': after})['issue']
                if data['id'] != issue_id or data['updatedAt'] != version:
                    stable = False
                    break
                connection = data['labels']
            if not stable or self.read_issue(issue_id)['updatedAt'] != version:
                continue
            issue['labels'] = [{'id': k, 'name': v} for k, v in labels.items()]
            return {'issue': issue, 'history': list(history.values())}
        raise TimesheetError('issue_changed_during_read')

    def collect(self, config, end, deferred):
        issues, after, seen = {}, None, set()
        filters = {'createdAt': {'lte': timestamp(end).isoformat()}, 'or': [
            {'project': {'id': {'in': list(config.projects)}}},
            {'updatedAt': {'gte': timestamp(config.beginning).isoformat()}},
        ]}
        while True:
            data = self.linear(ISSUES, {'filter': filters, 'after': after})['issues']
            after = next_page(data, seen)
            issues.update({issue['id']: issue for issue in data['nodes']})
            if after is None:
                break
        # Deferred rows remain discoverable after moving projects or becoming old.
        for issue_id in deferred:
            if issue_id not in issues:
                issues[issue_id] = self.read_issue(issue_id)
        result = []
        for issue_id, original in issues.items():
            try:
                item = self.evidence(issue_id)
            except TimesheetError as exc:
                if (original.get('project') or {}).get('id') in config.projects or issue_id in deferred:
                    result.append({'issue': original, 'history': [], 'error': str(exc)})
                    continue
                # A failed history read could hide a move out of a paid project.
                raise
            history = item['history']
            relevant = ((item['issue'].get('project') or {}).get('id') in config.projects
                        or issue_id in deferred or any(
                            h.get('fromProjectId') in config.projects or h.get('toProjectId') in config.projects
                            for h in history))
            if relevant:
                result.append(item)
        return result
