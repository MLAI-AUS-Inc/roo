import asyncio
import importlib.util
from pathlib import Path
import socket

import pytest


spec = importlib.util.spec_from_file_location("roo_egress", Path(__file__).resolve().parents[2] / "hermes/egress/proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


def hello(host, extra=b""):
    name = host.encode()
    names = b"\0" + len(name).to_bytes(2, "big") + name
    extension = len(names).to_bytes(2, "big") + names
    extensions = b"\0\0" + len(extension).to_bytes(2, "big") + extension + extra
    body = b"\x03\x03" + b"\0" * 32 + b"\0\0\2\x13\1\1\0" + len(extensions).to_bytes(2, "big") + extensions
    handshake = b"\1" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\1" + len(handshake).to_bytes(2, "big") + handshake


@pytest.mark.parametrize("value", ["*", "*.example.com", "localhost", "127.0.0.1", "example.com:443", "example.com/path", "example.com\nX: injected", ""])
def test_allowlist_rejects_ambiguous_destinations(value):
    with pytest.raises(ValueError):
        proxy.allowlist(value)


@pytest.mark.parametrize("authority", ["example.com:80", "evil.com:443", "example.com.evil.com:443", "user@example.com:443", "127.0.0.1:443", "[::1]:443", "example.com:0443", "example.com.:443"])
def test_connect_target_is_exact(authority):
    with pytest.raises(ValueError):
        proxy.destination(authority, frozenset({"example.com"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "192.0.2.1", "224.0.0.1", "100.64.0.1"])
async def test_dns_rejects_any_private_or_special_answer(monkeypatch, address):
    async def lookup(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ("1.1.1.1", address)]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(ValueError):
        await proxy.resolve_public("example.com")


def test_tls_hostname_and_encrypted_name_policy():
    assert proxy.client_hello_hostname(hello("example.com")[5:]) == "example.com"
    for packet in (hello("example.com", b"\xfe\x0d\0\0"), hello("example.com")[:-1], b"not TLS"):
        with pytest.raises(ValueError):
            proxy.client_hello_hostname(packet[5:])


class Writer:
    def __init__(self):
        self.data = bytearray()
    def write(self, data):
        self.data.extend(data)
    async def drain(self):
        pass
    def close(self):
        pass
    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_proxy_pins_dns_address_and_checks_sni_before_forwarding():
    called = []
    remote = Writer()
    async def resolve(host):
        called.append(("resolve", host))
        return "1.1.1.1"
    async def connect(address, port, **kwargs):
        called.append(("connect", address, port))
        stream = asyncio.StreamReader()
        stream.feed_eof()
        return stream, remote
    handler = proxy.Proxy(frozenset({"example.com"}), resolver=resolve, connector=connect)
    for sni in ("evil.com", "example.com"):
        reader = asyncio.StreamReader()
        packet = hello(sni)
        reader.feed_data(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n" + packet)
        reader.feed_eof()
        client = Writer()
        await handler.handle(reader, client)
        if sni == "evil.com":
            assert not remote.data
        else:
            assert bytes(remote.data) == packet
    assert called == [("resolve", "example.com"), ("connect", "1.1.1.1", 443)] * 2
    assert handler.active == 0
