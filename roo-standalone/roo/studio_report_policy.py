"""Private per-owner hours sources; selectors cannot grant or change access."""
from copy import deepcopy
import re

from .studio_report_clients import client_key, select_client
from .timesheets import TimesheetError


def validate_hours_policy(clients):
    """Validate optional source/history settings after the ordinary client maps."""
    from .studio_reports import month_offset

    owner_sources = {}
    for client in clients.values():
        source = client.get('hours_source', 'auto')
        if not isinstance(source, str) or source not in {'auto', 'linear'}:
            raise TimesheetError('invalid_client_hours_policy')
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
