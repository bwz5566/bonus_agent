"""Build a verifiable period-and-grain proposal from the ordinary history observations."""
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import re
from unicodedata import normalize

from bonus_agent.clauses import check_quote
from bonus_agent.data_profile import _covered_month, _source_column_name
from bonus_agent.facts import MAX_QUOTE, norm, number, scope_norm, target_parts
from bonus_agent.indicator_scope import _aggregates, _expression, _group_header
from bonus_core.data.mapping import ConvertError, convert
from bonus_core.dsl.parser import Call, Name, Str, names


def _selectors(tree):
    return {item.value for expr in _aggregates(tree) if isinstance(item := expr.kw().get('of'), Str)}


def _same_scope_parent(rules, name, ws):
    scopes = {v.scope for v in rules.variables.values() if v.scope != 'global'
              and name in names(_expression(v.expr)) and v.scope in ws.datasets}
    if len(scopes) == 1:
        scope, = scopes
        if ws.datasets[scope].key:
            return scope
    return None


def _active_amounts(data, dimension, stamp, measure, window, allowed):
    totals = defaultdict(Decimal)
    for row in data.rows:
        values = row.values
        key = str(values.get(dimension, '')).strip()
        month = _covered_month(window, values.get(stamp))
        value = values.get(measure)
        if key in allowed and month and isinstance(value, Decimal) and value:
            totals[key, month] += value
    return dict(totals)


def _historical_candidates(ws, definition, mapping, opts, dimensions, observed):
    dimension, stamp, measure, key_header, allowed = dimensions
    window = ws.period.window(definition.window)
    if not observed:
        return None
    anchor = ws.mapping[mapping]
    recognized_months = {month for _, month in observed}
    cents = Decimal('0.01')
    candidates = []
    for entry in opts:
        sheet = ws.inv.sheet(f'{entry["file"]}/{entry["sheet"]}')
        headers = entry['columns']
        if not sheet or not all(column in headers and sheet.headers.count(headers[column]) == 1
                                for column in (stamp, measure)):
            continue
        possible = [c for c in sheet.headers if _group_header(_source_column_name(c)) == key_header]
        if (Path(anchor.file).name, anchor.sheet) == (entry['file'], entry['sheet']):
            proven = anchor.columns.get(dimension)
            if proven in possible and sheet.headers.count(proven) == 1:
                possible = [proven]
        if len(possible) != 1:
            continue
        key_col, = possible
        positions = [sheet.headers.index(x) for x in (key_col, headers[stamp], headers[measure])]
        totals = defaultdict(Decimal)
        try:
            for _, row in sheet.rows:
                key, date, value = (row[i] if i < len(row) else None for i in positions)
                key = str(key or '').strip()
                month = _covered_month(window, date)
                if key in allowed and month and (amount := convert(value, definition.fields[measure])):
                    totals[key, month] += amount
        except ConvertError:
            continue
        supplied = {identity: amount for identity, amount in totals.items() if identity[1] in recognized_months}
        if (set(supplied) != set(observed) or any(abs(supplied[k] - observed[k]) >= cents for k in observed)
                or (window != ws.period and totals.keys() <= observed.keys())):
            continue
        fields = {
            dimension: {**ws.datasets[mapping].fields[dimension].model_dump(exclude_defaults=True), 'column': key_col},
            stamp: {**definition.fields[stamp].model_dump(exclude_defaults=True), 'column': headers[stamp]},
            measure: {**definition.fields[measure].model_dump(exclude_defaults=True), 'column': headers[measure]},
        }
        candidates.append((entry, fields, dict(totals)))
    if not candidates:
        return None
    entry, fields, totals = candidates[0]
    # Multiple physical inputs can be equivalent for this contract. Stop
    # proposing if any eligible key/month or measure amount materially differs.
    for _, _, another in candidates[1:]:
        if another.keys() != totals.keys() or any(abs(another[k] - totals[k]) >= cents for k in totals):
            return None
    return entry, fields, len(totals), len(candidates)


