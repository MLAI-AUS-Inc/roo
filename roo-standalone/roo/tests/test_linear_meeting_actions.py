import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
sys.modules.pop("roo.skills.executor", None)

from roo.skills.executor import SkillExecutor
import roo.skills.executor as executor_module
from roo.linear_meeting_sources import ParsedSource, SourceParseResult


def test_linear_meeting_transcript_uses_thread_history_and_message():
    executor = SkillExecutor()

    transcript = executor._build_linear_meeting_transcript(
        "turn this meeting summary into Linear tasks",
        {},
        [
            {"user": "U1", "text": "Sam will update onboarding docs by Friday.", "is_bot": False},
            {"user": "B1", "text": "Bot message", "is_bot": True},
        ],
    )

    assert "U1: Sam will update onboarding docs by Friday." in transcript
    assert "Bot message" not in transcript
    assert "turn this meeting summary into Linear tasks" in transcript


def test_linear_meeting_owner_matches_slack_mention_email(monkeypatch):
    executor = SkillExecutor()

    monkeypatch.setattr(
        "roo.slack_client.get_user_info",
        lambda user_id: {"email": "sam@example.com"},
    )

    match = executor._match_linear_meeting_owner(
        "<@U123>",
        [
            {"id": "lin-user-1", "name": "Sam", "displayName": "Sam", "email": "sam@example.com"},
        ],
    )

    assert match["user"]["id"] == "lin-user-1"
    assert match["confidence"] == pytest.approx(0.98)


def test_linear_meeting_owner_matches_name_fallback():
    executor = SkillExecutor()

    match = executor._match_linear_meeting_owner(
        "Jane Doe",
        [
            {"id": "lin-user-1", "name": "Jane Doe", "displayName": "Jane", "email": "jane@example.com"},
        ],
    )

    assert match["user"]["id"] == "lin-user-1"
    assert match["confidence"] >= 0.9


def test_linear_meeting_project_matches_explicit_hint():
    executor = SkillExecutor()

    match = executor._match_linear_meeting_project(
        {"project_hint": "Alpha Launch"},
        [
            {"id": "proj-1", "name": "Alpha Launch", "slugId": "alpha-launch"},
        ],
        owner_user=None,
    )

    assert match["project"]["id"] == "proj-1"
    assert match["confidence"] >= 0.9


def test_linear_meeting_project_hint_prepass_extracts_named_project():
    executor = SkillExecutor()

    params = executor._apply_linear_meeting_project_hint_prepass(
        '@Roo add this to the Linear project called “BITGET EVENT”',
        {},
    )

    assert params["project_hint"] == "BITGET EVENT"


@pytest.mark.asyncio
async def test_linear_thread_reference_source_excludes_current_command():
    executor = SkillExecutor()

    result = await executor._build_linear_meeting_source_result(
        text='@Roo add this to the Linear project called "BITGET EVENT"',
        params={},
        thread_history=[
            {
                "user": "U1",
                "text": "<@UYANA> should decide whether the event name leans into finance bro stereotypes.",
                "ts": "1.1",
                "is_bot": False,
            },
            {"user": "B1", "text": "Bot message", "ts": "1.2", "is_bot": True},
            {
                "user": "U2",
                "text": '@Roo add this to the Linear project called "BITGET EVENT"',
                "ts": "1.3",
                "is_bot": False,
            },
        ],
        event_files=[],
        settings=SimpleNamespace(OPENAI_API_KEY=None),
        current_message_ts="1.3",
        exclude_current_message=True,
    )

    combined = result.combined_text()
    assert "finance bro stereotypes" in combined
    assert "Bot message" not in combined
    assert "@Roo add this" not in combined


def test_linear_meeting_duplicate_detection_uses_similar_open_issue_title():
    executor = SkillExecutor()

    duplicate = executor._find_linear_meeting_duplicate(
        {"title": "Update onboarding documentation"},
        [
            {
                "id": "issue-1",
                "identifier": "ENG-12",
                "title": "Update onboarding docs",
                "url": "https://linear.app/acme/issue/ENG-12",
                "project": {"id": "proj-1"},
            }
        ],
        {"id": "proj-1"},
    )

    assert duplicate["identifier"] == "ENG-12"


