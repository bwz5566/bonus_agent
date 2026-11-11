"""场景：每类奖金（提成、开发奖金、年度奖金……）一个场景包，各有固定的输入格式和规则。

场景包是一个目录，里面有 scenario.yaml（名称、说明、输入模板、验收用例）和可选的 guide.md（财务给的规则和口径说明）。
同一个场景有三种用法：
  编制：智能体带着场景说明和输入模板检查读材料、起草规则，财务答复、确认后发布成规则版本（releases/v1、v2……）
  计算：选已发布的规则版本，换本期数据直接算，不调用大模型
  对话：对计算结果提问（只读解释）

场景包的查找顺序：环境变量 BONUS_SCENARIO_DIRS 里的目录（多个用路径分隔符隔开）→ 调用方给的目录（如网页数据目录下的
scenarios/）→ 仓库自带的 packs/（只有空骨架，不含任何公司的规则）。同名场景取最先找到的，真实规则放在本机目录，不进仓库。
规则版本和场景记忆写在本机的场景目录（home）下：<home>/<场景>/releases/、<home>/<场景>/memory.yaml。
金额只由计算引擎（bonus_core）算出，这里只管场景的元数据、版本和记忆。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator

SCENARIO_FILE = "scenario.yaml"
GUIDE_FILE = "guide.md"
PLAYBOOK_FILE = "playbook.md"
BUILTIN = Path(__file__).parent / "packs"
ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ScenarioError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Column(_Strict):
    name: str
    type: Literal["text", "number", "date"] = "text"
    required: bool = True             # 这一列必须有
    nonblank: bool = False            # 每行都要填
    aliases: list[str] = Field(default_factory=list)    # 同一列的其他叫法
    note: str = ""


class InputTable(_Strict):
    name: str
    note: str = ""
    required: bool = True
    aliases: list[str] = Field(default_factory=list)    # 工作表名或文件名的其他叫法
    columns: list[Column] = Field(default_factory=list)


class AcceptanceCase(_Strict):
    """发布规则版本前必须逐分对上的验收用例：数据文件和财务算好的期望结果（路径相对场景目录）。"""
    id: str
    data: list[str] = Field(min_length=1)
    expected: str
    period: str | None = None          # 这个用例的核算期间（如 2026-09）；不写用确认时的期间
    note: str = ""


class Scenario(_Strict):
    id: str
    name: str
    description: str = ""
    guide: str | None = None          # 场景说明文件名，默认 guide.md（有就用）
    playbook: str | None = None       # 做法说明文件名，默认 playbook.md（有就用）：这类奖金固定的做法（步骤、表怎么连、
                                      # 每步自查什么），不写参数和金额；智能体按它的步骤做，数从材料里读
    inputs: list[InputTable] = Field(default_factory=list)
    outputs: str = ""                 # 输出要求（给智能体和财务看的说明）
    tools: list[str] = Field(default_factory=list)      # 场景专用技能；全局技能（读表格、读文档、计算……）总是可用
    mcp: list[str] = Field(default_factory=list)        # 允许调用的外部服务（server/tool），默认不开
    memory: bool = True               # 是否把发布时财务确认过的口径记为场景记忆，供下次编制参考
    acceptance: list[AcceptanceCase] = Field(default_factory=list)
    _dir: Path = PrivateAttr(default=Path("."))

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID.fullmatch(v):
            raise ValueError("场景编号只能用小写字母、数字、下划线，字母开头")
        return v

    @property
    def dir(self) -> Path:
        return self._dir

    def guide_text(self) -> str:
        name = self.guide or GUIDE_FILE
        p = self.dir / name
        if not p.is_file():
            if self.guide:
                raise ScenarioError(f"场景 {self.id} 的说明文件不存在：{p}")
            return ""
        return p.read_text(encoding="utf-8").strip()

    def playbook_text(self) -> str:
        name = self.playbook or PLAYBOOK_FILE
        p = self.dir / name
        if not p.is_file() and not self.playbook:      # 本机场景包没写做法：用仓库自带同名场景的通用做法（不含任何公司的参数）
            p = BUILTIN / self.id / PLAYBOOK_FILE
        if not p.is_file():
            if self.playbook:
                raise ScenarioError(f"场景 {self.id} 的做法说明文件不存在：{p}")
            return ""
        return p.read_text(encoding="utf-8").strip()

    def file(self, rel: str) -> Path:
        p = (self.dir / rel).resolve()
        if Path(rel).is_absolute() or not p.is_relative_to(self.dir.resolve()):
            raise ScenarioError(f"场景 {self.id}：文件路径越界：{rel}")
        return p

    @property
    def label(self) -> str:
        return f"{self.name}（{self.id}）"


def load_scenario(directory: str | Path) -> Scenario:
    d = Path(directory)
    p = d / SCENARIO_FILE
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ScenarioError(f"读不了场景文件 {p}：{e}") from None
    if not isinstance(data, dict):
        raise ScenarioError(f"{p} 应该是 YAML 映射")
    try:
        sc = Scenario.model_validate(data)
    except ValidationError as e:
        raise ScenarioError(f"场景文件 {p} 不对：{e}") from None
    if sc.id != d.name:
        raise ScenarioError(f"{p}：id {sc.id} 要和目录名 {d.name} 一致")
    sc._dir = d.resolve()
    return sc


def env_dirs() -> list[Path]:
    raw = os.environ.get("BONUS_SCENARIO_DIRS", "")
    return [Path(x).expanduser() for x in raw.split(os.pathsep) if x.strip()]


class Registry:
    """按查找顺序列出场景；规则版本和记忆写在 home 下。"""

    def __init__(self, dirs: list[str | Path] | None = None, home: str | Path | None = None, builtin: bool = True):
        extra = [Path(x) for x in (dirs or [])]
        self.dirs = env_dirs() + extra + ([BUILTIN] if builtin else [])
        default_home = (env_dirs() or extra or [Path.home() / ".bonus_agent" / "scenarios"])[0]
        self.home = Path(home or os.environ.get("BONUS_SCENARIO_HOME") or default_home)

    def list(self) -> list[Scenario]:
        seen: dict[str, Scenario] = {}
        for root in self.dirs:
            if not root.is_dir():
                continue
            for d in sorted(root.iterdir()):
                if d.name in seen or not (d / SCENARIO_FILE).is_file():
                    continue
                seen[d.name] = load_scenario(d)
        return list(seen.values())

    def get(self, scenario_id: str | None) -> Scenario:
        if not isinstance(scenario_id, str) or not ID.fullmatch(scenario_id):
            raise ScenarioError(f"没有这个场景：{scenario_id!r}")
        for root in self.dirs:
            if (root / scenario_id / SCENARIO_FILE).is_file():
                return load_scenario(root / scenario_id)
        have = "、".join(s.id for s in self.list()) or "无"
        raise ScenarioError(f"没有场景 {scenario_id}（现有：{have}）")

    def data_dir(self, scenario_id: str) -> Path:
        """本机保存这个场景的规则版本和记忆的目录。"""
        if not ID.fullmatch(scenario_id):
            raise ScenarioError(f"场景编号不对：{scenario_id!r}")
        return self.home / scenario_id

    # 规则版本、记忆的具体操作见 releases.py、memory.py
    def releases(self, scenario_id: str):
        from bonus_agent.scenarios.releases import list_releases
        return list_releases(self, scenario_id)

    def release(self, scenario_id: str, version: str | None = None):
        from bonus_agent.scenarios.releases import get_release
        return get_release(self, scenario_id, version)

    def memory(self, scenario_id: str):
        from bonus_agent.scenarios.memory import ScenarioMemory
        return ScenarioMemory(self.data_dir(scenario_id) / "memory.yaml")


def default_registry() -> Registry:
    return Registry()


__all__ = ["AcceptanceCase", "BUILTIN", "Column", "GUIDE_FILE", "ID", "InputTable", "Registry", "SCENARIO_FILE",
           "Scenario", "ScenarioError", "default_registry", "env_dirs", "load_scenario"]
