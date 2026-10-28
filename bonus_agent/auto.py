"""全自动模式：给一批文件（规则文档 + Excel 数据，内容不固定），由大模型统筹规划，引擎计算，输出 Excel。

流程：
1. 盘点：解析所有文件，Excel 里识别出每个工作表的表头和数据行；
2. 规划（大模型）：判断哪些是规则、哪些是数据，建立数据字典和列映射，规划分配层级；
   程序校验（工作表/列是否存在、类型能否转换、表之间能否关联），有错就让模型修正；
3. 抽取规则（大模型）：在规划好的数据字典上抽取规则 DSL；
   静态校验通过后用真实数据试算，试算报错也交给模型修正；
4. 计算（引擎，不经过大模型），输出结果 Excel，以及可复算的规则、映射和待确认问题。

文档没写清楚的地方，按模型建议的理解先算一版，问题写进结果；财务在 questions.yaml 里填写答复后
用 --answers 重跑即可。
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from bonus_agent.extract_rules import ExtractResult, Question, extract, prompt
from bonus_agent.llm import LLM, LLMError, parse_json
from bonus_agent.parsing.docs import PARSERS, Block, ParsedDoc, md_table, parse_any
from bonus_core.data.mapping import DatasetMapping, Mapping, apply_mapping
from bonus_core.data.table import ConvertError, convert
from bonus_core.data.table_detect import SheetTable, detect
from bonus_core.dsl.functions import FUNCTIONS
from bonus_core.dsl.models import DatasetDef, FieldDef, FieldType, RuleSet
from bonus_core.engine.allocation import AdjustConflict
from bonus_core.engine.evaluator import EvalError
from bonus_core.engine.issues import EngineError, Issue
from bonus_core.engine.run import Result, compute
from bonus_core.report.excel import write_excel

DATA_SUFFIXES = {".xlsx", ".xlsm"}
SKIP_DIRS = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", "site-packages", ".pytest_cache",
             ".hypothesis", "reports"}
MAX_FILES = 30
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
RESERVED = {"and", "or", "not"} | set(FUNCTIONS)


# ---------------- 1. 盘点 ----------------
class TooManyFiles(ValueError):
    pass


def collect_files(paths: list[str | Path], exclude: list[str | Path] = (), max_files: int = MAX_FILES) -> list[Path]:
    """展开文件夹，跳过临时文件、程序目录和输出目录。一批计算应只包含本批次的文件。"""
    skip = [Path(e).resolve() for e in exclude]
    out: list[Path] = []
    for p in map(Path, paths):
        cands = sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else [p]
        for f in cands:
            rel = f.relative_to(p).parts[:-1] if p.is_dir() else ()
            if f.name.startswith(("~$", ".")) or f.suffix.lower() not in PARSERS:
                continue
            if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel):
                continue
            rf = f.resolve()
            if any(rf.is_relative_to(e) for e in skip) or rf in out:
                continue
            out.append(rf)
    if len(out) > max_files:
        raise TooManyFiles(f"找到 {len(out)} 个文件，超过上限 {max_files}。一次只放一个批次（一个部门、一期）的规则和数据，"
                           f"不要传整个项目目录；确实需要更多文件可加 --max-files。前几个文件：{[f.name for f in out[:8]]}")
    return out


def sheet_ref(st: SheetTable) -> str:
    return f"{st.file}/{st.sheet}"


@dataclass
class Inventory:
    files: list[Path]
    docs: list[ParsedDoc]                   # Word / PPT / 图片
    sheets: list[SheetTable]                # Excel 中识别出表格的工作表
    sheet_text: dict[str, str]              # "文件/工作表" -> 整张表的 Markdown（作规则用时交给模型）

    def sheet(self, ref: str) -> SheetTable | None:
        ref = ref.strip()
        hit = [s for s in self.sheets if sheet_ref(s) == ref]
        if not hit:  # 只写了工作表名，且不重名
            hit = [s for s in self.sheets if s.sheet == ref]
        return hit[0] if len(hit) == 1 else None


def build_inventory(files: list[Path]) -> Inventory:
    docs, sheets, text = [], [], {}
    for f in files:
        if f.suffix.lower() in DATA_SUFFIXES:
            sheets += detect(f)
            for b in parse_any(f).blocks:  # 整表文字，供规划判断和作为规则来源
                title, _, body = b.text.partition("\n")
                text[f"{f.name}/{title.removeprefix('工作表：')}"] = body
        else:
            docs.append(parse_any(f))
    return Inventory(files, docs, sheets, text)


def _fmt(v) -> str:
    return "" if v is None else str(v).replace("\n", " ")[:30]


def render_inventory(inv: Inventory, preview_rows: int = 5, doc_chars: int = 1500) -> str:
    parts = []
    for d in inv.docs:
        txt = d.full_text()
        imgs = sum(1 for b in d.blocks if b.kind == "image")
        parts.append(f"## 文件：{d.name}\n" + (txt[:doc_chars] + ("……（后略）" if len(txt) > doc_chars else ""))
                     + (f"\n[含 {imgs} 张图片，规则抽取时会转写]" if imgs else ""))
    detected = {sheet_ref(s) for s in inv.sheets}
    for s in inv.sheets:
        rows = [s.headers] + [[_fmt(v) for v in vals] for _, vals in s.rows[:preview_rows]]
        parts.append(f"## 工作表：{sheet_ref(s)}（表头在第 {s.header_row} 行，共 {len(s.rows)} 行数据"
                     + (f"，另有 {len(s.totals)} 行合计" if s.totals else "") + "）\n"
                     + "表头：" + json.dumps([h for h in s.headers if h], ensure_ascii=False) + "\n"
                     + md_table(rows))
    for ref, body in inv.sheet_text.items():
        if ref not in detected:
            parts.append(f"## 工作表：{ref}（未识别出规整的表格，以下为原始内容）\n" + body[:doc_chars])
    return "\n\n".join(parts)


# ---------------- 2. 规划 ----------------
class PlanField(BaseModel):
    model_config = ConfigDict(extra="ignore")
    column: str
    type: FieldType
    desc: str = ""
    unit: str | None = None


class PlanDataset(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sheet: str
    desc: str = ""
    key: str | None = None
    link: str | None = None
    fields: dict[str, PlanField]


class PlanLevel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    level: str
    key: str
    parent_key: str | None = None


class Ignored(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ref: str
    reason: str = ""


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = ""
    rule_files: list[str] = []
    rule_sheets: list[str] = []
    datasets: dict[str, PlanDataset]
    hierarchy: list[PlanLevel]
    ignored: list[Ignored] = []
    questions: list[Question] = []


PLAN_EXAMPLE = {
    "summary": "销售部季度奖金：按部门销售额阶梯计提奖金池，按小组绩效分到小组，组内按业绩和工龄分到个人；未回款部分暂缓发放。",
    "rule_files": ["提成办法.docx"],
    "rule_sheets": ["业务数据.xlsx/区域系数"],
    "datasets": {
        "人员": {"sheet": "业务数据.xlsx/人员名单", "key": "employee_id", "link": None, "desc": "每人一行",
                 "fields": {"employee_id": {"column": "工号", "type": "id", "desc": "工号"},
                            "name": {"column": "姓名", "type": "text", "desc": "姓名"},
                            "group_id": {"column": "所属小组", "type": "id", "desc": "小组编号"},
                            "seniority": {"column": "工龄", "type": "number", "desc": "工龄（年）"}}},
        "销售明细": {"sheet": "业务数据.xlsx/合同明细", "key": None, "link": "employee_id",
                     "fields": {"employee_id": {"column": "负责人工号", "type": "id", "desc": "负责人"},
                                "sales_amount": {"column": "合同金额（万元）", "type": "money", "unit": "万元", "desc": "合同额"}}},
    },
    "hierarchy": [{"level": "小组", "key": "group_id", "parent_key": None},
                  {"level": "个人", "key": "employee_id", "parent_key": "group_id"}],
    "ignored": [{"ref": "业务数据.xlsx/填表说明", "reason": "填写说明，不含数据"}],
    "questions": [{"id": "p1", "text": "人员名单和花名册两张表内容重复，以哪张为准？", "options": ["人员名单", "花名册"], "suggested": 0}],
}


@dataclass
class PlanOutcome:
    plan: Plan | None
    datasets: dict[str, DatasetDef]
    mapping: Mapping
    warnings: list[str]
    errors: list[str]
    raw_outputs: list[str]
    attempts: int


def _fix_column(col: str, headers: list[str]) -> str | None:
    if col in headers:
        return col
    norm = {re.sub(r"\s", "", h): h for h in headers if h}
    c = re.sub(r"\s", "", col)
    if c in norm:
        return norm[c]
    close = difflib.get_close_matches(c, list(norm), n=1, cutoff=0.85)
    return norm[close[0]] if close else None


def check_plan(plan: Plan, inv: Inventory) -> tuple[dict[str, DatasetDef], Mapping, list[str], list[str]]:
    """程序校验规划结果，并生成数据字典和列映射。返回 (数据字典, 映射, 警告, 错误)。"""
    errors, warnings = [], []
    datasets: dict[str, DatasetDef] = {}
    mapping: Mapping = {}
    field_types: dict[str, tuple[str, str]] = {}
    for name, pd in plan.datasets.items():
        st = inv.sheet(pd.sheet)
        if st is None:
            errors.append(f"数据表「{name}」的 sheet「{pd.sheet}」不存在或有重名，请写成清单里的「文件/工作表」原样")
            continue
        if pd.key and pd.link:
            errors.append(f"数据表「{name}」不能同时有 key 和 link")
        cols, fields = {}, {}
        if not pd.fields:
            errors.append(f"数据表「{name}」没有字段")
        for f, pf in pd.fields.items():
            if not FIELD_NAME.match(f) or f in RESERVED:
                errors.append(f"「{name}」的字段名 {f} 不合法：只能用英文小写字母、数字和下划线，且不能是函数名或 and/or/not")
                continue
            col = _fix_column(pf.column, st.headers)
            if col is None:
                errors.append(f"「{name}」字段 {f} 的列「{pf.column}」不在工作表 {sheet_ref(st)} 的表头中，"
                              f"可选表头：{[h for h in st.headers if h]}")
                continue
            if col != pf.column:
                warnings.append(f"「{name}」字段 {f} 的列名「{pf.column}」已自动更正为「{col}」")
            unit = pf.unit if pf.unit in ("元", "万元") else None
            if pf.type == "money" and unit is None:
                unit = "万元" if "万" in col else "元"
                warnings.append(f"「{name}」字段 {f} 未给出单位，按表头判断为{unit}")
            values = None
            if pf.type == "enum":
                i = st.headers.index(col)
                values = sorted({str(v).strip() for _, vals in st.rows if (v := vals[i]) not in (None, "")})
                if len(values) > 30:
                    errors.append(f"「{name}」字段 {f} 有 {len(values)} 种取值，不适合作为 enum，请改为 text")
                    continue
            prev = field_types.get(f)
            if prev and prev[0] != pf.type:
                errors.append(f"字段 {f} 在「{prev[1]}」里是 {prev[0]}，在「{name}」里是 {pf.type}；同名字段类型必须一致")
            field_types.setdefault(f, (pf.type, name))
            fields[f] = FieldDef(type=pf.type, desc=pf.desc or col, unit=unit if pf.type == "money" else None,
                                 values=values)
            cols[f] = col
        for k in (pd.key, pd.link):
            if k and k not in fields:
                errors.append(f"数据表「{name}」的 {'key' if k == pd.key else 'link'} 字段 {k} 不在它的 fields 里")
        # 试转换全部行，类型判断错了（如把文字当成金额）尽早发现
        bad = []
        for f, fd in fields.items():
            i = st.headers.index(cols[f])
            for excel_row, vals in st.rows:
                try:
                    convert(vals[i], fd)
                except Exception as e:  # 任何转换失败都反馈给模型，而不是中断规划
                    bad.append(f"{f}（{fd.type}）第 {excel_row} 行：{e}")
                    break
        if bad:
            errors.append(f"「{name}」有字段类型与数据不符：" + "；".join(bad[:5])
                          + "。请改成与数据相符的类型；月份、期间这类标签如果不参与日期计算，用 text 即可")
        datasets[name] = DatasetDef(desc=pd.desc, key=pd.key, link=pd.link, fields=fields)
        mapping[name] = DatasetMapping(file=st.file, sheet=st.sheet, columns=cols)

    # 层级与关联
    if not plan.hierarchy:
        errors.append("hierarchy 不能为空，至少要有一个分配层级（如 个人）")
    keys = [lv.key for lv in plan.hierarchy]
    entity = {d.key: n for n, d in datasets.items() if d.key}
    for i, lv in enumerate(plan.hierarchy):
        ent = entity.get(lv.key)
        if i == len(plan.hierarchy) - 1 and ent is None:
            errors.append(f"最末层「{lv.level}」需要一张 key={lv.key} 的实体表（每个对象一行）")
        if i > 0:
            if not lv.parent_key:
                errors.append(f"层级「{lv.level}」需要 parent_key 指向上一层「{plan.hierarchy[i - 1].level}」")
            elif ent and lv.parent_key not in datasets[ent].fields:
                errors.append(f"层级「{lv.level}」的 parent_key {lv.parent_key} 不在实体表「{ent}」的字段里")
    for n, d in datasets.items():
        if d.link and d.link not in keys:
            errors.append(f"明细表「{n}」的 link={d.link} 不是任何层级的 key（层级 key：{keys}）")
        if d.key and d.key not in keys:
            warnings.append(f"实体表「{n}」的主键 {d.key} 不是任何层级的 key，表中数据不会参与计算")
    for ref in plan.rule_sheets:
        if ref not in inv.sheet_text and inv.sheet(ref) is None:
            errors.append(f"rule_sheets 里的「{ref}」不存在，请写成清单里的「文件/工作表」原样")
    return datasets, mapping, warnings, errors


def make_plan(llm: LLM, inv: Inventory, max_repairs: int = 2) -> PlanOutcome:
    text = prompt("plan").replace("{inventory}", render_inventory(inv)) \
        .replace("{example}", json.dumps(PLAN_EXAMPLE, ensure_ascii=False, indent=1))
    messages = [{"role": "user", "content": text}]
    raws: list[str] = []
    plan, datasets, mapping, warnings, errors = None, {}, {}, [], []
    attempts = 0
    for attempts in range(1, max_repairs + 2):
        try:
            out = llm.chat(messages, json_out=True, temperature=0.1)
        except LLMError as e:
            return PlanOutcome(None, {}, {}, [], [str(e)], raws, attempts)
        raws.append(out)
        try:
            plan = Plan.model_validate(parse_json(out))
        except (LLMError, ValidationError, ValueError) as e:
            plan, errors = None, [f"输出不符合格式：{e}"[:3000]]
        else:
            datasets, mapping, warnings, errors = check_plan(plan, inv)
        if not errors:
            break
        messages = messages + [
            {"role": "assistant", "content": out},
            {"role": "user", "content": "你的规划有以下问题，请修正后重新输出完整的 JSON（只输出 JSON）：\n"
                                        + "\n".join(f"- {e}" for e in errors)},
        ]
    return PlanOutcome(plan, datasets, mapping, warnings, errors, raws, attempts)


# ---------------- 3. 抽取规则（带试算） ----------------
def rule_docs(inv: Inventory, plan: Plan) -> list[ParsedDoc]:
    ignored = {i.ref for i in plan.ignored}
    docs = [d for d in inv.docs if d.name not in ignored]
    for ref in plan.rule_sheets:
        body = inv.sheet_text.get(ref)
        if body is None and (st := inv.sheet(ref)) is not None:
            ref = sheet_ref(st)
            body = inv.sheet_text.get(ref, md_table([st.headers] + [v for _, v in st.rows]))
        if body:
            name = ref.split("/", 1)[0]
            doc = next((d for d in docs if d.name == name), None)
            if doc is None:
                doc = ParsedDoc(name)
                docs.append(doc)
            doc.blocks.append(Block(name, None, "table", f"工作表：{ref.split('/', 1)[-1]}\n{body}"))
    return docs


def plan_hint(plan: Plan, answers: list[dict] | None) -> str:
    lines = ["统筹规划的结论（数据字典已按此建立）：", plan.summary,
             "分配层级（从上到下）：" + " → ".join(f"{lv.level}（key={lv.key}）" for lv in plan.hierarchy),
             "请按这个层级输出 allocation。文档没有写某一层怎么分时，按最合理的方式（如按该层业绩或计算基数的占比）"
             "写出，并在 questions 里提问；文档没写的条件不要自己添加。"]
    answered = [a for a in answers or [] if a.get("answer") not in (None, "")]
    if answered:
        lines.append("\n财务已确认的答复（必须按此处理，不要再就这些问题提问）：")
        for a in answered:
            ans = a["answer"]
            if isinstance(ans, int) and 0 <= ans < len(a.get("options") or []):
                ans = a["options"][ans]
            lines.append(f"- {a.get('text', '')} → {ans}")
    return "\n".join(lines)


def _compute(rs: RuleSet, files: list[Path], mapping: Mapping) -> tuple[Result | None, list[Issue], str]:
    data, issues = apply_mapping(rs, files, mapping)
    blocking = [i for i in issues if i.severity == "blocking"]
    if blocking:
        return None, issues, "；".join(str(i) for i in blocking[:5])
    try:
        return compute(rs, data), issues, ""
    except EngineError as e:
        return None, issues + list(e.issues), "；".join(str(i) for i in e.issues if i.severity == "blocking")[:2000]
    except (EvalError, AdjustConflict, ValueError) as e:
        return None, issues, str(e)[:2000]


def trial_check(files: list[Path], mapping: Mapping):
    def check(rs: RuleSet) -> list[str]:
        res, _, err = _compute(rs, files, mapping)
        if err:
            return [f"用本期真实数据试算失败：{err}"]
        if res is not None and res.pool == 0:
            return ["用本期真实数据试算，奖金池为 0。请检查：阶梯门槛和金额是否都按“元”（数据已统一换算为元）、"
                    "聚合的字段和筛选条件是否正确。如果按规则确实为 0，保持不变并在 questions 里说明。"]
        return []
    return check


# ---------------- 4. 总流程 ----------------
@dataclass
class AutoResult:
    plan: PlanOutcome
    extraction: ExtractResult | None = None
    result: Result | None = None
    issues: list[Issue] = field(default_factory=list)
    outputs: dict[str, Path] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.result is not None

    @property
    def questions(self) -> list[tuple[str, Question]]:
        qs = [("规划", q) for q in (self.plan.plan.questions if self.plan.plan else [])]
        return qs + [("规则", q) for q in (self.extraction.questions if self.extraction else [])]


def run_auto(llm: LLM, paths: list[str | Path], out_dir: str | Path | None = None, *,
             answers: list[dict] | None = None, max_repairs: int = 2, max_files: int = MAX_FILES,
             on_files=None) -> AutoResult:
    try:
        files = collect_files(paths, exclude=[out_dir] if out_dir else [], max_files=max_files)
    except TooManyFiles as e:
        return AutoResult(PlanOutcome(None, {}, {}, [], [], [], 0), error=str(e))
    if on_files:
        on_files(files)
    if not files:
        return AutoResult(PlanOutcome(None, {}, {}, [], [], [], 0), error="没有找到可处理的文件")
    inv = build_inventory(files)
    po = make_plan(llm, inv, max_repairs)
    ar = AutoResult(po)
    if po.plan is None or po.errors:
        ar.error = "统筹规划失败：" + "；".join(po.errors)[:1000]
    else:
        data_files = [f for f in files if f.suffix.lower() in DATA_SUFFIXES]
        ar.extraction = ex = extract(llm, [], po.datasets, name=po.plan.summary[:30] or "智能体草稿",
                                     version="auto-draft", docs=rule_docs(inv, po.plan),
                                     hint=plan_hint(po.plan, answers), check=trial_check(data_files, po.mapping),
                                     max_repairs=max_repairs)
        if ex.ruleset is None:
            ar.error = "规则抽取失败：" + (ex.error or "")
        else:
            ar.result, ar.issues, err = _compute(ex.ruleset, data_files, po.mapping)
            if err:
                ar.error = "计算失败：" + err
    if out_dir is not None:
        ar.outputs = write_outputs(ar, Path(out_dir), answers)
    return ar


def _questions_rows(ar: AutoResult, answers: list[dict] | None) -> list[dict]:
    prev = {a.get("text"): a.get("answer") for a in answers or []}
    rows = []
    for stage, q in ar.questions:
        rows.append({"id": q.id, "stage": stage, "text": q.text, "options": q.options,
                     "suggested": q.suggested, "related": q.related, "answer": prev.get(q.text)})
    # 已答复的问题模型不会再问，保留在文件里
    asked = {r["text"] for r in rows}
    rows += [dict(a) for a in answers or [] if a.get("text") not in asked]
    return rows


def write_outputs(ar: AutoResult, out: Path, answers: list[dict] | None) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    po = ar.plan
    raw = {"plan_outputs": po.raw_outputs, "plan_errors": po.errors, "plan_warnings": po.warnings}
    if po.plan:
        paths["plan"] = out / "plan.json"
        paths["plan"].write_text(po.plan.model_dump_json(indent=2), encoding="utf-8")
    if po.mapping:
        paths["mapping"] = out / "mapping.yaml"
        paths["mapping"].write_text(yaml.safe_dump({k: v.model_dump(exclude_none=True, exclude_defaults=True)
                                                    for k, v in po.mapping.items()}, allow_unicode=True, sort_keys=False),
                                    encoding="utf-8")
    ex = ar.extraction
    if ex is not None:
        raw.update(extract_outputs=ex.raw_outputs, transcripts=ex.transcripts,
                   extract_issues=[str(i) for i in ex.issues])
        if ex.ruleset is not None:
            paths["rules"] = out / "rules.yaml"
            paths["rules"].write_text(ex.ruleset.dump_yaml(), encoding="utf-8")
    qrows = _questions_rows(ar, answers)
    paths["questions"] = out / "questions.yaml"
    paths["questions"].write_text(
        "# 财务答复：在 answer 里填选项序号（从 0 开始）或直接写文字，然后用 --answers 这个文件重跑\n"
        + yaml.safe_dump(qrows, allow_unicode=True, sort_keys=False), encoding="utf-8")
    paths["log"] = out / "agent_log.json"
    paths["log"].write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    if ar.result is not None and ex is not None and ex.ruleset is not None:
        paths["excel"] = out / "result.xlsx"
        write_excel(ex.ruleset, ar.result, paths["excel"], extra_sheets=_extra_sheets(ar, qrows))
    return paths


def _extra_sheets(ar: AutoResult, qrows: list[dict]):
    po, plan = ar.plan, ar.plan.plan
    rows = [["状态", "智能体草稿：规则和数据映射均由大模型生成、尚未经财务确认。请先处理「待确认问题」，确认后重算。"],
            ["方案概述", plan.summary],
            ["规则来源", "、".join([d for d in plan.rule_files] + plan.rule_sheets)],
            ["分配层级", " → ".join(f"{lv.level}（{lv.key}）" for lv in plan.hierarchy)]]
    for n, d in po.datasets.items():
        m = po.mapping[n]
        kind = f"实体表，主键 {d.key}" if d.key else f"明细表，关联 {d.link}" if d.link else "公司级表"
        rows.append([f"数据表：{n}", f"{m.file}/{m.sheet}（{kind}）"])
        rows += [[f"  {f}", f"← 「{c}」 {d.fields[f].type}" + (f"，{d.fields[f].unit}" if d.fields[f].unit else "")]
                 for f, c in m.columns.items()]
    rows += [["已忽略", f"{i.ref}：{i.reason}"] for i in plan.ignored]
    rows += [["自动更正", w] for w in po.warnings]
    if ar.extraction is not None:
        rows.append(["原文引用命中", f"{ar.extraction.quote_hits}/{ar.extraction.quote_total}"])
    q = [[r["id"], r.get("stage", ""), r["text"], " / ".join(r.get("options") or []),
          (r["options"][r["suggested"]] if isinstance(r.get("suggested"), int)
           and 0 <= r["suggested"] < len(r.get("options") or []) else ""), "" if r.get("answer") is None else str(r["answer"])]
         for r in qrows]
    return [("待确认问题", ["编号", "阶段", "问题", "选项", "本次计算采用", "财务答复"], q,
             {"问题": 60, "选项": 40, "本次计算采用": 20}),
            ("智能体规划", ["项目", "内容"], rows, {"项目": 22, "内容": 100})]
