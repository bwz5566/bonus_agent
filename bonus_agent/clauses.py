"""规则条款清单：开工时把每份规则文档的计算条款逐条摘出（要点 + 原文 + 出处），每一步都附给模型，
写规则后标注每条落在哪里；结束前没落实、也没说明理由的条款不允许结束。

材料多、文档长时，较早读过的原文会随对话裁剪而淡出，清单是不会丢的那份“规则记忆”。
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from bonus_agent.facts import MAX_QUOTE, norm

BUILTIN = "引擎自带"
NOT_APPLY = "不适用"
ASKED = "已提问"
MAX_POINT = 100


class Clause(BaseModel):
    id: str
    point: str                 # 要点：用自己的话概括这条条款要求什么（不超过 100 字）
    file: str
    quote: str                 # 原文摘录
    where: str = ""            # 落在哪里：variables.x、checks.y、datasets.z……，或 引擎自带：… / 不适用：原因 / 已提问 q1
    round: int = 1


def check_quote(file: str, quote: str, corpus: dict[str, str]) -> str | None:
    if file not in corpus:
        return f"没有文件「{file}」"
    q = norm(quote)
    if not q:
        return "quote 不能为空，要逐字复制原文"
    if len(quote) > MAX_QUOTE:
        return f"quote 太长（{len(quote)} 字），只复制这条条款的关键句（不超过 {MAX_QUOTE} 字）"
    parts = [norm(x) for x in str(quote).splitlines() if norm(x)]
    if q not in corpus[file] and not (len(parts) > 1 and all(p in corpus[file] for p in parts)):
        return f"quote 在「{file}」里找不到，要逐字复制原文"
    return None


def refs(where: str) -> list[str]:
    return [x.strip() for x in re.split(r"[、,，;；]\s*", where or "") if x.strip()]


def check_where(where: str, known: set[str], questions: set[str]) -> str | None:
    """where 里每一项都要指向现有的规则元素，或写明 引擎自带 / 不适用（带原因）/ 已提问 q?。返回错误原因。"""
    w = (where or "").strip()
    if not w:
        return "没有写落在哪里"
    if w.startswith(BUILTIN):
        return None
    if w.startswith(NOT_APPLY):
        return None if len(w) > len(NOT_APPLY) + 3 else f"写「{NOT_APPLY}」要说明原因，如 不适用：本批没有外包人员"
    if w.startswith(ASKED):
        ids = re.findall(r"q\d+", w)
        return None if ids and all(i in questions for i in ids) else f"「{w}」里的问题编号不存在"
    bad = [r for r in refs(w) if r not in known]
    if bad:
        return (f"{'、'.join(bad)} 不存在。写规则元素的位置，如 variables.net_receipt、checks.grade_known、pool、"
                "allocation.参与分配、payout.warranty、offsets.prepaid、tables.margin_tier、datasets.成本费用")
    return None


def known_refs(drafts: dict, datasets: dict) -> set[str]:
    out = {f"datasets.{n}" for n in datasets}
    for d in drafts.values():
        for k in ("tables", "variables"):
            out |= {f"{k}.{n}" for n in (d.get(k) or {})}
        if d.get("pool"):
            out.add("pool")
        for lv in d.get("allocation") or []:
            if isinstance(lv, dict) and lv.get("level"):
                out.add(f"allocation.{lv['level']}")
        for k in ("payout", "offsets", "checks"):
            out |= {f"{k}.{x['id']}" for x in d.get(k) or [] if isinstance(x, dict) and x.get("id")}
        out |= {f"revisions.{r['name']}" for r in d.get("revisions") or [] if isinstance(r, dict) and r.get("name")}
        out |= {f"patches.{x['source']}" for x in d.get("patches") or [] if isinstance(x, dict) and x.get("source")}
    return out


def next_id(clauses: list[Clause]) -> str:
    n = max((int(c.id[1:]) for c in clauses if re.fullmatch(r"C\d+", c.id)), default=0)
    return f"C{n + 1}"
