"""Explicit skill registration and deny-by-default scenario capabilities."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

GLOBAL_READ_ONLY = frozenset({"list_files"})


def allowed(name, pack=None, *, mcp=False):
    if not mcp and name in GLOBAL_READ_ONLY:
        return True
    if pack is None:
        return False
    field = "allowed_mcp" if mcp else "allowed_tools"
    metadata = getattr(pack, "metadata", pack)       # accept ScenarioPack or bare PackSchema
    return name in (getattr(metadata, field, None) or ())


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
