from __future__ import annotations

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
    assess_studio_effort_batch,
    is_studio_project,
)
from roo.linear_meeting_sources import ParsedSource, SourceParseResult
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
