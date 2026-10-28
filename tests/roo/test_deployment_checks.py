from pathlib import Path
import io
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from roo.deployment_checks import check_public_release, PUBLIC_PROBE
from roo.retired_feature_config import retire_feature_config


@pytest.mark.parametrize("value, expected", [
    ('"victor-ai-applications, mlai-points,content-factory"', "mlai-points,content-factory"),
    ("victor-ai-applications", "[]"),
    ("victor-ai-applications,unknown-skill", "unknown-skill"),
    ("mlai-points,content-factory", "mlai-points,content-factory"),
    ("[]", "[]"),
    ("", ""),
])
def test_retired_feature_env_cleanup_preserves_skill_scope(tmp_path, value, expected):
    path = tmp_path / ".env"
    path.write_text(
        "# keep this comment\n"
        "UNRELATED_CONFIG=synthetic-value\n"
        "VICTOR_AI_ROO_SIGNING_SECRET=synthetic-retired-secret\n"
        "OFFICE_MANAGER_ACTIONS_ENABLED=true\n"
        f"ROO_ENABLED_SKILLS={value}\n"
    )
    retire_feature_config(path)
    assert path.read_text() == (
        "# keep this comment\n"
        "UNRELATED_CONFIG=synthetic-value\n"
        f"ROO_ENABLED_SKILLS={expected}\n"
    )
    retire_feature_config(path)
    assert f"ROO_ENABLED_SKILLS={expected}\n" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_retired_only_allowlist_loads_no_skills_and_unknown_names_still_fail(tmp_path, monkeypatch):
    from roo.config import Settings
    from roo import agent as agent_module

    path = tmp_path / ".env"
    path.write_text("ROO_ENABLED_SKILLS=victor-ai-applications\n")
    retire_feature_config(path)
    configured = Settings(
        _env_file=path, SLACK_BOT_TOKEN="test", SLACK_SIGNING_SECRET="test",
        OPENAI_API_KEY="test", FOUNDER_TOOLS_LINK_ORIGINS="https://mlai.au",
    )
    assert configured.has_explicit_skill_allowlist
    assert configured.enabled_skill_names == frozenset()
    monkeypatch.setattr(agent_module, "get_settings", lambda: configured)
    assert agent_module.RooAgent().skills == []

    path.write_text("ROO_ENABLED_SKILLS=victor-ai-applications,unknown-skill\n")
    retire_feature_config(path)
    configured = Settings(
        _env_file=path, SLACK_BOT_TOKEN="test", SLACK_SIGNING_SECRET="test",
        OPENAI_API_KEY="test", FOUNDER_TOOLS_LINK_ORIGINS="https://mlai.au",
    )
    with pytest.raises(ValueError, match="unknown-skill"):
        agent_module.RooAgent()


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
    assert command[:8] == ['docker', 'compose', '-p', 'roo-standalone', '-f', '../infra/docker-compose.yml', 'exec', '-T']
    assert run.call_args.kwargs['check'] is True


def test_admin_workflow_depends_on_successful_public_deployment():
    root = Path(__file__).resolve().parents[2]
    # BaseLoader preserves YAML's `on` key rather than treating it as boolean.
    workflow = yaml.load((root / '.github/workflows/deploy-admin-production.yml').read_text(), Loader=yaml.BaseLoader)
    assert 'push' not in workflow['on']
    assert workflow['on']['workflow_run']['workflows'] == ['Deploy to Digital Ocean']
    assert "github.event.workflow_run.conclusion == 'success'" in workflow['jobs']['checks']['if']
    for job in ('checks', 'deploy'):
        assert 'github.event.workflow_run.head_sha' in workflow['jobs'][job]['env']['RELEASE_SHA']
    script = workflow['jobs']['deploy']['steps'][-1]['with']['script']
    assert 'checkout_release "$PUBLIC_DEPLOY_DIR"' not in script
    assert script.index('roo/deployment_checks.py') < script.index('docker compose --env-file .env -f ../infra/docker-compose.yml up')


