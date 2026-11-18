"""Binding failures identify the actual target problem without changing maths."""
from copy import deepcopy

import pytest

from bonus_agent.facts import Fact, binding_errors


def fact(target='S::/variables/factor/expr', **changes):
    return Fact(**({'name': '缺失毛利率时的默认系数', 'value': '1', 'file': 'policy.md',
                    'quote': '毛利率缺失时系数为1', 'targets': [target]} | changes))


def rules(expr):
    return {'S': {'variables': {'factor': {'scope': 'Project', 'expr': expr}}}}


def test_valid_expression_missing_literal_explains_actual_ast_and_repair_order():
    expr = 'if(gross_margin < 0.15, 0.8, 1.1)'
    draft = rules(expr)
    draft['S']['tables'] = {'unrelated': {'kind': 'lookup', 'mapping': {'other': 1}}}
    f = fact()
    before = deepcopy((f.model_dump(), draft))
    error = binding_errors([f], draft)[0]
    assert 'parameter.binding.expr_literal_missing' in error
    assert '值不一致' in error and '目标表达式有效' in error
    assert expr in error
    assert "实际AST数值常量=['0.15', '0.8', '1.1']" in error
    assert 'expr支持直接绑定AST数值常量' in error
    assert '先set_rules修改再bind_facts' in error
    assert '不要寻找不存在的threshold节点' in error
    assert '/tables/unrelated' not in error
    assert (f.model_dump(), draft) == before


def test_explicit_default_branch_can_bind_its_number_without_extra_threshold_field():
    expr = 'if(count(of: "margin") == 0, 1, tier_rate(margin_table, actual_margin))'
    assert not binding_errors([fact()], rules(expr))


def test_variable_reference_does_not_masquerade_as_an_inline_constant():
    draft = rules('fallback')
    draft['S']['variables']['fallback'] = {'expr': '1', 'scope': 'Project'}
    error = binding_errors([fact()], draft)[0]
    assert '实际AST数值常量=[]' in error
    assert '读取并绑定该变量的真实定义' in error
    assert not binding_errors([fact('S::/variables/fallback/expr')], draft)


def test_missing_path_shows_real_parent_fields_instead_of_claiming_expr_unsupported():
    draft = rules('if(age > 18, 1, 0)')
    error = binding_errors([fact('S::/variables/factor/threshold', value='18个月')], draft)[0]
    assert 'parameter.binding.path_missing' in error and '位置不存在' in error
    assert '最深已存在节点 /variables/factor' in error
    assert "已有字段=['scope', 'expr']" in error
    assert 'expr中的数值常量可直接绑定' in error
    assert not binding_errors([fact(value='18个月')], draft)


def test_missing_scheme_is_distinct_from_existing_formula_value_mismatch():
    error = binding_errors([fact('missing::/variables/factor/expr')], rules('1'))[0]
    assert '方案「missing」不存在' in error
    assert "现有方案：['S']" in error
    assert 'expr_literal_missing' not in error


@pytest.mark.parametrize('target_value', [{'expr': '1'}, [1], None, True])
def test_containers_and_unsupported_types_are_not_accepted_for_equal_nested_value(target_value):
    draft = {'S': {'custom': {'target': target_value}}}
    error = binding_errors([fact('S::/custom/target')], draft)[0]
    assert 'parameter.binding.target_type' in error
    assert '位置不存在' not in error
    if isinstance(target_value, dict):
        assert '该对象有expr字段' in error


def test_confirmed_unit_is_retained_and_scaled_value_is_reported_without_requestioning():
    f = fact('S::/tables/roles/mapping/owner', value='25', unit='%',
             unit_basis={'file': 'finance.md', 'quote': '角色比例25表示25%'})
    before = f.model_dump()
    draft = {'S': {'tables': {'roles': {'kind': 'lookup', 'mapping': {'owner': '0.2'}}}}}
    error = binding_errors([f], draft)[0]
    assert 'parameter.binding.value_mismatch' in error and '值不一致' in error
    assert '期望数值=0.25' in error and '当前标量=0.2' in error and '单位=%' in error
    assert '保留已确认单位' in error and 'ask_question' not in error
    assert f.model_dump() == before
    draft['S']['tables']['roles']['mapping']['owner'] = '0.25'
    assert not binding_errors([f], draft)


def test_precise_scalar_paths_remain_supported_in_mapping_overlay():
    draft = {'S': {'mapping': {'sales/main': {'filter': {'amount': {'not': ['0']}}}}}}
    assert not binding_errors([fact('S::/mapping/sales~1main/filter/amount/not/0', value='0')], draft)
    error = binding_errors([fact('S::/mapping/sales~1main/filter/amount/not/3', value='0')], draft)[0]
    assert '最深已存在节点 /mapping/sales~1main/filter/amount/not' in error
    assert '已有索引=0..0' in error


def test_syntax_error_is_not_reported_as_a_missing_literal_or_missing_path():
    error = binding_errors([fact()], rules('if('))[0]
    assert 'parameter.binding.expr_invalid' in error
    assert "当前expr='if('" in error
    assert 'expr_literal_missing' not in error and 'path_missing' not in error


def test_number_in_string_literal_does_not_satisfy_numeric_parameter():
    error = binding_errors([fact()], rules('if(role == "1", 2, 3)'))[0]
    assert "实际AST数值常量=['2', '3']" in error
    assert 'expr_literal_missing' in error


def test_unqualified_target_keeps_existing_cross_scheme_contract():
    f = fact('/variables/factor/expr')
    draft = rules('1')
    draft['other'] = {'variables': {}}
    assert not binding_errors([f], draft)
    draft['other']['variables']['factor'] = {'expr': '2'}
    error = binding_errors([f], draft)[0]
    assert '方案「other」' in error and '值不一致' in error
    assert '方案「S」' not in error


@pytest.mark.parametrize(('value', 'expr'), [('已通过', 'status == "已通过"'), ('2027-09-30', 'date("2027-09-30")')])
def test_existing_text_and_date_literal_binding_remains_supported(value, expr):
    assert not binding_errors([fact(value=value)], rules(expr))
