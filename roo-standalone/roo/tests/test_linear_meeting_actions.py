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


def test_linear_meeting_owner_matches_unique_plain_first_name():
    executor = SkillExecutor()

    match = executor._match_linear_meeting_owner(
        "Sonia",
        [
            {"id": "lin-user-1", "name": "Sonia Kaurah", "displayName": "sonia1", "email": "sonia@example.com"},
            {"id": "lin-user-2", "name": "Sam Donegan", "displayName": "Sam", "email": "sam@example.com"},
        ],
    )

    assert match["user"]["id"] == "lin-user-1"
    assert match["confidence"] >= 0.9


def test_linear_meeting_owner_reports_ambiguous_plain_name():
    executor = SkillExecutor()

    match = executor._match_linear_meeting_owner(
        "Sonia",
        [
            {"id": "lin-user-1", "name": "Sonia Kaurah", "displayName": "sonia1", "email": "sonia@example.com"},
            {"id": "lin-user-2", "name": "Sonia Lee", "displayName": "sonia2", "email": "sonia.lee@example.com"},
        ],
    )

    assert match["user"] is None
    assert "Ambiguous" in match["reason"]


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


def test_linear_meeting_project_matches_semantic_context_and_linked_channel():
    executor = SkillExecutor()
    project = {
        "id": "proj-founder-program",
        "name": "Founder Program 2026",
        "description": "Applications, operations, and support for Founder Games.",
        "content": "Run sheets and participant experience for the Founder Games event.",
        "slackChannelId": "CFOUNDERS",
    }

    match = executor._match_linear_meeting_project(
        {"project_hint": "Founder Games"},
        [project, {"id": "proj-other", "name": "Website Refresh"}],
        owner_user=None,
        channel_id="CFOUNDERS",
    )

    assert match["project"]["id"] == "proj-founder-program"
    assert match["confidence"] >= 0.9


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('@Roo add this to the Linear project called “BITGET EVENT”', "BITGET EVENT"),
        ("create a to do item in the linear project 'venture studio' assign to Sonia", "venture studio"),
        ('create an issue in Linear project "Venture Studio" assigned to <@U123>', "Venture Studio"),
        ("add a Linear task to Linear project Venture Studio for Sonia", "Venture Studio"),
        ("sync this to project called BITGET EVENT please", "BITGET EVENT"),
    ],
)
def test_linear_meeting_project_hint_prepass_extracts_project_forms(text, expected):
    executor = SkillExecutor()

    params = executor._apply_linear_meeting_project_hint_prepass(text, {})

    assert params["project_hint"] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("create a Linear issue in project Alpha assign to Sonia", "Sonia"),
        ("create a Linear issue in project Alpha assigned to Sonia Kaurah", "Sonia Kaurah"),
        ("create an issue in Linear project Alpha assigned to <@U123>", "<@U123>"),
        ("add a Linear task to project Alpha for Sonia", "Sonia"),
    ],
)
def test_linear_meeting_owner_hint_prepass_extracts_assignment_forms(text, expected):
    executor = SkillExecutor()

    params = executor._apply_linear_meeting_owner_hint_prepass(text, {})

    assert params["owner_hint"] == expected


def test_linear_direct_issue_body_uses_command_work_not_linear_meta_instruction():
    executor = SkillExecutor()

    body = executor._extract_linear_direct_issue_body(
        "create a to do item in the linear project 'venture studio' to rebrand and change the name of this project. assign to Sonia",
        {"project_hint": "venture studio", "owner_hint": "Sonia"},
    )

    assert body == "rebrand and change the name of this project"


