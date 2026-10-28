"""候选模型对比评测。

    # 真实调用（需要设置 DASHSCOPE_API_KEY）
    python -m evals.run_eval --repeats 3
    # 全部开源模型 + 商业版上限参考
    python -m evals.run_eval --models open,reference --repeats 3
    # 大模型主导的智能体：从原始文件开始，自己决定步骤
    python -m evals.run_eval --modes agent --repeats 3
    # 固定流程的全自动模式（对比用）
    python -m evals.run_eval --modes auto --repeats 3
    # 只测其中两个模型、对比“先转写再抽取”和“直接看图”两种方式
    python -m evals.run_eval --models qwen3-vl-32b,qwen3-vl-8b --modes two_step,direct
    # 不调用模型，用标准答案演练整条评测流程
    python -m evals.run_eval --fake

每次运行会在 evals/reports/ 下生成 Markdown 报告、CSV 明细和每次调用的原始输出。
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from bonus_agent.agent import run_agent
from bonus_agent.auto import run_auto
from bonus_agent.extract_rules import ExtractResult, extract
from bonus_agent.llm import LLM, FakeLLM, LLMError, load_config, resolve_models
from bonus_agent.parsing.docs import parse_any
from bonus_core.data.mapping import apply_mapping, load_mapping
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.run import compute
from evals.scoring import compare_results, numeric_score, run_check

ROOT = Path(__file__).parent
CASES = ROOT / "cases"


@dataclass
class Case:
    name: str
    dir: Path
    spec: dict
    reference: RuleSet
    data: dict
    ref_result: object

    @property
    def docs(self) -> list[Path]:
        return [self.dir / d for d in self.spec["docs"]]

    @property
    def data_files(self) -> list[Path]:
        return [self.dir / d for d in self.spec["data"]]


def load_case(d: Path) -> Case:
    spec = yaml.safe_load((d / "case.yaml").read_text(encoding="utf-8"))
    ref = RuleSet.load(d / spec["reference_rules"])
    missing = [p for p in spec["docs"] + spec["data"] if not (d / p).exists()]
    if missing:
        raise RuntimeError(f"样例 {d.name} 缺少文件 {missing}，请先运行 {d / 'make_case.py'}")
    data, issues = apply_mapping(ref, [d / p for p in spec["data"]], load_mapping(d / spec["mapping"]))
    if any(i.severity == "blocking" for i in issues):
        raise RuntimeError(f"样例 {d.name} 的数据有阻断问题：{issues}")
    return Case(d.name, d, spec, ref, data, compute(ref, data))


def fake_plan(case: Case) -> dict:
    """标准答案对应的统筹规划结果（演练 auto 模式用）。"""
    mapping = load_mapping(case.dir / case.spec["mapping"])
    default_file = Path(case.spec["data"][0]).name
    datasets = {}
    for n, d in case.reference.datasets.items():
        m = mapping[n]
        datasets[n] = {"sheet": f"{m.file or default_file}/{m.sheet}", "key": d.key, "link": d.link, "desc": d.desc,
                       "fields": {f: {"column": m.columns[f], "type": fd.type, "unit": fd.unit, "desc": fd.desc}
                                  for f, fd in d.fields.items() if f in m.columns}}
    return {"summary": case.reference.name, "rule_files": case.spec["docs"], "rule_sheets": [],
            "datasets": datasets,
            "hierarchy": [{"level": lv.level, "key": lv.key, "parent_key": lv.parent_key} for lv in case.reference.allocation],
            "ignored": [], "questions": []}


def fake_agent_script(case: Case) -> list:
    """标准答案对应的一串工具调用（演练 agent 模式用）。"""
    ref, plan = case.reference, fake_plan(case)
    script = [{"tool": "list_files"}]
    script += [{"tool": "read_document", "args": {"file": Path(d).name}} for d in case.spec["docs"]]
    for n, d in plan["datasets"].items():
        file, sheet = d["sheet"].split("/", 1)
        script.append({"tool": "define_dataset", "args": {"name": n, "file": file, "sheet": sheet, "key": d["key"],
                                                         "link": d["link"], "fields": d["fields"]}})
    script.append({"tool": "set_rules", "args": {
        "tables": {k: v.model_dump(mode="json") for k, v in ref.tables.items()},
        "variables": {k: v.model_dump(mode="json") for k, v in ref.variables.items()},
        "pool": ref.pool.model_dump(mode="json"),
        "allocation": [a.model_dump(mode="json") for a in ref.allocation],
        "payout": [p.model_dump(mode="json") for p in ref.payout]}})
    script.append({"tool": "trial_run"})
    for c in case.spec["checks"]:
        if c["type"] == "question":
            script.append({"tool": "ask_question", "args": {"text": "请确认：" + "、".join(map(str, c["keywords"])),
                                                           "options": ["全额累进", "超额累进"], "suggested": 0}})
    script.append({"tool": "finish", "args": {"summary": ref.name}})
    return script


def fake_llm_for(case: Case) -> FakeLLM:
    """演练用：转写请求返回固定文字，抽取请求返回标准答案。"""
    ref = case.reference
    answer = {
        "tables": {k: v.model_dump(mode="json") for k, v in ref.tables.items()},
        "variables": {k: v.model_dump(mode="json") for k, v in ref.variables.items()},
        "pool": ref.pool.model_dump(mode="json"),
        "allocation": [a.model_dump(mode="json") for a in ref.allocation],
        "payout": [p.model_dump(mode="json") for p in ref.payout],
        "questions": [{"id": "q1", "text": "请确认：" + "、".join(map(str, kws)), "options": ["全额累进", "超额累进"],
                       "suggested": 0, "related": ""}
                      for kws in [c["keywords"] for c in case.spec["checks"] if c["type"] == "question"]],
    }


    plan = fake_plan(case)
    script = fake_agent_script(case)
    state = {"i": 0}

    def respond(messages, tools=None):
        if tools is not None:  # agent 模式：按顺序返回工具调用
            item = script[min(state["i"], len(script) - 1)]
            state["i"] += 1
            return item
        sys = messages[0]["content"] if isinstance(messages[0]["content"], str) else ""
        if "统筹规划助手" in sys:
            return json.dumps(plan, ensure_ascii=False)
        if "转写" in sys and "规则 DSL" not in sys:
            return "| 档位 | 比例 |\n| --- | --- |\n| 示例 | 示例 |"
        return json.dumps(answer, ensure_ascii=False)

    return FakeLLM(respond, name="fake")


def answer_pending(out: Path, answers: dict[str, str]) -> None:
    """评测时扮演财务：给案卷里未答复的问题填答复。answers 是 关键字 -> 答复（问题里含关键字就用它）。"""
    p = out / "questions.yaml"
    qs = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    for q in qs:
        if q.get("answer") in (None, ""):
            q["answer"] = next((a for k, a in answers.items() if k in q["text"]), "按文件原意，以参考口径为准")
    p.write_text(yaml.safe_dump(qs, allow_unicode=True, sort_keys=False), encoding="utf-8")


def run_one(llm, case: Case, mode: str) -> dict:
    """mode：two_step / direct 用固定的数据字典只测规则抽取；auto 从原始文件开始走完整的统筹规划流程。"""
    t0 = time.monotonic()
    ar = None
    if mode == "agent":
        import tempfile
        with tempfile.TemporaryDirectory() as out:
            ag = run_agent(llm, case.docs + case.data_files, out)
            rounds = 1
            while ag.status == "NEEDS_CONFIRMATION" and rounds < 3:   # 模拟财务答复，下一轮接着做
                answer_pending(Path(out), case.spec.get("finance_answers", {}))
                ag = run_agent(llm, case.docs + case.data_files, out)
                rounds += 1
        ex = ExtractResult(llm.name, ag.ruleset, ag.questions, [], [], list(ag.ws.transcripts.values()),
                           len(ag.steps), llm.usage, ag.quote_hits, ag.quote_total, ag.error)
        row = {"model": llm.name, "case": case.name, "mode": mode, "ok": ag.ok, "error": ag.error,
               "attempts": len(ag.steps), "latency_s": round(time.monotonic() - t0, 1),
               "prompt_tokens": llm.usage.prompt_tokens, "completion_tokens": llm.usage.completion_tokens,
               "quote_hit_rate": round(ag.quote_hits / ag.quote_total, 3) if ag.quote_total else None,
               "questions": len(ag.questions), "rounds": rounds, "blocking_issues": 0}
        checks = {}
        if ag.ruleset is not None:
            for chk in case.spec["checks"]:
                try:
                    checks[chk["name"]] = run_check(chk, ag.ruleset, ex)
                except Exception:
                    checks[chk["name"]] = False
        else:
            checks = {c["name"]: False for c in case.spec["checks"]}
        row.update(compare_results(ag.result, case.ref_result) if ag.result is not None else
                   {"computed": False, "pool_match": False, "leaf_match_rate": 0.0, "held_match": False})
        row["checks"] = checks
        row["check_score"] = round(sum(checks.values()) / len(checks), 3) if checks else 0
        row["_steps"] = [vars(s) for s in ag.steps]
        row["_ruleset"] = ag.ruleset.dump_yaml() if ag.ruleset else None
        row["_questions"] = [q.model_dump() for q in ag.questions]
        row["_transcripts"] = ag.ws.transcripts
        return row
    if mode == "auto":
        ar = run_auto(llm, case.docs + case.data_files)
        ex = ar.extraction
        if ex is None:  # 规划阶段就失败了
            ex = ExtractResult(llm.name, None, [], [], [], [], 0, llm.usage, error=ar.error)
    else:
        docs = [parse_any(p) for p in case.docs]
        ex = extract(llm, [], case.reference.datasets, docs=docs, direct_images=(mode == "direct"))
    row = {
        "model": llm.name, "case": case.name, "mode": mode,
        "ok": ex.ok if ar is None else ar.ok, "error": ex.error if ar is None else ar.error, "attempts": ex.attempts,
        "latency_s": round(time.monotonic() - t0, 1),
        "prompt_tokens": ex.usage.prompt_tokens, "completion_tokens": ex.usage.completion_tokens,
        "quote_hit_rate": round(ex.quote_hits / ex.quote_total, 3) if ex.quote_total else None,
        "questions": len(ex.questions),
        "blocking_issues": sum(1 for i in ex.issues if i.severity == "blocking"),
    }
    checks = {}
    if ex.ruleset is not None:
        for chk in case.spec["checks"]:
            try:
                checks[chk["name"]] = run_check(chk, ex.ruleset, ex)
            except Exception:  # 单项检查出错按未通过处理
                checks[chk["name"]] = False
        if ar is None:
            row.update(numeric_score(ex.ruleset, case.data, case.ref_result))
        elif ar.result is not None:
            row.update(compare_results(ar.result, case.ref_result))
        else:
            row.update({"computed": False, "pool_match": False, "leaf_match_rate": 0.0, "held_match": False})
    else:
        checks = {c["name"]: False for c in case.spec["checks"]}
        row.update({"computed": False, "pool_match": False, "leaf_match_rate": 0.0, "held_match": False})
    row["checks"] = checks
    row["check_score"] = round(sum(checks.values()) / len(checks), 3) if checks else 0
    row["_raw"] = ex.raw_outputs
    row["_transcripts"] = ex.transcripts
    row["_ruleset"] = ex.ruleset.dump_yaml() if ex.ruleset else None
    row["_questions"] = [q.model_dump() for q in ex.questions]
    if ar is not None:
        row["plan_attempts"] = ar.plan.attempts
        row["_plan"] = ar.plan.plan.model_dump() if ar.plan.plan else None
        row["_plan_raw"] = ar.plan.raw_outputs
        row["_plan_errors"] = ar.plan.errors
    row["_issues"] = [str(i) for i in ex.issues]
    return row


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    keys = sorted({(r["model"], r["mode"]) for r in rows})
    for model, mode in keys:
        rs = [r for r in rows if r["model"] == model and r["mode"] == mode]
        def mean(k):
            vals = [r[k] for r in rs if r.get(k) is not None]
            return round(statistics.mean(vals), 3) if vals else None
        out.append({
            "model": model, "mode": mode, "runs": len(rs),
            "可用规则率": mean("ok"),
            "结构检查得分": mean("check_score"),
            "奖金池算对率": mean("pool_match"),
            "个人金额算对率": mean("leaf_match_rate"),
            "挂起金额全对率": mean("held_match"),
            "原文引用命中率": mean("quote_hit_rate"),
            "平均修正轮数": mean("attempts"),
            "平均耗时(秒)": mean("latency_s"),
            "平均输入tokens": mean("prompt_tokens"),
            "平均输出tokens": mean("completion_tokens"),
        })
    return out


def write_report(rows, summary, out_dir: Path, cases: list[Case]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    raw_dir = out_dir / f"{stamp}-raw"
    raw_dir.mkdir()
    for i, r in enumerate(rows):
        (raw_dir / f"{i:03d}-{r['model']}-{r['case']}-{r['mode']}.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    flat = [{k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v)
             for k, v in r.items() if not k.startswith("_")} for r in rows]
    with open(out_dir / f"{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in flat for k in r}))
        w.writeheader()
        w.writerows(flat)
    cols = list(summary[0].keys()) if summary else []
    lines = [f"# 模型选型评测报告 {stamp}", "",
             f"样例：{', '.join(c.name for c in cases)}；每个模型 × 方式的运行次数见 runs 列。", "",
             "| " + " | ".join(cols) + " |", "|" + " --- |" * len(cols)]
    for s in summary:
        lines.append("| " + " | ".join("" if s[c] is None else str(s[c]) for c in cols) + " |")
    lines += ["", "## 各项结构检查通过率", ""]
    names = [c["name"] for c in cases[0].spec["checks"]] if cases else []
    lines.append("| 模型 | 方式 | " + " | ".join(names) + " |")
    lines.append("|" + " --- |" * (len(names) + 2))
    for s in summary:
        rs = [r for r in rows if r["model"] == s["model"] and r["mode"] == s["mode"]]
        vals = [f"{sum(r['checks'].get(n, False) for r in rs)}/{len(rs)}" for n in names]
        lines.append(f"| {s['model']} | {s['mode']} | " + " | ".join(vals) + " |")
    errs = [r for r in rows if r.get("error")]
    if errs:
        lines += ["", "## 失败记录", ""] + [f"- {r['model']} / {r['mode']}：{r['error'][:200]}" for r in errs]
    lines += ["", "说明：", "- 「个人金额算对率」= 用模型抽取的规则在标准数据上实算，与标准答案逐人比对（精确到分）的比例，是最终最重要的指标；",
              "- 「原文引用命中率」低说明模型在编造出处；",
              "- 原始输出、图片转写和抽取出的规则在同名 -raw 目录中。"]
    path = out_dir / f"{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bonus-eval")
    ap.add_argument("--models", help="逗号分隔的模型名或分组名（candidates / open / reference / legacy），默认用配置里的 candidates")
    ap.add_argument("--cases", help="逗号分隔的样例目录名，默认全部")
    ap.add_argument("--modes", default="two_step",
                    help="two_step / direct（固定数据字典，只测规则抽取）；agent（大模型主导，从原始文件开始）；auto（固定流程全自动）")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--fake", action="store_true", help="不调用模型，用标准答案演练")
    ap.add_argument("--out", default=str(ROOT / "reports"))
    a = ap.parse_args(argv)

    case_dirs = [CASES / c for c in a.cases.split(",")] if a.cases else sorted(p for p in CASES.iterdir() if (p / "case.yaml").exists())
    cases = [load_case(d) for d in case_dirs]
    cfg = load_config()
    try:
        models = ["fake"] if a.fake else resolve_models(a.models, cfg)
    except LLMError as e:
        print(e)
        return 2
    rows = []
    for m in models:
        for case in cases:
            for mode in a.modes.split(","):
                for k in range(a.repeats):
                    llm = fake_llm_for(case) if a.fake else LLM.from_config(m, cfg)
                    print(f"→ {m} / {case.name} / {mode} / 第 {k + 1} 次 ...", flush=True)
                    r = run_one(llm, case, mode)
                    print(f"   可用={r['ok']} 结构={r['check_score']} 个人金额算对率={r['leaf_match_rate']} "
                          f"耗时={r['latency_s']}s {r['error'][:80]}", flush=True)
                    rows.append(r)
    summary = summarize(rows)
    path = write_report(rows, summary, Path(a.out), cases)
    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
