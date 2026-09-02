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

        return self.claim_lease(
            fingerprint,
            now=now,
            lease_seconds=ttl_seconds,
        ) is not None

    def claim_lease(
        self,
        fingerprint: str,
        *,
        now: Optional[float] = None,
        lease_seconds: int = 45,
    ) -> Optional[float]:
        """Claim processing ownership and return its fencing timestamp.

        An unfinished event becomes reclaimable when this short lease expires.
        Completion separately extends the same row to the normal dedupe TTL.
        """

        disposition, claim_token = self.claim_event(
            fingerprint,
            now=now,
            lease_seconds=lease_seconds,
        )
        return claim_token if disposition == "claimed" else None

    def claim_event(
        self,
        fingerprint: str,
        *,
        now: Optional[float] = None,
        lease_seconds: int = 45,
    ) -> tuple[str, Optional[float]]:
        """Return ``claimed``, ``processing``, or ``completed`` for an event.

        Processing rows store a negative fencing timestamp. This distinguishes
        work that still needs a retryable HTTP response from completed receipts
        without changing the existing SQLite schema.
        """

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not fingerprint or len(fingerprint) != 64:
            raise ValueError("fingerprint must be a SHA-256 hex digest")

        self._initialise()
        current_time = time.time() if now is None else float(now)
        claim_token = current_time
        expires_at = current_time + lease_seconds
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
                (fingerprint, -claim_token, expires_at),
            )
            if cursor.rowcount == 1:
                connection.commit()
                return "claimed", claim_token
            row = connection.execute(
                """
                SELECT received_at
                FROM slack_request_receipts
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
            connection.commit()
            if row is not None and float(row[0]) < 0:
                return "processing", None
            return "completed", None

    def renew(
        self,
        fingerprint: str,
        *,
        claim_token: float,
        lease_seconds: int,
        now: Optional[float] = None,
    ) -> bool:
        """Extend only the caller's fenced processing lease."""

        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        current_time = time.time() if now is None else float(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE slack_request_receipts
                SET expires_at = ?
                WHERE fingerprint = ? AND received_at = ?
                """,
                (
                    current_time + lease_seconds,
                    fingerprint,
                    -float(claim_token),
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def complete(
        self,
        fingerprint: str,
        *,
        claim_token: float,
        ttl_seconds: int,
        now: Optional[float] = None,
    ) -> bool:
        """Mark the fenced claim complete for the normal retry-suppression TTL."""

        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current_time = time.time() if now is None else float(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE slack_request_receipts
                SET received_at = ?, expires_at = ?
                WHERE fingerprint = ? AND received_at = ?
                """,
                (
                    float(claim_token),
                    current_time + ttl_seconds,
                    fingerprint,
                    -float(claim_token),
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def release(self, fingerprint: str, *, claim_token: float) -> bool:
        """Release only the caller's fenced claim so Slack can retry promptly."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM slack_request_receipts
                WHERE fingerprint = ? AND received_at = ?
                """,
                (fingerprint, -float(claim_token)),
            )
            connection.commit()
            return cursor.rowcount == 1


@lru_cache(maxsize=8)
def get_slack_receipt_store(database_path: str) -> SlackRequestReceiptStore:
    return SlackRequestReceiptStore(database_path)


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
    return not claimed
