"""A failed read/reminder is not a new failure of the rule quoted in its next step."""
import json

import pytest

from bonus_agent.recovery import RecoveryJournal, fingerprint, native_repair_feedback


# The Round9 duplicate-read prefix and diagnostic tail, with the intervening
# unrelated binding gaps shortened. Neither belongs to the read's own error.
ROUND9_REPLAY = (
    '这次调用与第 11 步完全相同，内容没有变化，已跳过。不要重复调用；需要更多内容请换参数（如 start_row、offset），'
    '否则根据已有信息进行下一步。程序判断的下一步：当前版本已编译未通过，先修复反例，不要原样重编译：'
    '规则材料.pptx::P2.T1.R2.C2.L1：parameter.unit，原文裸数 25 与公式中的百分数系数相差100倍。'
    '先核对该表单位；完整检查：read_workspace(section="checks")'
)


@pytest.mark.parametrize('text', [ROUND9_REPLAY,
    '内容和上面说明里【之前读过、内容没变的工作区内容】附的那段完全一样。下一步：parameter.unit，旧问题',
    '引用：parameter.binding：这是材料中的说明',
    '历史诊断：parameter.unit：旧单位问题',
    '错误：找不到指定文件。下一步：parameter.unit：旧单位问题',
    '错误：文件不支持。\n引用原文：parameter.binding：不要绑定',
    '错误：文档内容为「parameter.unit：用户提供的示例」',
])
def test_repeated_reads_and_quoted_guidance_do_not_create_native_repair(text):
    assert native_repair_feedback('read_workspace', {'section': 'rules', 'name': 'cust_factor'}, text) is None
    journal = RecoveryJournal(None, {})
    journal.observe_native_feedback('read_workspace', {'name': 'cust_factor'}, text, False)
    assert not journal.native_guidance


@pytest.mark.parametrize('code', ['parameter.binding', 'parameter.type', 'parameter.unit', 'parameter.range',
                                  'parameter.ref', 'parameter.order', 'construction.task', 'source.cell', 'tool.contract'])
def test_explicit_program_errors_remain_current_and_ignore_trailing_old_objects(code):
    result = native_repair_feedback('bind_facts', {'bindings': [{'name': 'A'}, {'name': 'B'}]},
                                   f'错误：{code}：A当前失败。\n下一步：parameter.unit：B历史问题')
    assert result['code'] == code and result['objects'] == ['A']
    assert 'B历史问题' not in result['issue']


def test_structured_compile_reads_only_current_failure_fields():
    result = native_repair_feedback('compile_formulas', {}, json.dumps({
        'status': 'FAIL', 'phase': 'basis',
        'issues': ['表格.pptx::P2.T1.R2.C2.L1：parameter.unit，单位需确认'],
        'next': 'construction.task：旧任务错误', 'quote': 'source.cell：原文示例',
        'history': [{'code': 'parameter.binding'}],
    }, ensure_ascii=False) + '\n下一步：parameter.range：旧范围问题')
    assert result['code'] == 'parameter.unit'
    assert result['issue'].startswith('表格.pptx::P2.')
    assert '旧任务' not in result['issue'] and '原文示例' not in result['issue']


@pytest.mark.parametrize('report', [
    {'status': 'FAIL', 'issues': [], 'next': 'parameter.unit：需要修复'},
    {'status': 'PASS', 'issues': ['parameter.unit：已修复'], 'next': 'parameter.binding'},
    {'section': 'checks', 'items': [{'message': 'parameter.unit：旧检查'}]},
    {'status': 'FAIL', 'issues': ['引用：parameter.binding：材料示例']},
    {'status': 'FAIL', 'issues': 1, 'next': 'source.cell'},
])
def test_structured_non_diagnostic_fields_cannot_be_promoted(report):
    assert native_repair_feedback('read_workspace', {}, json.dumps(report, ensure_ascii=False)) is None


def test_structured_partial_and_parameter_validation_diagnostics_still_route():
    partial = {'status': 'PARTIAL', 'facts': [{'quote': 'parameter.unit：引用'}],
               'failed': ['R2C3：source.cell：坐标无效']}
    assert native_repair_feedback('record_parameter_cells', {}, json.dumps(partial, ensure_ascii=False))['code'] == 'source.cell'
    validation = {'status': 'FAIL', 'gaps': ['F：parameter.unit，单位未确认'], 'errors': []}
    assert native_repair_feedback('validate_parameters', {}, json.dumps(validation, ensure_ascii=False))['code'] == 'parameter.unit'
    typed = {'status': 'FAIL', 'issues': [{'code': 'parameter.binding.value_mismatch', 'message': 'F不一致',
                                         'quote': 'construction.task：旧错误'}]}
    assert native_repair_feedback('submit_formula_task', {}, json.dumps(typed, ensure_ascii=False))['code'] == 'parameter.binding'


@pytest.mark.parametrize('text', ['错误：参数不是合法 JSON：unexpected text parameter.unit',
                                 '错误：参数不对：需要 name。下一步：parameter.unit：旧错误',
                                 '错误：没有工具 made_up。可用：bind_facts'])
def test_argument_failures_still_route_to_program_repair(text):
    assert native_repair_feedback('read_workspace', {}, text)['code'] == 'tool.arguments'


def test_same_context_migration_removes_only_proven_replay_and_quote_records(tmp_path):
    path = tmp_path / 'recovery.json'
    state = {'context': fingerprint('same'), 'failures': {'existing': {'attempts': 2}},
             'advised': ['existing'], 'halted': '', 'native_repairs': {
                 'fake': {'tool': 'read_workspace', 'code': 'parameter.unit', 'objects': ['cust_factor'], 'issue': ROUND9_REPLAY},
                 'quote': {'tool': 'read_document', 'code': 'parameter.unit', 'objects': [], 'issue': '引用：parameter.unit：旧材料'},
                 'binding': {'tool': 'bind_facts', 'code': 'parameter.binding', 'objects': ['A'], 'issue': 'parameter.binding：A目标无效'},
                 'compile': {'tool': 'compile_formulas', 'code': 'parameter.unit', 'objects': [],
                             'issue': json.dumps({'status': 'FAIL', 'issues': ['A：parameter.unit：单位无效']})},
                 'source': {'tool': 'record_parameter_cells', 'code': 'source.cell', 'objects': [], 'issue': 'R2C3：source.cell：坐标错误'},
                 'unknown': {'tool': 'update_dataset', 'code': 'unmatched_basis', 'objects': ['D'], 'issue': '旧版未结构化错误'},
             }}
    path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf8')
    resumed = RecoveryJournal(path, 'same')
    assert set(resumed.state['native_repairs']) == {'binding', 'compile', 'source', 'unknown'}
    assert resumed.state['failures'] == state['failures'] and resumed.state['advised'] == state['advised']
    assert set(json.loads(path.read_text(encoding='utf8'))['native_repairs']) == set(resumed.state['native_repairs'])
