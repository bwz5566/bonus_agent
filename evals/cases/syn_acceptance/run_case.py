"""用手写的标准规则（rules/）经引擎计算三个方案，和独立参考实现（reference.py）逐分核对。

运行：python evals/cases/syn_acceptance/run_case.py [05_期望奖金结果.xlsx]
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from bonus_core.data.mapping import apply_mapping, load_mapping  # noqa: E402
from bonus_core.dsl.models import RuleSet  # noqa: E402
from bonus_core.engine.run import Adjustment, Engine, Result  # noqa: E402

HERE = Path(__file__).parent
XLSX = HERE / "inputs" / "03_业务输入.xlsx"
ORDER = ["QUARTER", "PROCESS", "ANNUAL"]      # ANNUAL 抵扣引用 QUARTER R2，排在后面


def _y(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def load(scheme: str) -> tuple[RuleSet, list[dict]]:
    shared, own = _y(HERE / "rules" / "shared.yaml"), _y(HERE / "rules" / f"rules_{scheme}.yaml")
    revisions = own.pop("revisions", [])
    own["datasets"] = _y(HERE / "datasets.yaml")
    for k in ("tables", "variables"):
        own[k] = {**shared.get(k, {}), **own.get(k, {})}
    own.setdefault("checks", shared.get("checks", []))
    return RuleSet.model_validate(own), revisions


def run(xlsx: Path = XLSX) -> dict[str, dict[str, Result]]:
    out: dict[str, dict[str, Result]] = {}
    refs: dict[str, Result] = {}
    for s in ORDER:
        rs, revisions = load(s)
        data, issues = apply_mapping(rs, [xlsx], load_mapping(HERE / "mapping.yaml"))
        blocking = [i for i in issues if i.severity == "blocking"]
        if blocking:
            from bonus_core.engine.issues import EngineError
            raise EngineError(blocking)
        eng = Engine(rs, data, refs)
        res = {"R0": eng.compute()}
        done: list[Adjustment] = []
        for rev in revisions:
            done += [Adjustment(a["node"], Decimal(str(a["amount"])), reason=a.get("reason", ""),
                                created_at=f"{len(done) + i:06d}") for i, a in enumerate(rev["adjust"])]
            res[rev["name"]] = eng.compute(list(done))
        out[s] = res
        refs.update({f"{s}/{k}": v for k, v in res.items()})
    return out


def diff_reference(results, reference: dict) -> list[str]:
    """逐个对象核对应得、可发上限、本次可发、暂缓、已发抵扣、超发。"""
    bad = []
    for s, revs in reference.items():
        for rev, want in revs.items():
            r = results[s][rev]
            for k, v in want["dept"].items():
                if Decimal(r.nodes[f"部门:{k}"].final) != Decimal(v):
                    bad.append(f"{s} {rev} 部门:{k} 参考 {v} 引擎 {r.nodes[f'部门:{k}'].final}")
            for k, v in want["group"].items():
                if Decimal(r.nodes[f"小组:{k}"].final) != Decimal(v):
                    bad.append(f"{s} {rev} 小组:{k} 参考 {v} 引擎 {r.nodes[f'小组:{k}'].final}")
            for k, p in want["emp"].items():
                n, pr = r.nodes[f"个人:{k}"], r.payouts[f"个人:{k}"]
                got = {"final": n.final, "cap": pr.cap, "paid": pr.paid, "held": pr.held, "offset": pr.offset,
                       "overpaid": pr.overpaid}
                for f, v in p.items():
                    if Decimal(got[f]) != Decimal(v):
                        bad.append(f"{s} {rev} 个人:{k} {f} 参考 {v} 引擎 {got[f]}")
    return bad


def main():
    import json
    results = run()
    ref = json.loads((HERE / "reference.json").read_text(encoding="utf-8"))
    bad = diff_reference(results, ref)
    n = sum(len(w["dept"]) + len(w["group"]) + 6 * len(w["emp"]) for revs in ref.values() for w in revs.values())
    print(f"引擎 vs 参考实现：核对 {n} 个金额，不一致 {len(bad)} 个")
    for b in bad[:20]:
        print(" ", b)
    if len(sys.argv) > 1:
        from bonus_agent.compare import compare_expected, report
        for line in report(compare_expected(results, sys.argv[1])):
            print(line)


if __name__ == "__main__":
    main()
