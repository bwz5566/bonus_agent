"""Independent population contracts, evaluated from source rows before production filters.

Column identities are shared and remain subject to source review. Eligibility is
declared independently; production filters, periods, defaults and patches are
never silently copied into the reference population.
"""
from __future__ import annotations

from collections import Counter

from bonus_agent.clauses import check_quote
from bonus_core.data.mapping import DatasetMapping, apply_mapping
from bonus_core.dsl.models import DatasetDef, RuleSet, SourceRef
from bonus_core.dsl.parser import Call, Str, names, parse, walk


def requirements(rs):
    levels = {rs.pool.scope}
    if rs.report:
        levels.update(item.level for item in [*rs.report.totals, *rs.report.details, *rs.report.balances])
    result = {level: {'kind': 'objects', 'level': level} for level in sorted(levels - {'global'})}
    # Correct object IDs do not prove the completeness of their contributing
    # details. Check all amount inputs, including historical/global aggregates.
    expressions = [rs.pool.expr]
    if rs.report:
        expressions += [item.expr for item in [*rs.report.totals, *rs.report.details]]
        expressions += [expr for item in rs.report.balances for expr in [item.total, *item.parts]]
    seen, datasets = set(), set()
    while expressions:
        ast = parse(expressions.pop())
        refs = names(ast)
        datasets.update(n for n, d in rs.datasets.items() if refs & set(d.fields))
        for node in walk(ast):
            if isinstance(node, Call) and isinstance(node.kw().get('of'), Str):
                datasets.add(node.kw()['of'].value)
        for name in refs & set(rs.variables) - seen:
            seen.add(name)
            expressions.append(rs.variables[name].expr)
    result.update({'rows:' + n: {'kind': 'rows', 'dataset': n} for n in sorted(datasets & set(rs.datasets))})
    return result


