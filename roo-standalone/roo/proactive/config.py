"""Explicit worker configuration. Never loads Public or Admin Roo's .env."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit


def root_url(value: str, *, allow_http: bool = False) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid service origin") from exc
    if (parsed.scheme not in ({"https", "http"} if allow_http else {"https"})
            or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or port == 0):
        raise ValueError("An explicit service origin without credentials or a path is required")
    return value.rstrip("/")


def _boolean(env, name, default=False):
    raw = str(env.get(name, str(default))).lower()
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return raw == "true"


def _integer(env, name, default, lower, upper):
    value = int(env.get(name, default))
    if not lower <= value <= upper:
        raise ValueError(f"{name} is outside its supported range")
    return value


@dataclass(frozen=True)
class WorkerConfig:
    enabled: bool = False
    backend_origin: str = ""
    worker_key: str = field(default="", repr=False)
    broker_origin: str = ""
    model_origin: str = ""
    worker_id: str = "roo-hermes"
    image: str = ""
    network: str = "roo-hermes-internal"
    public_web_enabled: bool = False
    web_network: str = "roo-hermes-web"
    state_root: Path = Path("/var/lib/roo-hermes/tasks")
    runtime_uid: int = 10000
    runtime_gid: int = 10000
    poll_seconds: int = 10
    heartbeat_seconds: int = 10
    timeout_seconds: int = 600
    memory_mb: int = 2048
    cpus: int = 2
    pids_limit: int = 256
    max_output_bytes: int = 512_000
    max_artifact_bytes: int = 6_291_456
    max_artifacts: int = 10
    retain_failed_workspaces: bool = False

    @classmethod
    def from_env(cls, env):
        if not _boolean(env, "ROO_HERMES_ENABLED"):
            return cls()
        allow_http = _boolean(env, "ROO_HERMES_ALLOW_PRIVATE_HTTP")
        image = str(env.get("ROO_HERMES_IMAGE", ""))
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9./_:-]*@sha256:[0-9a-f]{64}", image):
            raise ValueError("ROO_HERMES_IMAGE must use an immutable image digest")
        key = str(env.get("ROO_HERMES_WORKER_KEY", ""))
        if len(key) < 32 or any(c.isspace() for c in key):
            raise ValueError("A dedicated worker key of at least 32 characters is required")
        worker_id = str(env.get("ROO_HERMES_WORKER_ID", "roo-hermes"))
        network = str(env.get("ROO_HERMES_NETWORK", "roo-hermes-internal"))
        web_network = str(env.get("ROO_HERMES_WEB_NETWORK", "roo-hermes-web"))
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", worker_id):
            raise ValueError("Invalid worker ID")
        if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value) for value in (network, web_network)) or network == web_network:
            raise ValueError("Invalid sandbox network")
        root = Path(env.get("ROO_HERMES_STATE_ROOT", "/var/lib/roo-hermes/tasks"))
        if not root.is_absolute() or root == Path("/") or "," in str(root):
            raise ValueError("A dedicated absolute state directory is required")
        return cls(
            enabled=True,
            backend_origin=root_url(str(env.get("ROO_HERMES_BACKEND_ORIGIN", ""))),
            broker_origin=root_url(str(env.get("ROO_HERMES_BROKER_ORIGIN", "")), allow_http=allow_http),
            model_origin=root_url(str(env.get("ROO_HERMES_MODEL_ORIGIN", "")), allow_http=allow_http),
            worker_key=key, worker_id=worker_id, image=image, network=network, state_root=root,
            public_web_enabled=_boolean(env, "ROO_HERMES_PUBLIC_WEB_ENABLED"), web_network=web_network,
            runtime_uid=_integer(env, "ROO_HERMES_RUNTIME_UID", 10000, 1, 65535),
            runtime_gid=_integer(env, "ROO_HERMES_RUNTIME_GID", 10000, 1, 65535),
            poll_seconds=_integer(env, "ROO_HERMES_POLL_SECONDS", 10, 1, 300),
            heartbeat_seconds=_integer(env, "ROO_HERMES_HEARTBEAT_SECONDS", 10, 1, 30),
            timeout_seconds=_integer(env, "ROO_HERMES_TIMEOUT_SECONDS", 600, 10, 3600),
            memory_mb=_integer(env, "ROO_HERMES_MEMORY_MB", 2048, 256, 16384),
            cpus=_integer(env, "ROO_HERMES_CPUS", 2, 1, 8),
            pids_limit=_integer(env, "ROO_HERMES_PIDS_LIMIT", 256, 32, 1024),
            max_artifact_bytes=_integer(env, "ROO_HERMES_MAX_ARTIFACT_BYTES", 6_291_456, 1024, 6_291_456),
            retain_failed_workspaces=_boolean(env, "ROO_HERMES_RETAIN_FAILED_WORKSPACES"),
        )
