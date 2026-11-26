"""Recover a distinct entity from an already mapped detail on the same physical rows."""
from __future__ import annotations

import copy
import re
from pathlib import Path

from bonus_agent.recovery import fingerprint
from bonus_core.data.mapping import apply_mapping
from bonus_core.dsl.models import RuleSet


def _definition_version(ws, name, source):
    return fingerprint(tuple(item.model_dump() for item in (
        ws.datasets[name], ws.mapping[name], ws.datasets[source], ws.mapping[source])))


def current_projections(ws):
    for name, proof in ws.workflow.get('dataset_repairs', {}).items():
        source = proof.get('source')
        if (name in ws.datasets and source in ws.datasets and name in ws.mapping and source in ws.mapping
                and proof.get('status') == 'PASS' and proof.get('period') == str(ws.period)
                and proof.get('source_version') == fingerprint(ws._source_version)
                and proof.get('definition') == _definition_version(ws, name, source)):
            yield name, proof


def recover_reference_dimensions(ws):
    """Construct an explicitly requested lookup on the unique verified transaction source."""
    from bonus_agent.agent import ToolError

    if ws.schemes:
        return []
    repairs = []
    owners = {ds.key for ds in ws.datasets.values() if ds.key}
    for ref, ds in list(ws.datasets.items()):
        key = ds.link
        if not key or key in owners or ds.unmatched != 'ignore' or ds.key:
            continue
        rm = ws.mapping[ref]
        basis = rm.unmatched_basis or {}
        label = rm.columns.get(key)
        if not label or not basis.get('quote') or ds.fields[key].type not in ('id', 'text'):
            continue
        clauses = [c for c in ws.clauses if ref in c.quote and label in c.quote]
        if not any(re.search(r'按.{0,45}(?:与|和).{0,55}(?:匹配|关联)', c.quote)
                   and not re.search(r'不按|禁止|不得|不匹配|不关联', c.quote) for c in clauses):
            continue
        evidence = [{'file': c.file, 'quote': c.quote} for c in clauses[:3]] + [basis]
        verified, bad = ws._evidence(evidence)
        if bad or len(verified) != len(evidence) or not all(p.get('ok') for p in verified):
            continue
        sources = set()
        for _, proof in current_projections(ws):
            source = proof['source']
            sm = ws.mapping[source]
            col = sm.columns.get(key, '')
            if (sm.file != rm.file and not sm.unpivot and ws.datasets[source].fields.get(key)
                    and ws.datasets[source].fields[key].type in ('id', 'text')
                    and re.sub(r'\([A-Z]+列\)$', '', col) == label
                    and any(col in parts for parts in proof['combine'].values())):
                sources.add(source)
        if len(sources) != 1:
            continue
        source, = sources
        spec = ws._dataset_definition(source)
        name = f'{source}（按{label}）'
        if name in ws.datasets or spec['fields'][key].get('if_empty'):
            continue
        candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm, id(ws.inv): ws.inv})
        declared = {k: spec[k] for k in ('file', 'sheet', 'header_rows', 'filter', 'period', 'window')}
        fields = {f: spec['fields'][f] for f in (key, spec['period']) if f}
        try:
            candidate.define_dataset(name=name, key=key, distinct=True, fields=fields, **declared)
            rs = candidate._flow()._data_rules()
            data, _ = candidate._data(rs)
        except (ToolError, ValueError, KeyError):
            continue
        raw = {row.values[key] for row in data[source].rows}
        keys = {row.values[key] for row in data[name].rows}
        reference = {row.values[key] for row in data[ref].rows}
        if not keys or keys != raw or keys.intersection({None, ''}) or len(keys) != len(data[name].rows) or not reference & keys:
            continue
        ws._check_source_version()
        ws.datasets[name], ws.mapping[name] = candidate.datasets[name], candidate.mapping[name]
        ws._prev_fields[name] = candidate._prev_fields[name]
        owners.add(key)
        report = {'dataset': name, 'source': source, 'reference': ref, 'key': key, 'column': label,
                  'source_column': ws.mapping[source].columns[key], 'keys': len(keys),
                  'matched_reference_keys': len(reference & keys), 'outside_reference_keys': len(reference - keys),
                  'evidence': [{'file': p['file'], 'quote': p['quote']} for p in verified]}
        report['version'] = _reference_version(ws, report)
        ws.workflow.setdefault('reference_dimensions', {})[ref] = report
        repairs.append(report)
    return repairs


def _reference_version(ws, report):
    names = [report[k] for k in ('dataset', 'source', 'reference')]
    return fingerprint([ws._source_version, str(ws.period), [
        (ws.datasets[n].model_dump(), ws.mapping[n].model_dump()) for n in names
        if n in ws.datasets and n in ws.mapping]])


