"""Bounded observations of declared populations; never choose accounting policy."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path


def scope_diagnostics(ws, *, scheme=None, data=None, focus=None, limit=3):
    """Observe the engine's actual links and separately retain raw exact keys.

    Neither a missing runtime link nor a raw spelling difference chooses the
    accounting population. No formulas, allocations or payouts are executed.
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
        from bonus_core.dsl.scope import Layout
        from bonus_core.dsl.validator import validate_structure
        from bonus_core.engine.tree import build_tree, _canonical_refs
        tree_blocking = [str(issue) for issue in validate_structure(rs) if issue.severity == 'blocking']
        if not tree_blocking:
            root, by_level, tree_issues = build_tree(rs, effective)
            tree_blocking = [str(issue) for issue in tree_issues if issue.severity == 'blocking']
        runtime = None
        if not tree_blocking:
            layout = Layout(rs)
            nodes = {level: {node.key: node for node in rows} for level, rows in by_level.items()}
            # build_tree uses these same canonical references before resolving
            # each parent. Reuse it, including its ambiguity protections; never
            # invent another normalization or business-alias matcher here.
            canonical = _canonical_refs(rs, layout, effective, nodes, root.index or {}, [])
            runtime = (root, nodes, layout, canonical)
        report = _compare(ws, rs, effective, name, focus, max(1, min(3, int(limit))), runtime=runtime)
        report['runtime_relationships'] = {
            'status': 'COMPLETE' if runtime is not None else 'UNAVAILABLE',
            'errors': tree_blocking[:2],
            'note': ('已使用与实际引擎相同的建树、引用规范化和父关联解析；未计算金额。' if runtime is not None else
                     '建树存在阻断错误，下面只展示原始精确键观察；部分挂接不能当作完整运行时覆盖。'),
        }
        return report
    except Exception as exc:
        # Diagnostic failure must preserve the original compile failure, not
        # replace it with an apparently empty population or an approval.
        return {'status': 'UNAVAILABLE', 'error': f'{type(exc).__name__}: {exc}'[:500],
                'note': '范围诊断未完成；这不是没有相关数据，也不是已确认缺失政策。'}