def test_linear_direct_issue_project_hint_can_be_inferred_from_known_project():
    executor = SkillExecutor()

    project_hint = executor._infer_linear_direct_project_hint_from_known_projects(
        "add a Linear task to Venture Studio to rename it for Sonia",
        [{"id": "project-1", "name": "Venture Studio", "slugId": "venture-studio"}],
    )
    body = executor._extract_linear_direct_issue_body(
        "add a Linear task to Venture Studio to rename it for Sonia",
        {"project_hint": project_hint, "owner_hint": "Sonia"},
    )

    assert project_hint == "Venture Studio"
    assert body == "rename it"


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
async def test_linear_client_reads_project_sizing_context(monkeypatch):
    module_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "linear_meeting_actions"
        / "client.py"
    )
    spec = importlib.util.spec_from_file_location("linear_sizing_context_client_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"project": {"id": "project-1", "name": "[Studio] Founder Games"}}

    class FakeBackend:
        def __init__(self, **kwargs):
            pass

        async def _request(self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            return FakeResponse()

    monkeypatch.setattr(module, "MLAIBackendClient", FakeBackend)
    client = module.LinearMeetingActionsClient()

    context = await client.get_project_sizing_context("project-1")

    assert context["project"]["name"] == "[Studio] Founder Games"
    assert calls[0][1] == (
        "/api/v1/integrations/linear/projects/project-1/sizing-context"
    )


@pytest.mark.asyncio
async def test_linear_client_resolves_concurrent_create_from_receipt(monkeypatch):
    module_path = (
        Path(__file__).resolve().parents[2]
        / "skills"
        / "linear_meeting_actions"
        / "client.py"
    )
    spec = importlib.util.spec_from_file_location("linear_receipt_client_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    responses = [
        FakeResponse(
            409,
            {
                "detail": "An identical creation is in progress.",
                "code": "linear_issue_creation_in_progress",
            },
        ),
        FakeResponse(
            200,
            {
                "status": "completed",
                "issue": {
                    "identifier": "STU-1",
                    "title": "Send the run sheet",
                    "sizingMetadata": {"effortLabel": "Small (S)"},
                },
            },
        ),
    ]

    class FakeBackend:
        def __init__(self, **kwargs):
            pass

        async def _request(self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            return responses.pop(0)

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(module, "MLAIBackendClient", FakeBackend)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    client = module.LinearMeetingActionsClient()

    issue = await client.create_issue(
        title="Send the run sheet",
        team_id="team-1",
        project_id="project-1",
        label_ids=["effort-small"],
        idempotency_key="a" * 64,
        sizing_metadata={"effortLabel": "Small (S)"},
    )

    assert issue["identifier"] == "STU-1"
    assert issue["idempotentReplay"] is True
    assert calls[0][1] == "/api/v1/integrations/linear/issues"
    assert calls[1][1].endswith("/" + ("a" * 64))


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
async def test_linear_meeting_executor_queues_review_from_parsed_file_source(monkeypatch):
    executor = SkillExecutor()
    executor_module.LINEAR_MEETING_PENDING_ACTIONS.clear()
    created_inputs = []

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="meeting.pdf page 3",
                    text="Sam will update onboarding docs in Alpha.",
                    kind="pdf",
                )
            ],
            files_seen=1,
            files_parsed=1,
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

    # Extraction path is "review first": nothing is auto-created; the fully-resolved
    # issue is stashed for Slack Approve/Reject instead.
    assert "data" in result, result
    assert result["data"]["created_count"] == 0
    assert result["data"]["review_count"] == 1
    assert created_inputs == []

    pending = list(executor_module.LINEAR_MEETING_PENDING_ACTIONS.values())
    assert len(pending) == 1
    issue_input = pending[0]["issue_input"]
    assert issue_input["title"] == "Update onboarding docs"
    assert issue_input["assignee_id"] == "user-1"
    assert issue_input["project_id"] == "project-1"
    assert issue_input["label_ids"] == ["label-1"]
    assert "meeting.pdf page 3" in issue_input["description"]


@pytest.mark.asyncio
async def test_linear_direct_issue_command_creates_immediately(monkeypatch):
    executor = SkillExecutor()
    created_inputs = []

    async def fake_chat(messages, **kwargs):
        prompt = messages[-1]["content"]
        assert "directly asking Roo to create Linear issue" in prompt
        return SimpleNamespace(
            content=json.dumps(
                {
                    "issues": [
                        {
                            "title": "Rebrand and rename the Venture Studio project",
                            "description": "Rebrand and rename the project because Venture Studio is confusing and does not describe the offering.",
                            "owner_hint": "Sonia",
                            "project_hint": "venture studio",
                            "evidence": "rebrand and change the name",
                            "source_label": "Slack command",
                            "confidence": 0.96,
                        }
                    ]
                }
            )
        )

    async def fail_meeting_extraction(**kwargs):
        raise AssertionError("direct issue commands should not use meeting transcript extraction")

    team = {"id": "team-1", "key": "MLA", "name": "MLAI"}
    user = {"id": "user-1", "name": "Sonia Kaurah", "displayName": "sonia1", "email": "sonia@example.com"}
    project = {
        "id": "project-1",
        "name": "Venture Studio",
        "slugId": "venture-studio",
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
            created_inputs.append(kwargs)
            return {"identifier": "MLA-123", "title": kwargs["title"], "url": "https://linear.test/MLA-123"}

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FakeClient

    monkeypatch.setattr(executor_module, "chat", fake_chat)
    monkeypatch.setattr(executor, "_extract_linear_meeting_candidates_from_sources", fail_meeting_extraction)
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            OPENAI_API_KEY=None,
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
            LINEAR_MEETING_LLM_MODEL="gpt-5.5",
            LINEAR_MEETING_LLM_REASONING_EFFORT="low",
        ),
    )

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text=(
            "create a to do item in the linear project 'venture studio' to rebrand and change "
            "the name of this project as the name 'venture studio' is confusing and doesnt "
            "describe the offering. assign to Sonia"
        ),
        params={},
        user_id="U1",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
    )

    assert result["data"]["created_count"] == 1
    assert created_inputs[0]["title"] == "Rebrand and rename the Venture Studio project"
    assert created_inputs[0]["assignee_id"] == "user-1"
    assert created_inputs[0]["project_id"] == "project-1"
    assert created_inputs[0]["team_id"] == "team-1"
    assert "Slack command" in created_inputs[0]["description"]


