"""Recovery must survive restarts, preserve business evidence and never bypass verification."""
import json
from types import SimpleNamespace as NS

import pytest

from bonus_agent.agent import Workspace, ToolError, run_agent
from bonus_agent.auto import build_inventory
from bonus_agent.failure import retryable
from bonus_agent.llm import FakeLLM, LLM, LLMError
from bonus_agent.recovery import RecoveryJournal, RunBudget, local_advice


def policy(tmp_path):
    p = tmp_path / 'policy.md'
    p.write_text('金额范围200-750万元，费率3%。', encoding='utf8')
    return p


def bad_table():
    return {'tool': 'compile_table', 'args': {'name': 'A', 'rows': [{'value': 'missing'}]}}


def test_error_budget_survives_resumes_and_blocks_unchanged_retry(tmp_path):
    p = policy(tmp_path); out = tmp_path/'out'
    first = run_agent(FakeLLM([bad_table()]*2), [p], out, max_steps=2, deep=0)
    assert '达到最大步数' in first.error
    second = run_agent(FakeLLM([bad_table()]*2), [p], out, max_steps=2, deep=0)
    assert '累计失败 3 次' in second.error
    llm = FakeLLM([bad_table()])
    third = run_agent(llm, [p], out, max_steps=2, deep=0)
    assert third.error == second.error and not third.steps and llm._i == 0
    # Changed source evidence permits recovery, without deleting the old case history.
    p.write_text('金额范围200-750万元，费率4%。', encoding='utf8')
    resumed = run_agent(FakeLLM([bad_table()]), [p], out, max_steps=1, deep=0)
    assert '达到最大步数' in resumed.error


def test_failures_are_scoped_to_objects_and_advice_is_once(tmp_path):
    j = RecoveryJournal(tmp_path/'r.json', {'inputs': 'v1'})
    for name in ['A','B','C']:
        assert j.failure('compile_table', {'name':name}, '错误：parameter.ref') == 1
    assert not j.error
    assert j.claim_advice('A')
    assert not RecoveryJournal(tmp_path/'r.json', {'inputs':'v1'}).claim_advice('A')
    j.failure('compile_table', {'name':'A'}, '错误：parameter.ref')
    j.failure('compile_table', {'name':'A'}, '错误：parameter.ref')
    assert j.error and not retryable(j.error)
    assert not retryable('智能体卡住了，已停止')
    assert retryable('达到最大步数 5 仍未完成')


def test_shared_job_budget_does_not_reset_at_next_round():
    llm = FakeLLM([]); budget = RunBudget(0, 100, 0)
    llm.usage.prompt_tokens = 60
    assert not budget.check(llm)
    llm.usage.prompt_tokens += 45
    assert '总上限 100' in budget.check(llm)


def test_pro_diagnoses_without_taking_over_primary_tools(tmp_path):
    class Primary(FakeLLM):
        def chat_tools(self, messages, tools, **kw):
            assert not kw.get('think')
            return super().chat_tools(messages, tools, **kw)
    llm = Primary([bad_table()]*3)
    advisor = FakeLLM([json.dumps({'diagnosis':'missing source', 'next_action':'record_facts',
                                 'evidence_needed':'rate', 'acceptance':'exact binding'})])
    llm.recovery_mode = 'advisor'; llm.think_llm = advisor; advisor.usage = llm.usage
    result = run_agent(llm, [policy(tmp_path)], tmp_path/'out', max_steps=8)
    assert '累计失败 3 次' in result.error
    assert sum(s.tool == '（局部诊断）' for s in result.steps) == 1
    assert len(advisor.calls) == 1
    assert advisor.calls[0][0]['role'] == 'system'
    assert '可用工具' not in json.dumps(advisor.calls, ensure_ascii=False)


def test_advice_packet_is_bounded_and_does_not_contain_full_task():
    from bonus_agent.agent import Step
    seen = []
    llm = FakeLLM(lambda messages: seen.append(messages) or '{"diagnosis":"need source"}')
    local_advice(llm, 'error', [Step('set_rules', {'huge':'x'*50000}, 'bad', False)], [], 'check source')
    assert len(seen[0][-1]['content']) <= 18000


