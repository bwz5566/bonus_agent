"""Basis schemes keep their own evidence without masquerading as recipient reports."""
import copy
import json

import pytest

from bonus_agent.agent import ToolError
from test_settlement_rounding import recipient_model

pytestmark = [pytest.mark.verify, pytest.mark.reread, pytest.mark.evidence]


def funded_model(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    ws = recipient_model(tmp_path)
    source = [{'doc': 'data.xlsx', 'quote': 'amount'}]
    ws.set_rules(scheme='Basis', pool={'scope': 'orders', 'expr': 'amount'},
                 allocation=[{'level': 'orders', 'key': 'pid'}],
                 report={'totals': [{'name': 'Funding', 'level': 'orders', 'expr': 'amount'}],
                         'details': [{'name': 'orders', 'level': 'orders', 'expr': 'amount', 'sources': source}]})
    variables = copy.deepcopy(ws.drafts['S']['variables'])
    variables['project_funding'] = {'scope': 'orders', 'expr': 'scheme_paid("Basis")'}
    variables['recipient_total']['expr'] = 'sum(project_funding / order_count, of: "shares")'
    ws.set_rules(scheme='S', variables=variables)
    ws.trial_run()
    ws.report_required = True
    ws.llm.calc = json.dumps({'schemes': {
        'Basis': {'pool': 'amount', 'details': {'orders': 'amount'}},
        'S': {'pool': 'sum(amount / independently_counted, of: "shares")',
              'helpers': {'independently_counted': {'scope': 'orders', 'expr': 'count(of: "shares")'}},
              'people': {'mode': 'after_sum', 'key': 'person',
                         'expr': 'sum(amount / independently_counted, of: "shares")'},
              'details': {'orders': 'amount'}},
    }})
    return ws


def verify_all(ws):
    for scheme in ws.schemes:
        for node, kind, _ in ws._verify_targets(scheme):
            expr = ('amount' if scheme == 'Basis' else
                    'sum(amount / independently_counted, of: "shares")')
            ws.verify(node, expr, 'amount', scheme=scheme, amount=kind,
                      helpers={'independently_counted': {'scope': 'orders', 'expr': 'count(of: "shares")'}})


def test_finish_accepts_project_basis_and_final_recipients_with_all_validation(tmp_path):
    ws = funded_model(tmp_path)
    assert not ws._report_output_gaps()
    assert not ws.results['Basis']['R0'].people
    assert {p['name'] for p in ws.results['S']['R0'].people} == {'A', 'B'}
    verify_all(ws)
    assert ws.finish('Project amounts and recipient settlement verified', review=False) == '已完成'
    assert ws.validation_report()['status'] == 'PASS'


def test_basis_exemption_does_not_skip_independent_recipient_or_sample_validation(tmp_path):
    ws = funded_model(tmp_path)
    with pytest.raises(ToolError, match='独立验算还没全部通过'):
        ws.finish('Missing sample proof', review=False)
    ws = funded_model(tmp_path / 'missing_people')
    spec = json.loads(ws.llm.calc)
    del spec['schemes']['S']['people']
    ws.llm.calc = json.dumps(spec)
    with pytest.raises(ToolError, match='缺少人员汇总独立验算'):
        ws.finish('Missing recipient proof', review=False)


def test_single_basis_and_disconnected_extra_basis_still_need_people(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts.pop('S')
    ws.results.pop('S')
    assert any('Basis' in gap and 'report.people' in gap for gap in ws._report_output_gaps())
    ws = funded_model(tmp_path / 'other')
    ws.drafts['Disconnected'] = copy.deepcopy(ws.drafts['Basis'])
    ws.results['Disconnected'] = {'R0': copy.deepcopy(ws.results['Basis']['R0'])}
    assert any('Disconnected' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


def test_description_or_unexecuted_reference_cannot_exempt_a_basis(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['S']['variables']['project_funding']['expr'] = 'amount'
    ws.drafts['S']['variables']['project_funding']['desc'] = 'scheme_paid("Basis")'
    ws.trial_run()
    assert ('Basis', 'R0') in ws._deps('S')
    assert any('Basis' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


def test_transitive_executed_basis_chain_still_requires_a_recipient_endpoint(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['Bridge'] = copy.deepcopy(ws.drafts['Basis'])
    ws.drafts['Bridge']['pool']['expr'] = 'scheme_paid("Basis")'
    ws.drafts['S']['variables']['project_funding']['expr'] = 'scheme_paid("Bridge")'
    ws.trial_run()
    assert not ws._report_output_gaps()
    ws.drafts['S']['report']['people'] = None
    assert any('S' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


def test_report_only_reference_is_not_a_recipient_funding_dependency(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['S']['variables']['recipient_total']['expr'] = 'sum(amount / order_count, of: "shares")'
    ws.drafts['S']['report']['details'][0]['expr'] = 'project_funding'
    ws.trial_run()
    assert any('Basis/R0:orders:P' in trace.inputs for trace in ws.results['S']['R0'].traces.values())
    assert any('Basis' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


def test_contract_pending_is_a_real_funding_destination_but_standalone_display_is_not(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['S']['variables']['recipient_total']['expr'] = '0'
    ws.drafts['S']['report']['pending'] = [{'name': 'Waiting', 'level': 'global',
        'expr': 'sum(project_funding, of: "orders")', 'sources': [{'doc': 'data.xlsx', 'quote': 'amount'}]}]
    ws.drafts['S']['report']['distributions'][0]['pending'] = ['Waiting']
    ws.trial_run()
    assert not ws._report_output_gaps()
    ws.drafts['S']['report']['distributions'] = []
    ws.trial_run()
    assert any('Basis' in gap and 'report.people' in gap for gap in ws._report_output_gaps())


@pytest.mark.parametrize('missing', ['declaration', 'result'])
def test_exempt_basis_must_preserve_referenced_object_details(tmp_path, missing):
    ws = funded_model(tmp_path)
    if missing == 'declaration':
        ws.drafts['Basis']['report']['details'] = []
    else:
        ws.results['Basis']['R0'].details['orders'] = {}
    assert any('Basis' in gap and 'report.details' in gap for gap in ws._report_output_gaps())


def test_dependency_identity_preserves_separators_in_scheme_and_report_names(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['Basis/R:1'] = ws.drafts.pop('Basis')
    ws.results.pop('Basis')
    ws.drafts['Basis/R:1']['report']['details'][0]['name'] = 'objects/:detail'
    ws.drafts['S']['variables']['project_funding']['expr'] = 'scheme_paid("Basis/R:1")'
    ws.trial_run()
    assert not ws._report_output_gaps()


@pytest.mark.parametrize('people', [[], [{'name': 'Waiting', 'pending': True, 'count': 0}]])
def test_report_structure_does_not_invent_a_nonempty_recipient_business_requirement(tmp_path, people):
    ws = funded_model(tmp_path)
    ws.results['S']['R0'].people = people
    assert not ws._report_output_gaps()


def test_fabricated_recipient_identity_still_fails_independent_validation(tmp_path):
    ws = funded_model(tmp_path)
    ws.results['S']['R0'].people[0]['name'] = 'Fabricated'
    with pytest.raises(ToolError, match='人员.*集合'):
        ws.finish('Wrong recipient identity', review=False)


def test_each_scheme_keeps_declared_and_calculated_totals(tmp_path):
    ws = funded_model(tmp_path)
    ws.drafts['Basis']['report']['totals'] = []
    assert any('Basis' in gap and 'report.totals' in gap for gap in ws._report_output_gaps())
    ws = funded_model(tmp_path / 'other')
    ws.results['S']['R0'].totals = []
    assert any('S' in gap and 'totals' in gap for gap in ws._report_output_gaps())
