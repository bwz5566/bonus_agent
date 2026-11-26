from types import SimpleNamespace

import pytest

from bonus_agent.agent import Workspace
from bonus_agent.facts import Fact
from bonus_agent.table_recovery import rejected_bound, replacement_bound


@pytest.mark.parametrize('change', [
    None, 'different_page', 'different_rate_cell', 'different_limit', 'different_rate',
    'missing_sibling', 'old_table_still_exists', 'different_file_version', 'different_owner',
])
def test_replaced_table_requires_current_cell_boundary_and_evidence(change):
    source = {'file': 'incentives.pptx', 'page': 3, 'table': 2}
    old = {'id': 'curve', 'scheme': 'sales', 'target': '/tables/combined', 'facts': ['base', 'renewal'], 'clauses': ['C9']}
    args = {'scheme': 'sales', 'name': 'combined', 'cells_from': source, 'rows': [{'lower': 'R4C1', 'value': 'R4C2'}]}
    issue = 'parameter.ref：第 1 档的 lower 用了 R4C1「60万以下」，这是上限写法'
    proof = rejected_bound(args, issue, old)
    assert proof and not rejected_bound({**args, 'rows': [{'lower': 'R4C1', 'value': 'R2C2'}]}, issue, old)
    first = {**old, 'target': '/tables/initial', 'facts': ['base']}
    tasks = {'curve': first, 'repeat': {**old, 'id': 'repeat', 'target': '/tables/renewed', 'facts': ['renewal']}}
    table = {'kind': 'tier', 'mode': 'excess', 'steps': [['0', '0.03'], ['600000', '0.04']]}
    materialized = {'initial': table}
    fact = Fact(name='base', file=source['file'], page=3, source_hash='source-v1', value='3%', quote='3%',
                source_cell={'table': 2, 'row': 4, 'column': 2}, targets=['sales::/tables/initial/steps/0/1'])
    if change == 'different_page':
        fact.page = 4
    elif change == 'different_rate_cell':
        fact.source_cell['column'] = 3
    elif change == 'different_limit':
        table['steps'][1][0] = '600001'
    elif change == 'different_rate':
        table['steps'][0][1] = '0.04'
    elif change == 'missing_sibling':
        del tasks['repeat']
    elif change == 'old_table_still_exists':
        materialized['combined'] = table
    elif change == 'different_file_version':
        fact.source_hash = 'stale'
    elif change == 'different_owner':
        tasks['unrelated'] = {**tasks.pop('curve'), 'id': 'unrelated'}
    ws = SimpleNamespace(facts=[fact], _merged=lambda _: {'tables': materialized}, _UPPER_BAND=Workspace._UPPER_BAND,
                         _source_version={source['file']: 'source-v1'},
                         _band_cells=lambda _: [('R4C1', '60万以下', None, True, 'limit')])
    board = SimpleNamespace(ws=ws, state={'tasks': tasks}, value=lambda _: table)
    item = {'scheme': 'sales', 'objects': ['combined'], 'rejected_bound': proof}
    assert replacement_bound(board, item) is (None if change else first)
