import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.skills.executor import SkillExecutor
from roo.clients import mlai_backend as backend_module
import roo.skills.executor as executor_module


class FakeClient:
    writes = []
    status_calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def list_linear_channel_statuses(self, **kwargs):
        self.__class__.status_calls += 1
        return {"statuses": [{"name": "Todo"}, {"name": "In Progress"}, {"name": "Done"}]}

    async def get_linear_channel_issue(self, **kwargs):
        return {
            "issue": {
                "identifier": kwargs["issue_identifier"],
                "title": "Controlled edits",
                "updatedAt": "2026-09-01T01:00:00.000Z",
                "state": {"name": "Todo"},
            },
            "comments": [],
        }

    async def list_linear_channel_issues(self, **kwargs):
        return {"list": {"displayName": "MLAI_TECH"}, "issues": [], "pageInfo": {}}

    async def write_linear_channel_issue(self, **kwargs):
        self.__class__.writes.append(kwargs)
        return {"operation": kwargs["operation"], "issue": {"identifier": kwargs["issue_identifier"]}}


def settings(**overrides):
    values = {
        "MLAI_BACKEND_URL": "https://backend.test",
        "MLAI_API_KEY": "",
        "ROO_API_KEY": "roo-key",
        "LINEAR_CHANNEL_ISSUE_WRITES_ENABLED": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("text", "params", "operation"),
    [
        ("add a comment to TECH-29 saying shipped", {"field": "comment", "value": "shipped"}, "add_comment"),
        ("change the title of TECH-29 to Safer", {"field": "title", "value": "Safer"}, "set_title"),
        ("replace the description of TECH-29 with New", {"field": "description", "mode": "replace", "value": "New"}, "replace_description"),
        ("append to the description of TECH-29: More", {"field": "description", "mode": "append", "value": "More"}, "append_description"),
        ("set the priority of TECH-29 to high", {"field": "priority", "value": "high"}, "set_priority"),
        ("set the estimate of TECH-29 to 8", {"field": "estimate", "value": "8"}, "set_estimate"),
        ("set the due date of TECH-29 to 2026-09-30", {"field": "due_date", "value": "2026-09-30"}, "set_due_date"),
        ("assign TECH-29 to Alex", {"field": "assignee", "value": "Alex"}, "set_assignee"),
        ("add label Bug to TECH-29", {"field": "label", "mode": "add", "value": "Bug"}, "add_label"),
        ("remove label Bug from TECH-29", {"field": "label", "mode": "remove", "value": "Bug"}, "remove_label"),
        ("set the project of TECH-29 to Project One", {"field": "project", "value": "Project One"}, "set_project"),
        ("set the cycle of TECH-29 to Cycle One", {"field": "cycle", "value": "Cycle One"}, "set_cycle"),
        ("move TECH-29 to Done", {"field": "status", "value": "Done"}, "set_status"),
        ("mark TECH-29 as duplicate of TECH-30", {"field": "duplicate", "value": "TECH-30"}, "mark_duplicate"),
    ],
)
def test_each_typed_write_requires_matching_explicit_text(text, params, operation):
    assert SkillExecutor()._linear_channel_write_request(text, params) == {
        "operation": operation,
        "value": params["value"],
    }


@pytest.mark.asyncio
async def test_lists_live_statuses(monkeypatch):
    FakeClient.status_calls = 0
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor()._execute_linear_channel_issues(
        text="what statuses are available?", params={"action": "list_statuses"},
        user_id="U123", channel_id="CTECH", thread_history=None,
        slack_team_id="TMLAI", request_id="Ev1",
    )

    assert "In Progress" in result
    assert "Done" in result
    assert FakeClient.status_calls == 1


@pytest.mark.asyncio
async def test_issue_status_question_returns_issue_detail_not_status_catalogue(monkeypatch):
    FakeClient.writes = []
    FakeClient.status_calls = 0
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor()._execute_linear_channel_issues(
        text="what is the status of TECH-29?",
        params={"action": "list_statuses", "issue_reference": "TECH-29"},
        user_id="U123", channel_id="CTECH", thread_history=None,
        slack_team_id="TMLAI", request_id="Ev-status-read",
    )

    assert "Controlled edits" in result
    assert "Available MLAI_TECH statuses" not in result
    assert FakeClient.status_calls == 0
    assert FakeClient.writes == []