@pytest.mark.asyncio
async def test_linear_direct_issue_command_reports_ambiguous_project(monkeypatch):
    executor = SkillExecutor()
    created_inputs = []

    async def fake_chat(messages, **kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "issues": [
                        {
                            "title": "Rename Venture Studio",
                            "description": "Rename the Venture Studio project.",
                            "owner_hint": "Sonia",
                            "project_hint": "venture studio",
                            "confidence": 0.96,
                        }
                    ]
                }
            )
        )

    team = {"id": "team-1", "key": "MLA", "name": "MLAI"}
    user = {"id": "user-1", "name": "Sonia Kaurah", "displayName": "sonia1", "email": "sonia@example.com"}
    projects = [
        {"id": "project-1", "name": "Venture Studio", "slugId": "venture-studio-a", "teams": {"nodes": [team]}},
        {"id": "project-2", "name": "Venture Studio", "slugId": "venture-studio-b", "teams": {"nodes": [team]}},
    ]

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return [user]

        async def list_active_projects(self):
            return projects

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

        async def create_issue(self, **kwargs):
            created_inputs.append(kwargs)
            return {"identifier": "MLA-123"}

    class FakeSkill:
        def get_client_class(self, name):
            return FakeClient

    monkeypatch.setattr(executor_module, "chat", fake_chat)
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            OPENAI_API_KEY=None,
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
            LINEAR_MEETING_LLM_MODEL="gpt-5.5",
            LINEAR_MEETING_LLM_REASONING_EFFORT="low",
        ),
    )

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text="create a to do item in the linear project 'venture studio' to rename it. assign to Sonia",
        params={},
        user_id="U1",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
    )

    assert created_inputs == []
    assert "Project unclear: multiple Linear projects matched" in result["message"]
    assert "project: Unresolved; assignee: sonia1" in result["message"]