def question_relation(ws, question):
    for proof in ws.workflow.get('reference_dimensions', {}).values():
        if (proof['reference'] in question.text and proof['column'] in question.text
                and proof['version'] == _reference_version(ws, proof)):
            return proof
    return None


def component_rebind(ws, args, changes):
    """Normalize a failed patch only when it explicitly retains the verified composite recipe."""
    from bonus_agent.agent import ToolError

    dataset = args['name']
    for name, proof in current_projections(ws):
        source, key = proof['source'], proof['key']
        if dataset not in (name, source) or set(changes) - {'fields', 'combine'}:
            continue
        fields = changes.get('fields')
        if not isinstance(fields, dict) or set(fields) != {key} or not isinstance(fields[key], dict):
            continue
        patch = fields[key]
        recipe = ws._dataset_definition(dataset)['combine'] or {}
        output = ws.mapping[dataset].columns[key]
        if (set(patch) - {'type', 'column', 'desc'} or output not in recipe
                or patch.get('column') not in recipe[output]
                or patch.get('type', ws.datasets[dataset].fields[key].type) != ws.datasets[dataset].fields[key].type):
            continue
        evidence = (f'当前生效的 {key} 取合成列「{output}」，来源为 {recipe[output]}。'
                    f'已核验 {proof["entities"]} 个「{name}」、{proof["detail_rows"]} 行「{source}」。')
        if 'combine' not in changes:
            raise ToolError(f'本次把编号换成单列「{patch["column"]}」的修改已拒绝。{evidence}'
                            '原映射继续生效。继续准备其余来源；确需更换项目粒度时按新依据提交完整编号定义。')
        if changes['combine'] != {output: recipe[output]}:
            continue
        ws._check_source_version()
        candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm, id(ws.inv): ws.inv})
        corrected = copy.deepcopy(args)
        corrected['fields'][key]['column'] = output
        message = candidate.define_dataset(**corrected)
        counts = _checked_rows(candidate, name, source, key, output, candidate.datasets[source],
                               candidate.datasets[source], candidate.datasets[name], candidate.mapping[name])
        if counts != (proof['entities'], proof['detail_rows']):
            raise ToolError('重新绑定合成列后，对象数或逐笔行数与当前回执不一致。')
        ws._check_source_version()
        ws.datasets, ws.mapping = candidate.datasets, candidate.mapping
        ws.untested, ws._prev_fields = candidate.untested, candidate._prev_fields
        proof['definition'] = _definition_version(ws, name, source)
        proof['normalized_patch'] = fingerprint([dataset, changes])
        return message + f'\n已按本次明确提交的合成规则校正字段绑定。{evidence}继续准备其余数据来源。'
    return None


