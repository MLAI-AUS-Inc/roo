#!/usr/bin/env python3
"""Offline check or atomic publication of a private hours/payment snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roo.studio_client_balance_source import validate_balance_snapshot
from roo.studio_report_worker import configuration
from roo.timesheets import TimesheetError


def target_path(env, config, *, required):
    raw = env.get('STUDIO_CLIENT_BALANCE_FILE')
    if not raw:
        if required:
            raise TimesheetError('client_balance_target_required')
        return None
    root = config.directory.resolve()
    target = Path(raw).resolve()
    if root not in target.parents or target.is_dir():
        raise TimesheetError('client_balance_target_must_be_private')
    return target


def atomic_install(raw, target):
    """Replace only after validation; interruption leaves the previous file whole."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.' + target.name + '.',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Reviewed source JSON; never an invoice source directory')
    parser.add_argument('--env-file', help='Explicit private worker dotenv file; otherwise process environment only')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check', action='store_true', help='Validate only (default); makes no changes')
    action.add_argument('--install', action='store_true', help='Publish to the configured private worker file')
    args = parser.parse_args(argv)
    try:
        env = dict(os.environ)
        if args.env_file:
            if not Path(args.env_file).is_file():
                raise TimesheetError('client_balance_env_file_unavailable')
            from dotenv import dotenv_values
            env.update({key: value for key, value in dotenv_values(args.env_file).items() if value is not None})
        config, clients = configuration(env)
        raw = Path(args.source).read_bytes()
        snapshot = validate_balance_snapshot(json.loads(raw), config, clients)
        target = target_path(env, config, required=args.install)
        if args.install:
            atomic_install(raw, target)
        count = {'clients': len(snapshot['clients'])}
        count.update({field: sum(len(dataset[field]) for dataset in snapshot['clients'].values())
                      for field in ('invoices', 'contractor_invoices', 'hours', 'payments')})
        count['pending_work'] = sum(len(dataset.get('pending_work', []))
                                    for dataset in snapshot['clients'].values())
        print(json.dumps({'status': 'installed' if args.install else 'checked',
                          'owners': sorted(snapshot['clients']), 'count': count,
                          'sha256': hashlib.sha256(raw).hexdigest()}, sort_keys=True))
        return 0
    except (OSError, UnicodeError, ValueError, TimesheetError):
        # Keep paths, source values and environment details out of operator logs.
        print(json.dumps({'status': 'blocked'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
