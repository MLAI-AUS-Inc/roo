"""Native Slack views for structured volunteer time."""

import re
from decimal import Decimal
TITLES = {"member": "Request Roo Points", "admin": "Award Roo Points"}


def plain(text):
    return {"type": "plain_text", "text": text}


def option(value, label):
    return {"text": plain(label), "value": value}


def field(key, label, element, *, optional=False, dispatch=False, hint=""):
    block = {"type": "input", "block_id": key, "label": plain(label),
             "element": {"action_id": key, **element}, "optional": optional}
    if dispatch:
        block["dispatch_action"] = True
    if hint:
        block["hint"] = plain(hint)
    return block


def other_duration(state):
    parts = {}
    errors = {}
    for key in ("hours", "minutes"):
        raw = state.get(key) or ""
        if not isinstance(raw, str) or (raw and not re.fullmatch(r"[0-9]+", raw)):
            errors[key] = "Enter a whole number of " + key + "."
            continue
        digits = raw.lstrip("0") or "0"
        if len(digits) > 12:
            errors[key] = "Enter a smaller number of " + key + "."
            continue
        parts[key] = int(digits)
    if parts.get("minutes", 0) >= 60:
        errors["minutes"] = "Enter 0–59 minutes."
    if errors:
        return None, errors
    total = parts["hours"] * 60 + parts["minutes"]
    if total == 0:
        return None, {"minutes": "Enter at least one minute in total."}
    if total * 100_000 > 2**53 - 1:
        return None, {"hours": "Enter a smaller number of hours."}
    return total, {}


def duration_minutes(state, catalogue):
    mode = state.get("duration")
    if mode in {option["value"] for option in catalogue["duration_options"]} - {"other"}:
        return int(mode)
    if mode == "other":
        return other_duration(state)[0]
    return None


def points_text(minutes):
    return format(Decimal(minutes) / Decimal(10), "f")


def build_view(mode, catalogue, state=None, *, draft_id=""):
    actions = {action["id"]: action for group in catalogue["groups"] for action in group["actions"]}
    durations = {option["value"]: option["label"] for option in catalogue["duration_options"]}
    state = state or {}
    blocks = []
    if mode == "admin":
        picker = {"type": "users_select", "placeholder": plain("Choose a member")}
        if state.get("recipient"):
            picker["initial_user"] = state["recipient"]
        blocks.append(field("recipient", "Who helped?", picker))

    groups = [{"label": plain(g["label"]),
               "options": [option(a["id"], a["label"]) for a in g["actions"]]}
              for g in catalogue["groups"]]
    action_ids = list(dict.fromkeys(state.get("actions", [])))
    task_picker = {"type": "multi_static_select", "option_groups": groups,
                   "placeholder": plain("Choose one or more tasks")}
    selected = [option(key, actions[key]["label"]) for key in action_ids if key in actions]
    if selected:
        task_picker["initial_options"] = selected
    blocks.append(field("actions", "What did you do?" if mode == "member" else "What did they do?",
                        task_picker, dispatch=True))

    duration = state.get("duration", "")
    duration_picker = {"type": "static_select", "placeholder": plain("Choose a duration"),
                       "options": [option(k, v) for k, v in durations.items()]}
    if duration in durations:
        duration_picker["initial_option"] = option(duration, durations[duration])
    blocks.append(field("duration", "How long did you help?" if mode == "member" else "How long did they help?",
                        duration_picker, dispatch=True, hint="10 minutes = 1 Roo Point. Count the time once across all tasks."))
    if duration == "other":
        for key, label in [("hours", "Hours"), ("minutes", "Minutes")]:
            number = {"type": "number_input", "is_decimal_allowed": False, "min_value": "0",
                      "placeholder": plain("0"),
                      "dispatch_action_config": {"trigger_actions_on": ["on_character_entered"]}}
            if key == "minutes":
                number["max_value"] = "59"
            value = state.get(key) or ""
            digits = value.lstrip("0") or "0"
            if re.fullmatch(r"[0-9]+", value) and len(digits) <= 12:
                if key == "hours" or int(digits) < 60:
                    number["initial_value"] = value
            blocks.append(field(key, label, number, optional=True, dispatch=True))

    with_other = "other" in action_ids
    note = {"type": "plain_text_input", "max_length": 500,
            "placeholder": plain("Briefly describe the other work" if with_other else "Event name, project or link")}
    if state.get("note"):
        note["initial_value"] = state["note"]
    is_restricted = any(actions.get(key, {}).get("restricted") for key in action_ids)
    blocks.append(field("note", "Describe the other work" if with_other else "Event, project or note",
                        note, optional=not with_other,
                        hint="Leave out names and private support details." if is_restricted else ""))
    minutes = duration_minutes(state, catalogue)
    if minutes is not None:
        blocks.append({"type": "section", "text": plain(f"{minutes:,} minutes  ·  {points_text(minutes)} Roo Points")})

    result = {"type": "modal", "callback_id": "roo_volunteer_time_" + mode,
              "title": plain(TITLES[mode]), "close": plain("Cancel"),
              "submit": plain("Request points" if mode == "member" else "Award points"),
              "blocks": blocks}
    if draft_id:
        result["private_metadata"] = draft_id
    return result


def read_state(values):
    def entry(key):
        return values.get(key, {}).get(key, {})
    selected = entry("actions").get("selected_options") or []
    duration = entry("duration").get("selected_option") or {}
    return {
        "actions": list(dict.fromkeys(a["value"] for a in selected)),
        "duration": duration.get("value", ""), "minutes": entry("minutes").get("value") or "",
        "hours": entry("hours").get("value") or "",
        "note": entry("note").get("value") or "", "recipient": entry("recipient").get("selected_user") or "",
    }


def review_submission(mode, state, catalogue):
    actions = {action["id"] for group in catalogue["groups"] for action in group["actions"]}
    errors = {}
    tasks = list(dict.fromkeys(state.get("actions", [])))
    if not tasks or any(t not in actions for t in tasks):
        errors["actions"] = "Choose at least one task from the list."
    minutes = duration_minutes(state, catalogue)
    if minutes is None:
        if state.get("duration") == "other":
            errors.update(other_duration(state)[1])
        else:
            errors["duration"] = "Choose a duration."
    note = state.get("note", "").strip()
    if "other" in tasks and not note:
        errors["note"] = "Briefly describe the other work."
    if len(note) > 500:
        errors["note"] = "Keep your note to 500 characters."
    if mode == "admin" and not state.get("recipient"):
        errors["recipient"] = "Choose the member who volunteered."
    if errors:
        return {"response_action": "errors", "errors": errors}
    return {"action_ids": tasks, "volunteer_minutes": minutes, "note": note}
