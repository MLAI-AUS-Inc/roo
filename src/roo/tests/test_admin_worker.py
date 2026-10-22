import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo import admin_worker
from roo.admin_dispatch import build_admin_dispatch
from roo.backend_identity import BackendActorContext, get_backend_actor_context
from roo.config import Settings
from roo.slack_security import get_slack_receipt_store


SERVICE_TOKEN = f"mlai_sp_{'a' * 32}.{'s' * 48}"
SECRET = "dispatch-secret-" + ("s" * 32)


def _settings(tmp_path):
    return Settings(
        _env_file=None,
        SLACK_BOT_TOKEN=None,
        SLACK_SIGNING_SECRET=None,
        OPENAI_API_KEY=None,
        ROO_ENVIRONMENT="production",
        ROO_SURFACE="admin",
        ROO_ADMIN_INTERNAL_ONLY=True,
        ROO_ENABLED_SKILLS="admin-brain",
        ORG_BRAIN_ENABLED=True,
        ORG_BRAIN_ACTIONS_ENABLED=False,
        ORG_BRAIN_API_KEY=SERVICE_TOKEN,
        MLAI_BACKEND_URL="https://backend.test",
        ROO_ADMIN_DISPATCH_SECRET=SECRET,
        ROO_ADMIN_DISPATCH_RECEIPTS_DB_PATH=str(tmp_path / "dispatch.db"),
    )


def _context():
    return BackendActorContext(
        slack_team_id="TMLAI123",
        acting_slack_user_id="UADMIN123",
        slack_channel_id="GADMIN123",
        slack_thread_ts="1700000000.123",
        event_id="Ev01ADMINROUTE",
    )


def _signed(kind, payload):
    envelope, signature = build_admin_dispatch(
        secret=SECRET,
        kind=kind,
        context=_context(),
        payload=payload,
    )
    return envelope, {"X-Roo-Dispatch-Signature": signature}


def setup_function():
    get_slack_receipt_store.cache_clear()


def teardown_function():
    get_slack_receipt_store.cache_clear()


def test_internal_worker_has_no_slack_ingress_and_binds_query_response(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    monkeypatch.setattr(admin_worker, "get_settings", lambda: configured)
    captured = {}

    class FakeExecutor:
        async def _execute_admin_brain(self, **kwargs):
            captured["context"] = get_backend_actor_context()
            captured.update(kwargs)
            return {"message": "Grounded answer", "data": {"query_id": "q1"}}

    monkeypatch.setattr(admin_worker, "SkillExecutor", FakeExecutor)
    envelope, headers = _signed(
        "query",
        {"text": "What did we decide?", "params": {"answer_mode": "summary"}},
    )
    with TestClient(admin_worker.app) as client:
        assert client.post("/slack/events", json={}).status_code == 404
        response = client.post("/internal/admin/query", json=envelope, headers=headers)

    assert response.status_code == 200
    assert response.json()["result"]["message"] == "Grounded answer"
    assert response.json()["destination"] == {
        "channel_id": "GADMIN123",
        "thread_ts": "1700000000.123",
        "requester_user_id": "UADMIN123",
    }
    assert captured["context"] == _context()
    assert get_backend_actor_context() is None


def test_internal_worker_rejects_unsigned_and_replayed_dispatch(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    monkeypatch.setattr(admin_worker, "get_settings", lambda: configured)
    class FakeExecutor:
        async def _execute_admin_brain(self, **kwargs):
            return {"message": "Grounded answer", "data": {}}

    monkeypatch.setattr(admin_worker, "SkillExecutor", FakeExecutor)
    envelope, headers = _signed("query", {"text": "Question", "params": {}})
    with TestClient(admin_worker.app) as client:
        assert client.post("/internal/admin/query", json=envelope).status_code == 403
        first = client.post("/internal/admin/query", json=envelope, headers=headers)
        replay = client.post("/internal/admin/query", json=envelope, headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 403
