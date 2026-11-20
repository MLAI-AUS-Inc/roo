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


def test_large_reconciliations_have_enough_time_to_return_each_disposition():
    large = inference.choose_linear_reasoning(inference.LinearReasoningSignals(
        stage="project_reconciliation", source_chars=151_235, candidate_count=37, ambiguity=True,
    ))
    ordinary = inference.choose_linear_reasoning(inference.LinearReasoningSignals(
        stage="project_reconciliation", source_chars=15_000, candidate_count=10, ambiguity=True,
    ))
    assert large.effort == ordinary.effort == "high"
    assert ordinary.timeout_seconds == 75.0
    assert large.timeout_seconds == 180.0
    assert inference.escalate_linear_reasoning(large).timeout_seconds == 180.0


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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pass", "missing_candidate", "unsupported_status", "conflicting_health"])
async def test_large_meeting_preserves_global_duplicates_and_original_evidence(monkeypatch, mode):
    action_quotes = [f"Priya: I will deliver expense export milestone {i}." for i in range(18)]
    status_quotes = [f"Priya: We verified expense export checkpoint {i} in staging." for i in range(49)]
    latest = "Priya: We put expense export on hold pending sign-off."
    report = "Priya: The project is on track overall."
    conflict = "Priya: The project is at risk overall."
    health_facts = [{"kind": "health", "text": report, "evidence": report, "health": "onTrack"}]
    if mode == "conflicting_health":
        health_facts.append({"kind": "health", "text": conflict, "evidence": conflict, "health": "atRisk"})
    meeting = {
        "sources": [ParsedSource("Meeting", "\n".join([*action_quotes, *status_quotes, latest, report, conflict]), "text")],
        "project": {"id": "p", "name": "Demo"},
        "inventory": ProjectIssueInventory("p", [
            {"id": "linear-uuid", "identifier": "DEMO-42", "title": "Deliver expense export milestone 0",
             "state": {"type": "started"}},
            *({"id": f"linear-{i}", "title": f"Deliver expense export milestone {i}", "state": {"type": "started"}}
              for i in range(1, 8)),
            {"id": "diagram-uuid", "identifier": "DEMO-77", "title": "Publish the architecture diagrams",
             "state": {"type": "started"}},
        ], True),
        "candidates": [
            {"title": "Publish the architecture diagrams" if i == 16 else f"Deliver expense export milestone {i}",
             "explicit_commitment": i != 10, "evidence": quote}
            for i, quote in enumerate(action_quotes)
        ],
        "facts": health_facts + [
            {"kind": "progress", "text": quote, "evidence": quote}
            for quote in [*status_quotes, latest]
        ],
    }
    calls = []

    class Client:
        async def responses_parse(self, messages, response_format, **kwargs):
            payload = json.loads(messages[-1]["content"])
            calls.append((response_format.__name__, payload))
            assert len(messages[-1]["content"]) <= 55_000
            if response_format.__name__ == "_ActionBatch":
                assert len(payload["candidates"]) <= 8
                assert len(payload["meeting_candidates"]) == 18
                assert latest in [claim["text"] for claim in payload["claims"]]
                actions = []
                for item in payload["candidates"]:
                    index = item["index"]
                    if mode == "missing_candidate" and index == 8:
                        continue
                    if index == 9:
                        assert "DEMO-77" not in item["issue_ids"]
                    dispositions = {0: "tracked", 8: "duplicate"}
                    actions.append({
                        "candidate_index": index,
                        "disposition": dispositions.get(index, "new"),
                        "semantic_issue_id": {0: "DEMO-42", 9: "DEMO-77"}.get(index),
                        "semantic_relation": {0: "same_outcome", 9: "distinct"}.get(index),
                        "duplicate_of_index": 1 if index == 8 else None,
                    })
                return {"actions": actions}
            assert not payload["candidates"]
            assert response_format.__name__ == "_StatusSelection"
            fact = next(row for row in reversed(payload["claims"]) if row["kind"] != "health")
            change = {"kind": "progress", "text": fact["text"], "evidence_ids": fact["evidence_ids"]}
            changes = [change, change]
            if mode == "unsupported_status":
                changes.append({"kind": "progress", "text": "An unsupported result.", "evidence_ids": ["e999"]})
            return {"changes": changes, "decisions": []}

    monkeypatch.setattr(inference, "get_llm_client", lambda provider: Client())
    if mode == "missing_candidate":
        with pytest.raises(inference.LinearInferenceValidationError, match="every supplied candidate"):
            await reconcile_project_meeting(**meeting)
        return

    result = await reconcile_project_meeting(**meeting)
    assert [action.candidate_index for action in result.actions] == list(range(18))
    assert result.actions[0].semantic_issue_id == "linear-uuid"
    assert result.actions[8].duplicate_of_index == 1
    assert [action.disposition for action in result.actions[9:12]] == ["review", "review", "new"]
    assert result.evidence[result.actions[8].evidence_ids[0]] == action_quotes[8]
    assert [fact.text for fact in result.changes] == [latest]
    assert result.evidence[result.changes[0].evidence_ids[0]] == latest
    assert result.health == ("onTrack" if mode == "pass" else None)
    if result.health:
        assert result.evidence[result.health_evidence_ids[0]] == report
    assert sum(model == "_ActionBatch" for model, _ in calls) == 3
    assert sum(model == "_StatusSelection" for model, _ in calls) == 4
