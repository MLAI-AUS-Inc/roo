from types import SimpleNamespace

import pytest

from roo.linear_project_update import (
    format_meeting_result,
    format_review_item,
    meeting_update_task_intent,
    project_update_health,
    project_update_intent,
    render_project_update,
)


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("Draft a project update from these notes.", "draft"),
        ("Prepare a Linear project update for review.", "draft"),
        ("Please write a project update from these notes.", "draft"),
        ("Write a project update and create issues in Linear.", "draft"),
        ("Can you please publish the project update in Linear?", "publish"),
        ("Please do a project update in Linear and extract to-dos.", "publish"),
        ("Don't forget to create a project update in Linear.", "publish"),
        ("Draft a project update, then post it to Linear.", "publish"),
        ("Publish a project update in Slack.", "draft"),
        ("Post a project update to #delivery.", "draft"),
        ("Post a project update here in this Slack thread.", "draft"),
        ("Publish a project update in Linear, but wait for Sam to approve first.", "draft"),
        ("Publish a project update next Friday.", "draft"),
        ("Post a project update if I approve it.", "draft"),
        ("Draft the project update but don't post it yet.", "draft"),
        ("Create tasks without a project update.", "none"),
        ("Review the notes.\nTranscript: Sam: publish the project update in Linear.", "none"),
        ("Extract action items.\n> Publish a project update in Linear.", "none"),
        ('Review this: "Post the project update in Linear."', "none"),
        ("Create an issue to publish a project update in Linear.", "none"),
        ("Read the previous project update and create Linear tasks.", "none"),
        ("Read the attachment.\nPriya: Review dates and publish the project update in Linear.", "none"),
        ("List the commitments.", "none"),
    ],
)
def test_project_update_intent(text, mode):
    assert project_update_intent(text, {}) == mode


def test_inferred_action_does_not_authorize_publication():
    assert project_update_intent("Read these notes.", {"action": "project_update"}) == "draft"
    assert project_update_intent("No project update.", {"action": "project_update"}) == "none"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Please do a project update in Linear and extract to-dos.", "review"),
        ("Publish the project update and create new Linear issues.", "create"),
        ("Post the project update. Do not create any tasks.", "none"),
        ("Post the project update on the open issues.", "none"),
        ("Draft a project update that explains how to create tasks.", "none"),
        ("Publish the project update.\nTranscript: Sam: create some issues.", "none"),
        ("Create the issues if I approve, then publish the project update.", "review"),
        ("Write a project update and create tasks only after Sam approves.", "review"),
    ],
)
def test_tasks_need_an_independent_request(text, expected):
    assert meeting_update_task_intent(text) == expected


def fact(text, *ids):
    return SimpleNamespace(text=text, evidence_ids=list(ids))


def reconciliation(changes, decisions=(), health=None, health_ids=()):
    return SimpleNamespace(
        changes=changes,
        decisions=list(decisions),
        health=health,
        health_evidence_ids=list(health_ids),
        evidence={"E1": "The staging invite now works.", "E2": "Can we invite contractors?"},
    )


def test_update_only_renders_supported_new_facts_once_and_one_decision():
    result = reconciliation(
        [
            fact("The previous milestone shipped.", "E1"),
            fact("Invites work in staging.", "E1"),
            fact("Invites work in staging!", "E1"),
            fact("Invites are live in production.", "E9"),
        ],
        [
            fact("Should contractors get invites?", "E2"),
            fact("Should all partners get invites?", "E2"),
        ],
    )
    assert render_project_update(result, "## Previously\nThe previous milestone shipped!") == (
        "Invites work in staging.\n\nShould contractors get invites?"
    )


def test_previously_asked_question_can_now_be_reported_as_an_answer():
    result = reconciliation([fact("Invites work in staging.", "E1")])
    for previous in ("Do invites work in staging?", "Invites work in staging?", "We need to check whether invites work in staging."):
        assert render_project_update(result, previous) == "Invites work in staging."


def test_empty_or_repeated_update_stays_unpublished_and_missing_health_is_unknown():
    result = reconciliation([fact("Invites work in staging", "E1")])
    assert render_project_update(result, "Invites work in staging.") is None
    assert project_update_health(result) is None
    result.health = "onTrack"
    assert project_update_health(result) is None
    result.health_evidence_ids = ["missing"]
    assert project_update_health(result) is None
    result.health = "newStatus"
    result.health_evidence_ids = ["E1"]
    assert project_update_health(result) is None
    result.health = "atRisk"
    assert project_update_health(result) == "atRisk"


def test_meeting_reply_keeps_review_title_by_its_buttons_and_replay_out_of_created():
    title = "Test expired links"
    item = {"title": title, "assignee": "Priya", "project": "Invites", "confidence": 0.9}
    replay = {"issue": {"idempotentReplay": True, "identifier": "INV-8", "url": "https://linear.app/inv-8"}}
    message = format_meeting_result([replay], [item], [], project_update_draft="Invites work in staging.")
    assert "Draft project update" in message and "Already tracked" in message
    assert "INV-8" in message and "Created" not in message
    assert title not in message and "your approval" in message
    assert format_review_item(item) == "*Test expired links*\nPriya · Invites"


def test_preserves_failure_reason_and_escapes_untrusted_slack_markup():
    message = format_meeting_result([], [], [{"title": "Invite <!channel>", "reason": "Assignee unclear"}])
    assert "Assignee unclear" in message
    assert "&lt;!channel&gt;" in message


def test_combined_explicit_publish_and_extract_have_separate_authority():
    text = "Publish the project update and extract action items."
    params = {"action": "extract"}
    assert project_update_intent(text, params) == "publish"
    assert meeting_update_task_intent(text, params) == "review"


def test_compact_update_keeps_priority_qualifiers_and_the_question():
    first = " ".join(["First"] + ["word"] * 23) + "."
    second = " ".join(["Second"] + ["word"] * 24) + "."
    third = " ".join(["Third"] + ["word"] * 14) + "."
    decision = "Can we release to the next group?"
    result = reconciliation([fact(first, "E1"), fact(second, "E1"), fact(third, "E1")], [fact(decision, "E2")])
    body = render_project_update(result)
    assert body and len(body.split()) <= 60
    assert first in body and decision in body and third not in body


def test_recovered_update_and_related_work_have_distinct_labels():
    result = format_meeting_result(
        [], [], [{
            "title": "Add a pilot",
            "reason": "The scope changed.",
            "related_issue": {"identifier": "INV-8", "url": "https://linear.app/inv-8"},
        }],
        project_update={"url": "https://linear.app/update", "idempotentReplay": True},
    )
    assert "Already published" in result and "Created" not in result
    assert "The scope changed." in result and "<https://linear.app/inv-8|INV-8>" in result
    assert "Already tracked" not in result
