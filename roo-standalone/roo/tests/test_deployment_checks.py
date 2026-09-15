from pathlib import Path
import io
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from roo.deployment_checks import check_public_release, PUBLIC_PROBE


@pytest.mark.parametrize('migrated', [False, True])
def test_runtime_probe_checks_actual_sqlite_schema_without_upgrading(tmp_path, monkeypatch, migrated):
    from roo import config, coworking_booking_intents
    from roo.coworking_booking_schema_v3 import migrate_coworking_booking_intents_v3
    import urllib.request
    path = tmp_path / 'intents.db'
    if migrated:
        migrate_coworking_booking_intents_v3(path)
    else:
        with sqlite3.connect(path) as connection:
            connection.execute('PRAGMA user_version=1')
    store = coworking_booking_intents.CoworkingBookingIntentStore(path)
    monkeypatch.setattr(coworking_booking_intents, 'get_coworking_intent_store', lambda: store)
    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(ROO_SURFACE='public'))
    monkeypatch.setenv('ROO_RELEASE_SHA', 'a' * 40)
    monkeypatch.setenv('EXPECTED_RELEASE_SHA', 'a' * 40)
    health = Mock(return_value=io.BytesIO(b'{"status":"ok","surface":"public"}'))
    monkeypatch.setattr(urllib.request, 'urlopen', health)
    if migrated:
        exec(PUBLIC_PROBE, {})
        health.assert_called_once()
    else:
        with pytest.raises(RuntimeError):
            exec(PUBLIC_PROBE, {})
        health.assert_not_called()
        with sqlite3.connect(path) as connection:
            assert connection.execute('PRAGMA user_version').fetchone()[0] == 1


def test_admin_first_cannot_advance_old_public_checkout(tmp_path):
    run = Mock(return_value=SimpleNamespace(stdout='a' * 40))
    with pytest.raises(RuntimeError, match='Deploy and verify'):
        check_public_release('b' * 40, tmp_path, run=run)
    assert run.call_count == 1
    assert run.call_args.args[0][-2:] == ['rev-parse', 'HEAD']


def test_failed_migration_or_stopped_public_prevents_admin(tmp_path):
    run = Mock(side_effect=[SimpleNamespace(stdout='a' * 40),
                           subprocess.CalledProcessError(1, ['docker'])])
    with pytest.raises(subprocess.CalledProcessError):
        check_public_release('a' * 40, tmp_path, run=run)
    assert 'validate_schema()' in PUBLIC_PROBE
    assert 'ROO_RELEASE_SHA' in PUBLIC_PROBE
    assert '/healthz/ready' in PUBLIC_PROBE


def test_public_first_matching_healthy_release_allows_admin(tmp_path):
    run = Mock(return_value=SimpleNamespace(stdout='a' * 40))
    check_public_release('a' * 40, tmp_path, run=run)
    assert run.call_count == 2
    command = run.call_args.args[0]
    assert command[:6] == ['docker', 'compose', '-p', 'roo-standalone', 'exec', '-T']
    assert run.call_args.kwargs['check'] is True


def test_admin_workflow_depends_on_successful_public_deployment():
    root = Path(__file__).resolve().parents[3]
    # BaseLoader preserves YAML's `on` key rather than treating it as boolean.
    workflow = yaml.load((root / '.github/workflows/deploy-admin-production.yml').read_text(), Loader=yaml.BaseLoader)
    assert 'push' not in workflow['on']
    assert workflow['on']['workflow_run']['workflows'] == ['Deploy to Digital Ocean']
    assert "github.event.workflow_run.conclusion == 'success'" in workflow['jobs']['checks']['if']
    for job in ('checks', 'deploy'):
        assert 'github.event.workflow_run.head_sha' in workflow['jobs'][job]['env']['RELEASE_SHA']
    script = workflow['jobs']['deploy']['steps'][-1]['with']['script']
    assert 'checkout_release "$PUBLIC_DEPLOY_DIR"' not in script
    assert script.index('roo/deployment_checks.py') < script.index('docker compose up')