@pytest.mark.asyncio
async def test_linear_direct_issue_command_reports_ambiguous_assignee(monkeypatch):
    executor = SkillExecutor()
    created_inputs = []

    async def fake_chat(messages, **kwargs):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "issues": [
                        {
                            "title": "Rename Venture Studio",
                            "description": "Rename the Venture Studio project.",
                            "owner_hint": "Sonia",
                            "project_hint": "venture studio",
                            "confidence": 0.96,
                        }
                    ]
                }
            )
        )

    team = {"id": "team-1", "key": "MLA", "name": "MLAI"}
    users = [
        {"id": "user-1", "name": "Sonia Kaurah", "displayName": "sonia1", "email": "sonia@example.com"},
        {"id": "user-2", "name": "Sonia Lee", "displayName": "sonia2", "email": "sonia.lee@example.com"},
    ]
    project = {"id": "project-1", "name": "Venture Studio", "slugId": "venture-studio", "teams": {"nodes": [team]}}

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return users

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

        async def create_issue(self, **kwargs):
            created_inputs.append(kwargs)
            return {"identifier": "MLA-123"}

    class FakeSkill:
        def get_client_class(self, name):
            return FakeClient

    monkeypatch.setattr(executor_module, "chat", fake_chat)
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            OPENAI_API_KEY=None,
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
            LINEAR_MEETING_LLM_MODEL="gpt-5.5",
            LINEAR_MEETING_LLM_REASONING_EFFORT="low",
        ),
    )

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text="create a to do item in the linear project 'venture studio' to rename it. assign to Sonia",
        params={},
        user_id="U1",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
    )

    assert created_inputs == []
    assert "Assignee unclear: multiple Linear users matched" in result["message"]
    assert "project: Venture Studio; assignee: Unresolved" in result["message"]


