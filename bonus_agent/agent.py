"""大模型主导的奖金计算智能体：不写死流程，模型通过工具自己决定看什么、怎么建模、怎么核对。

模型能做的事都封装成工具（看文件、看表、看图、定义数据表、写规则、试算、查算式、算表达式、提问、完成），
程序只负责：执行工具、校验输入、保存工作区状态。金额始终由计算引擎按模型写的规则算出。

长任务的上下文控制：工作区状态（已定义的数据表、规则、问题、试算结论）每轮重新附在请求末尾，
对话历史只保留最近的若干轮，所以模型不会因为对话变长而“忘记”已经做过的事。
"""
from __future__ import annotations

import json
from decimal import Decimal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from pydantic import ValidationError

from bonus_agent.auto import (DATA_SUFFIXES, MAX_FILES, Inventory, PlanDataset, TooManyFiles, build_inventory,
                              check_dataset, collect_files, sheet_ref)
from bonus_agent.extract_rules import Extraction, Question, check_quotes, prompt
from bonus_agent.llm import LLM, LLMError, image_part, parse_json
from bonus_core.data.mapping import DatasetMapping, apply_mapping
from bonus_core.data.table_detect import table_at
from bonus_core.dsl.functions import describe as describe_functions
from bonus_core.dsl.models import DatasetDef, RuleSet
from bonus_core.dsl.validator import validate
from bonus_core.engine.allocation import AdjustConflict
from bonus_core.engine.evaluator import EvalError, Evaluator, fmt
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import Result, compute
from bonus_core.engine.tree import ROOT_LEVEL, build_tree
from bonus_core.report.excel import write_excel

MAX_RESULT_CHARS = 6000
READ_ONLY = {"list_files", "read_document", "read_sheet", "view_image", "explain", "evaluate"}


class ToolError(Exception):
    """工具参数或执行出错，错误信息原样返回给模型。"""


# ---------------- 工具定义 ----------------
def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


_S = {"type": "string"}
_I = {"type": "integer"}
FIELD_SCHEMA = _obj({"column": {**_S, "description": "Excel 表头原文"},
                     "type": {"type": "string", "enum": ["money", "number", "int", "ratio", "text", "enum", "date", "id"]},
                     "unit": {"type": "string", "enum": ["元", "万元"], "description": "money 字段必填"},
                     "desc": _S}, ["column", "type"])