def historical_average_quote(quote):
    period = re.compile(r'(?:本年|当年|本年度)\s*(?:0?1|一)\s*月\s*(?:到|至)\s*(?:本月|当月|核算月)')
    return bool(period.search(quote) and re.search(r'月平均(?:出货|销售)', quote)
                and re.search(r'金额合计\s*[/÷／]\s*[^。；;]*实际有(?:出货|销售)的月份数', quote))


def historical_average_clause(ws, spec):
    if not spec['target'].startswith('/variables/'):
        return None
    return next((c for c in ws.clauses if c.id in spec['clauses'] and historical_average_quote(c.quote)), None)


def _grouping_quote(quote):
    return re.search(r'对应([^，,。；;|]{1,32})[，,]', quote) if '月平均' in quote else None


def _zero_months_quote(quote):
    return re.search(r'(?:为|等于)\s*0\b', quote) and '不参与' in quote and '月份' in quote


def _reference_exclusion_quote(quote):
    return re.search(r'参考(?:数据|资料|表)里有[、，, ]*但[^。；;]{1,28}里没有[^。；;]{0,36}不参与核算', quote)


def _history_policy_candidates(ws, missing):
    """Find existing rule excerpts for missing clauses, leaving their scope to validation."""
    predicates = {'grouping': _grouping_quote, 'zero_months': _zero_months_quote,
                  'exclusion': _reference_exclusion_quote, 'measure': historical_average_quote}
    candidates = {key: [] for key in missing}
    corpus = ws._corpus()
    for file, role in ws.workflow.get('plan', {}).get('files', {}).items():
        if role not in ('rule', 'rule_and_data') or file not in corpus:
            continue
        for line in ws._lines(file):
            quote = line.strip()
            if not quote or len(quote) > MAX_QUOTE:
                continue
            for key, rows in candidates.items():
                if len(rows) >= 6 or not predicates[key](quote) or check_quote(file, quote, corpus):
                    continue
                if any(row['file'] == file and row['quote'] == quote for row in rows):
                    continue
                registered = [c.id for c in ws.clauses if c.file == file and
                              (norm(c.quote) in norm(quote) or norm(quote) in norm(c.quote))]
                rows.append({'file': file, 'quote': quote, 'registered_clause_ids': registered})
    return candidates


def history_window_failures(ws, spec, value):
    """A recognized historical average cannot be satisfied by a current-period dataset."""
    if not isinstance(value, dict) or not isinstance(value.get('expr'), str):
        return []
    average = historical_average_clause(ws, spec)
    if average is None or check_quote(average.file, average.quote, ws._corpus()):
        return []
    expression = _expression(value['expr'])
    if not isinstance(expression, Call) or expression.fn != 'ratio' or len(expression.args) < 2:
        return []
    datasets = ws._scheme_datasets(spec['scheme'])
    references = set().union(*(_selectors(arg) for arg in expression.args[:2]))
    return [{'dataset': name, 'code': 'window_mismatch', 'required': {'window': 'year_to_date'},
             'detail': f'《{average.file}》要求本年 1 月至核算月，「{name}」当前期间范围为 {datasets[name].window}'}
            for name in sorted(references & datasets.keys()) if datasets[name].window != 'year_to_date']


def _average_requirements(board, spec):
    """Identify a source-backed year-to-date average before a formula is written."""
    ws = board.ws
    if (average := historical_average_clause(ws, spec)) is None:
        return None
    corpus = ws._corpus()
    if check_quote(average.file, average.quote, corpus):
        return None
    verified = [c for c in ws.clauses if not check_quote(c.file, c.quote, corpus)]
    own_and_consumers = set(spec['clauses'])
    for task in board.state['tasks'].values():
        if task['scheme'] == spec['scheme'] and spec['id'] in task['depends_on']:
            own_and_consumers.update(task['clauses'])
    grouping = {}
    for clause in verified:
        if clause.id not in own_and_consumers:
            continue
        if match := _grouping_quote(clause.quote):
            grouping[_group_header(_source_column_name(match[1]))] = clause
    if not grouping:
        # A plan may cite a comparison chart and omit the companion rule
        # giving the join key. Match the documented numerator and denominator.
        from bonus_agent.indicator_snapshot import _metric

        def measure_heading(value):
            return _metric(value).removeprefix('呆滞')

        compared = set()
        for clause in verified:
            if clause.id in own_and_consumers:
                terms = re.split(r'\bVS\b', clause.quote, flags=re.IGNORECASE)
                if len(terms) == 2 and '月平均' in terms[1]:
                    compared.add(measure_heading(terms[0]))
        for clause in verified:
            match = re.search(r'对应([^,。;|]{1,32}),([^,/]+)[/÷]([^,。;|]+)的月平均', normalize('NFKC', clause.quote))
            if match and measure_heading(match[2]) in compared and match[1].strip() == match[3].strip():
                grouping[_group_header(_source_column_name(match[1]))] = clause
    zeros = [c for c in verified if _zero_months_quote(c.quote)]
    excluded = [c for c in verified if _reference_exclusion_quote(c.quote)]
    measure = re.search(r'(?:出货|销售)金额(?:使用|用)([^。；;\n]+)', average.quote)
    measure_name = scope_norm(measure[1]).replace('销售额', '销售金额') if measure else ''
    return average, grouping, zeros, excluded, measure_name