@pytest.mark.asyncio
async def test_linear_meeting_executor_creates_project_update_when_requested(monkeypatch):
    executor = SkillExecutor()
    executor_module.LINEAR_MEETING_PENDING_ACTIONS.clear()
    created_updates = []
    created_issues = []

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
                "confidence": 0.95,
            }
        ]

    team = {"id": "team-1", "key": "MLA", "name": "MLAI"}
    user = {"id": "user-1", "name": "Sonia", "displayName": "sonia1", "email": "sonia@example.com"}
    project = {
        "id": "project-1",
        "name": "Bounties / Venture Studio",
        "lastUpdate": {
            "id": "update-0",
            "body": "Previous update",
            "health": "atRisk",
            "createdAt": "2026-05-01T00:00:00Z",
        },
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
            return [{"id": "issue-1", "identifier": "MLA-1", "title": "Existing project issue", "project": {"id": "project-1"}}]

        async def create_project_update(self, **kwargs):
            created_updates.append(kwargs)
            return {
                "id": "update-1",
                "url": "https://linear.app/acme/project-update/update-1",
                "project": {"name": "Bounties / Venture Studio"},
            }

        async def create_issue(self, **kwargs):
            created_issues.append(kwargs)
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
        assert kwargs["recent_issues"][0]["identifier"] == "MLA-1"
        assert kwargs["project"]["lastUpdate"]["id"] == "update-0"
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
    # Project updates are still posted immediately when explicitly requested, but the
    # extracted action item is queued for review rather than auto-created.
    assert created_issues == []
    assert result["data"]["review_count"] == 1
    pending = list(executor_module.LINEAR_MEETING_PENDING_ACTIONS.values())
    assert any(p["issue_input"]["title"] == "Validate the bounty model with VCs" for p in pending)
    assert "Created Linear project update" in result["message"]


@pytest.mark.asyncio
async def test_linear_project_update_writer_uses_chunk_summaries_last_update_and_recent_issues(monkeypatch):
    executor = SkillExecutor()
    prompts = []

    async def fake_chat(messages, **kwargs):
        prompt = messages[-1]["content"]
        prompts.append(prompt)
        if prompt.startswith("Summarize this meeting-notes chunk"):
            return SimpleNamespace(content="- Work done\n- Finished partner outreach.\n- Decisions\n- Keep launch scope tight.")
        return SimpleNamespace(
            content=json.dumps(
                {
                    "body": "## Summary\nPartner outreach moved forward.\n\n## Work done since last update\nFinished outreach.\n\n## Decisions made\nKeep launch scope tight.\n\n## Risks / open questions\nNone noted.\n\n## Next steps\nConfirm launch comms.",
                    "health": "atRisk",
                }
            )
        )

    monkeypatch.setattr(executor_module, "chat", fake_chat)

    project = {
        "id": "project-1",
        "name": "BITGET EVENT",
        "lastUpdate": {
            "id": "update-1",
            "body": "Previous update body: venue was still open.",
            "health": "onTrack",
            "createdAt": "2026-05-01T00:00:00Z",
            "user": {"displayName": "Yana"},
        },
    }
    result = await executor._build_linear_meeting_project_update_input(
        sources=[
            ParsedSource(label="meeting.pdf page 1", text="The team finished partner outreach.", kind="pdf"),
            ParsedSource(label="meeting.pdf page 2", text="Decision: keep launch scope tight.", kind="pdf"),
        ],
        params={},
        project=project,
        candidates=[{"title": "Confirm launch comms", "owner_hint": "Yana"}],
        recent_issues=[
            {
                "identifier": "MKT-7",
                "title": "Prepare launch comms",
                "project": {"id": "project-1"},
                "state": {"name": "In Progress"},
                "assignee": {"displayName": "Yana"},
            }
        ],
        settings=SimpleNamespace(LINEAR_MEETING_LLM_MODEL="gpt-5.5", LINEAR_MEETING_LLM_REASONING_EFFORT="low"),
    )

    assert result["project_id"] == "project-1"
    assert result["health"] == "atRisk"
    assert result["body"].endswith("_Generated by Roo from Slack meeting notes._")
    assert len(prompts) == 3
    final_prompt = prompts[-1]
    assert "Previous update body" in final_prompt
    assert "MKT-7: Prepare launch comms" in final_prompt
    assert "Finished partner outreach" in final_prompt
    assert "Confirm launch comms" in final_prompt


@pytest.mark.asyncio
async def test_linear_project_update_skips_when_project_match_low_confidence(monkeypatch):
    executor = SkillExecutor()
    created_updates = []

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="meeting.pdf",
                    text="The team discussed event positioning and follow-up work.",
                    kind="pdf",
                )
            ],
            files_seen=1,
            files_parsed=1,
        )

    async def no_candidates(**kwargs):
        return []

    class FakeClient:
        async def list_teams(self):
            return [{"id": "team-1", "key": "MKT", "name": "Marketing"}]

        async def list_users(self):
            return []

        async def list_active_projects(self):
            return [{"id": "project-1", "name": "Unrelated Product", "teams": {"nodes": [{"id": "team-1"}]}}]

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

        async def create_project_update(self, **kwargs):
            created_updates.append(kwargs)
            return {"id": "update-1"}

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
    monkeypatch.setattr(executor, "_extract_linear_meeting_candidates_from_sources", no_candidates)

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text="create a project update from this PDF",
        params={},
        user_id="U1",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
        event_files=[{"id": "F1", "name": "meeting.pdf"}],
    )

    assert created_updates == []
    assert "could not confidently match the Linear project" in result["message"]


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


@pytest.mark.parametrize(
    "text,expected",
    [
        ("if you're not sure who to assign to, assign to Dr Sam Donegan", "Dr Sam Donegan"),
        ("extract tasks and add to linear; if unsure, assign to Jane Doe", "Jane Doe"),
        ("turn these notes into tasks. Default assignee: Sam Donegan", "Sam Donegan"),
        ("add these to linear and assign to the correct people", None),
    ],
)
def test_linear_meeting_default_assignee_prepass_extracts_fallback(text, expected):
    executor = SkillExecutor()
    params = executor._apply_linear_meeting_default_assignee_prepass(text, {})
    assert params.get("default_assignee_hint") == expected


