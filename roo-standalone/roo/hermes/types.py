"""Task-local contracts. No upstream service credentials belong in these types."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class HermesRunRequest:
    task_id: str
    prompt: str
    workspace: Path
    state_dir: Path
    model: str
    broker_url: str
    broker_token: str = field(repr=False)
    model_proxy_url: str
    model_proxy_token: str = field(repr=False)
    timeout_seconds: float = 120
    max_output_bytes: int = 1_048_576
    max_artifact_bytes: int = 8_388_608
    max_artifacts: int = 16
    max_turns: int = 20
    browser_enabled: bool = False
    approved_permissions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HermesArtifact:
    relative_path: str
    size_bytes: int
    sha256: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class HermesRunResult:
    status: Literal["completed", "cancelled", "timed_out", "failed", "approval_required"]
    text: str = ""
    artifacts: tuple[HermesArtifact, ...] = ()
    session_id: str | None = None
    error_code: str | None = None
    permission_requests: tuple[str, ...] = ()
    permission_details: tuple[dict, ...] = ()
