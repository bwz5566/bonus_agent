"""智能体命令行：python -m bonus_agent.cli <命令> ...

  models [--remote]                        列出已配置的模型；--remote 用 Key 查询服务商实际可用的模型
  extract --datasets D.yaml [--model M] --out draft.yaml 文档...
                                           从规则文档抽取规则草稿和问题卡
  map --rules R.yaml [--model M] 数据.xlsx 推荐字段映射（启发式 + 大模型）
  auto [--model M] [--answers Q.yaml] --out 目录 文件或文件夹...
                                           全自动：统筹规划 → 抽取规则 → 计算 → 输出 Excel
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from bonus_agent.extract_rules import extract, load_datasets
from bonus_agent.llm import LLM, LLMError, list_remote, load_config
from bonus_agent.suggest_mapping import suggest
from bonus_core.data.table_detect import detect
from bonus_core.dsl.models import RuleSet


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bonus-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("models")
    p.add_argument("--remote", action="store_true", help="调用服务商 /models 接口，核对已配置模型能否调用")
    p = sub.add_parser("extract")
    p.add_argument("--datasets", required=True, help="数据字典 YAML，或任意一份含 datasets 的规则文件")
    p.add_argument("--model")
    p.add_argument("--name", default="规则草稿")
    p.add_argument("--version", default="draft")
    p.add_argument("--direct-images", action="store_true", help="不单独转写图片，直接看图抽取")
    p.add_argument("--out", required=True)
    p.add_argument("docs", nargs="+")
    p = sub.add_parser("map")
    p.add_argument("--rules", required=True)
    p.add_argument("--model")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("data", nargs="+")
    p = sub.add_parser("auto")
    p.add_argument("--model")
    p.add_argument("--answers", help="上一次输出的 questions.yaml，填写 answer 后传入重算")
    p.add_argument("--out", required=True, help="输出目录")
    p.add_argument("paths", nargs="+", help="规则文档、Excel 数据，或包含它们的文件夹")
    a = ap.parse_args(argv)

    cfg = load_config()
    if a.cmd == "models":
        remote: dict[str, set[str] | str] = {}
        if a.remote:
            for prov in sorted({m["provider"] for m in cfg["models"].values()}):
                if prov == "local_vllm":   # 选型阶段还没部署，查不到是正常的
                    try:
                        remote[prov] = set(list_remote(prov, cfg))
                    except LLMError:
                        remote[prov] = "本地 vLLM 还未部署（选型完成、部署后才需要），已跳过"
                    continue
                try:
                    remote[prov] = set(list_remote(prov, cfg))
                except LLMError as e:
                    remote[prov] = str(e)
        for name, m in cfg["models"].items():
            mark = "（默认）" if name == cfg["default"] else ""
            tags = [g for g, ms in [("候选", cfg.get("candidates", []))] + list(cfg.get("groups", {}).items())
                    if name in ms]
            line = f"{name}{mark}: {m['provider']} / {m['model']}" + (f"  [{', '.join(tags)}]" if tags else "")
            if a.remote:
                r = remote[m["provider"]]
                line += "  无法查询" if isinstance(r, str) else ("  可用" if m["model"] in r else "  不可用")
            print(line)
        for prov, r in remote.items():
            if isinstance(r, str):
                print(f"\n{r}")
            else:
                print(f"\n{prov} 共返回 {len(r)} 个模型，其中名字含 vl / 3.5 / 3.6 的：")
                print("  " + ", ".join(x for x in sorted(r) if any(k in x for k in ("vl", "3.5", "3.6", "omni"))))
        return 0

    if a.cmd == "extract":
        llm = LLM.from_config(a.model, cfg)
        res = extract(llm, a.docs, load_datasets(a.datasets), name=a.name, version=a.version,
                      direct_images=a.direct_images)
        out = Path(a.out)
        if res.ruleset is not None:
            out.write_text(res.ruleset.dump_yaml(), encoding="utf-8")
            print(f"规则草稿已写出：{out}")
        qpath = out.with_suffix(".questions.json")
        qpath.write_text(json.dumps({
            "questions": [q.model_dump() for q in res.questions],
            "issues": [i.to_dict() for i in res.issues],
            "transcripts": res.transcripts,
            "usage": vars(res.usage),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"问题卡与校验结果：{qpath}")
        for q in res.questions:
            print(f"  ? {q.text}  选项：{' / '.join(q.options)}")
        for i in res.issues:
            print(f"  {i}")
        if res.error:
            print(f"失败：{res.error}")
        return 0 if res.ok else 1

    if a.cmd == "map":
        rs = RuleSet.load(a.rules)
        llm = None if a.no_llm else LLM.from_config(a.model, cfg)
        m = suggest(rs, [s for f in a.data for s in detect(f)], llm)
        print(yaml.safe_dump({k: v.model_dump(exclude_none=True) for k, v in m.items()}, allow_unicode=True, sort_keys=False))
        return 0
    if a.cmd == "auto":
        from bonus_agent.auto import run_auto
        answers = yaml.safe_load(Path(a.answers).read_text(encoding="utf-8")) if a.answers else None
        llm = LLM.from_config(a.model, cfg)
        print(f"使用模型 {llm.name}，处理 {len(a.paths)} 个路径 ...", flush=True)
        ar = run_auto(llm, a.paths, a.out, answers=answers)
        po = ar.plan
        if po.plan:
            print(f"\n【规划】{po.plan.summary}")
            print("  分配层级：" + " → ".join(lv.level for lv in po.plan.hierarchy))
            for n, m in po.mapping.items():
                print(f"  数据表 {n} ← {m.file}/{m.sheet}（{len(m.columns)} 个字段）")
            for w in po.warnings:
                print(f"  注意：{w}")
        if ar.result is not None:
            print(f"\n【结果】奖金池 {ar.result.pool:,}，结果已写入 {ar.outputs.get('excel')}")
        if ar.questions:
            print(f"\n【待确认问题】{len(ar.questions)} 个（已按建议理解先算，答复后用 --answers 重跑）：")
            for stage, q in ar.questions:
                print(f"  [{stage}] {q.text}  选项：{' / '.join(q.options)}")
        for i in ar.issues:
            if i.severity == "warning":
                print(f"  {i}")
        if ar.error:
            print(f"\n失败：{ar.error}")
        print(f"\n输出目录：{a.out}（规则 rules.yaml、映射 mapping.yaml、问题 questions.yaml、日志 agent_log.json）")
        u = llm.usage
        print(f"调用 {u.calls} 次，输入 {u.prompt_tokens} tokens，输出 {u.completion_tokens} tokens，耗时 {u.latency_s:.0f} 秒")
        return 0 if ar.ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
