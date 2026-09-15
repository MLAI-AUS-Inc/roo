"""Render the real configurations used by automated public restarts."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which('docker') is None, reason='Docker Compose CLI required')
def test_base_public_restart_keeps_worker_queue():
    root = Path(__file__).resolve().parents[2]
    def config(*files):
        command = ['docker', 'compose']
        for name in files:
            command += ['-f', name]
        command += ['config', '--no-env-resolution', '--format', 'json']
        env = {key: value for key, value in os.environ.items() if not key.startswith('COMPOSE_')}
        return json.loads(subprocess.check_output(command, cwd=root, env=env, text=True))
    base = config('docker-compose.yml')
    legacy = config('docker-compose.yml', 'docker-compose.timesheet-commands.yml')
    worker = config('docker-compose.timesheets.yml')
    def queue(service):
        mounts = [mount for mount in service['volumes'] if mount['target'] == '/app/timesheets/queue']
        assert len(mounts) == 1
        assert mounts[0]['type'] == 'bind'
        return mounts[0]['source']
    assert base['name'] == 'roo-standalone'
    assert worker['name'] == 'roo-timesheets'
    assert queue(base['services']['roo']) == queue(legacy['services']['roo']) == queue(worker['services']['timesheets'])
    assert base['services']['roo']['environment']['TIMESHEET_QUEUE_DIR'] == '/app/timesheets/queue'
    assert not any(mount['target'] == '/app/timesheets/data' for mount in base['services']['roo']['volumes'])
