#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


MENTION_RE = re.compile(r"ROO MENTION:\s+from\s+(?P<user>\S+)\s+in\s+(?P<channel>\S+)")
TEXT_RE = re.compile(r"Text:\s*(?P<text>.*)")
PARAMS_RE = re.compile(r"Extracted params:\s*(?P<params>\{.*\})")
POST_RE = re.compile(r"Message posted to\s+(?P<channel>\S+)(?:\s+\(thread:\s+(?P<thread>[^)]+)\))?")
BOOKING_ENDPOINT = "/api/v1/points/coworking/book/"
FAILURE_MARKERS = (
    "MLAIBackendUnavailableError",
    "MLAI backend timed out",
    "MLAI backend is unavailable",
    "ReadTimeout",
    "request_failed",
    "circuit_breaker_open",
)


@dataclass
class ReplayCandidate:
    slack_user_id: str
    channel_id: str
    thread_ts: str
    booking_date: str
    source_line: int
    text: str
    idempotency_key: str


@dataclass
class QuarantinedCandidate:
    reason: str
    source_line: int
    slack_user_id: str = ""
    channel_id: str = ""
    thread_ts: str = ""
    booking_date: str = ""
    text: str = ""


class _CurrentMention:
    def __init__(self, line_number: int, slack_user_id: str, channel_id: str):
        self.line_number = line_number
        self.slack_user_id = slack_user_id
        self.channel_id = channel_id
        self.thread_ts = ""
        self.text = ""
        self.params: dict[str, Any] = {}
        self.saw_booking_failure = False


def _resolve_booking_date(raw_date: Any, *, incident_local_date: date) -> str:
    value = str(raw_date or "").strip().lower()
    if not value or value == "today":
        return incident_local_date.isoformat()
    if value == "tomorrow":
        return (incident_local_date + timedelta(days=1)).isoformat()
    if value == "yesterday":
        return (incident_local_date - timedelta(days=1)).isoformat()
    return date.fromisoformat(value).isoformat()


def _quarantine_current(current: _CurrentMention, reason: str) -> QuarantinedCandidate:
    booking_date = ""
    try:
        booking_date = str(current.params.get("date") or "")
    except Exception:
        booking_date = ""
    return QuarantinedCandidate(
        reason=reason,
        source_line=current.line_number,
        slack_user_id=current.slack_user_id,
        channel_id=current.channel_id,
        thread_ts=current.thread_ts,
        booking_date=booking_date,
        text=current.text,
    )


def _candidate_from_current(
    current: _CurrentMention,
    *,
    incident_local_date: date,
    include_successful: bool,
) -> tuple[ReplayCandidate | None, QuarantinedCandidate | None]:
    action = str(current.params.get("action") or "").strip()
    if action != "book_coworking":
        return None, None

    if not current.saw_booking_failure and not include_successful:
        return None, None

    if not current.slack_user_id:
        return None, _quarantine_current(current, "missing_slack_user_id")
    if not current.channel_id:
        return None, _quarantine_current(current, "missing_channel_id")
    if not current.thread_ts:
        return None, _quarantine_current(current, "missing_thread_ts_for_slack_confirmation")

    try:
        booking_date = _resolve_booking_date(
            current.params.get("date"),
            incident_local_date=incident_local_date,
        )
    except Exception:
        return None, _quarantine_current(current, "invalid_booking_date")

    idempotency_key = f"replay_coworking:{current.slack_user_id}:{booking_date}"
    return (
        ReplayCandidate(
            slack_user_id=current.slack_user_id,
            channel_id=current.channel_id,
            thread_ts=current.thread_ts,
            booking_date=booking_date,
            source_line=current.line_number,
            text=current.text,
            idempotency_key=idempotency_key,
        ),
        None,
    )


def parse_failed_coworking_log(
    lines: list[str],
    *,
    incident_local_date: date,
    include_successful: bool = False,
) -> tuple[list[ReplayCandidate], list[QuarantinedCandidate]]:
    current: _CurrentMention | None = None
    valid_by_user_date: dict[tuple[str, str], ReplayCandidate] = {}
    quarantined: list[QuarantinedCandidate] = []

    def flush_current() -> None:
        nonlocal current
        if current is None:
            return
        candidate, quarantine = _candidate_from_current(
            current,
            incident_local_date=incident_local_date,
            include_successful=include_successful,
        )
        if quarantine is not None:
            quarantined.append(quarantine)
        if candidate is not None:
            valid_by_user_date[(candidate.slack_user_id, candidate.booking_date)] = candidate
        current = None

    for line_number, line in enumerate(lines, start=1):
        mention_match = MENTION_RE.search(line)
        if mention_match:
            flush_current()
            current = _CurrentMention(
                line_number=line_number,
                slack_user_id=mention_match.group("user"),
                channel_id=mention_match.group("channel"),
            )
            continue

        if current is None:
            continue

        text_match = TEXT_RE.search(line)
        if text_match:
            current.text = text_match.group("text").strip()
            continue

        params_match = PARAMS_RE.search(line)
        if params_match:
            try:
                params = ast.literal_eval(params_match.group("params"))
                current.params = params if isinstance(params, dict) else {}
            except Exception:
                quarantined.append(_quarantine_current(current, "invalid_params_literal"))
            continue

        post_match = POST_RE.search(line)
        if post_match and post_match.group("thread"):
            current.thread_ts = post_match.group("thread").strip()

        if BOOKING_ENDPOINT in line or any(marker in line for marker in FAILURE_MARKERS):
            current.saw_booking_failure = True

    flush_current()
    valid = sorted(valid_by_user_date.values(), key=lambda item: item.source_line)
    return valid, quarantined


