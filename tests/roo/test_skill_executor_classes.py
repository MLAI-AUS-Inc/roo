"""Class ownership and prompt execution contracts, entirely offline."""
import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from roo.skills.contracts import SkillRequest
from roo.skills.executor import SkillExecutor
from roo.skills.loader import Skill
from roo.skills.registry import HANDLERS, EVENT_HANDLERS
from roo.skills.handlers.prompt import PromptExecutor
from roo.skills.handlers.tone_of_voice import ToneOfVoiceHandler


def request(name='test', **overrides):
    return SkillRequest(skill=Skill(name, 'Description', 'Style rules', Path('.')),
                        text='Original text', user_id='UHUMAN', **overrides)


@pytest.mark.parametrize('name', sorted(HANDLERS))
def test_registered_skill_owns_an_executor_object(name):
    first = HANDLERS[name].create()
    second = HANDLERS[name].create()
    assert not inspect.isfunction(first)
    assert first is not second
    assert inspect.iscoroutinefunction(first.execute)
    assert not isinstance(first, SkillExecutor)


def test_handlers_never_depend_on_central_executor():
    folder = Path(__file__).resolve().parents[2] / 'src/roo/skills/handlers'
    for path in folder.glob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or '').endswith('skills.executor'), path
                assert node.module != 'executor', path
            if isinstance(node, ast.Name):
                assert node.id != 'SkillExecutor', path


@pytest.mark.asyncio
async def test_prompt_executor_injects_instructions_parameters_and_history():
    model = AsyncMock(return_value=SimpleNamespace(content='Answer'))
    handler = PromptExecutor(chat_fn=model)
    result = await handler.execute(request(params={'query':'Python'}, thread_history=[{'text':'Earlier'}]))
    assert result == 'Answer'
    messages = model.call_args.args[0]
    assert messages[0]['role'] == 'system'
    for expected in ['Style rules', 'Original text', 'UHUMAN', 'Python', 'Earlier']:
        assert expected in messages[1]['content']
    model.assert_awaited_once()


@pytest.mark.asyncio
async def test_tone_uses_shared_prompt_executor_with_unchanged_provider_options():
    client = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content='Rewrite')))
    providers=[]
    def factory(provider):
        providers.append(provider)
        return client
    handler = ToneOfVoiceHandler(client_factory=factory)
    assert isinstance(handler, PromptExecutor)
    assert await handler.execute(request(params={'text':'Selected source'})) == 'Rewrite'
    assert providers == ['openai']
    assert client.chat.call_args.args[0][0] == {'role':'system','content':'Style rules'}
    assert client.chat.call_args.args[0][1]['content'].endswith('Selected source')
    assert client.chat.call_args.kwargs == dict(model='gpt-5.2', max_tokens=8192, reasoning_effort='high')


@pytest.mark.asyncio
async def test_coordinator_normalizes_injected_handler_result_without_changing_context():
    fake = SimpleNamespace(execute=AsyncMock(return_value={
        'message':'Done', 'data':{'receipt':'r1'}, 'blocks':[{'type':'divider'}], 'suppress_post':True}))
    coordinator = SkillExecutor(handlers={'test':fake})
    req=request(params={'action':'read'}, metadata={'event_id':'EV1'})
    result=await coordinator.execute(req.skill, req.text, req.user_id, channel_id='C1',
        thread_ts='1.2', thread_history=[], param_overrides=req.params, **req.metadata)
    delivered=fake.execute.call_args.args[0]
    assert delivered.user_id=='UHUMAN' and delivered.metadata=={'event_id':'EV1'}
    assert delivered.thread_history == [] and delivered.channel_id == 'C1'
    assert delivered.params == {'action':'read'} and delivered.thread_ts == '1.2'
    assert result.success and result.message=='Done' and result.data=={'receipt':'r1'}
    assert result.suppress_post and result.blocks==[{'type':'divider'}]


@pytest.mark.asyncio
async def test_model_failure_does_not_become_success():
    model=AsyncMock(side_effect=RuntimeError('model unavailable'))
    coordinator=SkillExecutor(handlers={'test':PromptExecutor(chat_fn=model)})
    req=request()
    result=await coordinator.execute(req.skill,req.text,req.user_id)
    assert not result.success and result.error=='model unavailable'


@pytest.mark.asyncio
async def test_event_only_class_is_not_executable_through_chat():
    binding=EVENT_HANDLERS['start-here-introductions']
    assert 'start-here-introductions' not in HANDLERS
    handler=binding.create()
    result=await handler.execute({})
    assert result=={'status':'ignored','reason':'ineligible_event'}
    req=request('start-here-introductions')
    result=await SkillExecutor().execute(req.skill, req.text, req.user_id)
    assert not result.success and result.error=='unregistered_skill_handler'