TOOLS: list[dict] = [
    {"name": "list_files", "description": "列出本批次所有文件；Excel 列出每个工作表及自动识别的表头。",
     "parameters": _obj({})},
    {"name": "read_document", "description": "读取 Word / PPT / 图片文件的文字和表格（图片显示为占位符，用 view_image 查看）。",
     "parameters": _obj({"file": _S, "offset": {**_I, "description": "从第几个字符开始，默认 0"}}, ["file"])},
    {"name": "view_image", "description": "查看文件中的第 index 张图片（从 1 开始），返回图片内容的逐字转写。",
     "parameters": _obj({"file": _S, "index": _I, "focus": {**_S, "description": "可选：希望重点看清的内容"}},
                        ["file", "index"])},
    {"name": "read_sheet", "description": "查看 Excel 工作表的原始内容（带 Excel 行号），用于判断表头位置、合计行、说明文字等。",
     "parameters": _obj({"file": _S, "sheet": _S, "start_row": _I, "rows": _I}, ["file", "sheet"])},
    {"name": "note",
     "description": "把读到的关键信息记在工作区（如某条规则、某个参数、文件用途、待核对事项）。笔记每轮都会附给你，"
                    "较早的对话会被裁剪，重要内容请及时记下。同一 topic 再次记录会覆盖，text 为空则删除。",
     "parameters": _obj({"topic": _S, "text": _S}, ["topic", "text"])},
    {"name": "define_dataset",
     "description": "定义（或重新定义）一张数据表。实体表写 key（每行一个对象）；明细表写 link（每行挂到某层级对象上）；"
                    "公司级表 key、link 都不写。同名字段表示关联。返回校验结果和换算后的样例行。",
     "parameters": _obj({"name": {**_S, "description": "中文表名，如 人员、销售明细"}, "file": _S, "sheet": _S,
                         "header_rows": {"type": "array", "items": _I, "description": "可选：表头所在的 Excel 行号，多行表头写多个"},
                         "key": _S, "link": _S, "desc": _S,
                         "fields": {"type": "object", "additionalProperties": FIELD_SCHEMA,
                                    "description": "字段名（英文小写加下划线） -> 定义"}},
                        ["name", "file", "sheet", "fields"])},
    {"name": "remove_dataset", "description": "删除一张已定义的数据表。", "parameters": _obj({"name": _S}, ["name"])},
    {"name": "set_rules",
     "description": "写入或修改计算规则。只给出要改的部分：tables / variables 按名字合并（值为 null 表示删除），"
                    "pool、allocation、payout 整体替换。返回静态校验结果。",
     "parameters": _obj({"tables": {"type": "object"}, "variables": {"type": "object"}, "pool": {"type": "object"},
                         "allocation": {"type": "array", "items": {"type": "object"}},
                         "payout": {"type": "array", "items": {"type": "object"}}})},
    {"name": "trial_run", "description": "用当前数据表和规则试算，返回奖金池、各层级金额分布和提示。", "parameters": _obj({})},
    {"name": "explain", "description": "查看计算链：节点 id（如 个人:E1001、全部）或变量追踪 id 的公式、代入数值后的算式和结果。",
     "parameters": _obj({"id": _S}, ["id"])},
    {"name": "evaluate", "description": "在某个节点上计算任意表达式（如 sum(net_profit)），用于核对数据和规则。需要先在 set_rules 里设定 allocation。",
     "parameters": _obj({"expr": _S, "node": {**_S, "description": "节点 id，默认 全部"}}, ["expr"])},
    {"name": "ask_question",
     "description": "记录需要财务确认的问题（不会中断工作）。按 suggested 的理解继续计算。",
     "parameters": _obj({"text": _S, "options": {"type": "array", "items": _S}, "suggested": _I, "related": _S},
                        ["text", "options"])},
    {"name": "finish", "description": "完成。要求最近一次修改之后已成功试算。",
     "parameters": _obj({"summary": {**_S, "description": "两三句话说明：什么奖金、奖金池怎么来、怎么分、发放条件"},
                         "ignored": {"type": "array", "items": _obj({"ref": _S, "reason": _S})}}, ["summary"])},
]


def openai_tools() -> list[dict]:
    return [{"type": "function", "function": t} for t in TOOLS]


# ---------------- 工作区 ----------------
@dataclass
class Step:
    tool: str
    args: dict
    result: str
    ok: bool


