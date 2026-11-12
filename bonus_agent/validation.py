"""Deterministic validation primitives. Missing coverage is never a successful check."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    INCOMPLETE = "INCOMPLETE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass
class CheckResult:
    id: str
    status: Status
    message: str = ""
    expected: int = 0
    checked: int = 0
    examples: list = field(default_factory=list)

    @property
    def ok(self):
        return self.status in (Status.PASS, Status.NOT_APPLICABLE)

    def to_dict(self):
        return asdict(self)


def compare_values(check_id, expected, actual, tolerance=Decimal("0.01")):
    """Check both key sets before comparing any values; retain counterexamples."""
    wanted, got = set(expected), set(actual)
    if wanted != got or not wanted:
        return CheckResult(check_id, Status.INCOMPLETE, "验算对象集合不完整或为空", len(wanted),
                           len(wanted & got), [{"missing": sorted(wanted-got), "extra": sorted(got-wanted)}])
    diffs = [{"node": k, "expected": str(expected[k]), "actual": str(actual[k])}
             for k in sorted(wanted) if abs(Decimal(expected[k])-Decimal(actual[k])) > tolerance]
    return CheckResult(check_id, Status.FAIL if diffs else Status.PASS,
                       "金额不一致" if diffs else "全部对象金额一致", len(wanted), len(wanted), diffs)


def result_integrity(result):
    """Engine-wide conservation laws, independent of any customer's business formula."""
    errors = []
    nodes = result.nodes
    children = {}
    for node in nodes.values():
        if node.parent is not None:
            children.setdefault(node.parent, []).append(node)
    for parent, kids in children.items():
        if parent not in nodes:
            errors.append({"node": parent, "error": "上级对象不存在"})
        elif sum((Decimal(n.final) for n in kids), Decimal(0)) != Decimal(nodes[parent].final):
            errors.append({"node": parent, "error": "下级金额合计与上级不一致"})
    leaves = [n for n in nodes.values() if n.level == result.levels[-1]] if result.levels else []
    total = sum((Decimal(n.final) for n in leaves), Decimal(0))
    if total != Decimal(result.pool):
        errors.append({"error": "最末层金额与奖金池不一致", "leaves": str(total), "pool": result.pool})
    if result.people and sum((Decimal(p["final"]) for p in result.people if not p.get("pending")), Decimal(0)) != total:
        errors.append({"error": "人员汇总与最末层金额不一致"})
    for row in [*result.payouts.values(), *result.rollups.values()]:
        final, held, paid = Decimal(row.final), Decimal(row.held), Decimal(row.paid)
        cap = Decimal(row.cap or row.final)
        deducted, offset, overpaid = Decimal(row.deducted), Decimal(row.offset), Decimal(row.overpaid)
        if final != held + cap or cap != paid + deducted or offset != deducted + overpaid:
            errors.append({"node": row.node_id, "error": "应得、暂缓、可发上限、抵扣与实发不闭合"})
    return CheckResult("engine.conservation", Status.FAIL if errors else Status.PASS,
                       "引擎金额不闭合" if errors else "分配及人员汇总闭合", len(nodes), len(nodes), errors)


def covered_nodes(result, origins):
    """Follow trace dependencies from auxiliary entities to allocated objects."""
    actual, origins = set(result.nodes), set(origins)
    found = actual & origins
    forward, todo = {}, []
    for key, trace in result.traces.items():
        if trace.node_id in origins:
            todo.append(key)
        for dependency in trace.inputs:
            forward.setdefault(dependency, []).append(key)
    seen = set()
    while todo:
        key = todo.pop()
        if key in seen:
            continue
        seen.add(key)
        trace = result.traces.get(key)
        if trace and trace.node_id in actual:
            found.add(trace.node_id)
        todo.extend(forward.get(key, []))
    return found
