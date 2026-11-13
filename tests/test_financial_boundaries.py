"""Monetary boundary regressions: real engine, acceptance, confirmation and durable state."""
import copy
import json
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
import pytest
import openpyxl
import yaml
from bonus_core.dsl.models import RuleSet
from bonus_core.data.table import from_records
from bonus_core.engine.run import compute
from bonus_core.engine.ledger import ledger_entries, release
from bonus_core.engine.issues import EngineError
from bonus_agent.compare import compare_expected
from bonus_agent import case
from bonus_agent.validation import result_integrity


def rules(**parts):
    return RuleSet.model_validate({'name':'audit','version':'v1',
        'datasets':{'people':{'key':'eid','fields':{k:{'type':t} for k,t in
          [('eid','id'),('amount','money'),('a','ratio'),('b','ratio'),('prepay','money')]}}},
        'pool':{'expr':'amount','scope':'person'},'allocation':[{'level':'person','key':'eid'}],**parts})


def data(rs, a='.8', b='.8', prepay=0, amount=100):
    return from_records(rs,{'people':[{'eid':'A','amount':amount,'a':a,'b':b,'prepay':prepay}]})


def test_compound_hold_release_is_capped_and_idempotent_after_posting():
    rs=rules(payout=[{'id':'A','scope':'person','hold_ratio':'a'},{'id':'B','scope':'person','hold_ratio':'b'}])
    entries=ledger_entries(compute(rs,data(rs)))
    later=data(rs,a=0)
    rows=release(rs,entries,later)
    assert sum(D(e['release_now']) for e in rows)==D(20)
    for e in rows: e['released']=str(D(e['released'])+D(e['release_now']))
    assert sum(D(e['release_now']) for e in release(rs,rows,later))==0
    assert sum(D(e['release_now']) for e in release(rs,rows,data(rs,a=0,b=0)))==80
    with pytest.raises(EngineError,match='不完整'):
        release(rs,entries[:1],later)
    with pytest.raises(EngineError,match='重复'):
        release(rs,entries+entries,later)


def test_release_keeps_earned_frozen_and_applies_prepay():
    rs=rules(payout=[{'id':'hold','scope':'person','hold_ratio':'a'}],
             offsets=[{'id':'prepay','scope':'person','expr':'prepay'}])
    entries=ledger_entries(compute(rs,data(rs,a=1,prepay=70)))
    # Sales amount changes to 999, historical earned must remain 100.
    rows=release(rs,entries,data(rs,a=0,prepay=70,amount=999))
    assert sum(D(e['release_now']) for e in rows)==30
    assert sum(D(e['offset_now']) for e in rows)==70 and sum(D(e['still_held']) for e in rows)==0
    for e in rows:e['released']=str(D(e['released'])+D(e['release_now']))
    assert sum(D(e['release_now']) for e in release(rs,rows,data(rs,a=0,prepay=70)))==0
    with pytest.raises(EngineError,match='抵扣金额减少'):
        release(rs,rows,data(rs,a=0,prepay=0))


def test_release_uses_changed_condition_and_rejects_edited_rules():
    rs=rules(payout=[{'id':'A','scope':'person','hold_ratio':'a'},{'id':'B','scope':'person','hold_ratio':'b'}])
    entries=ledger_entries(compute(rs,data(rs)))
    rows=release(rs,entries,data(rs,b=0))
    assert {e['condition']:D(e['release_now']) for e in rows}=={'A':D(0),'B':D(20)}
    changed=rs.model_copy(deep=True);changed.payout[0].hold_ratio='0'
    with pytest.raises(EngineError,match='规则内容'):
        release(changed,entries,data(changed,b=0))
    titled=rs.model_copy(deep=True);titled.name='已确认方案'
    assert sum(D(e['release_now']) for e in release(titled,entries,data(titled,b=0)))==20


def test_release_applies_person_offsets_to_complete_frozen_group():
    rs=RuleSet.model_validate({'name':'group','version':'v1',
      'datasets':{'people':{'key':'eid','fields':{'eid':{'type':'id'},'prepay':{'type':'money'}}},
                  'records':{'key':'rid','fields':{'rid':{'type':'id'},'eid':{'type':'id'},'amount':{'type':'money'},'h':{'type':'ratio'}}}},
      'pool':{'expr':'amount','scope':'record'},'allocation':[{'level':'record','key':'rid'}],
      'payout':[{'id':'h','scope':'record','hold_ratio':'h'}],
      'offsets':[{'id':'prepay','scope':'people','expr':'prepay'}]})
    original={'people':[{'eid':'A','prepay':70}],
              'records':[{'rid':'R1','eid':'A','amount':50,'h':1},{'rid':'R2','eid':'A','amount':50,'h':0}]}
    entries=ledger_entries(compute(rs,from_records(rs,original)))
    later=copy.deepcopy(original);later['records'][0].update(h=0,amount=999)
    released=release(rs,entries,from_records(rs,later))
    assert sum(D(e['release_now']) for e in released)==30
    assert sum(D(e['offset_now']) for e in released)==20
    assert sum(D(e['still_held']) for e in released)==0


