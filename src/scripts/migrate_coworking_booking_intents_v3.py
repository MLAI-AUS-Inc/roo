from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roo.config import get_settings
from roo.coworking_booking_schema_v3 import migrate_coworking_booking_intents_v3


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the approved coworking booking intents v3 migration."
    )
    parser.add_argument("--database", help="Override COWORKING_INTENTS_DB_PATH")
    args = parser.parse_args()
    database = args.database or get_settings().COWORKING_INTENTS_DB_PATH
    quarantined = migrate_coworking_booking_intents_v3(database)
    print(
        "coworking booking intents schema v3 is ready; "
        f"quarantined_notifications={quarantined}"
    )


if __name__ == "__main__":
    main()
