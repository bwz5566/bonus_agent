"""Cross-variable fallback provenance, using synthetic coefficients and real DSL evaluation."""
from decimal import Decimal

import pytest

from bonus_agent.contracts import ratio_default_gaps, ratio_default_warnings
from bonus_agent.lint import compile_warnings
from bonus_core.data.table import from_records
from bonus_core.dsl.models import RuleSet, SourceRef
from bonus_core.engine.run import Engine


def rules(expr='tier_ratio(C, alias, 1, 1, missing: alias == 1)', *, metric=None, coefficient='1.2'):
    return RuleSet.model_validate({
        'name': 'Synthetic', 'version': '1',
        'datasets': {'objects': {'key': 'pid', 'fields': {
            'pid': {'type': 'id'}, 'amount': {'type': 'number'}, 'budget': {'type': 'number'}}}},
        'allocation': [{'level': 'objects', 'key': 'pid'}],
        'tables': {'C': {'kind': 'tier', 'mode': 'whole', 'steps': [[0, '.6'], [1, coefficient]]}},
        'variables': {
            'metric': {'scope': 'objects', 'expr': metric or 'ratio(amount, budget, zero: 1)'},
            'alias': {'scope': 'objects', 'expr': 'metric'},
            'factor': {'scope': 'objects', 'expr': expr}},
        'pool': {'scope': 'objects', 'expr': 'factor'},
    })


def compute(rs, *, amount=100, budget=100):
    data = from_records(rs, {'objects': [{'pid': 'A', 'amount': amount, 'budget': budget}]})
    return Decimal(Engine(rs, data).compute().pool)


def test_real_normal_ratio_collision_has_factor_target_and_numeric_counterexample():
    rs = rules()
    gaps = ratio_default_gaps(rs)
    assert len(gaps) == 1 and gaps[0][0] == 'variables.factor'
    message = gaps[0][1]
    assert message.startswith('方案「Synthetic」 variables.factor：ratio_default.collision')
    assert '原分子=100' in message and '原分母=100' in message
    assert '1.2' in message and '默认系数1' in message
    assert 'alias -> metric' in message
    assert compute(rs) == Decimal('1.00')
    rs.variables['factor'].expr = 'tier_ratio(C, amount, budget, 1)'
    assert compute(rs) == Decimal('1.20')
    assert not ratio_default_gaps(rs) and not ratio_default_warnings(rs)


def test_collision_is_not_a_hardcoded_one_or_coefficient():
    rs = rules('tier_ratio(C, alias, 1, 0.8, missing: alias == 0.5)',
               metric='ratio(amount, budget, zero: 0.5)')
    rs.tables['C'].steps = [(Decimal(0), Decimal('.6')), (Decimal('.5'), Decimal('1.3'))]
    message = ratio_default_gaps(rs)[0][1]
    assert '原分子=50.0' in message and '返回1.3' in message and '默认系数0.8' in message
    assert compute(rs, amount=50) == Decimal('.8')


def test_declared_rate_fallback_does_not_excuse_normal_value_collision():
    rs = rules()
    rs.variables['factor'].ratio_default = 'rate'
    rs.variables['factor'].sources = [SourceRef(doc='rules.md', quote='缺失时达成率按100%继续查档')]
    assert 'ratio_default.collision' in ratio_default_gaps(rs)[0][1]


@pytest.mark.parametrize('expr', [
    'tier_ratio(C, amount, budget, 1, missing: count(of:"objects") == 0)',
    'tier_ratio(C, alias, 1, 1, missing: budget == 0)',
    'tier_ratio(C, alias, 1, 1, missing: budget == 0 or count(of:"objects") == 0)',
    'tier_ratio(C, alias, 1, 1, missing: alias == 1 and budget == 0)',
    'if(budget == 0, 1, tier_ratio(C, alias, 1, 1))',
    'if(budget != 0, tier_ratio(C, alias, 1, 1), 1)',
    'if(not (budget == 0), tier_ratio(C, alias, 1, 1), 1)',
    'if(alias != 1, tier_ratio(C, alias, 1, 1, missing: alias == 1), 1)',
    'tier_ratio(C, alias, 1, 1, missing: true)',
])
def test_original_denominator_or_explicit_branch_protection_is_respected(expr):
    rs = rules(expr)
    rs.variables['factor'].sources = [SourceRef(doc='rules.md', quote='分母为0时，最终系数默认为1')]
    assert not ratio_default_gaps(rs)
    assert not ratio_default_warnings(rs)


@pytest.mark.parametrize('metric,expr', [
    ('ratio(0, budget, zero: 1)', 'tier_ratio(C, alias, 1, 1, missing: alias == 1)'),
    ('ratio(amount, 0, zero: 1)', 'tier_ratio(C, alias, 1, 1, missing: alias == 1)'),
    ('ratio(20, 10, zero: 1)', 'tier_ratio(C, alias, 1, 1, missing: alias == 1)'),
    ('ratio(amount, budget, zero: 1)',
     'if(budget == 0 and amount > 0, tier_ratio(C, alias, 1, 1, missing: alias == 1), 1)'),
])
def test_unreachable_or_unproven_normal_witness_does_not_become_a_blocker(metric, expr):
    rs = rules(expr, metric=metric)
    rs.variables['factor'].sources = [SourceRef(doc='rules.md', quote='分母为0时，最终系数默认为1')]
    assert not ratio_default_gaps(rs)
    # The missing predicate handles the fallback even when no normal witness was established.
    assert not ratio_default_warnings(rs)


