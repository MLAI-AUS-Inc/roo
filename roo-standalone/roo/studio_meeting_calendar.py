"""Optional calendar enrichment for the existing meeting-actions response."""
import asyncio


async def enrich(client, settings, *, actor, candidates, mode, request_source="", channel=None):
    if not getattr(settings, "STUDIO_MEETINGS_CALENDAR_ENABLED", False):
        return "", []
    projects = sorted({row["project_match"]["project"]["id"] for row in candidates
                       if row.get("project_match", {}).get("project", {}).get("id")
                       and float(row["project_match"].get("confidence", 0)) >= .85})
    if not projects:
        return "", []
    try:
        response = await asyncio.wait_for(client.studio_calendar(workspace=settings.TIMESHEET_SLACK_TEAM_ID,
            actor=actor, projects=projects[:5], mode=mode), timeout=20)
    except Exception:
        return "\nCalendar check unavailable. Your action items are saved above.", []
    meetings = response.get("meetings", [])
    lines = []
    for meeting in meetings:
        issue = meeting.get("issue") or {}
        ident = issue.get("identifier", "")
        lines.append(f"• {meeting['title']} · {meeting['start'][:10]} · {meeting['action']} {ident}".strip())
    if (any(row.get("confirmation_needed") or row.get("action") == "needs_review" for row in meetings) and mode == "create"
            and getattr(settings, "STUDIO_TREASURY_COMMANDS_ENABLED", False) and channel):
        from .studio_treasury_commands import enqueue
        try:
            for meeting in meetings:
                if not meeting.get("confirmation_needed") and meeting.get("action") != "needs_review":
                    continue
                recipients = set(meeting.get("notify_slack_ids", [])) | {actor}
                for recipient in recipients:
                    await asyncio.to_thread(enqueue, settings, team=settings.TIMESHEET_SLACK_TEAM_ID,
                        actor=recipient, channel=channel, source="meeting-review:" + meeting["key"],
                        command={"operation": "meeting_review"})
            lines.append("I’ve queued the meeting time check privately for the project attendees.")
        except Exception:
            lines.append("Ask me privately to `check meeting time` when you’re ready.")
    return ("\nMeetings\n" + "\n".join(lines) if lines else ""), meetings
