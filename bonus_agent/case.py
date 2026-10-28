"""案卷：一笔奖金的计算往往要和财务来回几轮（提问 → 答复 → 补充材料 → 修改规则），每轮都接着上一轮做。

输出目录就是案卷，里面的 case.json 记录：
- 工作区快照：数据表定义和列映射、各方案规则、笔记、图片转写、问题和财务答复、方案概述；
- 每一轮：时间、输入文件指纹（sha256）、状态、问题、结果摘要（每个对象的金额），用于下一轮对比。

下一轮用同一个输出目录再运行，就从快照恢复工作区，只告诉智能体这一轮变了什么（答复、新增 / 修改 / 删除的文件），
不必从头读材料；算完后与上一轮逐个对象对比金额、列出规则改动。历次输出留在 history/第N轮/。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from bonus_agent.extract_rules import Question
from bonus_core.data.mapping import DatasetMapping
from bonus_core.dsl.models import DatasetDef
from bonus_core.engine.run import Result

CASE_FILE = "case.json"


def fingerprint(files: list[Path]) -> dict[str, str]:
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def load_case(out: Path) -> dict | None:
    p = Path(out) / CASE_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_case(out: Path, case: dict) -> Path:
    p = Path(out) / CASE_FILE
    p.write_text(json.dumps(case, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def snapshot(ws) -> dict:
    return {
        "datasets": {k: v.model_dump(mode="json", exclude_defaults=True) for k, v in ws.datasets.items()},
        "mapping": {k: v.model_dump(mode="json", exclude_defaults=True) for k, v in ws.mapping.items()},
        "drafts": ws.drafts, "notes": ws.notes, "transcripts": ws.transcripts, "summary": ws.summary,
        "ignored": ws.ignored, "questions": [q.model_dump() for q in ws.questions],
    }


def restore(ws, snap: dict) -> None:
    ws.datasets = {k: DatasetDef.model_validate(v) for k, v in snap.get("datasets", {}).items()}
    ws.mapping = {k: DatasetMapping.model_validate(v) for k, v in snap.get("mapping", {}).items()}
    ws.drafts = snap.get("drafts", {})
    ws.notes = snap.get("notes", {})
    ws.transcripts = snap.get("transcripts", {})
    ws.summary = snap.get("summary", "")
    ws.ignored = snap.get("ignored", [])
    ws.questions = [Question.model_validate(q) for q in snap.get("questions", [])]
    ws.untested = set(ws.schemes)            # 数据或答复可能变了，每个方案都要重新试算


def apply_answers(questions: list[Question], answers: list[dict] | None) -> list[Question]:
    """把财务答复（questions.yaml 里填的 answer）对到问题上：先按编号，再按问题原文。返回本轮新答复的问题。"""
    fresh = []
    by_id = {a.get("id"): a for a in answers or [] if a.get("id")}
    by_text = {a.get("text"): a for a in answers or [] if a.get("text")}
    for q in questions:
        a = by_id.get(q.id) or by_text.get(q.text)
        if a is None or a.get("answer") in (None, ""):
            continue
        ans = a["answer"]
        if isinstance(ans, int) and not isinstance(ans, bool) and 0 <= ans < len(q.options):
            ans = q.options[ans]
        if q.answer != str(ans):
            q.answer = str(ans)
            fresh.append(q)
    return fresh


def file_changes(before: dict[str, str], now: dict[str, str]) -> dict[str, list[str]]:
    return {"新增": sorted(set(now) - set(before)), "删除": sorted(set(before) - set(now)),
            "修改": sorted(n for n in set(now) & set(before) if now[n] != before[n])}


def digest(results: dict[str, dict[str, Result]]) -> dict:
    """结果摘要：方案 -> 修订 -> 对象 -> [名称, 应得, 本次实发]。"""
    out = {}
    for s, revs in results.items():
        for rev, r in revs.items():
            out.setdefault(s, {})[rev] = {
                nid: [n.label, n.final, r.payouts[nid].paid if nid in r.payouts else None]
                for nid, n in r.nodes.items() if n.key}
    return out


def diff_results(before: dict, results: dict[str, dict[str, Result]]) -> list[list]:
    """与上一轮逐个对象对比：[方案, 修订, 对象, 名称, 上一轮应得, 本轮应得, 差额, 上一轮实发, 本轮实发]，只列有变化的。"""
    now = digest(results)
    rows = []
    for s in sorted(set(before) | set(now)):
        for rev in sorted(set(before.get(s, {})) | set(now.get(s, {}))):
            b, n = before.get(s, {}).get(rev, {}), now.get(s, {}).get(rev, {})
            for nid in sorted(set(b) | set(n)):
                (lb, fb, pb), (ln, fn, pn) = b.get(nid, [None] * 3), n.get(nid, [None] * 3)
                if fb == fn and pb == pn:
                    continue
                delta = (Decimal(fn or 0) - Decimal(fb or 0)) if (fb or fn) else None
                rows.append([s, rev, nid, ln or lb, _f(fb), _f(fn), float(delta) if delta is not None else None,
                             _f(pb), _f(pn)])
    return rows


def _f(v):
    return None if v is None else float(Decimal(v))


def diff_rules(before: dict, now: dict) -> list[str]:
    """规则草稿的改动：每个方案的参数表、变量按名字比，其余部分整体比。"""
    lines = []
    for s in sorted(set(before) | set(now)):
        b, n = before.get(s, {}), now.get(s, {})
        if s not in before:
            lines.append(f"新增方案「{s}」")
            continue
        if s not in now:
            lines.append(f"删除方案「{s}」")
            continue
        parts = []
        for k in ("tables", "variables"):
            bk, nk = b.get(k) or {}, n.get(k) or {}
            add = [x for x in nk if x not in bk]
            rm = [x for x in bk if x not in nk]
            mod = [x for x in nk if x in bk and _strip(nk[x]) != _strip(bk[x])]
            label = "参数表" if k == "tables" else "变量"
            parts += ([f"{label}新增 {'、'.join(add)}"] if add else []) + ([f"{label}删除 {'、'.join(rm)}"] if rm else []) \
                + ([f"{label}修改 {'、'.join(mod)}"] if mod else [])
        for k, label in (("pool", "奖金池"), ("allocation", "分配层级与权重"), ("payout", "发放条件"), ("offsets", "已发抵扣"),
                         ("checks", "数据校验"), ("revisions", "修订")):
            if _strip(b.get(k)) != _strip(n.get(k)):
                parts.append(f"{label}修改")
        if parts:
            lines.append(f"「{s}」：" + "；".join(parts))
    return lines


def _strip(v):
    """比较时忽略出处、说明、置信度这些不影响计算的字段。"""
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items() if k not in ("sources", "desc", "confidence")}
    if isinstance(v, list):
        return [_strip(x) for x in v]
    return v


def archive(out: Path, n: int, paths: dict[str, Path]) -> Path:
    """把本轮输出复制到 history/第N轮/。"""
    d = Path(out) / "history" / f"第{n}轮"
    d.mkdir(parents=True, exist_ok=True)
    for p in paths.values():
        if p.exists() and p.is_file():
            shutil.copy2(p, d / p.name)
    return d


def new_round(case: dict | None, files: dict[str, str]) -> dict:
    rounds = (case or {}).get("rounds", [])
    return {"n": len(rounds) + 1, "at": datetime.now().isoformat(timespec="seconds"), "files": files}
