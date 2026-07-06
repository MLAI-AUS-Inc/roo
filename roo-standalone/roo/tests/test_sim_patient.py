"""Hermetic tests for the simulated-patient endpoint logic (no real LLM, no network).

Covers plan §2.5:
  (a) case loads + secrets stripped from the prompt payload
  (b) a non-guess question returns is_guess: false
  (c) a guess matching acceptable_answers/fuzzy diagnosis returns correct: true + diagnosis
  (d) a wrong guess returns correct: false and no diagnosis
  (e) 401 when SIM_PATIENT_API_KEY is set and the bearer header is missing (TestClient)
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import sim_patient


class _FakeLLMResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLMClient:
    """Records every chat() call and returns canned content.

    Both the guess classifier (gpt-4o-mini) and the narrator reply route through
    get_llm_client("openai").chat(...). We branch on the model kwarg so a single
    fake serves both: gpt-4o-mini → the classifier JSON, anything else → reply.
    """

    def __init__(self, classifier_json: str, reply_text: str = "The patient blinks slowly."):
        self.classifier_json = classifier_json
        self.reply_text = reply_text
        self.calls: list[dict] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if kwargs.get("model") == "gpt-4o-mini":
            return _FakeLLMResponse(self.classifier_json)
        return _FakeLLMResponse(self.reply_text)


def _install_fake(monkeypatch, classifier_json, reply_text="The patient blinks slowly."):
    fake = _FakeLLMClient(classifier_json, reply_text)
    # sim_patient imports get_llm_client into its own namespace.
    monkeypatch.setattr(sim_patient, "get_llm_client", lambda provider=None: fake)
    return fake


def _run(coro):
    # Use a fresh loop rather than asyncio.get_event_loop() — in the full-suite
    # run another test may have closed/detached the main-thread loop, which makes
    # get_event_loop() raise "no current event loop".
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# (a) case loads + secrets stripped from the prompt payload
# ---------------------------------------------------------------------------

def test_case_loads_and_secrets_stripped_from_prompt(monkeypatch):
    fake = _install_fake(monkeypatch, '{"is_guess": false, "diagnosis": null}')

    result = _run(sim_patient.handle_question("what are her vitals?"))

    # Case 1 loaded by default.
    assert result["case_id"] == 1
    assert result["case_title"] == "Salt & Static"
    assert result["patient_name"] == "Sasha 'Sash' Nguyen"
    assert result["presenting_complaint"].startswith("27F")

    # The narrator user-prompt payload must NOT contain the secret answer.
    reply_calls = [c for c in fake.calls if c["kwargs"].get("model") != "gpt-4o-mini"]
    assert reply_calls, "expected a narrator reply LLM call"
    user_prompt = reply_calls[0]["messages"][1]["content"]
    assert "Adrenal Crisis" not in user_prompt
    assert "acceptable_answers" not in user_prompt
    assert "addisonian crisis" not in user_prompt
    # But the case content (vitals etc.) IS present.
    assert "CASE FILE" in user_prompt

    # case_for_prompt directly strips the secret fields.
    full = sim_patient.load_case(1)
    stripped = sim_patient.case_for_prompt(full)
    assert "diagnosis" not in stripped
    assert "acceptable_answers" not in stripped
    assert "diagnosis" in full  # original untouched


# ---------------------------------------------------------------------------
# (b) non-guess → is_guess false
# ---------------------------------------------------------------------------

def test_non_guess_question(monkeypatch):
    _install_fake(monkeypatch, '{"is_guess": false, "diagnosis": null}')

    result = _run(sim_patient.handle_question("what medications is she on?"))

    assert result["is_guess"] is False
    assert result["correct"] is None
    assert result["diagnosis"] is None
    assert result["reply"] == "The patient blinks slowly."


# ---------------------------------------------------------------------------
# (c) correct guess (via acceptable_answers / fuzzy) → correct true + diagnosis
# ---------------------------------------------------------------------------

def test_correct_guess_reveals_diagnosis(monkeypatch):
    fake = _install_fake(
        monkeypatch,
        '{"is_guess": true, "diagnosis": "addisonian crisis"}',
        reply_text="Sash grins weakly. \"Yeah… that's it.\"",
    )

    result = _run(sim_patient.handle_question("is it addisonian crisis?"))

    assert result["is_guess"] is True
    assert result["correct"] is True
    assert result["diagnosis"] == "Adrenal Crisis"

    # The reply prompt should carry the celebratory extra instruction with the dx.
    reply_calls = [c for c in fake.calls if c["kwargs"].get("model") != "gpt-4o-mini"]
    user_prompt = reply_calls[0]["messages"][1]["content"]
    assert "guessed correctly" in user_prompt
    assert "Adrenal Crisis" in user_prompt  # revealed to the LLM only on a correct guess


def test_correct_guess_via_fuzzy_match(monkeypatch):
    # "adrenal crises" (typo) fuzzy-matches "adrenal crisis" at >= 0.75.
    _install_fake(monkeypatch, '{"is_guess": true, "diagnosis": "adrenal crises"}')

    result = _run(sim_patient.handle_question("adrenal crises?"))

    assert result["is_guess"] is True
    assert result["correct"] is True
    assert result["diagnosis"] == "Adrenal Crisis"


# ---------------------------------------------------------------------------
# (d) wrong guess → correct false, no diagnosis
# ---------------------------------------------------------------------------

def test_wrong_guess_no_diagnosis(monkeypatch):
    fake = _install_fake(monkeypatch, '{"is_guess": true, "diagnosis": "appendicitis"}')

    result = _run(sim_patient.handle_question("is it appendicitis?"))

    assert result["is_guess"] is True
    assert result["correct"] is False
    assert result["diagnosis"] is None

    # Wrong-guess reply prompt must NOT contain the real answer.
    reply_calls = [c for c in fake.calls if c["kwargs"].get("model") != "gpt-4o-mini"]
    user_prompt = reply_calls[0]["messages"][1]["content"]
    assert "INCORRECT" in user_prompt
    assert "Adrenal Crisis" not in user_prompt


# ---------------------------------------------------------------------------
# (e) 401 when key set and bearer header missing (FastAPI TestClient)
# ---------------------------------------------------------------------------

def test_401_when_key_set_and_header_missing(monkeypatch):
    from fastapi.testclient import TestClient
    from roo import config
    from roo.main import app

    # Force a Settings instance with the bearer key set. get_settings() is a
    # cached singleton, so replace it and restore the original key afterward to
    # avoid leaking mutated state into the full-suite run.
    real = config.get_settings()
    monkeypatch.setattr(real, "SIM_PATIENT_API_KEY", "secret-key", raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: real)

    # Do NOT use `with TestClient(app)` — that would run the lifespan (background
    # retry loops / agent load). A bare client dispatches routes without lifespan.
    client = TestClient(app)

    # Missing header → 401.
    resp = client.post("/api/sim-patient", json={"question": "hi"})
    assert resp.status_code == 401

    # Wrong token → 401.
    resp = client.post(
        "/api/sim-patient",
        json={"question": "hi"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_422_when_question_missing(monkeypatch):
    from fastapi.testclient import TestClient
    from roo import config
    from roo.main import app

    real = config.get_settings()
    monkeypatch.setattr(real, "SIM_PATIENT_API_KEY", None, raising=False)  # auth open
    monkeypatch.setattr(config, "get_settings", lambda: real)

    client = TestClient(app)
    resp = client.post("/api/sim-patient", json={"question": "   "})
    assert resp.status_code == 422
