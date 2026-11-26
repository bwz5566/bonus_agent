"""卡住时给财务一个具体问题：问什么、选项、怎么回答；程序自身的问题不问财务。"""
import json
from types import SimpleNamespace

import openpyxl
import pytest

from bonus_agent import stuck_question as sq
from bonus_agent.agent import run_agent
from bonus_agent.failure import to_text
from bonus_agent.llm import FakeLLM


def _sales(tmp_path):
    p = tmp_path / "sales.xlsx"
    wb = openpyxl.Workbook()
    s = wb.active
    s.title = "sales"
    s.append(["客户编码", "客户简称", "金额"])
    for row in [["1001", "甲(海外)", 1], ["1002", "甲(海外)", 2], ["2001", "乙", 3]]:
        s.append(row)
    wb.save(p)
    return p


def test_stuck_on_a_data_conflict_becomes_a_concrete_question(tmp_path):
    p = _sales(tmp_path)
    bad = {"name": "客户", "file": p.name, "sheet": "sales", "key": "short", "distinct": True,
           "fields": {"code": {"column": "客户编码", "type": "id"}, "short": {"column": "客户简称", "type": "text"}}}
    script = [{"tool": "define_dataset", "args": bad} for _ in range(4)] + [{"tool": "note", "args": {"topic": "t", "text": "x"}}]
    out = tmp_path / "out"
    ar = run_agent(FakeLLM(script), [p], out, max_steps=8, reflect=0)
    assert ar.status == "NEEDS_CONFIRMATION" and not ar.error           # 不再是“失败 + 补充材料、明确答复”
    q = SimpleNamespace(**ar.pending[0])
    assert "「客户简称」对应多个不同的「客户编码」" in q.text and "甲(海外)" in q.text
    assert q.options[0].startswith("合并") and q.options[-1].startswith("其他") and q.blocking
    assert '甲(海外)' in q.diagnosis['identified'] and '客户编码' in q.diagnosis['missing']
    assert any(s.tool == "（向财务提问）" for s in ar.steps)
    from bonus_agent.web import _round_status
    text = _round_status(ar)
    assert q.id in text and "{" not in text and "A. 合并" in text and "怎么回答" in text and "运行下一轮" in text and "补充材料" not in text
    assert all(label in text for label in ('涉及项目或指标', '已识别内容', '缺少或待确认', '影响'))


def test_model_writes_the_question_for_other_business_stalls():
    llm = FakeLLM([])
    llm.stuck = json.dumps({"program_issue": False, "question": "退货冲销的金额要从当月提成基数里扣掉吗？",
                            "options": ["扣：按负数计入当月", "不扣：退货另行处理"], "why": "规则没写退货"})
    ws = SimpleNamespace(datasets={}, mapping={})
    q = sq.build(ws, llm, "智能体卡住了：set_rules 连续 3 次失败，最后一次：错误：规则里退货怎么处理没有依据", [])
    assert q["text"].startswith("退货冲销") and q["options"][-1] == sq.OTHER
    llm.stuck = json.dumps({"program_issue": False, "question": "update_dataset 的 cust_code 怎么改？", "options": ["a", "b"]})
    assert "材料里这一点是怎么规定的" in sq.build(ws, llm, "智能体卡住了：set_rules 连续 3 次失败，最后一次：错误：某处不通过", [])["text"]   # 带程序术语：改用通用问法


def test_program_errors_do_not_ask_finance(tmp_path):
    assert sq.build(SimpleNamespace(datasets={}, mapping={}), FakeLLM([]),
                    "纠错预算耗尽（智能体卡住）：thought 同一问题累计失败 3 次：错误：没有工具 thought", []) is None
    text = to_text("纠错预算耗尽（智能体卡住），已保留工作区并停止自动接续：thought 同一问题累计失败 3 次：错误：没有工具 thought")
    assert "不需要你回答" in text and "需要补充材料" not in text