@dataclass
class Workspace:
    llm: LLM
    inv: Inventory
    datasets: dict[str, DatasetDef] = field(default_factory=dict)
    mapping: dict[str, DatasetMapping] = field(default_factory=dict)
    draft: dict = field(default_factory=dict)              # 规则草稿：tables / variables / pool / allocation / payout
    questions: list[Question] = field(default_factory=list)
    transcripts: dict[str, str] = field(default_factory=dict)
    result: Result | None = None
    dirty: bool = True                                     # 最近一次修改后是否还没试算成功
    summary: str = ""
    ignored: list[dict] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)
    done: bool = False
    budget: str = ""

    @property
    def data_files(self) -> list[Path]:
        return [f for f in self.inv.files if f.suffix.lower() in DATA_SUFFIXES]

    # ---- 文件 ----
    def _file(self, name: str) -> Path:
        hit = [f for f in self.inv.files if f.name == name or str(f) == name]
        if not hit:
            raise ToolError(f"没有文件「{name}」，可用文件：{[f.name for f in self.inv.files]}")
        return hit[0]

    def list_files(self) -> str:
        lines = []
        for f in self.inv.files:
            if f.suffix.lower() in DATA_SUFFIXES:
                lines.append(f"- {f.name}（Excel）")
                for ref, body in self.inv.sheet_text.items():
                    if ref.startswith(f.name + "/"):
                        st = self.inv.sheet(ref)
                        if st is None:
                            lines.append(f"  · 工作表「{ref.split('/', 1)[1]}」：未识别出规整表格（{body.count(chr(10)) + 1} 行），可用 read_sheet 查看")
                        else:
                            lines.append(f"  · 工作表「{st.sheet}」：表头在第 {st.header_row} 行，{len(st.rows)} 行数据"
                                         + (f"，{len(st.totals)} 行合计" if st.totals else "")
                                         + f"；表头 {[h for h in st.headers if h]}")
            else:
                d = next(d for d in self.inv.docs if d.name == f.name)
                imgs = sum(1 for b in d.blocks if b.kind == "image")
                lines.append(f"- {f.name}（{f.suffix[1:]}，约 {len(d.full_text())} 字" + (f"，{imgs} 张图片" if imgs else "") + "）")
        return "\n".join(lines)

    def read_document(self, file: str, offset: int = 0) -> str:
        f = self._file(file)
        d = next((d for d in self.inv.docs if d.name == f.name), None)
        if d is None:
            raise ToolError(f"{file} 是 Excel，请用 read_sheet")
        parts, k = [], 0
        for b in d.blocks:
            if b.kind == "image":
                k += 1
                t = self.transcripts.get(f"{d.name}#{k}")
                parts.append(f"{b.label()} 图片 #{k}" + (f"（已转写）\n{t}" if t else "（用 view_image 查看）"))
            elif b.text:
                parts.append(f"{b.label()}\n{b.text}")
        text = "\n\n".join(parts)
        chunk = text[offset:offset + MAX_RESULT_CHARS - 200]
        more = len(text) - offset - len(chunk)
        return chunk + (f"\n\n……还有 {more} 字，用 offset={offset + len(chunk)} 继续读取" if more > 0 else "")

    def view_image(self, file: str, index: int, focus: str = "") -> str:
        f = self._file(file)
        d = next((d for d in self.inv.docs if d.name == f.name), None)
        imgs = [b for b in d.blocks if b.kind == "image"] if d else []
        if not 1 <= index <= len(imgs):
            raise ToolError(f"{file} 中有 {len(imgs)} 张图片，index 应在 1~{len(imgs)}")
        b = imgs[index - 1]
        ask = "请转写这张图片。" + (f"重点看清：{focus}" if focus else "")
        text = self.llm.chat([{"role": "system", "content": prompt("transcribe")},
                              {"role": "user", "content": [image_part(b.image, b.mime), {"type": "text", "text": ask}]}],
                             temperature=0).strip()
        self.transcripts[f"{d.name}#{index}"] = text
        return text

    def read_sheet(self, file: str, sheet: str, start_row: int = 1, rows: int = 40) -> str:
        f = self._file(file)
        if f"{f.name}/{sheet}" not in self.inv.sheet_text:
            sheets = [r.split("/", 1)[1] for r in self.inv.sheet_text if r.startswith(f.name + "/")]
            raise ToolError(f"{f.name} 中没有工作表「{sheet}」，可选：{sheets}")
        import openpyxl
        ws = openpyxl.load_workbook(f, data_only=True, read_only=True)[sheet]
        start_row, rows = max(1, start_row), max(1, min(rows, 80))
        out, total = [], 0
        for i, r in enumerate(ws.iter_rows(values_only=True), start=1):
            total = i
            if start_row <= i < start_row + rows:
                cells = ["" if v is None else str(v).replace("\n", " ")[:30] for v in r]
                while cells and cells[-1] == "":
                    cells.pop()
                out.append(f"第{i}行 | " + " | ".join(cells))
        st = self.inv.sheet(f"{f.name}/{sheet}")
        head = (f"工作表共 {total} 行" + (f"，自动识别表头在第 {st.header_row} 行、{len(st.rows)} 行数据" if st else "，未自动识别出表格")
                + f"；以下为第 {start_row}~{min(start_row + rows - 1, total)} 行")
        end = start_row + rows - 1
        tail = (f"\n（还有第 {end + 1}~{total} 行，用 start_row={end + 1} 继续；数据表不必读完，define_dataset 会读取全部行）"
                if end < total else "\n（已到末尾）")
        return head + "\n" + "\n".join(out) + tail

    def note(self, topic: str, text: str) -> str:
        if text.strip():
            self.notes[topic] = text.strip()
        else:
            self.notes.pop(topic, None)
        return f"已记录「{topic}」（共 {len(self.notes)} 条笔记，每轮都会附在工作区状态里）"

    # ---- 数据表 ----
    def define_dataset(self, name: str, file: str, sheet: str, fields: dict, key: str | None = None,
                       link: str | None = None, desc: str = "", header_rows: list[int] | None = None) -> str:
        f = self._file(file)
        try:
            st = table_at(f, sheet, header_rows) if header_rows else self.inv.sheet(f"{f.name}/{sheet}")
        except (KeyError, ValueError) as e:
            raise ToolError(str(e)) from None
        if st is None:
            raise ToolError(f"{f.name}/{sheet} 没有自动识别出表格，请先 read_sheet 查看，再用 header_rows 指定表头行")
        try:
            pd = PlanDataset(sheet=sheet, key=key or None, link=link or None, desc=desc, fields=fields)
        except ValidationError as e:
            raise ToolError(f"参数格式不对：{e}") from None
        types = {fn: (fd.type, n) for n, d in self.datasets.items() if n != name for fn, fd in d.fields.items()}
        d, m, warnings, errors = check_dataset(name, pd, st, types, header_rows)
        if errors:
            raise ToolError("未保存，请修正：\n" + "\n".join(f"- {e}" for e in errors))
        self.datasets[name], self.mapping[name] = d, m
        self.dirty = True
        kind = f"实体表，主键 {d.key}" if d.key else f"明细表，关联 {d.link}" if d.link else "公司级表"
        sample = []
        from bonus_core.data.table import convert
        for _, vals in st.rows[:3]:
            sample.append({fn: fmt(convert(vals[st.headers.index(c)], d.fields[fn])) for fn, c in m.columns.items()})
        lines = [f"已保存「{name}」（{kind}），{len(st.rows)} 行" + (f"，另有 {len(st.totals)} 行合计（不计入）" if st.totals else "")]
        lines += [f"注意：{w}" for w in warnings]
        lines.append("前 3 行（金额已换算为元）：" + json.dumps(sample, ensure_ascii=False))
        return "\n".join(lines)

    def remove_dataset(self, name: str) -> str:
        if name not in self.datasets:
            raise ToolError(f"没有数据表「{name}」")
        del self.datasets[name], self.mapping[name]
        self.dirty = True
        return f"已删除「{name}」"

    # ---- 规则 ----
    def set_rules(self, **parts) -> str:
        draft = json.loads(json.dumps(self.draft))
        for k in ("tables", "variables"):
            for n, v in (parts.get(k) or {}).items():
                if v is None:
                    draft.setdefault(k, {}).pop(n, None)
                else:
                    draft.setdefault(k, {})[n] = v
        for k in ("pool", "allocation", "payout"):
            if parts.get(k) is not None:
                draft[k] = parts[k]
        try:
            rs = self._ruleset(draft)
        except (ValidationError, ValueError) as e:
            raise ToolError(f"规则格式不对，未保存：{str(e)[:2500]}") from None
        self.draft, self.dirty = draft, True
        issues = validate(rs)
        if "pool" not in draft:
            issues = [i for i in issues if i.where != "pool"]
        blocking = [str(i) for i in issues if i.severity == "blocking"]
        warns = [str(i) for i in issues if i.severity == "warning"]
        head = "已保存。" + ("" if "pool" in draft else "（尚未设定 pool）")
        if blocking:
            return head + "静态校验发现问题，需要修正：\n" + "\n".join(f"- {e}" for e in blocking)
        return head + "静态校验通过。" + ("\n提示：\n" + "\n".join(f"- {w}" for w in warns) if warns else "")

    def _ruleset(self, draft: dict | None = None) -> RuleSet:
        draft = self.draft if draft is None else draft
        ext = Extraction.model_validate({"pool": {"expr": "0"}, "allocation": [], **draft})
        return RuleSet(name=self.summary[:30] or "智能体草稿", version="agent-draft", datasets=self.datasets,
                       **ext.model_dump(exclude={"questions"}))

    def _data(self, rs: RuleSet):
        data, issues = apply_mapping(rs, self.data_files, self.mapping)
        blocking = [i for i in issues if i.severity == "blocking"]
        if blocking:
            raise ToolError("数据问题：\n" + "\n".join(f"- {i}" for i in blocking[:8]))
        return data, issues

    def trial_run(self) -> str:
        if not self.datasets:
            raise ToolError("还没有定义数据表")
        if "pool" not in self.draft or not self.draft.get("allocation"):
            raise ToolError("规则还缺 pool 或 allocation")
        rs = self._ruleset()
        data, issues = self._data(rs)
        try:
            res = compute(rs, data)
        except EngineError as e:
            raise ToolError("试算失败：\n" + "\n".join(f"- {i}" for i in e.issues[:8])) from None
        except (EvalError, AdjustConflict, ValueError) as e:
            raise ToolError(f"试算失败：{e}") from None
        self.result, self.dirty = res, False
        lines = [f"试算成功。奖金池 {fmt(Decimal(res.pool))} 元（精确值 {res.pool_exact}）"]
        for lv in res.levels:
            ns = sorted((n for n in res.nodes.values() if n.level == lv), key=lambda n: -float(n.final))
            total = sum(float(n.final) for n in ns)
            zero = sum(1 for n in ns if float(n.final) == 0)
            top = "，".join(f"{n.id}（{n.label}）{fmt(Decimal(n.final))}" for n in ns[:8])
            lines.append(f"「{lv}」{len(ns)} 个，合计 {total:,.2f}，为 0 的 {zero} 个；最多的：{top}"
                         + ("……" if len(ns) > 8 else "") + f"；最少的：{ns[-1].id} {fmt(Decimal(ns[-1].final))}")
        held = sum(float(p.held) for p in res.payouts.values())
        if res.payouts:
            lines.append(f"发放：本期暂缓合计 {held:,.2f}")
        warns = [str(i) for i in list(issues) + list(res.issues) if i.severity == "warning"]
        if warns:
            lines.append("数据提示：\n" + "\n".join(f"- {w}" for w in warns[:8]))
        return "\n".join(lines)

    def explain(self, id: str) -> str:
        if self.result is None:
            raise ToolError("先 trial_run")
        tr = self.result.traces
        keys = [k for k in tr if k == id or k.startswith(id + "/")] or [k for k in tr if id in k]
        if not keys:
            raise ToolError(f"没有找到「{id}」的计算链，节点 id 形如 {next(iter(self.result.nodes), '')}")
        out = []
        for k in keys[:12]:
            t = tr[k]
            out.append(f"{t.id}：{t.expr}\n  = {t.rendered}\n  = {t.value}" + (f"\n  明细行：{', '.join(t.rows[:5])}" if t.rows else ""))
        if id in self.result.nodes:
            n = self.result.nodes[id]
            out.append(f"{id} 最终金额 {fmt(Decimal(n.final))}（权重 {n.weight}，上级 {n.parent}）")
        return "\n".join(out)

    def evaluate(self, expr: str, node: str = ROOT_LEVEL) -> str:
        if not self.draft.get("allocation"):
            raise ToolError("需要先用 set_rules 设定 allocation（pool 可以先写 0）")
        rs = self._ruleset()
        data, _ = self._data(rs)
        root, by_level, issues = build_tree(rs, data)
        blocking = [i for i in issues if i.severity == "blocking"]
        if blocking:
            raise ToolError("建树失败：" + "；".join(str(i) for i in blocking[:5]))
        nodes = {n.id: n for n in root.walk()}
        if node not in nodes:
            raise ToolError(f"没有节点「{node}」，例：{list(nodes)[:5]}")
        ev = Evaluator(rs, root, by_level)
        try:
            v = ev.evaluate(expr, nodes[node], "probe", "probe")
        except (EvalError, ValueError) as e:
            raise ToolError(f"计算失败：{e}") from None
        return f"{expr} = {ev.traces['probe'].rendered} = {fmt(v)}"

    # ---- 问题与完成 ----
    def ask_question(self, text: str, options: list[str], suggested: int | None = 0, related: str = "") -> str:
        q = Question(id=f"q{len(self.questions) + 1}", text=text, options=options, suggested=suggested, related=related)
        self.questions.append(q)
        return f"已记录问题 {q.id}，请按「{options[suggested] if suggested is not None and 0 <= suggested < len(options) else '最可能的理解'}」继续"

    def finish(self, summary: str, ignored: list[dict] | None = None) -> str:
        if self.dirty or self.result is None:
            raise ToolError("最近一次修改之后还没有成功试算，请先 trial_run")
        self.summary, self.ignored, self.done = summary, ignored or [], True
        return "已完成"

    def state(self) -> str:
        """每轮附在请求末尾的工作区摘要。"""
        lines = ["【当前工作区状态】" + (f"（{self.budget}）" if self.budget else "")]
        if self.notes:
            lines.append("- 笔记：")
            lines += [f"  · {k}：{v}" for k, v in self.notes.items()]
        if self.datasets:
            for n, d in self.datasets.items():
                m = self.mapping[n]
                kind = f"key={d.key}" if d.key else f"link={d.link}" if d.link else "公司级"
                lines.append(f"- 数据表「{n}」← {m.file}/{m.sheet}（{kind}）字段：{', '.join(d.fields)}")
        else:
            lines.append("- 尚未定义数据表")
        if self.draft:
            lines.append("- 规则：" + json.dumps(self.draft, ensure_ascii=False)[:3000])
        else:
            lines.append("- 尚未写入规则")
        if self.questions:
            lines.append("- 已记录问题：" + "；".join(q.text for q in self.questions))
        if self.transcripts:
            lines.append(f"- 已查看图片：{', '.join(self.transcripts)}")
        lines.append("- 试算：" + ("最近修改后尚未试算" if self.dirty else f"已试算，奖金池 {fmt(Decimal(self.result.pool))}"))
        return "\n".join(lines)