@pytest.mark.parametrize('legacy', [True, False])
def test_deploy_captures_previous_image_with_either_compose_layout(tmp_path, legacy):
    """Exercise the real pre-checkout script against old and reorganized releases."""
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.load((root / '.github/workflows/deploy.yml').read_text(), Loader=yaml.BaseLoader)
    script = next(step['with']['script'] for step in workflow['jobs']['deploy']['steps']
                  if 'with' in step and 'script' in step['with'])
    start = script.index('previous_compose_dir=')
    end = script.index('previous_image_id=', start)
    app = tmp_path / 'roo-standalone'
    app.mkdir()
    compose = app if legacy else tmp_path / 'infra'
    compose.mkdir(exist_ok=True)
    (compose / 'docker-compose.yml').touch()
    # A fake Docker command rejects missing -f paths, just like Compose.
    shell = '''docker() {
      while [ "$#" -gt 0 ]; do
        if [ "$1" = "-f" ]; then
          shift
          test -f "$1" || return 1
        fi
        shift
      done
      printf 'previous-container'
    }
    ''' + script[start:end] + '\nprintf "%s" "$previous_container_id"\n'
    result = subprocess.run(['bash', '-e', '-c', shell], cwd=app,
                            check=True, capture_output=True, text=True)
    assert result.stdout == 'previous-container'
    assert '-f "$previous_compose_dir/docker-compose.yml" --ansi never up' in script


def _public_deploy_script():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.load(
        (root / '.github/workflows/deploy.yml').read_text(), Loader=yaml.BaseLoader
    )
    return next(step['with']['script'] for step in workflow['jobs']['deploy']['steps']
                if 'script' in step.get('with', {}))


def _founder_link_probe():
    script = _public_deploy_script()
    start = script.rindex('if [ "$FOUNDER_ACCOUNT_LINK_ENABLED" = "true" ]; then')
    return script[start:script.index('\nfi', start) + len('\nfi')]


def _run_founder_link_probe(tmp_path, enabled, docker_status=0):
    import json
    import os
    import shutil
    import sys

    # Docker is an external boundary. Record its real arguments while the shell
    # has no host Python or curl available, reproducing the deployment host.
    capture = tmp_path / 'docker-args.json'
    fake = tmp_path / 'docker.py'
    fake.write_text(
        'import json, pathlib, sys\n'
        f'pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n'
        f'sys.exit({docker_status})\n'
    )
    shell = f'docker() {{ "{sys.executable}" "{fake}" "$@"; }}\n' + _founder_link_probe()
    result = subprocess.run(
        [shutil.which('bash'), '-e', '-c', shell],
        env={**os.environ, 'PATH': '', 'FOUNDER_ACCOUNT_LINK_ENABLED': enabled},
        capture_output=True, text=True,
    )
    return result, json.loads(capture.read_text()) if capture.exists() else None


def _probe_python():
    import shlex
    words = shlex.split(_founder_link_probe())
    command = words[words.index('then') + 1:words.index('fi')]
    assert command[:-1] == [
        'docker', 'compose', '--env-file', '.env', '-f',
        '../infra/docker-compose.yml', 'exec', '-T', 'roo', 'python', '-c',
    ]
    return command[-1]


def test_founder_link_probe_uses_container_python(tmp_path, monkeypatch):
    import urllib.request
    result, arguments = _run_founder_link_probe(tmp_path, 'true')
    assert result.returncode == 0, result.stderr
    assert arguments[:-1] == [
        'compose', '--env-file', '.env', '-f', '../infra/docker-compose.yml',
        'exec', '-T', 'roo', 'python', '-c',
    ]
    calls = []

    def healthy_backend(url, *, timeout):
        calls.append((url, timeout))
        return io.BytesIO(
            b'{"status":"ok","dependencies":{"mlai_backend":'
            b'{"founder_account_link":{"status":"ok","contract":"slack-founder-link-v1"}}}}'
        )

    monkeypatch.setattr(urllib.request, 'urlopen', healthy_backend)
    exec(arguments[-1], {})
    assert calls == [('http://127.0.0.1:8000/healthz/dependencies', 8)]


