"""Small, dependency-free security helpers shared by public AI personas."""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional


def make_safety_identifier(player_id: str, salt: Optional[str]) -> Optional[str]:
    """Return a stable privacy-preserving OpenAI safety identifier."""
    secret = str(salt or "").strip()
    if not secret:
        return None
    digest = hmac.new(
        secret.encode("utf-8"),
        str(player_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"health-hack-{digest[:40]}"