def _has_booking_for_date(bookings: list[dict[str, Any]], booking_date: str) -> bool:
    for booking in bookings:
        if str(booking.get("date") or "") == booking_date and str(booking.get("status") or "booked") == "booked":
            return True
    return False


async def execute_replay(
    candidates: list[ReplayCandidate],
    *,
    replay_run_id: str,
    summary_channel_id: str,
) -> list[dict[str, Any]]:
    from roo.clients.mlai_backend import MLAIBackendClient
    from roo.config import get_settings
    from roo.slack_client import post_message

    settings = get_settings()
    client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
        internal_api_key=settings.INTERNAL_API_KEY or settings.ROO_API_KEY or settings.MLAI_API_KEY,
    )
    results: list[dict[str, Any]] = []

    for candidate in candidates:
        request_id = f"{replay_run_id}:{candidate.slack_user_id}:{candidate.booking_date}"
        result: dict[str, Any] = {
            **asdict(candidate),
            "replay_run_id": replay_run_id,
            "request_id": request_id,
        }
        try:
            bookings = await client.get_my_bookings(candidate.slack_user_id)
            if _has_booking_for_date(bookings, candidate.booking_date):
                result["status"] = "already_booked"
                result["backend_result"] = {"already_booked": True}
                post_message(
                    channel=candidate.channel_id,
                    thread_ts=candidate.thread_ts,
                    text=(
                        "I checked the incident replay: your coworking booking "
                        f"for {candidate.booking_date} is already confirmed."
                    ),
                )
            else:
                # A missing active booking is ambiguous: the original request
                # may never have committed, or it may have committed and then
                # been intentionally cancelled/refunded. The legacy log has no
                # durable operation identity that can distinguish those cases.
                # Never turn incident recovery into a new booking or charge.
                result["status"] = "manual_reconciliation_required"
                post_message(
                    channel=candidate.channel_id,
                    thread_ts=candidate.thread_ts,
                    text=(
                        "I checked the earlier timed-out coworking request for "
                        f"{candidate.booking_date}, but there is no active booking. "
                        "I did not create a new booking or charge any Roo Points; "
                        "support must verify the historical lifecycle first."
                    ),
                )
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = f"{exc.__class__.__name__}: {exc}"
            post_message(
                channel=candidate.channel_id,
                thread_ts=candidate.thread_ts,
                text=(
                    "I tried to replay the coworking booking that timed out earlier, "
                    f"but it still failed. replay_run_id={replay_run_id}"
                ),
            )

        print(json.dumps(result, sort_keys=True))
        results.append(result)

    if summary_channel_id:
        already_booked = sum(1 for item in results if item.get("status") == "already_booked")
        manual = sum(
            1
            for item in results
            if item.get("status") == "manual_reconciliation_required"
        )
        failed = sum(1 for item in results if item.get("status") == "failed")
        post_message(
            channel=summary_channel_id,
            text=(
                "Roo is back and processed the coworking booking incident replay. "
                f"replay_run_id={replay_run_id} already_booked={already_booked} "
                f"manual_reconciliation_required={manual} failed={failed}"
            ),
        )

    return results


def build_manifest(
    *,
    replay_run_id: str,
    valid: list[ReplayCandidate],
    quarantined: list[QuarantinedCandidate],
    execute: bool,
) -> dict[str, Any]:
    return {
        "replay_run_id": replay_run_id,
        "dry_run": not execute,
        "db_constraint_guard": "unique_active_booking_per_user_date",
        "valid_count": len(valid),
        "quarantined_count": len(quarantined),
        "valid": [asdict(candidate) for candidate in valid],
        "quarantined": [asdict(candidate) for candidate in quarantined],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit legacy failed Roo coworking requests. Missing bookings are "
            "quarantined for manual reconciliation and are never recreated."
        )
    )
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("--incident-id", default="coworking-timeout-2026-04-22")
    parser.add_argument("--incident-local-date", default=date.today().isoformat())
    parser.add_argument("--summary-channel-id", default="C09L4DMMU5N")
    parser.add_argument("--include-successful", action="store_true")
    parser.add_argument(
        "--constraint-verified",
        action="store_true",
        help="Required with --execute before querying live booking state.",
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    if args.execute and not args.constraint_verified:
        parser.error(
            "--execute requires --constraint-verified after applying and validating "
            "the unique_active_booking_per_user_date DB constraint. Execution "
            "only audits live state; it no longer creates bookings."
        )

    incident_local_date = date.fromisoformat(args.incident_local_date)
    replay_run_id = f"{args.incident_id}:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    lines = args.log_file.read_text().splitlines()
    valid, quarantined = parse_failed_coworking_log(
        lines,
        incident_local_date=incident_local_date,
        include_successful=args.include_successful,
    )
    manifest = build_manifest(
        replay_run_id=replay_run_id,
        valid=valid,
        quarantined=quarantined,
        execute=args.execute,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))

    if not args.execute:
        return 0

    asyncio.run(
        execute_replay(
            valid,
            replay_run_id=replay_run_id,
            summary_channel_id=args.summary_channel_id,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
