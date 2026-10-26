#!/usr/bin/env python3
"""Run the content-free unified Admin route smoke gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roo.config import Settings
from roo.unified_admin_smoke import unified_admin_route_smoke_report


def blocked(code):
    print(json.dumps({
        "schema_version": "roo-unified-admin-route-smoke-v1",
        "ready": False,
        "blockers": [code],
    }, sort_keys=True))
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.admin")
    parser.add_argument("--approval-manifest", required=True)
    parser.add_argument("--organization-domain", required=True)
    parser.add_argument("--slack-team-id", required=True)
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=args.env_file)
    except ValidationError:
        return blocked("roo_configuration_invalid")
    try:
        manifest = json.loads(Path(args.approval_manifest).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return blocked("approval_manifest_unreadable")
    try:
        report = asyncio.run(unified_admin_route_smoke_report(
            settings,
            manifest,
            organization_domain=args.organization_domain,
            slack_team_id=args.slack_team_id,
            router_token=os.environ.get("UNIFIED_ROUTE_PROBE_TOKEN", ""),
        ))
    except Exception:
        return blocked("route_smoke_execution_failed")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
