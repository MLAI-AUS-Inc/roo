import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roo.admin_dispatch import (
    AdminDispatchError,
    actor_context_from_dispatch,
    build_admin_dispatch,
    verify_and_claim_admin_dispatch,
)
from roo.backend_identity import BackendActorContext
from roo.slack_security import get_slack_receipt_store


SECRET = "dispatch-secret-" + ("s" * 32)


def _context():
    return BackendActorContext(
        slack_team_id="TMLAI123",
        acting_slack_user_id="UADMIN123",
        slack_channel_id="GADMIN123",
        slack_thread_ts="1700000000.123",
        event_id="Ev01ADMINROUTE",
    )


def setup_function():
    get_slack_receipt_store.cache_clear()


def teardown_function():
    get_slack_receipt_store.cache_clear()


def _raw(envelope):
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def test_dispatch_binds_actor_destination_kind_and_payload(tmp_path):
    envelope, signature = build_admin_dispatch(
        secret=SECRET,
        kind="query",
        context=_context(),
        payload={"text": "What did the committee decide?", "params": {}},
        issued_at=1_700_000_000,
        ttl_seconds=45,
        nonce="fixed_nonce_123456789012345",
    )

    verified = verify_and_claim_admin_dispatch(
        secret=SECRET,
        signature=signature,
        raw_body=_raw(envelope),
        receipt_db_path=str(tmp_path / "receipts.db"),
        now=1_700_000_001,
    )

    assert verified["kind"] == "query"
    assert verified["actor"]["acting_slack_user_id"] == "UADMIN123"
    assert verified["actor"]["slack_channel_id"] == "GADMIN123"
    assert actor_context_from_dispatch(verified) == _context()


def test_dispatch_rejects_tampering_expiry_and_replay(tmp_path):
    envelope, signature = build_admin_dispatch(
        secret=SECRET,
        kind="query",
        context=_context(),
        payload={"text": "Original", "params": {}},
        issued_at=1_700_000_000,
        ttl_seconds=45,
        nonce="fixed_nonce_123456789012345",
    )
    database = str(tmp_path / "receipts.db")
    tampered = {**envelope, "payload": {"text": "Tampered", "params": {}}}
    with pytest.raises(AdminDispatchError, match="does not match"):
        verify_and_claim_admin_dispatch(
            secret=SECRET,
            signature=signature,
            raw_body=_raw(tampered),
            receipt_db_path=database,
            now=1_700_000_001,
        )

    verify_and_claim_admin_dispatch(
        secret=SECRET,
        signature=signature,
        raw_body=_raw(envelope),
        receipt_db_path=database,
        now=1_700_000_001,
    )
    with pytest.raises(AdminDispatchError, match="already been used"):
        verify_and_claim_admin_dispatch(
            secret=SECRET,
            signature=signature,
            raw_body=_raw(envelope),
            receipt_db_path=database,
            now=1_700_000_002,
        )
    with pytest.raises(AdminDispatchError, match="expired"):
        verify_and_claim_admin_dispatch(
            secret=SECRET,
            signature=signature,
            raw_body=_raw(envelope),
            receipt_db_path=str(tmp_path / "expired.db"),
            now=1_700_000_046,
        )
