import json

import pytest

from bonus_agent.agent import Step, ToolError
from bonus_agent.data_profile import archived_category_probe, category_probe, repeated_category_probe
from bonus_agent.progress import discover
from bonus_core.data.period import Period
from tests.test_staged_workflow import make_workspace


def category_workspace(tmp_path, extra=0, *, blank=False):
    rule = '渠道为North时，产品线为特约的归入Special。'

    def add_data(wb):
        sheet = wb.create_sheet('events')
        sheet.append(['编号', '渠道', '产品线', '期间', '纳入'])
        for idx, (channel, line, month, keep) in enumerate([
                ('North', '普通', '202604', '是'), ('North', '特约', '202604', '是'),
                ('West（直）', '特约', '202604', '是'), (None if blank else 'North', '普通', '202604', '是'),
                ('旧渠道', '普通', '202603', '是'), ('已剔除', '普通', '202604', '否')]):
            sheet.append([str(idx), channel, line, month, keep])
        for idx in range(extra):
            sheet.append([str(100 + idx), f'渠道{idx:02}', '普通', '202604', '是'])

    ws = make_workspace(tmp_path, mutate_workbook=add_data, policy_tail='\n' + rule)
    ws.record_clauses([{'file': 'policy.md', 'quote': rule, 'point': '按渠道及产品线归类'}])
    ws.define_dataset('events', 'data.xlsx', 'events', {
        'eid': {'column': '编号', 'type': 'id'}, 'channel': {'column': '渠道', 'type': 'text'},
        'line': {'column': '产品线', 'type': 'text'}, 'month': {'column': '期间', 'type': 'text'},
    }, key='eid', period='month', filter={'纳入': ['是']})
    ws.period = Period.parse('2026-04')
    return ws


def test_distinct_probe_returns_exact_categories_and_points_to_the_classification_rule(tmp_path):
    ws = category_workspace(tmp_path)
    args = {'dataset': 'events', 'expr': 'count_distinct(channel, where: channel == "Special")'}
    answer = ws.query_dataset(**args)
    assert answer['value'] == '0'
    field, = answer['field_evidence']
    assert field['field'] == 'channel' and field['values_complete']
    assert field['source'] == {'file': 'data.xlsx', 'sheet': 'events', 'column': '渠道'}
    assert [(v['value'], v['count'], v['rows']) for v in field['values']] == [
        ('North', 3, [2, 3]), ('West（直）', 1, [4])]
    assert field['null'] == 0 and answer['rows'] == 4
    assert [c['quote'] for c in field['related_clauses']] == ['渠道为North时，产品线为特约的归入Special。']
    seen = set()
    assert discover(Step('query_dataset', args, json.dumps(answer), True), seen)
    args['expr'] = 'count_distinct(channel, where: channel == "another") + count_distinct(channel)'
    other = ws.query_dataset(**args)
    assert other['value'] == '2' and not discover(Step('query_dataset', args, json.dumps(other), True), seen)
    cross = ws.query_dataset('events', 'count_distinct(line, where: channel == "North")')
    assert cross['value'] == '2' and {f['field'] for f in cross['field_evidence']} == {'channel', 'line'}
    assert not ws.llm.calls


def test_field_pages_are_bounded_exact_and_independent_of_aggregate_where(tmp_path):
    ws = category_workspace(tmp_path, extra=17, blank=True)
    reply = ws.query_dataset('events', values_of='channel')
    values = []
    while True:
        field, = reply['field_evidence']
        assert not field['values_complete'] and field['null'] == 1
        values.extend(v['value'] for v in field['values'])
        if field['next_call'] is None:
            break
        reply = ws.query_dataset(**field['next_call']['args'])
    assert len(set(values)) == len(values) == 19
    for kwargs in ({'values_of': 'unknown'}, {'values_of': 'channel', 'offset': -1},
                   {'values_of': 'channel', 'expr': 'count_distinct(channel)'}):
        with pytest.raises(ToolError):
            ws.query_dataset('events', **kwargs)
    total = ws.query_dataset('events', 'count(of: "events")')
    assert total['value'] == '21'
    assert 'field_evidence' not in total
    with pytest.raises(ToolError, match='value_empty'):
        ws.query_dataset('events', 'count_distinct(channel)')


def test_frequency_evidence_only_supersedes_same_field_membership_checks():
    def query(expr):
        return {'dataset': 'events', 'expr': expr}

    first = query('count_distinct(channel)')
    second = query('count_distinct(channel, where: "East" == channel) + count_distinct(channel)')
    assert repeated_category_probe([first, second]) == ('events', 'channel')
    for independent in (
        'count_distinct(line, where: channel == "East")',
        'count_distinct(channel, where: channel != "East")',
        'sum(amount, where: channel == "East")',
        'count_distinct(channel, of: "archive")',
        'count_distinct(channel) + count_distinct(line)',
    ):
        assert category_probe(query(independent)) is None
    assert repeated_category_probe([first, {**second, 'dataset': 'archive'}]) is None


def test_resume_handoff_uses_only_successful_queries_from_the_matching_failed_round(tmp_path):
    last = {'n': 2, 'status': 'FAILED', 'error': 'construction:T7:/variables/rate'}
    query = {'tool': 'query_dataset', 'ok': True, 'args': {
        'dataset': 'events', 'expr': 'count_distinct(channel, where: channel == "East")'}}
    log = tmp_path / 'history' / '第2轮' / 'agent_log.json'
    log.parent.mkdir(parents=True)

    def write(error, steps):
        log.write_text(json.dumps({'error': error, 'steps': steps}), encoding='utf-8')

    write(last['error'], [query, query])
    assert archived_category_probe(tmp_path, last) == ('events', 'channel')
    assert archived_category_probe(tmp_path, {**last, 'status': 'DONE'}) is None
    assert archived_category_probe(tmp_path, {**last, 'n': 1}) is None
    write('construction:T8:/variables/next', [query, query])
    assert archived_category_probe(tmp_path, last) is None
    write(last['error'], [query, {**query, 'ok': False}])
    assert archived_category_probe(tmp_path, last) is None
    write(last['error'], [query, {**query, 'tool': 'read_workspace'}])
    assert archived_category_probe(tmp_path, last) is None
