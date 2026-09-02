"""Slack request authentication and durable retry suppression."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional


class SlackRequestVerificationError(ValueError):
    """Raised when Slack request authentication fails."""


def verify_slack_request(
    *,
    signing_secret: str,
    timestamp: str,
    signature: str,
    raw_body: bytes,
    now: Optional[float] = None,
    max_age_seconds: int = 300,
) -> str:
    """Verify Slack's v0 HMAC over the exact raw request body.

    Returns a body-bound fingerprint suitable for retry deduplication. The raw
    body is never logged or stored.
    """

    secret = str(signing_secret or "")
    timestamp = str(timestamp or "").strip()
    signature = str(signature or "").strip().lower()
    if not secret:
        raise SlackRequestVerificationError("Slack signing secret is unavailable")
    if not timestamp or not signature:
        raise SlackRequestVerificationError("Missing Slack signature headers")
    if not signature.startswith("v0=") or len(signature) != 67:
        raise SlackRequestVerificationError("Invalid Slack signature format")

    try:
        request_timestamp = int(timestamp)
    except ValueError as exc:
        raise SlackRequestVerificationError("Invalid Slack request timestamp") from exc

    current_time = time.time() if now is None else float(now)
    if abs(current_time - request_timestamp) > max_age_seconds:
        raise SlackRequestVerificationError("Slack request timestamp is outside the replay window")

    base_string = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    expected = "v0=" + hmac.new(
        secret.encode("utf-8"),
        base_string,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise SlackRequestVerificationError("Slack request signature does not match")

    return hashlib.sha256(
        timestamp.encode("ascii") + b":" + signature.encode("ascii") + b":" + raw_body
    ).hexdigest()


class SlackRequestReceiptStore:
    """SQLite-backed request receipts shared by workers and process restarts."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self._initialised = False
        self._initialise_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.database_path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialise(self) -> None:
        if self._initialised:
            return
        with self._initialise_lock:
            if self._initialised:
                return
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS slack_request_receipts (
                        fingerprint TEXT PRIMARY KEY,
                        received_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS slack_request_receipts_expiry_idx
                    ON slack_request_receipts (expires_at)
                    """
                )
            self._initialised = True

    def claim(
        self,
        fingerprint: str,
        *,
        now: Optional[float] = None,
        ttl_seconds: int = 600,
    ) -> bool:
        """Return true only for the first unexpired receipt of a fingerprint."""

        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not fingerprint or len(fingerprint) != 64:
            raise ValueError("fingerprint must be a SHA-256 hex digest")

        self._initialise()
        current_time = time.time() if now is None else float(now)
        expires_at = current_time + ttl_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM slack_request_receipts WHERE expires_at <= ?",
                (current_time,),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO slack_request_receipts
                    (fingerprint, received_at, expires_at)
                VALUES (?, ?, ?)
                """,
                (fingerprint, current_time, expires_at),
            )
            connection.commit()
            return cursor.rowcount == 1


@lru_cache(maxsize=8)
def get_slack_receipt_store(database_path: str) -> SlackRequestReceiptStore:
    return SlackRequestReceiptStore(database_path)


def verify_and_claim_slack_request_with_fingerprint(
    *,
    headers: Mapping[str, str],
    raw_body: bytes,
    signing_secret: str,
    receipt_db_path: str,
    max_age_seconds: int = 300,
    receipt_ttl_seconds: int = 600,
    now: Optional[float] = None,
) -> tuple[bool, str]:
    """Verify a request and return its duplicate state and stable fingerprint."""

    fingerprint = verify_slack_request(
        signing_secret=signing_secret,
        timestamp=headers.get("X-Slack-Request-Timestamp", ""),
        signature=headers.get("X-Slack-Signature", ""),
        raw_body=raw_body,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    claimed = get_slack_receipt_store(str(receipt_db_path)).claim(
        fingerprint,
        now=now,
        ttl_seconds=receipt_ttl_seconds,
    )
    return not claimed, fingerprint


def verify_and_claim_slack_request(
    *,
    headers: Mapping[str, str],
    raw_body: bytes,
    signing_secret: str,
    receipt_db_path: str,
    max_age_seconds: int = 300,
    receipt_ttl_seconds: int = 600,
    now: Optional[float] = None,
) -> bool:
    """Verify a request and return true when it is a previously seen retry."""

    duplicate, _fingerprint = verify_and_claim_slack_request_with_fingerprint(
        headers=headers,
        raw_body=raw_body,
        signing_secret=signing_secret,
        receipt_db_path=receipt_db_path,
        max_age_seconds=max_age_seconds,
        receipt_ttl_seconds=receipt_ttl_seconds,
        now=now,
    )
    return duplicate