@pytest.mark.asyncio
async def test_linear_meeting_command_with_file_uses_extraction_not_direct_path(monkeypatch):
    """Regression for the "one task about assigning the tasks" bug.

    A direct-looking command ("add ... tasks ... linear") with an attached file must
    extract action items from the file (not parse the command into a single issue), map
    every task to the explicitly named project, fall back to the named assignee when an
    owner is unclear, and queue everything for review.
    """
    executor = SkillExecutor()
    executor_module.LINEAR_MEETING_PENDING_ACTIONS.clear()
    created_inputs = []

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[
                ParsedSource(
                    label="MLAI Committee Meeting.pdf",
                    text="Committee discussed the venue, sponsors, and the budget.",
                    kind="pdf",
                )
            ],
            files_seen=1,
            files_parsed=1,
        )

    async def fake_candidates_from_sources(**kwargs):
        assert kwargs["sources"][0].label == "MLAI Committee Meeting.pdf"
        return [
            {"title": "Confirm the venue booking", "owner_hint": "Sonia", "confidence": 0.9,
             "source_label": "MLAI Committee Meeting.pdf"},
            {"title": "Draft the sponsorship deck", "owner_hint": "the team", "confidence": 0.9,
             "source_label": "MLAI Committee Meeting.pdf"},
        ]

    async def fail_direct(**kwargs):
        raise AssertionError("direct-issue path must not run when a file was parsed")

    team = {"id": "team-1", "key": "MLA", "name": "MLAI"}
    sonia = {"id": "user-sonia", "name": "Sonia", "displayName": "Sonia", "email": "sonia@example.com"}
    sam = {"id": "user-sam", "name": "Sam Donegan", "displayName": "Dr Sam Donegan", "email": "sam@example.com"}
    project = {
        "id": "project-mlai",
        "name": "MLAI Core",
        "teams": {"nodes": [team]},
        "members": {"nodes": [sonia, sam]},
    }

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return [sonia, sam]

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

        async def create_issue(self, **kwargs):
            created_inputs.append(kwargs)
            return {"identifier": "MLA-1", "title": kwargs["title"]}

    class FakeSkill:
        def get_client_class(self, name):
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
    monkeypatch.setattr(executor, "_extract_linear_meeting_candidates_from_sources", fake_candidates_from_sources)
    monkeypatch.setattr(executor, "_extract_linear_direct_issue_candidates", fail_direct)

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text=(
            "here's the committee meeting notes, please add them to the linear project "
            "called 'MLAI Core' and assign the tasks to the correct people. if you're not "
            "sure who to assign to, assign to Dr Sam Donegan"
        ),
        params={},
        user_id="U1",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
        event_files=[{"id": "F1", "name": "MLAI Committee Meeting.pdf"}],
    )

    # Nothing auto-created; both extracted tasks are queued for review.
    assert result["data"]["created_count"] == 0
    assert result["data"]["review_count"] == 2
    assert created_inputs == []

    pending = {
        p["issue_input"]["title"]: p["issue_input"]
        for p in executor_module.LINEAR_MEETING_PENDING_ACTIONS.values()
    }
    assert set(pending) == {"Confirm the venue booking", "Draft the sponsorship deck"}
    # Every task mapped to the explicitly named project and its team.
    for issue_input in pending.values():
        assert issue_input["project_id"] == "project-mlai"
        assert issue_input["team_id"] == "team-1"
    # Clear owner kept; vague owner fell back to the named default assignee.
    assert pending["Confirm the venue booking"]["assignee_id"] == "user-sonia"
    assert pending["Draft the sponsorship deck"]["assignee_id"] == "user-sam"


