"""场景记忆（长期）：发布规则版本时，把这次财务确认过的口径（智能体的提问和财务的答复）记下来。
下次在同一场景编制时交给智能体参考，同样的问题不用财务再答一遍；与新材料冲突或不确定时，智能体仍要提问。
记忆只是参考，不是规则：金额只按已发布的规则版本或本轮确认的规则算。"""
from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml

LIMIT = 60          # 交给智能体的最多条数（最新的在前）


def _key(text: str) -> str:
    t = "".join(unicodedata.normalize("NFKC", text or "").split()).casefold()
    return hashlib.sha256(t.encode()).hexdigest()[:12]


def answer_text(q: dict) -> str:
    """问题的答复转成文字：选项序号换成选项内容，附带的补充说明一起带上。"""
    a = q.get("answer")
    opts = q.get("options") or []
    if isinstance(a, bool):
        a = str(a)
    if isinstance(a, int) and 0 <= a < len(opts):
        a = opts[a]
    elif isinstance(a, str) and a.strip().isdigit() and int(a) < len(opts):
        a = opts[int(a)]
    text = str(a if a is not None else "").strip()
    note = str(q.get("note") or q.get("answer_note") or "").strip()
    return f"{text}（补充：{note}）" if note and note not in text else text


class ScenarioMemory:
    def __init__(self, path: Path):
        self.path = Path(path)

    def items(self) -> list[dict]:
        if not self.path.exists():
            return []
        return list(yaml.safe_load(self.path.read_text(encoding="utf-8")) or [])

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self.path)

    def add_questions(self, questions: list[dict], *, version: str, source: str = "", by: str = "") -> int:
        """记下已答复（未撤回）的问题；同一个问题（文字相同）只留最新的答复。返回记下的条数。"""
        at = datetime.now().isoformat(timespec="seconds")
        new = []
        for q in questions or []:
            if q.get("withdrawn") or q.get("answer") in (None, "") or not q.get("text"):
                continue
            ans = answer_text(q)
            if ans:
                new.append({"id": _key(q["text"]), "question": str(q["text"]).strip(), "answer": ans,
                            "version": version, "source": source, "by": by, "at": at})
        if not new:
            return 0
        ids = {x["id"] for x in new}
        self._save(new + [x for x in self.items() if x.get("id") not in ids])
        return len(new)

    def remove(self, ids: list[str]) -> int:
        items = self.items()
        keep = [x for x in items if x.get("id") not in set(ids)]
        if len(keep) != len(items):
            self._save(keep)
        return len(items) - len(keep)

    def add_repairs(self, repairs: dict, *, version: str, source: str = "", by: str = "") -> int:
        """Retain only verified, published case lessons; never promote them to business rules."""
        lessons = []
        for ident, repair in repairs.items():
            report = repair.get("report") or {}
            if repair.get("status") != "case_validated" or report.get("status") != "PASS" or not report.get("fingerprint"):
                continue
            lessons.append({"id": 'repair-' + ident, "kind": "repair", "status": "case_validated",
                            "question": "历史修复，仅原案验证；使用前核对本期原文与条件",
                            "answer": str(repair.get("reason", "")), "version": version, "source": source, "by": by,
                            "fingerprint": report["fingerprint"], "at": datetime.now().isoformat(timespec="seconds")})
        if lessons:
            ids = {i['id'] for i in lessons}
            self._save(lessons + [i for i in self.items() if i.get('id') not in ids])
        return len(lessons)

    def brief(self, limit: int = LIMIT) -> str:
        items = self.items()[:limit]
        if not items:
            return ""
        return "\n".join(f"- 问：{x['question']}\n  答：{x['answer']}（{x.get('version', '')}）" for x in items)
