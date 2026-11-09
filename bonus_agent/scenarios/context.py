"""编制模式交给智能体的场景信息：场景说明（财务给的规则和口径）、输入模板和本次材料的检查结果、场景记忆。
这些都是场景包和财务确认过的内容，随场景变化；通用提示词里不写任何场景专属的内容。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bonus_agent.scenarios import Registry, Scenario
from bonus_agent.scenarios.inputs import check_inputs, template_text


def guide_file(reg: Registry, sc: Scenario) -> Path | None:
    """场景说明作为一份材料交给智能体（能按原文引用、核对出处）：放在本机场景目录下，内容变了才重写。"""
    text = sc.guide_text()
    if not text:
        return None
    p = reg.data_dir(sc.id) / "staged" / f"场景说明（{sc.name}）.md"
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return p


def agent_brief(reg: Registry, sc: Scenario, files: list[str | Path], *, use_memory: bool = True) -> str:
    parts = [f"【场景】{sc.name}" + (f"：{sc.description.strip()}" if sc.description.strip() else "")
             + "\n本批次按这个场景计算，只用这个场景的规则和输入格式。"]
    guide = guide_file(reg, sc)
    if guide:
        parts.append(f"【场景说明】材料里的「{guide.name}」是财务为这个场景定的规则和口径，与其他材料同等效力；"
                     "与其他材料冲突、或说明里没写清楚的，用 ask_question 提问，不要自己选。")
    tpl = template_text(sc)
    if tpl:
        rep = check_inputs(sc, files)
        parts.append("【输入模板】本场景每期的数据按下面的表和列提供：\n" + tpl + "\n" + rep.text()
                     + ("\n有错误时：先用 ask_question 请财务确认或补正数据，不要自己猜测或改用别的列。" if rep.errors else ""))
    if sc.outputs.strip():
        parts.append("【输出要求】" + sc.outputs.strip())
    if use_memory and sc.memory:
        mem = reg.memory(sc.id).brief()
        if mem:
            parts.append("【场景记忆】以前在这个场景里财务确认过的口径（发布规则版本时记下的）。本次材料同样适用时可以直接采用，"
                         "在 note 里注明“沿用场景记忆”；材料变了、与本次材料冲突或拿不准是否适用时，仍要提问：\n" + mem)
    return "\n\n".join(parts)


@dataclass
class AgentScenario:
    """交给 run_agent 的场景信息：brief 放进任务说明，files 作为材料一起读（场景说明）。"""
    id: str
    brief: str
    files: list[Path] = field(default_factory=list)


def expand(paths: list[str | Path]) -> list[Path]:
    out = []
    for p in map(Path, paths):
        out += sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p]
    return out


def prepare(reg: Registry, scenario_id: str, materials: list[str | Path], *, use_memory: bool = True) -> AgentScenario:
    sc = reg.get(scenario_id)
    guide = guide_file(reg, sc)
    return AgentScenario(sc.id, agent_brief(reg, sc, expand(materials), use_memory=use_memory), [guide] if guide else [])
