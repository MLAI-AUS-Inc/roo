import sys
import types
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("frontmatter", SimpleNamespace(load=lambda *args, **kwargs: None))
fake_executor_module = types.ModuleType("roo.skills.executor")
fake_executor_module.SkillExecutor = type("SkillExecutor", (), {})
fake_executor_module.SkillResult = type("SkillResult", (), {})
sys.modules.setdefault("roo.skills.executor", fake_executor_module)

from roo.agent import RooAgent
from roo.skills.loader import Skill


def _make_skill(name: str, trigger_keywords: list[str]) -> Skill:
    return Skill(
        name=name,
        description=name,
        content="",
        path=Path("."),
        trigger_keywords=trigger_keywords,
    )


def _make_agent() -> RooAgent:
    agent = object.__new__(RooAgent)
    agent.skills = [
        _make_skill(
            "linear-meeting-actions",
            [
                "meeting actions",
                "meeting action items",
                "meeting notes to linear",
                "meeting summary to linear",
                "transcript to linear",
                "linear tasks from meeting",
                "create linear tickets from transcript",
                "extract action items",
                "sync meeting notes to linear",
            ],
        ),
        _make_skill("mlai-points", ["points", "balance", "task", "tasks"]),
    ]
    agent.skill_executor = SimpleNamespace()
    agent._thread_skill_context = {}
    return agent


def test_linear_meeting_file_prompts_route_to_linear_meeting_actions():
    agent = _make_agent()

    for text in [
        "turn this meeting summary into Linear tasks",
        "extract action items from this transcript and add them to Linear",
        "sync meeting notes to Linear project Alpha",
        "send this PDF to Linear as tasks",
        "create Linear issues from this image",
        "create a project update from this PDF",
        "summarize this meeting as a Linear project update",
        "do a project update in Linear",
    ]:
        skill = agent._select_skill_from_triggers(text)
        assert skill is not None
        assert skill.name == "linear-meeting-actions"


def test_linear_file_context_routes_short_attached_file_request():
    agent = _make_agent()

    skill = agent._select_skill_from_triggers(
        "send this to Linear as tasks",
        has_file_context=True,
    )

    assert skill is not None
    assert skill.name == "linear-meeting-actions"


def test_linear_thread_reference_routes_only_with_context():
    agent = _make_agent()

    assert agent._select_skill_from_triggers("add this to Linear") is None

    skill = agent._select_skill_from_triggers(
        '@Roo add this to the Linear project called "BITGET EVENT"',
        has_thread_context=True,
    )

    assert skill is not None
    assert skill.name == "linear-meeting-actions"