def _sales_filter_issue(ws, mapping, measure_column):
    """A grouped history may omit a status column only when it excludes zero rows."""
    from bonus_agent.data_profile import _source_sheet
    from bonus_core.data.mapping import row_filter

    filters = mapping.filter or {}
    if not filters:
        return None
    zero = filters.get(measure_column)
    if (not isinstance(zero, dict) or set(zero) != {'not'} or not zero['not']
            or any(number(str(v)) != 0 for v in zero['not'])):
        return '当前金额筛选不限于去除零金额，须核对历史数据的同口径取数'
    extra = {column: condition for column, condition in filters.items() if column != measure_column}
    if not extra:
        return None
    corpus = ws._corpus()
    inclusions = [scope_norm(match[1]) for c in ws.clauses if _zero_months_quote(c.quote)
                  and not check_quote(c.file, c.quote, corpus)
                  for match in re.finditer(r'(?:为|等于)\s*0\s*(?:的)?(?:行|记录|数据)\s*'
                                           r'\(\s*(?:包括|含)([^)]+)\)', normalize('NFKC', c.quote))]
    for column, condition in extra.items():
        field = re.escape(scope_norm(_source_column_name(column)))
        if (not isinstance(condition, dict) or set(condition) != {'not'} or not condition['not']
                or not all(any(re.fullmatch(field + r'(?:为|等于|是)' + re.escape(scope_norm(str(value)))
                                            + r'(?:的?(?:行|记录|数据))?', text) for text in inclusions)
                           for value in condition['not'])):
            return f'筛选列「{column}」缺少其排除对象包含在零金额行中的原文依据'
    try:
        sheet = _source_sheet(ws, mapping)
    except (ValueError, KeyError, OSError) as exc:
        return f'原表筛选列无法逐行复验：{exc}'
    nonzero = row_filter(sheet.headers, {measure_column: {'not': ['0']}})
    selected = row_filter(sheet.headers, filters)
    if isinstance(nonzero, str) or isinstance(selected, str):
        return '原表筛选列无法逐行复验'
    excluded = sum(nonzero(row) and not selected(row) for _, row in sheet.rows)
    if excluded:
        return f'额外筛选实际排除 {excluded} 条非零记录，须使用包含相应筛选字段的历史来源'
    return None


def _sales_sources(ws, datasets, measure_name, *, rejected=None):
    for name, definition in datasets.items():
        mapping = ws.mapping.get(name)
        if (definition.key or definition.window != 'period' or not definition.period
                or not mapping or definition.period not in mapping.columns
                or not mapping.file or not mapping.sheet):
            continue
        amounts = [f for f, column in mapping.columns.items() if f in definition.fields
                   and definition.fields[f].type == 'money'
                   and scope_norm(_source_column_name(column)) in measure_name]
        if len(amounts) != 1:
            continue
        measure, = amounts
        if issue := _sales_filter_issue(ws, mapping, mapping.columns[measure]):
            if rejected is not None:
                rejected.append({'dataset': name, 'filter': mapping.filter, 'detail': issue})
            continue
        yield name, definition, mapping, measure