def test_staged_model_question_preserves_a_concrete_reason():
    from bonus_agent.web import render_diagnosis

    llm = FakeLLM([])
    ws = SimpleNamespace(staged=True, datasets={}, mapping={})
    reason = '智能体卡住了：set_rules 连续 3 次失败，最后一次：错误：退货计算口径缺少依据'
    payload = {'question': 'P09 项目的退货款计入哪个月？', 'options': ['按原销售月', '按退货入账月']}
    llm.stuck = json.dumps({**payload, 'diagnosis': dict.fromkeys(('item', 'identified', 'missing', 'impact'), '暂时无法判断')})
    assert sq.build(ws, llm, reason, []) is None
    details = {'item': 'P09 项目的退货扣减', 'identified': '销售行在4月，退货行在5月。',
               'missing': '需确认退货扣减按销售月或退货月。', 'impact': '影响4月和5月的提成基数。'}
    llm.stuck = json.dumps({**payload, 'diagnosis': details})
    question = sq.build(ws, llm, reason, [])
    assert question['diagnosis'] == details
    assert all(value in render_diagnosis(question['diagnosis']) for value in details.values())


def test_failure_reason_keeps_the_registered_task_and_missing_binding():
    from bonus_agent.failure import explain

    error = '纠错预算耗尽：' + json.dumps({'task': 'T20', 'scheme': 'S', 'target': '/variables/achievement',
                                       'issues': ['[阻断] scope_bad: 变量 achievement 的 scope「区域」未登记']}, ensure_ascii=False)
    ws = {'workflow': {'construction': {'tasks': {'T20': {'scheme': 'S', 'title': '区域达成率', 'clauses': ['C1'],
                                                        'target': '/variables/achievement'}}}},
          'clauses': [{'id': 'C1', 'file': '规则.md', 'quote': '以区域为单位核算'}]}
    for recorded in (error, error[:-2] + ', "截断的后续信息'):
        details = explain(recorded, workspace=ws)['diagnosis']
        assert details['item'] == 'S / 区域达成率 / T20'
        assert '《规则.md》' in details['identified'] and '建立并验证实际核算分组' in details['missing']
        assert '后续金额' in details['impact']
    stopped = '纠错预算耗尽：连续 13 次检查没有消除既有问题。construction:T20:/variables/achievement'
    details = explain(stopped, workspace=ws)['diagnosis']
    assert details['item'] == 'S / 区域达成率 / T20' and '13 次' in details['identified']
    assert '通过回执' in details['missing']


@pytest.mark.parametrize(('choice', 'amount'), [(0, '0.125'), (1, '12.5')])
def test_missing_cell_unit_becomes_one_answerable_question_and_resumes(tmp_path, choice, amount):
    from decimal import Decimal
    from pptx import Presentation
    from pptx.util import Inches
    from bonus_agent.facts import numeric

    p = tmp_path / '方案.pptx'
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(0, 0, Inches(3), Inches(1)).text = '甲组分红'
    table = slide.shapes.add_table(2, 2, 0, Inches(1), Inches(4), Inches(2)).table
    for i, row in enumerate([['角色', '权重'], ['协调员', '12.5']]):
        for j, value in enumerate(row):
            table.cell(i, j).text = value
    deck.save(p)
    args = {'facts': [{'name': '协调权重', 'value': '12.5', 'unit': '%', 'quote': '协调员 12.5',
                       'file': p.name, 'page': 1, 'context': '甲组分红',
                       'source_cell': {'table': 1, 'row': 2, 'column': 2},
                       'unit_basis': {'file': p.name, 'quote': '甲组分红'}}]}
    call = {'tool': 'record_facts', 'args': args}
    out = tmp_path / 'case'
    first = run_agent(FakeLLM([call]), [p], out, max_steps=4, reflect=0)
    assert not first.error and first.status == 'NEEDS_CONFIRMATION' and len(first.pending) == 1
    q = first.pending[0]
    assert '协调员' in q['text'] and '权重' in q['text'] and '12.5' in q['text'] and '0.125' in q['options'][0]
    assert q['evidence'][0]['ok'] and not first.ws.facts
    waiting = run_agent(FakeLLM([]), [p], out, max_steps=1, reflect=0)
    assert waiting.status == 'NEEDS_CONFIRMATION' and not waiting.error and waiting.usage['calls'] == 0
    resumed = run_agent(FakeLLM([call]), [p], out, answers=[{'id': q['id'], 'answer': choice}],
                        max_steps=1, reflect=0)
    assert not resumed.pending and len(resumed.ws.facts) == 1
    assert numeric(resumed.ws.facts[0]) == Decimal(amount)
