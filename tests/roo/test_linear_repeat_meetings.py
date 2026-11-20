"""Replay consecutive meetings through the existing executor with frozen provider outputs."""
import json
from types import SimpleNamespace

import pytest

from roo import linear_inference
from roo.linear_inference import LinearMeetingActionBatch
from roo.skills.executors import linear_meeting_actions as workflow


@pytest.mark.asyncio
async def test_repeat_meetings_link_old_work_and_publish_only_a_material_change(monkeypatch):
    team = {"id": "team-1", "key": "ENG"}
    project = {"id": "invites", "name": "Guest Invitations", "teams": {"nodes": [team]}}
    users = [{"id": "person-1", "name": "Priya"}, {"id": "person-2", "name": "Sam"}]
    issues = []
    updates = []
    calls = []

    class Linear:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return users

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            # Deliberately exclude this project's older work from the workspace snapshot.
            return []

        async def list_project_issues(self, project_id):
            assert project_id == project["id"]
            return {"project": project, "nodes": list(issues), "truncated": False, "snapshotAt": "2026-11-20T02:00:00Z"}

        async def list_project_updates(self, project_id):
            assert project_id == project["id"]
            return {"project": project, "nodes": list(updates), "truncated": False}

        async def create_issue(self, **payload):
            assert payload["recheck_project_work"] is True
            assert payload["project_work_snapshot_at"] == "2026-11-20T02:00:00Z"
            identifier = f"ENG-{len(issues) + 1}"
            issue = {
                **payload, "id": identifier, "identifier": identifier,
                "url": f"https://linear.app/example/issue/{identifier}", "state": {"type": "started"},
            }
            issues.append(issue)
            return issue

        async def create_project_update(self, **payload):
            assert len(payload["idempotency_key"]) == 64
            number = len(updates) + 1
            update = {
                **payload, "id": f"update-{number}", "project": project,
                "createdAt": f"2026-11-{number + 17:02d}T00:00:00Z",
                "url": f"https://linear.app/example/project-update/{number}",
            }
            updates.append(update)
            return update

    build = "Priya will build email invites for guests."
    repeat = "Priya is still building the guest invitation email flow."
    staging = "Priya showed guest email invites working in staging."
    test = "Priya will test expired links on Thursday."
    optional = "Anyone interested can sign up for the invite beta."
    question = "Sam needs to decide whether admins should approve guests before release."
    health = "Sam said the project is on track."
    runs = [
        (f"{build} {health}", [(build, "Build email invitations")], [(build, "requirement")]),
        (f"{repeat} {staging} {test} {optional} {question} {health}",
         [(repeat, "Implement guest invite emails"), (test, "Test expired guest email links"),
          (optional, "Check guest email and opt into the beta if interested")],
         [(staging, "progress"), (test, "next_step"), (question, "question")]),
    ]
    meeting = 0

    class Provider:
        async def responses_parse(self, messages, response_format, *, model, **kwargs):
            calls.append((meeting, response_format.__name__, model))
            assert kwargs["store"] is False
            _, actions, claims = runs[min(meeting, 1)]
            if response_format is LinearMeetingActionBatch:
                return {
                    "action_items": [
                        {"title": title, "evidence": quote, "explicit_commitment": True,
                         "owner_hint": "Priya", "confidence": 0.99, "project_hint": project["name"]}
                        for quote, title in actions
                    ],
                    "project_facts": [
                        {"kind": kind, "text": quote, "evidence": quote,
                         "health": "onTrack" if kind == "health" else None}
                        for quote, kind in [(health, "health"), *claims]
                    ],
                }

            context = json.loads(messages[-1]["content"])
            claim_ids = {row["text"]: row["evidence_id"] for row in context["claims"]}
            if meeting > 0:
                assert "ENG-1" in context["candidates"][0]["issue_ids"]
                assert "ENG-1" in context["candidates"][1]["issue_ids"]
                assert context["previous_update"]["body"] == updates[-1]["body"]
            if meeting > 1:
                assert "ENG-2" in context["candidates"][1]["issue_ids"]
            matches = [None] if meeting == 0 else ["ENG-1", None] if meeting == 1 else ["ENG-1", "ENG-2"]
            if meeting > 0:
                matches.append(None)
            changes = (
                [(build, "requirement", "Priya will build email invitations for guests.")]
                if meeting == 0 else
                [(staging, "progress", "Email invites work in staging."),
                 (test, "next_step", test)]
                if meeting == 1 else []
            )
            return {
                "actions": [
                    {"candidate_index": item["index"],
                     "disposition": "ignore" if item["index"] == 2 else "tracked" if match else "new",
                     "evidence_ids": [item["evidence_id"]], "semantic_issue_id": match,
                     "semantic_relation": "same_outcome" if match else None, "duplicate_of_index": None}
                    for item, match in zip(context["candidates"], matches, strict=True)
                ],
                "changes": [
                    {"kind": kind, "text": sentence, "evidence_ids": [claim_ids[quote]]}
                    for quote, kind, sentence in changes
                ],
                "decisions": [
                    {"kind": "question", "text": "Sam, should admins approve guests before release?",
                     "evidence_ids": [claim_ids[question]]}
                ] if meeting == 1 else [],
                "health": "onTrack",
                "health_evidence_ids": [claim_ids[health]],
            }

    monkeypatch.setattr(linear_inference, "get_llm_client", lambda provider: Provider())
    monkeypatch.setattr(workflow, "get_settings", lambda: SimpleNamespace(LINEAR_MEETING_ASTRA_PROJECTS="invites"))
    executor = workflow.LinearMeetingActionsExecutor()

    async def run(text, notes, number):
        return await executor._execute_linear_meeting_actions(
            skill=SimpleNamespace(get_client_class=lambda name: Linear),
            text=text, params={"project_hint": project["name"], "transcript": notes},
            user_id="UPRIYA", channel_id="CENG", thread_ts=f"10.{number}",
            current_message_ts=f"10.{number}", thread_history=[],
        )

    request = "Publish a project update in Linear and create the agreed tasks in Linear."
    first = await run(request, runs[0][0], 1)
    assert first["data"]["created_count"] == 1
    assert len(updates) == 1

    meeting = 1
    second = await run(request, runs[1][0], 2)
    assert second["data"]["created_count"] == 1
    assert second["data"]["review_count"] == 0
    assert "Already tracked in Linear" in second["message"]
    assert "ENG-1" in second["message"]
    assert "opt into the beta" not in second["message"]
    assert [issue["title"] for issue in issues] == ["Build email invitations", "Test expired guest email links"]
    assert updates[-1]["body"] == (
        "Email invites work in staging. Priya will test expired links on Thursday.\n\n"
        "Sam, should admins approve guests before release?"
    )

    meeting = 2
    replay = await run(request, runs[1][0], 3)
    assert replay["data"]["created_count"] == 0
    assert "no material changes" in replay["message"]
    assert len(issues) == len(updates) == 2
    assert [entry[2] for entry in calls if entry[1] == "LinearMeetingActionBatch"] == ["gpt-6.2-sol"] * 3
    assert [entry[2] for entry in calls if entry[1] != "LinearMeetingActionBatch"] == ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.2-astra"]
