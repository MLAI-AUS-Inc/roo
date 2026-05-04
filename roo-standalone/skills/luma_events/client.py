from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import httpx


class LumaEventsClient:
    """Client and CSV helpers for Luma event attendee exports."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://public-api.luma.com",
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.api_key = api_key
        self.base_url = (base_url or "https://public-api.luma.com").rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def get_recent_ended_events(
        self,
        count: int = 3,
        now: Optional[datetime] = None,
        timezone_name: str = "Australia/Melbourne",
    ) -> List[Dict[str, Any]]:
        """Return the latest ended calendar events, newest first."""
        now_local = now or datetime.now(ZoneInfo(timezone_name))
        if now_local.tzinfo is None:
            now_local = now_local.replace(tzinfo=ZoneInfo(timezone_name))
        now_utc = now_local.astimezone(timezone.utc)

        events: List[Dict[str, Any]] = []
        cursor = None
        while len(events) < count:
            params: Dict[str, Any] = {
                "before": _isoformat_z(now_utc),
                "pagination_limit": 100,
                "sort_column": "start_at",
                "sort_direction": "desc",
                "status": "approved",
            }
            if cursor:
                params["pagination_cursor"] = cursor

            page = await self._get("/v1/calendar/list-events", params=params)
            for event in page.get("entries", []):
                end_at = _parse_datetime(event.get("end_at"))
                if end_at and end_at <= now_utc:
                    events.append(event)
                    if len(events) >= count:
                        break

            if len(events) >= count or not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break

        return events[:count]

    async def list_guests(
        self,
        event_id: str,
        approval_status: str = "approved",
    ) -> List[Dict[str, Any]]:
        """Return all guests for an event and approval status."""
        params = {
            "event_id": event_id,
            "approval_status": approval_status,
            "pagination_limit": 100,
            "sort_column": "registered_at",
            "sort_direction": "asc",
        }
        return await self._paginate("/v1/event/get-guests", params=params)

    async def _paginate(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        cursor = None
        while True:
            page_params = dict(params)
            if cursor:
                page_params["pagination_cursor"] = cursor

            page = await self._get(path, params=page_params)
            entries.extend(page.get("entries", []))

            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break

        return entries

    async def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        headers = {"x-luma-api-key": self.api_key}
        client_kwargs: Dict[str, Any] = {
            "base_url": self.base_url,
            "headers": headers,
            "timeout": self.timeout,
        }
        if self.transport is not None:
            client_kwargs["transport"] = self.transport

        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    def build_attendee_csv(self, event: Dict[str, Any], guests: Iterable[Dict[str, Any]]) -> str:
        """Build a CSV string with one row per guest and dynamic registration columns."""
        rows: List[Dict[str, Any]] = []
        question_headers: List[str] = []
        seen_questions = set()

        for guest in guests:
            row = self._guest_to_row(event, guest)
            for header in row:
                if header.startswith("question: ") and header not in seen_questions:
                    seen_questions.add(header)
                    question_headers.append(header)
            rows.append(row)

        headers = [
            "event_id",
            "event_name",
            "event_url",
            "event_start_at",
            "event_end_at",
            "guest_id",
            "user_id",
            "name",
            "first_name",
            "last_name",
            "email",
            "phone_number",
            "approval_status",
            "registered_at",
            "checked_in_at",
            "ticket_count",
            "ticket_names",
            "ticket_ids",
            "ticket_checked_in_at",
            "utm_source",
            "custom_source",
            "check_in_qr_code",
        ] + question_headers

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return output.getvalue()

    def build_csv_filename(self, event: Dict[str, Any]) -> str:
        start_at = _parse_datetime(event.get("start_at"))
        date_label = start_at.date().isoformat() if start_at else "unknown-date"
        slug = _slugify(str(event.get("name") or "event"))
        return f"luma-mlai-{date_label}-{slug}.csv"

    def _guest_to_row(self, event: Dict[str, Any], guest: Dict[str, Any]) -> Dict[str, Any]:
        guest_data = guest.get("guest") if isinstance(guest.get("guest"), dict) else guest
        tickets = _tickets_for_guest(guest_data)
        checked_in_values = [
            str(ticket.get("checked_in_at") or "").strip()
            for ticket in tickets
            if str(ticket.get("checked_in_at") or "").strip()
        ]
        checked_in_at = "; ".join(checked_in_values) or str(guest_data.get("checked_in_at") or "")

        row: Dict[str, Any] = {
            "event_id": event.get("id", ""),
            "event_name": event.get("name", ""),
            "event_url": event.get("url", ""),
            "event_start_at": event.get("start_at", ""),
            "event_end_at": event.get("end_at", ""),
            "guest_id": guest_data.get("id", ""),
            "user_id": guest_data.get("user_id", ""),
            "name": guest_data.get("user_name") or "",
            "first_name": guest_data.get("user_first_name") or "",
            "last_name": guest_data.get("user_last_name") or "",
            "email": guest_data.get("user_email") or "",
            "phone_number": guest_data.get("phone_number") or "",
            "approval_status": guest_data.get("approval_status") or "",
            "registered_at": guest_data.get("registered_at") or "",
            "checked_in_at": checked_in_at,
            "ticket_count": len(tickets),
            "ticket_names": "; ".join(_clean_string(ticket.get("name")) for ticket in tickets if _clean_string(ticket.get("name"))),
            "ticket_ids": "; ".join(_clean_string(ticket.get("id")) for ticket in tickets if _clean_string(ticket.get("id"))),
            "ticket_checked_in_at": "; ".join(checked_in_values),
            "utm_source": guest_data.get("utm_source") or "",
            "custom_source": guest_data.get("custom_source") or "",
            "check_in_qr_code": guest_data.get("check_in_qr_code") or "",
        }

        for answer in guest_data.get("registration_answers") or []:
            if not isinstance(answer, dict):
                continue
            label = _clean_string(answer.get("label")) or _clean_string(answer.get("question_id"))
            if not label:
                continue
            row[f"question: {label}"] = _answer_value(answer)

        return row


def _tickets_for_guest(guest: Dict[str, Any]) -> List[Dict[str, Any]]:
    tickets = guest.get("event_tickets")
    if isinstance(tickets, list):
        return [ticket for ticket in tickets if isinstance(ticket, dict)]
    ticket = guest.get("event_ticket")
    if isinstance(ticket, dict):
        return [ticket]
    return []


def _answer_value(answer: Dict[str, Any]) -> str:
    if answer.get("question_type") == "company":
        company = _clean_string(answer.get("answer_company") or answer.get("value") or answer.get("answer"))
        job_title = _clean_string(answer.get("answer_job_title"))
        return " - ".join(part for part in [company, job_title] if part)

    value = answer.get("answer")
    if value is None:
        value = answer.get("value")
    return _stringify_csv_value(value)


def _stringify_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "; ".join(_stringify_csv_value(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return (slug or "event")[:80].strip("-") or "event"