def _current_sales_source(ws, scheme, group_header, measure_name, *, rejected=None):
    sources = []
    datasets = ws._scheme_datasets(scheme)
    for name, definition, mapping, measure in _sales_sources(ws, datasets, measure_name, rejected=rejected):
        for scope, entity in datasets.items():
            anchor = ws.mapping.get(scope)
            field = entity.key
            if (anchor and field in mapping.columns and field in definition.fields
                    and entity.period in (None, definition.period) and entity.window == 'period'
                    and (anchor.file, anchor.sheet, anchor.columns.get(field))
                    == (mapping.file, mapping.sheet, mapping.columns[field])
                    and _group_header(_source_column_name(mapping.columns[field])) == group_header):
                sources.append((name, scope, field, definition.period, measure))
    return sources[0] if len(sources) == 1 else None


def _grouped_sales_candidate(ws, spec, group, rule, measure_name):
    """Derive a separate group when the verified policy names a non-key column."""
    from bonus_agent.data_profile import _source_sheet

    datasets = ws._scheme_datasets(spec['scheme'])
    original = spec.get('expected_scope')
    anchor, entity = ws.mapping.get(original), datasets.get(original)
    declared = re.search(r'对应([^，,。；;|]{1,32})[，,]', rule.quote)
    if not anchor or not entity or not entity.key or not declared:
        return None
    heading = _source_column_name(declared[1])
    group_scope = target_parts(spec['target'])[1] + '_group'
    if group_scope in datasets or group_scope in ws.datasets:
        return None
    candidates = []
    for sale, detail, mapping, measure in _sales_sources(ws, datasets, measure_name):
        if (anchor.file, anchor.sheet, anchor.header_rows) != (mapping.file, mapping.sheet, mapping.header_rows):
            continue
        sheet = _source_sheet(ws, mapping)
        columns = [column for column in sheet.headers if _group_header(_source_column_name(column)) == group]
        if len(columns) > 1:
            occurrence = re.search(r'(\d+)$', heading)
            if not occurrence:
                continue
            exact = [c for c in columns if _source_column_name(c) == heading]
            number = int(occurrence[1])
            if number < 1:
                continue
            columns = exact or columns[number - 1:number]
        if len(columns) != 1 or columns[0] == anchor.columns.get(entity.key):
            continue
        column, = columns
        aliases = [field for field, physical in mapping.columns.items() if physical == column and field in detail.fields]
        if column in anchor.columns.values() and len(aliases) == 1:
            candidates.append((sale, aliases[0], detail.period, measure))
    if len(candidates) != 1:
        return None
    sale, dimension, stamp, measure = candidates[0]
    mapped = ws._dataset_definition(sale)
    args = {'name': group_scope, 'file': mapped['file'], 'sheet': mapped['sheet'],
            'header_rows': mapped['header_rows'], 'fields': {f: mapped['fields'][f] for f in (dimension, stamp)},
            'key': dimension, 'period': stamp, 'distinct': True, 'filter': mapped['filter']}
    return (sale, group_scope, dimension, stamp, measure), {'operation': 'define', 'args': args}


