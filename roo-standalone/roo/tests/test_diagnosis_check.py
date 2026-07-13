"""Hermetic tests for POST /api/diagnosis-check (the ward-clerk contest).

No real LLM (the endpoint must never touch one), no network (the mlai-backend
recorder is monkeypatched). Invariants under test:

  - deterministic adjudication via check_guess, recorded BEFORE any reveal
  - result mapping: correct_first / correct_beaten / incorrect / already_guessed
  - the STORED verdict is authoritative on the already_guessed resume path
  - record failure → 503 with NO verdict fields (no free oracle, guess not burned)
  - the case defaults to the server pin; an explicit case_id targets that ward
    patient (unknown case → 404; which cases are OPEN is backend policy)
  - bearer auth mirrors /api/sim-patient
  - the recorder authenticates with ROO_API_KEY → INTERNAL_API_KEY →
    MLAI_API_KEY (deployments may set only MLAI_API_KEY)
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from roo import config, sim_patient
from roo.main import app


VALID_CLIENT = "aaaaaaaa-1111-4111-8111-111111111111"


def _client(monkeypatch, *, api_key=None, active_case=1):
    """TestClient with a pinned Settings singleton (no lifespan — bare client)."""
    real = config.get_settings()
    monkeypatch.setattr(real, "SIM_PATIENT_API_KEY", api_key, raising=False)
    monkeypatch.setattr(real, "SIM_ACTIVE_CASE_ID", active_case, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: real)
    return TestClient(app)


def _install_recorder(monkeypatch, response=None, error=None):
    """Replace record_web_guess; returns the list of recorded call kwargs."""
    calls: list[dict] = []

    async def fake_record(settings, **kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(sim_patient, "record_web_guess", fake_record)
    return calls


def _install_llm_bomb(monkeypatch):
    """The clerk path is scripted — any LLM call is a bug."""
    def bomb(provider=None):
        raise AssertionError("diagnosis-check must never call the LLM")
    monkeypatch.setattr(sim_patient, "get_llm_client", bomb)


def test_correct_first_records_then_reveals(monkeypatch):
    _install_llm_bomb(monkeypatch)
    calls = _install_recorder(monkeypatch, response={
        "already_guessed": False, "is_correct": True,
        "outcome": "pending_claim", "winner_taken": False,
    })
    client = _client(monkeypatch)

    resp = client.post("/api/diagnosis-check",
                       json={"guess": "adrenal crisis", "client_id": VALID_CLIENT})

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "correct_first"
    assert body["outcome"] == "pending_claim"
    assert body["winner_taken"] is False
    assert body["case_id"] == 1
    assert body["diagnosis"] == "Adrenal Crisis"
    # Adjudicated deterministically and recorded with the pinned case.
    assert calls == [{
        "case_id": 1, "client_id": VALID_CLIENT,
        "guess_text": "adrenal crisis", "is_correct": True,
    }]


def test_correct_beaten_when_winner_taken(monkeypatch):
    _install_recorder(monkeypatch, response={
        "already_guessed": False, "is_correct": True,
        "outcome": "pending_claim", "winner_taken": True,
    })
    client = _client(monkeypatch)

    body = client.post("/api/diagnosis-check",
                       json={"guess": "addisonian crisis", "client_id": VALID_CLIENT}).json()

    assert body["result"] == "correct_beaten"
    assert body["winner_taken"] is True
    assert body["diagnosis"] == "Adrenal Crisis"


def test_incorrect_guess_reveals_nothing(monkeypatch):
    calls = _install_recorder(monkeypatch, response={
        "already_guessed": False, "is_correct": False,
        "outcome": "incorrect", "winner_taken": False,
    })
    client = _client(monkeypatch)

    body = client.post("/api/diagnosis-check",
                       json={"guess": "banana allergy", "client_id": VALID_CLIENT}).json()

    assert body["result"] == "incorrect"
    assert body["diagnosis"] is None
    assert calls[0]["is_correct"] is False


def test_already_guessed_resume_uses_stored_verdict(monkeypatch):
    # The NEW text is wrong, but the STORED guess was correct (player resuming
    # after losing local state) — the stored verdict wins and the dx re-reveals.
    _install_recorder(monkeypatch, response={
        "already_guessed": True, "is_correct": True,
        "outcome": "pending_claim", "winner_taken": True,
    })
    client = _client(monkeypatch)

    body = client.post("/api/diagnosis-check",
                       json={"guess": "banana allergy", "client_id": VALID_CLIENT}).json()

    assert body["result"] == "already_guessed"
    assert body["outcome"] == "pending_claim"
    assert body["diagnosis"] == "Adrenal Crisis"


def test_record_failure_503_leaks_no_verdict_and_burns_nothing(monkeypatch):
    _install_recorder(monkeypatch, error=RuntimeError("backend down"))
    client = _client(monkeypatch)

    resp = client.post("/api/diagnosis-check",
                       json={"guess": "adrenal crisis", "client_id": VALID_CLIENT})

    assert resp.status_code == 503
    body = resp.json()
    assert body == {"detail": "record_failed"}  # no result/outcome/diagnosis keys
    for verdict_key in ("result", "outcome", "diagnosis", "winner_taken"):
        assert verdict_key not in body


def test_case_id_targets_that_ward_patient(monkeypatch):
    # The authenticated gateway may pin a specific ward patient (two-patient
    # ward): roo adjudicates and records against THAT case. Which cases are
    # open to players is the backend gateway's policy, not roo's.
    calls = _install_recorder(monkeypatch, response={
        "already_guessed": False, "is_correct": True,
        "outcome": "pending_claim", "winner_taken": False,
    })
    client = _client(monkeypatch, active_case=1)

    body = client.post("/api/diagnosis-check", json={
        "guess": "cerebral venous sinus thrombosis",
        "client_id": VALID_CLIENT,
        "case_id": 7,
    }).json()

    assert calls[0]["case_id"] == 7
    assert calls[0]["is_correct"] is True  # matched against case 7, not case 1
    assert body["case_id"] == 7
    assert body["result"] == "correct_first"
    assert body["diagnosis"] == "Cerebral Venous Sinus Thrombosis (CVST)"


def test_unknown_case_id_404s_and_records_nothing(monkeypatch):
    calls = _install_recorder(monkeypatch, response={})
    client = _client(monkeypatch)

    resp = client.post("/api/diagnosis-check", json={
        "guess": "adrenal crisis", "client_id": VALID_CLIENT, "case_id": 999,
    })

    assert resp.status_code == 404
    assert calls == []  # nothing recorded, nothing revealed


def test_misconfigured_active_case_503s(monkeypatch):
    _install_recorder(monkeypatch, response={})
    client = _client(monkeypatch, active_case=999)

    resp = client.post("/api/diagnosis-check",
                       json={"guess": "adrenal crisis", "client_id": VALID_CLIENT})

    assert resp.status_code == 503
    assert resp.json() == {"detail": "contest unavailable"}


def test_auth_mirrors_sim_patient(monkeypatch):
    _install_recorder(monkeypatch, response={
        "already_guessed": False, "is_correct": False,
        "outcome": "incorrect", "winner_taken": False,
    })
    client = _client(monkeypatch, api_key="secret-key")

    payload = {"guess": "gastro", "client_id": VALID_CLIENT}
    assert client.post("/api/diagnosis-check", json=payload).status_code == 401
    assert client.post("/api/diagnosis-check", json=payload,
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/api/diagnosis-check", json=payload,
                       headers={"Authorization": "Bearer secret-key"}).status_code == 200


@pytest.mark.parametrize("payload", [
    {"client_id": VALID_CLIENT},                              # guess missing
    {"guess": "   ", "client_id": VALID_CLIENT},              # guess blank
    {"guess": "x" * 201, "client_id": VALID_CLIENT},          # guess too long
    {"guess": "gastro"},                                      # client_id missing
    {"guess": "gastro", "client_id": "short"},                # client_id too short
    {"guess": "gastro", "client_id": "bad chars!" + "a" * 8}, # client_id bad chars
    {"guess": "gastro", "client_id": VALID_CLIENT, "case_id": True},   # bool is not a case
    {"guess": "gastro", "client_id": VALID_CLIENT, "case_id": "2"},    # string case_id
    {"guess": "gastro", "client_id": VALID_CLIENT, "case_id": 0},      # below minimum
])
def test_validation_422(monkeypatch, payload):
    calls = _install_recorder(monkeypatch, response={})
    client = _client(monkeypatch)

    resp = client.post("/api/diagnosis-check", json=payload)

    assert resp.status_code == 422
    assert calls == []  # nothing recorded on validation failure


def _recorder_settings(monkeypatch, **keys):
    """The real Settings singleton with only the given service keys set.

    Mutates the real object (not a stub) so a renamed settings field fails
    here instead of silently passing against attributes that no longer exist.
    """
    real = config.get_settings()
    monkeypatch.setattr(real, "MLAI_BACKEND_URL", "http://mlai-backend.test")
    for name in ("ROO_API_KEY", "INTERNAL_API_KEY", "MLAI_API_KEY"):
        monkeypatch.setattr(real, name, keys.get(name))
    return real


def _install_fake_httpx(monkeypatch):
    """Replace httpx.AsyncClient; returns the captured outbound POST."""
    sent: dict = {}

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, json=None, headers=None):
            sent.update({"url": url, "json": json, "headers": headers})
            return httpx.Response(
                200,
                json={"already_guessed": False},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(sim_patient.httpx, "AsyncClient", FakeAsyncClient)
    return sent


def test_recorder_falls_back_to_mlai_api_key(monkeypatch):
    # Regression: deployments holding the shared backend secret only under
    # MLAI_API_KEY must still authenticate the record call — this config shape
    # made every prod guess submission 503 ("record_failed") → 502 to the game.
    sent = _install_fake_httpx(monkeypatch)
    settings = _recorder_settings(monkeypatch, MLAI_API_KEY="mlai-secret")

    result = asyncio.run(sim_patient.record_web_guess(
        settings, case_id=1, client_id=VALID_CLIENT,
        guess_text="adrenal crisis", is_correct=True,
    ))

    assert sent["headers"] == {"X-API-Key": "mlai-secret"}
    assert sent["url"].endswith("/api/v1/hackathons/hospital/sim-guess/record/")
    assert result == {"already_guessed": False}


def test_recorder_prefers_roo_key_over_mlai_key(monkeypatch):
    sent = _install_fake_httpx(monkeypatch)
    settings = _recorder_settings(
        monkeypatch, ROO_API_KEY="roo-secret", MLAI_API_KEY="mlai-secret",
    )

    asyncio.run(sim_patient.record_web_guess(
        settings, case_id=1, client_id=VALID_CLIENT,
        guess_text="gastro", is_correct=False,
    ))

    assert sent["headers"] == {"X-API-Key": "roo-secret"}


def test_recorder_with_no_key_raises_without_posting(monkeypatch):
    sent = _install_fake_httpx(monkeypatch)
    settings = _recorder_settings(monkeypatch)

    with pytest.raises(RuntimeError, match="no service API key"):
        asyncio.run(sim_patient.record_web_guess(
            settings, case_id=1, client_id=VALID_CLIENT,
            guess_text="gastro", is_correct=False,
        ))

    assert sent == {}  # never reached the network