def test_linear_meeting_candidate_dedupe_merges_semantic_duplicates():
    executor = SkillExecutor()

    candidates = executor._dedupe_linear_meeting_candidates(
        [
            {
                "title": "Recruit technical talent for the bounty pool",
                "owner_hint": "Sonia",
                "confidence": 0.72,
                "source_label": "notes.pdf",
            },
            {
                "title": "Start building the vetted tech talent pool",
                "owner_hint": "Sonia",
                "confidence": 0.8,
                "source_label": "notes.pdf page 4",
            },
            {
                "title": "Post a project update in Linear",
                "confidence": 0.9,
            },
        ]
    )

    assert len(candidates) == 1
    assert candidates[0]["confidence"] == pytest.approx(0.8)
    assert "notes.pdf" in candidates[0]["source_label"]


def test_linear_meeting_decision_thresholds():
    executor = SkillExecutor()
    candidate = {"confidence": 0.9}
    owner = {"confidence": 0.9}
    project = {"confidence": 0.88}
    team = {"confidence": 0.96}

    decision, confidence = executor._linear_meeting_candidate_decision(
        candidate=candidate,
        owner_match=owner,
        project_match=project,
        team_match=team,
        duplicate=None,
        auto_threshold=0.85,
        uncertain_threshold=0.65,
    )
    assert decision == "create"
    assert confidence == pytest.approx(0.88)

    decision, confidence = executor._linear_meeting_candidate_decision(
        candidate={"confidence": 0.75},
        owner_match=owner,
        project_match=project,
        team_match=team,
        duplicate=None,
        auto_threshold=0.85,
        uncertain_threshold=0.65,
    )
    assert decision == "review"
    assert confidence == pytest.approx(0.75)

    decision, _ = executor._linear_meeting_candidate_decision(
        candidate={"confidence": 0.6},
        owner_match=owner,
        project_match=project,
        team_match=team,
        duplicate=None,
        auto_threshold=0.85,
        uncertain_threshold=0.65,
    )
    assert decision == "skip"


