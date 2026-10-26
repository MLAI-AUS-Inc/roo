from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roo.config import get_settings
from roo.coworking_notification_reconciliation import (
    reconcile_coworking_notification,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve one quarantined coworking notification with audit evidence."
    )
    parser.add_argument("--database", help="Override COWORKING_INTENTS_DB_PATH")
    parser.add_argument("--intent-id", required=True, type=int)
    parser.add_argument(
        "--outcome",
        required=True,
        choices=("delivered", "not-required", "retry"),
    )
    parser.add_argument(
        "--operator-reference",
        required=True,
        help="Non-secret incident, ticket, or evidence reference",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Required acknowledgement that this command changes durable state",
    )
    args = parser.parse_args()
    if not args.apply:
        parser.error("--apply is required")
    database = args.database or get_settings().COWORKING_INTENTS_DB_PATH
    reconciled = reconcile_coworking_notification(
        database,
        intent_id=args.intent_id,
        outcome=args.outcome,
        operator_reference=args.operator_reference,
    )
    print(
        json.dumps(
            {
                "intent_id": reconciled["id"],
                "notification_status": reconciled["notification_status"],
                "notification_reconciliation_outcome": reconciled[
                    "notification_reconciliation_outcome"
                ],
                "notification_reconciliation_reference": reconciled[
                    "notification_reconciliation_reference"
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
