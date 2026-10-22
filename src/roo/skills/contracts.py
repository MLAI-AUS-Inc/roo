"""Common input and output contracts for independently executable skills."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Protocol
from .loader import Skill

@dataclass
class SkillRequest:
    skill: Skill
    text: str
    user_id: str
    params: dict = field(default_factory=dict)
    channel_id: str | None = None
    thread_ts: str | None = None
    thread_history: list[dict] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class SkillResult:
    success: bool
    message: str
    data: Any = None
    error: str | None = None
    blocks: list | None = None
    suppress_post: bool = False

class SkillHandler(Protocol):
    async def execute(self, request: SkillRequest) -> str | dict[str, Any]:
        """Run one capability; the coordinator normalizes its response."""
        ...