@pytest.mark.asyncio
async def test_linear_client_reads_context_from_backend(monkeypatch):
    module_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "linear_meeting_actions"
        / "client.py"
    )
    spec = importlib.util.spec_from_file_location("linear_meeting_actions_client_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "teams": [{"id": "team-1"}],
                "users": [{"id": "user-1"}],
                "projects": [{"id": "project-1"}],
                "labels": [{"id": "label-1"}],
                "recentIssues": [{"id": "issue-1"}],
            }

    calls = []

    class FakeBackend:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def _request(self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            return FakeResponse()

    monkeypatch.setattr(module, "MLAIBackendClient", FakeBackend)

    client = module.LinearMeetingActionsClient(base_url="https://backend.test", api_key="roo-key")
    teams, users, projects, labels, recent_issues = await asyncio.gather(
        client.list_teams(),
        client.list_users(),
        client.list_active_projects(),
        client.list_issue_labels(),
        client.list_recent_open_issues(),
    )

    assert teams == [{"id": "team-1"}]
    assert users == [{"id": "user-1"}]
    assert projects == [{"id": "project-1"}]
    assert labels == [{"id": "label-1"}]
    assert recent_issues == [{"id": "issue-1"}]
    assert len(calls) == 1
    assert calls[0][0] == "GET"
    assert calls[0][1] == "/api/v1/integrations/linear/meeting-context"
    assert calls[0][2]["use_admin_headers"] is True


@pytest.mark.asyncio
async def test_linear_client_create_issue_calls_backend(monkeypatch):
    module_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "linear_meeting_actions"
        / "client.py"
    )
    spec = importlib.util.spec_from_file_location("linear_meeting_actions_client_create_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class FakeResponse:
        status_code = 201

        def json(self):
            return {"identifier": "ENG-123", "title": "Update onboarding docs"}

    calls = []

    class FakeBackend:
        def __init__(self, **kwargs):
            pass

        async def _request(self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            return FakeResponse()

    monkeypatch.setattr(module, "MLAIBackendClient", FakeBackend)

    client = module.LinearMeetingActionsClient(base_url="https://backend.test", api_key="roo-key")
    issue = await client.create_issue(
        title="Update onboarding docs",
        team_id="team-1",
        description="Meeting task",
        assignee_id="user-1",
        project_id="project-1",
        priority=2,
        due_date="2026-05-08",
        label_ids=["label-1"],
    )

    assert issue["identifier"] == "ENG-123"
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/api/v1/integrations/linear/issues"
    assert calls[0][2]["use_admin_headers"] is True
    assert calls[0][2]["json"] == {
        "title": "Update onboarding docs",
        "team_id": "team-1",
        "description": "Meeting task",
        "assignee_id": "user-1",
        "project_id": "project-1",
        "priority": 2,
        "due_date": "2026-05-08",
        "label_ids": ["label-1"],
    }


@pytest.mark.asyncio
async def test_linear_client_create_project_update_calls_backend(monkeypatch):
    module_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "linear_meeting_actions"
        / "client.py"
    )
    spec = importlib.util.spec_from_file_location("linear_meeting_actions_client_project_update_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class FakeResponse:
        status_code = 201

        def json(self):
            return {"id": "update-1", "url": "https://linear.test/update-1"}

    calls = []

    class FakeBackend:
        def __init__(self, **kwargs):
            pass

        async def _request(self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            return FakeResponse()

    monkeypatch.setattr(module, "MLAIBackendClient", FakeBackend)

    client = module.LinearMeetingActionsClient(base_url="https://backend.test", api_key="roo-key")
    project_update = await client.create_project_update(
        project_id="project-1",
        body="Meeting update",
        health="onTrack",
    )

    assert project_update["id"] == "update-1"
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/api/v1/integrations/linear/project-updates"
    assert calls[0][2]["json"] == {
        "project_id": "project-1",
        "body": "Meeting update",
        "health": "onTrack",
    }


@pytest.mark.asyncio
async def test_linear_client_backend_error_surfaces_detail(monkeypatch, capsys):
    module_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "linear_meeting_actions"
        / "client.py"
    )
    spec = importlib.util.spec_from_file_location("linear_meeting_actions_client_error_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class FakeResponse:
        status_code = 502

        def json(self):
            return {
                "detail": 'Cannot query field "state" on type "Project".',
                "code": "linear_graphql_error",
                "operation": "LinearProjects",
            }

    class FakeBackend:
        def __init__(self, **kwargs):
            pass

        async def _request(self, method, endpoint, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(module, "MLAIBackendClient", FakeBackend)

    client = module.LinearMeetingActionsClient(base_url="https://backend.test", api_key="roo-key")
    with pytest.raises(RuntimeError, match="LinearProjects"):
        await client.list_teams()
    captured = capsys.readouterr()
    assert "Linear meeting backend error" in captured.out
    assert 'Cannot query field "state"' in captured.out


@pytest.mark.asyncio
async def test_linear_meeting_executor_surfaces_backend_context_detail(monkeypatch):
    executor = SkillExecutor()

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="Slack thread",
                    text="Sam will update onboarding docs in Alpha after the meeting.",
                    kind="slack",
                )
            ]
        )

    class FailingClient:
        async def list_teams(self):
            raise RuntimeError(
                'Cannot query field "state" on type "Project". '
                "(linear_graphql_error; LinearProjects)"
            )

        async def list_users(self):
            return []

        async def list_active_projects(self):
            return []

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FailingClient

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
        ),
    )
    monkeypatch.setattr(executor, "_build_linear_meeting_source_result", fake_source_result)

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text="turn this meeting summary into Linear tasks",
        params={},
        user_id="U1",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
        event_files=[],
    )

    assert 'Cannot query field "state"' in result["message"]
    assert "LinearProjects" in result["message"]
    assert "RuntimeError:" not in result["message"]


@pytest.mark.asyncio
async def test_linear_meeting_candidate_extraction_chunks_sources(monkeypatch):
    executor = SkillExecutor()
    calls = []

    async def fake_extract(*, transcript, params, users, projects, source_label=None):
        calls.append(transcript)
        return [
            {
                "title": "Update onboarding docs",
                "owner_hint": "Sam",
                "source_label": source_label,
                "confidence": 0.8,
            }
        ]

    monkeypatch.setattr(executor, "_extract_linear_meeting_candidates", fake_extract)
    long_text = ("Sam will update onboarding docs after the meeting.\n\n" * 350).strip()

    candidates = await executor._extract_linear_meeting_candidates_from_sources(
        sources=[ParsedSource(label="notes.pdf", text=long_text, kind="pdf")],
        params={},
        users=[],
        projects=[],
    )

    assert len(calls) > 1
    assert all(call.startswith("Source: notes.pdf") for call in calls)
    assert all(len(call) <= 10100 for call in calls)
    assert len(candidates) == 1
    assert candidates[0]["source_label"] == "notes.pdf"


