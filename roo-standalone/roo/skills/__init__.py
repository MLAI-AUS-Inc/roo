"""Skills package. Keep metadata tooling independent of execution imports."""
from .loader import Skill, load_skills

__all__ = ["Skill", "load_skills", "SkillExecutor", "SkillResult"]


def __getattr__(name):
    if name in {"SkillExecutor", "SkillResult"}:
        from . import executor
        return getattr(executor, name)
    raise AttributeError(name)
