"""智能体卡住时，把卡点变成一个能直接回答的问题交给财务：问什么、有哪些选项、怎么回答。

原来卡住只说“需要补充材料、明确答复”，财务不知道补什么、答什么。这里按卡住的原因生成一个具体问题：
- 已知的数据口径分歧（同一编号下取值不同、参考表挂不到任何层级等）按模板生成，用表头原文说话；
- 其他业务卡点让模型把技术原因改写成一个业务问题（一次调用，失败就用通用问法）；
- 程序或模型自身的问题（工具名写错、格式不对、内部异常）不问财务，返回 None，由调用方说明“不需要你回答”。
问题登记为阻断问题：页面上选择或填写答复后点「运行下一轮」，或在对话框直接回复；答复后卡住记录自动清除，接着做。
"""
from __future__ import annotations

import json
import re

OTHER = "其他（请直接写明应该怎么处理）"

_PROGRAM = re.compile(r"没有工具|thought 不是工具|参数不是合法 JSON|参数结构不对|参数不对|参数必须是 JSON|"
                      r"上下文|tokens 用量|总时间上限|object has no attribute|not subscriptable|NoneType|"
                      r"KeyError|TypeError|AttributeError|模型多次没有调用工具|模型服务|"
                      # 智能体自己的流程没走完（没做独立验算、自查没走完、验算式写错），财务回答不了
                      r"独立验算还没|没有独立验算|结束前自查|条款清单还有没落实|验算式算不出来|验算式写错了|辅助变量|"
                      # 步骤顺序不对（前置阶段没过、表还没定义），是智能体自己的流程问题
                      r"workflow\.gate|前置阶段|还没有定义|尚未定义|dataset\.patch|"
                      # 原样重复同一个调用（参数相同、定义没变）：是智能体自己在打转，不是材料没写清
                      r"参数相同|no-op|完全相同|反复提交相同|"
                      # 编译公式时自己的登记、绑定、报表声明没做完（参数没绑到规则位置、填的数和原文不一致）
                      r"缺少精确规则位置|不一致或位置不存在|report\.balances|report\.distributions|parameter\.binding")


# 这些步骤是智能体照抄材料、做自己的核对（登记参数和条款、按登记的参数编表、结束前自查、独立验算），
# 出错说明它抄错了或流程没走完，财务回答不了
_AGENT_OWN = {"record_facts", "record_clauses", "record_parameter_cells", "cover_clauses", "bind_facts",
              "compile_table", "remove_facts", "finish", "verify", "note"}


def program_issue(reason: str) -> bool:
    """卡住的原因是程序或模型自身（不是材料口径）：问财务没有用。
    只有数据定义、规则、数据准备、试算这类操作被程序检查拦下、并给出了具体原因（“错误：……”）时，才可能是材料没写清；
    原地打转（反复读同一内容、原样重复调用、只读不写）、抄错材料、自己的流程没走完，都是模型自身的问题。"""
    reason = reason or ""
    m = re.search(r"(\w+) (?:同一问题累计失败|连续 \d+ 次失败)", reason)
    if m and m.group(1) in _AGENT_OWN:
        return True
    return bool(_PROGRAM.search(reason) or re.search(r"没有文件「|没有方案「", reason)) or "错误：" not in reason


def build(ws, llm, reason: str, steps) -> dict | None:
    """{text, options, why}；程序或模型自身的问题返回 None。"""
    if program_issue(reason):
        return None
    return _known(ws, reason, steps) or _from_model(ws, llm, reason, steps) or _generic(reason)


def _column(ws, dataset: str, field: str, steps=()) -> str:
    """字段名 → 表头原文（财务认表头，不认内部字段名）。表没保存成功时，从最近一次定义它的调用参数里找。"""
    m = getattr(ws, "mapping", {}).get(dataset)
    col = (getattr(m, "columns", None) or {}).get(field) if m else None
    for s in reversed(list(steps)):
        if col:
            break
        args = s.args if isinstance(getattr(s, "args", None), dict) else {}
        if args.get("name") == dataset:
            spec = (args.get("fields") or (args.get("changes") or {}).get("fields") or {}).get(field)
            col = spec.get("column") if isinstance(spec, dict) else None
    return re.sub(r"\([A-Z]+列\)$", "", str(col or field))


