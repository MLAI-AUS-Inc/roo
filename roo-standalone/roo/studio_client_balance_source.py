"""Reviewed client finance snapshots; Xero credentials never enter Public Roo."""
from datetime import date, timedelta
import json
from pathlib import Path
import re

from .studio_report_clients import select_client
from .timesheets import TimesheetError, timestamp


def _text(value, limit=2000):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 for char in value)):
        raise ValueError
    return value


def _cents(value, *, positive=False):
    if type(value) is not int or value < int(positive) or value > 10**12:
        raise ValueError
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError
    return date.fromisoformat(value)


def _records(value, *, maximum=10000):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError
    result = {}
    for row in value:
        if not isinstance(row, dict):
            raise ValueError
        key = _text(row['id'], 200)
        if key in result:
            raise ValueError
        result[key] = row
    return result


def validate_balance_snapshot(value, config, clients):
    """Conserve reviewed hours and paid cents; contractor pay rates are irrelevant."""
    try:
        if (type(value['version']) is not int or value['version'] != 1
                or value['team'] != config.team or value['organization'] != config.organization
                or value['currency'] != 'AUD' or value['basis'] != 'gross'
                or not isinstance(value['clients'], dict) or not value['clients']):
            raise ValueError
        evidence = value['evidence']
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError
        for key, source in evidence.items():
            _text(key, 200)
            _text(source['reference'])
            if not re.fullmatch(r'[a-f0-9]{64}', source['sha256']):
                raise ValueError
        for actor, dataset in value['clients'].items():
            if (actor not in clients or not isinstance(dataset, dict)
                    or not isinstance(dataset['project_ids'], list)
                    or len(set(dataset['project_ids'])) != len(dataset['project_ids'])
                    or set(dataset['project_ids']) != set(clients[actor]['project_ids'])):
                raise ValueError
            if dataset.get('currency') != 'AUD' or dataset.get('basis') != 'gross':
                raise ValueError
            rate = _cents(dataset['allocation_rate_cents_per_hour'], positive=True)
            start = _date(dataset['coverage']['start'])
            through = _date(dataset['coverage']['through'])
            as_of = timestamp(dataset['as_of'])
            if start > through or through > as_of.astimezone(config.tz).date():
                raise ValueError
            for note in dataset['limitations']:
                _text(note)
            if 'scope_label' in dataset:
                _text(dataset['scope_label'], 150)
            if not isinstance(dataset['limitations'], list):
                raise ValueError
            invoices = _records(dataset['invoices'])
            contractor_invoices = _records(dataset['contractor_invoices'])
            hours = _records(dataset['hours'])
            pending = _records(dataset.get('pending_work', []))
            payments = _records(dataset['payments'])

            def provenance(row):
                keys = row['evidence_ids']
                if (not isinstance(keys, list) or not keys or len(set(keys)) != len(keys)
                        or any(key not in evidence for key in keys)):
                    raise ValueError

            def source_date(row):
                day = _date(row['date'])
                if not start <= day <= through:
                    raise ValueError

            numbers = set()
            for invoice in invoices.values():
                source_date(invoice)
                provenance(invoice)
                number = _text(invoice['number'], 200)
                if number in numbers:
                    raise ValueError
                numbers.add(number)
                _cents(invoice['amount_cents'], positive=True)
                paid = _cents(invoice['paid_cents'])
                if paid > invoice['amount_cents']:
                    raise ValueError
                status = invoice['status']
                if status not in {'paid', 'authorised', 'voided', 'draft'}:
                    raise ValueError
                if ((status == 'paid' and paid != invoice['amount_cents'])
                        or (status in {'voided', 'draft'} and paid)):
                    raise ValueError
            allocations = {key: 0 for key in invoices}
            bank_ids = set()
            for payment in payments.values():
                source_date(payment)
                provenance(payment)
                _cents(payment['amount_cents'], positive=True)
                # Credits must be representable at the same millionth-hour
                # precision as the reviewed work. Never round paid credits.
                if payment['amount_cents'] * 1_000_000 % rate:
                    raise ValueError
                _text(payment['description'])
                bank_id = _text(payment['bank_transaction_id'], 200)
                if bank_id in bank_ids:
                    raise ValueError
                bank_ids.add(bank_id)
                parts = payment['allocations']
                if not isinstance(parts, list) or not parts:
                    raise ValueError
                selected = set()
                for part in parts:
                    key = part['invoice_id']
                    _cents(part['amount_cents'], positive=True)
                    if (key in selected or key not in invoices
                            or invoices[key]['status'] not in {'paid', 'authorised'}
                            or _date(payment['date']) < _date(invoices[key]['date'])):
                        raise ValueError
                    selected.add(key)
                    allocations[key] += part['amount_cents']
                if (sum(p['amount_cents'] for p in parts) != payment['amount_cents']
                        or payment['invoice_ids'] != [p['invoice_id'] for p in parts]):
                    raise ValueError
            if any(allocations[key] != row['paid_cents'] for key, row in invoices.items()):
                raise ValueError
            totals = {key: 0 for key in contractor_invoices}
            cost_numbers = set()
            for invoice in contractor_invoices.values():
                source_date(invoice)
                provenance(invoice)
                identity = (_text(invoice['supplier'], 200), _text(invoice['number'], 200))
                if identity in cost_numbers:
                    raise ValueError
                cost_numbers.add(identity)
                _cents(invoice['hour_units'], positive=True)
                _cents(invoice['excluded_hour_units'])
                _cents(invoice['unresolved_hour_units'])
                if invoice['excluded_hour_units'] or invoice['unresolved_hour_units']:
                    _text(invoice['allocation_note'])
            for cost in hours.values():
                provenance(cost)
                invoice_id = cost.get('invoice_id')
                if ((invoice_id is not None and invoice_id not in contractor_invoices)
                        or cost['project_id'] not in dataset['project_ids']):
                    raise ValueError
                _cents(cost['hour_units'], positive=True)
                first, last = _date(cost['work_start']), _date(cost['work_end'])
                if not start <= first <= last <= through:
                    raise ValueError
                if cost['date_basis'] not in {'work_dates', 'estimated_work_period', 'invoice_date'}:
                    raise ValueError
                _text(cost['description'])
                _text(cost['allocation_note'])
                if cost['date_basis'] == 'invoice_date' and (
                        invoice_id is None or first != last
                        or first.isoformat() != contractor_invoices[invoice_id]['date']):
                    raise ValueError
                if invoice_id is not None:
                    totals[invoice_id] += cost['hour_units']
            if any(totals[key] + row['excluded_hour_units'] + row['unresolved_hour_units'] != row['hour_units']
                   for key, row in contractor_invoices.items()):
                raise ValueError
            for item in pending.values():
                provenance(item)
                _text(item['supplier'], 200)
                _text(item['invoice_number'], 200)
                _text(item['note'])
                if not start <= _date(item['evidence_date']) <= through:
                    raise ValueError
                if any(key in item for key in ('hour_units', 'hours', 'amount_cents')):
                    raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError, TimesheetError) as exc:
        raise TimesheetError('invalid_client_balance_snapshot') from exc
    return value


def load_balance_snapshot(path, config, clients):
    if not path:
        raise TimesheetError('client_balance_not_configured')
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise TimesheetError('client_balance_source_unavailable') from exc
    return validate_balance_snapshot(value, config, clients)


def select_balance_dataset(snapshot, config, clients, actor, selector, now, *, max_age_days=7):
    """Existing project grants are necessary; a reviewed finance scope is required too."""
    if selector.get('client', 'self').casefold() == 'all':
        raise TimesheetError('client_balance_single_client_required')
    client, _, targets = select_client(clients, actor, selector)
    owner = next(iter(targets)) if targets else actor
    dataset = snapshot['clients'].get(owner)
    if dataset is None or set(dataset['project_ids']) != set(client['project_ids']):
        raise TimesheetError('client_balance_access_unavailable')
    as_of = timestamp(dataset['as_of'])
    current = timestamp(now)
    if type(max_age_days) is not int or not 1 <= max_age_days <= 31:
        raise TimesheetError('invalid_client_balance_max_age')
    if as_of > current + timedelta(minutes=5) or current - as_of > timedelta(days=max_age_days):
        raise TimesheetError('client_balance_source_stale')
    return client, dataset, owner
