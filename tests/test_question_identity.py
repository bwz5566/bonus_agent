"""Questions cannot turn a sampled identity guess or invented citation into a business decision."""
import pytest

from bonus_agent.agent import ToolError
from bonus_agent.question_match import same_pending
from bonus_agent.extract_rules import Question
from bonus_agent.skills.link_suggest import suggest
from bonus_core.data.table_detect import SheetTable
from test_t3_fixes import _ws


def test_paraphrased_pending_question_reuses_id_without_merging_changed_premises(tmp_path):
    ws=_ws(tmp_path)
    a='销售单「客户编码」列存在两种写法：客户简称甲既有 7001（第2-4行）也有 7002（第5行起），而「送达方客户」列均为 7002。项目编号由月份+客户编码+型号组成，请问用哪一列？'
    b=a.replace('存在两种写法','有两种写法').replace('请问用哪一列','用哪一列')
    ws.ask_question(a,['原列','另一列'])
    assert '已经问过（q1）' in ws.ask_question(b,['原列不变','另一列'])
    assert len(ws.questions)==1
    ws.ask_question(b.replace('7002','7003'),['原列','另一列'])
    assert len(ws.questions)==2
    assert not same_pending(Question(id='q3',text=a,related='A'),b,'B')
    assert not same_pending(Question(id='q4',text=a,withdrawn='旧问题已撤回'),b,'')
    assert not same_pending(Question(id='q5',text=a,answer='已确认'),b,'')


def test_coordinate_evidence_is_read_from_real_cells_and_cannot_certify_invented_quote(tmp_path):
    ws=_ws(tmp_path)
    ev={'file':'数据.xlsx','sheet':'销售','row':2,'columns':['客户','金额']}
    ws.ask_question('核算字段以哪个为准？',['客户','金额'],suggested=0,evidence=[ev])
    proof=ws.questions[0].evidence[0]
    assert proof['ok'] and proof['quote']=='客户=C1 | 金额=100' and proof['row']==2
    for changed in [{'row':100},{'columns':['不存在']},{'quote':'客户=C2 | 金额=100'}]:
        with pytest.raises(ToolError,match='引用未核对通过'):
            ws.ask_question('新的引用问题'+str(changed),['是','否'],suggested=0,evidence=[{**ev,**changed}])
    assert len(ws.questions)==1


def test_same_kind_alternatives_are_compared_as_complete_keys_even_when_single_values_tie():
    def sheet(name,headers,rows):
        return SheetTable('data.xlsx',name,1,headers,[(i+2,r) for i,r in enumerate(rows)])
    base=sheet('sales',['日期','客户编码','送达方客户','产品型号'],[
        ['202612','C71','C72','M1'],['202612','C72','C71','M2']])
    ref=sheet('roster',['日期','客户编码','产品型号'],[
        ['202612','C71','M1'],['202612','C72','M2']])
    text=suggest(base,ref,top=50)
    combos=[line for line in text.splitlines() if '↔' in line]
    assert any('日期+客户编码+产品型号' in line and '2/2' in line for line in combos)
    assert any('日期+送达方客户+产品型号' in line and '0/2' in line for line in combos)
    assert '不构成别名证据' in text
