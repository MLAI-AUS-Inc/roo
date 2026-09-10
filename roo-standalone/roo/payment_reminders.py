"""Deterministic builder payment reminders; no ticket or payment mutations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Builder:
    slack_user_id: str
    linear_user_id: str


@dataclass(frozen=True)
class ReminderConfig:
    enabled: bool
    first_payment_date: date
    timezone: ZoneInfo
    reminder_hour: int
    slack_team_id: str
    linear_organization_id: str
    builders: tuple[Builder, ...]
    receipts_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ReminderConfig:
        enabled = env.get("PAYMENT_REMINDERS_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("PAYMENT_REMINDERS_ENABLED must be true or false")
        first = date.fromisoformat(env.get("PAYMENT_FIRST_FRIDAY", "2026-09-11"))
        if first.weekday() != 4:
            raise ValueError("PAYMENT_FIRST_FRIDAY must be a Friday")
        hour = int(env.get("PAYMENT_REMINDER_HOUR", "9"))
        if not 0 <= hour <= 23:
            raise ValueError("PAYMENT_REMINDER_HOUR must be between 0 and 23")
        raw = json.loads(env.get("PAYMENT_BUILDERS_JSON", "[]"))
        if not isinstance(raw, list):
            raise ValueError("PAYMENT_BUILDERS_JSON must be a list")
        builders = []
        for item in raw:
            if not isinstance(item, dict) or set(item) != {"slack_user_id", "linear_user_id"}:
                raise ValueError("Each builder needs slack_user_id and linear_user_id")
            slack_id = item["slack_user_id"]
            if not isinstance(slack_id, str) or not re.fullmatch(r"[UW][A-Z0-9]+", slack_id):
                raise ValueError("Invalid builder Slack user ID")
            builders.append(Builder(slack_id, str(UUID(item["linear_user_id"]))))
        if (len({b.slack_user_id for b in builders}) != len(builders)
                or len({b.linear_user_id for b in builders}) != len(builders)):
            raise ValueError("Builder mappings must be one-to-one")
        team = env.get("PAYMENT_SLACK_TEAM_ID", "")
        organization = env.get("PAYMENT_LINEAR_ORGANIZATION_ID", "")
        if enabled == "true":
            if not builders or not re.fullmatch(r"T[A-Z0-9]+", team):
                raise ValueError("Enabled reminders require builders and PAYMENT_SLACK_TEAM_ID")
            organization = str(UUID(organization))
        return cls(
            enabled == "true", first,
            ZoneInfo(env.get("PAYMENT_TIMEZONE", "Australia/Melbourne")),
            hour, team, organization, tuple(builders),
            Path(env.get("PAYMENT_RECEIPTS_DIR", "/app/data/payment-reminders")),
        )

    def due_friday(self, now: datetime) -> date | None:
        """Thursday reminder delivery window for Friday payments."""
        if not self.enabled or now.tzinfo is None:
            return None
        local = now.astimezone(self.timezone)
        if local.weekday() != 3 or local.hour < self.reminder_hour:
            return None
        friday = local.date() + timedelta(days=1)
        days = (friday - self.first_payment_date).days
        return friday if days >= 0 and days % 14 == 0 else None


def _escape(value: str) -> str:
    return " ".join(value.split()).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_messages(issues: list[dict], *, first_payment: bool) -> list[str]:
    """Keep every issue, including older/backlog work, without Slack truncation."""
    if not issues:
        return []
    opening = "First payments are this Friday!" if first_payment else "Payments are this Friday!"
    intro = (
        f"*Payment reminder:* {opening} Please get all your hours in and mark your "
        "completed Linear tasks as done Friday by 12pm (noon) so your completed work is included "
        "in this payment run.\n\n"
        f"*Your tasks still open in Linear ({len(issues)}):*\n"
    )
    messages = [intro]
    continuation = "*Your open Linear tasks (continued):*\n"
    for issue in sorted(issues, key=lambda item: item["identifier"]):
        url = issue["url"]
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "linear.app"
                or "/issue/" not in parsed.path or re.search(r"[\s<>|]", url)
                or len(url) > 2000):
            raise ValueError("Invalid Linear issue URL")
        identifier = issue["identifier"]
        if len(identifier) > 100 or not re.fullmatch(r"[A-Za-z0-9_]+-\d+", identifier):
            raise ValueError("Invalid Linear issue identifier")
        # The link uses the short ID; even unusually long titles can be split
        # without breaking the link or silently dropping title text.
        line = f"• <{url}|{identifier}> — {_escape(issue['title'])} · {_escape(issue['state']['name'])}\n"
        if len(messages[-1]) + len(line) > 3800:
            messages.append(continuation)
        while line:
            room = 3800 - len(messages[-1])
            messages[-1] += line[:room]
            line = line[room:]
            if line:
                messages.append(continuation)
    footer = "\nPlease review these and mark any you’ve already completed as done."
    if len(messages[-1]) + len(footer) > 3800:
        messages.append(footer.strip())
    else:
        messages[-1] += footer
    return messages


class ReceiptStore:
    """Atomic files on a persistent local volume; no application DB migration.

    A process lock covers each builder's entire delivery. A persisted 'sending'
    part is never automatically retried: after a crash or ambiguous network
    result an operator must check Slack before deciding whether to retry it.
    """

    def __init__(self, directory: Path):
        self.directory = directory

    @contextmanager
    def locked(self, key: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError("Invalid receipt key")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / f"{key}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self, key: str) -> dict | None:
        path = self.directory / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def write(self, key: str, receipt: dict) -> None:
        temporary = self.directory / f"{key}.tmp"
        with temporary.open("w") as handle:
            os.chmod(temporary, 0o600)
            json.dump(receipt, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.directory / f"{key}.json")
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


class DeliveryRejected(Exception):
    """Definite non-delivery; only outcomes with retry_after retry unaided."""

    def __init__(self, code: str, retry_after: int | None = None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(code)


class DeliveryNotSent(DeliveryRejected):
    """The transport could not acquire/connect a socket; Slack received no request."""

    def __init__(self):
        super().__init__("not_sent", retry_after=60)


def run_reminders(config, api, store, now, *, dry_run=False, clock=None):
    """One scheduler tick. Transport methods are injected for offline testing."""
    friday = config.due_friday(now)
    if friday is None:
        return {"status": "not_due", "builders": []}
    api.verify_workspace(config)
    results = []
    for builder in config.builders:
        key = f"{config.slack_team_id}_{friday.isoformat()}_{builder.slack_user_id}"
        try:
            if dry_run:
                api.verify_builder(builder, config.slack_team_id)
                issues = api.open_issues(builder.linear_user_id)
                results.append({"builder": builder.slack_user_id, "status": "preview",
                                "messages": render_messages(issues, first_payment=friday == config.first_payment_date)})
                continue
            with store.locked(key) as acquired:
                if not acquired:
                    results.append({"builder": builder.slack_user_id, "status": "busy"})
                    continue
                receipt = store.read(key)
                if receipt and receipt["linear_user_id"] != builder.linear_user_id:
                    raise ValueError("Builder mapping changed after preparation")
                if receipt and receipt["status"] in {"sent", "empty"}:
                    results.append({"builder": builder.slack_user_id, "status": receipt["status"]})
                    continue
                if receipt and any(p["status"] in {"sending", "failed"} for p in receipt["parts"]):
                    results.append({"builder": builder.slack_user_id, "status": "needs_review"})
                    continue
                api.verify_builder(builder, config.slack_team_id)
                if receipt is None:
                    issues = api.open_issues(builder.linear_user_id)
                    messages = render_messages(issues, first_payment=friday == config.first_payment_date)
                    receipt = {"linear_user_id": builder.linear_user_id,
                               "status": "pending" if messages else "empty",
                               "parts": [{"text": text, "status": "pending"} for text in messages]}
                    store.write(key, receipt)
                if receipt["status"] == "empty":
                    results.append({"builder": builder.slack_user_id, "status": "empty"})
                    continue
                channel = api.open_dm(builder.slack_user_id)
                for part in receipt["parts"]:
                    if part["status"] == "sent":
                        continue
                    current = clock() if clock else now
                    if config.due_friday(current) != friday:
                        break
                    if current.timestamp() < part.get("retry_at", 0):
                        break
                    part["status"] = "sending"
                    store.write(key, receipt)
                    try:
                        part["ts"] = api.post_message(channel, part["text"])
                    except DeliveryRejected as exc:
                        part["status"] = "pending" if exc.retry_after is not None else "failed"
                        if exc.retry_after is not None:
                            rejected_at = clock() if clock else now
                            part["retry_at"] = rejected_at.timestamp() + max(60, exc.retry_after)
                        store.write(key, receipt)
                        break
                    part["status"] = "sent"
                    store.write(key, receipt)
                if all(p["status"] == "sent" for p in receipt["parts"]):
                    receipt["status"] = "sent"
                    store.write(key, receipt)
                status = "needs_review" if any(p["status"] == "failed" for p in receipt["parts"]) else receipt["status"]
                retry_at = max((p.get("retry_at", 0) for p in receipt["parts"]
                                if p["status"] == "pending"), default=0)
                retry_after = retry_at - (clock() if clock else now).timestamp() if retry_at else 0
                results.append({"builder": builder.slack_user_id, "status": status,
                                "retry_after": max(0, retry_after)})
                if retry_after > 0:
                    break
        except DeliveryRejected as exc:
            results.append({"builder": builder.slack_user_id, "status": "error",
                            "error_type": type(exc).__name__, "retry_after": exc.retry_after or 60})
            if exc.retry_after is not None:
                break
        except Exception as exc:
            # Never log API bodies, tokens, or task text. Other builders continue.
            results.append({"builder": builder.slack_user_id, "status": "error",
                            "error_type": type(exc).__name__})
    return {"status": "checked", "payment_date": friday.isoformat(), "builders": results}