def test_acceptance_checks_extra_recipients_and_explicit_partial_mode(tmp_path):
    rs=rules(); values=from_records(rs,{'people':[{'eid':'A','amount':100},{'eid':'B','amount':999}]})
    res=compute(rs,values)
    p=tmp_path/'expected.xlsx';wb=openpyxl.Workbook();wb.active.append(['人员编号','期望应得金额']);wb.active.append(['A',100]);wb.save(p);wb.close()
    matches=compare_expected({'S':{'R0':res}},p)
    assert matches[0].extra==['B'] and not matches[0].ok
    assert compare_expected({'S':{'R0':res}},p,complete=False)[0].ok
    matches=compare_expected({'S':{'R0':res,'R1':res}},p)
    assert any(not m.ok and m.ambiguous for m in matches if m.revision=='R1')


def saved_case(tmp_path):
    out=tmp_path/'out';out.mkdir();p=tmp_path/'data.xlsx';wb=openpyxl.Workbook()
    wb.active.append(['人员编号','金额','预发']);wb.active.append(['A',100,20]);wb.save(p);wb.close()
    rs=rules(offsets=[{'id':'prepay','scope':'person','expr':'prepay'}])
    # Only mapped fields participate; remove unused fixture attributes.
    rs.datasets['people'].fields={k:v for k,v in rs.datasets['people'].fields.items() if k in ['eid','amount','prepay']}
    rp,mp=out/'rules.yaml',out/'mapping.yaml';rp.write_text(rs.model_dump_json(),encoding='utf8')
    mp.write_text(yaml.safe_dump({'people':{'file':p.name,'sheet':'Sheet','header_rows':[1],
                            'columns':{'eid':'人员编号','amount':'金额','prepay':'预发'}}},allow_unicode=True),encoding='utf8')
    res=compute(rs,from_records(rs,{'people':[{'eid':'A','amount':100,'prepay':20}]}));results={'默认':{'R0':res}}
    snap={'rounds':[{'n':1,'status':'OK','inputs':[str(p)],'files':{p.name:case.sha256(p)},
        'outputs':{'rules':[rp.name,case.sha256(rp)],'mapping':[mp.name,case.sha256(mp)]},
        'digest':case.digest(results),'financial_digest':case.financial_digest(results)}]}
    case.save_case(out,snap)
    return out,p,snap


def test_confirmation_rejects_changed_inputs_and_full_money_baseline(tmp_path):
    out,p,snap=saved_case(tmp_path)
    wb=openpyxl.load_workbook(p);wb.active.cell(2,3).value=80;wb.save(p);wb.close()
    with pytest.raises(case.ConfirmError,match='输入版本'):
        case.confirm(out,'reviewer',review=False)
    # Even if a persisted input fingerprint matches, the frozen paid baseline must catch a difference.
    snap['rounds'][0]['files'][p.name]=case.sha256(p);case.save_case(out,snap)
    with pytest.raises(case.ConfirmError,match='不一致'):
        case.confirm(out,'reviewer',review=False)
    assert not (out/'template').exists()


def test_confirmation_accepts_unchanged_complete_baseline(tmp_path):
    out,_,_=saved_case(tmp_path)
    assert (case.confirm(out,'reviewer',review=False)/'manifest.yaml').exists()


def test_negative_rollup_never_creates_negative_deduction():
    rs=RuleSet.model_validate({'name':'negative','version':'v1',
      'datasets':{'people':{'key':'eid','fields':{'eid':{'type':'id'}}},
                  'records':{'key':'rid','fields':{'rid':{'type':'id'},'eid':{'type':'id'},'amount':{'type':'money'}}}},
      'pool':{'expr':'amount','scope':'record','negative':'allow'},'allocation':[{'level':'record','key':'rid'}],
      'offsets':[{'id':'prepay','scope':'people','expr':'0'}]})
    res=compute(rs,from_records(rs,{'people':[{'eid':'A'}],'records':[{'rid':'R','eid':'A','amount':-100}]}))
    r=next(iter(res.rollups.values()))
    assert D(r.paid)==-100 and D(r.deducted)==D(r.overpaid)==0
    assert result_integrity(res).ok
    r.deducted='-100';r.paid='0';r.overpaid='100'
    assert not result_integrity(res).ok


def test_interrupted_save_preserves_prior_and_corruption_recovers(tmp_path):
    case.save_case(tmp_path,{'rounds':[{'n':1}]})
    from bonus_agent.infra import atomic
    original=atomic.os.replace
    def fail_current(src,dst):
        if Path(dst).name=='case.json': raise OSError('interrupted replacement')
        return original(src,dst)
    with patch.object(atomic.os,'replace',fail_current):
        with pytest.raises(OSError):case.save_case(tmp_path,{'rounds':[{'n':2}]})
    assert case.load_case(tmp_path)['rounds'][0]['n']==1
    (tmp_path/'case.json').write_text('{broken',encoding='utf8')
    assert case.load_case(tmp_path)['rounds'][0]['n']==1
    with patch('bonus_agent.infra.checkpoints.latest_case',return_value={'rounds':[{'n':9}]}):
        assert case.load_case(tmp_path)['rounds'][0]['n']==9