def _known(ws, reason: str, steps=()) -> dict | None:
    m = re.search(r"「([^」]+)」distinct 按 (\S+?) 合并时，(\S+?) 在同一编号下取值不同（如 (.+?)：(.+?) / (.+?)）", reason)
    if m:
        table, key, other, sample, a, b = m.groups()
        k, o = _column(ws, table, key, steps), _column(ws, table, other, steps)
        return {"text": f"数据里同一个「{k}」对应多个不同的「{o}」（例如「{sample}」对应 {a}、{b} 等）。"
                        f"计算时，这几个「{o}」应该算同一个对象合并计算，还是分开计算？",
                "options": [f"合并：同一个「{k}」下的多个「{o}」视为同一个对象，一起计算",
                            f"分开：按「{o}」分别计算；只按「{k}」给的数据（如参考表）再按「{k}」对应过去",
                            OTHER],
                "why": f"按「{k}」还是按「{o}」区分对象，会改变后面的汇总和金额"}
    m = re.search(r"(\S+?) 关联字段 (\S+?) 没有父实体", reason)
    if m:
        table, link = m.groups()
        col = _column(ws, table, link, steps)
        entities = [(n, d.key) for n, d in getattr(ws, "datasets", {}).items() if d.key and n != table]
        options = [f"按「{col}」把同一{col}的数据合并成一层来计算（「{table}」挂在这一层）"]
        options += [f"对应到「{n}」（按「{_column(ws, n, key)}」），「{table}」的数据换算到这一层再用" for n, key in entities[:2]]
        return {"text": f"「{table}」是按「{col}」给的。计算时它应该对应到哪一层（每个{col}一份，还是对应到其他对象上）？",
                "options": options + [OTHER],
                "why": f"程序不知道「{table}」的每一行该用在哪个对象上，没法往下算"}
    m = re.search(r"「([^」]+)」[^\n]*?(?:主键|编号)[^\n]*?重复", reason)
    if m:
        return {"text": f"「{m.group(1)}」里有重复的编号。重复的行是录入错误，还是一个编号本来就对应多行（如一人参与多个项目）？",
                "options": ["录入错误：同一编号只保留一行（请写明保留哪一行）", "本来就对应多行：同一编号的多行要合并计算", OTHER],
                "why": "编号重复时不知道该按一个对象算还是按多行算"}
    return None


PROMPT = """【卡住提问】奖金核算智能体在下面这一步卡住了。请把卡住的原因改写成给财务人员的一个问题，让财务一句话就能答。
要求：
- 只问业务口径（怎么算、按什么对应、哪些计入），用材料和表头里的原文说法；不要出现工具名、英文字段名、程序术语。
- 给 2~4 个具体选项，每个选项写清选了以后会怎么算；最后一个选项写“{other}”。
- 如果卡住原因是程序或模型自身的问题（调用格式、内部错误），program_issue 填 true，其余留空。
只输出 JSON：{{"program_issue": false, "question": "...", "options": ["...", "..."], "why": "一句话：为什么要问这个"}}"""


def _from_model(ws, llm, reason: str, steps) -> dict | None:
    recent = [{"tool": s.tool, "ok": s.ok, "thought": (s.thought or "")[:200], "result": (s.result or "")[:300]}
              for s in list(steps)[-8:]]
    data = {n: {"key": d.key, "link": d.link,
                "columns": {f: _column(ws, n, f) for f in list(d.fields)[:12]}}
            for n, d in list(getattr(ws, "datasets", {}).items())[:12]}
    messages = [{"role": "system", "content": PROMPT.format(other=OTHER)},
                {"role": "user", "content": json.dumps({"卡住原因": reason[:1500], "最近几步": recent, "数据表": data},
                                                       ensure_ascii=False, default=str)}]
    try:
        out = llm.chat_json(messages)
    except Exception:  # noqa: BLE001 - asking is best effort; the generic question still tells finance what to answer
        return None
    if not isinstance(out, dict) or out.get("program_issue"):
        return None
    text, options = str(out.get("question") or "").strip(), [str(x).strip() for x in out.get("options") or [] if str(x).strip()]
    if len(text) < 8 or not 2 <= len(options) <= 5 or re.search(r"[a-z]+_[a-z]+|\b(?:dataset|update_|define_)", text):
        return None
    if not any(o.startswith("其他") for o in options):
        options.append(OTHER)
    return {"text": text, "options": options[:5], "why": str(out.get("why") or "").strip()[:200]}


def _generic(reason: str) -> dict:
    where = re.sub(r"^.*?累计失败 \d+ 次：|^错误：|未保存，请修正：\s*-?\s*", "", reason.split("\n", 1)[-1] if "\n" in reason else reason)
    where = where.strip()[:200]
    return {"text": f"智能体在这一步拿不准该怎么处理：{where}。材料里这一点是怎么规定的？",
            "options": ["按材料原文处理（请在答复里写明是哪份材料、哪一句）", OTHER],
            "why": "这一步材料没写清，智能体反复尝试都没通过程序检查"}