# ---------------- 主循环 ----------------
def system_prompt() -> str:
    dsl = prompt("extract_rules")
    dsl = dsl[dsl.index("# 规则 DSL") + len("# 规则 DSL"):dsl.index("# 数据字典")]
    dsl = dsl.replace("数据字典（datasets）已固定，见下文，你**只能引用其中的字段名**，不能自造字段。你需要输出：",
                      "表达式只能引用你用 define_dataset 定义过的字段名。规则包括：")
    dsl = dsl.replace("{functions}", describe_functions())
    return prompt("agent").replace("{dsl}", dsl.strip())


JSON_PROTOCOL = """
# 回复格式（本模型不使用原生工具调用）
每次只输出一个 JSON 对象，不要输出其他文字：{"thought": "简短的思考", "tool": "工具名", "args": {参数}}
可用工具（JSON Schema）：
"""


@dataclass
class AgentResult:
    ws: Workspace
    steps: list[Step]
    ruleset: RuleSet | None
    result: Result | None
    error: str = ""
    outputs: dict[str, Path] = field(default_factory=dict)
    quote_hits: int = 0
    quote_total: int = 0

    @property
    def ok(self) -> bool:
        return self.ws.done and self.result is not None

    @property
    def questions(self) -> list[Question]:
        return self.ws.questions


