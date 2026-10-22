#!/usr/bin/env python3
"""Run the aggregate-only Admin Roo signed-request smoke gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roo.admin_pilot_smoke import admin_pilot_signed_smoke_report
from roo.config import Settings


def _blocked(code: str) -> int:
    print(
        json.dumps(
            {
                "schema_version": "admin-roo-pilot-signed-smoke-v1",
                "ready": False,
                "blockers": [code],
            },
            sort_keys=True,
        )
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.admin")
    parser.add_argument("--approval-manifest", required=True)
    parser.add_argument("--organization-domain", required=True)
    parser.add_argument("--slack-team-id", required=True)
    args = parser.parse_args()

    try:
        settings = Settings(_env_file=args.env_file)
    except ValidationError:
        return _blocked("roo_configuration_invalid")
    try:
        manifest = json.loads(
            Path(args.approval_manifest).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _blocked("approval_manifest_unreadable")

    try:
        report = asyncio.run(
            admin_pilot_signed_smoke_report(
                settings,
                manifest,
                organization_domain=args.organization_domain,
                slack_team_id=args.slack_team_id,
            )
        )
    except Exception:
        return _blocked("signed_smoke_execution_failed")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