def history_average_input(board, spec):
    """Resolve an unbuilt data dependency by corroborating historical rows with current sales."""
    from bonus_agent.agent import ToolError

    ws = board.ws
    if ws.period is None or board.value(spec) is not None:
        return None
    requirement = _average_requirements(board, spec)
    if not requirement:
        return None
    average, grouping, zeros, excluded, measure_name = requirement
    window = ws.period.window('year_to_date')

    def blocked(code, detail, **evidence):
        return {'task': spec['id'], 'scheme': spec['scheme'], 'target': spec['target'],
                'status': 'FAIL', 'saved': False, 'stage': 'data',
                'input_failures': [{'code': code, 'detail': detail, 'window': window.label, **evidence}],
                'issues': ['construction.data_dependency：' + detail],
                'next': ('核对 evidence_candidates 的原文及当前指标适用范围。'
                         '未登记的依据用 record_clauses，已登记的沿用条款 ID，'
                         '用 plan_formula_tasks 补充当前任务的条款归属，再由程序复验。'
                         if code == 'history_policy_unresolved'
                         else '先核对历史原表、关联键和期间，补齐映射后再提交公式。')}

    if len(grouping) != 1 or not zeros or len(excluded) != 1 or not measure_name:
        missing = {}
        if len(grouping) != 1:
            missing['grouping'] = '月平均金额对应的关联字段'
        if not zeros:
            missing['zero_months'] = '零金额记录是否计入实际出货月份的口径'
        if len(excluded) != 1:
            missing['exclusion'] = '参考数据中无当期对象时的排除依据'
        if not measure_name:
            missing['measure'] = '月平均出货取金额及币种的原文'
        return blocked('history_policy_unresolved', '尚需唯一核对' + '、'.join(missing.values()),
                       evidence_candidates=_history_policy_candidates(ws, missing))
    group, group_source = next(iter(grouping.items()))
    rejected = []
    source = _current_sales_source(ws, spec['scheme'], group, measure_name, rejected=rejected)
    new_group = []
    if not source:
        try:
            proposal = _grouped_sales_candidate(ws, spec, group, group_source, measure_name)
            if proposal:
                from copy import deepcopy

                source, operation = proposal
                ws = deepcopy(ws, {id(ws.llm): ws.llm})
                ws.define_dataset(**operation['args'])
                new_group = [operation]
        except (ToolError, ValueError, KeyError, OSError) as exc:
            return blocked('history_anchor_invalid', f'已识别「{group}」口径，独立分组未通过当前原表复验。{exc}')
        if not source:
            if rejected:
                return blocked('history_filter_unresolved', rejected[0]['detail'], candidates=rejected)
            return blocked('history_anchor_missing', f'《{group_source.file}》要求按「{group}」归组。'
                           f'已声明作用域为「{spec.get("expected_scope") or "未声明"}」。'
                           f'缺少当期原表中唯一对应的分组列、金额列或实体键。')
    sale, scope, dimension, stamp, measure = source
    mapping = ws.mapping[sale]
    date_header, amount_header = [_source_column_name(mapping.columns[f]) for f in (stamp, measure)]
    definition = ws.datasets[sale].model_copy(update={'window': 'year_to_date'})
    flow = ws._flow()
    original_files = {file for file, role in ws.workflow['plan']['files'].items() if role in ('data', 'rule_and_data')}
    ignored = flow.skipped()
    options = []
    for sheet in ws.inv.sheets:
        if sheet.file not in original_files or f'{sheet.file}/{sheet.sheet}' in ignored or sheet.file in ignored:
            continue
        columns = defaultdict(list)
        for column in sheet.headers:
            columns[_source_column_name(column)].append(column)
        if len(columns[date_header]) == len(columns[amount_header]) == 1:
            options.append({'file': sheet.file, 'sheet': sheet.sheet,
                            'columns': {stamp: columns[date_header][0], measure: columns[amount_header][0]}})
    try:
        data, _ = ws._data(ws._ruleset(spec['scheme']))
        allowed = {str(r.values[dimension]).strip() for r in data[scope].rows}
        sales_groups = {str(r.values[dimension]).strip() for r in data[sale].rows if r.values.get(dimension) is not None}
        observed = _active_amounts(data[sale], dimension, stamp, measure, window, sales_groups)
        active = {group for group, _ in observed}
        scope_mapping = ws.mapping[scope]
        fixed_scope = []
        if ws.datasets[scope].period is None and (scope_mapping.filter or {}) in ({}, mapping.filter or {}):
            stamp_column = mapping.columns[stamp]
            if scope_mapping.columns.get(stamp, stamp_column) != stamp_column or not active <= allowed:
                return blocked('history_scope_unresolved', '当期有销售的业务分组与待核算分组未逐项对齐')
            fixed_scope = [{'operation': 'update', 'args': {'name': scope, 'changes': {
                'period': stamp, 'filter': {**(mapping.filter or {}), mapping.columns[measure]: {'not': ['0']}}, 'fields': {
                    stamp: {**definition.fields[stamp].model_dump(exclude_defaults=True), 'column': stamp_column},
                },
            }}}]
            allowed = active
        if active != allowed:
            return blocked('history_scope_unresolved', '当期有销售的业务分组与待核算分组未逐项对齐')
        chosen = _historical_candidates(ws, definition, scope, options, (dimension, stamp, measure, group, allowed), observed)
    except (ToolError, KeyError, TypeError, ValueError) as exc:
        return blocked('history_source_invalid', str(exc)[:360])
    if not chosen:
        return blocked('history_source_unresolved', f'没有可唯一验证的「{group}」历史金额来源，共检查 {len(options)} 个同字段来源')
    entry, fields, group_months, equivalent = chosen
    requirements = spec.get('data_requirements', [])
    name = requirements[0]['dataset'] if len(requirements) == 1 else target_parts(spec['target'])[1] + '_history'
    if requirements:
        if len(requirements) != 1:
            return blocked('history_contract_mismatch', '当前任务指定了多套历史输入，需核对各输入的使用范围')
        prior, = requirements
        original_scope = spec.get('expected_scope')
        old_group = ws.datasets.get(original_scope)
        old_anchor = ws.mapping.get(original_scope)
        replace_key = (old_group.key if old_group and old_group.key and original_scope != scope
                       and old_anchor and (old_anchor.file, old_anchor.sheet) == (mapping.file, mapping.sheet)
                       and mapping.columns[dimension] in old_anchor.columns.values()
                       else None)
        allowed_fields = fields.keys() | ({replace_key} if replace_key else set())
        if (prior.get('window', 'year_to_date') != 'year_to_date' or prior.get('period_field', stamp) != stamp
                or prior.get('link', dimension) not in (dimension, replace_key) or not set(prior['fields']) <= allowed_fields):
            return blocked('history_contract_mismatch', '已声明的历史期间、关联字段或金额列与已核原文不一致')
    if name in ws.datasets:
        # An existing draft can serve other formulas. Give the verified input
        # its own data contract, leaving those consumers with their own sources.
        name = target_parts(spec['target'])[1] + '_history'
    if name in ws.datasets:
        return blocked('history_dataset_exists', f'「{name}」已经定义，先按前置要求核对现有来源和期间')
    required = {'dataset': name, 'fields': sorted(fields), 'window': 'year_to_date',
                'period_field': stamp, 'link': dimension}
    literal = json.dumps(name, ensure_ascii=False)
    expr = f'ratio(sum({measure}, of: {literal}), count_distinct(month({stamp}), of: {literal}))'
    citations = [{'doc': c.file, 'quote': c.quote} for c in (average, group_source, zeros[0])]
    return {
        'source': {'file': entry['file'], 'sheet': entry['sheet']}, 'contract': required, 'window': window.label,
        'compared_group_months': len(observed), 'historical_group_months': group_months,
        'equivalent_sources': equivalent,
        'candidate_call': {
            'scheme': spec['scheme'], 'task': spec['id'],
            'formulas': [{'task': spec['id'], 'value': {'scope': scope, 'expr': expr, 'sources': citations},
                          'expected_scope': scope, 'data_requirements': [required]}],
            'datasets': [*new_group, *fixed_scope, {'operation': 'define', 'args': {
                'name': name, 'file': entry['file'], 'sheet': entry['sheet'], 'fields': fields,
                'link': dimension, 'period': stamp, 'window': 'year_to_date',
                'filter': {fields[measure]['column']: {'not': ['0']}},
                'unmatched': 'ignore', 'unmatched_basis': excluded[0].id,
            }}],
        },
    }


