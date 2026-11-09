"""Explicit skill registration and deny-by-default scenario capabilities."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

GLOBAL_READ_ONLY = frozenset({"list_files"})


def allowed(name, scenario=None, *, mcp=False):
    """全局只读技能总是可用；其他技能和外部服务（MCP，写成 server/tool）要场景的 tools / mcp 里列出才能用。"""
    if not mcp and name in GLOBAL_READ_ONLY:
        return True
    if scenario is None:
        return False
    return name in (getattr(scenario, "mcp" if mcp else "tools", None) or ())


@dataclass(frozen=True)
class Skill:
    name: str
    instructions: str
    handler: Callable


class SkillRegistry:
    def __init__(self):
        self._skills = {}

    def register(self, name, handler, *, instructions="", instruction_file: Path | None = None):
        if name in self._skills:
            raise ValueError("Duplicate skill")
        self._skills[name] = Skill(name, instruction_file.read_text() if instruction_file else instructions, handler)

    def available(self, pack=None):
        return [s for s in self._skills.values() if allowed(s.name, pack)]

    def call(self, name, arguments, *, pack=None):
        if not allowed(name, pack):
            raise PermissionError(f"Skill not allowed: {name}")
        if name not in self._skills:
            raise KeyError(f"Unknown skill: {name}")
        return self._skills[name].handler(**arguments)
