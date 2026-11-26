"""编制模式交给智能体的场景信息：场景说明（财务给的规则和口径）、输入模板和本次材料的检查结果、场景记忆。
这些都是场景包和财务确认过的内容，随场景变化；通用提示词里不写任何场景专属的内容。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bonus_agent.scenarios import InputTable, Registry, Scenario, ScenarioError
from bonus_agent.scenarios.inputs import check_inputs, template_text


def guide_file(reg: Registry, sc: Scenario) -> Path | None:
    """场景说明作为一份材料交给智能体（能按原文引用、核对出处）：放在本机场景目录下，内容变了才重写。"""
    return _stage_document(reg, sc, f"场景说明（{sc.name}）.md", sc.guide_text())


def _stage_document(reg, sc, name, text):
    if not text:
        return None
    p = reg.data_dir(sc.id) / "staged" / name
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return p


def agent_brief(reg: Registry, sc: Scenario, files: list[str | Path], *, use_memory: bool = True,
                confirmations=(), output_source=None) -> str:
    parts = [f"【场景】{sc.name}" + (f"：{sc.description.strip()}" if sc.description.strip() else "")
             + "\n本批次按这个场景计算，只用这个场景的规则和输入格式。"]
    guide = guide_file(reg, sc)
    if guide:
        parts.append(f"【场景说明】材料里的「{guide.name}」是财务为这个场景定的规则和口径，与其他材料同等效力；"
                     "与其他材料冲突、或说明里没写清楚的，用 ask_question 提问，不要自己选。")
    play = sc.playbook_text()
    if play:
        parts.append("【做法】这类奖金固定的做法：按下面的步骤做，表和表照这里写的关系连，每一步做完按这里的自查项核对后再往下走。"
                     "做法只管怎么做，不给参数和金额：参数、比例、名单、口径一律从材料原文读，并照常登记台账、摘条款。"
                     "本次材料和做法对不上（如多了一类表、少了一步、关系不一样）时以材料为准，用 ask_question 说明哪里不一样再继续：\n"
                     + play)
    tpl = template_text(sc)
    if tpl:
        rep = check_inputs(sc, files)
        parts.append("【输入模板】本场景每期的数据按下面的表和列提供：\n" + tpl + "\n" + rep.text()
                     + ("\n有错误时：先用 ask_question 请财务确认或补正数据，不要自己猜测或改用别的列。" if rep.errors else ""))
    if sc.outputs.strip():
        citation = f"原文可引用材料「{output_source.name}」。\n" if output_source else ""
        parts.append("【输出要求】" + citation + sc.outputs.strip())
    if confirmations:
        parts.append("【本批已确认口径】核算期间及所有原始规则、业务材料指纹已经匹配。"
                     "以下正式确认文件作为可引用依据；只登记实际参与计算的参数，行号、页码与例子是定位信息。"
                     "新财务答复与本记录冲突时须核对适用范围。\n"
                     + "\n\n".join(f"《{file.name}》原文：\n" + file.read_text(encoding="utf-8").strip()
                                      for file in confirmations))
    if use_memory and sc.memory:
        mem = reg.memory(sc.id).brief()
        if mem:
            parts.append("【场景记忆】以前在这个场景里财务确认过的口径，以及已发布案卷的修复检查提示。问答口径在本次材料同样适用时可以采用，"
                         "在 note 里注明“沿用场景记忆”；修复提示仅原案验证，不能直接当成费率或默认政策。"
                         "材料变了、与本次材料冲突或拿不准是否适用时，仍要提问：\n" + mem)
    return "\n\n".join(parts)


@dataclass
class AgentScenario:
    """交给 run_agent 的场景信息：brief 放进任务说明，files 作为材料一起读（场景说明）。"""
    id: str
    brief: str
    files: list[Path] = field(default_factory=list)
    outputs: str = ""          # 场景的输出要求：有的话，规则里要写 report（汇总核算、分配人员金额）
    regressions: list[dict] = field(default_factory=list, repr=False)  # server-only holdout references; never in brief
    inputs: list[InputTable] = field(default_factory=list)
    supplements: list[Path] = field(default_factory=list)
    execution: str = 'generic'
    rule_store: Path | None = None  # fixed-chain, versioned financial decisions


def expand(paths: list[str | Path]) -> list[Path]:
    out = []
    for p in map(Path, paths):
        out += sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p]
    return out


def _matching_confirmations(sc, materials, guide, period):
    from bonus_agent.auto import collect_files
    from bonus_agent.case import fingerprint, sha256
    from bonus_core.data.period import Period

    if not period or not sc.confirmed_batches:
        return []
    label = Period.parse(period).label
    matching_period = [entry for entry in sc.confirmed_batches if Period.parse(entry.period).label == label]
    if not matching_period:
        return []
    hashes = fingerprint(collect_files(materials) + ([guide] if guide else []))
    selected = []
    for entry in matching_period:
        if entry.material_hashes != hashes:
            continue
        path = sc.file(entry.document)
        if not path.is_file() or sha256(path) != entry.document_sha256:
            raise ScenarioError(f"本批确认文件缺失或已修改：{entry.document}；须核对确认版本")
        selected.append(path)
    return list(dict.fromkeys(selected))


def prepare(reg: Registry, scenario_id: str, materials: list[str | Path], *, use_memory: bool = True,
            period: str | None = None) -> AgentScenario:
    sc = reg.get(scenario_id)
    guide = guide_file(reg, sc)
    output = _stage_document(reg, sc, f"场景输出要求（{sc.name}）.md", sc.outputs.strip())
    confirmations = _matching_confirmations(sc, materials, guide, period)
    supplements = ([output] if output else []) + confirmations
    cases = [{"id": c.id, "files": [str(sc.file(f)) for f in c.data], "expected": str(sc.file(c.expected)), "period": c.period}
             for c in sc.acceptance]
    brief = agent_brief(reg, sc, expand(materials), use_memory=use_memory,
                        confirmations=confirmations, output_source=output)
    return AgentScenario(sc.id, brief, ([guide] if guide else []) + supplements,
                         sc.outputs.strip(), cases, sc.inputs, supplements, sc.execution,
                         reg.data_dir(sc.id) / 'commission_rules' / 'versions.json' if sc.execution == 'sales_commission_v1' else None)
