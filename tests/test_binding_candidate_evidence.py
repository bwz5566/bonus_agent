"""Binding feedback must not direct equal-valued policies to unrelated cells."""
from copy import deepcopy

from bonus_agent.facts import Fact, binding_errors


def drafts():
    return {'S': {
        'tables': {'unrelated': {'kind': 'tier', 'steps': [[0, 1], [5, 1], [8, 1]],
                                 'sources': [{'doc': 'policy.md', 'quote': '达成率达到档位时系数为1'}]}},
        'variables': {'default_value': {'expr': 'if(budget == 0, 1, calculated)',
                                        'sources': [{'doc': 'policy.md', 'quote': '缺少预算时最终系数指定为1'}]}}}}


def fact(**changes):
    return Fact(**({'name': '缺少预算默认值', 'value': '1', 'file': 'policy.md',
                    'quote': '缺少预算时最终系数指定为1'} | changes))


def test_grounded_variable_is_not_hidden_by_three_equal_tier_cells():
    rules = drafts()
    before = deepcopy(rules)
    f = fact()
    error = binding_errors([f], rules)[0]
    assert 'S::/variables/default_value/expr' in error
    assert '/tables/unrelated' not in error
    assert '同文件、原文引用重合' in error
    assert rules == before and not f.targets


def test_ambiguous_numeric_matches_do_not_become_repair_instructions():
    rules = drafts()
    rules['S']['variables']['default_value']['sources'] = []
    error = binding_errors([fact()], rules)[0]
    assert '4 处数值相同' in error
    assert '/tables/unrelated' not in error and '/variables/default_value' not in error
    assert 'read_workspace' in error
    assert 'section="mapping"' in error and '/mapping/数据表/filter/' in error


def test_same_quote_on_another_page_or_document_is_not_grounding():
    rules = drafts()
    source = rules['S']['variables']['default_value']['sources'][0]
    source['page'] = 2
    assert '同文件、原文引用重合' not in binding_errors([fact(page=3)], rules)[0]
    source['page'] = 3
    assert '同文件、原文引用重合' in binding_errors([fact(page=3)], rules)[0]
    source['doc'] = 'another.md'
    assert '同文件、原文引用重合' not in binding_errors([fact(page=3)], rules)[0]


def test_bare_equal_number_in_quotes_does_not_count_as_shared_evidence():
    rules = drafts()
    rules['S']['variables']['default_value']['sources'][0]['quote'] = '1'
    assert '同文件、原文引用重合' not in binding_errors([fact()], rules)[0]


def test_source_candidate_can_be_in_a_later_scheme_with_escaped_names():
    rules = drafts()
    value = rules['S']['variables'].pop('default_value')
    rules['later'] = {'variables': {'default/a~b': value}}
    error = binding_errors([fact()], rules)[0]
    assert 'later::/variables/default~1a~0b/expr' in error


def test_sole_numeric_candidate_remains_explicitly_unverified():
    rules = {'S': {'variables': {'x': {'expr': 'if(budget == 0, 1, calculated)'}}}}
    error = binding_errors([fact()], rules)[0]
    assert 'S::/variables/x/expr' in error and '尚未证明业务含义相同' in error


def test_unit_ambiguity_still_blocks_instead_of_suggesting_scaled_binding():
    f = fact(name='角色比例', value='25', quote='负责人分成比例25', context='分成比例')
    rules = {'S': {'tables': {'role': {'kind': 'lookup', 'mapping': {'owner': '.25'}}}}}
    error = binding_errors([f], rules)[0]
    assert 'parameter.unit' in error
    assert 'unit_basis' in error and '财务对话核对已有答复' in error
    assert not f.targets and f.unit is None
