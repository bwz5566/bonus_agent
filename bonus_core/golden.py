"""运行规则集自带的金标样例（财务手算），必须精确到分一致才能确认版本。"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bonus_core.data.table import from_records
from bonus_core.dsl.models import RuleSet
from bonus_core.engine.issues import EngineError
from bonus_core.engine.run import compute


@dataclass
class GoldenResult:
    case: str
    passed: bool
    diffs: list[str]


def run_golden(rs: RuleSet) -> list[GoldenResult]:
    out = []
    for case in rs.tests:
        diffs = []
        try:
            res = compute(rs, from_records(rs, case.datasets, file=f"golden:{case.name}"))
        except EngineError as e:
            out.append(GoldenResult(case.name, False, [str(i) for i in e.issues]))
            continue
        for k, want in case.expect.items():
            got = Decimal(res.pool) if k == "pool" else (res.amount(k) if k in res.nodes else None)
            if got is None:
                diffs.append(f"{k}: 结果中没有这个节点")
            elif got != Decimal(want):
                diffs.append(f"{k}: 期望 {want}，实际 {got}")
        out.append(GoldenResult(case.name, not diffs, diffs))
    return out