@pytest.mark.asyncio
async def test_read_cannot_become_write_from_hallucinated_router_fields(monkeypatch):
    FakeClient.writes = []
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor()._execute_linear_channel_issues(
        text="tell me about TECH-29",
        params={
            "action": "update_issue", "issue_reference": "TECH-29",
            "field": "status", "value": "Done",
        },
        user_id="U123", channel_id="CTECH", thread_history=None,
        slack_team_id="TMLAI", request_id="Ev-hallucinated-write",
    )

    assert "couldn't identify one explicit Linear edit" in result
    assert FakeClient.writes == []


@pytest.mark.asyncio
async def test_raw_issue_identifier_cannot_be_overridden_by_router(monkeypatch):
    FakeClient.writes = []
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor()._execute_linear_channel_issues(
        text="move TECH-29 to Done",
        params={
            "action": "update_issue", "issue_reference": "TECH-30",
            "field": "status", "value": "Done",
        },
        user_id="U123", channel_id="CTECH", thread_history=None,
        slack_team_id="TMLAI", request_id="Ev-wrong-issue",
    )

    assert "conflicts with the routed issue" in result
    assert FakeClient.writes == []


@pytest.mark.asyncio
async def test_comment_without_body_cannot_use_hallucinated_router_value(monkeypatch):
    FakeClient.writes = []
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor()._execute_linear_channel_issues(
        text="comment on TECH-29",
        params={
            "action": "update_issue", "issue_reference": "TECH-29",
            "field": "comment", "value": "Invented progress update",
        },
        user_id="U123", channel_id="CTECH", thread_history=None,
        slack_team_id="TMLAI", request_id="Ev-invented-comment",
    )

    assert "couldn't identify one explicit Linear edit" in result
    assert FakeClient.writes == []


@pytest.mark.asyncio
async def test_multiple_edits_are_rejected_as_ambiguous(monkeypatch):
    FakeClient.writes = []
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor()._execute_linear_channel_issues(
        text="change the title of TECH-29 to Safer edits and move TECH-29 to Done",
        params={
            "action": "update_issue", "issue_reference": "TECH-29",
            "field": "title", "value": "Safer edits",
        },
        user_id="U123", channel_id="CTECH", thread_history=None,
        slack_team_id="TMLAI", request_id="Ev-two-edits",
    )

    assert "couldn't identify one explicit Linear edit" in result
    assert FakeClient.writes == []


@pytest.mark.asyncio
async def test_explicit_status_change_uses_current_issue_version(monkeypatch):
    FakeClient.writes = []
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor()._execute_linear_channel_issues(
        text="move TECH-29 to In Progress",
        params={"action": "update_issue", "issue_reference": "TECH-29", "field": "status", "value": "In Progress"},
        user_id="U123", channel_id="CTECH", thread_history=None,
        slack_team_id="TMLAI", request_id="Ev123",
    )

    assert "completed in Linear" in result
    assert FakeClient.writes == [{
        "slack_workspace_id": "TMLAI", "slack_channel_id": "CTECH",
        "requester_slack_id": "U123", "issue_identifier": "TECH-29",
        "operation": "set_status", "value": "In Progress",
        "expected_updated_at": "2026-09-01T01:00:00.000Z", "request_id": "Ev123",
    }]


@pytest.mark.asyncio
async def test_disabled_writes_fail_before_mutation(monkeypatch):
    FakeClient.writes = []
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings(LINEAR_CHANNEL_ISSUE_WRITES_ENABLED=False))
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor()._execute_linear_channel_issues(
        text="move TECH-29 to Done",
        params={"action": "update_issue", "issue_reference": "TECH-29", "field": "status", "value": "Done"},
        user_id="U123", channel_id="CTECH", thread_history=None,
        slack_team_id="TMLAI", request_id="Ev123",
    )

    assert "disabled" in result
    assert FakeClient.writes == []


@pytest.mark.asyncio
async def test_destructive_request_is_refused(monkeypatch):
    FakeClient.writes = []
    monkeypatch.setattr(executor_module, "get_settings", lambda: settings())
    monkeypatch.setattr(backend_module, "MLAIBackendClient", FakeClient)

    result = await SkillExecutor()._execute_linear_channel_issues(
        text="delete TECH-29", params={"action": "update_issue", "issue_reference": "TECH-29"},
        user_id="U123", channel_id="CTECH", thread_history=None,
        slack_team_id="TMLAI", request_id="Ev123",
    )

    assert "can't delete" in result
    assert FakeClient.writes == []
