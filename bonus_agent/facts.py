"""参数台账与冲突检测：同一参数在不同文件（或同一文件不同位置）里写法不一致时，由程序判定冲突并向财务提问。

模型读材料时把每个参数、口径登记成一条“事实”：参数名、原文写法、所在文件、逐字引用的原文片段。
程序负责（不交给模型判断）：
- 引用片段必须真实出现在该文件里（文档文字、图片转写、Excel 单元格），取值必须出现在引用片段里；
- 同名参数换算后的取值不一致（5% 和 0.06、30万元 和 280000）就是冲突，自动生成阻断问题，列出各处原文；
  只有原文明确写了修改（“修改为”“以本通知为准”等）、标为 override 的那一条，才以它为准、不算冲突；
- 财务答复后，后来的材料又出现新的取值，再次提问；
- 完成前检查：每份规则文档都登记过参数；登记的数值都在规则里用到了（冲突的按财务答复的取值查）。
"""
from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict

from bonus_agent.extract_rules import Question

MAX_QUOTE = 200
OTHER = "都不对，另行说明"
_NUM = re.compile(r"^([-+]?\d+(?:\.\d+)?)(%|‰|万元|万|亿元|亿|元)?$")
_SCALE = {"%": Decimal("0.01"), "‰": Decimal("0.001"), "万": Decimal(10000), "万元": Decimal(10000),
          "亿": Decimal(10) ** 8, "亿元": Decimal(10) ** 8, "元": Decimal(1), None: Decimal(1)}
OVERRIDE_WORDS = ("修改为", "调整为", "改为", "变更为", "以本通知为准", "以本文件为准", "以此为准", "替代", "代替", "取代",
                  "废止", "不再执行", "停止执行", "更正为")
_IN_TEXT = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(%|‰|万元|万|亿元|亿)?")


class Fact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str            # 参数名，同一参数在不同文件里用同一个名字
    value: str           # 原文写法
    file: str
    quote: str           # 包含取值的原文片段
    meaning: str = ""    # 文字口径的规范说法（可选）
    override: bool = False   # 原文明确写了修改旧规定，以这一条为准
    round: int = 1


def norm(s: str) -> str:
    """比较原文时忽略全角半角、空白和标点。"""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = re.sub(r"第\d+行", "", s)            # read_sheet 输出的行号
    return re.sub(r"[\s，,。：:；;、“”\"'（）()【】\[\]|]", "", s)


def number(value: str) -> Decimal | None:
    """原文写法换算成数：5% → 0.05，30万元 → 300000；不是“数 + 单位”的写法返回 None（按文字口径比较）。"""
    m = _NUM.match(unicodedata.normalize("NFKC", value).replace(",", "").replace(" ", ""))
    if not m:
        return None
    try:
        return Decimal(m.group(1)) * _SCALE[m.group(2)]
    except InvalidOperation:
        return None


def same_value(f: Fact) -> str:
    """用来判断两条记录是否一致的取值。"""
    n = number(f.value)
    if n is not None:
        return format(n.normalize(), "f")
    return norm(f.meaning or f.value)


def label(f: Fact) -> str:
    return f.value + (f"（{f.meaning}）" if f.meaning and f.meaning != f.value else "")


def check_fact(f: Fact, corpus: dict[str, str]) -> str | None:
    """引用是否真实：返回错误原因，没问题返回 None。corpus：文件名 -> 规范化后的全文。"""
    if not f.name.strip() or not f.value.strip():
        return "name 和 value 不能为空"
    if f.file not in corpus:
        return f"没有文件「{f.file}」"
    q = norm(f.quote)
    if not q:
        return "quote 不能为空，要逐字复制包含取值的原文"
    if len(f.quote) > MAX_QUOTE:
        return f"quote 太长（{len(f.quote)} 字），只复制包含取值的那一句（不超过 {MAX_QUOTE} 字）"
    if q not in corpus[f.file]:
        return f"quote 在「{f.file}」里找不到，要逐字复制原文（图片要先 view_image 转写）"
    if norm(f.value) not in q:
        return f"value「{f.value}」不在 quote 里，value 写原文的写法（如 5%、30万元），换算交给程序"
    if f.override and not any(w in q for w in OVERRIDE_WORDS):
        return f"quote 里没有写明修改旧规定（如 {'、'.join(OVERRIDE_WORDS[:5])}），不能标为 override"
    return None


class Conflict(BaseModel):
    """一个冲突问题：参数名、各种取值（与问题选项一一对应）。"""
    name: str
    values: list[str]          # same_value
    labels: list[str]          # 选项文字里的取值
    question: str              # 问题编号
    round: int = 1             # 第几轮提出的


def _override(fs: list[Fact]) -> tuple[set[str], int]:
    """原文明确修改的取值、最近一次修改登记的轮次。"""
    over = [f for f in fs if f.override]
    return {same_value(f) for f in over}, max((f.round for f in over), default=0)


