"""Patch shared test dependencies after splitting the former executor module.

Older regression suites replaced one executor global. Patch the same dependency
at each new import site so the existing behavioral assertions remain intact.
New domain tests should inject/patch the specific handler they exercise.
"""
import importlib
from roo.skills.registry import HANDLERS

def patch_skill_dependency(monkeypatch, name, value):
    modules = {binding.module for binding in HANDLERS.values()}
    modules.update({'.handlers.prompt', '.handlers.publishing_support',
                    '.handlers.linear_read_support', '.handlers.backend_errors'})
    for path in sorted(modules):
        module = importlib.import_module(path, 'roo.skills')
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
