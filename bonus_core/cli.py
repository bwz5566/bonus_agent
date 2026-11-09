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
    p.add_argument("--scenario", required=True)
    p.add_argument("--from-pack", action="store_true",
                   help="正式结算迁入的规则绑定（金额只走 bonus_core）。不能同时再给规则或映射")
    p.add_argument("--product-approval", help="产品书面批准记录。设置了状态目录时，目录里的决定才是权威")
    p.add_argument("--rules")
    p.add_argument("--mapping")
    p.add_argument("--out", required=True)
    p.add_argument("--adjust")
    p.add_argument("--json", help="同时输出 JSON 结果")
    p.add_argument("data", nargs="+")
    p = sub.add_parser("run-template")
    p.add_argument("--scenario", required=True)
    p.add_argument("--product-approval", help="产品书面批准记录。迁入草稿的模板还必须带同一份批准")
    p.add_argument("--template", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("data", nargs="+")
    a = ap.parse_args(argv)

    if a.cmd == "run" and not a.from_pack and (not a.rules or not a.mapping):
        ap.error("--rules and --mapping are required unless --from-pack")
    if a.cmd in {"run", "run-template"}:
        from bonus_agent.scenarios import ScenarioError, require_settlement
        try:
            pack = require_settlement(a.scenario, getattr(a, "product_approval", None))
        except ScenarioError as exc:
            print(f"Settlement rejected: {exc}")
            return 1
        if a.cmd == "run" and pack.metadata.status == "migrated_draft":
            if not a.from_pack or a.rules or a.mapping or a.adjust:
                print("Settlement rejected: migrated_draft formal settlement must use --from-pack "
                      "without alternate rules, mapping, or adjustments.")
                return 1
            from bonus_agent.scenarios.settle import SettlementError, settle_bound
            try:
                results = settle_bound(a.scenario, a.data, a.out, a.product_approval)
            except (SettlementError, EngineError, AdjustConflict) as exc:
                print(f"Settlement rejected: {exc}")
                return 1
            print(f"migrated_draft settlement via bonus_core; finance_signed=false; wrote {a.out}")
            if a.json:
                payload = {name: result.to_dict() for name, result in results.items()}
                Path(a.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0
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
    from bonus_agent.scenarios import ScenarioError
    from bonus_core.template import Template, TemplateError
    try:
        tpl = Template.load(a.template)
        if tpl.manifest.get("scenario") not in (None, a.scenario):
            print("Settlement rejected: template scenario does not match --scenario")
            return 1
        supplied = getattr(a, "product_approval", None)
        if supplied and tpl.manifest.get("product_approval"):
            from bonus_agent.scenarios.approval import _body, _coerce
            if _body(_coerce(supplied)) != _body(tpl.manifest["product_approval"]):
                print("Settlement rejected: --product-approval does not match the template record")
                return 1
        results, warnings = tpl.run(a.data)
    except (TemplateError, EngineError, AdjustConflict, ScenarioError) as e:
        print(f"计算失败：{e}")
        return 1
    for i in warnings:
        print(i)
    for f, (name, rev, res) in tpl.write(results, a.out):
        print(f"{name}{'' if rev == 'R0' else ' ' + rev}：奖金池 {res.pool}，已写出 {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
