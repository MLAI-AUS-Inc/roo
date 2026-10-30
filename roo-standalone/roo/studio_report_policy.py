"""Private per-owner hours sources; selectors cannot grant or change access."""
from copy import deepcopy
import re
from uuid import UUID

from .studio_report_clients import client_key, select_client
from .timesheets import LABELS, UNITS, TimesheetError, timestamp


def validate_hours_policy(clients):
    """Validate optional source/history settings after the ordinary client maps."""
    from .studio_reports import month_offset

    owner_sources = {}
    for client in clients.values():
        source = client.get('hours_source', 'auto')
        if not isinstance(source, str) or source not in {'auto', 'linear'}:
            raise TimesheetError('invalid_client_hours_policy')
        resolutions = client.get('effort_resolutions', {})
        if (not isinstance(resolutions, dict) or (resolutions and
                (source != 'linear' or 'report_client_ids' in client))):
            raise TimesheetError('invalid_effort_resolution')
        for issue_id, resolution in resolutions.items():
            if (not isinstance(resolution, dict)
                    or set(resolution) != {'project_id', 'completion_event_id', 'size'}
                    or resolution['project_id'] not in client['project_ids']
                    or not isinstance(resolution['size'], str) or resolution['size'] not in UNITS):
                raise TimesheetError('invalid_effort_resolution')
            try:
                UUID(issue_id)
                UUID(resolution['completion_event_id'])
            except (ValueError, TypeError, AttributeError) as exc:
                raise TimesheetError('invalid_effort_resolution') from exc
        if 'history_start_month' in client:
            month = client['history_start_month']
            if not isinstance(month, str) or not re.fullmatch(r'\d{4}-\d{2}', month):
                raise TimesheetError('invalid_client_hours_policy')
            try:
                month_offset(month, 0)
            except TimesheetError as exc:
                raise TimesheetError('invalid_client_hours_policy') from exc
        # Reporting entries aggregate existing owner grants; their broad project
        # access is not an additional ownership/source declaration.
        if 'report_client_ids' in client:
            continue
        for project in client['project_ids']:
            previous = owner_sources.setdefault(project, source)
            if previous != source:
                raise TimesheetError('invalid_client_hours_policy')


def linear_projects(clients, actor, selector):
    """Return explicitly Linear projects within the selected, authorized scope."""
    selected, _, targets = select_client(clients, actor, selector)
    requester = clients[actor]
    scope = set(selected['project_ids'])
    query = client_key(selector.get('client', 'self'))
    if query in {'self', 'all'} and 'report_client_ids' in requester:
        owners = [clients[key] for key in requester['report_client_ids']]
    else:
        owners = list(targets.values()) if targets else [requester]
    return {project for owner in owners
            if owner.get('hours_source', 'auto') == 'linear'
            and set(owner['project_ids']) <= scope
            for project in owner['project_ids']}


def selected_effort_resolutions(clients, actor, selector):
    """Private owner reviews follow the same grants as the source policy."""
    selected, _, targets = select_client(clients, actor, selector)
    requester = clients[actor]
    owners = (list(targets.values()) if targets else
              [requester, *(clients[key] for key in requester.get('report_client_ids', []))])
    projects = linear_projects(clients, actor, selector)
    result = {}
    for owner in owners:
        if owner.get('hours_source') != 'linear' or not set(owner['project_ids']) <= set(selected['project_ids']):
            continue
        for issue_id, resolution in owner.get('effort_resolutions', {}).items():
            if resolution['project_id'] in projects:
                if issue_id in result and result[issue_id] != resolution:
                    raise TimesheetError('invalid_effort_resolution')
                result[issue_id] = resolution
    return result


def resolve_report_effort(issue, history, completed, resolutions):
    """An explicit owner review can choose a size added at first completion."""
    resolution = (resolutions or {}).get(issue['id'])
    if resolution is not None:
        events = [event for event in history if event['id'] == resolution['completion_event_id']]
        if len(events) != 1 or resolution['project_id'] != (issue.get('project') or {}).get('id'):
            raise TimesheetError('effort_resolution_evidence_mismatch')
        event = events[0]
        sizes = [LABELS[name] for key, name in issue['labels'].items()
                 if key in (event.get('addedLabelIds') or []) and name in LABELS]
        if (timestamp(event['createdAt']) != timestamp(completed)
                or (event.get('toState') or {}).get('type') != 'completed'
                or (event.get('fromState') or {}).get('type') == 'completed'
                or sizes.count(resolution['size']) != 1):
            raise TimesheetError('effort_resolution_evidence_mismatch')
        return resolution['size'], True
    sizes = [LABELS[name] for name in issue['labels'].values() if name in LABELS]
    if len(sizes) != 1:
        raise TimesheetError('exactly_one_effort_label_required')
    return sizes[0], False


def filter_backfill(backfill, excluded_projects):
    """Copy the manifest without invoice policy/evidence for task-only projects."""
    if backfill is None:
        return None
    result = deepcopy(backfill)
    excluded = set(excluded_projects)
    if not excluded:
        return result
    for field in ('entries', 'pending', 'qualifications'):
        if field in result:
            result[field] = [row for row in result[field] if row['project_id'] not in excluded]
    if 'invoice_first_projects' in result:
        result['invoice_first_projects'] = [project for project in result['invoice_first_projects']
                                            if project not in excluded]
    if 'sources' in result:
        referenced = {row['source_id'] for field in ('entries', 'pending', 'qualifications')
                      for row in result.get(field, []) if row.get('source_id')}
        result['sources'] = {key: source for key, source in result['sources'].items()
                             if key in referenced}
    return result
