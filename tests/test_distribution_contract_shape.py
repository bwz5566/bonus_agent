from bonus_core.dsl.validator import validate, validate_basis
from tests.test_staged_workflow import make_workspace


def codes(ws):
    return {i.code for i in validate(ws._ruleset('S')) if i.severity == 'blocking'}


def test_project_pool_cannot_claim_recipient_sum_rounding(tmp_path):
    ws = make_workspace(tmp_path)
    contract = ws.drafts['S']['report']['distributions'][0]
    contract['rounding'] = 'after_sum'
    assert 'recipient_rounding_scope' in codes(ws)
    contract['level'] = 'global'
    assert 'recipient_rounding_scope' in codes(ws)  # Changing only the label still cannot change rounding.
    assert not any(i.code == 'recipient_rounding_scope' for i in validate_basis(ws._ruleset('S')))


def test_total_label_is_not_a_pending_definition(tmp_path):
    ws = make_workspace(tmp_path)
    ws.drafts['S']['report']['distributions'][0]['pending'] = ['项目奖合计']
    assert 'distribution_pending_missing' in codes(ws)


def test_pending_sources_and_duplicate_claims_are_checked(tmp_path):
    ws = make_workspace(tmp_path)
    report = ws.drafts['S']['report']
    report['pending'] = [{'name': '待分配', 'level': '项目', 'expr': '0'}]
    report['distributions'][0]['pending'] = ['待分配', '待分配']
    assert {'distribution_pending_source', 'distribution_pending_duplicate'} <= codes(ws)


def test_valid_per_pool_contract_is_unchanged(tmp_path):
    ws = make_workspace(tmp_path)
    assert not codes(ws)