def _trim(history: list[dict], max_chars: int) -> list[dict]:
    """从最早的一轮开始丢弃，直到总长度在限制内；保证保留的历史以 assistant 消息开头（工具结果不落单）。"""
    def size(ms):
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in ms)
    starts = [i for i, m in enumerate(history) if m["role"] == "assistant"]
    for i in starts:
        if size(history[i:]) <= max_chars:
            return history[i:]
    return history[starts[-1]:] if starts else history


def _answers_text(answers: list[dict] | None) -> str:
    done = [a for a in answers or [] if a.get("answer") not in (None, "")]
    if not done:
        return ""
    lines = ["\n财务已确认的答复（必须按此处理，不要再就这些问题提问）："]
    for a in done:
        ans = a["answer"]
        if isinstance(ans, int) and 0 <= ans < len(a.get("options") or []):
            ans = a["options"][ans]
        lines.append(f"- {a.get('text', '')} → {ans}")
    return "\n".join(lines)


def run_agent(llm: LLM, paths: list[str | Path], out_dir: str | Path | None = None, *,
              answers: list[dict] | None = None, max_steps: int = 80, max_history_chars: int = 60000,
              max_files: int = MAX_FILES, max_tokens: int | None = 1_000_000,
              on_step: Callable[[int, Step], None] | None = None) -> AgentResult:
    """max_steps 和 max_tokens 都是安全上限：模型调用 finish 就结束，正常不会用满。"""
    try:
        files = collect_files(paths, exclude=[out_dir] if out_dir else [], max_files=max_files)
    except TooManyFiles as e:
        raise ToolError(str(e)) from None
    if not files:
        raise ToolError("没有找到可处理的文件（支持 .docx .pptx .xlsx .xlsm .png .jpg .txt .md .json .csv）")
    ws = Workspace(llm, build_inventory(files))
    handlers = {t["name"]: getattr(ws, t["name"]) for t in TOOLS}
    task = ("请完成本批次的奖金计算。文件清单：\n" + ws.list_files() + _answers_text(answers)
            + "\n\n完成后调用 finish。")
    mode = llm.tool_mode
    sys = system_prompt()
    history: list[dict] = []
    steps: list[Step] = []
    error = ""
    nudges = 0
    seen: dict[str, int] = {}          # 只读工具的调用 -> 第几步，用来发现原地打转
    repeats = 0
    for turn in range(max_steps):
        used = llm.usage.prompt_tokens + llm.usage.completion_tokens
        if max_tokens and used >= max_tokens:
            error = f"tokens 用量 {used} 达到上限 {max_tokens}，已停止"
            break
        ws.budget = f"已用 {len(steps)} 步，上限 {max_steps} 步" + (f"；tokens 已用 {used}，上限 {max_tokens}" if max_tokens else "")
        tail = [{"role": "user", "content": ws.state()}]
        if mode == "json":
            msgs = [{"role": "system", "content": sys + JSON_PROTOCOL + json.dumps(TOOLS, ensure_ascii=False)},
                    {"role": "user", "content": task}] + _trim(history, max_history_chars) + tail
            try:
                text = llm.chat(msgs, json_out=True, temperature=0.1)
            except LLMError as e:
                error = str(e)
                break
            history.append({"role": "assistant", "content": text})
            try:
                act = parse_json(text)
                calls = [{"id": "", "name": act["tool"], "arguments": json.dumps(act.get("args") or {}, ensure_ascii=False)}]
            except (LLMError, KeyError, TypeError, ValueError):
                history.append({"role": "user", "content": '格式不对：请只输出 {"thought": ..., "tool": ..., "args": {...}}'})
                continue
        else:
            msgs = [{"role": "system", "content": sys}, {"role": "user", "content": task}] \
                + _trim(history, max_history_chars) + tail
            try:
                reply = llm.chat_tools(msgs, openai_tools())
            except LLMError as e:
                if turn == 0:  # 模型或服务不支持原生工具调用，改用 JSON 协议
                    mode = "json"
                    continue
                error = str(e)
                break
            calls = reply["tool_calls"]
            history.append({"role": "assistant", "content": reply["content"],
                            **({"tool_calls": [{"id": c["id"], "type": "function",
                                                "function": {"name": c["name"], "arguments": c["arguments"]}}
                                               for c in calls]} if calls else {})})
            if not calls:
                nudges += 1
                if nudges > 3:
                    error = "模型多次没有调用工具"
                    break
                history.append({"role": "user", "content": "请调用工具继续工作；全部完成后调用 finish。"})
                continue
        for c in calls:
            args = {}
            sig = f"{c['name']}:{c['arguments']}"
            if c["name"] in READ_ONLY and sig in seen:
                repeats += 1
                result = (f"这次调用与第 {seen[sig]} 步完全相同，内容没有变化，已跳过。不要重复调用；"
                          f"需要更多内容请换参数（如 start_row、offset），否则根据已有信息进行下一步（定义数据表、写规则、试算）。")
                steps.append(Step(c["name"], {}, result, False))
                if on_step:
                    on_step(len(steps), steps[-1])
                history.append({"role": "user", "content": f"【{c['name']} 的结果】\n{result}"} if mode == "json"
                               else {"role": "tool", "tool_call_id": c["id"], "content": result})
                continue
            try:
                args = json.loads(c["arguments"] or "{}")
                fn = handlers.get(c["name"])
                if not isinstance(args, dict):
                    raise ToolError("参数必须是 JSON 对象")
                if fn is None:
                    raise ToolError(f"没有工具 {c['name']}，可用：{list(handlers)}")
                result, ok = str(fn(**args)), True
            except ToolError as e:
                result, ok = f"错误：{e}", False
            except json.JSONDecodeError as e:
                result, ok = f"错误：参数不是合法 JSON：{e}", False
            except TypeError as e:
                result, ok = f"错误：参数不对：{e}", False
            if len(result) > MAX_RESULT_CHARS:
                result = result[:MAX_RESULT_CHARS] + "……（已截断）"
            step = Step(c["name"], args if isinstance(args, dict) else {}, result, ok)
            if c["name"] in READ_ONLY and ok:
                seen[sig] = len(steps) + 1
            elif c["name"] not in READ_ONLY:
                seen.clear()   # 工作区变了，重新读取是合理的
            steps.append(step)
            if on_step:
                on_step(len(steps), step)
            if mode == "json":
                history.append({"role": "user", "content": f"【{c['name']} 的结果】\n{result}"})
            else:
                history.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        if ws.done:
            break
        if repeats >= 6:
            error = "智能体反复重复相同的调用，已停止"
            break
    else:
        error = f"达到最大步数 {max_steps} 仍未完成"

    rs = ws._ruleset() if ws.datasets and ws.draft.get("allocation") else None
    ar = AgentResult(ws, steps, rs if ws.result is not None else None, ws.result if not ws.dirty else None, error)
    if rs is not None:
        ar.quote_hits, ar.quote_total, _ = check_quotes(rs, ws.inv.docs)
    if out_dir is not None:
        ar.outputs = write_agent_outputs(ar, Path(out_dir), answers)
    return ar


