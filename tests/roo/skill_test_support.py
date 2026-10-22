"""Patch shared test dependencies after splitting the former executor module.

Older regression suites replaced one executor global. Patch the same dependency
at each new import site so the existing behavioral assertions remain intact.
New domain tests should inject/patch the specific executor they exercise.
"""
import importlib
from roo.skills.registry import EXECUTORS

def patch_skill_dependency(monkeypatch, name, value):
    modules = {executor_class.__module__ for executor_class in EXECUTORS.values()}
    modules.update({'.executors.prompt', '.executors.publishing_support',
                    '.executors.linear_read_support', '.executors.backend_errors'})
    for path in sorted(modules):
        module = importlib.import_module(path, 'roo.skills')
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