def history_repair(board, scheme, prepared_observations):
    """Propose a grouped average only when earlier raw values corroborate the observed periods."""
    from bonus_agent.agent import ToolError

    ws = board.ws
    if ws.period is None:
        return None
    rules = ws._ruleset(scheme)
    variable_tasks = {target_parts(task['target'])[1]: task for task in board.state['tasks'].values()
                      if task['scheme'] == scheme and task['target'].startswith('/variables/')}
    exclusions = [c for c in ws.clauses if _reference_exclusion_quote(c.quote)]
    if len(exclusions) != 1:
        return None
    mapping_policy = {'unmatched': 'ignore', 'unmatched_basis': exclusions[0].id}
    for name, variable in rules.variables.items():
        spec = variable_tasks.get(name)
        expression = _expression(variable.expr)
        if not spec or not isinstance(expression, Call) or expression.fn != 'ratio' or len(expression.args) < 2:
            continue
        shipment = expression.args[0]
        sources = _selectors(expression)
        if (len(sources) != 1 or not isinstance(shipment, Call) or shipment.fn != 'sum'
                or not shipment.args or not isinstance(measure := shipment.args[0], Name)):
            continue
        history, = sources
        coverage = next((r for r in prepared_observations if r['dataset'] == history and r.get('alternatives')), None)
        scope = _same_scope_parent(rules, name, ws)
        if not coverage or not scope or history not in ws.mapping:
            continue
        definition = ws.datasets[history]
        dimension = ws.datasets[scope].key
        if dimension not in definition.fields or definition.period not in definition.fields:
            continue
        divisor = expression.args[1]
        if (not isinstance(divisor, Call) or divisor.fn != 'count_distinct' or not divisor.args
                or not isinstance(month := divisor.args[0], Call) or month.fn != 'month'
                or month.args != (Name(definition.period),)):
            continue
        clauses = [c for c in ws.clauses if c.id in spec['clauses']]
        clause = next((c for c in clauses if re.search(r'本年|当年|本年度', c.quote) and '实际' in c.quote
                       and '月份' in c.quote), None)
        zero = next((c for c in ws.clauses if re.search(r'(?:为|等于)\s*0\b', c.quote)
                     and '不参与' in c.quote and '月份' in c.quote), None)
        if not clause or not zero or measure.id not in definition.fields:
            continue
        if any(other != name and history in _selectors(_expression(v.expr)) for other, v in rules.variables.items()):
            continue
        try:
            data, _ = ws._data(rules)
            allowed = {str(row.values[dimension]).strip() for row in data[scope].rows}
        except (ToolError, KeyError, ValueError):
            continue
        window = ws.period.window(definition.window)
        desired = (dimension, definition.period, measure.id,
                   _group_header(_source_column_name(ws.mapping[scope].columns[dimension])), allowed)
        observed = _active_amounts(data[history], dimension, definition.period, measure.id, window, allowed)
        if not observed:
            continue
        alternative = _historical_candidates(ws, definition, scope, coverage['alternatives'], desired, observed)
        if alternative is None:
            continue
        entry, fields, groups_months, equivalent_sources = alternative
        # A group may have had no activity outside the current month. Only the
        # explicitly corroborated keys are compared, never a fixed month count.
        proposal = {
            'scheme': scheme, 'task': spec['id'],
            'formulas': [{'task': spec['id'], 'changes': {'scope': scope}, 'expected_scope': scope}],
            'datasets': [{'operation': 'define', 'args': {
                'name': history, 'file': entry['file'], 'sheet': entry['sheet'], 'fields': fields,
                'link': dimension, 'period': definition.period, 'window': definition.window,
                'filter': {fields[measure.id]['column']: {'not': ['0']}}, **mapping_policy,
            }}],
        }
        for other, value in rules.variables.items():
            parsed = _expression(value.expr)
            dependent = variable_tasks.get(other)
            if (not dependent or not dependent['clauses'] or value.scope == scope or not isinstance(parsed, Call)
                    or parsed.fn != 'tier_ratio' or name not in names(parsed)):
                continue
            reference = _selectors(parsed)
            if len(reference) != 1:
                continue
            table, = reference
            if table not in ws.datasets or dimension not in ws.datasets[table].fields:
                continue
            if ws.datasets[table].key:
                continue
            proposal['datasets'].append({'operation': 'update', 'args': {
                'name': table, 'changes': {'link': dimension, **mapping_policy},
            }})
            proposal['formulas'].append({'task': dependent['id'], 'changes': {'scope': scope}, 'expected_scope': scope})
        return {
            'anchor': spec['id'], 'scheme': scheme, 'code': 'history_source_current_period_corroborated',
            'source': {'file': entry['file'], 'sheet': entry['sheet']},
            'evidence': {'clauses': [{'id': c.id, 'file': c.file, 'quote': c.quote} for c in (clause, zero, exclusions[0])],
                         'compared_group_months': len(observed), 'historical_group_months': groups_months,
                         'equivalent_sources': equivalent_sources},
            'candidate_call': {'tool': 'submit_construction_repair', 'args': proposal},
        }
    return None
