"""规则抽取：文档 -> 规则 DSL 草稿 + 问题卡。

流程：
1. 解析文档；图片先让多模态模型“只转写不理解”，转成文字/表格；
2. 把全部文字连同 DSL 说明、数据字典交给模型，输出 JSON；
3. 程序校验（结构、语法、名字、原文引用是否真实存在），有错误就把错误反馈给模型修正，最多 2 轮；
4. 返回草稿、问题卡和校验结果，交财务逐条确认（H1）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from bonus_agent.llm import LLM, LLMError, Usage, image_part, parse_json
from bonus_agent.parsing.docs import Block, ParsedDoc, parse_any
from bonus_core.dsl.functions import describe as describe_functions
from bonus_core.dsl.models import AllocationLevel, DatasetDef, PayoutCondition, Pool, RuleSet, Table, Variable
from bonus_core.dsl.validator import validate
from bonus_core.engine.issues import Issue

PROMPTS = Path(__file__).parent / "prompts"


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


class Question(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = ""
    text: str
    options: list[str] = []
    suggested: int | None = None
    related: str = ""


class Extraction(BaseModel):
    """模型需要输出的部分（数据字典由调用方固定提供）。"""
    model_config = ConfigDict(extra="forbid")
    tables: dict[str, Table] = {}
    variables: dict[str, Variable] = {}
    pool: Pool
    allocation: list[AllocationLevel]
    payout: list[PayoutCondition] = []
    questions: list[Question] = []


EXAMPLE = {
    "tables": {"bonus_tier": {"kind": "tier", "mode": "whole", "steps": [[0, 0], [5000000, 0.08]],
                              "desc": "回款达 500 万提 8%",
                              "sources": [{"doc": "示例.docx", "page": None, "quote": "回款额达到500万元的，按8%计提"}]}},
    "variables": {"total_collected": {"scope": "global", "expr": "sum(collected)", "desc": "部门回款合计"}},
    "pool": {"expr": "tier(bonus_tier, total_collected)"},
    "allocation": [{"level": "个人", "key": "employee_id", "parent_key": None, "weight": "share(seniority)"}],
    "payout": [],
    "questions": [{"id": "q1", "text": "8% 是全额累进还是只对超过 500 万的部分计提？",
                   "options": ["全额累进", "超额累进"], "suggested": 0, "related": "tables.bonus_tier"}],
}


@dataclass
class ExtractResult:
    model: str
    ruleset: RuleSet | None
    questions: list[Question]
    issues: list[Issue]
    raw_outputs: list[str]
    transcripts: list[str]
    attempts: int
    usage: Usage
    quote_hits: int = 0
    quote_total: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.ruleset is not None and not any(i.severity == "blocking" for i in self.issues)


def data_dictionary(datasets: dict[str, DatasetDef]) -> str:
    lines = []
    for n, d in datasets.items():
        kind = f"实体表，主键 {d.key}" if d.key else f"明细表，每行通过 {d.link} 关联"
        lines.append(f"## {n}（{kind}）")
        for f, fd in d.fields.items():
            extra = f"，取值 {fd.values}" if fd.values else ""
            unit = f"，单位已统一为元" if fd.type == "money" else ""
            lines.append(f"- {f}: {fd.type}，{fd.desc}{extra}{unit}")
    return "\n".join(lines)


def system_prompt(datasets: dict[str, DatasetDef]) -> str:
    return (prompt("extract_rules")
            .replace("{functions}", describe_functions())
            .replace("{data_dictionary}", data_dictionary(datasets))
            .replace("{example}", json.dumps(EXAMPLE, ensure_ascii=False, indent=1)))


def transcribe(llm: LLM, docs: list[ParsedDoc]) -> list[str]:
    """把图片块转写为文字块（原地替换），返回转写文本列表。"""
    out = []
    for d in docs:
        for i, b in enumerate(d.blocks):
            if b.kind != "image" or not b.image:
                continue
            text = llm.chat([
                {"role": "system", "content": prompt("transcribe")},
                {"role": "user", "content": [image_part(b.image, b.mime), {"type": "text", "text": "请转写这张图片。"}]},
            ], temperature=0)
            d.blocks[i] = Block(b.doc, b.page, "transcript", f"【图片转写】\n{text.strip()}", b.image, b.mime)
            out.append(text)
    return out


def render_docs(docs: list[ParsedDoc]) -> str:
    parts = []
    for d in docs:
        parts.append(f"===== 文档：{d.name} =====")
        for b in d.blocks:
            if b.text:
                parts.append(f"{b.label()}\n{b.text}")
    return "\n\n".join(parts)


def _norm(s: str) -> str:
    return re.sub(r"[\s，,。．.：:；;、“”\"'（）()【】\[\]|-]", "", s)


def check_quotes(rs: RuleSet, docs: list[ParsedDoc]) -> tuple[int, int, list[Issue]]:
    """原文引用必须真实出现在文档（含图片转写）里，否则视为幻觉。"""
    corpus = {d.name: _norm(d.full_text()) for d in docs}
    everything = "".join(corpus.values())
    hits = total = 0
    issues = []
    items = [(f"tables.{k}", v) for k, v in rs.tables.items()] + [(f"variables.{k}", v) for k, v in rs.variables.items()]
    items += [("pool", rs.pool)] + [(f"allocation.{a.level}", a) for a in rs.allocation] + [(f"payout.{p.id}", p) for p in rs.payout]
    for where, item in items:
        for s in item.sources:
            q = _norm(s.quote)
            if not q:
                continue
            total += 1
            if q in corpus.get(s.doc, everything) or q in everything:
                hits += 1
            else:
                issues.append(Issue.warn("quote_not_found", f"原文引用在文档中找不到：{s.quote[:40]}", where))
    return hits, total, issues


def extract(llm: LLM, files: list[str | Path], datasets: dict[str, DatasetDef], *, name: str = "草稿",
            version: str = "draft", max_repairs: int = 2, docs: list[ParsedDoc] | None = None,
            direct_images: bool = False) -> ExtractResult:
    """direct_images=True 时不做单独转写，直接把图片和文字一起交给模型（用于对比两种方式）。"""
    docs = docs if docs is not None else [parse_any(f) for f in files]
    start_usage = Usage(**vars(llm.usage))
    transcripts = [] if direct_images else transcribe(llm, docs)
    user_content: list = [{"type": "text", "text": "以下是奖金规则文档的内容：\n\n" + render_docs(docs)}]
    if direct_images:
        for d in docs:
            for b in d.blocks:
                if b.kind == "image" and b.image:
                    user_content += [{"type": "text", "text": f"{b.label()}（图片）"}, image_part(b.image, b.mime)]
    messages = [{"role": "system", "content": system_prompt(datasets)}, {"role": "user", "content": user_content}]
    schema = Extraction.model_json_schema()
    raw_outputs: list[str] = []
    rs, questions, issues, err = None, [], [], ""
    attempts = 0
    for attempts in range(1, max_repairs + 2):
        try:
            text = llm.chat(messages, schema=schema, json_out=True, temperature=0.1)
        except LLMError as e:
            err = str(e)
            break
        raw_outputs.append(text)
        errors: list[str] = []
        try:
            obj = parse_json(text)
            ext = Extraction.model_validate(obj)
            rs = RuleSet(name=name, version=version, datasets=datasets, **ext.model_dump(exclude={"questions"}))
            questions = ext.questions
            issues = validate(rs)
            errors = [str(i) for i in issues if i.severity == "blocking"]
        except (LLMError, ValidationError, ValueError) as e:
            rs, errors = None, [f"输出不符合格式：{e}"[:3000]]
        if not errors:
            break
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": prompt("repair").replace("{errors}", "\n".join(f"- {e}" for e in errors))},
        ]
    hits = total = 0
    if rs is not None:
        hits, total, qi = check_quotes(rs, docs)
        issues += qi
    used = Usage(llm.usage.prompt_tokens - start_usage.prompt_tokens,
                 llm.usage.completion_tokens - start_usage.completion_tokens,
                 llm.usage.latency_s - start_usage.latency_s, llm.usage.calls - start_usage.calls)
    if rs is None and not err:
        err = "多轮修正后仍未得到可用的规则"
    return ExtractResult(llm.name, rs, questions, issues, raw_outputs, transcripts, attempts, used, hits, total, err)


def load_datasets(path: str | Path) -> dict[str, DatasetDef]:
    """数据字典可以来自单独的 YAML，也可以直接复用一份规则集里的 datasets。"""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = raw.get("datasets", raw)
    return {k: DatasetDef.model_validate(v) for k, v in raw.items()}
