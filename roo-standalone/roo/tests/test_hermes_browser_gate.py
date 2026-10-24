import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def browser():
    spec = importlib.util.spec_from_file_location("roo_browser_gate", ROOT / "hermes/browser/gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def policy(browser, tmp_path):
    path = tmp_path / "lease.json"
    lease = {"version": 1, "authority_id": "community-roo", "profile_id": "isolated-one",
             "attempt_id": "unique-attempt", "mode": "agent", "expires_at": 1100, "token": "a" * 64}
    path.write_text(json.dumps(lease))
    return browser.LeasePolicy(path, authority_id="community-roo", profile_id="isolated-one", clock=lambda: 1000), lease


def test_exclusive_lease_revokes_existing_version_on_mode_change(policy):
    policy, lease = policy
    version = policy.authorize("agent", "a" * 64)
    with pytest.raises(ValueError):
        policy.authorize("operator")
    lease.update(mode="operator", token="b" * 64)
    policy.path.write_text(json.dumps(lease))
    assert not policy.current("agent", version)
    assert policy.authorize("operator")
    with pytest.raises(ValueError):
        policy.authorize("agent", "a" * 64)


@pytest.mark.parametrize("changes", [
    {"authority_id": "another-member"}, {"profile_id": "another-profile"},
    {"expires_at": 999}, {"expires_at": 1901}, {"token": "short"},
])
def test_invalid_profile_expiry_or_capability_denies_access(policy, changes):
    policy, lease = policy
    lease.update(changes)
    policy.path.write_text(json.dumps(lease))
    with pytest.raises(ValueError):
        policy.authorize("agent", "a" * 64)


def test_cdp_gate_strips_token_and_credentials(browser):
    request = ("GET /devtools/browser/abcd1234-abcd1234?lease=" + "a" * 64 + " HTTP/1.1\r\n"
               "Host: attacker\r\nAuthorization: secret\r\nCookie: secret\r\nOrigin: http://attacker\r\n"
               "Upgrade: websocket\r\nSec-WebSocket-Key: key\r\n\r\n")
    token, forwarded, port = browser.request_header(request.encode(), "agent")
    assert token == "a" * 64 and port == 9222
    assert b"lease=" not in forwarded and b"secret" not in forwarded and b"attacker" not in forwarded
    assert b"Host: 127.0.0.1:9222" in forwarded


@pytest.mark.parametrize("wire_request", [
    b"POST /devtools/browser/abcd1234-abcd1234 HTTP/1.1\r\n\r\n",
    b"GET /json/version HTTP/1.1\r\n\r\n",
    b"GET http://metadata.internal/ HTTP/1.1\r\n\r\n",
    b"GET /devtools/browser/abcd1234-abcd1234?lease=x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
    b"GET /devtools/browser/abcd1234-abcd1234?lease=x HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n",
])
def test_invalid_cdp_routes_or_http_framing_fail(browser, wire_request):
    with pytest.raises(ValueError):
        browser.request_header(wire_request, "agent")


@pytest.mark.asyncio
async def test_open_socket_closes_on_lease_revocation(browser, policy):
    policy, lease = policy
    remote_reader = asyncio.StreamReader()
    class Writer:
        def __init__(self): self.data = bytearray(); self.closed = False
        def write(self, data): self.data.extend(data)
        async def drain(self): pass
        def close(self): self.closed = True
        async def wait_closed(self): pass
    remote_writer = Writer()
    async def connect(host, port):
        assert (host, port) == ("127.0.0.1", 9222)
        return remote_reader, remote_writer
    reader = asyncio.StreamReader()
    reader.feed_data(("GET /devtools/browser/abcd1234-abcd1234?lease=" + "a" * 64 +
                      " HTTP/1.1\r\nUpgrade: websocket\r\n\r\n").encode())
    writer = Writer()
    task = asyncio.create_task(browser.Gate(policy, connector=connect).handle(reader, writer, "agent"))
    for _ in range(100):
        if remote_writer.data:
            break
        await asyncio.sleep(0.005)
    lease.update(mode="operator", token="b" * 64)
    policy.path.write_text(json.dumps(lease))
    await asyncio.wait_for(task, 1)
    assert writer.closed and remote_writer.closed
