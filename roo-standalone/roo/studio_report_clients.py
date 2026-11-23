"""Private client selection; names select an existing grant, never create one."""
import re

from .timesheets import TimesheetError


def client_key(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(char) < 32 for char in value)):
        raise TimesheetError('invalid_report_client')
    return ' '.join(value.split()).casefold()


def validate_client_reporting(clients):
    for actor, client in clients.items():
        aliases = client.get('aliases', [])
        grants = client.get('report_client_ids', [])
        if (not isinstance(aliases, list) or not isinstance(grants, list)
                or any(not isinstance(key, str) for key in grants)
                or len(set(grants)) != len(grants)):
            raise TimesheetError('invalid_client_reporting_scope')
        for alias in [client['name'], *aliases]:
            if client_key(alias) in {'all', 'self'}:
                raise TimesheetError('invalid_report_client')
        covered = set()
        for target in grants:
            if target == actor or target not in clients:
                raise TimesheetError('invalid_client_reporting_scope')
            projects = set(clients[target]['project_ids'])
            if not projects <= set(client['project_ids']) or covered & projects:
                raise TimesheetError('invalid_client_reporting_scope')
            covered.update(projects)


def select_client(clients, actor, selector):
    if actor not in clients:
        raise TimesheetError('client_access_not_configured')
    requester = clients[actor]
    query = client_key(selector.get('client', 'self'))
    grants = requester.get('report_client_ids', [])
    if query == 'self':
        return requester, [], {}
    if query == 'all':
        targets = grants if 'report_client_ids' in requester else [actor]
    else:
        mention = re.fullmatch(r'<@([UW][A-Z0-9]+)(?:\|[^>]+)?>', selector['client'].strip())
        query = mention[1].casefold() if mention else query
        targets = [key for key in [actor, *grants] if key in clients and query in
                   {client_key(value) for value in [key, clients[key]['name'], *clients[key].get('aliases', [])]}]
        if len(targets) != 1:
            # Same response for unknown, ambiguous and inaccessible clients.
            raise TimesheetError('report_client_unavailable')
    selected, groups, covered = {}, [], set()
    for target in targets:
        client = clients.get(target)
        if client is None or not set(client['project_ids']) <= set(requester['project_ids']):
            raise TimesheetError('report_client_unavailable')
        if covered & set(client['project_ids']):
            raise TimesheetError('report_client_unavailable')
        covered.update(client['project_ids'])
        selected[target] = client
        groups.append({'name': client['name'], 'project_ids': client['project_ids']})
    if query != 'all':
        return selected[targets[0]], [], selected
    unmapped = [key for key in requester['project_ids'] if key not in covered]
    if unmapped:
        groups.append({'name': 'Projects without a client mapping', 'project_ids': unmapped, 'unmapped': True})
    # An aggregate is usage only; never reuse one owner's contract as a shared
    # allowance over other clients or unassigned projects.
    aggregate = {key: value for key, value in requester.items() if key != 'contract_terms'}
    return {**aggregate, 'name': 'All accessible clients', 'monthly_hours': None,
            'monthly_allowances': {}}, groups, selected


def _project_name_key(value):
    # Studio display names may carry a literal "[Studio]" prefix. Clients
    # use both "Study Nash" and "Studynash" for the same configured name.
    label = value.strip().casefold().removeprefix('[studio]')
    return ''.join(label.split())


def select_project(client, groups, selector, projects):
    """Restrict an already-authorized client selection to one known project."""
    if 'project' not in selector:
        return client, groups
    value = selector['project']
    if not isinstance(value, str) or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise TimesheetError('report_project_unavailable')
    labels = [value]
    words = value.split(maxsplit=1)
    if len(words) == 2 and words[0].casefold() == 'project':
        # Some names, such as Project Acquire, include the word "Project".
        # Try the full name first, then the user's "Project Study Nash" form.
        labels.append(words[1])
    for label in labels:
        name = _project_name_key(label)
        matches = [key for key in client['project_ids']
                   if key in projects and (key == label.strip().casefold()
                                           or (name and name == _project_name_key(projects[key])))]
        if matches:
            break
    if len(matches) != 1:
        raise TimesheetError('report_project_unavailable')
    project_id = matches[0]
    selected = client
    if len(client['project_ids']) > 1:
        # A shared allowance and its rollover cannot be assigned to one
        # project. Keep the original owner configuration intact for access
        # checks and source selection.
        selected = {**client, 'project_ids': [project_id],
                    'monthly_hours': None, 'monthly_allowances': {}}
        selected.pop('contract_terms', None)
        selected.pop('contract_cash', None)
    groups = [{**group, 'project_ids': [project_id]} for group in groups
              if project_id in group['project_ids']]
    return selected, groups
