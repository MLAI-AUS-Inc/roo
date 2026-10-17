"""Exclusive browser transport gate, driven by a read-only controller lease.

A changed/expired lease closes existing sockets as well as rejecting new ones.
The operator listener is private to the TLS+auth proxy. Nothing here grants or
renews authority: the backend/controller must stop the agent before issuing an
operator lease and revalidate the task before issuing a new agent lease.
"""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit


ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
TOKEN = re.compile(r"[0-9a-f]{64}\Z")


class LeasePolicy:
    def __init__(self, path, *, authority_id, profile_id, clock=time.time):
        if not ID.fullmatch(authority_id) or not ID.fullmatch(profile_id):
            raise ValueError("Explicit authority/profile binding required")
        self.path, self.authority, self.profile, self.clock = Path(path), authority_id, profile_id, clock

    def read(self, expected_mode):
        data = self.path.read_bytes()
        if len(data) > 4096:
            raise ValueError("Lease too large")
        lease = json.loads(data)
        if (set(lease) != {"version", "authority_id", "profile_id", "attempt_id", "mode", "expires_at", "token"}
                or lease["version"] != 1 or lease["authority_id"] != self.authority
                or lease["profile_id"] != self.profile or lease["mode"] != expected_mode
                or not isinstance(lease["attempt_id"], str) or not ID.fullmatch(lease["attempt_id"])
                or type(lease["expires_at"]) not in (float, int)
                or not self.clock() < lease["expires_at"] <= self.clock() + 900
                or not isinstance(lease["token"], str) or not TOKEN.fullmatch(lease["token"])):
            raise ValueError("Browser lease is invalid, expired or belongs to another authority")
        return lease, hashlib.sha256(data).digest()

    def authorize(self, mode, token=""):
        lease, version = self.read(mode)
        if mode == "agent" and not hmac.compare_digest(token, lease["token"]):
            raise ValueError("Invalid browser capability")
        return version

    def current(self, mode, version):
        try:
            return hmac.compare_digest(self.read(mode)[1], version)
        except (ValueError, OSError, TypeError):
            return False


def request_header(data, mode):
    if len(data) > 16384:
        raise ValueError("Header limit")
    lines = data.decode("ascii").split("\r\n")
    method, target, version = lines[0].split(" ")
    parsed = urlsplit(target)
    if (method != "GET" or version != "HTTP/1.1" or not target.startswith("/")
            or target.startswith("//") or parsed.scheme or parsed.netloc or parsed.fragment
            or "\\" in target or any(ord(c) < 33 for c in target)):
        raise ValueError("Invalid browser request")
    token = ""
    if mode == "agent":
        # Full browser WS endpoint supplied by the controller. No discovery or
        # arbitrary HTTP service is exposed on the agent listener.
        if not re.fullmatch(r"/devtools/browser/[0-9A-Fa-f-]{16,64}", parsed.path):
            raise ValueError("Only the browser CDP websocket is exposed")
        query = parse_qs(parsed.query, strict_parsing=True)
        if set(query) != {"lease"} or len(query["lease"]) != 1:
            raise ValueError("Missing browser capability")
        token = query["lease"][0]
        target = parsed.path
    forwarded = []
    names = set()
    upgrade = False
    for line in lines[1:]:
        if not line:
            continue
        if line[0].isspace() or ":" not in line:
            raise ValueError("Invalid header")
        key, value = line.split(":", 1)
        key, value = key.lower(), value.strip()
        if not re.fullmatch(r"[a-z0-9-]+", key) or key in names:
            raise ValueError("Repeated or invalid header")
        names.add(key)
        if key == "transfer-encoding" or (key == "content-length" and value != "0"):
            raise ValueError("Request bodies are not supported")
        if key == "upgrade":
            if value.lower() != "websocket":
                raise ValueError("Only websocket upgrades are supported")
            upgrade = True
        if key in {"upgrade", "sec-websocket-key", "sec-websocket-version", "sec-websocket-protocol", "accept", "accept-encoding", "if-modified-since", "if-none-match"}:
            forwarded.append(f"{key}: {value}\r\n")
    if mode == "agent" and not upgrade:
        raise ValueError("A websocket is required")
    upstream = 9222 if mode == "agent" else 6080
    # Never forward authentication/query capabilities to Chromium or noVNC.
    header = (f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{upstream}\r\n"
              f"Connection: {'Upgrade' if upgrade else 'close'}\r\n" + "".join(forwarded) + "\r\n")
    return token, header.encode("ascii"), upstream


class Gate:
    def __init__(self, policy, *, connector=asyncio.open_connection):
        self.policy, self.connector, self.active = policy, connector, 0

    async def handle(self, reader, writer, mode):
        remote_writer = None
        admitted = False
        try:
            if self.active >= 32:
                raise ValueError("Capacity")
            self.active += 1
            admitted = True
            async with asyncio.timeout(900):
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                token, header, port = request_header(raw, mode)
                version = self.policy.authorize(mode, token)
                remote, remote_writer = await asyncio.wait_for(self.connector("127.0.0.1", port), 5)
                if not self.policy.current(mode, version):
                    raise ValueError("Lease changed")
                remote_writer.write(header)
                await remote_writer.drain()
                total = 0

                async def transfer(source, target):
                    nonlocal total
                    while chunk := await source.read(65536):
                        if not self.policy.current(mode, version):
                            raise ValueError("Lease revoked")
                        total += len(chunk)
                        if total > 1_073_741_824:
                            raise ValueError("Transfer limit")
                        target.write(chunk)
                        await target.drain()

                async def watch():
                    while self.policy.current(mode, version):
                        await asyncio.sleep(0.2)

                tasks = [asyncio.create_task(transfer(reader, remote_writer)),
                         asyncio.create_task(transfer(remote, writer)), asyncio.create_task(watch())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (ValueError, TypeError, OSError, TimeoutError, UnicodeError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            if remote_writer is None:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                try:
                    await writer.drain()
                except OSError:
                    pass
        finally:
            if admitted:
                self.active -= 1
            for stream in (remote_writer, writer):
                if stream:
                    stream.close()
                    try:
                        await asyncio.wait_for(stream.wait_closed(), 1)
                    except (OSError, TimeoutError):
                        pass


async def main():
    policy = LeasePolicy("/run/roo-browser/lease.json", authority_id=os.environ.get("ROO_BROWSER_AUTHORITY_ID", ""),
                         profile_id=os.environ.get("ROO_BROWSER_PROFILE_ID", ""))
    gate = Gate(policy)
    agent = await asyncio.start_server(lambda r, w: gate.handle(r, w, "agent"), "0.0.0.0", 9223, limit=16384)
    # Operator transport is only accessible to nginx in the same container.
    operator = await asyncio.start_server(lambda r, w: gate.handle(r, w, "operator"), "127.0.0.1", 8081, limit=16384)
    async with agent, operator:
        await asyncio.gather(agent.serve_forever(), operator.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
