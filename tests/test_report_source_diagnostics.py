"""Evidence failures point to editable report entries, never projected array positions."""
import copy
import json

import pytest

from bonus_agent.progress import compile_objects
from tests.test_staged_workflow import make_workspace


pytestmark = pytest.mark.evidence
VALID = {'doc': 'policy.md', 'quote': '每个项目按销售金额的10%计算奖金'}
INVALID = {'doc': 'policy.md', 'quote': '不存在的逐字引用'}


def entry(section, name='同名报告', sources=None, **extra):
    value = {'name': name, 'level': '项目', 'sources': [VALID, INVALID] if sources is None else sources}
    value.update({'total': 'amount * 0.1', 'parts': ['amount * 0.1']} if section == 'balances' else
                 {'total': 'amount * 0.1'} if section == 'distributions' else {'expr': 'amount * 0.1'})
    return {**value, **extra}


def issues(ws):
    return [message for message in ws._binding_gaps() if message.startswith('[阻断] source.')]


def next_call(message):
    return json.JSONDecoder().raw_decode(message.split('next_call=', 1)[1])[0]


@pytest.mark.parametrize('section', ['balances', 'pending', 'distributions', 'details'])
def test_quote_error_contains_real_section_source_index_and_executable_read(tmp_path, section):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['report'][section] = [entry(section)]
    errors = issues(ws)
    assert len(errors) == 1
    assert '[阻断] source.quote:' in errors[0]
    assert errors[0].endswith(f'(S::/report/{section}/0/sources/1)')
    call = next_call(errors[0])
    assert call['tool'] == 'read_workspace'
    result = ws.read_workspace(**call['args'])
    assert result['items'][0]['definition']['sources'][1] == INVALID
    assert not ws.llm.calls


@pytest.mark.parametrize('section', ['balances', 'pending', 'distributions', 'details'])
def test_missing_evidence_points_to_sources_collection_not_other_same_named_item(tmp_path, section):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['report'][section] = [entry(section, sources=[])]
    errors = issues(ws)
    assert len(errors) == 1 and '[阻断] source.required:' in errors[0]
    assert errors[0].endswith(f'(S::/report/{section}/0/sources)')
    assert next_call(errors[0])['args']['name'] == f'S::/report/{section}/0'


@pytest.mark.parametrize('section', ['balances', 'details'])
def test_basis_projection_maps_filtered_index_back_to_full_report(tmp_path, section):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['report'][section] = [entry(section, name='分配阶段', stage='distribution'),
                                        entry(section, name='基础阶段', stage='basis')]
    view = ws._flow().basis_view()
    assert len(getattr(view._ruleset('S').report, section)) == 1
    errors = issues(view)
    assert len(errors) == 1
    assert errors[0].endswith(f'(S::/report/{section}/1/sources/1)')
    assert next_call(errors[0])['args']['name'] == f'S::/report/{section}/1'
    result = ws.read_workspace(**next_call(errors[0])['args'])
    assert result['items'][0]['definition']['name'] == '基础阶段'


def test_ambiguous_projected_item_offers_only_real_container(tmp_path):
    ws = make_workspace(tmp_path)
    duplicate = entry('details')
    ws.drafts['S']['report']['details'] = [copy.deepcopy(duplicate), copy.deepcopy(duplicate)]
    errors = issues(ws._flow().basis_view())
    assert len(errors) == 2
    for message in errors:
        assert '无法唯一回溯' in message and message.endswith('(S::/report/details)')
        assert next_call(message)['args']['name'] == 'S::/report/details'
        result = ws.read_workspace(**next_call(message)['args'])
        assert len(result['items'][0]['definition']) == 2


def test_repeated_sources_keep_each_actual_index_and_stable_object_identity(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['report']['details'] = [entry('details', sources=[INVALID, INVALID])]
    errors = issues(ws._flow().basis_view())
    assert len(errors) == 2
    assert errors[0].endswith('(S::/report/details/0/sources/0)')
    assert errors[1].endswith('(S::/report/details/0/sources/1)')
    report = {'status': 'FAIL', 'phase': 'basis', 'issues': errors}
    objects = compile_objects(report)
    assert len(objects) == 2 and all('source.quote' in key for key in objects)
    report['issues'] = [message.replace('不存在的逐字引用', '不同诊断文案') for message in errors]
    assert compile_objects(report) == objects


def test_valid_report_evidence_still_passes_same_check(tmp_path):
    ws = make_workspace(tmp_path)
    assert issues(ws) == []
    assert issues(ws._flow().basis_view()) == []