def check_populations(ws, rs, spec, root, by_level):
    """Compare sets (or physical row multiplicities), without changing production data."""
    wanted = requirements(rs)
    supplied = spec.get('populations')
    issues, checks, valid = [], [], True
    if not isinstance(supplied, dict):
        return {'status': 'FAIL', 'contract_valid': False, 'issues': ['独立复核缺少 populations 核算范围声明'], 'checks': []}
    for label, requirement in wanted.items():
        cfg = supplied.get(label)
        try:
            if not isinstance(cfg, dict):
                raise ValueError(f'缺少 {label} 的独立核算范围')
            allowed = {'dataset', 'key', 'filter', 'period', 'window', 'sources'}
            if set(cfg) - allowed or not {'dataset', 'filter', 'period', 'window', 'sources'} <= set(cfg):
                raise ValueError('须显式填写 dataset/filter/period/window/sources；对象范围另填 key，不继承生产筛选')
            source_name = cfg['dataset']
            if source_name not in ws.datasets or source_name not in ws.mapping:
                raise ValueError(f'未知来源数据表 {source_name}')
            sources = cfg['sources']
            if not isinstance(sources, list) or not sources:
                raise ValueError('核算范围缺少原文依据 sources')
            for source in sources:
                source = SourceRef.model_validate(source)
                bad = check_quote(source.doc, source.quote, ws._corpus())
                if bad:
                    raise ValueError('核算范围依据无效：' + bad)
                if source.page is not None:
                    from bonus_agent.facts import norm
                    page = ws._page_texts(source.doc).get(source.page, '')
                    if check_quote(source.doc, source.quote, {source.doc: norm(page)}):
                        raise ValueError(f'核算范围引文不在声明的第 {source.page} 页')
            original = ws.datasets[source_name]
            key = cfg.get('key')
            if requirement['kind'] == 'objects' and (not isinstance(key, str) or key not in original.fields):
                raise ValueError(f'对象 key 必须是 {source_name} 的实际标准字段')
            if requirement['kind'] == 'rows' and source_name != requirement['dataset']:
                raise ValueError('金额取数范围必须核对对应来源表，不能换表')
            period = cfg['period']
            if period is not None and (period not in original.fields or ws.period is None):
                raise ValueError('独立期间字段不存在，或本轮未指定核算期间')
            selected = set(original.fields) if requirement['kind'] == 'rows' else {key}
            if period:
                selected.add(period)
            fields = {n: original.fields[n].model_copy(update={'if_empty': None}) for n in selected}
            independent_ds = DatasetDef(fields=fields, period=period, window=cfg['window'])
            # Raw source mapping operations (e.g. combined IDs) remain visible to
            # the reviewer. No production row filter or population patch is used.
            mapping = ws.mapping[source_name].model_copy(update={
                'columns': {n: c for n, c in ws.mapping[source_name].columns.items() if n in selected},
                'filter': cfg['filter'],
            })
            mapping = DatasetMapping.model_validate(mapping.model_dump())
            independent = RuleSet(name='independent_population', version='1',
                                  datasets={source_name: independent_ds}, pool={'expr': '0'}, allocation=[])
            raw, errors = apply_mapping(independent, ws.data_files, {source_name: mapping}, ws.period)
            blocking = [str(e) for e in errors if e.severity == 'blocking']
            if blocking:
                raise ValueError('独立来源读取失败：' + '；'.join(blocking[:3]))
            rows = raw[source_name].rows
            if requirement['kind'] == 'objects':
                empty = [r for r in rows if r.values.get(key) in (None, '')]
                if empty:
                    raise ValueError(f'独立对象标识为空：{empty[0].src}')
                expected = {str(r.values[key]) for r in rows}
                nodes = by_level.get(label) or list((root.index or {}).get(label, {}).values())
                actual = {str(n.key) for n in nodes}
                # Only accept spelling aliases already attached unambiguously by
                # the real engine, never a guessed customer/business alias.
                from bonus_core.engine.tree import _norm_id
                attachments = {}
                for node in nodes:
                    for row in node.rows(source_name):
                        attachments.setdefault(str(row.src), set()).add(str(node.key))
                aliases = {}
                for row in rows:
                    raw_key = str(row.values[key])
                    hits = attachments.get(str(row.src), set())
                    if raw_key not in actual and len(hits) == 1:
                        canonical = next(iter(hits))
                        if (_norm_id(raw_key) == _norm_id(canonical)
                                and sum(_norm_id(k) == _norm_id(canonical) for k in actual) == 1):
                            aliases[raw_key] = canonical
                expected = {aliases.get(key, key) for key in expected}
                missing, extra = sorted(expected - actual), sorted(actual - expected)
            else:
                # Compare participating physical rows before entity distinct,
                # otherwise valid grouping would look like missing source rows.
                production_ds = original.model_copy(update={'key': None, 'link': None, 'distinct': False,
                                                             'population_from': None})
                production_rs = RuleSet(name='production_source_rows', version='1',
                                        datasets={source_name: production_ds}, pool={'expr': '0'}, allocation=[])
                production_data, production_errors = apply_mapping(
                    production_rs, ws.data_files, {source_name: ws.mapping[source_name]}, ws.period)
                blocking = [str(e) for e in production_errors if e.severity == 'blocking']
                if blocking:
                    raise ValueError('生产参与行无法验证：' + '；'.join(blocking[:3]))
                actual = Counter(str(r.src) for r in production_data[source_name].rows)
                expected = Counter(str(r.src) for r in rows)
                missing, extra = sorted((expected - actual).elements()), sorted((actual - expected).elements())
            entry = {'scope': label, 'dataset': source_name, 'reference_rows': len(rows),
                     'expected_count': len(expected) if isinstance(expected, set) else sum(expected.values()),
                     'actual_count': len(actual) if isinstance(actual, set) else sum(actual.values()),
                     'missing': missing[:8], 'extra': extra[:8], 'status': 'FAIL' if missing or extra else 'PASS'}
            if requirement['kind'] == 'objects':
                entry['repeated_key_rows'] = len(rows) - len(expected)
                entry['verified_spelling_aliases'] = dict(list(aliases.items())[:8])
                entry['spelling_alias_count'] = len(aliases)
            checks.append(entry)
            if missing or extra:
                issues.append(f'{label} 核算范围与独立原文取数不符：缺少 {len(missing)}，多出 {len(extra)}；'
                              '核对本期对象与历史统计数据的各自范围，不能通过修改金额或盲目裁历史明细消除此差异')
        except (ValueError, TypeError, KeyError) as exc:
            valid = False
            issues.append(f'{label}：{exc}')
    return {'status': 'FAIL' if issues else 'PASS', 'contract_valid': valid, 'issues': issues, 'checks': checks}