def test_disabled_founder_link_skips_dependency_probe(tmp_path):
    result, arguments = _run_founder_link_probe(tmp_path, 'false')
    assert result.returncode == 0, result.stderr
    assert arguments is None


@pytest.mark.parametrize('failure', [
    'timeout', 'http-error', 'invalid-json', 'missing-capability',
    'wrong-contract', 'unhealthy',
])
def test_founder_link_probe_rejects_invalid_dependency(monkeypatch, failure):
    import urllib.error
    import urllib.request
    responses = {
        'invalid-json': b'not json',
        'missing-capability': b'{"status":"ok","dependencies":{"mlai_backend":{}}}',
        'wrong-contract': b'{"status":"ok","dependencies":{"mlai_backend":'
                          b'{"founder_account_link":{"status":"ok","contract":"wrong"}}}}',
        'unhealthy': b'{"status":"degraded","dependencies":{"mlai_backend":'
                     b'{"founder_account_link":{"status":"ok","contract":"slack-founder-link-v1"}}}}',
    }

    def failed_backend(url, *, timeout):
        if failure == 'timeout':
            raise TimeoutError('synthetic timeout')
        if failure == 'http-error':
            raise urllib.error.HTTPError(url, 503, 'unavailable', {}, None)
        return io.BytesIO(responses[failure])

    monkeypatch.setattr(urllib.request, 'urlopen', failed_backend)
    probe = _probe_python()
    with pytest.raises((TimeoutError, urllib.error.HTTPError, ValueError, AssertionError)):
        exec(probe, {})


def test_failed_founder_link_probe_fails_deployment(tmp_path):
    result, arguments = _run_founder_link_probe(tmp_path, 'true', docker_status=9)
    assert arguments is not None
    assert result.returncode == 9


def test_dependency_health_remains_private():
    root = Path(__file__).resolve().parents[2]
    nginx = (root / 'infra/nginx/roo.conf').read_text()
    assert '/healthz/dependencies' not in nginx
    assert 'location / { return 404; }' in nginx
    assert 'expect_public_status 404 GET /healthz/dependencies' in _public_deploy_script()


def test_runtime_account_link_check_rejects_wrong_release_or_flag(monkeypatch):
    import os
    import shlex
    from roo import config

    script = _public_deploy_script()
    line = next(line for line in script.splitlines()
                if 'actions = settings.implicit_action_allowlist' in line)
    words = shlex.split(line)
    assert 'EXPECTED_RELEASE_SHA=$RELEASE_SHA' in words
    assert 'EXPECTED_FOUNDER_ACCOUNT_LINK_ENABLED=$FOUNDER_ACCOUNT_LINK_ENABLED' in words
    code = words[words.index('-c') + 1]
    monkeypatch.setenv('EXPECTED_RELEASE_SHA', 'a' * 40)
    monkeypatch.setenv('ROO_RELEASE_SHA', 'a' * 40)
    monkeypatch.setenv('EXPECTED_FOUNDER_ACCOUNT_LINK_ENABLED', 'true')
    settings = SimpleNamespace(
        FOUNDER_ACCOUNT_LINK_ENABLED=True,
        implicit_action_allowlist={'mlai-points:link_founder_account'},
    )
    monkeypatch.setattr(config, 'get_settings', lambda: settings)
    exec(code, {})
    monkeypatch.setenv('ROO_RELEASE_SHA', 'b' * 40)
    with pytest.raises(AssertionError):
        exec(code, {})
    monkeypatch.setenv('ROO_RELEASE_SHA', os.environ['EXPECTED_RELEASE_SHA'])
    settings.FOUNDER_ACCOUNT_LINK_ENABLED = False
    settings.implicit_action_allowlist = set()
    with pytest.raises(AssertionError):
        exec(code, {})
