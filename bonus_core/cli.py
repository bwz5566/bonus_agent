"""命令行：python -m bonus_core.cli <命令> ...

  validate  --rules R            静态校验规则
  test      --rules R            运行金标样例
  detect    DATA.xlsx            识别表头与数据区域
  suggest   --rules R DATA...    推荐字段映射（启发式），输出 YAML
  run       --rules R --mapping M DATA... --out result.xlsx [--adjust A.yaml]
  run-template --template 模板目录 --out 结果目录 DATA...
                                 按财务确认的模板计算全部方案和修订（模板被改过会拒绝）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from bonus_core.data.mapping import apply_mapping, load_mapping, suggest_heuristic
from bonus_core.data.table_detect import detect
from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.validator import validate
from bonus_core.engine.allocation import AdjustConflict
from bonus_core.engine.issues import EngineError
from bonus_core.engine.ledger import ledger_entries
from bonus_core.engine.run import Adjustment, Engine
from bonus_core.golden import run_golden
from bonus_core.report.excel import write_excel


def _load_adjustments(path) -> list[Adjustment]:
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        return [Adjustment(**a) for a in (yaml.safe_load(f) or [])]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bonus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "test"):
        p = sub.add_parser(name)
        p.add_argument("--rules", required=True)
    p = sub.add_parser("detect")
    p.add_argument("data", nargs="+")
    p = sub.add_parser("suggest")
    p.add_argument("--rules", required=True)
    p.add_argument("data", nargs="+")
    p = sub.add_parser("run")
    p.add_argument("--rules", required=True)
    p.add_argument("--mapping", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--adjust")
    p.add_argument("--json", help="同时输出 JSON 结果")
    p.add_argument("data", nargs="+")
    p = sub.add_parser("run-template")
    p.add_argument("--template", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("data", nargs="+")
    a = ap.parse_args(argv)

    if a.cmd == "run-template":
        return _run_template(a)

    if a.cmd == "detect":
        for f in a.data:
            for s in detect(f):
                print(f"{s.file} / {s.sheet}: 表头在第 {s.header_row} 行，{len(s.rows)} 行数据，{len(s.totals)} 行合计")
                print("   列：", " | ".join(h for h in s.headers if h))
        return 0

    rs = RuleSet.load(a.rules)
    if a.cmd == "validate":
        issues = validate(rs)
        for i in issues:
            print(i)
        print("校验通过" if not any(i.severity == "blocking" for i in issues) else "存在阻断问题")
        return 1 if any(i.severity == "blocking" for i in issues) else 0
    if a.cmd == "test":
        ok = True
        for g in run_golden(rs):
            print(("✓ " if g.passed else "✗ ") + g.case)
            for d in g.diffs:
                print("    " + d)
            ok &= g.passed
        return 0 if ok else 1
    if a.cmd == "suggest":
        m = suggest_heuristic(rs, [s for f in a.data for s in detect(f)])
        print(yaml.safe_dump({k: v.model_dump(exclude_none=True) for k, v in m.items()}, allow_unicode=True, sort_keys=False))
        return 0
    if a.cmd == "run":
        data, issues = apply_mapping(rs, a.data, load_mapping(a.mapping))
        for i in issues:
            print(i)
        if any(i.severity == "blocking" for i in issues):
            print("数据存在阻断问题，已停止计算")
            return 1
        try:
            eng = Engine(rs, data)
            eng.issues += issues
            res = eng.compute(_load_adjustments(a.adjust))
        except (EngineError, AdjustConflict, PermissionError) as e:
            print(f"计算失败：{e}")
            return 1
        write_excel(rs, res, a.out, ledger_entries(res))
        print(f"奖金池 {res.pool}，已写出 {a.out}")
        if a.json:
            Path(a.json).write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    return 2


def _run_template(a) -> int:
    from bonus_core.template import Template, TemplateError
    try:
        tpl = Template.load(a.template)
        results, warnings = tpl.run(a.data)
    except (TemplateError, EngineError, AdjustConflict) as e:
        print(f"计算失败：{e}")
        return 1
    for i in warnings:
        print(i)
    c = tpl.manifest["confirmed"]
    info = [("确认信息", ["项目", "值"], [["确认人", c["by"]], ["确认时间", c["at"]], ["确认的轮次", f"第 {c['round']} 轮"],
                                       ["说明", c.get("note", "")], ["模板目录", str(tpl.dir)]], {"项目": 14, "值": 70})]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rules = {s["name"]: RuleSet.load(tpl.dir / s["rules"]) for s in tpl.manifest["schemes"]}
    for name, revs in results.items():
        for rev, res in revs.items():
            f = out / (f"result_{name}.xlsx" if rev == "R0" else f"result_{name}_{rev}.xlsx")
            write_excel(rules[name], res, f, ledger_entries(res), extra_sheets=info)
            print(f"{name}{'' if rev == 'R0' else ' ' + rev}：奖金池 {res.pool}，已写出 {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
