"""Read-only release gate before Admin may restart the Public runtime."""
import argparse
from pathlib import Path
import re
import subprocess


PUBLIC_PROBE = """
import json, os, urllib.request
from roo.config import get_settings
from roo.coworking_booking_intents import get_coworking_intent_store
settings = get_settings()
assert settings.ROO_SURFACE == 'public', 'Expected Public Roo'
assert os.environ.get('ROO_RELEASE_SHA') == os.environ['EXPECTED_RELEASE_SHA'], 'Public image revision differs'
get_coworking_intent_store().validate_schema()
with urllib.request.urlopen('http://127.0.0.1:8000/healthz/ready', timeout=5) as response:
    health = json.load(response)
assert health['status'] == 'ok' and health['surface'] == 'public', 'Public Roo is not ready'
"""


def check_public_release(release_sha, public_directory, *, run=subprocess.run):
    if not re.fullmatch(r'[0-9a-f]{40}', release_sha):
        raise ValueError('Expected a full reviewed release SHA')
    public_directory = Path(public_directory)
    current = run(['git', '-C', str(public_directory), 'rev-parse', 'HEAD'],
                  check=True, text=True, capture_output=True).stdout.strip()
    if current != release_sha:
        raise RuntimeError('Deploy and verify this Public Roo release before deploying Admin')
    run(['docker', 'compose', '-p', 'roo-standalone', 'exec', '-T',
         '-e', 'EXPECTED_RELEASE_SHA=' + release_sha, 'roo', 'python', '-c', PUBLIC_PROBE],
        cwd=public_directory / 'roo-standalone', check=True, timeout=30)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-sha', required=True)
    parser.add_argument('--public-directory', required=True)
    args = parser.parse_args()
    check_public_release(args.release_sha, args.public_directory)
