from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import roo.linear_inference as inference_module
import roo.skills.executor as executor_module
from roo.linear_effort_sizing import (
    StudioEffortAssessment,
    StudioEffortAssessmentBatch,
    _bounded_json,
    assess_studio_effort_batch,
    is_project_issue,
    is_studio_project,
)
from roo.linear_meeting_sources import ParsedSource, SourceParseResult
from roo.llm import ToolCallParseError
from roo.skills.executor import SkillExecutor


def _assessment(candidate_key: str, **overrides):
    values = {
        "candidate_key": candidate_key,
        "effort_label": "Small (S)",
        "rationale": "The remaining scoped edit should take about 45 minutes.",
        "confidence": 0.91,
        "sizing_basis": "duration",
        "expected_effort_range": ">15m-1h",
        "context_sufficient": True,
        "missing_context": [],
        "evidence_refs": [candidate_key],
        "split_recommended": False,
    }
    values.update(overrides)
    return StudioEffortAssessment(**values)


def test_studio_trigger_is_exact_prefix():
    assert is_studio_project({"name": "[Studio] Founder Games"})
    assert not is_studio_project({"name": " [Studio] Founder Games"})
    assert not is_studio_project({"name": "[studio] Founder Games"})


def test_project_issue_trigger_accepts_any_resolved_project():
    assert is_project_issue({"id": "project-1", "name": "Aaron AI"})
    assert is_project_issue({"id": "project-2", "name": "[Studio] Founder Games"})
    assert not is_project_issue({"name": "No resolved ID"})


def test_bounded_sizing_context_stays_valid_and_preserves_batch_and_sections():
    candidates = [
        {
            "candidate_key": f"c{index}",
            "title": f"Task {index}",
            "description": "Detailed candidate scope. " * 300,
            "acceptance_criteria": ["Criterion " * 100 for _ in range(10)],
        }
        for index in range(1, 4)
    ]
    project_context = {
        "project": {
            "id": "project-1",
            "name": "[Studio] Aaron AI",
            "content": "Project scope. " * 500,
        },
        **{
            collection: {
                "nodes": [
                    {
                        "id": f"{collection}-{index}",
                        "body": "Historical project evidence. " * 200,
                    }
                    for index in range(15)
                ]
            }
            for collection in (
                "projectUpdates",
                "activeIssues",
                "terminalReferences",
                "sizingPrecedents",
            )
        },
    }

    serialized = _bounded_json(
        {
            "candidates": candidates,
            "project_context": project_context,
        },
        max_chars=40_000,
    )
    parsed = json.loads(serialized)

    assert len(serialized) <= 40_000
    assert parsed["context_truncated"] is True
    assert [
        candidate["candidate_key"]
        for candidate in parsed["candidates"]
    ] == ["c1", "c2", "c3"]
    assert set(parsed["project_context"]) == set(project_context)
    assert all(
        parsed["project_context"][collection]["nodes"]
        for collection in (
            "projectUpdates",
            "activeIssues",
            "terminalReferences",
            "sizingPrecedents",
        )
    )


def test_effort_schema_rejects_inconsistent_range_and_split():
    with pytest.raises(ValidationError):
        _assessment("c1", expected_effort_range=">1h-2h")
    with pytest.raises(ValidationError):
        _assessment("c1", split_recommended=True)

    xl = _assessment(
        "c1",
        effort_label="Extra Large (XL)",
        rationale="The remaining implementation exceeds five hours and should be split.",
        expected_effort_range=">5h",
        sizing_basis="duration",
        split_recommended=True,
    )
    assert xl.split_recommended is True


def test_effort_schema_normalizes_duration_rationale_to_one_sentence_with_time_anchor():
    assessment = _assessment(
        "c1",
        rationale=(
            "The implementation is well scoped. "
            "The existing endpoint can be reused."
        ),
    )

    assert assessment.rationale.startswith("Estimated at up to 1 hour because ")
    assert assessment.rationale.endswith(".")
    assert "\n" not in assessment.rationale
    assert len(
        re.findall(r"[.!?](?:[\"')\]]+)?(?=\s|$)", assessment.rationale)
    ) == 1
    assert len(assessment.rationale) <= 280


