"""智能体命令行：python -m bonus_agent.cli <命令> ...

  models                                   列出已配置的模型
  extract --datasets D.yaml [--model M] --out draft.yaml 文档...
                                           从规则文档抽取规则草稿和问题卡
  map --rules R.yaml [--model M] 数据.xlsx 推荐字段映射（启发式 + 大模型）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from bonus_agent.extract_rules import extract, load_datasets
from bonus_agent.llm import LLM, load_config
from bonus_agent.suggest_mapping import suggest
from bonus_core.data.table_detect import detect
from bonus_core.dsl.models import RuleSet


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bonus-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models")
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
    a = ap.parse_args(argv)

    cfg = load_config()
    if a.cmd == "models":
        for name, m in cfg["models"].items():
            mark = "（默认）" if name == cfg["default"] else ""
            cand = "  [候选]" if name in cfg.get("candidates", []) else ""
            print(f"{name}{mark}: {m['provider']} / {m['model']}{cand}")
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
