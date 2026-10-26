"""Opt-in real-model routing through fake booking/points boundaries.

Run separately from suites that stub imports. Only api.openai.com may receive
HTTP traffic. No production credentials, real Slack messages or databases.
"""
import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio

from roo.config import Settings
from roo.router import route
from roo.skills.loader import load_skill_report
from roo.skills.executor import SkillExecutor
from roo.skills.executors.mlai_points import MlaiPointsExecutor

TODAY = date(2026, 10, 23)
ACTOR = 'USMOKEMEMBER'
CHANNEL = 'CSMOKETEST'
THREAD = '1792700000.001'
LIVE = os.environ.get('ROO_SKILL_SMOKE_LIVE') == '1'


@pytest.fixture(scope='session')
def smoke_results():
    rows=[]
    yield rows
    path=os.environ.get('ROO_SKILL_SMOKE_REPORT')
    if path:
        output=Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({'model':os.environ.get('ROO_SKILL_SMOKE_MODEL','gpt-5.5'),
            'fixed_local_date':TODAY.isoformat(), 'live':LIVE,
            'boundaries':'Real model; fake backend/Slack; no database', 'cases':rows}, indent=2)+'\n')


@pytest.fixture
def sandbox(monkeypatch):
    settings=Settings(_env_file=None, SLACK_BOT_TOKEN='test', SLACK_SIGNING_SECRET='test',
        OPENAI_API_KEY='test', ROO_API_KEY='test', MLAI_BACKEND_URL='https://backend.test',
        FOUNDER_TOOLS_LINK_ORIGINS='https://mlai.au')
    monkeypatch.setattr('roo.config.get_settings', lambda: settings)
    monkeypatch.setattr('roo.skills.executors.mlai_points.get_settings', lambda: settings)
    monkeypatch.setattr('roo.utils.get_current_date', lambda: TODAY)
    # Transport guard also catches accidentally unmocked backend requests.
    original_send=httpx.AsyncClient.send
    async def guarded_send(self, request, *args, **kwargs):
        assert request.url.host == 'api.openai.com', f'Blocked external host: {request.url.host}'
        return await original_send(self, request, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, 'send', guarded_send)
    def no_network(*args, **kwargs):
        raise AssertionError('Unexpected synchronous network/Slack access')
    monkeypatch.setattr(httpx.Client, 'send', no_network)
    monkeypatch.setattr('urllib.request.urlopen', no_network)
    monkeypatch.setattr('slack_sdk.WebClient.api_call', no_network)
    monkeypatch.setattr('roo.slack_client.get_bot_user_id', lambda: 'USMOKEBOT')

    backend=SimpleNamespace(
        create_points_request=AsyncMock(return_value={'id':7,'status':'pending'}),
        attach_points_request_slack_summary=AsyncMock(return_value={}),
        approve_points_request=AsyncMock(return_value={'points_awarded':5,'new_balance':17}),
        get_points_request_by_slack_message=AsyncMock(return_value={
            'id':7,'status':'pending','requester_slack_id':ACTOR,'target_slack_id':ACTOR,
            'points':5,'reason':'helping at the event','slack_thread_ts':THREAD}),
    )
    monkeypatch.setattr('roo.clients.mlai_backend.MLAIBackendClient', lambda **kwargs: backend)
    post=Mock(return_value={'ok':True,'ts':'1792700000.002'})
    dm=Mock(return_value={'ok':True})
    monkeypatch.setattr('roo.skills.executors.mlai_points.post_message',post)
    monkeypatch.setattr('roo.skills.executors.mlai_points.send_dm',dm)
    points=MlaiPointsExecutor()
    booking=AsyncMock(return_value={'message':'Simulated booking confirmed','data':{'receipt':'fake-booking'}})
    monkeypatch.setattr(points,'_book_coworking_with_intent',booking)
    coordinator=SkillExecutor(executors={'mlai-points':points})
    from roo.points_request_approval import clear_points_request_summaries
    clear_points_request_summaries()
    yield SimpleNamespace(settings=settings,backend=backend,post=post,dm=dm,
                          booking=booking,coordinator=coordinator)
    clear_points_request_summaries()