def _compare(ws, rs, data, scheme, focus, limit, *, runtime=None):
    owners = defaultdict(list)
    for name, spec in rs.datasets.items():
        if spec.key:
            owners[spec.key].append(name)
    cache = {}
    resolved_cache = {}

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

    def resolved(parent, detail_name):
        """Original row -> actual owner, using the engine resolver and tree."""
        identity = (parent, detail_name)
        if identity not in resolved_cache:
            from bonus_core.engine.tree import _parent_node
            root, nodes, layout, canonical = runtime
            field = rs.datasets[parent].key
            level = layout.key_level.get(field)
            pool = (nodes[rs.allocation[level].level] if level is not None else (root.index or {}).get(parent, {}))
            owner_nodes = set(pool.values())
            matched, unmatched, associations = set(), set(), []
            for row in canonical[detail_name].rows:
                actual = _parent_node(layout, detail_name, row, root, nodes, root.index or {}, [], [])
                owner = next((n for n in actual.chain() if n in owner_nodes), None) if actual else None
                key = row.values.get(rs.datasets[detail_name].link)
                associations.append((row, owner.key if owner else None))
                if owner is not None:
                    matched.add(owner.key)
                elif key not in (None, ''):
                    unmatched.add(key)
            resolved_cache[identity] = (set(pool), matched, unmatched, associations)
        return resolved_cache[identity]

    def other_references(detail_name, parent):
        # A historical row with no current project can still be an actual input
        # to a customer aggregate. Report that visibility instead of implying
        # that every unmatched parent link makes the whole row irrelevant.
        root = runtime[0]
        visible = []
        for entity, nodes in (root.index or {}).items():
            if entity == parent:
                continue
            rows = {id(row) for node in nodes.values() for row in node.fk_rows.get(detail_name, [])}
            if rows:
                visible.append({'entity': entity, 'rows': len(rows),
                                'objects': sum(bool(node.fk_rows.get(detail_name)) for node in nodes.values())})
        return sorted(visible, key=lambda item: (-item['rows'], item['entity']))[:3]

    comparisons = []
    for detail_name, detail in rs.datasets.items():
        candidates = owners.get(detail.link, [])
        if len(candidates) != 1 or detail_name not in data:
            continue
        parent = candidates[0]
        if parent == detail_name or parent not in data:
            continue
        raw_parent, detail_keys = keys(parent, detail.link), keys(detail_name, detail.link)
        raw_absent, raw_unmatched = raw_parent - detail_keys, detail_keys - raw_parent
        raw_retained = raw_parent & detail_keys
        if runtime is not None:
            parent_keys, retained, unmatched, associations = resolved(parent, detail_name)
            absent = parent_keys - retained
        else:
            parent_keys, retained = raw_parent, raw_retained
            absent, unmatched = raw_absent, raw_unmatched
        if not absent and not unmatched and not raw_absent and not raw_unmatched:
            continue
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
            if runtime is not None:
                _, sibling_parents, _, sibling_rows = resolved(parent, sibling)
                lost = sibling_parents - retained
                losing_rows = sum(key in lost for _, key in sibling_rows)
            else:
                lost = (keys(sibling, detail.link) & parent_keys) - retained
                losing_rows = sum(row.values.get(detail.link) in lost for row in data[sibling].rows)
            if lost:
                impacts.append({'dataset': sibling, 'keys_losing_parent': len(lost),
                                'rows_losing_parent': losing_rows,
                                'window': spec.window,
                                'read_call': {'tool': 'read_workspace', 'args': {'section': 'mapping', 'name': sibling}}})
        impacts.sort(key=lambda item: (-item['keys_losing_parent'], item['dataset']))
        comparisons.append({
            'parent': parent, 'detail': detail_name, 'key': detail.link,
            'key_match': 'runtime_resolved' if runtime is not None else 'raw_exact_only_runtime_unavailable',
            'parent_keys': len(parent_keys), 'detail_keys': len(detail_keys),
            'parent_without_detail': len(absent), 'detail_without_parent': len(unmatched),
            'parent_without_detail_examples': examples,
            'raw_exact': {'parent_keys': len(raw_parent), 'detail_keys': len(detail_keys),
                          'parent_without_detail': len(raw_absent), 'detail_without_parent': len(raw_unmatched),
                          'matched_parent_keys': len(raw_retained),
                          'note': '仅为准备后的原始键字面比较，不代表运行时关联缺失或业务别名结论。'},
            'runtime_linked_rows': sum(key is not None for _, key in associations) if runtime is not None else None,
            'other_reference_visibility': other_references(detail_name, parent) if runtime is not None else [],
            'parent_mapping': source(parent), 'detail_mapping': source(detail_name),
            'hypothetical_population_from': {
                'source': detail_name, 'retained_parent_keys': len(retained),
                'other_details_losing_parent': impacts[:3], 'other_detail_count': len(impacts),
                'note': ('仅展示当前真实关联对象集合被用于收窄父实体时的影响；不是population_from的执行预演，'
                         '未应用修改，不能据此批准范围或删除历史行。' if runtime is not None else
                         '仅按原始精确键比较，运行时关系未验证，不能据此批准范围或删除历史行。')},
        })
    comparisons.sort(key=lambda item: (item['detail'] != focus and item['parent'] != focus,
                                      -item['parent_without_detail'], item['parent'], item['detail']))
    return {
        'status': 'OBSERVATION', 'scheme': scheme, 'comparisons': comparisons[:limit],
        'comparison_count': len(comparisons), 'omitted': max(0, len(comparisons) - limit),
        'ambiguous_owner_keys': sorted(key for key, names in owners.items() if len(names) > 1)[:3],
        'notes': [
            '覆盖数量是当前映射的观察，不是业务范围结论；runtime_resolved使用实际引擎关联，raw_exact保留大小写、空格、分隔符等字面差异，不能把raw差集当作运行时缺失。',
            '同一行可能经其他真实编号引用被客户等实体汇总；原父关联缺失不能证明该历史行不参与任何计算，见other_reference_visibility。',
            '提问缺值该如何分类之前，先按对象来源行和材料核对本期奖金对象、期间、关联键与历史统计范围。',
            '如果原文要求以有效明细限定本期对象，可在核对完整键后使用 population_from；同时保留历史行，检查其原有父关联是否仍可达。',
            '仅在原文无法确定且该对象确属本期范围后，再用真实对象和单元格例子提问；不得把报错改为默认 0、空串或任意类别。',
        ],
    }
