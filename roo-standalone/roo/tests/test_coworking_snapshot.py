"""Deterministic command behaviour; all network boundaries are synthetic."""
import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from roo import coworking_snapshot as command
from roo.clients.mlai_backend import MLAIBackendClient, MLAIBackendUnavailableError


def client_with(data):
    client = AsyncMock()
    client.get_coworking_snapshot.return_value = data
    return client


def empty(day='2026-09-21'):
    return {'date':day, 'count':0, 'people':[]}


@pytest.mark.parametrize(('instant', 'expected'), [
    ('2026-09-20T14:30:00+00:00', '2026-09-21'),
    ('2026-10-03T16:30:00+00:00', '2026-10-04'),
    ('2026-04-04T16:30:00+00:00', '2026-04-05'),
])
def test_today_uses_melbourne_calendar_day(instant, expected):
    client = client_with(empty(expected))
    result = asyncio.run(command.handle_command('', 'UADMIN', client, now=datetime.fromisoformat(instant)))
    client.get_coworking_snapshot.assert_awaited_once_with('UADMIN', expected)
    assert result['response_type'] == 'ephemeral'
    assert 'No active bookings' in result['text']


@pytest.mark.parametrize('text', ['tomorrow', '20260921', '2026-02-30', '2026-09-21 extra', 'connect github'])
def test_invalid_input_is_usage_without_lookup(text):
    client = client_with(empty())
    result = asyncio.run(command.handle_command(text, 'UADMIN', client))
    assert 'Usage:' in result['text']
    assert result['response_type'] == 'ephemeral'
    client.get_coworking_snapshot.assert_not_awaited()


def test_explicit_date_and_whitespace_ignore_current_date():
    client = client_with(empty())
    result = asyncio.run(command.handle_command(' 2026-09-21 ', 'UADMIN', client,
        now=datetime.fromisoformat('2026-01-01T00:00:00+00:00')))
    client.get_coworking_snapshot.assert_awaited_once_with('UADMIN', '2026-09-21')
    assert '21 September 2026' in result['text']


def test_missing_actor_makes_no_request():
    client = client_with(empty())
    result = asyncio.run(command.handle_command('', '', client))
    assert result['response_type'] == 'ephemeral'
    assert 'requester' in result['text']
    client.get_coworking_snapshot.assert_not_awaited()


@pytest.mark.parametrize('data', [None, {}, {'date':'2026-09-21','count':1,'people':[]},
    {'date':'2026-09-20','count':0,'people':[]}, {'date':'2026-09-21','count':True,'people':[]},
    {'date':'2026-09-21','count':1,'people':[{'user_id':'1','name':''}]},
    {'date':'2026-09-21','count':2,'people':[{'user_id':'1','name':'A'},{'user_id':'1','name':'B'}]},
    {'date':'2026-09-21','count':1,'people':[None]},
])
def test_malformed_snapshot_is_private_error_not_empty_success(data):
    result = asyncio.run(command.handle_command('2026-09-21', 'UADMIN', client_with(data)))
    assert result['response_type'] == 'ephemeral'
    assert "Couldn't load" in result['text']
    assert 'No active bookings' not in result['text']


@pytest.mark.parametrize('status', [401,403,500,503])
def test_http_failures_are_private(status):
    client = client_with(empty())
    response = httpx.Response(status, request=httpx.Request('GET','https://backend.example.test/'))
    client.get_coworking_snapshot.side_effect = httpx.HTTPStatusError('error', request=response.request, response=response)
    result = asyncio.run(command.handle_command('2026-09-21', 'UADMIN', client))
    assert result['response_type'] == 'ephemeral'
    assert ('Only active' if status == 403 else "Couldn't load") in result['text']


@pytest.mark.parametrize('error', [httpx.ConnectError('network'), MLAIBackendUnavailableError('unavailable'), ValueError('invalid JSON')])
def test_transport_and_client_errors_do_not_leak_details(error):
    client = client_with(empty())
    client.get_coworking_snapshot.side_effect = error
    result = asyncio.run(command.handle_command('2026-09-21', 'UADMIN', client))
    assert result['response_type'] == 'ephemeral'
    assert 'try again' in result['text']
    assert str(error) not in result['text']


def test_lookup_total_budget_cancels_slow_request(monkeypatch):
    monkeypatch.setattr(command, 'LOOKUP_BUDGET', 0.01)
    cancelled = []
    async def slow(*args):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    client = client_with(empty())
    client.get_coworking_snapshot.side_effect = slow
    result = asyncio.run(command.handle_command('2026-09-21', 'UADMIN', client))
    assert 'try again' in result['text']
    assert result['response_type'] == 'ephemeral'
    assert cancelled == [True]


def test_names_are_plain_text_and_all_people_survive_chunking():
    people = [{'user_id': str(i), 'name': f'Member {i} <!channel> & <@U123>'} for i in range(150)]
    result = command.render_snapshot({'date':'2026-09-21', 'count':150, 'people':people}, date(2026,9,21))
    assert len(result['blocks']) > 1
    assert all(b['text']['type'] == 'plain_text' and len(b['text']['text']) <= 2900 for b in result['blocks'])
    body = '\n'.join(b['text']['text'] for b in result['blocks'])
    assert all(p['name'] in body for p in people)
    assert 'Member' not in result['text']  # fallback has no untrusted markup


def test_newlines_do_not_forge_extra_people_and_singular_copy():
    result = command.render_snapshot({'date':'2026-09-21', 'count':1,
        'people':[{'user_id':'1','name':'Alice\n Smith'}]}, date(2026,9,21))
    assert '1 person booked' in result['text']
    assert '• Alice Smith' in result['blocks'][0]['text']['text']


def test_oversized_name_is_reported_not_silently_truncated():
    result = command.render_snapshot({'date':'2026-09-21','count':1,
        'people':[{'user_id':'1','name':'A'*3000}]}, date(2026,9,21))
    assert 'too large' in result['text']
    assert result['response_type'] == 'ephemeral'


def test_backend_client_wire_contract():
    client = MLAIBackendClient(base_url='https://backend.example.test', api_key='synthetic-key')
    client._request = AsyncMock(return_value=httpx.Response(200,
        request=httpx.Request('GET','https://backend.example.test/'), json=empty()))
    assert asyncio.run(client.get_coworking_snapshot('UADMIN', '2026-09-21')) == empty()
    client._request.assert_awaited_once_with('GET', '/api/v1/points/coworking/bookings-for-date/',
        params={'slack_user_id':'UADMIN','date':'2026-09-21'}, timeout=1.5,
        transport_retries=0, circuit_breaker=True)