@pytest_asyncio.fixture
async def live_router(monkeypatch,sandbox,smoke_results):
    if not LIVE:
        pytest.skip('Opt-in: run tests/run_skill_smoke.py --live with a development model key')
    key=os.environ.get('ROO_SMOKE_DEV_OPENAI_API_KEY')
    if not key or key=='test':
        pytest.fail('Live smoke tests require a development-only model key')
    from roo.llm import OpenAIClient
    model=os.environ.get('ROO_SKILL_SMOKE_MODEL') or sandbox.settings.ROUTER_MODEL
    client=OpenAIClient(api_key=key,model=model)
    async def bounded_tools(messages,tools,**kwargs):
        return await client.chat_tools(messages,tools,**{**kwargs,'timeout':45,'max_retries':0})
    monkeypatch.setattr('roo.router.chat_tools',bounded_tools)
    report=load_skill_report(Path(__file__).resolve().parents[2]/'roo-standalone/skills',
                             sandbox.settings.enabled_skill_names,import_clients=False)
    report.require(sandbox.settings.enabled_skill_names)
    skills=report.skills
    async def predict(text,expected,history=None):
        decision=await route(text,skills=skills,channel_name='general',thread_history=history,model=model)
        row={'query':text,'expected':expected,'skill':decision.skill,'action':decision.action,
             'params':decision.params,'source':decision.source,'clarification':decision.clarification,'passed':False}
        smoke_results.append(row)
        print('\nSMOKE '+json.dumps(row))
        assert decision.source != 'error', f'Model/API error: {decision.reason}'
        return decision,next((s for s in skills if s.name==decision.skill),None),row
    try:
        yield predict
    finally:
        await client.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,expected_date',[
    ('Book me into the coworking space today.','2026-10-23'),
    ('Can you book me in for coworking tomorrow?','2026-10-24'),
    ('Book me into coworking on 29 October 2026 from 1pm to 5pm.','2026-10-29'),
])
async def test_live_coworking_booking(live_router,sandbox,text,expected_date):
    decision,skill,row=await live_router(text,'mlai-points/book_coworking')
    assert (decision.skill,decision.action)==('mlai-points','book_coworking')
    result=await sandbox.coordinator.execute(skill,text,ACTOR,channel_id=CHANNEL,thread_ts=THREAD,
        param_overrides={**decision.params,'action':decision.action},event_id='EVSMOKEBOOK')
    row["reply"]=result.message
    assert result.success,result.message
    sandbox.booking.assert_awaited_once()
    args=sandbox.booking.call_args.kwargs
    assert args['booking_date']==expected_date
    assert args['target_user_id']==ACTOR and args['requested_by_user_id']==ACTOR
    assert args['admin_checkin'] is False
    row['resolved_date']=args['booking_date'];row['passed']=True


@pytest.mark.asyncio
@pytest.mark.parametrize('text,amount,reason',[
    ('Request 5 Roo Points for helping at the event.',5,'helping at the event'),
    ('Can I request 8 Roo Points for writing documentation?',8,'writing documentation'),
])
async def test_live_request_roo_points(live_router,sandbox,text,amount,reason):
    decision,skill,row=await live_router(text,'mlai-points/request_points')
    assert (decision.skill,decision.action)==('mlai-points','request_points')
    result=await sandbox.coordinator.execute(skill,text,ACTOR,channel_id=CHANNEL,thread_ts=THREAD,
        param_overrides={**decision.params,'action':decision.action})
    assert result.success,result.message
    sandbox.backend.create_points_request.assert_awaited_once()
    args=sandbox.backend.create_points_request.call_args.kwargs
    assert args['points']==amount and reason in args['reason'].lower()
    assert args['requester_slack_id']==ACTOR and args['target_slack_id']==ACTOR
    sandbox.backend.approve_points_request.assert_not_called()
    assert result.data['request_id']==7 and result.suppress_post
    row['reply']=sandbox.post.call_args.kwargs['text'];row['passed']=True


@pytest.mark.asyncio
@pytest.mark.parametrize('text',[
    'Approve Roo Points request #7.',
    'Please approve <@USMOKEMEMBER>\'s pending request for 5 Roo Points for helping at the event.',
])
async def test_live_points_approval_does_not_become_a_direct_award(live_router,sandbox,text):
    history=[{'user':'USMOKEBOT','text':
        'Pending Roo Points request #7: USMOKEMEMBER requested 5 points for helping at the event. '
        'A Points Admin can approve this request by reacting with ✅.'}]
    decision,skill,row=await live_router(text,'clarify/respond; approval uses Slack reaction',history)
    # Approval of an existing request must not become a fresh award or a task approval.
    # Keep this a failing smoke check if the real model makes that substitution.
    assert decision.skill is None, f'Unsupported approval routed to {decision.skill}/{decision.action}'
    sandbox.backend.approve_points_request.assert_not_called()
    row["passed"]=True


@pytest.mark.asyncio
@pytest.mark.parametrize('denied',[False,True],ids=['authorized','denied'])
async def test_reaction_approval_uses_event_actor_and_backend_result(sandbox,monkeypatch,denied):
    from roo import main
    monkeypatch.setattr(main,'get_settings',lambda: sandbox.settings)
    monkeypatch.setattr(main,'post_message',sandbox.post)
    monkeypatch.setattr(main,'send_dm',sandbox.dm)
    monkeypatch.setattr(main,'handle_boost_recheck_reaction',AsyncMock(return_value={'handled':False}))
    if denied:
        response=httpx.Response(403,request=httpx.Request('POST','https://backend.test/approve'),json={'detail':'Not a Points Admin'})
        sandbox.backend.approve_points_request.side_effect=httpx.HTTPStatusError('Denied',request=response.request,response=response)
    await main._handle_reaction_added({'user':'USMOKEADMIN','reaction':'white_check_mark',
        'item':{'type':'message','channel':CHANNEL,'ts':'1792700000.002'}})
    sandbox.backend.approve_points_request.assert_awaited_once_with(7,'USMOKEADMIN')
    if denied:
        sandbox.post.assert_not_called()
        assert sandbox.dm.call_args.args[0]=='USMOKEADMIN'
        assert "couldn't approve" in sandbox.dm.call_args.args[1]
    else:
        assert 'approved by <@USMOKEADMIN>' in sandbox.post.call_args.kwargs['text']
        assert sandbox.dm.call_args.args[0]==ACTOR