@pytest.mark.asyncio
async def test_linear_meeting_executor_creates_issue_from_parsed_file_source(monkeypatch):
    executor = SkillExecutor()
    created_inputs = []

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="meeting.pdf page 3",
                    text="Sam will update onboarding docs in Alpha.",
                    kind="pdf",
                )
            ]
        )

    async def fake_candidates_from_sources(**kwargs):
        assert kwargs["sources"][0].label == "meeting.pdf page 3"
        return [
            {
                "title": "Update onboarding docs",
                "description": "Update the onboarding docs from the meeting.",
                "owner_hint": "Sam",
                "project_hint": "Alpha",
                "team_hint": "ENG",
                "evidence": "Sam will update onboarding docs in Alpha.",
                "source_label": "meeting.pdf page 3",
                "confidence": 0.95,
            }
        ]

    team = {"id": "team-1", "key": "ENG", "name": "Engineering"}
    user = {"id": "user-1", "name": "Sam", "displayName": "Sam", "email": "sam@example.com"}
    project = {
        "id": "project-1",
        "name": "Alpha",
        "teams": {"nodes": [team]},
        "members": {"nodes": [user]},
    }

    class FakeClient:
        def __init__(self):
            pass

        async def list_teams(self):
            return [team]

        async def list_users(self):
            return [user]

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return [{"id": "label-1", "name": "meeting-action"}]

        async def list_recent_open_issues(self):
            return []

        async def create_issue(self, **kwargs):
            created_inputs.append(kwargs)
            return {
                "identifier": "ENG-123",
                "title": kwargs["title"],
                "url": "https://linear.app/acme/issue/ENG-123",
            }

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FakeClient

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
        ),
    )
    monkeypatch.setattr(executor, "_build_linear_meeting_source_result", fake_source_result)
    monkeypatch.setattr(
        executor,
        "_extract_linear_meeting_candidates_from_sources",
        fake_candidates_from_sources,
    )

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text="send this PDF to Linear as tasks",
        params={},
        user_id="U1",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
        event_files=[{"id": "F1", "name": "meeting.pdf"}],
    )

    assert "data" in result, result
    assert result["data"]["created_count"] == 1
    assert created_inputs[0]["title"] == "Update onboarding docs"
    assert created_inputs[0]["assignee_id"] == "user-1"
    assert created_inputs[0]["project_id"] == "project-1"
    assert created_inputs[0]["label_ids"] == ["label-1"]
    assert "meeting.pdf page 3" in created_inputs[0]["description"]


@pytest.mark.asyncio
async def test_linear_meeting_executor_creates_project_update_when_requested(monkeypatch):
    executor = SkillExecutor()
    created_updates = []

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="Bounty_Venture Studio kick off call - notes.pdf",
                    text="Sonia will validate the bounty model with VCs. Sam will share the recording.",
                    kind="pdf",
                )
            ],
            files_seen=1,
            files_parsed=1,
        )

    async def fake_candidates_from_sources(**kwargs):
        return [
            {
                "title": "Validate the bounty model with VCs",
                "owner_hint": "Sonia",
                "project_hint": "Bounties / Venture Studio",
                "confidence": 0.8,
            }
        ]

    team = {"id": "team-1", "key": "MLA", "name": "MLAI"}
    user = {"id": "user-1", "name": "Sonia", "displayName": "sonia1", "email": "sonia@example.com"}
    project = {
        "id": "project-1",
        "name": "Bounties / Venture Studio",
        "teams": {"nodes": [team]},
        "members": {"nodes": [user]},
    }

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return [user]

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

        async def create_project_update(self, **kwargs):
            created_updates.append(kwargs)
            return {
                "id": "update-1",
                "url": "https://linear.app/acme/project-update/update-1",
                "project": {"name": "Bounties / Venture Studio"},
            }

        async def create_issue(self, **kwargs):
            return {"identifier": "MLA-1", "title": kwargs["title"]}

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FakeClient

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
            LINEAR_MEETING_LLM_MODEL="gpt-5.5",
            LINEAR_MEETING_LLM_REASONING_EFFORT="low",
        ),
    )
    monkeypatch.setattr(executor, "_build_linear_meeting_source_result", fake_source_result)
    monkeypatch.setattr(executor, "_extract_linear_meeting_candidates_from_sources", fake_candidates_from_sources)
    async def fake_project_update_input(**kwargs):
        return {
            "project_id": "project-1",
            "body": "Meeting update",
            "health": "onTrack",
        }

    monkeypatch.setattr(executor, "_build_linear_meeting_project_update_input", fake_project_update_input)

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text="Please do a project update in Linear and extract to-dos",
        params={},
        user_id="U1",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
        event_files=[{"id": "F1", "name": "Bounty_Venture Studio kick off call - notes.pdf"}],
    )

    assert created_updates == [{"project_id": "project-1", "body": "Meeting update", "health": "onTrack"}]
    assert "Created Linear project update" in result["message"]