# ---------------- 输出 ----------------
def write_agent_outputs(ar: AgentResult, out: Path, answers: list[dict] | None) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    ws, paths = ar.ws, {}
    if ws.mapping:
        paths["mapping"] = out / "mapping.yaml"
        paths["mapping"].write_text(yaml.safe_dump({k: v.model_dump(exclude_none=True, exclude_defaults=True)
                                                    for k, v in ws.mapping.items()}, allow_unicode=True, sort_keys=False),
                                    encoding="utf-8")
    if ar.ruleset is not None:
        paths["rules"] = out / "rules.yaml"
        paths["rules"].write_text(ar.ruleset.dump_yaml(), encoding="utf-8")
    prev = {a.get("text"): a.get("answer") for a in answers or []}
    qrows = [{"id": q.id, "text": q.text, "options": q.options, "suggested": q.suggested, "related": q.related,
              "answer": prev.get(q.text)} for q in ws.questions]
    asked = {r["text"] for r in qrows}
    qrows += [dict(a) for a in answers or [] if a.get("text") not in asked]
    paths["questions"] = out / "questions.yaml"
    paths["questions"].write_text(
        "# 财务答复：在 answer 里填选项序号（从 0 开始）或直接写文字，然后用 --answers 这个文件重跑\n"
        + yaml.safe_dump(qrows, allow_unicode=True, sort_keys=False), encoding="utf-8")
    paths["log"] = out / "agent_log.json"
    paths["log"].write_text(json.dumps({"summary": ws.summary, "error": ar.error, "transcripts": ws.transcripts,
                                        "steps": [vars(s) for s in ar.steps]}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    if ar.result is not None and ar.ruleset is not None:
        paths["excel"] = out / "result.xlsx"
        write_excel(ar.ruleset, ar.result, paths["excel"], extra_sheets=_sheets(ar, qrows))
    return paths


def _sheets(ar: AgentResult, qrows: list[dict]):
    ws = ar.ws
    plan = [["状态", ("智能体草稿：规则和数据映射由大模型生成，尚未经财务确认。请先处理「待确认问题」，确认后重算。"
                      if ar.ok else f"未完成：{ar.error}")],
            ["方案概述", ws.summary],
            ["规则来源", "、".join(d.name for d in ws.inv.docs)]]
    for n, d in ws.datasets.items():
        m = ws.mapping[n]
        kind = f"实体表，主键 {d.key}" if d.key else f"明细表，关联 {d.link}" if d.link else "公司级表"
        plan.append([f"数据表：{n}", f"{m.file}/{m.sheet}（{kind}）"])
        plan += [[f"  {f}", f"← 「{c}」 {d.fields[f].type}" + (f"，{d.fields[f].unit}" if d.fields[f].unit else "")]
                 for f, c in m.columns.items()]
    plan += [["已忽略", f"{i.get('ref')}：{i.get('reason', '')}"] for i in ws.ignored]
    plan.append(["原文引用命中", f"{ar.quote_hits}/{ar.quote_total}"])
    q = [[r["id"], r["text"], " / ".join(r.get("options") or []),
          (r["options"][r["suggested"]] if isinstance(r.get("suggested"), int)
           and 0 <= r["suggested"] < len(r.get("options") or []) else ""), "" if r.get("answer") is None else str(r["answer"])]
         for r in qrows]
    log = [[i, s.tool, json.dumps(s.args, ensure_ascii=False)[:300], ("" if s.ok else "失败：") + s.result[:500]]
           for i, s in enumerate(ar.steps, start=1)]
    return [("待确认问题", ["编号", "问题", "选项", "本次计算采用", "财务答复"], q, {"问题": 60, "选项": 40, "本次计算采用": 20}),
            ("智能体规划", ["项目", "内容"], plan, {"项目": 22, "内容": 100}),
            ("智能体工作记录", ["步骤", "工具", "参数", "结果"], log, {"参数": 60, "结果": 90})]
