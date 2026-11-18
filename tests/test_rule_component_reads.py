"""Rules can be navigated by components and exact pointers without guessing text offsets."""
import copy
import json
from types import SimpleNamespace

import pytest

from bonus_agent.agent import ToolError
from bonus_agent.context import workspace_view
from bonus_agent.facts import Fact
from tests.test_staged_workflow import make_workspace


def follow(ws, entry):
    return ws.read_workspace(**entry['next_call']['args'])


def test_default_rules_index_reaches_exact_formula_and_existing_binding(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['variables'] = {'threshold': {'scope': 'global', 'expr': '18', 'sources': []}}
    ws.facts.append(Fact(name='threshold months', file='policy.md', quote='18 months', value='18',
                         targets=['S::/variables/threshold/expr']))
    page = ws.read_workspace('rules', scheme='S')
    assert 'item_fragment' not in page
    variables = next(i for i in page['items'] if i['name'] == 'variables')
    assert variables['target'] == 'S::/variables'
    children = follow(ws, variables)
    node = follow(ws, next(i for i in children['items'] if i['name'] == 'threshold'))['items'][0]
    assert node['definition']['expr'] == '18'
    assert node['binding_count'] == 1 and node['bindings'][0]['name'] == 'threshold months'
    leaf = ws.read_workspace('rules', name='S::/variables/threshold/expr')['items'][0]
    assert leaf['definition'] == '18' and leaf['target'] == 'S::/variables/threshold/expr'
    assert ws.read_workspace(**leaf['bindings'][0]['next_call']['args'])['items'][0]['targets']


def test_component_index_paginates_without_losing_nodes_or_returning_sources_as_fragments(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['variables'] = {f'v{i}': {'scope': 'global', 'expr': str(i),
        'sources': [{'doc': 'policy.md', 'quote': 'long source ' * 500}]} for i in range(80)}
    args = {'section': 'rules', 'scheme': 'S', 'name': '/variables', 'limit': 50}
    names = []
    while True:
        page = ws.read_workspace(**args)
        assert len(json.dumps(page, ensure_ascii=False)) < 6000 and 'item_fragment' not in page
        names += [item['name'] for item in page['items']]
        if page['next_call'] is None:
            break
        args = page['next_call']['args']
        assert 'detail_offset' not in args
    assert names == [f'v{i}' for i in range(80)]
    large_node = ws.read_workspace('rules', scheme='S', name='/variables/v3')
    assert 'item_fragment' not in large_node
    formula = follow(ws, next(i for i in large_node['items'] if i['name'] == 'expr'))
    assert formula['items'][0]['definition'] == '3'


def test_detail_true_preserves_complete_legacy_rules_fragment_stream(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['variables'] = {'large': {'expr': '1', 'desc': '\\"\n' * 9000}}
    args = {'section': 'rules', 'scheme': 'S', 'detail': True}
    fragments = []
    while True:
        page = ws.read_workspace(**args)
        assert len(json.dumps(page, ensure_ascii=False)) < 6000
        fragments.append(page['item_fragment']['text'])
        if not page['next_call']:
            break
        args = page['next_call']['args']
        assert args['detail'] is True
    assert json.loads(''.join(fragments))['rule'] == ws._merged('S')


def test_exact_pointers_preserve_escaped_names_and_read_array_cells(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['tables'] = {'a/b~c': {'kind': 'tier', 'steps': [[0, 0.1], [100, 0.2]]}}
    found = ws.read_workspace('rules', scheme='S', name='/tables/a~1b~0c/steps/1/1')
    assert found['items'][0]['definition'] == 0.2
    assert found['items'][0]['target'] == 'S::/tables/a~1b~0c/steps/1/1'


def test_pointer_scheme_conflicts_do_not_fall_back_to_another_scheme(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['Other'] = copy.deepcopy(ws.drafts['S'])
    with pytest.raises(ToolError, match='scheme不一致'):
        ws.read_workspace('rules', scheme='Other', name='S::/pool/expr')
    with pytest.raises(ToolError, match='方案不存在'):
        ws.read_workspace('rules', name='Missing::/pool/expr')
    with pytest.raises(ToolError, match='scheme不存在'):
        ws.read_workspace('rules', scheme='Missing', name='/pool/expr')
    ws.drafts['Other']['pool']['expr'] = 'wrong-other-formula'
    assert ws.read_workspace('rules', scheme='S', name='/pool/expr')['items'][0]['definition'] != 'wrong-other-formula'


def test_same_numbers_and_other_scheme_bindings_are_not_shown_as_existing_evidence(tmp_path):
    ws = make_workspace(tmp_path)
    ws.facts.append(Fact(name='unbound same value', file='policy.md', quote='10%', value='10%'))
    ws.facts.append(Fact(name='other scheme', file='policy.md', quote='10%', value='10%',
                         targets=['Other::/pool/expr']))
    found = ws.read_workspace('rules', scheme='S', name='/pool/expr')['items'][0]
    assert [f['name'] for f in found['bindings']] == ['提成率']


def test_missing_pointer_returns_targeted_parent_index_not_an_arbitrary_full_rule(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['variables'] = {'present': {'expr': '1'}}
    found = ws.read_workspace('rules', scheme='S', name='/variables/absent')
    assert found['status'] == 'NOT_FOUND' and not found['items']
    assert found['next_call']['args'] == {'section': 'rules', 'scheme': 'S', 'name': '/variables'}
    assert follow(ws, found)['items'][0]['name'] == 'present'


def test_existing_name_and_root_query_reads_keep_their_definitions(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['variables'] = {'small': {'scope': 'global', 'expr': '18'}}
    assert ws.read_workspace('rules', scheme='S', name='small')['items'][0]['definition']['expr'] == '18'
    assert ws.read_workspace('rules', scheme='S', query='allocation')['items'][0]['definition'] == ws.drafts['S']['allocation']
    assert ws.read_workspace('rules', name='S')['items'][0]['rule'] == ws._merged('S')
