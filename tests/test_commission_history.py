from types import SimpleNamespace

import pytest

from bonus_agent.commission_history import (HistoryCoverageError, NO_PRIOR_ANSWER, check_history_coverage,
                                           history_confirmation, history_source_previews)
from bonus_agent.commission_sources import Sheet


def spec(sid):return {'sheet':sid,'header_row':1,'columns':{'month':'A','customer':'B','amount':'C'}}


def catalog_with(*tables):
    sheets={sid:Sheet(sid,f'{sid}.xlsx','data',[('月份','客户简称','实际销售金额'),*rows]) for sid,rows in tables}
    return SimpleNamespace(sheets=sheets,sheet=lambda s:sheets[s['sheet']])


def test_single_month_cannot_replace_actual_history_even_when_monthly_amounts_match():
    current=[('202509','a',100),('202509','b',200)]
    prior=[('202501','a',300),('202503','b',500)]
    c=catalog_with(('S21',current),('S89',prior+current),('S34',prior+current),('S12',[('202507','a',10),current[0]]))
    scope={'sales':spec('S21'),'gross':spec('S34'),'unpaid':spec('S12')}
    with pytest.raises(HistoryCoverageError,match='历史出货绑定不完整') as exc:
        check_history_coverage(c,spec('S21'),'202509',scope=scope)
    assert exc.value.replacement==spec('S89')
    proof=check_history_coverage(c,exc.value.replacement,'202509',scope=scope)
    assert proof['months']==['202501','202503','202509']
    assert proof['current_customers']==2
    assert any(r['mapping']==spec('S89') and r['months']==proof['months'] for r in history_source_previews(c,'202509'))


def test_partial_history_is_rejected_when_an_actual_strict_superset_reconciles():
    current=[('202509','a',100),('202509','b',200)]
    partial=[('202501','a',300),*current]
    complete=[('202503','b',500),*partial]
    c=catalog_with(('S21',current),('S22',partial),('S23',complete))
    with pytest.raises(HistoryCoverageError) as exc:
        check_history_coverage(c,spec('S22'),'202509',scope={'sales':spec('S21')})
    assert exc.value.replacement==spec('S23')


def test_conflicting_plausible_sources_do_not_authorize_an_automatic_rebinding():
    current=[('202509','a',100)]
    c=catalog_with(('S1',current),('S2',[('202501','a',123),*current]),('S3',[('202501','a',999),*current]))
    with pytest.raises(HistoryCoverageError) as exc:
        check_history_coverage(c,spec('S1'),'202509',scope={'sales':spec('S1')})
    assert exc.value.replacement is None


def test_missing_prior_months_requires_actual_scope_confirmation_and_not_a_guessed_zero():
    c=catalog_with(('S1',[('202509','a',100)]))
    with pytest.raises(HistoryCoverageError) as exc:
        check_history_coverage(c,spec('S1'),'202509',source_key='source-A')
    assert exc.value.finance and exc.value.question['example']
    q={'text':exc.value.question['text'],'related':'reference_mapping','answer':NO_PRIOR_ANSWER}
    assert history_confirmation([q],'202509','source-A')
    assert not history_confirmation([q],'202509','source-B')
    assert not history_confirmation([q],'202508','source-A')
    assert check_history_coverage(c,spec('S1'),'202509',allow_current_only=True)['months']==['202509']


def test_january_or_a_real_full_history_with_no_prior_sales_for_active_customer_is_valid():
    c=catalog_with(('S1',[('202501','a',10)]))
    assert check_history_coverage(c,spec('S1'),'202501')['months']==['202501']
    current=[('202509','new',100)]
    c=catalog_with(('S1',current),('S2',[('202501','former',20),*current]))
    with pytest.raises(HistoryCoverageError) as exc:
        check_history_coverage(c,spec('S1'),'202509')
    assert exc.value.replacement==spec('S2')
    assert check_history_coverage(c,spec('S2'),'202509')['months']==['202501','202509']