@pytest.mark.asyncio
async def test_linear_thread_reference_fallback_requires_review(monkeypatch):
    executor = SkillExecutor()
    executor_module.LINEAR_MEETING_PENDING_ACTIONS.clear()
    create_calls = []

    async def fake_source_result(**kwargs):
        assert kwargs["exclude_current_message"] is True
        assert kwargs["current_message_ts"] == "1.2"
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="Slack thread",
                    text="<@UYANA> should decide whether the AI Meets Markets event leans into finance bro stereotypes.",
                    kind="slack_text",
                )
            ]
        )

    async def no_concrete_candidates(**kwargs):
        return []

    async def contextual_candidate(**kwargs):
        return {
            "title": "Decide AI Meets Markets event positioning and naming",
            "description": "Decide whether the event should lean into finance bro stereotypes and confirm the name.",
            "owner_hint": "<@UYANA>",
            "project_hint": "BITGET EVENT",
            "evidence": "Can we go full finance bro...",
            "source_label": "Slack thread",
            "confidence": 0.95,
            "contextual_review_only": True,
        }

    team = {"id": "team-1", "key": "MKT", "name": "Marketing"}
    user = {"id": "user-1", "name": "Yana", "displayName": "Yana", "email": "yana@example.com"}
    project = {
        "id": "project-1",
        "name": "BITGET EVENT",
        "teams": {"nodes": [team]},
        "members": {"nodes": [user]},
    }

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return [user]

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

        async def create_issue(self, **kwargs):
            create_calls.append(kwargs)
            return {"identifier": "MKT-1", "title": kwargs["title"]}

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FakeClient

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
        ),
    )
    monkeypatch.setattr("roo.slack_client.get_user_info", lambda user_id: {"email": "yana@example.com"})
    monkeypatch.setattr(executor, "_build_linear_meeting_source_result", fake_source_result)
    monkeypatch.setattr(executor, "_extract_linear_meeting_candidates_from_sources", no_concrete_candidates)
    monkeypatch.setattr(executor, "_extract_linear_thread_context_candidate", contextual_candidate)

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text='@Roo add this to the Linear project called "BITGET EVENT"',
        params={},
        user_id="UASKER",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
        event_files=[],
        current_message_ts="1.2",
    )

    assert create_calls == []
    assert result["data"]["created_count"] == 0
    assert result["data"]["review_count"] == 1
    assert "Please review before I create it" in result["message"]
    assert result["blocks"]
    assert "Can we go full finance bro" in str(result["blocks"])
    pending = next(iter(executor_module.LINEAR_MEETING_PENDING_ACTIONS.values()))
    assert pending["issue_input"]["title"] == "Decide AI Meets Markets event positioning and naming"
    assert pending["issue_input"]["project_id"] == "project-1"
    assert pending["issue_input"]["assignee_id"] == "user-1"


@pytest.mark.asyncio
async def test_linear_thread_reference_fallback_skips_when_assignee_unresolved(monkeypatch):
    executor = SkillExecutor()

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="Slack thread",
                    text="The team discussed whether AI Meets Markets should use a finance bro theme.",
                    kind="slack_text",
                )
            ]
        )

    async def no_concrete_candidates(**kwargs):
        return []

    async def contextual_candidate(**kwargs):
        return {
            "title": "Decide AI Meets Markets event positioning and naming",
            "description": "Confirm the event positioning and name.",
            "owner_hint": "Someone",
            "project_hint": "BITGET EVENT",
            "evidence": "finance bro theme",
            "source_label": "Slack thread",
            "confidence": 0.95,
            "contextual_review_only": True,
        }

    team = {"id": "team-1", "key": "MKT", "name": "Marketing"}
    project = {
        "id": "project-1",
        "name": "BITGET EVENT",
        "teams": {"nodes": [team]},
        "members": {"nodes": []},
    }

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return []

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

        async def create_issue(self, **kwargs):
            raise AssertionError("contextual issue with unresolved owner should not be created")

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FakeClient

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
        ),
    )
    monkeypatch.setattr(executor, "_build_linear_meeting_source_result", fake_source_result)
    monkeypatch.setattr(executor, "_extract_linear_meeting_candidates_from_sources", no_concrete_candidates)
    monkeypatch.setattr(executor, "_extract_linear_thread_context_candidate", contextual_candidate)

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text='@Roo add this to the Linear project called "BITGET EVENT"',
        params={},
        user_id="UASKER",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
        event_files=[],
        current_message_ts="1.2",
    )

    assert result["data"]["review_count"] == 0
    assert result["data"]["skipped_count"] == 1
    assert "Assignee unclear" in result["message"]


