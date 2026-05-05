import importlib.util
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
async def test_linear_client_raises_on_graphql_errors(monkeypatch):
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
        def raise_for_status(self):
            return None

        def json(self):
            return {"errors": [{"message": "bad query"}]}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)

    client = module.LinearMeetingActionsClient(api_key="lin_api_key")
    with pytest.raises(RuntimeError, match="bad query"):
        await client._graphql("query { viewer { id } }")


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
        def __init__(self, api_key):
            assert api_key == "lin_api_key"

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
            LINEAR_API_KEY="lin_api_key",
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