def test_local_diagnosis_budget_does_not_reduce_critical_review_thinking(monkeypatch):
    captured = []
    llm = LLM('pro','pro','http://localhost','', think_body={'thinking':{'type':'enabled'},'reasoning_effort':'high'},
              think_max_tokens=8192, think_timeout=300)
    def respond(self, messages, **kw):
        captured.append((self.think_body, self.think_max_tokens, self.think_timeout, kw))
        return {'diagnosis':'missing evidence'}
    monkeypatch.setattr(LLM, 'chat_json', respond)
    local_advice(llm, 'error', [], [], 'inspect source')
    body, tokens, timeout, kw = captured[0]
    assert body['reasoning_effort'] == 'low' and tokens == kw['max_tokens'] == 4096 and timeout == 180
    assert llm.think_body['reasoning_effort'] == 'high' and llm.think_max_tokens == 8192


def test_no_later_write_in_batch_can_bypass_recovery_stop(tmp_path):
    p=policy(tmp_path)
    batch=[bad_table(),bad_table(),bad_table(),{'tool':'note','args':{'topic':'late','text':'must not be saved'}}]
    result=run_agent(FakeLLM([batch]),[p],tmp_path/'out',max_steps=2,deep=0)
    assert '累计失败 3 次' in result.error
    assert 'late' not in result.ws.notes


@pytest.mark.parametrize('kind', ['chat','chat_tools'])
def test_truncated_model_output_never_reaches_executor(monkeypatch, kind):
    llm = LLM('test','served-model','http://localhost','')
    response = NS(id='response',model='actual-model',usage=None,
                  choices=[NS(finish_reason='length',message=NS(content='{}', tool_calls=[]))])
    client = NS(chat=NS(completions=NS(create=lambda **kw: response)))
    monkeypatch.setattr(LLM, '_client', lambda *a:client)
    with pytest.raises(LLMError, match='未执行工具'):
        if kind == 'chat': llm.chat([])
        else: llm.chat_tools([], [])
    assert llm.last_call['model'] == 'actual-model'
    assert llm.last_call['finish_reason'] == 'length'


def test_compile_origin_is_structural_but_business_threshold_requires_source(tmp_path):
    p = policy(tmp_path); ws = Workspace(FakeLLM([]), build_inventory([p]))
    ws.record_facts([{'name':'rate','value':'3%','file':p.name,'quote':'费率3%'}])
    ws.compile_table('A',[{'lower':'0','value':'rate'}],scheme='S')
    assert ws.drafts['S']['tables']['A']['steps'] == [['0','0.03']]
    before = json.dumps(ws.drafts, sort_keys=True)
    with pytest.raises(ToolError,match='parameter.ref.*steps/1/0'):
        ws.compile_table('A',[{'value':'rate'},{'lower':'25%','value':'rate'}],scheme='S')
    assert json.dumps(ws.drafts, sort_keys=True) == before


@pytest.mark.evidence
def test_missing_bindings_precede_trial_hint_and_phase_progress(tmp_path):
    import openpyxl
    p=policy(tmp_path); data=tmp_path/'data.xlsx'
    wb=openpyxl.Workbook(); wb.active.append(['id','amount']); wb.active.append(['A',100]); wb.save(data); wb.close()
    ws=Workspace(FakeLLM([]),build_inventory([p,data]))
    ws.define_dataset(name='items',file=data.name,sheet='Sheet',key='id',
                      fields={'id':{'column':'id','type':'id'}, 'amount':{'column':'amount','type':'money'}})
    ws.record_facts([{'name':'rate','value':'3%','file':p.name,'quote':'费率3%'}])
    ws.set_rules(scheme='S', tables={'A':{'kind':'tier','mode':'whole','steps':[[0,'.03']]}})
    for fact in ws.facts:
        fact.targets = []  # simulate a restored legacy rule with its evidence binding missing
    assert 'validate_parameters' in ws.next_hint(), (ws._binding_gaps(), ws.drafts, [f.model_dump() for f in ws.facts])
    assert '用 trial_run 试算' not in ws.next_hint()