@pytest.mark.parametrize('metric,expr', [
    ('ratio(amount * 2, amount, zero: 1)', 'tier_ratio(C, alias, 1, 1, missing: alias == 1)'),
    ('ratio(abs(amount), amount, zero: 2)', 'tier_ratio(C, alias, 1, 1, missing: alias == 2)'),
    ('ratio(sum(amount, of:"objects"), sum(budget, of:"objects"), zero: 1)',
     'tier_ratio(C, alias, 1, 1, missing: alias == 1)'),
    ('ratio(amount, budget, zero: 1)',
     'if(amount > 1000, tier_ratio(C, alias, 1, 1, missing: alias == 1), 1)'),
])
def test_unproved_transforms_or_aggregate_constraints_get_only_symbolic_warning(metric, expr):
    rs = rules(expr, metric=metric)
    assert not ratio_default_gaps(rs)
    warning = ratio_default_warnings(rs)[0][1]
    assert 'ratio_default.collision_risk' in warning
    assert '原分子=' not in warning and '原分母=' not in warning


def test_count_cannot_supply_a_fractional_row_witness():
    rs = rules('tier_ratio(C, alias, 1, 1, missing: alias == 0.5)',
               metric='ratio(count(of:"objects"), 1, zero: 0.5)')
    assert not ratio_default_gaps(rs) and not ratio_default_warnings(rs)


def test_fixed_nonzero_denominator_uses_its_actual_value_in_witness():
    rs = rules(metric='ratio(amount, 250, zero: 1)')
    message = ratio_default_gaps(rs)[0][1]
    assert '原分子=250' in message and '原分母=250' in message


def test_final_coefficient_zero_policy_is_required_for_blocking_shadowed_fallback():
    rs = rules('tier_ratio(C, alias, 1, 1, missing: count(of:"objects") == 0)')
    factor = rs.variables['factor']
    factor.sources = [SourceRef(doc='rules.md', quote='查不到记录时系数指定为1')]
    assert not ratio_default_gaps(rs)
    warning = ratio_default_warnings(rs)[0][1]
    assert 'ratio_default.shadowed' in warning and '仅提示风险' in warning
    assert warning in compile_warnings(rs)
    # We demonstrate the information loss without inventing the business policy for it.
    assert compute(rs, budget=0) == Decimal('1.2')
    factor.sources = [SourceRef(doc='rules.md', quote='当月预算为0时，该项系数指定为1')]
    assert 'ratio_default.shadowed' in ratio_default_gaps(rs)[0][1]
    assert not ratio_default_warnings(rs)


@pytest.mark.parametrize('quote', [
    '预算为100时，系数默认为1',
    '预算为0.1时，系数默认为1',
    '查不到预算时，系数默认为1',
    '分母为0时保留错误记录。查不到记录时系数默认为1',
    '分母为0时，系数默认口径待确认',
    '预算为0时不应把系数固定为1',
    '预算为0时，是否将系数指定为1？',
])
def test_missing_only_or_unrelated_source_is_not_a_zero_coefficient_policy(quote):
    rs = rules('tier_ratio(C, alias, 1, 1)')
    rs.variables['factor'].sources = [SourceRef(doc='rules.md', quote=quote)]
    assert not ratio_default_gaps(rs)
    assert ratio_default_warnings(rs)


def test_intentional_rate_policy_and_equal_coefficient_do_not_generate_shadowed_warning():
    rs = rules('tier_ratio(C, alias, 1, 1)')
    rs.variables['factor'].ratio_default = 'rate'
    assert ratio_default_warnings(rs)  # A bare declaration is insufficient.
    rs.variables['factor'].sources = [SourceRef(doc='rules.md', quote='缺失时达成率按100%继续查档')]
    assert not ratio_default_gaps(rs) and not ratio_default_warnings(rs)
    rs = rules(coefficient='1')
    assert not ratio_default_gaps(rs) and not ratio_default_warnings(rs)


def test_unused_shadowed_draft_does_not_spam_compile_warning_and_cycles_terminate():
    rs = rules('tier_ratio(C, alias, 1, 1)')
    rs.pool.expr = 'amount'
    assert not any('ratio_default.shadowed' in item for item in compile_warnings(rs))
    rs.variables['metric'].expr = 'alias'
    assert not ratio_default_gaps(rs) and not ratio_default_warnings(rs)


@pytest.mark.evidence
def test_local_formula_issues_target_actual_factor_before_amount_review(tmp_path):
    from bonus_agent.agent import Workspace
    from bonus_agent.auto import build_inventory
    from bonus_agent.construction import TaskBoard
    from bonus_agent.llm import FakeLLM

    policy = tmp_path / 'rules.md'
    policy.write_text('按原分子分母查阶梯；缺失时系数为1。', encoding='utf8')
    ws = Workspace(FakeLLM([]), build_inventory([policy]))
    rs = rules()
    ws.datasets = rs.datasets
    ws.drafts['S'] = rs.model_dump(mode='json', exclude={
        'name', 'version', 'effective_from', 'effective_to', 'datasets', 'tests'})
    ws.plan_formula_tasks([{'id': 'factor_task', 'scheme': 'S', 'target': '/variables/factor'}])
    board = TaskBoard(ws)
    issues = board.issues(board.task_spec('factor_task'))
    assert any('variables.factor' in str(item) and 'ratio_default.collision' in str(item)
               for item in issues), issues
    assert not ws.llm.calls
