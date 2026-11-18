"""Explicit calculation projections; no business defaults or fabricated weights."""
from __future__ import annotations

from bonus_core.dsl.models import RuleSet
from bonus_core.dsl.parser import ExprError, names, parse


def basis_rules(rs: RuleSet) -> RuleSet:
    """Keep the transitive inputs of the pre-distribution calculation.

    Data, population patches and declared relationships remain authoritative.
    Only explicitly distribution-stage checks/report rows are deferred. Broken
    selected expressions are kept so validation reports them, never discarded.
    """
    result = rs.model_copy(deep=True)
    result.payout = []
    result.offsets = []
    result.checks = [c for c in result.checks if c.stage == 'basis']
    if result.report is not None:
        result.report.totals = [r for r in result.report.totals if r.stage == 'basis']
        result.report.details = [r for r in result.report.details if r.stage == 'basis']
        result.report.people = None
        result.report.pending = []
        result.report.balances = []
        result.report.distributions = []

    pending = [result.pool.expr, *(c.expr for c in result.checks)]
    if result.report:
        pending += [r.expr for r in [*result.report.totals, *result.report.details]]
    variables, tables = set(), set()
    while pending:
        expr = pending.pop()
        try:
            refs = names(parse(expr))
        except ExprError:
            continue
        tables.update(refs & set(result.tables))
        for name in refs & set(result.variables) - variables:
            variables.add(name)
            pending.append(result.variables[name].expr)
    result.variables = {n: v for n, v in result.variables.items() if n in variables}
    result.tables = {n: t for n, t in result.tables.items() if n in tables}
    return result
