"""提示词不许带测试案例的说法。

模型看到的文字（prompts/*.md、工具说明、示例、报错信息）里如果出现评测材料的字段名、
表名、日期，模型就只会认这种写法，换一套材料就不灵；评测通过也说明不了泛化能力。
这里从 evals/cases 的材料里自动收集字段名和表名，再加上人工列的案例专有说法，
扫描所有发给模型的文字，出现即失败。新增案例时词表自动扩充。
"""
import ast
import glob
import json
import re
from pathlib import Path

import openpyxl
import yaml

ROOT = Path(__file__).resolve().parents[1]

# 通用词，任何材料都会有，不算泄露
GENERIC = {
    "key", "link", "fields", "name", "desc", "unit", "type", "filter", "file", "sheet", "columns",
    "month", "months", "period", "status", "grade", "region", "share", "approved", "difficulty",
    "员工", "部门", "项目", "小组", "月份", "负责人", "部门名称", "填表说明",
    # 规则语言自己的关键字
    "parent_key", "hold_ratio", "pay_ratio", "lower_inclusive", "upper_inclusive",
    "数据表", "字段名", "人工调整",
}

# 评测材料（含财务验收案例）里的专有说法，材料不在仓库里的也列在这
CURATED = {
    "recognition_date", "receipt_date", "receipt_amount", "receipt_id", "base_bonus", "contract_date",
    "entity_type", "entity_id", "sales_id", "project_id", "join_date", "dept_id", "group_id",
    "employee_id", "sales_amount", "acceptance_date", "milestone_id", "perf_q3", "perf_fy",
    "QUARTER", "PROCESS", "ANNUAL", "finance_answers", "negative_cases", "Q_CASH",
    "已验收", "项目参与", "回款明细", "销售明细", "绩效评分", "毛利率", "员工花名册", "历史已发",
    "个人目标", "小组目标", "部门目标", "部门KPI", "月度利润",
}
DATE = re.compile(r"2026[-/.年]")
# 材料里的对象编号（如 E1001、P001），提示词举例用了它等于把答案的一部分写进去
ID = re.compile(r"[A-Z]{1,3}\d{2,}")


def _case_words():
    words = set(CURATED)
    for p in glob.glob(str(ROOT / "evals/cases/**/*.xlsx"), recursive=True):
        wb = openpyxl.load_workbook(p, read_only=True)
        for ws in wb.worksheets:
            words.add(ws.title)
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                cells = [str(c) for c in row if c is not None]
                if i == 0:
                    words.update(cells)
                words.update(c for c in cells if ID.fullmatch(c))
    for p in glob.glob(str(ROOT / "evals/cases/**/*.json"), recursive=True):
        def walk_json(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    words.add(str(k))
                    walk_json(v)
            elif isinstance(x, list):
                for v in x:
                    walk_json(v)
            elif isinstance(x, str) and ID.fullmatch(x):
                words.add(x)
        walk_json(json.load(open(p, encoding="utf-8")))
    for p in glob.glob(str(ROOT / "evals/cases/**/*.yaml"), recursive=True):
        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    words.add(str(k))
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(yaml.safe_load(open(p, encoding="utf-8")))
    # 只留像字段名（snake_case）或短中文表名的词；句子、数字、通用词不算
    keep = set()
    for w in words:
        w = w.strip()
        if w in GENERIC or not w:
            continue
        if re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", w) or w in CURATED or ID.fullmatch(w):
            keep.add(w)
        elif re.fullmatch(r"[一-鿿A-Za-z]{3,8}", w) and not w.isascii():
            keep.add(w)
    return keep


def _strings(path):
    """模块里发给模型的字符串：去掉 docstring，保留其他字符串常量（含 f-string 的固定部分）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out.append((node.lineno, node.value))
    return out


MODEL_FACING = [
    "bonus_agent/agent.py", "bonus_agent/auto.py", "bonus_agent/extract_rules.py", "bonus_agent/fixes.py",
    "bonus_agent/lint.py", "bonus_agent/failure.py", "bonus_core/dsl/validator.py",
    "bonus_core/dsl/functions.py", "bonus_core/dsl/scope.py", "bonus_core/engine/evaluator.py",
]


def _texts():
    for p in sorted((ROOT / "bonus_agent/prompts").glob("*.md")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            yield f"{p.relative_to(ROOT)}:{i}", line
    for rel in MODEL_FACING:
        p = ROOT / rel
        if p.exists():
            for line, s in _strings(p):
                yield f"{rel}:{line}", s
    from bonus_agent.agent import TOOLS
    yield "agent.TOOLS", json.dumps(TOOLS, ensure_ascii=False)


def _matcher(words):
    """一个正则匹配全部词；英文和编号要整词匹配，中文按子串。"""
    asc = sorted((w for w in words if w.isascii()), key=len, reverse=True)
    cjk = sorted((w for w in words if not w.isascii()), key=len, reverse=True)
    parts = []
    if asc:
        parts.append(r"(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, asc)) + r")(?![A-Za-z0-9_])")
    if cjk:
        parts.append("|".join(map(re.escape, cjk)))
    return re.compile("|".join(parts))


def test_vocabulary_is_collected():
    words = _case_words()
    assert "recognition_date" in words and "项目参与" in words
    assert "key" not in words
    assert any(ID.fullmatch(w) for w in words)


def test_model_facing_text_has_no_case_vocabulary():
    pat = _matcher(_case_words())
    hits = []
    for where, text in _texts():
        for m in pat.finditer(text):
            hits.append(f"{where}: {m.group(0)}")
        if DATE.search(text):
            hits.append(f"{where}: 2026 日期")
    assert not hits, "提示词里出现了测试案例的说法，请换成中性写法：\n" + "\n".join(sorted(set(hits)))