def detect(facts: list[Fact], conflicts: dict[str, Conflict], questions: list[Question], round_: int) -> list[Question]:
    """同名参数有不同取值且这组取值还没问过时，生成阻断问题。返回新生成的问题。"""
    groups: dict[str, list[Fact]] = {}
    for f in facts:
        groups.setdefault(f.name.strip(), []).append(f)
    new = []
    for name, fs in groups.items():
        by: dict[str, list[Fact]] = {}
        for f in fs:
            by.setdefault(same_value(f), []).append(f)
        if len(by) < 2:
            continue
        over, at = _override(fs)
        if len(over) == 1 and len(by) == 2 and not any(
                not f.override and same_value(f) not in over and f.round > at for f in fs):
            continue      # 原文明确把旧值修改为唯一的新值，之后没有别的材料再改
        if any(set(by) <= set(c.values) and c.round >= at for c in conflicts.values() if c.name == name):
            continue      # 这组取值问过了，之后也没有新的明确修改
        # 已答复过的取值仍然是新问题的选项之一
        vals = list(by)
        where = "；".join(f"{label(g[0])}：" + "，".join(f"{x.file}“{x.quote}”" for x in g) for g in by.values())
        q = Question(id=f"q{len(questions) + 1}", round=round_, related=f"参数:{name}", blocking=True,
                     text=f"材料中「{name}」有 {len(vals)} 种不同的取值，以哪个为准？{where}",
                     options=[f"{label(g[0])}（{'、'.join(dict.fromkeys(x.file for x in g))}）" for g in by.values()] + [OTHER])
        conflicts[f"{name}|{'|'.join(sorted(vals))}"] = Conflict(name=name, values=vals,
                                                               labels=[label(g[0]) for g in by.values()], question=q.id,
                                                               round=round_)
        questions.append(q)
        new.append(q)
    return new


def withdraw_stale(facts: list[Fact], conflicts: dict[str, Conflict], questions: list[Question]) -> list[Question]:
    """冲突所依据的某个取值已经没有材料支持（文件修改或删除）且还没答复的问题，由程序撤回。返回撤回的问题。"""
    out = []
    for k, c in list(conflicts.items()):
        q = next((q for q in questions if q.id == c.question), None)
        if q is None or q.answer or q.withdrawn:
            continue
        now = {same_value(f) for f in facts if f.name.strip() == c.name}
        if not set(c.values) <= now:
            q.withdrawn = "冲突所依据的材料已修改或删除，冲突不再成立"
            del conflicts[k]
            out.append(q)
    return out


def chosen(name: str, conflicts: dict[str, Conflict], questions: list[Question],
           facts: list[Fact] = ()) -> tuple[bool, str | None]:
    """参数是否有冲突（或被明确修改）、应采用的取值（same_value；财务答复不是某个选项时为 None）。取最近一次冲突。"""
    cs = [c for c in conflicts.values() if c.name == name]
    over, at = _override([f for f in facts if f.name.strip() == name])
    if len(over) == 1 and (not cs or cs[-1].round < at):     # 财务答复之后材料又明确修改了
        return True, over.pop()
    if not cs:
        return False, None
    c = cs[-1]
    q = next((q for q in questions if q.id == c.question), None)
    if q is None or not q.answer:
        return True, None
    i = next((i for i, o in enumerate(q.options) if o == q.answer), None)
    return True, (c.values[i] if i is not None and i < len(c.values) else None)


def draft_numbers(drafts: dict) -> set[str]:
    """规则草稿里出现的数（含 5% 换算成 0.05、5 两种），用于检查登记的参数是否用到了。"""
    out = set()
    for m in _IN_TEXT.finditer(json.dumps(drafts, ensure_ascii=False)):
        n = Decimal(m.group(1))
        out.add(format(n.normalize(), "f"))
        if m.group(2):
            out.add(format((n * _SCALE[m.group(2)]).normalize(), "f"))
    return out


def unused(facts: list[Fact], drafts: dict, conflicts: dict[str, Conflict], questions: list[Question]) -> list[str]:
    """登记了数值、规则里却没有用到的参数（冲突的按财务选定的取值查）。"""
    nums = draft_numbers(drafts)
    out = []
    for name in dict.fromkeys(f.name.strip() for f in facts):
        fs = [f for f in facts if f.name.strip() == name and number(f.value) is not None]
        if not fs:
            continue
        has, pick = chosen(name, conflicts, questions, fs)
        if has:
            if pick is None or pick in nums:
                continue
            out.append(f"{name}（财务选定 {pick}）")
            continue
        n = number(fs[0].value)
        raw = _NUM.match(unicodedata.normalize("NFKC", fs[0].value).replace(",", "").replace(" ", "")).group(1)
        if format(n.normalize(), "f") not in nums and format(Decimal(raw).normalize(), "f") not in nums:
            out.append(f"{name}（{fs[0].value}）")
    return out
