"""Slack-signed meeting time modal and payment review button handoffs."""
import json

from .studio_treasury_commands import enqueue, enqueue_button

ACTION_IDS = {"studio_treasury_authorize", "studio_treasury_review", "studio_meeting_confirm", "studio_meeting_edit", "studio_meeting_link", "studio_meeting_add_scan"}


def modal_context(payload):
    view = payload.get("view", {})
    if payload.get("type") == "view_submission" and view.get("callback_id") == "studio_meeting_time":
        return json.loads(view["private_metadata"])
    return {}


def handle(settings, payload, slack):
    context = modal_context(payload)
    if context:
        value = context["value"]
        minutes = payload["view"]["state"]["values"]["time"]["minutes"]["value"]
        if not str(minutes).isdigit() or not 0 < int(minutes) <= 1440:
            return {"response_action": "errors", "errors": {"time": "Enter the minutes you attended, from 1 to 1440."}}
        value["minutes"] = int(minutes)
        enqueue(settings, team=payload["team"]["id"], actor=payload["user"]["id"], channel=context["channel"],
                source="meeting-modal:" + payload["view"]["id"] + ":" + payload["view"]["hash"],
                command={"operation": "meeting_time", **value})
        return {"response_action": "clear"}
    action = payload["actions"][0]
    if action["action_id"] == "studio_meeting_edit":
        value = json.loads(action["value"])
        slack.views_open(trigger_id=payload["trigger_id"], view={
            "type": "modal", "callback_id": "studio_meeting_time", "private_metadata": json.dumps({
                "value": value, "channel": payload["channel"]["id"]}),
            "title": {"type": "plain_text", "text": "Meeting time"},
            "submit": {"type": "plain_text", "text": "Save"},
            "blocks": [{"type": "input", "block_id": "time", "label": {"type": "plain_text", "text": "Minutes you attended"},
                "element": {"type": "plain_text_input", "action_id": "minutes", "initial_value": str(value["minutes"])}}]})
        return {}
    enqueue_button(settings, payload)
    return {}