@pytest.mark.asyncio
async def test_linear_meeting_slack_approval_creates_contextual_issue(monkeypatch):
    import importlib
    import roo.main as main_module

    live_executor_module = importlib.import_module("roo.skills.executor")
    live_executor_module.LINEAR_MEETING_PENDING_ACTIONS.clear()
    pending_id = "pending-contextual-1"
    issue_input = {
        "title": "Decide AI Meets Markets event positioning and naming",
        "team_id": "team-1",
        "description": "Meeting action description",
        "assignee_id": "user-1",
        "project_id": "project-1",
        "priority": 3,
        "due_date": None,
        "label_ids": [],
    }
    live_executor_module.LINEAR_MEETING_PENDING_ACTIONS[pending_id] = {
        "requested_by": "UASKER",
        "issue_input": issue_input,
        "display": {"title": issue_input["title"]},
        "reason": "Needs approval",
    }
    create_calls = []
    posted_messages = []

    class FakeClient:
        async def create_issue(self, **kwargs):
            create_calls.append(kwargs)
            return {
                "identifier": "MKT-42",
                "title": kwargs["title"],
                "url": "https://linear.test/MKT-42",
            }

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FakeClient

    class FakeAgent:
        def _get_skill_by_name(self, name):
            assert name == "linear-meeting-actions"
            return FakeSkill()

    class FakeRequest:
        async def form(self):
            return {
                "payload": json.dumps(
                    {
                        "user": {"id": "UASKER"},
                        "channel": {"id": "C1"},
                        "message": {"ts": "1.2", "thread_ts": "1.1"},
                        "actions": [
                            {
                                "action_id": "linear_meeting_approve",
                                "value": json.dumps(
                                    {"pending_id": pending_id, "requested_by": "UASKER"}
                                ),
                            }
                        ],
                    }
                )
            }

    monkeypatch.setattr(main_module, "get_agent", lambda: FakeAgent())
    monkeypatch.setattr(main_module, "post_message", lambda **kwargs: posted_messages.append(kwargs))

    response = await main_module.slack_actions(FakeRequest())

    assert response.status_code == 200
    assert create_calls == [issue_input]
    assert pending_id not in live_executor_module.LINEAR_MEETING_PENDING_ACTIONS
    assert "Created <https://linear.test/MKT-42|MKT-42>" in posted_messages[0]["text"]


@pytest.mark.asyncio
async def test_linear_meeting_slack_reject_clears_contextual_issue(monkeypatch):
    import importlib
    import roo.main as main_module

    live_executor_module = importlib.import_module("roo.skills.executor")
    live_executor_module.LINEAR_MEETING_PENDING_ACTIONS.clear()
    pending_id = "pending-contextual-2"
    live_executor_module.LINEAR_MEETING_PENDING_ACTIONS[pending_id] = {
        "requested_by": "UASKER",
        "issue_input": {"title": "Decide event positioning"},
        "display": {"title": "Decide event positioning"},
        "reason": "Needs approval",
    }
    posted_messages = []

    class FakeRequest:
        async def form(self):
            return {
                "payload": json.dumps(
                    {
                        "user": {"id": "UASKER"},
                        "channel": {"id": "C1"},
                        "message": {"ts": "1.2", "thread_ts": "1.1"},
                        "actions": [
                            {
                                "action_id": "linear_meeting_reject",
                                "value": json.dumps(
                                    {"pending_id": pending_id, "requested_by": "UASKER"}
                                ),
                            }
                        ],
                    }
                )
            }

    monkeypatch.setattr(main_module, "post_message", lambda **kwargs: posted_messages.append(kwargs))

    response = await main_module.slack_actions(FakeRequest())

    assert response.status_code == 200
    assert pending_id not in live_executor_module.LINEAR_MEETING_PENDING_ACTIONS
    assert posted_messages[0]["text"] == "Skipped Linear issue creation for: Decide event positioning"
