from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roo.config import get_settings
from roo.coworking_booking_schema_v2 import migrate_coworking_booking_intents_v2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the one-shot coworking booking intents v2 migration."
    )
    parser.add_argument("--database", help="Override COWORKING_INTENTS_DB_PATH")
    args = parser.parse_args()
    database = args.database or get_settings().COWORKING_INTENTS_DB_PATH
    migrate_coworking_booking_intents_v2(database)
    print("coworking booking intents schema v2 is ready")


if __name__ == "__main__":
    main()
