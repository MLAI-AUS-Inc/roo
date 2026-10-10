"""Offline publication keeps reviewed hours/payment files private and atomic."""
import hashlib
import json
from pathlib import Path
import stat
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scripts import install_studio_client_balance as installer


@pytest.fixture
def source(tmp_path, monkeypatch):
    project = '00000000-0000-0000-0000-000000000001'
    config = SimpleNamespace(team='T123', organization='00000000-0000-0000-0000-000000000020',
                             tz=ZoneInfo('Australia/Melbourne'), directory=tmp_path / 'private')
    clients = {'UMARK': {'name': 'Example client', 'project_ids': [project]}}
    dataset = {'project_ids': [project], 'currency': 'AUD', 'basis': 'gross',
        'allocation_rate_cents_per_hour': 10000,
        'coverage': {'start': '2026-01-01', 'through': '2026-02-28'},
        'as_of': '2026-03-01T00:00:00Z', 'limitations': ['Synthetic source.'],
        'invoices': [{'id': 'I1', 'number': 'EXAMPLE-I1', 'date': '2026-01-01',
                      'amount_cents': 10000, 'paid_cents': 10000, 'status': 'paid', 'evidence_ids': ['audit']}],
        'payments': [{'id': 'P1', 'bank_transaction_id': 'EXAMPLE-BANK1', 'date': '2026-01-10',
                      'amount_cents': 10000, 'invoice_ids': ['I1'], 'description': 'Private payment description',
                      'allocations': [{'invoice_id': 'I1', 'amount_cents': 10000}], 'evidence_ids': ['audit']}],
        'contractor_invoices': [{'id': 'B1', 'number': 'EXAMPLE-B1', 'supplier': 'Builder', 'date': '2026-02-01',
                           'hour_units': 12345000, 'excluded_hour_units': 0, 'unresolved_hour_units': 0,
                           'allocation_note': 'Reviewed', 'evidence_ids': ['audit']}],
        'hours': [{'id': 'H1', 'invoice_id': 'B1', 'project_id': project, 'hour_units': 12345000,
                  'work_start': '2026-01-01', 'work_end': '2026-01-31', 'date_basis': 'work_dates',
                  'allocation_note': 'Reviewed', 'description': 'Private work description', 'evidence_ids': ['audit']}]}
    value = {'version': 1, 'team': config.team, 'organization': config.organization, 'currency': 'AUD',
             'basis': 'gross', 'clients': {'UMARK': dataset},
             'evidence': {'audit': {'reference': 'Private source', 'sha256': 'a' * 64}}}
    path = tmp_path / 'source.json'
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    target = config.directory / 'client-balance.json'
    monkeypatch.setenv('STUDIO_CLIENT_BALANCE_FILE', str(target))
    monkeypatch.setattr(installer, 'configuration', lambda env: (config, clients))
    return SimpleNamespace(path=path, raw=raw, value=value, target=target, config=config)


def test_default_check_has_no_file_side_effects_or_financial_log_content(source, capsys):
    assert installer.main(['--source', str(source.path)]) == 0
    assert not source.config.directory.exists()
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result == {'status': 'checked', 'owners': ['UMARK'],
                      'count': {'clients': 1, 'invoices': 1, 'contractor_invoices': 1, 'hours': 1,
                                'payments': 1, 'pending_work': 0},
                      'sha256': hashlib.sha256(source.raw).hexdigest()}
    assert '12345' not in output and 'Private' not in output and str(source.path) not in output


def test_valid_install_is_exact_private_and_invalid_replacement_preserves_it(source, capsys):
    assert installer.main(['--source', str(source.path), '--install']) == 0
    assert source.target.read_bytes() == source.raw
    assert stat.S_IMODE(source.target.stat().st_mode) == 0o600
    source.value['clients']['UMARK']['payments'][0]['amount_cents'] += 1
    source.path.write_text(json.dumps(source.value))
    assert installer.main(['--source', str(source.path), '--install']) == 1
    assert source.target.read_bytes() == source.raw
    assert list(source.target.parent.iterdir()) == [source.target]
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {'status': 'blocked'}


def test_resolved_symlink_outside_private_data_cannot_be_overwritten(source, tmp_path, capsys):
    source.config.directory.mkdir()
    outside = tmp_path / 'outside.json'
    outside.write_text('preserve outside')
    source.target.symlink_to(outside)
    assert installer.main(['--source', str(source.path), '--install']) == 1
    assert outside.read_text() == 'preserve outside'
    assert source.target.is_symlink()
    assert json.loads(capsys.readouterr().out) == {'status': 'blocked'}


def test_explicit_missing_environment_file_does_not_fall_back_to_process_config(source, capsys):
    assert installer.main(['--source', str(source.path), '--env-file', str(source.path.parent / 'missing.env')]) == 1
    assert not source.config.directory.exists()
    assert json.loads(capsys.readouterr().out) == {'status': 'blocked'}
