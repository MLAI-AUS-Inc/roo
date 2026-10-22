"""Signed mention delivery shares successful reports and never enters model routing."""
import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from roo import main as runtime
from roo import coworking_snapshot_mentions as mentions
from roo.clients.mlai_backend import MLAIBackendClient
from roo.config import Settings, get_settings
from roo.slack_security import get_slack_receipt_store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    configured = Settings(_env_file=None, SLACK_BOT_TOKEN='xoxb-test', SLACK_SIGNING_SECRET='test-secret',
        OPENAI_API_KEY='test', MLAI_BACKEND_URL='https://backend.example.test', ROO_API_KEY='test-roo-key',
        SLACK_RECEIPTS_DB_PATH=str(tmp_path/'receipts.db'))
    runtime.app.dependency_overrides[get_settings] = lambda: configured
    backend = AsyncMock(return_value=httpx.Response(200, request=httpx.Request('GET','https://backend.example.test'),
        json={'date':'2026-09-21','count':1,'people':[{'user_id':'42','name':'Alice <@UEVERYONE>'}]}))
    monkeypatch.setattr(MLAIBackendClient, '_request', backend)
    monkeypatch.setattr(mentions, 'get_bot_user_id', lambda: 'UBOT')
    monkeypatch.setattr('roo.slack_client.get_channel_id', lambda name: None)
    private = Mock(return_value={'ok':True})
    monkeypatch.setattr(mentions, 'post_ephemeral', private)
    public = Mock(return_value={'ok':True})
    monkeypatch.setattr(mentions, 'post_message', public)
    private.public = public
    forbidden = AsyncMock(side_effect=AssertionError('AI/normal routing must not run'))
    for name in ['_handle_app_mention_with_room_choice','_handle_public_message_with_room_choice','_handle_mention']:
        monkeypatch.setattr(runtime, name, forbidden)
    monkeypatch.setattr(runtime, 'get_agent', Mock(side_effect=AssertionError('AI must not run')))
    get_slack_receipt_store.cache_clear()
    yield configured, backend, private, forbidden
    runtime.app.dependency_overrides.clear()
    get_slack_receipt_store.cache_clear()


def event(**overrides):
    return {'type':'app_mention','text':'<@UBOT> coworking-today 2026-09-21',
            'channel':'C123','user':'UADMIN','ts':'123.456', **overrides}


async def send(data, *, event_id='Ev1', tamper=False):
    body = json.dumps({'type':'event_callback','team_id':'T123','event_id':event_id,'event':data}).encode()
    stamp = str(int(time.time()))
    sig = hmac.new(b'test-secret', b'v0:'+stamp.encode()+b':'+body, hashlib.sha256).hexdigest()
    headers = {'X-Slack-Request-Timestamp':stamp,'X-Slack-Signature':'v0='+sig,'Content-Type':'application/json'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.app), base_url='http://test') as client:
        result = await client.post('/slack/events', content=body+(b' ' if tamper else b''), headers=headers)
        await runtime.drain_slack_actions(timeout_seconds=3)
        return result


@pytest.mark.parametrize('thread', [None, '100.001'])
def test_channel_and_thread_return_visible_plain_text(setup, thread):
    _, backend, private, forbidden = setup
    result = asyncio.run(send(event(**({'thread_ts':thread} if thread else {}))))
    # Roo's existing event lease asks Slack to retry until delivery is
    # complete, then acknowledges without running the command again.
    assert result.status_code == 503
    assert asyncio.run(send(event(**({'thread_ts':thread} if thread else {})))).status_code == 200
    private.assert_not_called()
    private.public.assert_called_once()
    reply = private.public.call_args.kwargs
    assert reply['channel'] == 'C123' and 'user' not in reply
    assert reply['thread_ts'] == thread
    assert reply['blocks'][0]['text']['type'] == 'plain_text'
    assert 'Alice <@UEVERYONE>' in reply['blocks'][0]['text']['text']
    assert backend.call_args.kwargs['params'] == {'slack_user_id':'UADMIN','date':'2026-09-21'}
    forbidden.assert_not_called()


@pytest.mark.parametrize('text', ['<@UBOT> coworking-today tomorrow','<@UBOT> coworking-today 2026-02-30',
    '<@UBOT> coworking-today 2026-09-21 extra','<@UBOT> coworking-today connect github'])
def test_malformed_arguments_return_usage_without_ai_or_lookup(setup, text):
    _, backend, private, forbidden = setup
    asyncio.run(send(event(text=text)))
    assert '@Roo coworking-today' in private.call_args.kwargs['text']
    backend.assert_not_called()
    forbidden.assert_not_called()


