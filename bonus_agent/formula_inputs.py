"""Validate dataset dependencies independently of formula compilation."""

from bonus_core.dsl.parser import Call, ExprError, Str, parse, walk


def normalize_requirements(requirements):
    if not isinstance(requirements, list):
        raise ValueError('data_requirements 须为数组')
    result = []
    for item in requirements:
        if not isinstance(item, dict) or set(item) - {'dataset', 'fields', 'window', 'period_field', 'link'}:
            raise ValueError('每项数据要求须提供 dataset、fields，可指定 window、period_field、link')
        fields = item.get('fields')
        if not isinstance(fields, list) or any(not isinstance(f, str) or not f.strip() for f in fields):
            raise ValueError('data_requirements.fields 须为字段名数组')
        row = {'fields': list(dict.fromkeys(f.strip() for f in fields))}
        for name in ('dataset', 'window', 'period_field', 'link'):
            if name != 'dataset' and name not in item:
                continue
            value = item.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'data_requirements.{name} 须为非空文本')
            row[name] = value.strip()
        if row.get('window', 'period') not in ('period', 'year_to_date', 'to_date'):
            raise ValueError('data_requirements.window 须为 period、year_to_date 或 to_date')
        if any(prior['dataset'] == row['dataset'] for prior in result):
            raise ValueError(f'数据要求重复：{row["dataset"]}')
        result.append(row)
    return result


def with_requirements(spec, *requirements):
    """Extend a task's input contract only when the existing constraints agree."""
    combined = {row['dataset']: dict(row) for row in spec.get('data_requirements', ())}
    for requirement in requirements:
        dataset = requirement['dataset']
        prior = combined.get(dataset, {'fields': []})
        if any(key in prior and prior[key] != value for key, value in requirement.items() if key != 'fields'):
            return None
        fields = list(dict.fromkeys([*prior['fields'], *requirement['fields']]))
        combined[dataset] = {**prior, **requirement, 'fields': fields}
    return list(combined.values())


def with_rebound_requirement(spec, requirement, previous_link, *other):
    """Update the declared join after proof that a mapped reference uses the new key."""
    key = requirement['link']
    revised = []
    for item in spec.get('data_requirements', ()):
        if item['dataset'] == requirement['dataset'] and item.get('link') == previous_link and previous_link != key:
            item = {**item, 'link': key, 'fields': [key if field == previous_link else field for field in item['fields']]}
        revised.append(item)
    return with_requirements({**spec, 'data_requirements': revised}, requirement, *other)


def input_failures(ws, spec, value):
    """Report the first unmet condition per input, including unplanned 'of' references."""
    requirements = {row['dataset']: row for row in spec.get('data_requirements', ())}
    if isinstance(value, dict) and isinstance(value.get('expr'), str):
        try:
            for node in walk(parse(value['expr'])):
                table = node.kw().get('of') if isinstance(node, Call) else None
                if isinstance(table, Str):
                    requirements.setdefault(table.value, {'dataset': table.value, 'fields': []})
        except ExprError:
            pass  # Invalid expressions are handled by the compiler.
    if not requirements:
        return []
    datasets = ws._scheme_datasets(spec['scheme'])
    scope = (value.get('scope') if isinstance(value, dict) else None) or spec.get('expected_scope')
    failures = []
    for name, required in requirements.items():
        definition, mapping = datasets.get(name), ws.mapping.get(name)
        if definition is None or mapping is None:
            code, detail = 'dataset_missing', f'「{name}」尚未在当前方案定义并映射'
        elif fields := sorted(set(required['fields']) - (definition.fields.keys() & mapping.columns.keys())):
            code, detail = 'fields_missing', f'「{name}」缺少映射字段 {fields}'
        elif 'window' in required and definition.window != required['window']:
            code, detail = 'window_mismatch', f'「{name}」要求期间范围 {required["window"]}，实际为 {definition.window}'
        elif 'period_field' in required and definition.period != required['period_field']:
            code, detail = 'period_field_mismatch', f'「{name}」要求用 {required["period_field"]} 过滤期间'
        elif 'link' in required and definition.link != required['link']:
            link = required['link']
            # An entity is identified by its own primary key. Its optional
            # link describes its parent, not how a formula accesses that entity.
            if (scope not in (None, 'global') and definition.key == link
                    and link in definition.fields and link in mapping.columns
                    and (name == scope or any(level.get('level') == scope and level.get('key') == link
                                             for level in (ws._merged(spec['scheme']).get('allocation') or [])))):
                continue
            code, detail = 'link_mismatch', (f'「{name}」要求按 {link} 关联核算对象，'
                                             f'实际主键 {definition.key!r}、关联键 {definition.link!r}、作用域 {scope!r}')
        else:
            continue
        failures.append({'dataset': name, 'code': code, 'detail': detail, 'required': required})
    if not failures:
        from bonus_agent.indicator_history import history_window_failures

        failures = history_window_failures(ws, spec, value)
    return failures


def input_message(failure):
    return 'construction.data_dependency：' + failure['detail'] + '。先核对原表、字段、期间和关联，再提交公式。'


def input_preflight_versions(ws, spec):
    from bonus_agent.recovery import fingerprint
    from bonus_agent.facts import target_parts

    construction = ws.workflow.get('construction', {})
    tasks = construction.get('tasks', {})
    consumers = [(task['id'], task['clauses']) for task in tasks.values()
                 if task['scheme'] == spec['scheme'] and spec['id'] in task['depends_on']]
    inputs = {key: spec.get(key) for key in ('id', 'scheme', 'target', 'clauses', 'data_requirements', 'expected_scope', 'depends_on')}
    draft = ws._merged(spec['scheme'])
    dependencies = []
    for ident in spec['depends_on']:
        if ident in tasks:
            parts = target_parts(tasks[ident]['target'])
            value = draft.get(parts[0], {}).get(parts[1]) if len(parts) == 2 else draft.get(parts[0])
            dependencies.append((ident, value, construction.get('receipts', {}).get(ident)))
    inputs_version = fingerprint((ws._flow().version('data'), inputs, dependencies, consumers,
                                  [(c.id, c.file, c.quote) for c in ws.clauses]))
    # Broad table changes may affect an active resolver's search for physical
    # parameter tables. They need not repeatedly trigger speculative failures.
    return {'inputs_version': inputs_version, 'version': fingerprint((inputs_version, draft.get('tables', {})))}


def input_preflight_version(ws, spec):
    return input_preflight_versions(ws, spec)['version']