@pytest.mark.asyncio
async def test_sizing_uses_dedicated_openai_responses_configuration(monkeypatch):
    calls = []

    class FakeClient:
        async def responses_parse(self, messages, response_format, **kwargs):
            calls.append((messages, response_format, kwargs))
            return StudioEffortAssessmentBatch(assessments=[_assessment("c1")])

    monkeypatch.setattr(
        inference_module,
        "get_llm_client",
        lambda provider: FakeClient() if provider == "openai" else None,
    )

    result = await assess_studio_effort_batch(
        candidates=[
            {
                "candidate_key": "c1",
                "title": "Update the run sheet",
                "remaining_work": "Apply the confirmed timing changes.",
            }
        ],
        project_context={
            "project": {"id": "project-1", "name": "[Studio] Founder Games"}
        },
        context_max_chars=40000,
        safety_identifier="roo-linear-test",
    )

    assert result["c1"].effort_label == "Small (S)"
    _, response_format, kwargs = calls[0]
    assert response_format is StudioEffortAssessmentBatch
    assert kwargs["model"] == "gpt-5.6-sol"
    assert kwargs["reasoning_effort"] == "xhigh"
    assert kwargs["store"] is False
    assert "untrusted data" in calls[0][0][-1]["content"]


@pytest.mark.asyncio
async def test_sizing_discards_unsupplied_evidence_refs_without_retry(monkeypatch):
    calls = []

    class FakeClient:
        async def responses_parse(self, messages, response_format, **kwargs):
            calls.append((messages, response_format, kwargs))
            return StudioEffortAssessmentBatch(
                assessments=[
                    _assessment(
                        "c1",
                        rationale=(
                            "The endpoint and secure sharing steps are already "
                            "well understood."
                        ),
                        evidence_refs=[
                            "c1",
                            "invented-reference",
                            "project-1",
                            "invented-reference",
                        ],
                    )
                ]
            )

    monkeypatch.setattr(
        inference_module,
        "get_llm_client",
        lambda provider: FakeClient() if provider == "openai" else None,
    )

    result = await assess_studio_effort_batch(
        candidates=[
            {
                "candidate_key": "c1",
                "title": "Create the Aaron AI API key",
                "remaining_work": "Create and securely share the key.",
            }
        ],
        project_context={
            "project": {"id": "project-1", "name": "[Studio] Aaron AI"}
        },
        context_max_chars=40_000,
        safety_identifier="roo-linear-test",
    )

    assert result["c1"].evidence_refs == ["c1", "project-1"]
    assert result["c1"].rationale.startswith(
        "Estimated at up to 1 hour because "
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_executor_batches_one_sizing_call_per_studio_project(monkeypatch):
    executor = SkillExecutor()
    project = {"id": "project-1", "name": "[Studio] Founder Games"}
    team = {"id": "team-1", "key": "STU"}
    calls = []

    async def fake_assess(**kwargs):
        calls.append(kwargs)
        return {
            candidate["candidate_key"]: _assessment(candidate["candidate_key"])
            for candidate in kwargs["candidates"]
        }

    class FakeClient:
        async def get_project_sizing_context(self, project_id):
            assert project_id == "project-1"
            return {
                "project": project,
                "projectUpdates": {"nodes": [{"id": "update-1", "body": "Scope is confirmed."}]},
                "activeIssues": {"nodes": []},
                "terminalReferences": {"nodes": []},
                "sizingPrecedents": {"nodes": []},
                "effortLabelRegistry": {
                    "nodes": [
                        {
                            "id": "effort-small",
                            "name": "Small (S)",
                            "team": {"id": "team-1"},
                        }
                    ]
                },
            }

    def prepared(key, title):
        return {
            "candidate_key": key,
            "candidate": {
                "title": title,
                "description": title,
                "work_status": "open",
                "completed_work": "",
                "remaining_work": title,
                "available_artifacts": [],
                "dependencies": [],
                "acceptance_criteria": [],
                "evidence": title,
                "source_label": "Slack thread",
            },
            "owner_match": {"user": {"id": "user-1", "name": "Sam"}},
            "project_match": {"project": project},
            "team_match": {"team": team},
            "source": {"source_permalink": "https://slack.test/source"},
            "decision": "create",
            "display": {"title": title},
        }

    monkeypatch.setattr(executor_module, "assess_studio_effort_batch", fake_assess)
    prepared_candidates = [
        prepared("c1", "Update the run sheet"),
        prepared("c2", "Email the final run sheet"),
    ]
    shadow = await executor._size_linear_studio_prepared_candidates(
        prepared_candidates=prepared_candidates,
        client=FakeClient(),
        labels=[],
        settings=SimpleNamespace(
            LINEAR_STUDIO_SIZING_MODE="required",
            LINEAR_STUDIO_SIZING_CONTEXT_MAX_CHARS=40000,
            LINEAR_STUDIO_SIZING_RUBRIC_VERSION="studio-effort-v1",
        ),
        requester_slack_id="USAM",
    )

    assert len(calls) == 1
    assert len(calls[0]["candidates"]) == 2
    assert len(shadow) == 2
    for item in prepared_candidates:
        assert item["effort_label_id"] == "effort-small"
        assert item["effort_assessment"]["effort_label"] == "Small (S)"
        assert item["effort_assessment"]["project_name_at_assessment"].startswith(
            "[Studio]"
        )
        assert item["display"]["effort_rationale"].endswith("minutes.")


@pytest.mark.asyncio
async def test_executor_bounds_studio_sizing_batches(monkeypatch):
    executor = SkillExecutor()
    calls = []

    async def fake_assess(**kwargs):
        calls.append(
            {
                "keys": [
                    candidate["candidate_key"]
                    for candidate in kwargs["candidates"]
                ],
                "chunk_index": kwargs["batch_chunk_index"],
                "chunk_count": kwargs["batch_chunk_count"],
                "recovery_depth": kwargs.get("recovery_depth", 0),
            }
        )
        return {
            candidate["candidate_key"]: _assessment(
                candidate["candidate_key"]
            )
            for candidate in kwargs["candidates"]
        }

    monkeypatch.setattr(executor_module, "assess_studio_effort_batch", fake_assess)
    candidates = [
        {"candidate_key": f"c{index}", "title": f"Task {index}"}
        for index in range(1, 8)
    ]

    assessments, errors = (
        await executor._assess_linear_studio_candidates_resilient(
            candidates=candidates,
            project_context={
                "project": {"id": "project-1", "name": "[Studio] Aaron AI"}
            },
            context_max_chars=40_000,
            batch_size=3,
            safety_identifier="roo-linear-test",
        )
    )

    assert list(assessments) == [f"c{index}" for index in range(1, 8)]
    assert errors == {}
    assert calls == [
        {
            "keys": ["c1", "c2", "c3"],
            "chunk_index": 1,
            "chunk_count": 3,
            "recovery_depth": 0,
        },
        {
            "keys": ["c4", "c5", "c6"],
            "chunk_index": 2,
            "chunk_count": 3,
            "recovery_depth": 0,
        },
        {
            "keys": ["c7"],
            "chunk_index": 3,
            "chunk_count": 3,
            "recovery_depth": 0,
        },
    ]


@pytest.mark.asyncio
async def test_failed_studio_batch_recovers_per_candidate_and_preserves_successes(
    monkeypatch,
):
    executor = SkillExecutor()
    project = {"id": "project-1", "name": "[Studio] Aaron AI"}
    team = {"id": "team-1", "key": "STU"}
    calls = []

    async def fake_assess(**kwargs):
        keys = [
            candidate["candidate_key"]
            for candidate in kwargs["candidates"]
        ]
        calls.append((keys, kwargs.get("recovery_depth", 0)))
        if keys == ["c1", "c2"] or keys == ["c2"]:
            raise ToolCallParseError("structured_response_missing")
        return {key: _assessment(key) for key in keys}

    class FakeClient:
        async def get_project_sizing_context(self, project_id):
            return {
                "project": project,
                "projectUpdates": {"nodes": []},
                "activeIssues": {"nodes": []},
                "terminalReferences": {"nodes": []},
                "sizingPrecedents": {"nodes": []},
                "effortLabelRegistry": {
                    "nodes": [
                        {
                            "id": "effort-small",
                            "name": "Small (S)",
                            "team": {"id": "team-1"},
                        }
                    ]
                },
            }

    def prepared(key):
        return {
            "candidate_key": key,
            "candidate": {
                "title": f"Task {key}",
                "description": f"Complete task {key}.",
                "work_status": "open",
                "completed_work": "",
                "remaining_work": f"Complete task {key}.",
                "available_artifacts": [],
                "dependencies": [],
                "acceptance_criteria": [f"Task {key} is complete"],
                "evidence": f"Do task {key}",
                "source_label": "Slack thread",
            },
            "owner_match": {"user": {"id": "user-1", "name": "Sam"}},
            "project_match": {"project": project},
            "team_match": {"team": team},
            "source": {"source_permalink": "https://slack.test/source"},
            "decision": "create",
            "display": {"title": f"Task {key}"},
        }

    monkeypatch.setattr(executor_module, "assess_studio_effort_batch", fake_assess)
    prepared_candidates = [prepared("c1"), prepared("c2"), prepared("c3")]

    shadow = await executor._size_linear_studio_prepared_candidates(
        prepared_candidates=prepared_candidates,
        client=FakeClient(),
        labels=[],
        settings=SimpleNamespace(
            LINEAR_STUDIO_SIZING_MODE="required",
            LINEAR_STUDIO_SIZING_CONTEXT_MAX_CHARS=40_000,
            LINEAR_STUDIO_SIZING_BATCH_SIZE=2,
            LINEAR_STUDIO_SIZING_RUBRIC_VERSION="studio-effort-v1",
        ),
        requester_slack_id="USAM",
    )

    assert calls == [
        (["c1", "c2"], 0),
        (["c1"], 1),
        (["c2"], 1),
        (["c3"], 0),
    ]
    assert prepared_candidates[0]["effort_label_id"] == "effort-small"
    assert "studio_sizing_error" not in prepared_candidates[0]
    assert "effort_label_id" not in prepared_candidates[1]
    assert "structured_response_missing" in prepared_candidates[1][
        "studio_sizing_error"
    ]
    assert prepared_candidates[2]["effort_label_id"] == "effort-small"
    assert "studio_sizing_error" not in prepared_candidates[2]
    assert {
        result.get("candidate_key")
        for result in shadow
        if result.get("effort_label") == "Small (S)"
    } == {"c1", "c3"}
    assert [
        result["candidate_key"]
        for result in shadow
        if result.get("error")
    ] == ["c2"]


@pytest.mark.asyncio
async def test_new_issue_sizing_runs_for_non_studio_project(monkeypatch):
    executor = SkillExecutor()
    project = {"id": "project-1", "name": "Aaron AI"}
    team = {"id": "team-1", "key": "ENG"}

    async def fake_assess(**kwargs):
        candidate = kwargs["candidates"][0]
        return {candidate["candidate_key"]: _assessment(candidate["candidate_key"])}

    class FakeClient:
        async def get_project_sizing_context(self, project_id):
            return {
                "project": project,
                "projectUpdates": {"nodes": []},
                "activeIssues": {"nodes": []},
                "terminalReferences": {"nodes": []},
                "sizingPrecedents": {"nodes": []},
                "effortLabelRegistry": {
                    "nodes": [
                        {
                            "id": "effort-small",
                            "name": "Small (S)",
                            "team": {"id": "team-1"},
                        }
                    ]
                },
            }

        async def get_issue_receipt(self, _key):
            return {"status": "not_found"}

    monkeypatch.setattr(executor_module, "assess_studio_effort_batch", fake_assess)
    prepared = {
        "candidate_key": "c1",
        "idempotency_key": "a" * 64,
        "candidate": {
            "title": "Send interview invite",
            "description": "Send the prepared invitation.",
            "work_status": "open",
            "remaining_work": "Send the prepared invitation.",
            "available_artifacts": [],
            "dependencies": [],
            "acceptance_criteria": ["Invitation sent"],
        },
        "owner_match": {"user": {"id": "user-1", "name": "Sam"}},
        "project_match": {"project": project},
        "team_match": {"team": team},
        "source": {},
        "decision": "create",
        "display": {"title": "Send interview invite"},
    }

    await executor._size_linear_studio_prepared_candidates(
        prepared_candidates=[prepared],
        client=FakeClient(),
        labels=[],
        settings=SimpleNamespace(
            LINEAR_TASK_SIZING_MODE="required",
            LINEAR_TASK_SIZING_CONTEXT_MAX_CHARS=40_000,
            LINEAR_TASK_SIZING_BATCH_SIZE=3,
            LINEAR_TASK_SIZING_RUBRIC_VERSION="project-effort-v2",
        ),
        requester_slack_id="USAM",
    )

    assert prepared["effort_label_id"] == "effort-small"
    assert prepared["effort_assessment"]["model"] == "gpt-5.6-sol"
    assert prepared["effort_assessment"]["rubric_version"] == "project-effort-v2"


@pytest.mark.asyncio
async def test_project_backfill_builds_no_write_preview_with_full_context(monkeypatch):
    executor = SkillExecutor()
    project = {
        "id": "project-1",
        "name": "Aaron AI",
        "slugId": "aaron-ai",
        "members": {"nodes": [{"id": "linear-sam"}]},
    }
    labels = [
        {
            "id": f"label-{index}",
            "name": name,
            "team": {"id": "team-1"},
        }
        for index, name in enumerate(
            (
                "Extra Small (XS)",
                "Small (S)",
                "Medium (M)",
                "Large (L)",
                "Extra Large (XL)",
            )
        )
    ]
    created_payloads = []

    class FakeClient:
        async def list_active_projects(self):
            return [project]

        async def list_users(self):
            return [
                {
                    "id": "linear-sam",
                    "name": "Dr Sam",
                    "email": "sam@example.com",
                }
            ]

        async def list_issue_labels(self):
            return labels

        async def list_project_issues(self, project_id, max_issues):
            return {
                "project": project,
                "snapshotAt": "2026-08-23T01:00:00Z",
                "terminalStateTypes": ["completed", "canceled", "duplicate"],
                "nodes": [
                    {
                        "id": "issue-open",
                        "identifier": "ENG-1",
                        "title": "Send interview invite",
                        "description": "Use the prepared copy and recipient list.",
                        "updatedAt": "2026-08-23T00:00:00Z",
                        "state": {"type": "started"},
                        "team": {"id": "team-1", "key": "ENG"},
                        "labels": {
                            "nodes": [{"id": "meeting", "name": "meeting-action"}]
                        },
                    },
                    {
                        "id": "issue-sized",
                        "identifier": "ENG-2",
                        "title": "Already sized",
                        "updatedAt": "2026-08-22T00:00:00Z",
                        "state": {"type": "unstarted"},
                        "team": {"id": "team-1", "key": "ENG"},
                        "labels": {
                            "nodes": [
                                {"id": "label-1", "name": "Small (S)"}
                            ]
                        },
                    },
                    {
                        "id": "issue-done",
                        "identifier": "ENG-3",
                        "title": "Done",
                        "updatedAt": "2026-08-21T00:00:00Z",
                        "state": {"type": "completed"},
                        "team": {"id": "team-1", "key": "ENG"},
                        "labels": {"nodes": []},
                    },
                ],
            }

        async def list_project_updates(self, project_id):
            return {
                "nodes": [
                    {"id": "update-1", "body": "Interview materials are ready."}
                ],
                "truncated": False,
            }

        async def get_project_sizing_context(self, project_id):
            return {
                "project": project,
                "projectUpdates": {"nodes": []},
                "activeIssues": {"nodes": []},
                "terminalReferences": {"nodes": []},
                "sizingPrecedents": {"nodes": []},
            }

        async def create_project_sizing_run(self, *, project_id, payload):
            created_payloads.append(payload)
            return {"id": "run-1"}

    async def fake_resilient(**kwargs):
        assert kwargs["project_context"]["projectUpdates"]["nodes"][0]["id"] == "update-1"
        assert len(kwargs["project_context"]["activeIssues"]["nodes"]) == 2
        return {"issue-open": _assessment("issue-open")}, {}

    monkeypatch.setattr(
        executor,
        "_assess_linear_studio_candidates_resilient",
        fake_resilient,
    )
    result = await executor._execute_linear_project_issue_sizing(
        text="Size the unsized tasks in Linear project Aaron AI",
        params={
            "project_hint": "Aaron AI",
            "requester_email": "sam@example.com",
        },
        user_id="USAM",
        client=FakeClient(),
        settings=SimpleNamespace(
            LINEAR_TASK_SIZING_CONTEXT_MAX_CHARS=40_000,
            LINEAR_TASK_SIZING_BATCH_SIZE=3,
            LINEAR_TASK_SIZING_RUBRIC_VERSION="project-effort-v2",
            LINEAR_PROJECT_SIZING_MAX_ISSUES=500,
        ),
    )

    assert result["data"]["preview_count"] == 1
    assert result["data"]["skipped_already_sized"] == 1
    assert result["data"]["skipped_terminal"] == 1
    assert result["blocks"][1]["elements"][0]["action_id"] == (
        "linear_project_sizing_apply"
    )
    assert len(created_payloads) == 1
    assert created_payloads[0]["items"][0]["issue_id"] == "issue-open"
    assert created_payloads[0]["model"] == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_completed_receipt_skips_project_context_and_model(monkeypatch):
    executor = SkillExecutor()
    project = {"id": "project-1", "name": "[Studio] Founder Games"}
    prepared = {
        "candidate_key": "c1",
        "idempotency_key": "a" * 64,
        "candidate": {"title": "Send the run sheet"},
        "owner_match": {"user": {"id": "user-1"}},
        "project_match": {"project": project},
        "team_match": {"team": {"id": "team-1"}},
        "source": {},
        "decision": "create",
        "display": {"title": "Send the run sheet"},
    }

    class FakeClient:
        async def get_issue_receipt(self, idempotency_key):
            assert idempotency_key == "a" * 64
            return {
                "status": "completed",
                "issue": {
                    "identifier": "STU-1",
                    "sizingMetadata": {
                        "effort_label": "Small (S)",
                        "rationale": "The remaining edit took about 45 minutes.",
                    },
                },
            }

        async def get_project_sizing_context(self, project_id):
            raise AssertionError("completed receipts should bypass context loading")

    async def fail_assess(**kwargs):
        raise AssertionError("completed receipts should bypass model sizing")

    monkeypatch.setattr(executor_module, "assess_studio_effort_batch", fail_assess)
    await executor._size_linear_studio_prepared_candidates(
        prepared_candidates=[prepared],
        client=FakeClient(),
        labels=[],
        settings=SimpleNamespace(LINEAR_STUDIO_SIZING_MODE="required"),
        requester_slack_id="USAM",
    )

    assert prepared["receipt_replay_issue"]["identifier"] == "STU-1"
    assert prepared["display"]["effort_label"] == "Small (S)"


def test_issue_input_includes_effort_description_and_metadata():
    executor = SkillExecutor()
    candidate = executor._normalize_linear_meeting_candidate(
        {
            "title": "Send the run sheet",
            "description": "Prepare and send the run sheet.",
            "completed_work": "The run sheet is already drafted.",
            "remaining_work": "Check the dates and email Jess.",
            "available_artifacts": ["Draft run sheet"],
            "dependencies": ["Final event date"],
            "acceptance_criteria": ["Jess receives the final run sheet"],
        }
    )
    sizing_metadata = {
        "effort_label": "Extra Small (XS)",
        "rationale": "The remaining check and email should take about 15 minutes.",
    }

    issue_input = executor._build_linear_meeting_issue_input(
        candidate=candidate,
        owner_match={"user": {"id": "user-1"}},
        project_match={
            "project": {"id": "project-1", "name": "[Studio] Founder Games"}
        },
        team_match={"team": {"id": "team-1"}},
        label_ids=["meeting-action", "effort-xs"],
        source={"workspace_id": "T1", "channel_id": "C1", "source_message_ts": "1.1"},
        sizing_metadata=sizing_metadata,
    )

    assert issue_input["sizing_metadata"] == sizing_metadata
    assert issue_input["label_ids"] == ["meeting-action", "effort-xs"]
    assert "### Effort estimate" in issue_input["description"]
    assert "**Extra Small (XS)**" in issue_input["description"]
    assert "### Work already completed" in issue_input["description"]


@pytest.mark.asyncio
async def test_required_mode_creates_studio_issue_with_exact_effort_label(monkeypatch):
    executor = SkillExecutor()
    created_inputs = []
    team = {"id": "team-1", "key": "STU", "name": "Studio"}
    user = {
        "id": "user-1",
        "name": "Sam Donegan",
        "displayName": "Sam",
        "email": "sam@example.com",
    }
    project = {
        "id": "project-1",
        "name": "[Studio] Founder Games",
        "teams": {"nodes": [team]},
        "members": {"nodes": [user]},
    }

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="Slack command",
                    text="Create the final Founder Games run sheet and send it to Jess.",
                    kind="slack",
                )
            ]
        )

    async def fake_candidates(**kwargs):
        return [
            {
                "title": "Send the Founder Games run sheet to Jess",
                "description": "Check the final dates and send the drafted run sheet.",
                "work_status": "open",
                "completed_work": "The run sheet is already drafted.",
                "remaining_work": "Check the final dates and email the run sheet to Jess.",
                "available_artifacts": ["Draft run sheet"],
                "dependencies": [],
                "acceptance_criteria": ["Jess receives the final run sheet"],
                "owner_hint": "Sam",
                "project_hint": "[Studio] Founder Games",
                "evidence": "send me through the run sheet by EOW",
                "explicit_commitment": True,
                "source_label": "Slack command",
                "confidence": 0.96,
            }
        ]

    async def fake_assess(**kwargs):
        return {kwargs["candidates"][0]["candidate_key"]: _assessment(
            kwargs["candidates"][0]["candidate_key"]
        )}

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return [user]

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return [{"id": "meeting-action", "name": "meeting-action"}]

        async def list_recent_open_issues(self):
            return []

        async def get_project_sizing_context(self, project_id):
            return {
                "project": project,
                "projectUpdates": {"nodes": []},
                "activeIssues": {"nodes": []},
                "terminalReferences": {"nodes": []},
                "sizingPrecedents": {"nodes": []},
                "effortLabelRegistry": {
                    "nodes": [{"id": "effort-small", "name": "Small (S)"}]
                },
            }

        async def create_issue(self, **kwargs):
            created_inputs.append(kwargs)
            return {
                "identifier": "STU-42",
                "title": kwargs["title"],
                "url": "https://linear.test/STU-42",
                "sizingMetadata": kwargs["sizing_metadata"],
            }

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FakeClient

    monkeypatch.setattr(executor, "_build_linear_meeting_source_result", fake_source_result)
    monkeypatch.setattr(executor, "_extract_linear_direct_issue_candidates", fake_candidates)
    monkeypatch.setattr(executor_module, "assess_studio_effort_batch", fake_assess)
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
            LINEAR_CONTEXTUAL_AUTO_CREATE_ENABLED=True,
            LINEAR_STUDIO_SIZING_MODE="required",
            LINEAR_STUDIO_SIZING_AUTO_CREATE_MIN_CONFIDENCE=0.75,
            LINEAR_STUDIO_SIZING_CONTEXT_MAX_CHARS=40000,
            LINEAR_STUDIO_SIZING_RUBRIC_VERSION="studio-effort-v1",
        ),
    )

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text=(
            "Create a Linear task in project [Studio] Founder Games for Sam "
            "to send the final run sheet to Jess."
        ),
        params={"project_hint": "[Studio] Founder Games", "owner_hint": "Sam"},
        user_id="USAM",
        channel_id="C1",
        thread_ts="1.1",
        current_message_ts="1.2",
    )

    assert result["data"]["created_count"] == 1
    assert created_inputs[0]["label_ids"] == ["meeting-action", "effort-small"]
    assert created_inputs[0]["sizing_metadata"]["effort_label"] == "Small (S)"
    assert "### Effort estimate" in created_inputs[0]["description"]
    assert "Small (S)" in result["message"]


