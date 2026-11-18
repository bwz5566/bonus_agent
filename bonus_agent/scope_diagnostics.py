"""Bounded observations of declared populations; never choose accounting policy."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path


def scope_diagnostics(ws, *, scheme=None, data=None, focus=None, limit=3):
    """Compare effective keys, including different source files, without mutation.

    Exact key coverage deliberately does not claim that spelling variants are
    unrelated business objects. Nor does an absent detail imply that an entity
    must be excluded: population and missing-data policy require source evidence.
    """
    from bonus_core.data.population import prepare

    try:
        name = ws._scheme(scheme)
        rs = ws._ruleset(name)
        mapping_issues = []
        if data is None:
            data, mapping_issues = ws._data(rs)
        mapping_errors = [str(issue) for issue in mapping_issues if issue.severity == 'blocking']
        if mapping_errors:
            return {'status': 'UNAVAILABLE', 'errors': mapping_errors[:2],
                    'note': '数据映射存在阻断错误，不能把部分读取结果当作完整范围或空表。'}
        effective, issues = prepare(rs, data)
        blocking = [str(issue) for issue in issues if issue.severity == 'blocking']
        if blocking:
            return {'status': 'UNAVAILABLE', 'errors': blocking[:2],
                    'note': '有效数据准备未通过，不能把未读到的数据当成空表。'}
        return _compare(ws, rs, effective, name, focus, max(1, min(3, int(limit))))
    except Exception as exc:
        # Diagnostic failure must preserve the original compile failure, not
        # replace it with an apparently empty population or an approval.
        return {'status': 'UNAVAILABLE', 'error': f'{type(exc).__name__}: {exc}'[:500],
                'note': '范围诊断未完成；这不是没有相关数据，也不是已确认缺失政策。'}


def _compare(ws, rs, data, scheme, focus, limit):
    owners = defaultdict(list)
    for name, spec in rs.datasets.items():
        if spec.key:
            owners[spec.key].append(name)
    cache = {}

    def keys(name, field):
        identity = (name, field)
        if identity not in cache:
            cache[identity] = {row.values.get(field) for row in data[name].rows
                               if row.values.get(field) not in (None, '')}
        return cache[identity]

    def source(name):
        spec, mapping = rs.datasets[name], ws.mapping[name]
        return {'file': Path(mapping.file).name if mapping.file else None,
                'sheet': mapping.sheet, 'period': spec.period, 'window': spec.window,
                'population_from': spec.population_from,
                'filter_columns': sorted(mapping.filter),
                'read_call': {'tool': 'read_workspace', 'args': {'section': 'mapping', 'name': name}}}

    comparisons = []
    for detail_name, detail in rs.datasets.items():
        candidates = owners.get(detail.link, [])
        if len(candidates) != 1 or detail_name not in data:
            continue
        parent = candidates[0]
        if parent == detail_name or parent not in data:
            continue
        parent_keys, detail_keys = keys(parent, detail.link), keys(detail_name, detail.link)
        absent, unmatched = parent_keys - detail_keys, detail_keys - parent_keys
        if not absent and not unmatched:
            continue
        retained = parent_keys & detail_keys
        examples = []
        seen = set()
        for row in data[parent].rows:
            key = row.values.get(detail.link)
            if key in absent and key not in seen:
                examples.append({'key': str(key)[:160], 'source': str(row.src)[:200]})
                seen.add(key)
                if len(examples) == 3:
                    break
        impacts = []
        for sibling, spec in rs.datasets.items():
            if sibling == detail_name or spec.link != detail.link or sibling not in data:
                continue
            lost = (keys(sibling, detail.link) & parent_keys) - retained
            if lost:
                impacts.append({'dataset': sibling, 'keys_losing_parent': len(lost),
                                'rows_losing_parent': sum(row.values.get(detail.link) in lost for row in data[sibling].rows),
                                'window': spec.window,
                                'read_call': {'tool': 'read_workspace', 'args': {'section': 'mapping', 'name': sibling}}})
        impacts.sort(key=lambda item: (-item['keys_losing_parent'], item['dataset']))
        comparisons.append({
            'parent': parent, 'detail': detail_name, 'key': detail.link, 'key_match': 'exact',
            'parent_keys': len(parent_keys), 'detail_keys': len(detail_keys),
            'parent_without_detail': len(absent), 'detail_without_parent': len(unmatched),
            'parent_without_detail_examples': examples,
            'parent_mapping': source(parent), 'detail_mapping': source(detail_name),
            'hypothetical_population_from': {
                'source': detail_name, 'retained_parent_keys': len(retained),
                'other_details_losing_parent': impacts[:3], 'other_detail_count': len(impacts),
                'note': '仅模拟按这张明细的完整键收窄父实体的影响，未应用修改，不能据此批准范围或删除历史行。'},
        })
    comparisons.sort(key=lambda item: (item['detail'] != focus and item['parent'] != focus,
                                      -item['parent_without_detail'], item['parent'], item['detail']))
    return {
        'status': 'OBSERVATION', 'scheme': scheme, 'comparisons': comparisons[:limit],
        'comparison_count': len(comparisons), 'omitted': max(0, len(comparisons) - limit),
        'ambiguous_owner_keys': sorted(key for key, names in owners.items() if len(names) > 1)[:3],
        'notes': [
            '覆盖数量是当前映射的观察，不是业务范围结论；完整键的大小写、空格等差异也需核对，不能仅凭差集认定对象不存在。',
            '提问缺值该如何分类之前，先按对象来源行和材料核对本期奖金对象、期间、关联键与历史统计范围。',
            '如果原文要求以有效明细限定本期对象，可在核对完整键后使用 population_from；同时保留历史行，检查其原有父关联是否仍可达。',
            '仅在原文无法确定且该对象确属本期范围后，再用真实对象和单元格例子提问；不得把报错改为默认 0、空串或任意类别。',
        ],
    }
