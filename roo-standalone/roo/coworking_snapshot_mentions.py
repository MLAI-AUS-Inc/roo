"""Exact mention command delivery, before Roo's conversational routing."""
from __future__ import annotations

import asyncio
import re

from .clients.mlai_backend import MLAIBackendClient
from .config import Settings
from .coworking_snapshot import LOAD_ERROR, handle_command, private
from .slack_client import get_bot_user_id, post_ephemeral, post_message

_COMMAND = re.compile(r'\A\s*<@([A-Z0-9]+)>\s+coworking-today(?:\s+(.*))?\s*\Z', re.DOTALL)


def parse_command(text: str) -> tuple[str, str] | None:
    """Require a real leading Slack mention and an exact command token."""
    match = _COMMAND.fullmatch(text)
    if match is None:
        return None
    return match.group(1), (match.group(2) or '').strip()


async def handle_mention(event: dict, settings: Settings) -> None:
    parsed = parse_command(str(event.get('text') or ''))
    if parsed is None:
        return
    mentioned_user, arguments = parsed
    # The bot ID is discovered from this deployment's own token, never message text.
    bot_id = await asyncio.to_thread(get_bot_user_id)
    if not bot_id or mentioned_user != bot_id:
        return
    if not settings.ROO_API_KEY or not settings.MLAI_BACKEND_URL:
        result = private(LOAD_ERROR)
    else:
        client = MLAIBackendClient(base_url=settings.MLAI_BACKEND_URL,
            api_key=settings.ROO_API_KEY, surface=settings.ROO_SURFACE)
        result = await handle_command(arguments, str(event.get('user') or ''), client)
    successful = result['response_type'] == 'in_channel'
    destination = {'channel': event['channel']}
    if not successful:
        destination['user'] = event['user']
    response = await asyncio.to_thread(
        post_message if successful else post_ephemeral,
        **destination,
        text=result['text'], blocks=result.get('blocks'),
        thread_ts=event.get('thread_ts'),
        redact_logs=True,
    )
    if not response.get('ok'):
        # Release the event lease for retries without changing visibility or
        # falling back to conversational routing.
        raise RuntimeError('Coworking reply failed')