def recover_entity(ws, args, population_from=None):
    from bonus_agent.agent import DistinctFieldError, ToolError

    name, key, fields = args.get('name'), args.get('key'), args.get('fields')
    if (args.get('distinct') is not True or not isinstance(key, str) or not isinstance(fields, dict)
            or not isinstance(args.get('file'), str)
            or not isinstance(fields.get(key), dict) or args.get('unmatched', 'block') != 'block'):
        return None
    output = fields[key].get('column')
    if not isinstance(output, str) or fields[key].get('type') not in ('text', 'id'):
        return None
    if any(other != name and ds.key == key for other, ds in ws.datasets.items()):
        return None

    candidates = []
    for owner, mapping in ws.mapping.items():
        ds = ws.datasets[owner]
        if (owner == name or ds.key or ds.distinct or ds.link not in (None, key)
                or population_from and population_from != owner or mapping.columns.get(key) != output
                or output not in (mapping.combine or {}) or Path(mapping.file).name != Path(args['file']).name
                or mapping.sheet != args.get('sheet') or mapping.header_rows != args.get('header_rows')
                or ds.period != args.get('period') or ds.window != args.get('window', 'period')):
            continue
        candidates.append(owner)
    if len(candidates) != 1:
        if len(candidates) > 1:
            raise ToolError(f'数据集 {name} 的合成编号 {key} 在多个同源明细中已定义：{candidates}。'
                            '明确选择本实体的 population_from 或完整提交 combine 与实体字段。')
        return None
    source = candidates[0]
    original = ws.datasets[source]
    if not original.link and (ws.schemes or set(ws.datasets) - {source, name}):
        return None  # Attaching a shared global table could change existing calculations.
    source_args = ws._dataset_definition(source)
    if (source_args.get('unpivot') != args.get('unpivot')
            or fields[key].get('if_empty') != original.fields[key].if_empty):
        return None
    recipe = {output: source_args['combine'][output]}
    if args.get('combine') is not None and args['combine'] != recipe:
        return None
    ws._check_source_version()
    candidate = copy.deepcopy(ws, {id(ws.llm): ws.llm, id(ws.inv): ws.inv})
    declaration = {**args, 'combine': recipe}
    varying = set()
    try:
        message = candidate._define_dataset(**declaration)
    except DistinctFieldError as exc:
        varying = set(exc.fields)
        if varying.intersection({key, args.get('link'), args.get('period')}):
            raise
        for field, (column, validated) in exc.fields.items():
            saved = original.fields.get(field)
            if (not saved or ws.mapping[source].columns.get(field) != column
                    or saved.model_dump(exclude={'desc'}) != validated.model_dump(exclude={'desc'})):
                raise
        declaration['fields'] = {field: spec for field, spec in fields.items() if field not in varying}
        message = candidate._define_dataset(**declaration)

    entity, entity_map = candidate.datasets[name], candidate.mapping[name]
    source_map = ws.mapping[source]
    if (entity_map.filter != source_map.filter or entity_map.unpivot != source_map.unpivot
            or (entity_map.combine or {}).get(output) != source_map.combine[output]):
        raise ToolError(f'数据集 {name} 与候选明细 {source} 的行范围或组合键定义不同，须明确各自的来源。')
    entity.population_from = population_from
    if name in ws.datasets and (ws.datasets[name] != entity or ws.mapping[name] != entity_map):
        return None  # Only an identical retry may reuse an existing entity.
    linked = original.model_copy(update={'link': key})
    counts = _checked_rows(ws, name, source, key, output, original, linked, entity, entity_map)
    ws._check_source_version()
    ws.datasets[source], ws.datasets[name], ws.mapping[name] = linked, entity, entity_map
    ws.untested.update(ws.schemes)
    receipt = {'status': 'PASS', 'source': source, 'key': key, 'combine': entity_map.combine,
               'detail_fields': sorted(varying), 'entities': counts[0], 'detail_rows': counts[1],
               'source_version': fingerprint(ws._source_version), 'period': str(ws.period),
               'definition': _definition_version(ws, name, source)}
    ws.workflow.setdefault('dataset_repairs', {})[name] = receipt
    return (f'已验证并保存「{name}」{counts[0]} 个唯一对象，{counts[1]} 行「{source}」按 {key} 全部关联。'
            f'同组取值不同的字段保留在逐笔明细：{sorted(varying)}。\n{message}')


def _checked_rows(ws, name, source, key, output, original, linked, entity, entity_map):
    from bonus_agent.agent import ToolError
    from bonus_agent.contracts import identity_conflicts

    source_map = ws.mapping[source]
    path = ws._file(Path(source_map.file).name)
    rules = RuleSet.model_validate({'name': '数据关联核验', 'version': '1',
                                    'datasets': {source: linked, name: entity}, 'pool': {'expr': '0'},
                                    'allocation': [{'level': name, 'key': key, 'weight': '1'}]})
    mappings = {source: source_map, name: entity_map}
    data, issues = apply_mapping(rules, [path], mappings, period=ws.period)
    errors = [str(issue) for issue in issues if issue.severity == 'blocking']
    if not errors:
        errors.extend(str(issue) for issue in identity_conflicts(rules, data, mappings))
    if errors:
        raise ToolError('实体与明细的候选映射未通过验证：' + '；'.join(errors[:3]))
    records, entities = data[source].rows, data[name].rows
    keys = {row.values[key] for row in entities}
    if (not records or len(keys) != len(entities) or keys.intersection({None, ''})
            or keys != {row.values[key] for row in records}):
        raise ToolError('候选实体的唯一键与逐笔明细未完整对应，保留原定义。')
    parts = len(source_map.combine[output])
    if any(str(value).count('|') != parts - 1 or not str(value).strip('|').strip() for value in keys):
        raise ToolError(f'组合编号 {key} 的原始分量全空或含有分隔符，无法证明它唯一标识每个对象。')
    # Same raw filters and periods can still discard different rows through if_empty:skip.
    # Compare physical source rows before entity deduplication, in addition to the resulting key sets.
    rows_rule = rules.model_copy(update={'datasets': {source: original, name: entity.model_copy(update={'distinct': False, 'key': None})}})
    raw, raw_issues = apply_mapping(rows_rule, [path], mappings, period=ws.period)
    if (any(i.severity == 'blocking' for i in raw_issues) or records != raw[source].rows
            or [r.src for r in records] != [r.src for r in raw[name].rows]):
        raise ToolError('候选实体与原明细的逐行来源或取值不同，保留原定义。')
    return len(entities), len(records)