@pytest.mark.asyncio
async def test_required_mode_creates_recovered_candidates_and_skips_only_failure(
    monkeypatch,
):
    executor = SkillExecutor()
    created_inputs = []
    team = {"id": "team-1", "key": "STU", "name": "Studio"}
    user = {
        "id": "user-1",
        "name": "Sam Donegan",
        "displayName": "Sam",
        "email": "sam@example.com",
    }
    project = {
        "id": "project-1",
        "name": "[Studio] Aaron AI",
        "teams": {"nodes": [team]},
        "members": {"nodes": [user]},
    }

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="Slack meeting notes",
                    text="Create the first, second, and third implementation tasks.",
                    kind="slack",
                )
            ]
        )

    async def fake_candidates(**kwargs):
        return [
            {
                "title": title,
                "description": f"Complete {title.lower()}.",
                "work_status": "open",
                "completed_work": "",
                "remaining_work": f"Complete {title.lower()}.",
                "available_artifacts": [],
                "dependencies": [],
                "acceptance_criteria": [f"{title} is complete"],
                "owner_hint": "Sam",
                "project_hint": "[Studio] Aaron AI",
                "evidence": title,
                "explicit_commitment": True,
                "source_label": "Slack meeting notes",
                "confidence": 0.96,
            }
            for title in ("First task", "Second task", "Third task")
        ]

    async def fake_assess(**kwargs):
        candidates = kwargs["candidates"]
        titles = [candidate["title"] for candidate in candidates]
        if len(candidates) > 1:
            raise ToolCallParseError("structured_response_missing")
        if titles == ["Second task"]:
            raise ToolCallParseError("structured_response_missing")
        candidate_key = candidates[0]["candidate_key"]
        return {candidate_key: _assessment(candidate_key)}

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return [user]

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return [{"id": "meeting-action", "name": "meeting-action"}]

        async def list_recent_open_issues(self):
            return []

        async def get_project_sizing_context(self, project_id):
            return {
                "project": project,
                "projectUpdates": {"nodes": []},
                "activeIssues": {"nodes": []},
                "terminalReferences": {"nodes": []},
                "sizingPrecedents": {"nodes": []},
                "effortLabelRegistry": {
                    "nodes": [
                        {
                            "id": "effort-small",
                            "name": "Small (S)",
                        }
                    ]
                },
            }

        async def create_issue(self, **kwargs):
            created_inputs.append(kwargs)
            issue_number = len(created_inputs)
            return {
                "identifier": f"STU-{issue_number}",
                "title": kwargs["title"],
                "url": f"https://linear.test/STU-{issue_number}",
                "sizingMetadata": kwargs["sizing_metadata"],
            }

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FakeClient

    monkeypatch.setattr(
        executor,
        "_build_linear_meeting_source_result",
        fake_source_result,
    )
    monkeypatch.setattr(
        executor,
        "_extract_linear_direct_issue_candidates",
        fake_candidates,
    )
    monkeypatch.setattr(
        executor,
        "_extract_linear_meeting_candidates_from_sources",
        fake_candidates,
    )
    monkeypatch.setattr(executor_module, "assess_studio_effort_batch", fake_assess)
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
            LINEAR_CONTEXTUAL_AUTO_CREATE_ENABLED=True,
            LINEAR_STUDIO_SIZING_MODE="required",
            LINEAR_STUDIO_SIZING_AUTO_CREATE_MIN_CONFIDENCE=0.75,
            LINEAR_STUDIO_SIZING_CONTEXT_MAX_CHARS=40_000,
            LINEAR_STUDIO_SIZING_BATCH_SIZE=2,
            LINEAR_STUDIO_SIZING_RUBRIC_VERSION="studio-effort-v1",
        ),
    )

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text=(
            "Create these Linear tasks in project [Studio] Aaron AI and assign "
            "them to Sam."
        ),
        params={"project_hint": "[Studio] Aaron AI", "owner_hint": "Sam"},
        user_id="USAM",
        channel_id="C1",
        thread_ts="1.1",
        current_message_ts="1.2",
    )

    assert result["data"]["created_count"] == 2
    assert result["data"]["skipped_count"] == 1
    assert [item["title"] for item in created_inputs] == [
        "First task",
        "Third task",
    ]
    assert all(
        item["label_ids"] == ["meeting-action", "effort-small"]
        for item in created_inputs
    )
    assert all(
        item["sizing_metadata"]["effort_label"] == "Small (S)"
        for item in created_inputs
    )
    assert "Second task" in result["message"]
    assert "no unlabeled issue was created" in result["message"]
