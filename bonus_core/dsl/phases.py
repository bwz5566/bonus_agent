"""Explicit calculation projections; no business defaults or fabricated weights."""
from __future__ import annotations

from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.parser import ExprError, names, parse


def funding_only(rs: RuleSet) -> bool:
    """An entity's earmarked funds can be an output without a recipient allocation."""
    entity = rs.datasets.get(rs.pool.scope)
    report = rs.report
    return bool(
        not rs.allocation and not rs.payout and not rs.offsets and entity and entity.key
        and rs.pool.scope != 'global' and report and report.totals
        and not (report.people or report.pending or report.distributions)
        and any(item.level == rs.pool.scope and item.sources for item in report.details)
    )


def basis_rules(rs: RuleSet, *, additional_inputs=()) -> RuleSet:
    """Keep the transitive inputs of the pre-distribution calculation.

    Data, population patches and declared relationships remain authoritative.
    Distribution weights and explicitly distribution-stage checks/report rows
    are deferred. Broken selected expressions stay visible to validation.
    """
    result = rs.model_copy(deep=True)
    for level in result.allocation:
        level.weight = ''
    result.payout = []
    result.offsets = []
    result.checks = [c for c in result.checks if c.stage == 'basis']
    if result.report is not None:
        result.report.totals = [r for r in result.report.totals if r.stage == 'basis']
        result.report.details = [r for r in result.report.details if r.stage == 'basis']
        result.report.people = None
        result.report.pending = []
        result.report.balances = [b for b in result.report.balances if b.stage == 'basis']
        result.report.distributions = []

    pending = [result.pool.expr, *(c.expr for c in result.checks), *additional_inputs]
    if result.report:
        pending += [r.expr for r in [*result.report.totals, *result.report.details]]
        pending += [expr for b in result.report.balances for expr in [b.total, *b.parts]]
    variables, tables = set(), set()
    while pending:
        expr = pending.pop()
        try:
            refs = names(parse(expr))
        except ExprError:
            continue
        for name in refs & set(result.tables) - tables:
            tables.add(name)
            if result.tables[name].applies_when:
                pending.append(result.tables[name].applies_when)
        for name in refs & set(result.variables) - variables:
            variables.add(name)
            pending.append(result.variables[name].expr)
    result.variables = {n: v for n, v in result.variables.items() if n in variables}
    result.tables = {n: t for n, t in result.tables.items() if n in tables}
    return result
