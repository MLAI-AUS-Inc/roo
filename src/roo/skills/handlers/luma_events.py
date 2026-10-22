"""Luma events skill execution."""
from __future__ import annotations
from ..contracts import SkillRequest
from typing import Any, Optional
from ...clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError
from ..loader import Skill
import base64
import calendar
from datetime import date, datetime
from ...config import get_settings
import httpx
import re
from ...slack_client import upload_file
from .backend_errors import BackendErrors


class LumaEventsHandler(BackendErrors):
    async def execute(self, request: SkillRequest):
        skill, text, params, user_id = request.skill, request.text, request.params, request.user_id
        channel_id, thread_ts = request.channel_id, request.thread_ts
        thread_history, kwargs = request.thread_history, request.metadata
        result = await self._execute_luma_events(skill, text, params, user_id, channel_id, thread_ts)
        return result

    async def _execute_luma_events(
        self,
        skill: Skill,
        text: str,
        params: dict,
        user_id: str,
        channel_id: Optional[str],
        thread_ts: Optional[str],
    ) -> Any:
        """Execute the Luma Events skill through mlai-backend."""
        settings = get_settings()
        if not getattr(settings, "MLAI_BACKEND_URL", None):
            return (
                "Luma attendee reports need mlai-backend to be configured. "
                "Ask the team to set `MLAI_BACKEND_URL`."
            )

        from roo.clients.mlai_backend import (
            MLAIBackendClient,
            MLAIBackendUnavailableError,
        )

        backend_client = MLAIBackendClient(
            base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY or settings.MLAI_API_KEY,
            internal_api_key=(
                settings.INTERNAL_API_KEY
                or settings.ROO_API_KEY
                or settings.MLAI_API_KEY
            ),
        )

        event_date = self._resolve_luma_event_date(params, text)
        event_count = self._resolve_luma_event_count(params, text, default=1 if event_date else 3)
        include_csv = self._luma_request_includes_csv(params, text)
        approval_status = str(params.get("approval_status") or "approved").strip() or "approved"
        if include_csv and not channel_id:
            return "I need a Slack channel or DM to upload the Luma CSV files."

        try:
            report = await backend_client.get_luma_attendee_report(
                user_id,
                event_count=event_count,
                event_date=event_date,
                approval_status=approval_status,
                include_csv=include_csv,
            )
        except MLAIBackendUnavailableError:
            return "I'm having trouble reaching mlai-backend for Luma right now. Try again in a tick."
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            detail = self._extract_luma_http_error_detail(e)
            if status_code == 403:
                return (
                    "Sorry mate, you'll need to be a Points Admin with the `admin`, "
                    "`committee`, or `partner` role to access Luma attendee data. 🔒"
                )
            if status_code == 503:
                return detail or "Luma isn't configured in mlai-backend yet. Ask the team to set `LUMA_API_KEY` there."
            if status_code == 429:
                return detail or "Luma rate-limited the report. Try again in a minute."
            return detail or f"mlai-backend returned HTTP {status_code} for the Luma report."

        events = report.get("events") or []
        if not events:
            if event_date:
                return f"I couldn't find an ended Luma event on {event_date}."
            return "I couldn't find any ended Luma events on the configured MLAI calendar."

        uploaded = []
        if include_csv:
            from ...slack_client import upload_file

            for event in events:
                csv_payload = event.get("csv") if isinstance(event, dict) else None
                if not isinstance(csv_payload, dict):
                    continue
                filename = str(csv_payload.get("filename") or "luma-attendees.csv")
                content_base64 = str(csv_payload.get("content_base64") or "")
                try:
                    csv_content = base64.b64decode(content_base64).decode("utf-8")
                except Exception:
                    return "mlai-backend returned a Luma CSV I couldn't decode. Ask the team to check the Luma report endpoint."
                title = f"{event.get('event_name') or 'Luma event'} attendees"
                upload_file(
                    channel=channel_id,
                    content=csv_content,
                    filename=filename,
                    title=title,
                    thread_ts=thread_ts,
                )
                uploaded.append(filename)

        message = self._format_luma_attendee_report(report, include_csv=include_csv, uploaded_filenames=uploaded)
        return {
            "message": message,
            "data": {
                "action": "luma_attendee_report",
                "approval_status": approval_status,
                "event_date": event_date,
                "include_csv": include_csv,
                "events": events,
                "uploaded_filenames": uploaded,
            },
        }


    def _resolve_luma_event_count(self, params: dict, text: str, default: int = 3) -> int:
        raw_count = params.get("event_count") or params.get("limit")
        if raw_count is None:
            text_lower = text.lower()
            if re.search(r"\blatest\s+(?:mlai\s+)?event\b", text_lower):
                raw_count = 1
            else:
                count_match = re.search(
                    r"\b(?:past|last|recent|latest)\s+(\d{1,2})\s+(?:mlai\s+)?events?\b",
                    text_lower,
                )
                raw_count = count_match.group(1) if count_match else default
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            count = default
        return max(1, min(count, 10))


    def _luma_request_includes_csv(self, params: dict, text: str) -> bool:
        raw_value = params.get("include_csv")
        if raw_value is not None:
            return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}

        text_lower = text.lower()
        return bool(
            re.search(r"\bcsvs?\b", text_lower)
            or re.search(r"\bexport\b", text_lower)
            or re.search(r"\bguest\s+list\b", text_lower)
            or re.search(r"\battendee\s+list\b", text_lower)
        )


    def _resolve_luma_event_date(self, params: dict, text: str) -> Optional[str]:
        raw_value = params.get("event_date") or params.get("date")
        if raw_value:
            raw_text = str(raw_value).strip()
            iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw_text)
            if iso_match:
                return iso_match.group(1)

        iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
        if iso_match:
            return iso_match.group(1)

        month_lookup = {
            name.lower(): index
            for index, names in enumerate(calendar.month_name)
            for name in [names]
            if name
        }
        month_lookup.update(
            {
                name.lower(): index
                for index, names in enumerate(calendar.month_abbr)
                for name in [names]
                if name
            }
        )
        month_pattern = "|".join(sorted(month_lookup, key=len, reverse=True))
        text_lower = text.lower()
        patterns = [
            rf"\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b",
            rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})(?:\s+(20\d{{2}}))?\b",
        ]
        today = date.today()
        for index, pattern in enumerate(patterns):
            match = re.search(pattern, text_lower)
            if not match:
                continue
            if index == 0:
                month_name, day_text, year_text = match.groups()
            else:
                day_text, month_name, year_text = match.groups()
            month = month_lookup[month_name]
            day = int(day_text)
            year = int(year_text) if year_text else today.year
            try:
                resolved = date(year, month, day)
            except ValueError:
                return None
            if not year_text and resolved > today:
                try:
                    resolved = date(year - 1, month, day)
                except ValueError:
                    return None
            return resolved.isoformat()

        return None


    def _format_luma_attendee_report(
        self,
        report: dict,
        *,
        include_csv: bool,
        uploaded_filenames: list[str],
    ) -> str:
        events = report.get("events") or []
        total_guest_count = report.get("total_guest_count")
        lines = ["*Luma attendee report*"]
        if total_guest_count is not None:
            lines.append(f"Total approved guests: {total_guest_count}")
        for event in events:
            event_name = event.get("event_name") or "Luma event"
            start_label = self._format_luma_event_date(event.get("start_at"))
            guest_count = int(event.get("guest_count") or 0)
            checked_in_count = int(event.get("checked_in_count") or 0)
            suffix = f" ({start_label})" if start_label else ""
            lines.append(
                f"• {event_name}{suffix}: {guest_count} approved guest"
                f"{'s' if guest_count != 1 else ''}, {checked_in_count} checked in"
            )
        if include_csv:
            if uploaded_filenames:
                lines.append("")
                lines.append(
                    f"Uploaded {len(uploaded_filenames)} CSV file"
                    f"{'s' if len(uploaded_filenames) != 1 else ''}: "
                    + ", ".join(f"`{filename}`" for filename in uploaded_filenames)
                )
            else:
                lines.append("")
                lines.append("No CSV files were returned by mlai-backend.")
        return "\n".join(lines)


    @staticmethod
    def _format_luma_event_date(raw_value: Any) -> str:
        if not raw_value:
            return ""
        try:
            raw = str(raw_value).strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return ""
        return parsed.date().isoformat()