@pytest.mark.asyncio
async def test_contextual_founder_games_assignment_creates_one_step(monkeypatch):
    executor = SkillExecutor()
    created_inputs = []
    team = {"id": "team-1", "key": "MLA", "name": "MLAI"}
    sam = {
        "id": "linear-sam",
        "name": "Sam Donegan",
        "displayName": "Sam",
        "email": "sam@example.com",
    }
    project = {
        "id": "project-founder-program",
        "name": "Founder Program 2026",
        "description": "Applications and operations for Founder Games.",
        "content": "Founder Games run sheets, setup, and participant experience.",
        "slackChannelId": "CFOUNDERS",
        "teams": {"nodes": [team]},
        "members": {"nodes": [sam]},
        "membersSource": "project",
    }
    slack_context = {
        "workspace_id": "TMLAI",
        "channel": {
            "id": "CFOUNDERS",
            "name": "founder-programs",
            "topic": "Founder Games planning",
            "purpose": "",
        },
        "request": {
            "user_id": "USAM",
            "display_name": "Dr Sam",
            "email": "sam@example.com",
            "message_ts": "1784595900.000002",
            "local_datetime": "2026-07-21T10:05:00+10:00",
            "timezone": "Australia/Sydney",
            "event_id": "Ev-founder-task",
        },
        "messages": [
            {
                "user": "UJESS",
                "display_name": "Jess",
                "email": "jess@example.com",
                "text": (
                    "Sam can you send me through the run sheet for the founder games by EOW "
                    "and we can allocate support to setting up for that"
                ),
                "ts": "1784592300.000001",
                "local_datetime": "2026-07-21T09:05:00+10:00",
                "is_bot": False,
                "files": [],
            },
            {
                "user": "USAM",
                "display_name": "Dr Sam",
                "email": "sam@example.com",
                "text": "@Roo add this as a task for me in Linear",
                "ts": "1784595900.000002",
                "local_datetime": "2026-07-21T10:05:00+10:00",
                "is_bot": False,
                "files": [],
            },
        ],
        "selection": {"mode": "recent_channel", "contextual_reference": True},
    }

    async def extracted_candidates(**kwargs):
        combined = "\n".join(source.text for source in kwargs["sources"])
        assert "Jess (<@UJESS>, jess@example.com)" in combined
        assert "@Roo add this" not in combined
        return [
            {
                "title": "Send Founder Games run sheet to Jess",
                "description": "Send Jess the run sheet so HEX can allocate setup support.",
                "owner_hint": "Sam",
                "project_hint": "Founder Games",
                "due_expression": "EOW",
                # The evidence timestamp is authoritative for relative dates,
                # even if extraction supplied a stale normalization.
                "due_date": "2026-07-31",
                "evidence": (
                    "Sam can you send me through the run sheet for the founder games by EOW"
                ),
                "evidence_message_ts": "1784592300.000001",
                "explicit_commitment": True,
                "source_label": "Slack thread",
                "confidence": 0.95,
            }
        ]

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return [sam]

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

        async def create_issue(self, **kwargs):
            created_inputs.append(kwargs)
            return {
                "identifier": "MLA-42",
                "title": kwargs["title"],
                "url": "https://linear.test/MLA-42",
            }

    class FakeSkill:
        def get_client_class(self, name):
            assert name == "LinearMeetingActionsClient"
            return FakeClient

    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(
            OPENAI_API_KEY=None,
            LINEAR_DEFAULT_TEAM=None,
            LINEAR_MEETING_AUTO_CREATE_MIN_CONFIDENCE=0.85,
            LINEAR_MEETING_UNCERTAIN_MIN_CONFIDENCE=0.65,
            LINEAR_CONTEXTUAL_AUTO_CREATE_ENABLED=True,
        ),
    )
    monkeypatch.setattr(
        "roo.slack_client.get_user_info",
        lambda user_id: {"email": "sam@example.com"} if user_id == "USAM" else {},
    )
    monkeypatch.setattr(
        "roo.slack_client.get_message_permalink",
        lambda channel_id, message_ts: "https://slack.test/source-message",
    )
    monkeypatch.setattr(
        executor,
        "_extract_linear_meeting_candidates_from_sources",
        extracted_candidates,
    )

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text="@Roo add this as a task for me in Linear",
        params={},
        user_id="USAM",
        channel_id="CFOUNDERS",
        thread_ts="1784595900.000002",
        thread_history=slack_context["messages"],
        current_message_ts="1784595900.000002",
        slack_context=slack_context,
    )

    assert result["data"]["created_count"] == 1
    assert result["data"]["review_count"] == 0
    assert len(created_inputs) == 1
    issue = created_inputs[0]
    assert issue["title"] == "Send Founder Games run sheet to Jess"
    assert issue["assignee_id"] == "linear-sam"
    assert issue["project_id"] == "project-founder-program"
    assert issue["team_id"] == "team-1"
    assert issue["due_date"] == "2026-07-24"
    assert len(issue["idempotency_key"]) == 64
    assert "https://slack.test/source-message" in issue["description"]


