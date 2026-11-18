"""Final-only report rows must not invalidate an unchanged approved basis."""
from decimal import Decimal

from bonus_agent.recovery import fingerprint
from tests.test_staged_workflow import make_workspace, reviewed


def test_final_reports_extend_but_do_not_replace_confirmed_basis(tmp_path):
    ws = make_workspace(tmp_path)
    source = ws.drafts['S']['pool']['sources']
    report = ws.drafts['S']['report']
    item = {'name': '分配记录金额', 'level': '参与', 'expr': 'amount * share * 0.1',
            'stage': 'distribution', 'sources': source}
    report['details'].append(dict(item))
    report['totals'].append({**item, 'name': '分配记录合计'})
    fact = ws.facts[0]
    ws.bind_facts([{'name': fact.name, 'targets': fact.targets +
                   ['S::/report/details/1/expr', 'S::/report/totals/1/expr']}])
    reviewed(ws)
    assert set(ws.workflow['basis']['S']['details']) == {'项目奖'}
    ws._flow().approve('S', '财务', fingerprint(ws.workflow['basis']['S']))
    ws.trial_run('S')
    result = ws.results['S']['R0']
    assert set(result.details) == {'项目奖', '分配记录金额'}
    assert Decimal(dict(result.totals)['分配记录合计']) == 30
    assert {p['name']: Decimal(p['final']) for p in result.people} == {'甲': Decimal(20), '乙': Decimal(10)}
