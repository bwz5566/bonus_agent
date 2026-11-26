"""Inventory real case rounds without replaying models or opening expected results.

Terminal cause is counted once per round. A loop/budget stop is distinguished
from the earlier business/technical fault; unresolved causes remain unknown.
The detailed ledger is local because error strings can contain business keys.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def cause(error):
    e = str(error or '')
    groups = [
        ('人工停止', r'手动停止|用户停止|已取消'),
        ('接口/用量未知', r'用量未确认|Connection error|APIConnectionError|APITimeoutError|ReadTimeout|HTTP (?:429|5\d\d)|调用失败'),
        ('时间/步数/token上限', r'最大步数|tokens.*(?:达到|上限)|作业预算事前拦截|总时间上限|请求时间事前拦截|预算用尽'),
        ('上下文/模型协议', r'上下文.*(?:超限|不足)|没有 JSON 对象|finish_reason=length|模型多次没有调用工具|team\.protocol|没有工具 thought|参数不对|读取范围须为|没有方案「共用」'),
        ('阶段工具/审批依赖', r'需要先用 set_rules 设定 allocation|construction\.dependency|已声明方案与计划不一致|先检查依赖任务'),
        ('数据粒度/键/行范围', r'parent_unknown|level_no_entity|leaf_no_entity|value_empty|在同一编号下取值不同|一个都对不上|link 字段|key 字段|不在工作表|表里有 \d+ 列都叫|construction\.data_dependency|agg_unrelated|scope_bad|fn_of|agg_dataset|detail_outside_agg'),
        ('参数来源/单位/区间/绑定', r'parameter\.|algorithm\.source|参数证据绑定|参数名里的|construction\.binding|不在引用原文|没有登记任何新参数|缺少精确参数绑定|缺少精确规则位置'),
        ('公式/分配/报表构建', r'branch_empty|eval_error|验算式里的|construction:|交付缺口|奖金池任务|report\.balances|report\.distributions|no_levels'),
        ('金额独立复核不一致', r'应得对不上|奖金池汇总不一致|逐对象奖金池|金额不一致|独立验算.*不一致'),
        ('重复/无进展（终止症状）', r'卡住|停滞|重复|没有进展|no-op|连续.*失败|未完成'),
    ]
    for group, pattern in groups:
        if re.search(pattern, e, re.I | re.S):
            return group
    return '其他/缺少具体诊断'


def audit(root: Path):
    rows, missing_logs, unreadable = [], [], []
    for case_dir in sorted(root.iterdir()):
        if not case_dir.is_dir():
            continue
        try:
            c = read(case_dir / 'out' / 'case.json')
            meta = read(case_dir / 'meta.json') if (case_dir / 'meta.json').exists() else {}
        except (OSError, ValueError) as exc:
            unreadable.append({'case': case_dir.name, 'error': str(exc)})
            continue
        for rnd in c.get('rounds', []):
            n = rnd.get('n')
            status, error = rnd.get('status'), rnd.get('error', '')
            log_path = case_dir / 'out' / 'history' / f'第{n}轮' / 'agent_log.json'
            if not log_path.exists() and rnd is c['rounds'][-1]:
                log_path = case_dir / 'out' / 'agent_log.json'
            last_bad, observed, failed_tools = '', set(), collections.Counter()
            try:
                log = read(log_path)
                for step in (log.get('steps', []) if isinstance(log, dict) else log):
                    result = step.get('result', '')
                    if step.get('ok') is False or re.search(r'"status"\s*:\s*"FAIL"', str(result)[:500]):
                        failed_tools[step.get('tool', '?')] += 1
                        last_bad = str(result)
                        cat = cause(last_bad)
                        if cat not in ('其他/缺少具体诊断', '重复/无进展（终止症状）'):
                            observed.add(cat)
            except (OSError, ValueError):
                missing_logs.append(f'{case_dir.name}/第{n}轮')
            primary = cause(error) if status == 'FAILED' else ''
            underlying = cause(last_bad) if last_bad else ''
            u = rnd.get('usage') or {}
            rows.append(dict(case=case_dir.name, title=meta.get('name', meta.get('title', case_dir.name)),
                             round=n, started=rnd.get('at', ''), status=status, model=rnd.get('model', '未记录'),
                             code=rnd.get('code', ''), terminal=primary, last_bad_category=underlying,
                             tokens=(u.get('prompt_tokens') or 0)+(u.get('completion_tokens') or 0),
                             calls=u.get('calls') or 0, seconds=u.get('secs') or 0,
                             steps=u.get('steps') or 0, failed_tools=dict(failed_tools),
                             observed_categories=sorted(observed), error=error, last_bad=last_bad[:2500],
                             log=str(log_path)))
    failures = [r for r in rows if r['status'] == 'FAILED']
    return dict(cases=len({r['case'] for r in rows}), rounds=len(rows),
                states=dict(collections.Counter(r['status'] for r in rows)),
                terminal=dict(collections.Counter(r['terminal'] for r in failures).most_common()),
                observed=dict(collections.Counter(c for r in rows for c in r['observed_categories']).most_common()),
                failed_tokens=sum(r['tokens'] for r in failures), total_tokens=sum(r['tokens'] for r in rows),
                model_calls=sum(r['calls'] for r in rows), elapsed_seconds=sum(r['seconds'] for r in rows),
                missing_logs=missing_logs, unreadable=unreadable, records=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    audit_result = audit(args.cases)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out/'failure_history.json').write_text(json.dumps(audit_result, ensure_ascii=False, indent=2), encoding='utf-8')
    rows = audit_result['records']
    if rows:
        with (args.out/'failure_history.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps({k:v for k,v in audit_result.items() if k != 'records'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
