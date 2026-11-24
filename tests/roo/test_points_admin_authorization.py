"""Points Admin authority uses verified Slack identity, not model parameters."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from roo.backend_identity import BackendActorContext, BackendIdentityError, use_backend_actor_context
from roo.clients.mlai_backend import MLAIBackendClient
from roo.config import Settings
from roo.skills.executors import mlai_points
from roo.skills.executors.mlai_points import MlaiPointsExecutor

ACTIONS = ('promote_points_admin', 'revoke_points_admin', 'set_points_admin_allowance')
CLIENT_METHODS = ('promote_points_admin', 'revoke_points_admin', 'set_points_admin_weekly_allowance')


def actor(user='UDEVADMIN', team='TDEV'):
    return BackendActorContext(team, user, 'CTEST', '111.222', 'Ev-test')


def configure(monkeypatch, user='UDEVADMIN', team='TDEV'):
    monkeypatch.setattr(mlai_points, 'get_settings', lambda: SimpleNamespace(
        POINTS_SUPER_ADMIN_SLACK_ID=user, POINTS_SUPER_ADMIN_SLACK_TEAM_ID=team,
    ))


def test_settings_load_environment_without_dotenv(monkeypatch):
    monkeypatch.setenv('POINTS_SUPER_ADMIN_SLACK_ID', 'UDEVADMIN')
    monkeypatch.setenv('POINTS_SUPER_ADMIN_SLACK_TEAM_ID', 'TDEV')
    settings = Settings(_env_file=None)
    assert settings.POINTS_SUPER_ADMIN_SLACK_ID == 'UDEVADMIN'
    assert settings.POINTS_SUPER_ADMIN_SLACK_TEAM_ID == 'TDEV'


@pytest.mark.parametrize('user,team', [('UDEVADMIN', 'TDEV'), ('U05QPB483K9', 'TPROD')])
def test_configured_identity_allowed(monkeypatch, user, team):
    configure(monkeypatch, user, team)
    with use_backend_actor_context(actor(user, team)):
        assert MlaiPointsExecutor()._is_points_super_admin(user)
        assert not MlaiPointsExecutor()._is_points_super_admin('UOTHER')


@pytest.mark.parametrize('user,team', [
    ('', 'TDEV'), ('UDEVADMIN', ''), ('', ''),
    ('<@UDEVADMIN>', 'TDEV'), ('uDEV', 'TDEV'), ('B123', 'TDEV'),
    ('UDEVADMIN,UOTHER', 'TDEV'), ('UDEVADMIN', 'CDEV'),
    ('UDEVADMIN', 'TDEV,TOTHER'), ('UDEVADMIN ', 'TDEV'),
    (None, 'TDEV'), ('UDEVADMIN', None),
])
def test_invalid_configuration_denies_even_matching_context(monkeypatch, user, team):
    configure(monkeypatch, user, team)
    with use_backend_actor_context(actor(user, team)):
        assert not MlaiPointsExecutor()._is_points_super_admin(user)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ACTIONS)
@pytest.mark.parametrize('context', [None, actor(team='TOTHER'), actor(team=''), actor(user='UOTHER')])
async def test_untrusted_identity_denied_before_any_side_effect(monkeypatch, action, context):
    configure(monkeypatch)
    executor = MlaiPointsExecutor()
    ensure = AsyncMock()
    monkeypatch.setattr(executor, '_ensure_user_exists', ensure)
    client = SimpleNamespace(**{name: AsyncMock() for name in CLIENT_METHODS})
    with use_backend_actor_context(context):
        result = await executor._handle_points_action(
            client=client, action=action,
            # Model-supplied identity must not substitute for verified context.
            params={'weekly_allowance': 150, 'slack_team_id': 'TDEV'},
            text='promote <@UTARGET> to points admin', user_id='UDEVADMIN',
            channel_id='CTEST', thread_ts='111.222', skill=None,
        )
    assert 'only the configured Points Admin manager' in result
    ensure.assert_not_awaited()
    for name in CLIENT_METHODS:
        getattr(client, name).assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('method,args,http_method,endpoint', [
    ('promote_points_admin', ('UDEVADMIN', 'UTARGET'), 'POST', '/api/v1/points/admins/'),
    ('revoke_points_admin', ('UDEVADMIN', 'UTARGET'), 'DELETE', '/api/v1/points/admins/UTARGET/'),
    ('set_points_admin_weekly_allowance', ('UDEVADMIN', 'UTARGET', 150), 'PATCH', '/api/v1/points/admins/UTARGET/'),
])
async def test_client_forwards_verified_workspace_with_roo_key(method, args, http_method, endpoint):
    client = MLAIBackendClient(base_url='https://backend.test', api_key='synthetic-roo',
                               internal_api_key='synthetic-internal', actor_context=actor())
    client._request = AsyncMock(return_value=httpx.Response(
        200, request=httpx.Request(http_method, 'https://backend.test' + endpoint), json={},
    ))
    await getattr(client, method)(*args)
    call = client._request.call_args
    assert call.args == (http_method, endpoint)
    assert call.kwargs['json']['requester_slack_id'] == 'UDEVADMIN'
    assert call.kwargs['json']['slack_team_id'] == 'TDEV'
    assert call.kwargs['headers']['X-API-Key'] == 'synthetic-roo'
    assert not call.kwargs.get('use_admin_headers')


@pytest.mark.asyncio
@pytest.mark.parametrize('method,args', [
    ('promote_points_admin', ('UDEVADMIN', 'UTARGET')),
    ('revoke_points_admin', ('UDEVADMIN', 'UTARGET')),
    ('set_points_admin_weekly_allowance', ('UDEVADMIN', 'UTARGET', 150)),
])
@pytest.mark.parametrize('context', [None, actor(user='UOTHER'), actor(team='')])
async def test_client_rejects_missing_or_mismatched_actor(method, args, context):
    with use_backend_actor_context(None):
        client = MLAIBackendClient(base_url='https://backend.test', api_key='synthetic-roo',
                                   internal_api_key='synthetic-internal', actor_context=context)
    client._request = AsyncMock()
    with pytest.raises(BackendIdentityError):
        await getattr(client, method)(*args)
    client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_requires_roo_key_instead_of_internal_key():
    client = MLAIBackendClient(base_url='https://backend.test', api_key='',
                               internal_api_key='synthetic-internal', actor_context=actor())
    client._request = AsyncMock()
    with pytest.raises(BackendIdentityError):
        await client.promote_points_admin('UDEVADMIN', 'UTARGET')
    client._request.assert_not_awaited()
