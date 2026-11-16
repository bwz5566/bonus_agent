"""Local budget policy and provider context errors are separate recovery paths."""
import json
import time
from types import SimpleNamespace

import pytest

from bonus_agent.agent import run_agent
from bonus_agent.context import ContextBudgetError, estimate_tokens, guard_messages
from bonus_agent.llm import FakeLLM, LLMError


def no_advisor(messages):
    pytest.fail('Context management must not call a business advisor')


def test_chinese_utf8_bytes_are_not_a_token_count():
    messages=[{'role':'user','content':'核算规则'*9000}]
    llm=FakeLLM([]);llm.ctx=1048576
    assert len(json.dumps(messages,ensure_ascii=False).encode('utf8'))>96000
    assert guard_messages(messages,llm)<96000
    assert estimate_tokens(messages)<len(json.dumps(messages,ensure_ascii=False).encode('utf8'))


@pytest.mark.parametrize('mode',['native','json'])
def test_oversized_fixed_prompt_stops_locally_without_advisor_or_repeat(tmp_path,mode):
    p=tmp_path/'policy.md';p.write_text('核算制度：比例按材料。',encoding='utf8')
    llm=FakeLLM([{'tool':'note','args':{'topic':'late','text':'not called'}}])
    llm.workflow_mode='staged';llm.ctx=1048576;llm.max_input_tokens=100
    llm.tool_mode=mode;llm.recovery_mode='advisor';llm.think_llm=FakeLLM(no_advisor)
    result=run_agent(llm,[p],tmp_path/'out',max_steps=5)
    assert '固定系统指令' in result.error and '本地上下文预算' in result.error
    assert not llm.calls and not llm.think_llm.calls and not result.steps
    assert result.deep_steps==0 and result.status=='FAILED'
    assert (tmp_path/'out/case.json').exists()


def test_local_history_compaction_keeps_material_index_and_avoids_pro(tmp_path,monkeypatch):
    import bonus_agent.context as context
    p=tmp_path/'policy.md';p.write_text('核算规则与材料原文。'*400,encoding='utf8')
    real_guard=context.guard_messages
    blocked=[False]
    def controlled(messages,llm,**kw):
        if not blocked[0] and any(m.get('role')=='assistant' for m in messages):
            blocked[0]=True
            raise ContextBudgetError('本地上下文预算：测试历史整理')
        return real_guard(messages,llm,**kw)
    monkeypatch.setattr(context,'guard_messages',controlled)
    llm=FakeLLM([
        {'tool':'read_document','args':{'file':p.name}},
        {'tool':'plan_calculation','args':{'summary':'核算','outputs':['核算表'],'files':{p.name:'rule'},'schemes':['S']}},
        {'tool':'note','args':{'topic':'continued','text':'from source index'}},
    ])
    llm.workflow_mode='staged';llm.recovery_mode='advisor';llm.think_llm=FakeLLM(no_advisor)
    result=run_agent(llm,[p],tmp_path/'out',max_steps=4)
    assert blocked[0] and result.ws.notes.get('continued')=='from source index'
    assert any(s.tool=='（本地上下文整理）' for s in result.steps)
    assert not llm.think_llm.calls
    assert result.ws._flow().has_plan()


def test_provider_context_rejection_recovers_without_business_advice(tmp_path):
    class Provider(FakeLLM):
        attempts=0
        def chat_tools(self,*args,**kw):
            self.attempts+=1
            if self.attempts==1:raise LLMError('maximum context length exceeded')
            return super().chat_tools(*args,**kw)
    p=tmp_path/'policy.md';p.write_text('核算制度。',encoding='utf8')
    llm=Provider([{'tool':'note','args':{'topic':'recovered','text':'yes'}}])
    llm.recovery_mode='advisor';llm.think_llm=FakeLLM(no_advisor)
    result=run_agent(llm,[p],tmp_path/'out',max_steps=2)
    assert result.ws.notes['recovered']=='yes'
    assert any(s.tool=='（上下文恢复）' for s in result.steps)
    assert not llm.think_llm.calls


def test_calibration_reads_request_usage_not_cumulative_job_usage():
    llm=FakeLLM([]);llm.usage.prompt_tokens=1000000
    messages=[{'role':'user','content':'核算公式'}]
    first=guard_messages(messages,llm)
    response=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5000,completion_tokens=10,prompt_tokens_details=None))
    # FakeLLM intentionally has no transport; exercise the real usage-accounting method.
    from bonus_agent.llm import LLM
    LLM._usage(llm,response,time.monotonic())
    assert llm.last_call['actual_input_tokens']==5000
    assert first<guard_messages(messages,llm)<96000


def test_local_reviewer_budget_does_not_consume_review_attempts(tmp_path):
    from tests.test_staged_workflow import make_workspace,prepared
    ws=prepared(make_workspace(tmp_path));ws.calculate_basis('S')
    ws.llm.checker_llm.max_input_tokens=100
    assert ws.review_basis('S')['status']=='ERROR'
    assert not ws.workflow['attempts']
    assert not ws.llm.checker_llm.calls
