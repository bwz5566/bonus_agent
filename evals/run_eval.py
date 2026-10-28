"""候选模型对比评测。

    # 真实调用（需要设置 DASHSCOPE_API_KEY）
    python -m evals.run_eval --repeats 3
    # 测千问 3.5/3.6 开源多模态模型这一组
    python -m evals.run_eval --models next --repeats 3
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

from bonus_agent.extract_rules import extract
from bonus_agent.llm import LLM, FakeLLM, load_config, resolve_models
from bonus_agent.parsing.docs import parse_any
from bonus_core.data.mapping import apply_mapping, load_mapping
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.run import compute
from evals.scoring import numeric_score, run_check

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


def load_case(d: Path) -> Case:
    spec = yaml.safe_load((d / "case.yaml").read_text(encoding="utf-8"))
    ref = RuleSet.load(d / spec["reference_rules"])
    data, issues = apply_mapping(ref, [d / p for p in spec["data"]], load_mapping(d / spec["mapping"]))
    if any(i.severity == "blocking" for i in issues):
        raise RuntimeError(f"样例 {d.name} 的数据有阻断问题：{issues}")
    missing = [p for p in spec["docs"] if not (d / p).exists()]
    if missing:
        raise RuntimeError(f"样例 {d.name} 缺少文档 {missing}，请先运行 {d / 'make_case.py'}")
    return Case(d.name, d, spec, ref, data, compute(ref, data))


def fake_llm_for(case: Case) -> FakeLLM:
    """演练用：转写请求返回固定文字，抽取请求返回标准答案。"""
    ref = case.reference
    answer = {
        "tables": {k: v.model_dump(mode="json") for k, v in ref.tables.items()},
        "variables": {k: v.model_dump(mode="json") for k, v in ref.variables.items()},
        "pool": ref.pool.model_dump(mode="json"),
        "allocation": [a.model_dump(mode="json") for a in ref.allocation],
        "payout": [p.model_dump(mode="json") for p in ref.payout],
        "questions": [{"id": "q1", "text": "净利润达到1千万按5%计提，是全额累进还是超额累进？",
                       "options": ["全额累进", "超额累进"], "suggested": 0, "related": "tables.profit_tier"}],
    }

    def respond(messages):
        sys = messages[0]["content"] if isinstance(messages[0]["content"], str) else ""
        if "转写" in sys and "规则 DSL" not in sys:
            return "| 档位 | 比例 |\n| --- | --- |\n| 示例 | 示例 |"
        return json.dumps(answer, ensure_ascii=False)

    return FakeLLM(respond, name="fake")


def run_one(llm, case: Case, mode: str) -> dict:
    t0 = time.monotonic()
    docs = [parse_any(p) for p in case.docs]
    ex = extract(llm, [], case.reference.datasets, docs=docs, direct_images=(mode == "direct"))
    row = {
        "model": llm.name, "case": case.name, "mode": mode,
        "ok": ex.ok, "error": ex.error, "attempts": ex.attempts,
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
        row.update(numeric_score(ex.ruleset, case.data, case.ref_result))
    else:
        checks = {c["name"]: False for c in case.spec["checks"]}
        row.update({"computed": False, "pool_match": False, "leaf_match_rate": 0.0, "held_match": False})
    row["checks"] = checks
    row["check_score"] = round(sum(checks.values()) / len(checks), 3) if checks else 0
    row["_raw"] = ex.raw_outputs
    row["_transcripts"] = ex.transcripts
    row["_ruleset"] = ex.ruleset.dump_yaml() if ex.ruleset else None
    row["_questions"] = [q.model_dump() for q in ex.questions]
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
    ap.add_argument("--models", help="逗号分隔的模型名或分组名（next / open / reference），默认用配置里的 candidates")
    ap.add_argument("--cases", help="逗号分隔的样例目录名，默认全部")
    ap.add_argument("--modes", default="two_step", help="two_step（先转写图片再抽取）, direct（直接看图）")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--fake", action="store_true", help="不调用模型，用标准答案演练")
    ap.add_argument("--out", default=str(ROOT / "reports"))
    a = ap.parse_args(argv)

    case_dirs = [CASES / c for c in a.cases.split(",")] if a.cases else sorted(p for p in CASES.iterdir() if (p / "case.yaml").exists())
    cases = [load_case(d) for d in case_dirs]
    cfg = load_config()
    models = ["fake"] if a.fake else resolve_models(a.models, cfg)
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