def test_wrong_bot_cannot_trigger_snapshot(setup):
    _, backend, private, forbidden = setup
    asyncio.run(send(event(text='<@UOTHER> coworking-today 2026-09-21 <@UBOT>')))
    backend.assert_not_called()
    private.assert_not_called()
    forbidden.assert_not_called()


@pytest.mark.parametrize('text', ['coworking-today','@Roo coworking-today','hello <@UBOT> coworking-today',
    '<@UBOT> coworking-report','<@UBOT> coworking-today-ish'])
def test_only_exact_leading_mention_command_matches(text):
    assert mentions.parse_command(text) is None


def test_no_date_parses_as_today():
    assert mentions.parse_command(' <@UBOT> coworking-today ') == ('UBOT', '')


def test_duplicate_channel_message_and_retries_do_not_duplicate_reply(setup):
    _, backend, private, forbidden = setup
    async def run():
        await send(event(type='message'), event_id='EvMessage')
        await send(event(), event_id='EvMention')
        await send(event(), event_id='EvMention')
    asyncio.run(run())
    backend.assert_awaited_once()
    private.assert_not_called()
    private.public.assert_called_once()
    forbidden.assert_not_called()


@pytest.mark.parametrize('allowed', [True, False])
def test_admin_dm_allowlist_and_explicit_mention(setup, allowed):
    configured, backend, private, forbidden = setup
    configured.ROO_SURFACE='admin'
    configured.ROO_ALLOWED_DM_USER_IDS='UADMIN' if allowed else 'UOTHER'
    asyncio.run(send(event(type='message', channel_type='im', channel='D123')))
    assert private.public.call_count == int(allowed)
    private.assert_not_called()
    assert backend.await_count == int(allowed)
    forbidden.assert_not_called()


def test_admin_channel_allowlist_is_preserved(setup):
    configured, backend, private, forbidden = setup
    configured.ROO_SURFACE='admin'
    configured.ROO_ALLOWED_CHANNEL_IDS='GOTHER'
    asyncio.run(send(event()))
    backend.assert_not_called()
    private.assert_not_called()
    forbidden.assert_not_called()


def test_tampered_event_never_executes(setup):
    _, backend, private, forbidden = setup
    assert asyncio.run(send(event(), tamper=True)).status_code == 403
    backend.assert_not_called()
    private.assert_not_called()
    forbidden.assert_not_called()


def test_denied_admin_receives_private_denial_without_names(setup):
    _, backend, private, forbidden = setup
    backend.return_value = httpx.Response(403,request=httpx.Request('GET','https://backend.example.test'))
    asyncio.run(send(event()))
    private.public.assert_not_called()
    assert 'Only active' in private.call_args.kwargs['text']
    assert not private.call_args.kwargs.get('blocks')
    forbidden.assert_not_called()


def test_missing_key_fails_privately_without_fallback(setup):
    configured, backend, private, forbidden = setup
    configured.ROO_API_KEY=''
    configured.MLAI_API_KEY='must-not-use'
    asyncio.run(send(event()))
    assert "Couldn't load" in private.call_args.kwargs['text']
    backend.assert_not_called()
    forbidden.assert_not_called()


def test_failed_public_delivery_releases_receipt_for_retry_without_ai(setup):
    _, backend, private, forbidden = setup
    private.public.side_effect = [{'ok':False}, {'ok':True}]
    async def run():
        await send(event())
        await send(event())
    asyncio.run(run())
    assert private.public.call_count == 2
    private.assert_not_called()
    forbidden.assert_not_called()


def test_unrelated_mention_keeps_normal_routing(setup):
    _, backend, private, normal = setup
    normal.side_effect = None
    asyncio.run(send(event(text='<@UBOT> hello')))
    normal.assert_awaited_once()
    backend.assert_not_called()
    private.assert_not_called()


def test_bot_generated_command_is_ignored(setup):
    _, backend, private, normal = setup
    asyncio.run(send(event(bot_id='BBOT')))
    backend.assert_not_called()
    private.assert_not_called()
    normal.assert_not_called()


def test_no_mention_in_dm_does_not_query_booking_snapshot(setup):
    _, backend, private, normal = setup
    normal.side_effect = None
    asyncio.run(send(event(type='message', channel_type='im', channel='D123', text='coworking-today')))
    backend.assert_not_called()
    private.assert_not_called()


def test_backend_failure_stays_private_in_existing_thread(setup):
    _, backend, private, normal = setup
    backend.side_effect = httpx.ConnectError('sensitive backend diagnostic')
    asyncio.run(send(event(thread_ts='100.001')))
    private.public.assert_not_called()
    assert 'try again' in private.call_args.kwargs['text']
    assert 'sensitive' not in private.call_args.kwargs['text']
    assert private.call_args.kwargs['thread_ts'] == '100.001'
    normal.assert_not_called()