@pytest.mark.asyncio
async def test_linear_meeting_unmatched_owner_skipped_without_fallback(monkeypatch):
    """Without a fallback assignee, a task whose owner can't be resolved is skipped
    (not reviewed). The fallback is what rescues it (see the test above)."""
    executor = SkillExecutor()
    executor_module.LINEAR_MEETING_PENDING_ACTIONS.clear()
    created_inputs = []

    async def fake_source_result(**kwargs):
        return SourceParseResult(
            sources=[ParsedSource(label="notes.pdf", text="The team will sort it out.", kind="pdf")],
            files_seen=1,
            files_parsed=1,
        )

    async def fake_candidates_from_sources(**kwargs):
        return [{"title": "Sort out logistics", "owner_hint": "the team", "confidence": 0.9}]

    team = {"id": "team-1", "key": "MLA", "name": "MLAI"}
    project = {"id": "project-mlai", "name": "MLAI Core", "teams": {"nodes": [team]}, "members": {"nodes": []}}

    class FakeClient:
        async def list_teams(self):
            return [team]

        async def list_users(self):
            return [{"id": "user-sonia", "name": "Sonia", "displayName": "Sonia", "email": "sonia@example.com"}]

        async def list_active_projects(self):
            return [project]

        async def list_issue_labels(self):
            return []

        async def list_recent_open_issues(self):
            return []

        async def create_issue(self, **kwargs):
            created_inputs.append(kwargs)
            return {"identifier": "MLA-1", "title": kwargs["title"]}

    class FakeSkill:
        def get_client_class(self, name):
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
    monkeypatch.setattr(executor, "_extract_linear_meeting_candidates_from_sources", fake_candidates_from_sources)

    result = await executor._execute_linear_meeting_actions(
        skill=FakeSkill(),
        text="add these notes to the linear project 'MLAI Core' as tasks",
        params={},
        user_id="U1",
        channel_id="C1",
        thread_ts="1.1",
        thread_history=[],
        event_files=[{"id": "F1", "name": "notes.pdf"}],
    )

    assert result["data"]["created_count"] == 0
    assert result["data"]["review_count"] == 0
    assert result["data"]["skipped_count"] == 1
    assert created_inputs == []


def test_normalize_candidate_string_false_is_not_an_explicit_commitment():
    candidate = SkillExecutor()._normalize_linear_meeting_candidate(
        {
            "title": "Consider changing the format",
            "explicit_commitment": "false",
        }
    )

    assert candidate["explicit_commitment"] is False


@pytest.mark.asyncio
async def test_contextual_command_without_readable_history_explains_recovery(monkeypatch):
    monkeypatch.setattr(
        executor_module,
        "get_settings",
        lambda: SimpleNamespace(OPENAI_API_KEY=None),
    )
    current_message = {
        "user": "USAM",
        "text": "@Roo add this as a task for me in Linear",
        "ts": "1784595900.000002",
        "is_bot": False,
        "files": [],
    }

    result = await SkillExecutor()._execute_linear_meeting_actions(
        skill=SimpleNamespace(),
        text=current_message["text"],
        params={},
        user_id="USAM",
        channel_id="CFOUNDERS",
        thread_ts=current_message["ts"],
        thread_history=[current_message],
        current_message_ts=current_message["ts"],
        slack_context={
            "messages": [current_message],
            "selection": {"mode": "recent_channel", "contextual_reference": True},
        },
    )

    assert "couldn't find enough preceding Slack context" in result["message"]
    assert "channel-history access" in result["message"]
