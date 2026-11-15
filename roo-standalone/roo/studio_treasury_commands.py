"""Signed Slack ingress only. Private worker credentials never enter public Roo."""
from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import UUID

from .payment_reminders import ReceiptStore
from .timesheets import fingerprint

HELP = "Ask `check builder invoices` or `pay builder invoices`. Roo sends a private review before the bank handoff."


def parse(text, bot, dm):
    text = str(text or "").strip()
    if text.startswith(f"<@{bot}>") and bot:
        text = text[len(bot) + 3:].strip()
    elif not dm:
        return None
    match = re.fullmatch(r"(?:please )?(?:check|pay) (?:the )?(?:builder'?s?|people'?s?) invoices(?:\s+([\w-]+))?[.!]?", text, re.I)
    if match:
        return {"operation": "preview", "selector": match[1] or "recent"}
    match = re.fullmatch(r"(?:pay ready invoices|review invoices) ([a-f0-9-]{36})", text, re.I)
    if match:
        return {"operation": "authorize" if text.lower().startswith("pay") else "show", "review_id": str(UUID(match[1]))}
    match = re.fullmatch(r"confirm meeting ([a-f0-9]{64}) (\d{1,4}) min(?:utes)?", text, re.I)
    if match:
        return {"operation": "meeting_time", "meeting": match[1], "minutes": int(match[2])}
    if text.casefold() in {"check meeting time", "review meeting time"}:
        return {"operation": "meeting_review"}
    match = re.fullmatch(r"check meetings (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})", text, re.I)
    if match:
        return {"operation": "meeting_scan", "start": match[1], "end": match[2]}
    match = re.fullmatch(r"record payment ([a-f0-9-]{36}) ([\w-]+) ([a-f0-9-]{36})", text, re.I)
    if match:
        return {"operation": "receipt", "review_id": str(UUID(match[1])), "invoice": match[2], "receipt_id": str(UUID(match[3]))}
    match = re.fullmatch(r"mark payment unknown ([a-f0-9-]{36}) ([\w-]+)", text, re.I)
    if match:
        return {"operation": "unknown", "review_id": str(UUID(match[1])), "invoice": match[2]}
    return None


def enqueue(settings, *, team, actor, channel, source, command):
    if not command:
        return {"handled": False}
    if not getattr(settings, "STUDIO_TREASURY_COMMANDS_ENABLED", False):
        return {"handled": True, "new": True, "text": "Studio invoice checks are not enabled on this Roo deployment."}
    allowed = {x.strip() for x in settings.STUDIO_TREASURY_SLACK_IDS.split(",") if x.strip()}
    if (team != settings.TIMESHEET_SLACK_TEAM_ID or not re.fullmatch(r"[UW][A-Z0-9]+", str(actor))
            or not re.fullmatch(r"[CDG][A-Z0-9]+", str(channel))
            or command["operation"] not in {"meeting_time", "meeting_review", "meeting_link", "meeting_scan"} and actor not in allowed):
        return {"handled": True, "new": True, "text": "This private Studio request could not be authorised."}
    key = fingerprint({"team": team, "actor": actor, "source": source})
    store = ReceiptStore(Path(settings.STUDIO_TREASURY_QUEUE_DIR))
    with store.locked(key) as acquired:
        if not acquired:
            raise RuntimeError("treasury_queue_busy")
        previous = store.read(key)
        if previous:
            if previous["command"] != command:
                raise ValueError("treasury_request_replay_mismatch")
            return {"handled": True, "new": False}
        store.write(key, {"workspace": team, "actor": actor, "command": command,
                          "request_key": key, "status": "pending", "requested_at": datetime.now(timezone.utc).isoformat()})
    return {"handled": True, "new": True, "text": "I’m checking this privately. Look for Roo’s DM."}


def enqueue_event(settings, payload):
    event = payload.get("event") or {}
    if event.get("bot_id") or event.get("subtype") or event.get("type") not in {"message", "app_mention"}:
        return {"handled": False}
    dm = event.get("channel_type") == "im" and str(event.get("channel", "")).startswith("D")
    return enqueue(settings, team=payload.get("team_id"), actor=event.get("user"), channel=event.get("channel"),
        source=event.get("ts"), command=parse(event.get("text"), settings.TIMESHEET_SLACK_BOT_USER_ID, dm))


def enqueue_button(settings, payload):
    action = payload["actions"][0]
    if action["action_id"] == "studio_meeting_add_scan":
        import json
        command = {"operation": "meeting_scan", **json.loads(action["value"]), "mode": "create"}
        return enqueue(settings, team=payload["team"]["id"], actor=payload["user"]["id"],
            channel=payload["channel"]["id"], source="meeting-scan:" + str(action["action_ts"]), command=command)
    if action["action_id"] in {"studio_meeting_confirm", "studio_meeting_link"}:
        import json
        value = json.loads(action["value"])
        command = {"operation": "meeting_time" if action["action_id"] == "studio_meeting_confirm" else "meeting_link", **value}
        return enqueue(settings, team=payload["team"]["id"], actor=payload["user"]["id"],
            channel=payload["channel"]["id"], source="meeting-time:" + str(action["action_ts"]), command=command)
    operation = {"studio_treasury_authorize": "authorize", "studio_treasury_review": "show"}[action["action_id"]]
    return enqueue(settings, team=payload["team"]["id"], actor=payload["user"]["id"],
        channel=payload["channel"]["id"], source="treasury-button:" + str(action["action_ts"]),
        command={"operation": operation, "review_id": str(UUID(action["value"]))})
