import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "roo-standalone"))

from roo import linear_inference as inference
from roo.linear_meeting_sources import ParsedSource
from roo.linear_project_context import ProjectIssueInventory
from roo.linear_project_reconciliation import (
    _MAX_RECONCILIATION_CONTEXT_CHARS,
    _serialize_project_context,
    reconcile_project_meeting,
)


@pytest.fixture
def meeting():
    return {
        "sources": [ParsedSource("Demo", "Mira: I will add CSV export.\nLater: hold CSV export until security signs off.", "text")],
        "project": {"id": "p", "name": "Demo", "lastUpdate": {"body": "The team agreed to add CSV export."}},
        "inventory": ProjectIssueInventory("p", [{"id": "i", "title": "CSV export", "state": {"type": "started"}}], True),
        "facts": [
            {"kind": "blocker", "text": "Security sign-off is the next step.", "evidence": "hold CSV export until security signs off.", "source_label": "Demo"},
            {"kind": "requirement", "text": "Mira agreed to add CSV export.", "evidence": "I will add CSV export.", "source_label": "Demo"},
        ],
        "candidates": [{"title": "Add CSV export", "explicit_commitment": True, "evidence": "I will add CSV export.", "source_label": "Demo"}],
    }


def fake_provider(monkeypatch, *, mutate=None):
    calls = []

    class Client:
        async def responses_parse(self, messages, response_format, **kwargs):
            payload = json.loads(messages[-1]["content"])
            calls.append((payload, kwargs))
            promised, held = [row["evidence_id"] for row in payload["claims"]]
            decision = {
                "changes": [{"kind": "blocker", "text": "CSV export is waiting on security sign-off.", "evidence_ids": [held]}],
                "decisions": [], "health": None, "health_evidence_ids": [],
                "actions": [{"candidate_index": 0, "disposition": "tracked", "semantic_relation": "same_outcome",
                             "semantic_issue_id": "i", "duplicate_of_index": None, "evidence_ids": [promised]}],
            }
            if mutate:
                mutate(decision)
            return response_format.model_validate(decision)

    monkeypatch.setattr(inference, "get_llm_client", lambda provider: Client())
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_astra,expected", [(False, "gpt-6.2-sol"), (True, "gpt-6.2-astra")])
async def test_complex_meeting_uses_scoped_model_and_chronological_evidence(monkeypatch, meeting, allow_astra, expected):
    calls = fake_provider(monkeypatch)
    meeting["sources"][0].text = meeting["sources"][0].text.replace("security", "“security”")
    meeting["facts"][0]["evidence"] = 'hold CSV export until "security" signs off.'
    meeting["candidates"][0].update({"evidence": "two merged quotes", "source_label": "Demo, mirror", "_source_quote": "I will add CSV export.", "_source_label": "Demo"})
    result = await reconcile_project_meeting(**meeting, allow_astra=allow_astra)
    assert len(calls) == 1
    payload, params = calls[0]
    assert params["model"] == expected and params["store"] is False
    assert [c["kind"] for c in payload["claims"]] == ["requirement", "blocker"]
    assert result.evidence[result.changes[0].evidence_ids[0]] == 'hold CSV export until "security" signs off.'
    assert result.actions[0].matched_issue_id == "i" and result.health is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda d: d["actions"][0].update(semantic_issue_id="some-other-project"),
    lambda d: d.update(health="onTrack", health_evidence_ids=d["changes"][0]["evidence_ids"]),
    lambda d: d.update(actions=[]),
    lambda d: d["changes"][0].update(evidence_ids=["fake anchor"]),
])
async def test_conclusions_require_source_links_and_full_candidate_coverage(monkeypatch, meeting, mutate):
    calls = fake_provider(monkeypatch, mutate=mutate)
    with pytest.raises(inference.LinearInferenceValidationError):
        await reconcile_project_meeting(**meeting)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_partial_project_and_wrong_source_fail_before_inference(monkeypatch, meeting):
    calls = fake_provider(monkeypatch)
    meeting["inventory"].complete = False
    with pytest.raises(inference.LinearInferenceValidationError, match="incomplete"):
        await reconcile_project_meeting(**meeting)
    meeting["inventory"].complete = True
    meeting["facts"][0]["source_label"] = "Different project notes"
    with pytest.raises(inference.LinearInferenceValidationError, match="source"):
        await reconcile_project_meeting(**meeting)
    assert not calls


def test_meeting_candidates_require_source_and_explicit_commitment():
    batch = inference.LinearMeetingActionBatch.model_validate({"action_items": [{"title": "Ship", "evidence": 'consider "shipping"'}]})
    assert batch.action_items[0].explicit_commitment is False
    inference.validate_meeting_fact_quotes(batch, "We should consider\n“shipping”.")
    with pytest.raises(inference.LinearInferenceValidationError):
        inference.validate_meeting_fact_quotes(batch, "Tomorrow we'll cancel.")
    assert inference.choose_linear_reasoning(inference.LinearReasoningSignals(stage="project_reconciliation", allow_astra=True)).model == "gpt-6.2-sol"


def test_large_meeting_fits_by_shortening_issue_descriptions_before_touching_evidence():
    competing_issues = [
        {
            "id": f"issue-{i}",
            "title": f"Implementation outcome {i}",
            "description": "Implementation details and acceptance criteria. " * 25,
            "state": {"type": "started"},
        }
        for i in range(167)
    ]
    quotes = {
        f"quote-{i}": f"Decision {i}. " + "Material progress reported by the owner. " * 12
        for i in range(90)
    }
    candidates = [
        {"title": f"Outcome {i}", "issue_ids": [f"issue-{n}" for n in range(i * 3, i * 3 + 3)]}
        for i in range(38)
    ]
    payload = {"quotes": quotes, "candidates": candidates, "issues": competing_issues}
    assert len(json.dumps(payload, separators=(",", ":"))) > _MAX_RECONCILIATION_CONTEXT_CHARS

    compacted = _serialize_project_context(payload)
    assert len(compacted) <= _MAX_RECONCILIATION_CONTEXT_CHARS
    result = json.loads(compacted)
    assert result["issue_descriptions_shortened"]
    assert result["quotes"] == quotes
    assert result["candidates"] == candidates
    assert [issue["id"] for issue in result["issues"]] == [issue["id"] for issue in competing_issues]
    assert all(issue["state"] == {"type": "started"} for issue in result["issues"])
    assert 0 < len(result["issues"][0]["description"]) < len(competing_issues[0]["description"])
    assert result["issues"][-1]["description"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("status,accepted", [("atRisk", True), ("onTrack", False)])
async def test_published_health_must_agree_with_extracted_health(monkeypatch, meeting, status, accepted):
    meeting["sources"][0].text += "\nThe project is now at risk."
    meeting["facts"].append({"kind": "health", "text": "The project is at risk.", "health": "atRisk",
                             "evidence": "The project is now at risk.", "source_label": "Demo"})

    class Client:
        async def responses_parse(self, messages, response_format, **kwargs):
            payload = json.loads(messages[-1]["content"])
            return response_format.model_validate({
                "changes": [], "decisions": [], "actions": [], "health": status,
                "health_evidence_ids": [payload["claims"][-1]["evidence_id"]],
            })

    meeting["candidates"] = []
    monkeypatch.setattr(inference, "get_llm_client", lambda provider: Client())
    if accepted:
        assert (await reconcile_project_meeting(**meeting)).health == status
    else:
        with pytest.raises(inference.LinearInferenceValidationError):
            await reconcile_project_meeting(**meeting)
