"""Restricted HTTPS CONNECT egress. No credentials, HTTP forwarding or MITM.

The task network must be internal; this service alone has an outbound network.
Resolve once, reject every non-public answer, connect to that pinned IPv4, and
never follow redirects. A redirected website requires a new checked CONNECT.
"""

import asyncio
import ipaddress
import os
import re
import socket


HOST_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")


def allowlist(raw: str) -> frozenset[str]:
    hosts = frozenset(h.strip().lower() for h in raw.split(",") if h.strip())
    if not hosts or any(not HOST_RE.fullmatch(h) for h in hosts):
        raise ValueError("Configure explicit public DNS hostnames, without wildcards")
    if len(hosts) > 128:
        raise ValueError("Too many allowed hosts")
    return hosts


def destination(authority: str, hosts: frozenset[str]) -> str:
    if authority.count(":") != 1:
        raise ValueError("HTTPS hostname:443 required")
    host, port = authority.rsplit(":", 1)
    host = host.lower()
    if port != "443" or not HOST_RE.fullmatch(host) or host not in hosts:
        raise ValueError("Destination not allowed")
    return host


async def resolve_public(host: str) -> str:
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    addresses = {record[4][0] for record in records}
    if not addresses or any(not public_ipv4(a) for a in addresses):
        raise ValueError("Destination has a private or special-use address")
    return sorted(addresses)[0]


def public_ipv4(address: str) -> bool:
    ip = ipaddress.IPv4Address(address)
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def client_hello_hostname(body: bytes) -> str:
    """Conservative TLS ClientHello parser: reject fragments, ECH and ambiguity.

    This keeps the TLS SNI consistent with CONNECT. It does not decrypt HTTPS or
    inspect URLs/HTTP Host. Avoid allowlisting services that offer open proxies
    or domain fronting; application authorization still belongs in the broker.
    """
    try:
        if body[0] != 1 or int.from_bytes(body[1:4], "big") != len(body) - 4:
            raise ValueError("client hello")
        pos = 4 + 2 + 32
        pos += 1 + body[pos]
        length = int.from_bytes(body[pos:pos + 2], "big")
        pos += 2 + length
        pos += 1 + body[pos]
        length = int.from_bytes(body[pos:pos + 2], "big")
        pos += 2
        if pos + length != len(body):
            raise ValueError("extensions")
        names = []
        while pos < len(body):
            if len(body) - pos < 4:
                raise ValueError("truncated extension")
            kind = int.from_bytes(body[pos:pos + 2], "big")
            size = int.from_bytes(body[pos + 2:pos + 4], "big")
            pos += 4
            value = body[pos:pos + size]
            if len(value) != size or kind == 0xFE0D:
                raise ValueError("invalid or encrypted server name")
            pos += size
            if kind == 0:
                if len(value) < 5 or int.from_bytes(value[:2], "big") != len(value) - 2:
                    raise ValueError("server name")
                if value[2] != 0 or int.from_bytes(value[3:5], "big") != len(value) - 5:
                    raise ValueError("ambiguous server name")
                names.append(value[5:].decode("ascii").lower())
        if len(names) != 1 or not HOST_RE.fullmatch(names[0]):
            raise ValueError("server name required")
        return names[0]
    except (IndexError, UnicodeError) as exc:
        raise ValueError("malformed client hello") from exc


class Proxy:
    def __init__(self, hosts: frozenset[str], *, resolver=resolve_public, connector=asyncio.open_connection,
                 timeout_seconds=120, max_bytes=32 * 1024 * 1024):
        self.hosts, self.resolver, self.connector = hosts, resolver, connector
        self.timeout_seconds, self.max_bytes = timeout_seconds, max_bytes
        self.active = 0

    async def handle(self, reader, writer):
        remote_writer = None
        admitted = False
        connected = False
        try:
            if self.active >= 32:
                raise ValueError("capacity")
            self.active += 1
            admitted = True
            async with asyncio.timeout(self.timeout_seconds):
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                if len(header) > 8192:
                    raise ValueError("headers")
                first = header.split(b"\r\n", 1)[0].decode("ascii")
                method, authority, version = first.split(" ")
                if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise ValueError("CONNECT required")
                host = destination(authority, self.hosts)
                address = await asyncio.wait_for(self.resolver(host), 10)
                if not public_ipv4(address):
                    raise ValueError("private address")
                # Pass the literal validated address, never the DNS name, to the
                # socket connector. This closes a lookup/connect rebinding gap.
                remote_reader, remote_writer = await asyncio.wait_for(self.connector(address, 443, family=socket.AF_INET), 10)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                connected = True
                # HTTPS only: an arbitrary raw TCP protocol or a different SNI
                # cannot be tunneled through an allowlisted destination.
                tls_header = await asyncio.wait_for(reader.readexactly(5), 10)
                size = int.from_bytes(tls_header[3:5], "big")
                if tls_header[0] != 22 or tls_header[1] != 3 or not 40 <= size <= 16384:
                    raise ValueError("TLS required")
                hello = await asyncio.wait_for(reader.readexactly(size), 10)
                if client_hello_hostname(hello) != host:
                    raise ValueError("SNI mismatch")
                remote_writer.write(tls_header + hello)
                await remote_writer.drain()
                transferred = len(tls_header) + len(hello)

                async def copy(source, target):
                    nonlocal transferred
                    while chunk := await source.read(65536):
                        transferred += len(chunk)
                        if transferred > self.max_bytes:
                            raise ValueError("transfer budget")
                        target.write(chunk)
                        await target.drain()

                flows = [asyncio.create_task(copy(reader, remote_writer)), asyncio.create_task(copy(remote_reader, writer))]
                try:
                    done, _ = await asyncio.wait(flows, return_when=asyncio.FIRST_COMPLETED)
                    for flow in done:
                        flow.result()
                finally:
                    for flow in flows:
                        flow.cancel()
                    await asyncio.gather(*flows, return_exceptions=True)
        except (ValueError, OSError, UnicodeError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            if not connected:
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
    proxy = Proxy(allowlist(os.environ.get("ROO_HERMES_WEB_HOSTS", "")))
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", 3128, limit=8192)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
