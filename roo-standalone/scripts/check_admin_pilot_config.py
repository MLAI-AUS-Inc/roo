#!/usr/bin/env python3
"""Validate an Admin Roo env file against the restricted pilot approval."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roo.admin_pilot_config import admin_pilot_config_report
from roo.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.admin")
    parser.add_argument("--approval-manifest", required=True)
    parser.add_argument("--organization-domain", required=True)
    args = parser.parse_args()

    try:
        settings = Settings(_env_file=args.env_file)
    except ValidationError:
        print(
            json.dumps(
                {
                    "schema_version": "admin-roo-pilot-config-v1",
                    "ready": False,
                    "blockers": ["roo_configuration_invalid"],
                },
                sort_keys=True,
            )
        )
        return 1

    try:
        manifest = json.loads(
            Path(args.approval_manifest).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(
            json.dumps(
                {
                    "schema_version": "admin-roo-pilot-config-v1",
                    "ready": False,
                    "blockers": ["approval_manifest_unreadable"],
                },
                sort_keys=True,
            )
        )
        return 1

    report = admin_pilot_config_report(
        settings,
        manifest,
        organization_domain=args.organization_domain,
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
